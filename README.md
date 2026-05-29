# Amazon Cognito Cross-Region Disaster Recovery

An AWS-native disaster recovery solution for Amazon Cognito User Pools using **Pre-Migration Sync + JIT (Just-In-Time) Migration Lambda**. This pattern eliminates the password-reset requirement for users during regional failover while keeping costs near zero.

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

**Bottom line:** Active sessions cannot survive a failover without re-authentication. The goal is to eliminate the password-reset requirement and minimize disruption to a single re-login per user.

---

## Solution: Pre-Sync + JIT Migration (Option 2b)

This solution combines two complementary mechanisms:

### 1. Pre-Sync Lambda (Proactive)
Runs hourly via EventBridge Scheduler. Copies user records from the primary pool to the DR pool with a placeholder password. Ensures the DR pool has a complete user roster before any failover.

### 2. Migration Lambda (Reactive)
Attached as a Cognito User Migration trigger on the DR pool. When a user not in the DR pool attempts to log in, Cognito invokes this Lambda. It verifies credentials against the primary pool and migrates the user just-in-time — same password, no reset required.

---

## Architecture

![Cognito DR](images/cognito-dr-architecture.png)

## Why This Approach?

| Criteria | This Solution |
|---|---|
| **Seamless login after failover** | ✅ Same password, no reset |
| **Works if primary completely down** | ✅ For pre-synced users (with enhancement) |
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
| **Pre-Sync + JIT Migration** ✅ | **❌ Not required** | **✅ Yes** | **~$5** | **Medium** |

---

## Components

```
cognito-dr-opensource/
├── README.md                          # This file
├── lambda/
│   ├── migration/
│   │   └── lambda_function.py         # JIT Migration Lambda
│   └── presync/
│       └── lambda_function.py         # Pre-Sync Lambda (hourly)
├── docs/
│   ├── SETUP.md                       # Step-by-step setup guide
│   └── TESTING.md                     # DR testing guide with CLI commands
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