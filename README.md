# Gmail MCP Server

Self-hosted [MCP](https://modelcontextprotocol.io) server that exposes Gmail and Google Calendar to Claude.ai (or any MCP client) via a standard OAuth 2.0 flow — no stored tokens, no pre-generated credentials.

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
| `search_emails` | Search with Gmail operators (`from:`, `subject:`, `has:attachment`, …). First 100 results include from/to/subject/date/snippet/labels, not just id/threadId. |
| `read_message` | Full message with decoded body |
| `read_thread` | Full thread |
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
  easiest path and covered step by step below; Python 3.12+ or Docker for self-hosting
  behind your own reverse proxy also work (see "Self-hosting" further down)

## Setup (Railway)

This is the full walkthrough, in the order that actually works — including the
non-obvious steps. Railway is used as the example; Render or any similar platform works
the same way, just with different menu names.

### 1. Deploy the service

1. On [railway.com](https://railway.com), **New Project → Deploy from GitHub repo** →
   select your fork of this repo. If Railway hasn't seen your GitHub account before,
   it'll ask to install its GitHub App — you can scope that to just this one repo rather
   than granting access to everything.
2. Railway detects the `Dockerfile` and starts building automatically. **The first
   deploy will crash on boot** — expected, there are no environment variables set yet.
3. Open the service → **Settings → Networking → Generate Domain**. When it asks for a
   target port, leave whatever it suggests (e.g. `8080`) — the server reads Railway's
   own `PORT` variable automatically, so this doesn't need to match anything in the code.
   You'll get a domain like `your-app-production.up.railway.app`.

### 2. Set up Google OAuth

All of this is in one Google Cloud project — create one at
[console.cloud.google.com](https://console.cloud.google.com) if you don't have one, then:

1. **OAuth consent screen** — in the left sidebar (under "Google Auth Platform"), go
   through the initial setup: choose **External**, fill in an app name and your support
   email.
2. **Audience** (left sidebar) → add **test users**: every Gmail address you plan to
   connect (e.g. your personal address and a second account). While the app is
   unpublished, only these addresses can complete the consent screen. If adding one
   comes back "ineligible" even though it's a real address you can log into, it usually
   means that Google account has never fully completed first-time setup (recovery
   info, etc.) — check that from the account itself, then retry.
3. **APIs & Services → Library** (a different section, not under "Google Auth
   Platform") → search for and enable **Gmail API** and **Google Calendar API**.
4. **Clients** (left sidebar, back under "Google Auth Platform") → **Create OAuth
   client** → Application type **Web application** → under Authorized redirect URIs,
   add `https://<your-railway-domain>/auth/callback` → Create.
5. Copy the **Client ID** and **Client Secret** it shows you.

### 3. Configure the server

Back in Railway, on the service → **Variables**, add:

| Key | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | from step 2.5 |
| `GOOGLE_CLIENT_SECRET` | from step 2.5 |
| `BASE_URL` | `https://<your-railway-domain>` — bare domain, no path, no trailing slash |
| `JWT_SECRET` | any long random string, e.g. output of `openssl rand -hex 32` |

Railway needs an explicit **Deploy changes** click after editing variables — it doesn't
redeploy on every keystroke. Once it redeploys, confirm it's up:

```bash
curl https://<your-railway-domain>/.well-known/oauth-authorization-server
```

This should return a small JSON document, not an error page.

### 4. Connect in Claude

Settings → Connectors → **Add → Add custom connector**:

| Field | Value |
|---|---|
| Name | anything, e.g. `Gmail (Personal)` |
| URL | `https://<your-railway-domain>/personal/mcp` |

Expand **Advanced settings** and fill in:

| Field | Value |
|---|---|
| OAuth Client ID | any placeholder text, e.g. `claude` |
| OAuth Client Secret | leave blank |

That last part isn't optional in practice: this server doesn't implement automatic
client registration (it's a single-user proxy, not a multi-tenant OAuth provider), so
without something in the Client ID field, Claude will show a "client registration isn't
supported" warning and refuse to connect. The value itself doesn't matter — the server
never checks it, it only needs to be non-empty.

Click **Add**, then **Connect**, and sign in with one of the Google accounts you added
as a test user. You should land back in Claude successfully authenticated.

**Second account:** repeat this whole step with a different alias in the URL —
`https://<your-railway-domain>/work/mcp` — and sign in with the second account. Any
alias name works for either connector; `/personal/` and `/work/` are just examples.
Both connectors share the same deployed server and the same OAuth setup.

## Self-hosting

If you'd rather run this yourself instead of on a PaaS platform:

```bash
git clone https://github.com/<you>/gmail-mcp-proxy
cd gmail-mcp-proxy

cp .env.example .env
# Fill in GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET (see the Google OAuth steps above)
# JWT_SECRET can be any long random string: openssl rand -hex 32
# BASE_URL should be https://<your-domain>/gmail
```

```bash
# With Python directly
pip install -r requirements.txt
set -a && . ./.env && set +a   # load the variables from .env
python server.py

# Or with the included Dockerfile
docker build -t gmail-mcp .
docker run -d --env-file .env -p 8000:8000 gmail-mcp
```

Put it behind any reverse proxy that terminates HTTPS (Caddy, nginx, Traefik, …) and
forwards `https://<your-domain>/gmail` to the container's port `8000` — set `PORT=8000`
in `.env` to match, or leave `PORT` unset since `8000` is the default.

#### Docker Compose example

A minimal `compose.yml` to run the container — uncomment the labels for whichever proxy you use:

```yaml
services:
  gmail-mcp:
    build: .
    # image: gmail-mcp           # or a prebuilt image instead of build
    env_file: .env
    restart: unless-stopped
    # expose the port only if your proxy reaches it directly (not via a shared network)
    # ports:
    #   - "8000:8000"

    labels:
      # ── caddy-docker-proxy ────────────────────────────────────────────────
      # caddy: your-domain.com
      # caddy.handle_path: /gmail*
      # caddy.handle_path.reverse_proxy: "{{upstreams 8000}}"

      # ── Traefik (strips the /gmail prefix before forwarding) ──────────────
      # traefik.enable: "true"
      # traefik.http.routers.gmail-mcp.rule: "Host(`your-domain.com`) && PathPrefix(`/gmail`)"
      # traefik.http.routers.gmail-mcp.tls.certresolver: "le"
      # traefik.http.routers.gmail-mcp.middlewares: "gmail-strip"
      # traefik.http.middlewares.gmail-strip.stripprefix.prefixes: "/gmail"
      # traefik.http.services.gmail-mcp.loadbalancer.server.port: "8000"
```

With **nginx** (no labels — point a `location` block at the container):

```nginx
location /gmail/ {
    proxy_pass http://gmail-mcp:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Note: `BASE_URL` must match the public path the proxy serves (e.g. `https://your-domain.com/gmail`), since the server builds its OAuth redirect URIs from it. Then connect in Claude the same way as step 4 above, substituting your own domain for the Railway one.

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

## Read-only accounts

To connect an account you want Claude to only ever read from — never send, delete, or
modify — add its alias to `READ_ONLY_ALIASES`, e.g. `READ_ONLY_ALIASES=work` for a
connector added at `/work/mcp`. That account's Google OAuth grant will only ever request
`gmail.readonly`/`calendar.readonly` — no write scope is ever issued for it, so even a
bug in this server can't make it send or delete anything; Google's API rejects it
regardless. The server also refuses write tool calls itself with a clear error, as a
second layer.

**This depends on Claude sending the standard OAuth `resource` parameter** identifying
which connector is authenticating, which MCP's authorization spec calls for but isn't
something this project controls. After connecting a read-only-configured account, verify
it actually took effect:

1. Check the server logs right after connecting — look for a line like
   `authorize: alias='work' resource='...' -> read-only`. If `alias` comes back empty
   instead, the restriction silently didn't apply for that connection, even though the
   account was still granted read access — reconnect and check again, and if it keeps
   happening, treat that alias as read/write for now.
2. Ask Claude, through that connector, to send a test email or trash a message. It
   should be refused.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Tests mock all Gmail/Calendar API calls (via `respx`) and cover the pure-logic
helpers (PKCE, alias parsing, MIME building) plus tool behavior that's easy to get
wrong — graceful degradation on network errors, 204-No-Content handling on deletes,
and read-only enforcement. No live Google credentials needed to run them.

## Notes

- Sessions are stored in memory — a server restart requires re-authentication in Claude.ai
- Google access tokens are refreshed automatically using the stored refresh token
- The server issues 30-day JWTs; Claude re-authenticates when they expire
