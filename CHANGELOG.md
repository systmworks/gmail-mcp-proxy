[← Back to README](README.md)

# Changelog

All notable **code changes** to this project — `server.py` and the test suite — in
version order, starting at 0.1 for the original fork and incrementing from there.
Administrative changes (documentation, README/SETUP.md, CI/tooling config, LICENSE,
dependency pins, changelog maintenance itself, etc.) are tracked in git commit
history only, not here. Version numbers are permanent once assigned — removing an
out-of-scope entry leaves a gap rather than renumbering everything after it, so a
missing number means an administrative-only change, not a lost entry. No GitHub
Releases (no external consumers to serve release notes to), but working milestones
get a lightweight git tag (`v0.1`, `v0.2`, …) as a rollback anchor. A date heading
only appears when the date changes from the entry above it.

## 2026-09-12

### 0.30 — Fix get_attachment: Gmail's attachmentId is not stable across calls

Live testing immediately after deploying 0.29 found `get_attachment` failing with
"no attachment found" on a real attachment it had just listed via `read_message`.
Logs showed two separate `messages.get(format=full)` calls for the very same
message returned two *different* `attachmentId` values for the identical PDF part —
`get_attachment`'s own internal metadata lookup (a fresh `messages.get` call) never
matched the `attachmentId` the caller had gotten from an earlier `read_message` call,
so the design was fundamentally broken for real-world use, not just an edge case.

**Fixed**
- `read_message`'s `attachments` list now surfaces each part's `partId` instead of
  `attachmentId` — `partId` is Gmail's documented-immutable structural identifier,
  unlike `attachmentId` which is apparently only valid within the response that
  minted it.
- `get_attachment(message_id, part_id)` (was `attachment_id`) now locates the part by
  `partId` (`_find_part_by_id`, a new helper) and reads `attachmentId` fresh from
  that same fetch, immediately before using it to download the bytes — never accepts
  an `attachmentId` from a separate prior call.
- `tests/test_server.py`: new regression test
  (`test_get_attachment_resolves_fresh_attachment_id_each_call`) simulating exactly
  this scenario — two `messages.get` responses for the same `partId` with different
  `attachmentId` values — asserting `get_attachment` downloads using the second
  (fresh) one, not a value cached from the first call. All other attachment tests
  updated to key off `partId`.

### 0.29 — Attachment download support

No tool exposed Gmail file attachments — `read_message`'s MIME walker only looked at
`text/plain`/`text/html` parts, silently skipping attachment parts, so there was no
way to even learn an attachment existed, let alone fetch its bytes.

**Added**
- `get_attachment(message_id, attachment_id)` tool — downloads an attachment's bytes,
  re-encoded as standard base64 (not Gmail's base64url) for portability to a generic
  MCP client. Looks up filename/mimeType/size from the message's MIME tree first and
  rejects attachments over `ATTACHMENT_MAX_MB` there, before ever downloading the
  bytes; re-checks size after decoding as a safety net in case that metadata was
  missing or wrong. No `_require_write()` gate — this is a read, covered by the
  existing `gmail.readonly` scope, so it works the same for read-only aliased
  sessions as `read_message`/`search_emails` do.
- `ATTACHMENT_MAX_MB` env var (default `3`, clamped 1–25). Attachment bytes return as
  base64 text inside the MCP tool result — straight into the calling LLM's context,
  not just over the network — so the default sits well below Gmail's own 25MB decoded
  cap on this endpoint.
- `read_message` now also returns an `attachments` list (filename/attachmentId/
  mimeType/size) via a new shared `_find_attachments` MIME-tree walker, so callers can
  discover attachment IDs to pass to `get_attachment`.
- `tests/test_server.py`: coverage for attachment metadata appearing (and staying
  empty when absent) in `read_message`, successful download with base64 re-encoding,
  rejecting an oversized attachment without downloading it, rejecting one whose real
  size exceeds the limit even when metadata understated it, and raising on an unknown
  attachment ID.

`read_thread` is intentionally left as a raw passthrough — attachment info is already
present in its raw payload, just nested/undecoded; restructuring its return shape was
out of scope for this change.

## 2026-09-09

### 0.28 — Fix stale Google token used right after a successful refresh

Self-hosted deployment (not seen on Railway) showed `get_profile` and other tool
calls 401 immediately after a logged-successful token refresh — `_token_store` had
the fresh token, but the per-request Google token was resolved once in the outer
ASGI auth check and cached into a `ContextVar`. If FastMCP executed the actual tool
call in a different asyncio Task than the one that ran that auth check, that task
never saw the later `.set()` and kept using whatever token was current when its own
context was created.

**Fixed**
- `_auth()` is now `async` and resolves the token from `_token_store` at the moment
  of use (`_google_access_token(jti)`) instead of trusting a value handed to it
  earlier. The ContextVar (renamed `_session_jti`, was `_google_token`) now carries
  only the session's `jti` — invariant for the session's lifetime — not the token
  itself. All 22 `headers=_auth()` call sites updated to `await _auth()`.

## 2026-09-08

### 0.27 — Retry write-tool calls on transient Gmail failures

No write tool (`send_email`, `modify_labels`, `create_label`, etc.) retried on
failure — a single Gmail rate-limit or 5xx during a bulk operation (e.g.
labelling hundreds of messages) failed that call outright with no recovery.

**Added**
- `_request_with_retry` helper: retries a write-tool API call up to
  `API_RETRY_ATTEMPTS` total tries (env var, default `2`) on network errors or
  retryable statuses (429, 5xx), with a short delay between attempts. Permanent
  4xx still fails on the first try. All 11 write tools now use it.
- `tests/test_server.py`: coverage for recovery on retry, exhausting retries on
  persistent failure, not retrying permanent 4xx, and retrying network errors.

### 0.26 — Configurable search_emails enrichment retry count

Self-hosted deployment saw ~50% of a 20-message enrichment batch fail even
with the single built-in retry (0.24), typically recovering by a 3rd manual
re-run — the fixed 1-retry budget wasn't enough for that network's failure
rate under batch concurrency.

**Changed**
- `_enrich` now retries up to `SEARCH_ENRICH_ATTEMPTS` total tries (env var,
  default `2` — same as before), with a short delay between attempts, before
  falling back to bare id/threadId. Permanent 4xx still stops retrying
  immediately.

**Added**
- `tests/test_server.py`: coverage for a 3-attempt configuration recovering
  on the 3rd try.

## 2026-09-07

### 0.25 — Log token expiry/refresh decisions

Self-hosted deployment (moved off Railway) reported access tokens timing out
after ~1hr with no matching `ReauthRequired`/`invalid_grant` in logs — nothing
existing told us whether `_refresh` was even being called, or what Google
actually returned when it was.

**Added**
- `_auth_callback` logs `has_refresh_token`/`expires_in` from the initial
  token exchange.
- `_google_access_token` logs the remaining seconds whenever it decides a
  refresh is needed.
- `_refresh` logs Google's raw `expires_in` on each refresh response, and the
  new expiry once stored.

## 2026-08-14

### 0.24 — Retry transient search_emails enrichment failures, log persistent ones

Live testing showed a meaningful fraction of `search_emails`' concurrent per-message
enrichment calls failing on any given run, with different messages failing each
time — the signature of a transient issue, not permanently broken messages. Neither
failure path logged anything, so there was no way to diagnose it after the fact.

**Fixed**
- `_enrich` retries once on network errors and retryable HTTP statuses (`429`, `5xx`)
  before degrading to bare `id`/`threadId` — most of these clear up on an immediate
  retry. Other 4xx (403/404, etc.) are treated as permanent and not retried.

**Added**
- Both failure paths in `_enrich` now log a `log.warning` with the message ID and the
  actual reason (exception message, or HTTP status + truncated body), noting whether
  it happened after a retry.
- `tests/test_server.py`: coverage for retry-then-recover, retry-then-still-fails
  (bounded at one retry), and no-retry-on-permanent-4xx.

### 0.22 — Harden malformed Authorization header handling

Fifth code quality review found one real robustness gap.

**Fixed**
- `_App.__call__`: non-UTF-8 bytes in the `Authorization` header raised an uncaught
  `UnicodeDecodeError` instead of the clean 401 every other malformed-input case in
  this function gets. Now decodes with `errors="replace"`.
- Deduplicated the default redirect URI (`https://claude.ai/api/mcp/auth_callback`),
  previously hardcoded in two places, into one `DEFAULT_REDIRECT_URI` constant.

### 0.21 — OAuth flow test coverage

Zero test coverage existed for `_authorize`, `_auth_callback`, `_refresh`, or the
bearer-auth/JWT branch of `_App.__call__` — the exact code that's had three prior
security fixes (0.4, 0.7, 0.14).

**Added**
- `tests/test_oauth_flow.py` — redirect_uri/PKCE validation, the authorize → callback
  → token handoff (mocked via `respx`), refresh success/failure/concurrency-lock
  behavior, and 401 handling plus alias routing on `/mcp`.

### 0.17 — Fix /token crash on multipart form data

Starlette form values are `UploadFile | str`. `_token` passed `code_verifier`
straight to `_pkce_ok`, which calls `.encode()` on it — a client posting
`/token` as multipart with `code_verifier` as a file part instead of a plain
field crashed with an unhandled `AttributeError` (500) instead of a clean 400.

**Fixed**
- `_token` now filters form values to strings only before use, so any
  non-string field is treated as absent rather than crashing.

**Added**
- `tests/test_token.py` — first direct test coverage for the `/token`
  endpoint, including a regression test for the crash above.

### 0.15 — Configurable search_emails enrichment limit

`SEARCH_ENRICH_LIMIT` was a hardcoded constant; made it an env var so
deployments can tune the token-cost/usefulness trade-off without a code change.

**Changed**
- `SEARCH_ENRICH_LIMIT` env var, default `20` (was a hardcoded `100`), clamped
  to 0–200.

### 0.14 — Fail-closed per-alias read-only enforcement

Found while reviewing upstream's independent port of this feature
(devgar/gmail-mcp-proxy#5): read-only status was decided once at `/authorize`
time from the client-supplied OAuth `resource` parameter and baked into the
30-day JWT — if a client ever omitted or mishandled that parameter, a
`READ_ONLY_ALIASES`-restricted connector would silently mint a full read/write
JWT instead.

**Fixed**
- `_read_only` is now set from `payload.get("read_only", False) or alias in
  READ_ONLY_ALIASES` (`_effective_read_only`), where `alias` comes from
  server-side path routing (`_split_alias`) on the current request — not
  anything the client asserts. A restricted alias can no longer silently
  degrade to read/write.

## 2026-08-12

### 0.13 — Code quality review #4: shared HTTP client, search resilience, first tests

Fourth code quality review. Found one real bug and one real efficiency problem;
the rest were smaller robustness/staleness cleanups.

**Fixed**
- `search_emails`: a network-level exception (timeout, connection reset) enriching
  any one of up to 100 concurrent messages used to fail the *entire* search instead
  of degrading just that message to bare id/threadId, contradicting the tool's own
  docstring. `_enrich` now catches `httpx.HTTPError` around the fetch, same as the
  existing HTTP-status fallback.

**Changed**
- All ~25 call sites that opened a fresh `httpx.AsyncClient` (and therefore a new
  TCP+TLS connection to googleapis.com) per tool call now share one pooled client,
  created on ASGI lifespan startup and closed on shutdown (`_http_client` in
  `_App.__call__`). Cuts connection-setup latency off every single tool invocation.
  Access goes through a `_client()` helper that raises a clear `RuntimeError`
  instead of a bare `AttributeError` if ever called before the lifespan sets it
  (caught by running `mypy` over the change — `_http_client`'s `| None` type was
  otherwise silently unchecked at all 25 call sites).
- `read_message`: `_extract_body` walked the MIME part tree twice (once for
  text/plain, once for text/html on fallback). Replaced with a single recursive
  pass (`_find_bodies`) that collects both and prefers plain.

**Added**
- First test suite for the project: `tests/`, `pytest.ini`, `requirements-dev.txt`.
  Covers the pure-logic helpers (`_pkce_ok`, `_alias_from_resource`, `_split_alias`,
  `_build_email`) directly, and tool behavior via `respx`-mocked Gmail responses —
  including a regression test for the `search_emails` fix above, 204-No-Content
  handling on `delete_draft`/`delete_label`, and read-only enforcement. No live
  Google credentials needed to run them (`pip install -r requirements-dev.txt &&
  pytest`).

### 0.12 — Draft lifecycle, label CRUD, and report_phishing tools

Closed the highest-value tool-surface gaps found comparing against
ArtyMcLabin/Gmail-MCP-Server: draft lifecycle (create-only before), label CRUD
(message-level toggle only before), and a named `report_phishing` tool. No new
OAuth scopes needed — `gmail.send`/`compose`/`modify` already cover all seven
additions.

**Added**
- `send_draft`, `update_draft`, `delete_draft` — full draft lifecycle, reusing the
  existing `_build_email()` MIME builder for `update_draft`. `drafts.delete` returns
  204 No Content, so `delete_draft` checks the status code instead of calling
  `.json()` unconditionally like the other write tools.
- `create_label`, `update_label`, `delete_label` — full label management alongside
  the existing `list_labels`/`modify_labels`. `update_label` only sends the fields
  that were actually passed (partial update via `PATCH`). Same 204-handling note
  applies to `delete_label`.
- `report_phishing` — thin wrapper over the same `messages/{id}/modify` endpoint
  `modify_labels` already uses, adding `SPAM` and removing `INBOX`.

### 0.11 — Raise search_emails enrichment cap to 100

Verified against Google's Gmail API quota docs (6,000 units/min; `messages.get`=20,
`messages.list`=5) before raising the cap — 100 stays well within budget for normal
usage.

**Changed**
- `SEARCH_ENRICH_LIMIT`: 50 → 100.

### 0.10 — Code quality review #3

Third code quality review — two small cleanups, no new bugs found.

**Changed**
- `_oauth_server_metadata` and `_openid_configuration` shared 6 of 7 fields verbatim;
  extracted into `_base_oauth_metadata()`.

### 0.9 — Enrich search_emails results (2405ac8)

Gmail's `messages.list` (which `search_emails` calls) never returned more than
id/threadId, forcing a separate `read_message` call just to see who a result was
from.

**Added**
- `search_emails` now fetches from/to/subject/date/snippet/labels for the first 50
  results (parallelized, one shared connection), so most follow-ups don't need a
  separate `read_message` call. Capped at 50 regardless of `max_results` (excess
  results stay bare id/threadId); individual fetch failures degrade the same way
  rather than failing the whole search.

### 0.7 — Fix alias detection for per-alias read-only (46b28c4)

`_protected_resource` always advertised the bare `BASE_URL` as the OAuth `resource`
regardless of alias, so a `READ_ONLY_ALIASES`-restricted connector still requested
full read/write scopes — a bug in this server's metadata, not client behavior.

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

Starting point for everything above: the original project as forked.
