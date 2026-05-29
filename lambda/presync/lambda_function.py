"""
Cognito DR — Pre-Sync Lambda

Runs hourly via EventBridge Scheduler. Copies user records from the primary
Cognito pool to the DR pool with a placeholder password. Ensures the DR pool
has a complete user roster before any failover occurs.

Runtime: Python 3.12
Region: DR region (e.g., us-west-2)
Timeout: 300 seconds (for large user pools)

Environment Variables:
  PRIMARY_REGION       - Region of the primary pool (e.g., us-east-1)
  PRIMARY_USER_POOL_ID - Pool ID of the primary pool
  DR_REGION            - Region of the DR pool (e.g., us-west-2)
  DR_USER_POOL_ID      - Pool ID of the DR pool

Key Design Decisions:
  - Uses attrs.get("email") as username, NOT user["Username"] (which is a UUID)
  - Placeholder password must satisfy pool password policy
  - AdminSetUserPassword with Permanent=True sets user to CONFIRMED status
  - MessageAction=SUPPRESS prevents welcome emails to users
"""

import boto3
import json
import logging
import os
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PRIMARY_REGION = os.environ["PRIMARY_REGION"]
PRIMARY_USER_POOL_ID = os.environ["PRIMARY_USER_POOL_ID"]
DR_REGION = os.environ["DR_REGION"]
DR_USER_POOL_ID = os.environ["DR_USER_POOL_ID"]

# Placeholder password — must satisfy pool password policy
# (uppercase, lowercase, number, symbol, min 8 chars)
PLACEHOLDER_PASSWORD = "Placeholder1@DR!"

primary = boto3.client("cognito-idp", region_name=PRIMARY_REGION)
dr = boto3.client("cognito-idp", region_name=DR_REGION)


def list_all_users():
    """List all users from the primary pool with pagination."""
    users, token = [], None
    while True:
        kwargs = {"UserPoolId": PRIMARY_USER_POOL_ID, "Limit": 60}
        if token:
            kwargs["PaginationToken"] = token
        resp = primary.list_users(**kwargs)
        users.extend(resp.get("Users", []))
        token = resp.get("PaginationToken")
        if not token:
            break
    return users


def user_exists_in_dr(email):
    """Check if a user already exists in the DR pool."""
    try:
        dr.admin_get_user(UserPoolId=DR_USER_POOL_ID, Username=email)
        return True
    except dr.exceptions.UserNotFoundException:
        return False


def sync_user(user):
    """
    Sync a single user from primary to DR pool.

    Returns: "created", "exists", "skipped", or "failed"
    """
    # Only sync CONFIRMED users
    if user.get("UserStatus") not in ("CONFIRMED", "FORCE_CHANGE_PASSWORD"):
        return "skipped"

    # Extract email from attributes — NOT from user["Username"] which is a UUID
    attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
    email = attrs.get("email")
    if not email:
        logger.warning(f"No email attribute on user {user['Username']}, skipping")
        return "skipped"

    # Skip if already in DR pool
    if user_exists_in_dr(email):
        return "exists"

    try:
        # Create user with placeholder password, suppress welcome email
        dr.admin_create_user(
            UserPoolId=DR_USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            TemporaryPassword=PLACEHOLDER_PASSWORD,
            MessageAction="SUPPRESS",
        )
        # Set permanent password to move from FORCE_CHANGE_PASSWORD to CONFIRMED
        dr.admin_set_user_password(
            UserPoolId=DR_USER_POOL_ID,
            Username=email,
            Password=PLACEHOLDER_PASSWORD,
            Permanent=True,
        )
        logger.info(f"Synced: {email}")
        return "created"
    except dr.exceptions.UsernameExistsException:
        return "exists"
    except Exception as e:
        logger.error(f"Failed {email}: {type(e).__name__}: {e}")
        return "failed"


def handler(event, context):
    """Main handler — invoked by EventBridge Scheduler hourly."""
    logger.info(f"Pre-sync started: {datetime.utcnow().isoformat()}")

    users = list_all_users()
    logger.info(f"Primary pool has {len(users)} users")

    results = {"created": 0, "exists": 0, "skipped": 0, "failed": 0}
    for user in users:
        r = sync_user(user)
        results[r] = results.get(r, 0) + 1

    logger.info(f"Sync complete: {results}")
    return {"statusCode": 200, "body": json.dumps(results)}
