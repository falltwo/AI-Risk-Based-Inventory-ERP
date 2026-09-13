# Security Foundation Improvement Plan

## Purpose

This document defines the first implementation phase for strengthening identity, authorization, and LINE integration boundaries before further AI decision features are added.

## Scope

### 1. Password storage and migration

- Replace the current custom salted SHA-256 password scheme with Argon2id.
- Keep a version field for password hashes.
- On a successful legacy login, transparently re-hash the password with Argon2id.
- Add a rate limit and temporary lockout for repeated failed logins.
- Record successful logins, failures, lockouts, and password migration events in the audit trail.

### 2. Backend authorization

- Represent the authenticated actor with an explicit `Principal` object: user ID, organization ID, role, entitlements, authentication method, and session expiry.
- Require a principal and explicit capability check on every sensitive backend write.
- Reload membership and entitlement state from the database for sensitive operations.
- Fail closed when identity, organization membership, entitlement, or session validity is missing or revoked.
- Do not allow Streamlit runtime detection to bypass backend authorization.

### 3. LINE identity boundary

- Map LINE user IDs only to approved, active ERP identities.
- Deny access by default when a LINE identity is unknown, revoked, or cannot be looked up.
- Remove permissive role fallbacks such as defaulting lookup failures to a warehouse role.
- Return a generic, user-safe error message; write technical details only to server logs and audit events.

### 4. Verification

- Unit tests: Argon2id verification, legacy migration, rate limiting, invalid principals, revoked users, and unknown LINE users.
- Integration tests: sensitive writes reject unauthenticated or unauthorized requests.
- Regression tests: error replies do not disclose stack traces, database paths, tokens, or internal exception details.

## Acceptance criteria

- [ ] New passwords are stored with Argon2id.
- [ ] Legacy credentials upgrade on successful login without exposing passwords.
- [ ] Repeated failed logins are throttled and logged.
- [ ] Sensitive backend writes require a valid principal and capability.
- [ ] Unknown or revoked LINE users cannot access ERP tools.
- [ ] User-facing errors do not expose internal technical details.
- [ ] Automated tests cover the security behaviour above.

## Out of scope

- SSO or external IAM integration
- PostgreSQL migration
- Shared-database multi-tenancy
- AI decision-evidence records and explainability
- External ERP outbox or worker architecture
