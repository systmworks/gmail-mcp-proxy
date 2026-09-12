# Gmail MCP Server

Self-hosted [MCP](https://modelcontextprotocol.io) server that exposes Gmail and Google Calendar to Claude.ai (or any MCP client) via a standard OAuth 2.0 flow — no stored tokens, no pre-generated credentials.

## Quick Links

- [Setup](SETUP.md) — Railway deployment walkthrough and self-hosting instructions
- [Changelog](CHANGELOG.md) — version history

## How it works

```
Claude.ai ──[OAuth]──► This server ──[OAuth]──► Google
Claude.ai ──[MCP]────► This server ──[Gmail API]──► Gmail / Calendar
```

The server acts as an OAuth proxy: it presents itself as an OAuth 2.0 authorization server to Claude, and internally delegates authentication to Google. After the user grants access, Google tokens are stored server-side (in memory) and injected per-request. A short-lived JWT is issued to Claude as the bearer token.

**Multiple accounts** — add the server twice in Claude with different alias URLs (`/personal/mcp`, `/work/mcp`). Each session is isolated; authenticate each with a different Google account.

## Tools

| Tool | Description |
|------|-------------|
| `get_profile` | Gmail account profile |
| `search_emails` | Search with Gmail operators (`from:`, `subject:`, `has:attachment`, …). Enriches results up to `SEARCH_ENRICH_LIMIT` — see Configuration below. Use `has:attachment` in the query to filter to messages with attachments — enrichment itself doesn't include attachment info (see the `SEARCH_ENRICH_LIMIT` row below). |
| `read_message` | Full message with decoded body and attachment metadata (filename/attachmentId/mimeType/size) |
| `read_thread` | Full thread |
| `get_attachment` | Download an attachment's bytes (base64) by `message_id`/`attachment_id` from `read_message`'s `attachments` list — see `ATTACHMENT_MAX_MB` |
| `send_email` | Send or reply to a thread |
| `create_draft` | Save a draft |
| `list_drafts` | List drafts |
| `send_draft` | Send an existing draft |
| `update_draft` | Replace the content of an existing draft |
| `delete_draft` | Permanently delete a draft |
| `list_labels` | All Gmail labels |
| `create_label` | Create a new label |
| `update_label` | Rename or change visibility of a label |
| `delete_label` | Permanently delete a label |
| `modify_labels` | Add/remove labels on a message |
| `report_phishing` | Mark a message as spam |
| `trash_message` | Move to trash |
| `list_calendars` | All calendars |
| `list_events` | Events with optional time range filter |
| `search_events` | Search events by keyword |
| `get_event` | Single event by ID |

## Prerequisites

- A GitHub account with this repo forked (or cloned) into it
- A Google Cloud project (free) — used only to issue OAuth credentials, no billing needed
- A place to run the server with HTTPS — a PaaS platform (Railway, Render, etc.) is the
  easiest path and covered step by step in [Setup](SETUP.md); Python 3.12+ or Docker
  for self-hosting behind your own reverse proxy also work (see
  [Setup](SETUP.md#self-hosting))

## Configuration

| Variable | Description |
|----------|-------------|
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `JWT_SECRET` | Secret for signing session JWTs (any random string) |
| `BASE_URL` | Public base URL, no trailing slash. Bare domain on a PaaS platform (`https://your-app.up.railway.app`), or with a path prefix if self-hosting behind a shared reverse proxy (`https://example.com/gmail`). |
| `ALLOWED_REDIRECT_URIS` | Optional. Comma-separated allowlist of OAuth redirect URIs `/authorize` will accept. Defaults to Claude.ai's callback (`https://claude.ai/api/mcp/auth_callback`) — only change this if you're connecting a non-Claude.ai MCP client. |
| `LOG_LEVEL` | Optional. Python logging level (`INFO`, `WARNING`, `DEBUG`, etc.). Defaults to `INFO`. |
| `READ_ONLY_ALIASES` | Optional. Comma-separated list of connector aliases (e.g. `work`) that should be restricted to read-only access — no send, draft, label changes, or trash. See below. |
| `SEARCH_ENRICH_LIMIT` | Optional. How many `search_emails` results (0–200) get enriched with from/to/subject/date/snippet/labels instead of bare id/threadId. Defaults to `20`. Trades tokens for fewer round-trips: each enriched result costs a few hundred tokens, which pays off when scanning many results at once but is wasted on results never looked at. `read_message` is unaffected either way — it always returns a message's full decoded text body and attachment metadata (not attachment bytes — use `get_attachment` for that), so it's the more expensive call per-message, just not per-search. Enrichment itself can't include attachment info even if you raise this — it uses a lighter-weight Gmail fetch that doesn't include the MIME structure attachments live in; use `has:attachment` in the search query instead. |
| `SEARCH_ENRICH_ATTEMPTS` | Optional. Total attempts (1–5) per message before giving up on enrichment and falling back to bare id/threadId. Defaults to `2`. Raise this if you're seeing a lot of bare results in `search_emails` output — self-hosted networks under the concurrency of a full batch tend to see more transient failures than a Railway/PaaS deployment. |
| `API_RETRY_ATTEMPTS` | Optional. Total attempts (1–5) for a write-tool call (`send_email`, `modify_labels`, `create_label`, etc.) before giving up. Defaults to `2`. Raise this if a bulk operation (e.g. labelling hundreds of messages) is hitting Gmail's rate limit partway through. |
| `ATTACHMENT_MAX_MB` | Optional. Max attachment size (1–25MB, decoded) `get_attachment` will fetch. Defaults to `3`. Attachment bytes return as base64 text inside the MCP tool result — straight into the calling LLM's context, not just over the network — so this is set well below Gmail's own 25MB cap on this endpoint. Raise it if you need larger attachments and have the context budget. |

**`search_emails` result fields**

| | Fields |
|---|---|
| Simple (bare) | `id`, `threadId` |
| Enriched | `id`, `threadId`, `from`, `to`, `subject`, `date`, `snippet`, `labels` |

Results beyond `SEARCH_ENRICH_LIMIT` come back simple; everything within it comes
back enriched (or simple, if that one message's metadata fetch failed — see
CHANGELOG 0.13).

## Read-only accounts

To connect an account you want Claude to only ever read from — never send, delete, or
modify — add its alias to `READ_ONLY_ALIASES`, e.g. `READ_ONLY_ALIASES=work` for a
connector added at `/work/mcp`. That account's Google OAuth grant will only ever request
`gmail.readonly`/`calendar.readonly` — no write scope is ever issued for it, so even a
bug in this server can't make it send or delete anything; Google's API rejects it
regardless. The server also refuses write tool calls itself with a clear error, as a
second layer.

**Enforcement is server-side and unconditional** — which alias a request comes in
through is derived from the URL path on every single request, not something the
client asserts, so a restricted alias stays restricted even if the OAuth client
never echoes back the `resource` parameter during authorization. That parameter
still affects which Google scopes get requested up front (best-effort
minimization — a restricted account ideally never even gets asked to grant write
scopes), so it's still worth confirming it worked:

1. Check the server logs right after connecting — look for a line like
   `authorize: alias='work' resource='...' -> read-only`. If `alias` comes back
   empty, Google still asked for full read/write scopes for that grant (the OAuth
   client didn't send `resource`) — the tool-level restriction still applies
   regardless, but reconnect and check again if you want the scope-minimization
   layer working too.
2. Ask Claude, through that connector, to send a test email or trash a message. It
   should be refused.

## Development

```bash
pip install -r requirements-dev.txt
pytest          # test suite
ruff check .    # lint
mypy            # type check (config in pyproject.toml)
```

Tests mock all Gmail/Calendar API calls (via `respx`) and cover the pure-logic
helpers (PKCE, alias parsing, MIME building) plus tool behavior that's easy to get
wrong — graceful degradation on network errors, 204-No-Content handling on deletes,
and read-only enforcement. No live Google credentials needed to run them.

## Notes

- Sessions are stored in memory — a server restart requires re-authentication in Claude.ai
- Runs as a single process — don't scale to multiple Railway replicas or `uvicorn --workers N`.
  Session/state stores are per-process in-memory, so a request landing on a different
  process than the one that authenticated it would fail as if unauthenticated.
- Google access tokens are refreshed automatically using the stored refresh token
- The server issues 30-day JWTs; Claude re-authenticates when they expire
