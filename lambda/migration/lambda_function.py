"""
Cognito DR — JIT Migration Lambda

Triggered by Cognito User Pool when a user attempts to log in but does not exist
in the DR pool. Verifies credentials against the primary pool and returns user
attributes so Cognito can create the user in the DR pool.

Trigger: UserMigration_Authentication, UserMigration_ForgotPassword
Runtime: Python 3.12
Region: DR region (e.g., us-west-2)

Environment Variables:
  PRIMARY_REGION       - Region of the primary Cognito pool (e.g., us-east-1)
  PRIMARY_USER_POOL_ID - Pool ID of the primary pool (e.g., us-east-1_XXXXXXXXX)
  PRIMARY_CLIENT_ID    - App client ID (must be secret-less for AdminInitiateAuth)
"""

import boto3
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PRIMARY_REGION = os.environ["PRIMARY_REGION"]
PRIMARY_USER_POOL_ID = os.environ["PRIMARY_USER_POOL_ID"]
PRIMARY_CLIENT_ID = os.environ["PRIMARY_CLIENT_ID"]

cognito = boto3.client("cognito-idp", region_name=PRIMARY_REGION)


def handler(event, context):
    trigger = event.get("triggerSource", "")
    username = event.get("userName", "")
    logger.info(f"Migration trigger: {trigger} for user: {username}")

    if trigger == "UserMigration_Authentication":
        password = event.get("request", {}).get("password", "")
        try:
            # Verify credentials against primary pool
            cognito.admin_initiate_auth(
                UserPoolId=PRIMARY_USER_POOL_ID,
                ClientId=PRIMARY_CLIENT_ID,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": username, "PASSWORD": password},
            )
            # Fetch user attributes
            user = cognito.admin_get_user(
                UserPoolId=PRIMARY_USER_POOL_ID, Username=username
            )
            attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}

            # Return attributes to Cognito — it will create the user in DR pool
            event["response"]["userAttributes"] = {
                "email": attrs.get("email", username),
                "email_verified": "true",
            }
            event["response"]["finalUserStatus"] = "CONFIRMED"
            event["response"]["messageAction"] = "SUPPRESS"
            logger.info(f"Migrated: {username}")
            return event

        except cognito.exceptions.NotAuthorizedException:
            raise Exception("Bad credentials")
        except cognito.exceptions.UserNotFoundException:
            raise Exception("Bad credentials")
        except Exception as e:
            logger.error(f"Migration error: {type(e).__name__}")
            raise Exception("Migration failed")

    elif trigger == "UserMigration_ForgotPassword":
        try:
            user = cognito.admin_get_user(
                UserPoolId=PRIMARY_USER_POOL_ID, Username=username
            )
            attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
            event["response"]["userAttributes"] = {
                "email": attrs.get("email", username),
                "email_verified": "true",
            }
            event["response"]["finalUserStatus"] = "RESET_REQUIRED"
            event["response"]["messageAction"] = "SUPPRESS"
            return event
        except Exception as e:
            logger.error(f"ForgotPassword error: {type(e).__name__}")
            raise Exception("User not found")

    return event
