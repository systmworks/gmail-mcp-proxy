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
| `search_emails` | Search with Gmail operators (`from:`, `subject:`, `has:attachment`, …) |
| `read_message` | Full message with decoded body |
| `read_thread` | Full thread |
| `send_email` | Send or reply to a thread |
| `create_draft` | Save a draft |
| `list_drafts` | List drafts |
| `list_labels` | All Gmail labels |
| `modify_labels` | Add/remove labels on a message |
| `trash_message` | Move to trash |
| `list_calendars` | All calendars |
| `list_events` | Events with optional time range filter |
| `search_events` | Search events by keyword |
| `get_event` | Single event by ID |

## Prerequisites

- Docker + Docker Compose
- A reverse proxy that terminates HTTPS (the examples use [caddy-docker-proxy](https://github.com/lucaslorentz/caddy-docker-proxy))
- A Google Cloud project with:
  - **Gmail API** and **Google Calendar API** enabled
  - An OAuth 2.0 **Web application** client
  - Authorized redirect URI: `https://<your-domain>/gmail/auth/callback`

## Setup

### 1. Google Cloud

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Credentials
2. Create an **OAuth 2.0 Client ID** (type: Web application)
3. Add `https://<your-domain>/gmail/auth/callback` to **Authorized redirect URIs**
4. Copy the Client ID and Client Secret

### 2. Deploy

```bash
git clone https://github.com/yourusername/gmail-mcp
cd gmail-mcp

cp .env.example .env
# Fill in GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
# JWT_SECRET can be any long random string: openssl rand -hex 32
# BASE_URL should be https://<your-domain>/gmail

docker compose up -d
```

The included `compose.yml` uses [caddy-docker-proxy](https://github.com/lucaslorentz/caddy-docker-proxy) labels. If you use a different proxy, adjust the labels or add your own reverse proxy config pointing to port `8000`.

### 3. Connect to Claude.ai

In Claude.ai → Settings → Integrations → Add MCP server:

| Field | Value |
|-------|-------|
| URL | `https://<your-domain>/gmail/mcp` |
| OAuth Client ID | your Google client ID |
| OAuth Client Secret | your Google client secret |

Click **Connect** and authenticate with your Google account.

**Two accounts:** add a second connector with URL `https://<your-domain>/gmail/work/mcp` (any alias works — `/personal/mcp`, `/work/mcp`, etc.) and authenticate with the second account. Both share the same server and OAuth flow.

## Configuration

| Variable | Description |
|----------|-------------|
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `JWT_SECRET` | Secret for signing session JWTs (any random string) |
| `BASE_URL` | Public base URL, e.g. `https://example.com/gmail` (no trailing slash) |

## Notes

- Sessions are stored in memory — a server restart requires re-authentication in Claude.ai
- Google access tokens are refreshed automatically using the stored refresh token
- The server issues 30-day JWTs; Claude re-authenticates when they expire
