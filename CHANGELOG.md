# Changelog

All notable changes to this project, in version order. This repo isn't tagged/released
on GitHub, so these are informal version numbers rather than official releases —
starting at 0.1 for the original fork and incrementing from there. A date heading only
appears when the date changes from the entry above it.

## 2026-08-12

### 0.10 — Code quality review #3

Third review pass, specifically hunting for bugs, dead/stale code, and complexity not
earning its keep. No new bugs found — this round is two small cleanups plus a written
record of what was deliberately left alone (see the commit message for the full list).

**Changed**
- `_oauth_server_metadata` and `_openid_configuration` shared 6 of 7 fields verbatim;
  extracted into `_base_oauth_metadata()`.
- `.env.example` now lists `ALLOWED_REDIRECT_URIS`, `LOG_LEVEL`, `READ_ONLY_ALIASES` —
  all three were added earlier today but never added here, so they were easy to miss
  when setting up a new deployment.

### 0.9 — Enrich search_emails results (2405ac8)

Found during live testing: summarizing senders/subjects from search results required a
separate read_message call per message, or narrower per-sender searches as a
workaround — because Gmail's messages.list endpoint (which search_emails calls) has
never returned anything beyond id/threadId. Not a bug, but a real usability gap worth
closing.

**Added**
- `search_emails` now fetches from/to/subject/date/snippet/labels for the first 50
  results (parallelized, reusing one HTTP connection) so most follow-up questions about
  search results don't need a separate `read_message` call. Capped at 50 regardless of
  `max_results` so a large explicit value can't fan out into hundreds of API calls;
  results beyond the cap still come back as bare id/threadId. Individual fetch failures
  fall back to the bare id/threadId for that message rather than failing the search.

### 0.8 — Railway + Google OAuth setup walkthrough (47636a0)

**Changed**
- Rewrote the Setup section as a step-by-step Railway + Google Cloud walkthrough based
  on an actual live deployment, including the non-obvious parts: the first deploy
  crashing before env vars are set, Railway's target-port prompt being safe to leave at
  default, current Google Cloud Console navigation (Audience/Clients under "Google Auth
  Platform", APIs & Services/Library being separate), the test-user "ineligible" gotcha,
  and the OAuth Client ID placeholder needed to skip past Claude's
  automatic-registration warning. Self-hosting (Docker/reverse proxy) instructions kept
  as a secondary path rather than removed.

### 0.7 — Fix alias detection for per-alias read-only (46b28c4)

Verified live against a real deployment: connecting a `READ_ONLY_ALIASES`-restricted
connector still requested full read/write scopes. Root cause traced from the deploy
logs — `_protected_resource` always advertised the bare `BASE_URL` as the OAuth
`resource`, regardless of which aliased connector the 401 challenge came from, so
Claude correctly echoed that bare value back on `/authorize` and the alias never came
through. Not a client-behavior gap — a bug in this server's own metadata.

**Fixed**
- `/.well-known/oauth-protected-resource` and the 401 challenge's `resource_metadata`
  URL are now alias-aware: the alias a request came in through is captured before
  routing strips it (`_split_alias`, replacing `_normalise_path`) and threaded through
  `scope["state"]` to the Starlette route handler, which echoes
  `{BASE_URL}/{alias}/mcp` back as the resource. Claude then sends that same value as
  the `resource` parameter on `/authorize`, which `_alias_from_resource` can parse
  correctly.
- `_protected_resource`'s `resource` field for the unaliased case also now points at
  `{BASE_URL}/mcp` specifically rather than the bare origin, matching the RFC 9728
  convention of identifying the actual protected resource, not just the server root.

### 0.6 — Per-alias read-only access (c536902)

**Added**
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

### 0.5 — Code quality review #2 (bf85d23)

**Fixed**
- `_refresh_locks` no longer leaks a lock per session when a token refresh fails
  (revoked/expired) — only the natural-expiry cleanup path handled this before.
- `_auth_callback` now checks the Google userinfo fetch succeeded instead of silently
  creating a session/JWT bound to a `None` identity on failure.

**Security**
- `_pkce_ok` now compares the PKCE challenge with a constant-time comparison
  (`hmac.compare_digest`) instead of `==`.
- Added standard security response headers (`Strict-Transport-Security`,
  `X-Content-Type-Options`, `X-Frame-Options`) to every response.
- Docker image now runs as a non-root user.
- `requirements.txt` now has upper version bounds on all floor-pinned dependencies.

**Added**
- Structured logging throughout the OAuth flow and the bearer-auth check, so failures
  (expired JWTs, revoked refresh tokens, Google API errors, unexpected bugs) are
  distinguishable in the log stream instead of all looking like a silent 401. Level
  configurable via `LOG_LEVEL` (default `INFO`). Logs error codes/messages and email
  addresses only — never tokens, secrets, or PKCE verifiers.
- Per-request cleanup (`_purge_expired_states`/`_purge_expired_tokens`) is now wrapped
  so a bug there can't take down every request.

### 0.4 — Code quality review #1 (820bcb8)

**Security**
- `/authorize` now rejects any `redirect_uri` outside an explicit allowlist
  (`ALLOWED_REDIRECT_URIS`, defaults to Claude.ai's callback) and requires a PKCE
  `code_challenge` on every request. Previously an attacker could craft an
  `/authorize?redirect_uri=<attacker-controlled>` link with no PKCE challenge; once the
  victim completed Google's consent screen, the resulting single-use authorization code
  would be redirected straight to the attacker with no proof-of-possession required to
  redeem it — full read/send/delete access to the victim's Gmail and Calendar.

**Fixed**
- `_code_store` entries from abandoned OAuth flows (state and token already had this)
  now expire after 10 minutes instead of leaking forever.
- `send_email` now raises an error instead of silently sending an unthreaded standalone
  email when `reply_to_message_id` was given but the lookup failed.

**Changed**
- `requirements.txt` now lists `starlette` and `uvicorn` directly instead of relying on
  them arriving transitively via `fastmcp`.
- README: fixed a stale/incorrect clone URL, added a Railway/Render deployment section,
  documented `ALLOWED_REDIRECT_URIS`.
- Removed a stray unrelated entry (`con.yml`) from `.gitignore`.

### 0.3 — Platform port binding (19b1d1d)

**Fixed**
- Server now binds to the platform-injected `PORT` env var (Railway, Render, etc.)
  instead of a hardcoded `8000`, so it works out of the box on PaaS platforms without
  manually pinning a target port.

### 0.2 — Initial hardening pass (4755a0a)

**Added**
- Expiry-based cleanup for `_state_store` (10 min TTL) and `_token_store` (tied to the
  client JWT's own expiry) — both previously grew unbounded.
- Path-alias normalisation now covers all routes, not just `/mcp` — fixes 404s on
  `/<alias>/.well-known/...` when running a second aliased Claude connector.
- `send_email` now sets `In-Reply-To`/`References` headers so replies thread correctly
  in the recipient's mail client, not just via Gmail's internal `threadId`.
- Shared 30s `httpx` timeout applied to all outbound requests.

**Fixed**
- `_refresh()`: handle Google refresh-token errors (revoked/expired) with a clear
  re-auth signal instead of an unhandled `KeyError`.
- `_refresh()`: serialize concurrent refreshes for the same session with a per-session
  lock, preventing duplicate `refresh_token` grants from racing each other.
- `read_message`: recurse through nested MIME parts instead of stopping one level deep —
  fixes missing bodies on deeply nested multipart emails.
- `modify_labels`: replaced mutable default arguments (`[]`) with `None`.

## 2026-05-31

### 0.1 — Forked from devgar/gmail-mcp-proxy (2d92cbf)

Starting point for everything above: the original project as forked, including its
README and deployment docs (added 2026-06-15, shortly after the initial commit).
