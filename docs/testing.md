# DR Testing Guide

## Shell Variables — Set Before Testing

```bash
ACCOUNT_ID="<YOUR_ACCOUNT_ID>"
PRIMARY_REGION="us-east-1"
DR_REGION="us-west-2"
PRIMARY_POOL_ID="<YOUR_PRIMARY_POOL_ID>"
DR_POOL_ID="<YOUR_DR_POOL_ID>"
PRIMARY_CLIENT_ID_CLI="<YOUR_PRIMARY_CLI_CLIENT_ID>"
DR_CLIENT_ID_CLI="<YOUR_DR_CLI_CLIENT_ID>"
```

---

## Test 0 — Verify Infrastructure State

**Rationale:** Confirm the full stack is wired correctly before running DR scenarios.

```bash
# 0a — Confirm both pools exist and Lambda is attached to DR pool
aws cognito-idp describe-user-pool \
  --user-pool-id "$PRIMARY_POOL_ID" --region "$PRIMARY_REGION" \
  --query 'UserPool.{Name:Name,Id:Id,LambdaConfig:LambdaConfig}'

aws cognito-idp describe-user-pool \
  --user-pool-id "$DR_POOL_ID" --region "$DR_REGION" \
  --query 'UserPool.{Name:Name,Id:Id,LambdaConfig:LambdaConfig}'
```

**Expected:**
- Primary: `LambdaConfig: {}` (no trigger — correct)
- DR: `LambdaConfig.UserMigration` points to `cognito-migration-trigger` ✅

```bash
# 0b — Confirm users in primary pool
aws cognito-idp list-users \
  --user-pool-id "$PRIMARY_POOL_ID" --region "$PRIMARY_REGION" \
  --query 'Users[].{Email:Attributes[?Name==`email`].Value|[0],Status:UserStatus}'
```

**Expected:** alice, bob, charlie — all `CONFIRMED` ✅

```bash
# 0c — Confirm users in DR pool (pre-synced)
aws cognito-idp list-users \
  --user-pool-id "$DR_POOL_ID" --region "$DR_REGION" \
  --query 'Users[].{Email:Attributes[?Name==`email`].Value|[0],Status:UserStatus}'
```

**Expected:** alice, bob, charlie — all `CONFIRMED` ✅

```bash
# 0d — Confirm EventBridge schedule is active
aws scheduler get-schedule \
  --name cognito-presync-hourly --region "$DR_REGION" \
  --query '{State:State,Expression:ScheduleExpression}'
```

**Expected:** `{"State": "ENABLED", "Expression": "rate(1 hour)"}` ✅

```bash
# 0e — Confirm Migration Lambda environment
aws lambda get-function-configuration \
  --function-name cognito-migration-trigger --region "$DR_REGION" \
  --query '{Runtime:Runtime,Timeout:Timeout,Env:Environment.Variables}'
```

**Expected:** `PRIMARY_USER_POOL_ID` and `PRIMARY_CLIENT_ID` are set correctly ✅

---

## Test 1 — Baseline: Login to Primary Pool

**Rationale:** Proves the primary pool and credentials work. Establishes what a successful auth response looks like.

```bash
aws cognito-idp admin-initiate-auth \
  --user-pool-id "$PRIMARY_POOL_ID" \
  --client-id "$PRIMARY_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=alice@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$PRIMARY_REGION" \
  --query 'AuthenticationResult.{AccessToken:AccessToken,ExpiresIn:ExpiresIn,TokenType:TokenType}'
```

**Expected:**
```json
{
    "AccessToken": "eyJraWQiOiJ...",
    "ExpiresIn": 3600,
    "TokenType": "Bearer"
}
```

✅ **PASS** if you receive an AccessToken with ExpiresIn: 3600

---

## Test 2 — Pre-Sync Lambda: Populate DR Pool

**Rationale:** The pre-sync Lambda is the proactive component. It runs hourly and copies user records to the DR pool with a placeholder password. This test runs it manually.

```bash
# 2a — Trigger pre-sync manually
aws lambda invoke \
  --function-name cognito-presync \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  --region "$DR_REGION" \
  /tmp/presync-output.json && cat /tmp/presync-output.json
```

**Expected:**
```json
{"statusCode": 200, "body": "{\"created\": 0, \"exists\": 3, \"skipped\": 0, \"failed\": 0}"}
```

> `exists: 3` means all users were already synced. `created: 3` on first run.

```bash
# 2b — Verify DR pool users
aws cognito-idp list-users \
  --user-pool-id "$DR_POOL_ID" --region "$DR_REGION" \
  --query 'Users[].{Email:Attributes[?Name==`email`].Value|[0],Status:UserStatus}'
```

```bash
# 2c — Check Lambda logs
aws logs tail /aws/lambda/cognito-presync \
  --region "$DR_REGION" --since 5m --format short
```

✅ **PASS** if `failed: 0` and all users show as `CONFIRMED` in DR pool

---

## Test 3 — JIT Migration: User Not in DR Pool (Primary Available)

**Rationale:** This is the core DR mechanism. When a user not in the DR pool logs in, Cognito invokes the Migration Lambda, which verifies credentials against the primary pool and creates the user transparently.

```bash
# 3a — Remove alice from DR pool
aws cognito-idp admin-delete-user \
  --user-pool-id "$DR_POOL_ID" \
  --username alice@example.com \
  --region "$DR_REGION"

echo "alice removed from DR pool"
```

```bash
# 3b — Confirm alice is gone
aws cognito-idp admin-get-user \
  --user-pool-id "$DR_POOL_ID" \
  --username alice@example.com \
  --region "$DR_REGION"
# Expected: UserNotFoundException
```

```bash
# 3c — Login to DR pool — Migration Lambda fires automatically
aws cognito-idp admin-initiate-auth \
  --user-pool-id "$DR_POOL_ID" \
  --client-id "$DR_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=alice@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$DR_REGION" \
  --query 'AuthenticationResult.{AccessToken:AccessToken,ExpiresIn:ExpiresIn}'
```

**Expected:** AccessToken returned ✅

```bash
# 3d — Verify alice now exists in DR pool
aws cognito-idp admin-get-user \
  --user-pool-id "$DR_POOL_ID" \
  --username alice@example.com \
  --region "$DR_REGION" \
  --query '{Username:Username,Status:UserStatus}'
```

**Expected:** `Status: CONFIRMED` ✅

```bash
# 3e — Check Migration Lambda logs
aws logs tail /aws/lambda/cognito-migration-trigger \
  --region "$DR_REGION" --since 5m --format short
```

**Expected log:** `Migrated: alice@example.com`

✅ **PASS** if login succeeds and alice is created in DR pool via JIT migration

---

## Test 4 — Pre-Migrated User Login Limitation

**Rationale:** Exposes the key limitation. Pre-synced users have a placeholder password. When they log in with their real password, the DR pool finds them, sees a mismatch, and returns `NotAuthorizedException` — it does NOT invoke the Migration Lambda (Lambda only fires for users who don't exist).

```bash
# 4a — Confirm bob exists in DR pool
aws cognito-idp admin-get-user \
  --user-pool-id "$DR_POOL_ID" \
  --username bob@example.com \
  --region "$DR_REGION" \
  --query '{Username:Username,Status:UserStatus}'
```

**Expected:** `Status: CONFIRMED` (bob exists with placeholder password)

```bash
# 4b — Login with real password — WILL FAIL (expected behaviour)
aws cognito-idp admin-initiate-auth \
  --user-pool-id "$DR_POOL_ID" \
  --client-id "$DR_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=bob@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$DR_REGION"
```

**Expected:** `NotAuthorizedException: Incorrect username or password.` ❌

> **This is the documented limitation.** Bob exists with placeholder password → mismatch → Lambda NOT invoked.

```bash
# 4c — Workaround: delete bob, then JIT migration handles it
aws cognito-idp admin-delete-user \
  --user-pool-id "$DR_POOL_ID" \
  --username bob@example.com \
  --region "$DR_REGION"

aws cognito-idp admin-initiate-auth \
  --user-pool-id "$DR_POOL_ID" \
  --client-id "$DR_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=bob@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$DR_REGION" \
  --query 'AuthenticationResult.AccessToken'
```

**Expected:** AccessToken returned ✅ (JIT migration after deletion)

✅ **PASS** if 4b fails with NotAuthorizedException AND 4c succeeds

---

## Test 5 — Simulate Primary Region Outage

**Rationale:** Demonstrates what happens during a complete us-east-1 outage. The Migration Lambda cannot reach the primary pool to verify credentials.

```bash
# 5a — Break the Migration Lambda (simulate primary unreachable)
aws lambda update-function-configuration \
  --function-name cognito-migration-trigger \
  --environment "Variables={PRIMARY_REGION=$PRIMARY_REGION,PRIMARY_USER_POOL_ID=us-east-1_INVALID,PRIMARY_CLIENT_ID=$PRIMARY_CLIENT_ID_CLI}" \
  --region "$DR_REGION" \
  --query 'LastUpdateStatus'
```

**Expected:** `"Successful"` — config updated

```bash
sleep 5  # Wait for Lambda update to propagate
```

```bash
# 5b — Test A: charlie exists in DR (placeholder) — login fails at password check
aws cognito-idp admin-initiate-auth \
  --user-pool-id "$DR_POOL_ID" \
  --client-id "$DR_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=charlie@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$DR_REGION"
```

**Expected:** `NotAuthorizedException` ❌ (password mismatch, Lambda never invoked)

```bash
# 5c — Test B: delete charlie, then try — Lambda fires but primary unreachable
aws cognito-idp admin-delete-user \
  --user-pool-id "$DR_POOL_ID" \
  --username charlie@example.com \
  --region "$DR_REGION"

aws cognito-idp admin-initiate-auth \
  --user-pool-id "$DR_POOL_ID" \
  --client-id "$DR_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=charlie@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$DR_REGION"
```

**Expected:** `NotAuthorizedException` ❌ (Lambda invoked but primary call fails)

```bash
# 5d — Check Lambda logs to confirm it was invoked
aws logs tail /aws/lambda/cognito-migration-trigger \
  --region "$DR_REGION" --since 5m --format short
```

**Expected log:** `Migration error: ResourceNotFoundException` (or similar)

```bash
# ⚠️  RESTORE IMMEDIATELY — required before continuing
aws lambda update-function-configuration \
  --function-name cognito-migration-trigger \
  --environment "Variables={PRIMARY_REGION=$PRIMARY_REGION,PRIMARY_USER_POOL_ID=$PRIMARY_POOL_ID,PRIMARY_CLIENT_ID=$PRIMARY_CLIENT_ID_CLI}" \
  --region "$DR_REGION" \
  --query 'LastUpdateStatus'

echo "Migration Lambda RESTORED ✅"
```

✅ **PASS** if both 5b and 5c fail with NotAuthorizedException, and Lambda is restored

---

## Test 6 — Token Portability: Proves Re-Login After Failover

**Rationale:** JWT tokens from the primary pool are signed with its RSA private key. The DR pool has a different key pair. Refresh tokens from the primary are cryptographically invalid in the DR pool. Users must re-authenticate after failover.

```bash
# 6a — Get tokens from primary pool
PRIMARY_AUTH=$(aws cognito-idp admin-initiate-auth \
  --user-pool-id "$PRIMARY_POOL_ID" \
  --client-id "$PRIMARY_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=alice@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$PRIMARY_REGION")

echo "$PRIMARY_AUTH" | python3 -c "
import sys, json
r = json.load(sys.stdin)['AuthenticationResult']
print('AccessToken (first 50 chars):', r['AccessToken'][:50])
print('RefreshToken (first 50 chars):', r['RefreshToken'][:50])
print('ExpiresIn:', r['ExpiresIn'])
"
```

```bash
# 6b — Extract refresh token
PRIMARY_REFRESH=$(echo "$PRIMARY_AUTH" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['AuthenticationResult']['RefreshToken'])")

echo "Refresh token captured (length: ${#PRIMARY_REFRESH})"
```

```bash
# 6c — Try to use primary refresh token against DR pool
aws cognito-idp initiate-auth \
  --client-id "$DR_CLIENT_ID_CLI" \
  --auth-flow REFRESH_TOKEN_AUTH \
  --auth-parameters REFRESH_TOKEN="$PRIMARY_REFRESH" \
  --region "$DR_REGION"
```

**Expected:** `NotAuthorizedException` ❌ — token signed by primary pool's RSA key, DR pool cannot verify it

```bash
# 6d — Prove fresh login to DR pool works (re-login after failover)
aws cognito-idp admin-initiate-auth \
  --user-pool-id "$DR_POOL_ID" \
  --client-id "$DR_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=alice@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$DR_REGION" \
  --query 'AuthenticationResult.{AccessToken:AccessToken,ExpiresIn:ExpiresIn}'
```

**Expected:** New AccessToken from DR pool ✅

✅ **PASS** if 6c fails with NotAuthorizedException AND 6d succeeds

---

## Test 7 — New User Created After Last Sync (RPO Demonstration)

**Rationale:** Users created after the last hourly sync don't exist in the DR pool. The Migration Lambda covers this gap as long as the primary is reachable. This demonstrates the RPO window.

```bash
# 7a — Create a new user in primary pool
aws cognito-idp admin-create-user \
  --user-pool-id "$PRIMARY_POOL_ID" \
  --username dave@example.com \
  --user-attributes Name=email,Value=dave@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS \
  --temporary-password 'TempPass1@123' \
  --region "$PRIMARY_REGION" \
  --query 'User.{Username:Username,Status:UserStatus}'

aws cognito-idp admin-set-user-password \
  --user-pool-id "$PRIMARY_POOL_ID" \
  --username dave@example.com \
  --password 'PermanentPass@2026!' \
  --permanent \
  --region "$PRIMARY_REGION"

echo "dave created in primary pool"
```

```bash
# 7b — Confirm dave does NOT exist in DR pool
aws cognito-idp admin-get-user \
  --user-pool-id "$DR_POOL_ID" \
  --username dave@example.com \
  --region "$DR_REGION"
# Expected: UserNotFoundException
```

```bash
# 7c — Dave logs in to DR pool — JIT migration covers the sync gap
aws cognito-idp admin-initiate-auth \
  --user-pool-id "$DR_POOL_ID" \
  --client-id "$DR_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=dave@example.com,PASSWORD='PermanentPass@2026!' \
  --region "$DR_REGION" \
  --query 'AuthenticationResult.AccessToken'
```

**Expected:** AccessToken returned ✅ — JIT migration covered the RPO gap

```bash
# 7d — Cleanup dave from both pools
aws cognito-idp admin-delete-user \
  --user-pool-id "$PRIMARY_POOL_ID" \
  --username dave@example.com \
  --region "$PRIMARY_REGION"

aws cognito-idp admin-delete-user \
  --user-pool-id "$DR_POOL_ID" \
  --username dave@example.com \
  --region "$DR_REGION"

echo "dave cleaned up"
```

✅ **PASS** if dave can log in to DR pool via JIT migration without being pre-synced

---

## Test 8 — Restore Full State

**Rationale:** Restore the DR pool to a clean state after all tests.

```bash
# 8a — Re-run pre-sync to restore deleted users
aws lambda invoke \
  --function-name cognito-presync \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  --region "$DR_REGION" \
  /tmp/presync-output.json && cat /tmp/presync-output.json
```

```bash
# 8b — Final state check
echo "=== PRIMARY POOL ==="
aws cognito-idp list-users \
  --user-pool-id "$PRIMARY_POOL_ID" --region "$PRIMARY_REGION" \
  --query 'Users[].{Email:Attributes[?Name==`email`].Value|[0],Status:UserStatus}'

echo "=== DR POOL ==="
aws cognito-idp list-users \
  --user-pool-id "$DR_POOL_ID" --region "$DR_REGION" \
  --query 'Users[].{Email:Attributes[?Name==`email`].Value|[0],Status:UserStatus}'
```

**Expected:** Both pools show alice, bob, charlie — all `CONFIRMED` ✅

```bash
# 8c — Confirm Migration Lambda is correctly configured
aws lambda get-function-configuration \
  --function-name cognito-migration-trigger --region "$DR_REGION" \
  --query 'Environment.Variables'
```

**Expected:** `PRIMARY_USER_POOL_ID` points to your actual primary pool ✅

---

## Test Results Summary

| Test | Scenario | Expected Result | Status |
|---|---|---|---|
| T0 | Infrastructure state verification | All components confirmed | |
| T1 | Baseline login to primary pool | AccessToken returned | |
| T2 | Pre-sync Lambda manual trigger | `created/exists` count correct, `failed: 0` | |
| T3 | JIT migration — user not in DR pool | AccessToken returned, user created in DR | |
| T4a | Pre-migrated user with real password | `NotAuthorizedException` (expected limitation) | |
| T4b | Delete + JIT workaround | AccessToken returned | |
| T5a | Outage sim — user in DR (placeholder) | `NotAuthorizedException` (expected) | |
| T5b | Outage sim — user not in DR | `NotAuthorizedException` (expected) | |
| T6a | Primary refresh token against DR pool | `NotAuthorizedException` (expected) | |
| T6b | Fresh login to DR pool | AccessToken returned | |
| T7 | New user after last sync (RPO gap) | JIT migration covers gap | |
| T8 | Restore full state | Both pools in sync | |

---

## Behaviour Summary

| Scenario | What Happens | User Impact |
|---|---|---|
| User pre-synced, primary UP | DR pool finds user → password mismatch → Lambda NOT invoked | ❌ Login fails (placeholder password) |
| User NOT in DR pool, primary UP | Migration Lambda fires → verifies against primary → creates user | ✅ Seamless login |
| User NOT in DR pool, primary DOWN | Migration Lambda fires → cannot reach primary → fails | ❌ Login fails |
| Active session at failover | Primary token invalid in DR pool (different RSA key) | ⚠️ Re-login required once |
| New user (within last hour), primary UP | JIT migration covers the sync gap | ✅ Seamless login |
| New user (within last hour), primary DOWN | No record in DR, Lambda fails | ❌ Login fails |

**The honest summary:** This solution eliminates the password-reset requirement for the vast majority of users. The residual risk is users created in the last sync interval when the primary is simultaneously unreachable — a narrow window that shrinks with more frequent syncing.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `NotAuthorizedException: SECRET_HASH was not received` | Using secret-enabled client from CLI | Use `$PRIMARY_CLIENT_ID_CLI` / `$DR_CLIENT_ID_CLI` |
| `NotAuthorizedException` on DR login (user exists) | User has placeholder password | Delete user from DR pool, re-login triggers JIT migration |
| `failed: N` in pre-sync output | IAM or Lambda code issue | `aws logs tail /aws/lambda/cognito-presync --region "$DR_REGION" --since 5m` |
| Migration Lambda not firing | Lambda not attached to DR pool | `aws cognito-idp describe-user-pool --user-pool-id "$DR_POOL_ID" --region "$DR_REGION" --query 'UserPool.LambdaConfig'` |
| `UserNotFoundException` after Test 5 | Forgot to restore Lambda env vars | Re-run the RESTORE command in Test 5d |
| `for dquote>` prompt | `!` in password inside double quotes | Always use single quotes: `'PermanentPass@2026!'` |
