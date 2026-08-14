[← Back to README](README.md)

# Setup

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
