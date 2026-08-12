# Changelog

All notable changes to this project are documented here. Dated entries, keyed to
commits — this repo isn't tagged/released, so there are no version numbers.

## 2026-08-12 — Per-alias read-only access

### Added
- `READ_ONLY_ALIASES` env var: aliased connectors named here (e.g. `work`) now get a
  Google OAuth grant covering only `gmail.readonly`/`calendar.readonly` — no write scope
  is ever issued for that session, so a bug in this server can't make it send or delete
  anything regardless of what the server-side code does. The server also refuses the
  four write tools (`send_email`, `create_draft`, `modify_labels`, `trash_message`)
  itself for read-only sessions, as a second layer.
- Which alias is authenticating is detected from the OAuth `resource` parameter
  (RFC 8707) on `/authorize`, logged at INFO so it's verifiable after connecting —
  see the README's "Read-only accounts" section for how to check it actually took
  effect for a given connector.

## 2026-08-12 — Code quality review #2

### Fixed
- `_refresh_locks` no longer leaks a lock per session when a token refresh fails
  (revoked/expired) — only the natural-expiry cleanup path handled this before.
- `_auth_callback` now checks the Google userinfo fetch succeeded instead of silently
  creating a session/JWT bound to a `None` identity on failure.

### Security
- `_pkce_ok` now compares the PKCE challenge with a constant-time comparison
  (`hmac.compare_digest`) instead of `==`.
- Added standard security response headers (`Strict-Transport-Security`,
  `X-Content-Type-Options`, `X-Frame-Options`) to every response.
- Docker image now runs as a non-root user.
- `requirements.txt` now has upper version bounds on all floor-pinned dependencies.

### Added
- Structured logging throughout the OAuth flow and the bearer-auth check, so failures
  (expired JWTs, revoked refresh tokens, Google API errors, unexpected bugs) are
  distinguishable in the log stream instead of all looking like a silent 401. Level
  configurable via `LOG_LEVEL` (default `INFO`). Logs error codes/messages and email
  addresses only — never tokens, secrets, or PKCE verifiers.
- Per-request cleanup (`_purge_expired_states`/`_purge_expired_tokens`) is now wrapped
  so a bug there can't take down every request.

## 2026-08-12 — Code quality review #1 (820bcb8)

### Security
- `/authorize` now rejects any `redirect_uri` outside an explicit allowlist
  (`ALLOWED_REDIRECT_URIS`, defaults to Claude.ai's callback) and requires a PKCE
  `code_challenge` on every request. Previously an attacker could craft an
  `/authorize?redirect_uri=<attacker-controlled>` link with no PKCE challenge; once the
  victim completed Google's consent screen, the resulting single-use authorization code
  would be redirected straight to the attacker with no proof-of-possession required to
  redeem it — full read/send/delete access to the victim's Gmail and Calendar.

### Fixed
- `_code_store` entries from abandoned OAuth flows (state and token already had this)
  now expire after 10 minutes instead of leaking forever.
- `send_email` now raises an error instead of silently sending an unthreaded standalone
  email when `reply_to_message_id` was given but the lookup failed.

### Changed
- `requirements.txt` now lists `starlette` and `uvicorn` directly instead of relying on
  them arriving transitively via `fastmcp`.
- README: fixed a stale/incorrect clone URL, added a Railway/Render deployment section,
  documented `ALLOWED_REDIRECT_URIS`.
- Removed a stray unrelated entry (`con.yml`) from `.gitignore`.

## 2026-08-12 — Platform port binding (19b1d1d)

### Fixed
- Server now binds to the platform-injected `PORT` env var (Railway, Render, etc.)
  instead of a hardcoded `8000`, so it works out of the box on PaaS platforms without
  manually pinning a target port.

## 2026-08-12 — Initial hardening pass (4755a0a)

### Added
- Expiry-based cleanup for `_state_store` (10 min TTL) and `_token_store` (tied to the
  client JWT's own expiry) — both previously grew unbounded.
- Path-alias normalisation now covers all routes, not just `/mcp` — fixes 404s on
  `/<alias>/.well-known/...` when running a second aliased Claude connector.
- `send_email` now sets `In-Reply-To`/`References` headers so replies thread correctly
  in the recipient's mail client, not just via Gmail's internal `threadId`.
- Shared 30s `httpx` timeout applied to all outbound requests.

### Fixed
- `_refresh()`: handle Google refresh-token errors (revoked/expired) with a clear
  re-auth signal instead of an unhandled `KeyError`.
- `_refresh()`: serialize concurrent refreshes for the same session with a per-session
  lock, preventing duplicate `refresh_token` grants from racing each other.
- `read_message`: recurse through nested MIME parts instead of stopping one level deep —
  fixes missing bodies on deeply nested multipart emails.
- `modify_labels`: replaced mutable default arguments (`[]`) with `None`.
