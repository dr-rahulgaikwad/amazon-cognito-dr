# Setup & Configuration Guide

> **Strategy:** Pre-Migration Sync + JIT Migration Lambda  
> **Primary region:** us-east-1 | **DR region:** us-west-2  
> **Runtime:** Python 3.12 | **CLI:** AWS CLI v2  
> **Time to complete:** ~30 minutes

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Known Pitfalls](#known-pitfalls)
3. [Shell Variables](#shell-variables)
4. [Part 1 — Primary Pool (us-east-1)](#part-1--primary-pool-us-east-1)
5. [Part 2 — DR Pool (us-west-2)](#part-2--dr-pool-us-west-2)
6. [Part 3 — IAM Roles](#part-3--iam-roles)
7. [Part 4 — Lambda Functions](#part-4--lambda-functions)
8. [Part 5 — EventBridge Schedule](#part-5--eventbridge-schedule)
9. [Part 6 — Initial Sync & Verify](#part-6--initial-sync--verify)
10. [Cleanup](#cleanup)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

```bash
# Confirm your AWS account ID
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}'

# Confirm CLI can reach both regions
aws cognito-idp list-user-pools --max-results 1 --region us-east-1
aws cognito-idp list-user-pools --max-results 1 --region us-west-2
```

**Required IAM permissions:**
- `cognito-idp:*` (for pool and user management)
- `lambda:*` (for function deployment)
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`
- `secretsmanager:CreateSecret`, `secretsmanager:ReplicateSecretToRegions`
- `scheduler:CreateSchedule`
- `logs:*` (for CloudWatch Logs access)

---

## Known Pitfalls

These were discovered during real execution. Read before starting.

| Pitfall | Symptom | Fix |
|---|---|---|
| `!` in password inside double quotes | `for dquote>` prompt in bash/zsh | Always wrap passwords in **single quotes**: `'Password@2026!'` |
| Multi-line command pasted with line breaks | `zsh: no such file or directory: fileb://...` | Run AWS CLI commands as a **single line** |
| IAM policy has wrong account ID | `AccessDeniedException` on Lambda invoke | Run `aws sts get-caller-identity` first |
| Primary pool stores UUIDs as usernames | `InvalidParameterException` in pre-sync | Use `attrs.get("email")` — not `user["Username"]` |
| Placeholder password missing numbers | `InvalidPasswordException` in pre-sync | Must satisfy pool policy: `Placeholder1@DR!` |
| Secret-enabled client used from CLI | `NotAuthorizedException: SECRET_HASH was not received` | Create a secret-less client for CLI testing |
| Migration Lambda not firing | Login fails with `NotAuthorizedException` for non-existent user | Confirm Lambda is attached: `describe-user-pool --query 'UserPool.LambdaConfig'` |

---

## Shell Variables

Set these at the start of your session. Fill in values as you complete each step.

```bash
# Set your account ID (replace with your actual account)
ACCOUNT_ID="<YOUR_ACCOUNT_ID>"

# These will be filled in as you complete each step
PRIMARY_REGION="us-east-1"
DR_REGION="us-west-2"
PRIMARY_POOL_ID=""          # After Step 1
PRIMARY_CLIENT_ID=""        # After Step 1
DR_POOL_ID=""               # After Step 4
DR_CLIENT_ID=""             # After Step 4
DR_POOL_ARN=""              # After Step 4
MIGRATION_LAMBDA_ARN=""     # After Step 7
PRESYNC_LAMBDA_ARN=""       # After Step 8
```

---

## Part 1 — Primary Pool (us-east-1)

### Step 1 — Create Primary User Pool

```bash
aws cognito-idp create-user-pool \
  --pool-name cognito-dr-primary \
  --username-attributes email \
  --username-configuration CaseSensitive=false \
  --admin-create-user-config AllowAdminCreateUserOnly=true \
  --auto-verified-attributes email \
  --policies 'PasswordPolicy={MinimumLength=8,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=true,TemporaryPasswordValidityDays=7}' \
  --region "$PRIMARY_REGION" \
  --query 'UserPool.{Id:Id,Arn:Arn}'
```

Record the output:
```bash
PRIMARY_POOL_ID="us-east-1_XXXXXXXXX"   # Replace with Id from output
```

### Step 2 — Create App Client (with secret, for server-side apps)

```bash
aws cognito-idp create-user-pool-client \
  --user-pool-id "$PRIMARY_POOL_ID" \
  --client-name primary-app-client \
  --generate-secret \
  --explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --region "$PRIMARY_REGION" \
  --query 'UserPoolClient.{ClientId:ClientId,ClientSecret:ClientSecret}'
```

Record:
```bash
PRIMARY_CLIENT_ID="<ClientId from output>"
```

### Step 2b — Create CLI Test Client (no secret, for testing)

> **Why?** The AWS CLI cannot compute the `SECRET_HASH` that secret-enabled clients require. Create a secret-less client for all CLI-based testing.

```bash
PRIMARY_CLIENT_ID_CLI=$(aws cognito-idp create-user-pool-client \
  --user-pool-id "$PRIMARY_POOL_ID" \
  --client-name primary-cli-client \
  --no-generate-secret \
  --explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --region "$PRIMARY_REGION" \
  --query 'UserPoolClient.ClientId' --output text)
echo "Primary CLI Client: $PRIMARY_CLIENT_ID_CLI"
```

### Step 3 — Create Test Users

```bash
for USER in alice@example.com bob@example.com charlie@example.com; do
  aws cognito-idp admin-create-user \
    --user-pool-id "$PRIMARY_POOL_ID" \
    --username "$USER" \
    --user-attributes Name=email,Value="$USER" Name=email_verified,Value=true \
    --message-action SUPPRESS \
    --temporary-password 'TempPass1@123' \
    --region "$PRIMARY_REGION" \
    --query 'User.{Username:Username,Status:UserStatus}'
done
```

Set permanent passwords (moves users from `FORCE_CHANGE_PASSWORD` to `CONFIRMED`):

```bash
for USER in alice@example.com bob@example.com charlie@example.com; do
  aws cognito-idp admin-set-user-password \
    --user-pool-id "$PRIMARY_POOL_ID" \
    --username "$USER" \
    --password 'PermanentPass@2026!' \
    --permanent \
    --region "$PRIMARY_REGION"
  echo "Confirmed: $USER"
done
```

Verify:
```bash
aws cognito-idp list-users \
  --user-pool-id "$PRIMARY_POOL_ID" \
  --region "$PRIMARY_REGION" \
  --query 'Users[].{Email:Attributes[?Name==`email`].Value|[0],Status:UserStatus}'
```

**Expected:** All three users with `Status: CONFIRMED`

### Step 3b — Store Primary Config in Secrets Manager

```bash
aws secretsmanager create-secret \
  --name cognito-dr/primary-config \
  --description "Cognito primary pool credentials for DR" \
  --secret-string "{\"primary_user_pool_id\":\"$PRIMARY_POOL_ID\",\"primary_client_id\":\"$PRIMARY_CLIENT_ID_CLI\"}" \
  --region "$PRIMARY_REGION" \
  --query '{ARN:ARN,Name:Name}'
```

Replicate to DR region:
```bash
aws secretsmanager replicate-secret-to-regions \
  --secret-id cognito-dr/primary-config \
  --add-replica-regions Region="$DR_REGION" \
  --region "$PRIMARY_REGION"
```

Verify replication:
```bash
aws secretsmanager describe-secret \
  --secret-id cognito-dr/primary-config \
  --region "$PRIMARY_REGION" \
  --query 'ReplicationStatus'
# Expected: [{"Region": "us-west-2", "Status": "InSync"}]
```

---

## Part 2 — DR Pool (us-west-2)

### Step 4 — Create DR User Pool

```bash
aws cognito-idp create-user-pool \
  --pool-name cognito-dr-standby \
  --username-attributes email \
  --username-configuration CaseSensitive=false \
  --admin-create-user-config AllowAdminCreateUserOnly=true \
  --auto-verified-attributes email \
  --policies 'PasswordPolicy={MinimumLength=8,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=true,TemporaryPasswordValidityDays=7}' \
  --region "$DR_REGION" \
  --query 'UserPool.{Id:Id,Arn:Arn}'
```

Record:
```bash
DR_POOL_ID="us-west-2_YYYYYYYYY"    # Replace with Id from output
DR_POOL_ARN="arn:aws:cognito-idp:$DR_REGION:$ACCOUNT_ID:userpool/$DR_POOL_ID"
```

### Step 5 — Create DR App Client

```bash
aws cognito-idp create-user-pool-client \
  --user-pool-id "$DR_POOL_ID" \
  --client-name dr-app-client \
  --generate-secret \
  --explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --region "$DR_REGION" \
  --query 'UserPoolClient.{ClientId:ClientId,ClientSecret:ClientSecret}'
```

Create CLI test client:
```bash
DR_CLIENT_ID_CLI=$(aws cognito-idp create-user-pool-client \
  --user-pool-id "$DR_POOL_ID" \
  --client-name dr-cli-client \
  --no-generate-secret \
  --explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --region "$DR_REGION" \
  --query 'UserPoolClient.ClientId' --output text)
echo "DR CLI Client: $DR_CLIENT_ID_CLI"
```

---

## Part 3 — IAM Roles

> **Important:** Confirm `$ACCOUNT_ID` is set correctly: `echo $ACCOUNT_ID`

### Step 6a — Migration Lambda Role

```bash
aws iam create-role \
  --role-name cognito-migration-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  --query 'Role.Arn'

aws iam attach-role-policy \
  --role-name cognito-migration-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

```bash
aws iam put-role-policy \
  --role-name cognito-migration-role \
  --policy-name cognito-migration-policy \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Sid\": \"VerifyAgainstPrimaryPool\",
        \"Effect\": \"Allow\",
        \"Action\": [\"cognito-idp:AdminInitiateAuth\", \"cognito-idp:AdminGetUser\"],
        \"Resource\": \"arn:aws:cognito-idp:$PRIMARY_REGION:$ACCOUNT_ID:userpool/$PRIMARY_POOL_ID\"
      },
      {
        \"Sid\": \"ReadPrimaryConfig\",
        \"Effect\": \"Allow\",
        \"Action\": \"secretsmanager:GetSecretValue\",
        \"Resource\": [
          \"arn:aws:secretsmanager:$PRIMARY_REGION:$ACCOUNT_ID:secret:cognito-dr/primary-config*\",
          \"arn:aws:secretsmanager:$DR_REGION:$ACCOUNT_ID:secret:cognito-dr/primary-config*\"
        ]
      }
    ]
  }"
```

### Step 6b — Pre-Sync Lambda Role

```bash
aws iam create-role \
  --role-name cognito-presync-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  --query 'Role.Arn'

aws iam attach-role-policy \
  --role-name cognito-presync-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

```bash
aws iam put-role-policy \
  --role-name cognito-presync-role \
  --policy-name cognito-presync-policy \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Sid\": \"ReadPrimaryPool\",
        \"Effect\": \"Allow\",
        \"Action\": [\"cognito-idp:ListUsers\", \"cognito-idp:AdminGetUser\"],
        \"Resource\": \"arn:aws:cognito-idp:$PRIMARY_REGION:$ACCOUNT_ID:userpool/$PRIMARY_POOL_ID\"
      },
      {
        \"Sid\": \"WriteDRPool\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"cognito-idp:AdminCreateUser\",
          \"cognito-idp:AdminSetUserPassword\",
          \"cognito-idp:AdminGetUser\",
          \"cognito-idp:ListUsers\"
        ],
        \"Resource\": \"arn:aws:cognito-idp:$DR_REGION:$ACCOUNT_ID:userpool/$DR_POOL_ID\"
      },
      {
        \"Sid\": \"ReadConfig\",
        \"Effect\": \"Allow\",
        \"Action\": \"secretsmanager:GetSecretValue\",
        \"Resource\": [
          \"arn:aws:secretsmanager:$PRIMARY_REGION:$ACCOUNT_ID:secret:cognito-dr/primary-config*\",
          \"arn:aws:secretsmanager:$DR_REGION:$ACCOUNT_ID:secret:cognito-dr/primary-config*\"
        ]
      }
    ]
  }"
```

Verify both roles:
```bash
aws iam get-role-policy --role-name cognito-migration-role --policy-name cognito-migration-policy --query 'PolicyDocument'
aws iam get-role-policy --role-name cognito-presync-role --policy-name cognito-presync-policy --query 'PolicyDocument'
```

---

## Part 4 — Lambda Functions

> **Important:** All `aws lambda` commands must be run as a **single line**. Multi-line pastes break on `--zip-file`.

### Step 7 — Deploy Migration Lambda

Package the code:
```bash
cd lambda/migration && zip -j /tmp/migration-lambda.zip lambda_function.py
```

Deploy:
```bash
MIGRATION_ROLE_ARN=$(aws iam get-role --role-name cognito-migration-role --query 'Role.Arn' --output text)
```

```bash
aws lambda create-function --function-name cognito-migration-trigger --runtime python3.12 --role "$MIGRATION_ROLE_ARN" --handler lambda_function.handler --zip-file fileb:///tmp/migration-lambda.zip --timeout 10 --environment "Variables={PRIMARY_REGION=$PRIMARY_REGION,PRIMARY_USER_POOL_ID=$PRIMARY_POOL_ID,PRIMARY_CLIENT_ID=$PRIMARY_CLIENT_ID_CLI}" --region "$DR_REGION" --query 'FunctionArn'
```

Record:
```bash
MIGRATION_LAMBDA_ARN=$(aws lambda get-function --function-name cognito-migration-trigger --region "$DR_REGION" --query 'Configuration.FunctionArn' --output text)
echo "$MIGRATION_LAMBDA_ARN"
```

Add resource-based policy (restrict invocation to DR pool only):
```bash
aws lambda add-permission --function-name cognito-migration-trigger --statement-id AllowCognitoInvoke --action lambda:InvokeFunction --principal cognito-idp.amazonaws.com --source-arn "$DR_POOL_ARN" --region "$DR_REGION"
```

Attach to DR pool:
```bash
aws cognito-idp update-user-pool --user-pool-id "$DR_POOL_ID" --lambda-config "UserMigration=$MIGRATION_LAMBDA_ARN" --region "$DR_REGION"
```

Verify:
```bash
aws cognito-idp describe-user-pool --user-pool-id "$DR_POOL_ID" --region "$DR_REGION" --query 'UserPool.LambdaConfig'
# Expected: {"UserMigration": "arn:aws:lambda:us-west-2:...:function:cognito-migration-trigger"}
```

### Step 8 — Deploy Pre-Sync Lambda

Package:
```bash
cd lambda/presync && zip -j /tmp/presync-lambda.zip lambda_function.py
```

Deploy:
```bash
PRESYNC_ROLE_ARN=$(aws iam get-role --role-name cognito-presync-role --query 'Role.Arn' --output text)
```

```bash
aws lambda create-function --function-name cognito-presync --runtime python3.12 --role "$PRESYNC_ROLE_ARN" --handler lambda_function.handler --zip-file fileb:///tmp/presync-lambda.zip --timeout 300 --environment "Variables={PRIMARY_REGION=$PRIMARY_REGION,PRIMARY_USER_POOL_ID=$PRIMARY_POOL_ID,DR_REGION=$DR_REGION,DR_USER_POOL_ID=$DR_POOL_ID}" --region "$DR_REGION" --query 'FunctionArn'
```

Record:
```bash
PRESYNC_LAMBDA_ARN=$(aws lambda get-function --function-name cognito-presync --region "$DR_REGION" --query 'Configuration.FunctionArn' --output text)
echo "$PRESYNC_LAMBDA_ARN"
```

---

## Part 5 — EventBridge Schedule

### Step 9 — Create Scheduler IAM Role

```bash
aws iam create-role \
  --role-name cognito-presync-scheduler-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  --query 'Role.Arn'
```

```bash
aws iam put-role-policy \
  --role-name cognito-presync-scheduler-role \
  --policy-name allow-invoke-presync \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"$PRESYNC_LAMBDA_ARN\"}]}"
```

### Step 10 — Create the Schedule

```bash
SCHEDULER_ROLE_ARN=$(aws iam get-role --role-name cognito-presync-scheduler-role --query 'Role.Arn' --output text)
```

```bash
aws scheduler create-schedule --name cognito-presync-hourly --schedule-expression "rate(1 hour)" --flexible-time-window Mode=OFF --target "{\"Arn\":\"$PRESYNC_LAMBDA_ARN\",\"RoleArn\":\"$SCHEDULER_ROLE_ARN\",\"Input\":\"{}\"}" --region "$DR_REGION" --query 'ScheduleArn'
```

Verify:
```bash
aws scheduler get-schedule --name cognito-presync-hourly --region "$DR_REGION" --query '{State:State,Expression:ScheduleExpression}'
# Expected: {"State": "ENABLED", "Expression": "rate(1 hour)"}
```

---

## Part 6 — Initial Sync & Verify

### Step 11 — Run Pre-Sync Manually

```bash
aws lambda invoke --function-name cognito-presync --payload '{}' --cli-binary-format raw-in-base64-out --region "$DR_REGION" /tmp/presync-output.json && cat /tmp/presync-output.json
```

**Expected:**
```json
{"statusCode": 200, "body": "{\"created\": 3, \"exists\": 0, \"skipped\": 0, \"failed\": 0}"}
```

If you see failures, check logs:
```bash
aws logs tail /aws/lambda/cognito-presync --region "$DR_REGION" --since 5m --format short
```

### Step 12 — Verify DR Pool Users

```bash
aws cognito-idp list-users \
  --user-pool-id "$DR_POOL_ID" \
  --region "$DR_REGION" \
  --query 'Users[].{Email:Attributes[?Name==`email`].Value|[0],Status:UserStatus}'
```

**Expected:** alice, bob, charlie — all `CONFIRMED` ✅

### Step 13 — Test JIT Migration

Delete a user from DR pool and verify migration works:

```bash
# Remove alice from DR pool
aws cognito-idp admin-delete-user --user-pool-id "$DR_POOL_ID" --username alice@example.com --region "$DR_REGION"

# Login to DR pool — Migration Lambda should fire
aws cognito-idp admin-initiate-auth --user-pool-id "$DR_POOL_ID" --client-id "$DR_CLIENT_ID_CLI" --auth-flow ADMIN_USER_PASSWORD_AUTH --auth-parameters USERNAME=alice@example.com,PASSWORD='PermanentPass@2026!' --region "$DR_REGION" --query 'AuthenticationResult.{AccessToken:AccessToken,ExpiresIn:ExpiresIn}'
```

**Expected:** `AccessToken` returned ✅ — JIT migration successful

Verify alice was created in DR pool:
```bash
aws cognito-idp admin-get-user --user-pool-id "$DR_POOL_ID" --username alice@example.com --region "$DR_REGION" --query '{Username:Username,Status:UserStatus}'
# Expected: {"Username": "alice@example.com", "Status": "CONFIRMED"}
```

---

## Setup Complete ✅

Your Cognito DR infrastructure is now operational:

- ✅ Primary pool with test users in us-east-1
- ✅ DR pool with pre-synced users in us-west-2
- ✅ Migration Lambda attached to DR pool for JIT migration
- ✅ Pre-Sync Lambda running hourly via EventBridge
- ✅ Secrets Manager replicated across regions

**Next:** Run the full [DR Testing Guide](testing.md) to validate all failover scenarios.

---

## Cleanup

Run in this exact order to avoid dependency errors:

```bash
# 1. Delete EventBridge schedule
aws scheduler delete-schedule --name cognito-presync-hourly --region "$DR_REGION"

# 2. Remove Lambda trigger from DR pool
aws cognito-idp update-user-pool --user-pool-id "$DR_POOL_ID" --lambda-config '{}' --region "$DR_REGION"

# 3. Delete Lambda functions
aws lambda delete-function --function-name cognito-migration-trigger --region "$DR_REGION"
aws lambda delete-function --function-name cognito-presync --region "$DR_REGION"

# 4. Delete IAM roles
aws iam detach-role-policy --role-name cognito-migration-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role-policy --role-name cognito-migration-role --policy-name cognito-migration-policy
aws iam delete-role --role-name cognito-migration-role

aws iam detach-role-policy --role-name cognito-presync-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role-policy --role-name cognito-presync-role --policy-name cognito-presync-policy
aws iam delete-role --role-name cognito-presync-role

aws iam delete-role-policy --role-name cognito-presync-scheduler-role --policy-name allow-invoke-presync
aws iam delete-role --role-name cognito-presync-scheduler-role

# 5. Delete secret (7-day recovery window)
aws secretsmanager delete-secret --secret-id cognito-dr/primary-config --recovery-window-in-days 7 --region "$PRIMARY_REGION"

# 6. Delete user pools
aws cognito-idp delete-user-pool --user-pool-id "$DR_POOL_ID" --region "$DR_REGION"
aws cognito-idp delete-user-pool --user-pool-id "$PRIMARY_POOL_ID" --region "$PRIMARY_REGION"

# 7. Clean temp files
rm -f /tmp/migration-lambda.zip /tmp/presync-lambda.zip /tmp/presync-output.json

echo "Cleanup complete"
```

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `for dquote>` prompt | `!` in password inside double quotes | Use single quotes: `'PermanentPass@2026!'` |
| `zsh: no such file or directory: fileb://...` | Multi-line paste broke `--zip-file` | Run as single line |
| `AccessDeniedException: ListUsers` | IAM policy wrong account ID | Verify `echo $ACCOUNT_ID` matches your account |
| `InvalidParameterException` in pre-sync | Lambda using UUID as username | Ensure code uses `attrs.get("email")` |
| `InvalidPasswordException` in pre-sync | Placeholder fails pool policy | Use `Placeholder1@DR!` (has uppercase, lowercase, number, symbol) |
| `NotAuthorizedException: SECRET_HASH` | Using secret-enabled client from CLI | Use the `*-cli-client` (no secret) for all CLI commands |
| Migration Lambda not firing | Lambda not attached to DR pool | Run: `aws cognito-idp describe-user-pool --user-pool-id "$DR_POOL_ID" --region "$DR_REGION" --query 'UserPool.LambdaConfig'` |
| `list-users` returns `[]` | Shell variable not set | Re-set `$DR_POOL_ID` in your terminal |
| Pre-sync shows `failed: N` | IAM or code issue | Check: `aws logs tail /aws/lambda/cognito-presync --region "$DR_REGION" --since 5m` |

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Lambda in VPC? | No | Cognito APIs are public endpoints. VPC adds NAT Gateway cost (~$99/month) with no security benefit. |
| Auth flow | ADMIN_USER_PASSWORD_AUTH | Required — SRP flow does not send password to Lambda. Must be enabled on primary pool app client. |
| Pre-sync password | AdminSetUserPassword Permanent=true | Only AWS-native way to set a known password without forcing reset. Sets user to CONFIRMED. |
| Sync frequency | Hourly | Balances RPO (1 hour for new users) against Lambda cost. Configurable via EventBridge schedule expression. |
| Secret storage | Secrets Manager replicated | Primary client ID stored securely, replicated so DR Lambda reads locally even if primary region is degraded. |
| Username in DR pool | email attribute | Cognito stores internal UUIDs as Username in email-based pools. Must use `attrs.get("email")` in sync Lambda. |
| Separate IAM roles | One per Lambda | Least-privilege: each role has only the permissions its Lambda needs. |
