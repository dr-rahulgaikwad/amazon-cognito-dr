# Amazon Cognito Cross-Region Disaster Recovery

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![AWS](https://img.shields.io/badge/AWS-Native-orange)](https://aws.amazon.com)
[![Cost](https://img.shields.io/badge/Cost-~%245%2Fmonth-green)](docs/setup.md)

An AWS-native disaster recovery solution for Amazon Cognito User Pools using **Pre-Migration Sync + JIT (Just-In-Time) Migration Lambda**. This pattern minimises the need for users to reset their passwords during regional failover while keeping costs near zero.

---

## The Problem

Amazon Cognito User Pools are **strictly regional**. AWS provides no native cross-region replication. If your primary region goes down, users cannot authenticate — regardless of how well the rest of your DR architecture is designed.

Four fundamental constraints make Cognito DR uniquely challenging:

| Constraint | Impact |
|---|---|
| **Password hashes are never exportable** | Cognito stores bcrypt hashes with user-specific salts. No API exposes them. You cannot copy passwords between pools. |
| **JWT tokens are pool-specific** | Each pool has its own RSA key pair. Tokens from the primary pool are cryptographically invalid against the DR pool. |
| **Refresh tokens are pool-bound** | A refresh token from the primary pool cannot be exchanged for tokens from the DR pool. |
| **App client secrets are AWS-generated** | Client IDs and secrets cannot be set manually. The DR pool will have different credentials. |

**The practical implication is simple:** Users will need to re-authenticate after failover. The goal is not to eliminate re-login completely — the real goal is to minimise the need for users to reset their passwords during an outage.

---

## Solution: Pre-Sync + JIT Migration (Option 2b)

This solution combines two complementary mechanisms:

### 1. Pre-Sync Lambda (Proactive)
Runs hourly via EventBridge Scheduler. Copies user records from the primary pool to the DR pool with a placeholder password. Ensures the DR pool has a complete user roster before any failover.

### 2. Migration Lambda (Reactive)
Attached as a Cognito User Migration trigger on the DR pool. When a user not in the DR pool attempts to log in, Cognito invokes this Lambda. It verifies credentials against the primary pool and migrates the user just-in-time — same password, no reset required.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        NORMAL OPERATION                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  ┌─────────────────────┐         ┌──────────────────────────┐               │
│  │   PRIMARY REGION    │         │      DR REGION           │               │
│  │    (us-east-1)      │         │     (us-west-2)          │               │
│  │                     │         │                          │               │
│  │  ┌───────────────┐  │         │  ┌────────────────────┐  │               │
│  │  │ Cognito Pool  │◄─┼─────────┼──│  Pre-Sync Lambda   │  │               │
│  │  │  (Primary)    │  │ ListUsers│  │  (hourly via       │  │               │
│  │  └───────┬───────┘  │         │  │   EventBridge)     │  │               │
│  │          │           │         │  └─────────┬──────────┘  │               │
│  │          │           │         │            │             │               │
│  │          │           │         │            ▼             │               │
│  │          │           │         │  ┌────────────────────┐  │               │
│  │          │           │         │  │  Cognito Pool      │  │               │
│  │          │           │         │  │  (DR - standby)    │  │               │
│  │          │           │         │  └────────────────────┘  │               │
│  └──────────┼───────────┘         └──────────────────────────┘               │
│             │                                                                 │
│  ┌──────────┼───────────────────────────────────────────────┐               │
│  │  Secrets Manager (replicated us-east-1 → us-west-2)      │               │
│  │  Contains: primary pool ID, client ID                     │               │
│  └───────────────────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                        DR FAILOVER                                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  User Login to DR Pool                                                        │
│       │                                                                       │
│       ▼                                                                       │
│  ┌─────────────────┐    User NOT found     ┌──────────────────────┐         │
│  │  DR Cognito     │ ──────────────────────►│  Migration Lambda    │         │
│  │  Pool           │                        │                      │         │
│  │                 │◄───────────────────────│  Verifies password   │         │
│  │  Issues tokens  │   Returns user attrs   │  against primary     │         │
│  └─────────────────┘                        └──────────────────────┘         │
│                                                                               │
│  CASE A: User NOT in DR pool, primary reachable → JIT migration ✅           │
│  CASE B: User NOT in DR pool, primary DOWN → login fails ❌ (residual risk)  │
│  CASE C: User previously JIT-migrated (e.g., DR drill) → login works ✅      │
│                                                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Why This Approach?

| Criteria | This Solution |
|---|---|
| **Seamless login after failover** | ✅ Same password, no reset (when primary is reachable) |
| **Works if primary completely down** | ✅ For users previously JIT-migrated (e.g., during DR drills) |
| **Active session continuity** | ❌ Re-login required once (unavoidable with any multi-pool strategy) |
| **RPO** | ~1 hour (sync interval, configurable) |
| **RTO** | < 5 minutes |
| **App code changes** | None |
| **Additional cost** | ~$5/month |
| **AWS-native** | ✅ No third-party dependencies |
| **Complexity** | Medium |

### Comparison with Alternatives

| Approach | Password Reset? | Works if Primary Down? | Cost | Complexity |
|---|---|---|---|---|
| CSV Backup & Restore | ✅ Required | ✅ Yes | ~$0 | Low |
| JIT Migration Only | ❌ Not required | ❌ No | ~$0 | Medium |
| OIDC Federation | ❌ Not required | ❌ No (defeats DR) | ~$0 | High |
| Third-Party IdP (Okta/Auth0) | ❌ Not required | ✅ Yes | $200-500/mo | Very High |
| AWS Export Reference Arch | ✅ Required | ✅ Yes | ~$10-20 | Medium |
| **Pre-Sync + JIT Migration** ✅ | **❌ Not required** | **Partial** (see limitations) | **~$5** | **Medium** |

---

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| **Pre-synced users have placeholder passwords** | Users exist in DR pool with a placeholder — login with real password fails because Cognito finds the user, sees a mismatch, and does NOT invoke the Migration Lambda | Failover runbook bulk-deletes pre-synced users to force JIT migration path; or run quarterly DR drills to permanently store real passwords |
| **Primary must be reachable for JIT migration** | If primary is completely down AND user was never JIT-migrated before, login fails | Increase sync frequency; run DR drills quarterly to pre-migrate active users |
| **Active sessions require re-login** | Users with valid primary tokens must re-authenticate against DR pool | Bounded by access token TTL (typically 1 hour) |
| **New users in last sync interval** | Users created after last sync don't exist in DR pool | JIT migration covers this gap if primary is reachable |

**The honest summary:** This solution eliminates the password-reset requirement for the vast majority of users. The residual risk is users who have never completed a JIT migration (either during normal DR drills or during the failover itself) when the primary is simultaneously unreachable — they must reset their password. Regular DR drills shrink this risk to near zero.

---

## Components

```
amazon-cognito-dr/
├── README.md                          # This file
├── lambda/
│   ├── migration/
│   │   └── lambda_function.py         # JIT Migration Lambda
│   └── presync/
│       └── lambda_function.py         # Pre-Sync Lambda (hourly)
├── docs/
│   ├── setup.md                       # Step-by-step setup guide
│   └── testing.md                     # DR testing guide with CLI commands
└── LICENSE
```

| Component | Purpose | Region |
|---|---|---|
| Primary Cognito Pool | Production user pool | us-east-1 (configurable) |
| DR Cognito Pool | Standby pool with pre-synced users | us-west-2 (configurable) |
| Migration Lambda | JIT user migration on login | DR region |
| Pre-Sync Lambda | Hourly user roster sync | DR region |
| EventBridge Scheduler | Triggers pre-sync hourly | DR region |
| Secrets Manager | Stores primary pool config, replicated to DR | Both regions |

---

## How It Works

### Normal Operation (Hourly Sync)

```
EventBridge Scheduler (rate: 1 hour)
    │
    ▼
Pre-Sync Lambda (us-west-2)
    │
    ├── ListUsers from Primary Pool (us-east-1)
    │
    └── For each user NOT in DR Pool:
        ├── AdminCreateUser (with placeholder password, SUPPRESS email)
        └── AdminSetUserPassword (Permanent=true → CONFIRMED status)
```

### DR Failover — JIT Migration

```
User attempts login to DR Pool
    │
    ▼
DR Cognito Pool: User not found
    │
    ▼
Invokes Migration Lambda (UserMigration_Authentication trigger)
    │
    ├── AdminInitiateAuth against Primary Pool (verify password)
    ├── AdminGetUser (fetch attributes)
    │
    ▼
Returns user attributes to DR Pool
    │
    ▼
DR Pool creates user (CONFIRMED) → Issues tokens → Login succeeds ✅
```

### Token Expiry After Failover

Users with active sessions at failover time will experience a one-time re-login when their access token expires (typically 1 hour). This is unavoidable with any multi-pool Cognito strategy because each pool has its own RSA key pair.

---

## Cost Estimate

| Resource | Monthly Cost |
|---|---|
| Pre-Sync Lambda (hourly, ~3s per run) | ~$0.01 |
| Migration Lambda (on-demand, per login) | ~$0.00 |
| EventBridge Scheduler | ~$0.00 |
| Secrets Manager (1 secret, replicated) | ~$1.00 |
| DR Cognito Pool (standby, no MAU until failover) | ~$0.00 |
| CloudWatch Logs | ~$1-3 |
| **Total** | **~$2-5/month** |

---

## Quick Start

1. **[Setup Guide](docs/setup.md)** — Step-by-step infrastructure deployment with AWS CLI
2. **[Testing Guide](docs/testing.md)** — DR scenario testing with CLI commands
3. **Lambda Code** — Ready-to-deploy in `lambda/` directory

---

## Prerequisites

- AWS CLI v2 configured with appropriate permissions
- Python 3.12+ (for Lambda runtime)
- Two AWS regions available (default: us-east-1 primary, us-west-2 DR)
- IAM permissions to create: Cognito User Pools, Lambda functions, IAM roles, Secrets Manager secrets, EventBridge schedules

---

## AWS References

| # | Reference | URL |
|---|---|---|
| 1 | Migrate user Lambda trigger | https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-migrate-user.html |
| 2 | Importing users with Lambda trigger | https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-import-using-lambda.html |
| 3 | AdminCreateUser API | https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_AdminCreateUser.html |
| 4 | AdminSetUserPassword API | https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_AdminSetUserPassword.html |
| 5 | AdminInitiateAuth API | https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_AdminInitiateAuth.html |
| 6 | Approaches for migrating users (AWS Security Blog) | https://aws.amazon.com/blogs/security/approaches-for-migrating-users-to-amazon-cognito-user-pools/ |
| 7 | Migrating users to Cognito (AWS Mobile Blog) | https://aws.amazon.com/blogs/mobile/migrating-users-to-amazon-cognito-user-pools/ |
| 8 | Password hashing — why export is impossible | https://docs.aws.amazon.com/cognito/latest/developerguide/managing-users-passwords.html |
| 9 | User Profiles Export Reference Architecture | https://aws.amazon.com/solutions/guidance/user-profiles-export-with-amazon-cognito/ |
| 10 | Cross-Region Failover Guidance | https://aws.amazon.com/solutions/guidance/cross-region-failover-and-graceful-failback-on-aws/ |

---

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

## Author

**Rahul Gaikwad** — AWS Solutions Architect

Built from real-world production DR implementation. Tested and verified with AWS CLI in May 2026.
