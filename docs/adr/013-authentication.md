# ADR 013: Authentication and authorization

Status: accepted (2026-10-15).

## Context
Until now every endpoint was anonymous: anyone could list donors (personal health data) or invent blood stock.
Authentication must come before any real user. Constraints: one Postgres, no Redis yet ("no tool before the pain"), a Next.js
browser client (`frontend/lib/api.ts` already assumes an in-memory access token and an HttpOnly refresh cookie).

## Options
1. Server sessions (random session id in a cookie, a row per session, a DB read per request).
2. Long-lived JWT only (no refresh). Simple, but a stolen token works until it expires and cannot be revoked.
3. Short JWT access token + opaque, rotating refresh token stored hashed in Postgres. Reuse detection per login "family".
4. A hosted identity provider (Cognito, Auth0, Keycloak). Right for many real products; hides exactly the mechanics this project exists to show.

## Decision
Option 3.
- **Passwords:** argon2id (`argon2-cffi` defaults), 12-128 characters, no composition rules (NIST SP 800-63B). Hashing runs in
  `asyncio.to_thread`: argon2 is CPU-heavy on purpose and would block the event loop inside `async def`.
- **Login:** one message ("Invalid email or password") for unknown email, wrong password, locked and deactivated accounts.
  argon2 runs against a dummy hash for unknown emails, so timing does not reveal which emails exist.
  5 failures lock the account for 15 minutes (row lock `FOR UPDATE`, so parallel guesses all count).
- **Access token:** HS256 JWT, 15 minutes, claims `sub`, `role`, `org`, `iat`, `exp`. Verified with `algorithms=["HS256"]`
  pinned (blocks `alg: none` and algorithm confusion). No DB read per request.
- **Refresh token:** 32 random bytes, stored only as SHA-256, 14 days, sent as an HttpOnly, SameSite=Strict cookie scoped to
  `/v1/auth`. Every refresh rotates it. Presenting an already-used token revokes the whole family (theft signal).
- **Authorization:** `require(*roles)` for role checks (RBAC); object-level checks (BOLA) inside the route where the row is known.
  A donor probing someone else's donor id gets 404, not 403, so the id's existence is not confirmed.
- **Accounts:** public signup creates donors only (role is not a request field). Staff and admin accounts are created by an admin
  (`POST /v1/auth/users`). The first admin is inserted by an operator (SQL), never through the API.
- **Abuse:** per-IP limit on login/register (10/minute, in process). NOT on refresh: the token is unguessable, and limiting it
  locked users out when page loads refreshed (found in UI testing; a whole hospital behind one NAT IP would share the budget).
- **Client:** `frontend/lib/api.ts` refreshes single-flight (concurrent callers share one request), because two parallel
  refreshes with one rotating token trip reuse detection. Found in UI testing: React dev mode's double effect logged users out on reload. Security headers and an explicit CORS allow-list on
  every response. The app refuses to start with a secret under 32 characters, or with insecure cookies in production.
- **Audit:** registered, logged_in, login_failed, locked, refresh_reuse and user.created go to `audit_events` (ADR 010).

## Consequences
- A deactivated or demoted user keeps access for up to 15 minutes (no deny-list). Upgrade: a Redis deny-list checked in
  `get_current_user` when an incident shows 15 minutes is too long.
- The rate limiter is per process and forgets on restart. Two instances means two counters. Upgrade: Redis `INCR`/`EXPIRE` once there are several instances.
- Two browser TABS refreshing at the same instant still trip reuse detection (single-flight is per tab). Upgrade: a short
  server grace window, when it is seen in practice.
- `409 email-taken` on signup reveals that an email has an account. Accepted for signup UX until email verification exists (3.6).
- The admin can attach a `hospital` user to an NGO: role vs organization kind is not cross-checked (needs a call into the
  organizations module). Add with facility verification.
- HS256 means the API that verifies tokens can also mint them. Fine for one service; move to RS256/EdDSA when a second service
  must verify tokens without being able to issue them.

## Amendment 2026-10-24 (B10): lockout per (account, IP)

The per-account lockout let anyone who knew a bank's or hospital's login email keep it locked out (5 wrong passwords
every 15 minutes). Failures are now counted per (account, client IP) in `login_failures` (migration 0018): the
attacker's IP is locked, staff on their own network still log in, and a success from an IP deletes that IP's row.
`users.failed_logins` / `users.locked_until` are unused and will be dropped in a later contract migration.
Limits: an attacker with many IPs gets 5 guesses per IP per 15 min (the per-IP rate limit still applies; MFA for staff
is the upgrade); behind a proxy the API must trust `X-Forwarded-For` (uvicorn `--proxy-headers`), or every request
shares the proxy's IP and this is per account again.
