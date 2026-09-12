"""
Gmail MCP Server — FastMCP + Google OAuth proxy

Flow:
  Claude.ai ──[OAuth]──► This server ──[OAuth]──► Google
  Claude.ai ──[MCP]────► This server ──[Gmail API]──► Gmail/Calendar
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from contextvars import ContextVar
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gmail_mcp")

# ── Config ─────────────────────────────────────────────────────────────────────

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")  # e.g. https://your-app.up.railway.app
JWT_SECRET = os.environ["JWT_SECRET"]

HTTPX_TIMEOUT = 30.0
STATE_TTL = 600  # seconds; abandoned OAuth flows are purged after this
# Max search_emails results to fetch metadata for per call. User-configurable —
# direct token-cost/usefulness trade-off, see README. Clamped so a bad env value
# can't silently disable enrichment or blow past Gmail's quota.
SEARCH_ENRICH_LIMIT = max(0, min(200, int(os.environ.get("SEARCH_ENRICH_LIMIT", "20"))))
# Total attempts (including the first) per message before giving up on enrichment.
# Some networks (e.g. self-hosted behind a home NAT/router under the concurrency
# of a full batch) see more transient failures than a single retry recovers.
SEARCH_ENRICH_ATTEMPTS = max(1, min(5, int(os.environ.get("SEARCH_ENRICH_ATTEMPTS", "2"))))
_ENRICH_RETRY_DELAY = 0.3  # seconds between attempts

# Max attachment size (decoded bytes) get_attachment will fetch/return. Attachment
# bytes come back as base64 text inside the MCP tool result — i.e. straight into the
# calling LLM's context, not just over the network — so the default is well below
# Gmail's own 25MB decoded cap on this endpoint (base64 inflates ~33% and tokenizes
# poorly). Raise it if you need larger attachments and have the context budget.
ATTACHMENT_MAX_MB = max(1, min(25, int(os.environ.get("ATTACHMENT_MAX_MB", "3"))))
ATTACHMENT_MAX_BYTES = ATTACHMENT_MAX_MB * 1024 * 1024

DEFAULT_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"

# Redirect URIs /authorize is allowed to send the auth code to. Without this allowlist,
# an attacker can craft an /authorize?redirect_uri=<attacker-controlled> link and, once
# the victim completes Google's consent screen, receive the resulting single-use code
# themselves — full account takeover if PKCE isn't also enforced (see _authorize below).
ALLOWED_REDIRECT_URIS = frozenset(
    u.strip() for u in os.environ.get(
        "ALLOWED_REDIRECT_URIS", DEFAULT_REDIRECT_URI
    ).split(",") if u.strip()
)

def _parse_read_only_aliases(value: str) -> frozenset[str]:
    """Split a comma-separated READ_ONLY_ALIASES value into a set of bare alias
    names. Filters on the *final* stripped value (walrus operator) rather than on
    an intermediate one — a naive `a.strip().strip("/") for a in ... if a.strip()`
    can pass its own filter on a slash-only token (e.g. a stray "/") whose
    whitespace-only strip is truthy, then collapse to "" once slashes are also
    stripped, silently inserting the empty string (the *unaliased* connector's own
    alias) into the restricted set."""
    return frozenset(
        stripped for a in value.split(",")
        if (stripped := a.strip().strip("/"))
    )


# Aliased connectors (e.g. /work/mcp) named here get Google scopes covering only
# read access — see _google_scopes() below.
READ_ONLY_ALIASES = _parse_read_only_aliases(os.environ.get("READ_ONLY_ALIASES", ""))

GOOGLE_SCOPES_BASE = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
GOOGLE_SCOPES_WRITE = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _google_scopes(read_only: bool) -> str:
    scopes = GOOGLE_SCOPES_BASE if read_only else GOOGLE_SCOPES_BASE + GOOGLE_SCOPES_WRITE
    return " ".join(scopes)


GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GCAL = "https://www.googleapis.com/calendar/v3"

# Outbound Gmail/Calendar API calls retry on these — rate limiting and server
# errors are usually transient. Other 4xx (403/404, etc.) are permanent.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# HTTP methods safe to retry even on a network-level error (timeout, connection
# reset) rather than just a definite HTTP status. GET/HEAD are defined by HTTP
# itself as safe/idempotent — retrying one can't duplicate an effect. POST/PUT/
# PATCH/DELETE are not: whether the original request already landed server-side
# before a network error is ambiguous, so retrying one of those risks silently
# duplicating it (e.g. sending the same email twice). See _request_with_retry.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})

# Total attempts (including the first) for an outbound API call before giving up.
# A bulk write operation (e.g. labelling hundreds of messages) otherwise fails
# outright the moment Gmail rate-limits a single call, with no chance to recover;
# a read hitting a transient 429/503 (or, for GET/HEAD, a network error) gets the
# same benefit.
API_RETRY_ATTEMPTS = max(1, min(5, int(os.environ.get("API_RETRY_ATTEMPTS", "2"))))
_API_RETRY_DELAY = 0.3  # seconds between attempts

# Shared connection-pooled client for all outbound Gmail/Calendar/Google OAuth requests.
# Created/closed around the ASGI lifespan in _App.__call__ — avoids paying a fresh
# TCP+TLS handshake to googleapis.com on every single tool call.
_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized — server lifespan hasn't started")
    return _http_client


# ── In-memory stores ───────────────────────────────────────────────────────────
# Fine for single-process personal use; restart clears sessions (re-auth needed).

_state_store: dict[str, dict] = {}   # our_state  → {..., "created": ts}
_code_store: dict[str, dict] = {}    # our_code   → {jti, email, code_challenge, ..., "created": ts}
_token_store: dict[str, dict] = {}   # jti        → {access_token, refresh_token, expiry, email, jwt_exp}
_refresh_locks: dict[str, asyncio.Lock] = {}  # jti → lock guarding concurrent token refreshes

# ── Per-request context ────────────────────────────────────────────────────────

_session_jti: ContextVar[str] = ContextVar("session_jti", default="")
_read_only: ContextVar[bool] = ContextVar("read_only", default=False)

# ── Helpers ────────────────────────────────────────────────────────────────────

class ReauthRequired(Exception):
    """Raised when a session is unknown or Google has revoked/expired the refresh token."""


def _enc(value: str) -> str:
    """URL-encode a value before interpolating it into a REST URL *path* segment
    (never into a JSON request body field, where it doesn't apply). Google's own
    generated ids are base64url (no "/" by construction) so this rarely bites in
    practice, but calendar_id in particular can be an arbitrary caller-supplied
    string (e.g. an email address used as a calendar id) — encode unconditionally
    rather than relying on how unlikely a literal "/" is today."""
    return quote(str(value), safe="")


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


def _purge_expired_states() -> None:
    now = time.time()
    expired = [k for k, v in _state_store.items() if now - v.get("created", now) > STATE_TTL]
    for k in expired:
        _state_store.pop(k, None)
    expired = [k for k, v in _code_store.items() if now - v.get("created", now) > STATE_TTL]
    for k in expired:
        _code_store.pop(k, None)


def _purge_expired_tokens() -> None:
    # Runs on every incoming HTTP request (not just ones for the session being
    # purged), so a session's jwt_exp elapsing while _refresh() is mid-flight for
    # that same session — awaiting Google's response — could otherwise have this
    # pop the entry out from under it: _refresh then writes the new token into a
    # dict no longer referenced by _token_store, silently losing the session
    # despite _refresh reporting success. Skip a session whose refresh lock is
    # currently held; the next purge pass (on the next request) will catch it once
    # the in-flight refresh finishes and releases the lock.
    now = time.time()
    expired = [jti for jti, d in _token_store.items() if now >= d.get("jwt_exp", float("inf"))]
    for jti in expired:
        lock = _refresh_locks.get(jti)
        if lock is not None and lock.locked():
            continue
        _token_store.pop(jti, None)
        _refresh_locks.pop(jti, None)


async def _refresh(jti: str) -> str:
    # Lock per session so two concurrent requests hitting an expired token don't
    # both fire a refresh_token grant (Google can reject the second as reused).
    lock = _refresh_locks.setdefault(jti, asyncio.Lock())
    async with lock:
        d = _token_store.get(jti)
        if not d:
            raise ReauthRequired("session not found")
        if time.time() < d["expiry"] - 60:
            # Another coroutine already refreshed while we waited on the lock.
            return d["access_token"]
        r = await _request_with_retry("POST", "https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": d["refresh_token"],
            "grant_type": "refresh_token",
        })
        t = r.json()
        log.info("session %s: refresh response has_access_token=%s expires_in=%s",
                 jti, "access_token" in t, t.get("expires_in"))
        if "access_token" not in t:
            _token_store.pop(jti, None)
            _refresh_locks.pop(jti, None)
            reason = t.get("error_description", t.get("error", "refresh failed"))
            log.warning("token refresh failed, session needs re-auth: %s", reason)
            raise ReauthRequired(reason)
        d["access_token"] = t["access_token"]
        d["expiry"] = time.time() + t.get("expires_in", 3600)
        log.info("session %s: refreshed, new expiry in %.0fs (now=%.0f, expiry=%.0f)",
                 jti, d["expiry"] - time.time(), time.time(), d["expiry"])
        return d["access_token"]


async def _google_access_token(jti: str) -> str:
    d = _token_store.get(jti)
    if not d:
        raise ReauthRequired("session not found")
    remaining = d["expiry"] - time.time()
    if remaining <= 60:
        log.info("session %s: access token needs refresh (remaining=%.0fs, expiry=%.0f, now=%.0f)",
                 jti, remaining, d["expiry"], time.time())
        return await _refresh(jti)
    return d["access_token"]


async def _auth() -> dict:
    # Resolves the token from _token_store at the moment of use rather than trusting
    # a value captured earlier in _App.__call__ — self-hosted (non-Railway) traffic
    # showed a refreshed token sometimes never reaching the task that actually makes
    # the Gmail call, so a token good for another hour got used to build a header
    # for a task still holding an expired one from before the refresh.
    jti = _session_jti.get()
    if not jti:
        raise RuntimeError("not authenticated")
    token = await _google_access_token(jti)
    return {"Authorization": f"Bearer {token}"}


async def _request_with_retry(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """API call with retry — up to API_RETRY_ATTEMPTS total tries on a definite
    retryable HTTP status (429, 5xx) before giving up. Callers keep calling
    r.raise_for_status() as before: a final retryable-status response is returned
    as-is (so that still raises).

    Network-level errors (timeouts, connection resets) are only retried for
    GET/HEAD (_IDEMPOTENT_METHODS) — methods HTTP itself defines as safe to repeat.
    For everything else (POST/PUT/PATCH/DELETE), whether the original request
    already landed server-side before the network error is ambiguous, so a network
    error propagates immediately instead: blindly retrying a non-idempotent write
    (send_email, create_draft, etc.) risks silently duplicating it. This falls out
    of `method`, already required at every call site — no extra parameter for
    callers to get backwards."""
    c = _client()
    r: httpx.Response | None = None
    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            r = await c.request(method, url, **kwargs)
        except httpx.HTTPError:
            if method not in _IDEMPOTENT_METHODS or attempt == API_RETRY_ATTEMPTS:
                raise
            await asyncio.sleep(_API_RETRY_DELAY)
            continue
        if r.status_code not in _RETRYABLE_STATUSES:
            return r
        if attempt < API_RETRY_ATTEMPTS:
            await asyncio.sleep(_API_RETRY_DELAY)
    assert r is not None
    return r


def _require_write() -> None:
    if _read_only.get():
        raise PermissionError("this connection is authorized read-only; write actions are disabled")


def _effective_read_only(payload: dict, alias: str) -> bool:
    """A restricted alias stays restricted even if the JWT itself says
    read_only=False — e.g. because READ_ONLY_ALIASES was edited to add this alias
    *after* the JWT was already minted. JWTs are immutable for their 30-day life, so
    without this re-check, a config change would only take effect for brand-new
    logins — existing sessions would keep read/write access until their token
    happened to expire. `alias` here comes from server-side path routing
    (_split_alias) on the *current* request, not anything the client asserts, so
    this can't be bypassed by client behavior either."""
    return payload.get("read_only", False) or alias in READ_ONLY_ALIASES


def _build_email(to: str, subject: str, body: str, cc: str = "",
                 in_reply_to: str = "", references: str = "") -> str:
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _find_bodies_and_attachments(part: dict, bodies: dict[str, str],
                                 attachments: list[dict]) -> None:
    """Single recursive pass over a message's MIME part tree collecting both decoded
    text bodies (text/plain and text/html) and attachment metadata (filename/
    mimeType/size/partId) — one walk instead of two separate ones over the same
    tree. Unlike a body-only walker, this can't early-exit once both body types are
    found: attachments can appear anywhere in the tree and all of them must be
    collected regardless.

    Deliberately surfaces partId, not attachmentId, for attachments: Gmail's
    attachmentId is only valid transiently and has been observed to differ across
    separate messages.get calls for the very same message/attachment, so it can't be
    handed out here for a later get_attachment call to reuse. partId is documented
    as immutable, so get_attachment takes that instead and re-resolves a fresh
    attachmentId from it within its own single request.
    """
    mime = part.get("mimeType")
    if mime in ("text/plain", "text/html") and mime not in bodies:
        raw = part.get("body", {}).get("data", "")
        if raw:
            bodies[mime] = base64.urlsafe_b64decode(raw + "==").decode("utf-8", errors="replace")

    filename = part.get("filename")
    attachment_id = part.get("body", {}).get("attachmentId")
    if filename and attachment_id:
        attachments.append({
            "partId": part.get("partId", ""),
            "filename": filename,
            "mimeType": part.get("mimeType", ""),
            "size": part.get("body", {}).get("size", 0),
        })

    for sub in part.get("parts", []):
        _find_bodies_and_attachments(sub, bodies, attachments)


def _find_part_by_id(part: dict, part_id: str) -> dict | None:
    """Locate a MIME part by its partId, for get_attachment to re-resolve a fresh
    attachmentId from within a single request (see _find_bodies_and_attachments'
    docstring for why attachmentId itself can't be reused across calls)."""
    if part.get("partId") == part_id:
        return part
    for sub in part.get("parts", []):
        found = _find_part_by_id(sub, part_id)
        if found is not None:
            return found
    return None


# ── FastMCP tools ──────────────────────────────────────────────────────────────

mcp = FastMCP("Gmail MCP")


async def _call(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Shared auth + retry + raise-for-status boilerplate for every Gmail/Calendar
    API call, read or write alike — both go through the same _request_with_retry
    (see its own docstring for exactly what it does and doesn't retry). Always
    injects the auth header itself — callers must not pass their own `headers`
    kwarg (Google's OAuth endpoints build their own headers directly and don't go
    through this helper, since they're not authenticated the same way)."""
    r = await _request_with_retry(method, url, headers=await _auth(), **kwargs)
    r.raise_for_status()
    return r


async def _call_json(method: str, url: str, **kwargs: Any) -> dict:
    return (await _call(method, url, **kwargs)).json()


async def _call_list(method: str, url: str, *, key: str, **kwargs: Any) -> list[dict]:
    return (await _call(method, url, **kwargs)).json().get(key, [])


async def _call_delete(method: str, url: str, deleted_id: str, **kwargs: Any) -> dict:
    # raise_for_status() only ever raises on 4xx/5xx regardless of which specific
    # 2xx code a delete endpoint returns (200 vs 204) — no special-casing needed,
    # and neither caller wants the (possibly-empty) response body anyway.
    await _call(method, url, **kwargs)
    return {"deleted": deleted_id}


@mcp.tool
async def get_profile() -> dict:
    """Get the authenticated Gmail account's profile."""
    return await _call_json("GET", f"{GMAIL}/profile")


@mcp.tool
async def search_emails(query: str, max_results: int = 20) -> list[dict]:
    """Search Gmail. Supports all Gmail search operators (from:, subject:, has:attachment, etc.).
    Each of the first several results (deployment-configurable, default 20) includes
    from/to/subject/date/snippet/labels alongside id/threadId, so most questions about the
    results don't need a follow-up read_message call. Beyond that limit, only id/threadId
    are included."""
    messages = (await _call_json("GET", f"{GMAIL}/messages",
                                 params={"q": query, "maxResults": max_results})).get("messages", [])

    to_enrich, rest = messages[:SEARCH_ENRICH_LIMIT], messages[SEARCH_ENRICH_LIMIT:]

    async def _fetch_metadata(message_id: str) -> httpx.Response | None:
        # Deliberately NOT routed through _call/_request_with_retry — _enrich just
        # below already implements its own retry loop (SEARCH_ENRICH_ATTEMPTS) with
        # degrade-to-bare-id-on-exhaustion semantics that predate and differ from
        # the generic retry helper; kept as its own reviewed, separately-tested path.
        try:
            return await _client().get(f"{GMAIL}/messages/{_enc(message_id)}", headers=await _auth(),
                               params={"format": "metadata",
                                       "metadataHeaders": ["From", "To", "Subject", "Date"]})
        except httpx.HTTPError:
            return None

    async def _enrich(msg: dict) -> dict:
        # Network errors, rate limiting, and server errors are usually transient —
        # retry up to SEARCH_ENRICH_ATTEMPTS total tries before degrading. Other 4xx
        # (403/404, etc.) are permanent and stop retrying immediately.
        er = None
        attempt = 0
        for attempt in range(1, SEARCH_ENRICH_ATTEMPTS + 1):
            er = await _fetch_metadata(msg["id"])
            if er is not None and er.status_code not in _RETRYABLE_STATUSES:
                break
            if attempt < SEARCH_ENRICH_ATTEMPTS:
                await asyncio.sleep(_ENRICH_RETRY_DELAY)

        if er is None:
            log.warning("search_emails: enrich failed for %s (network error, %d attempt%s)",
                        msg["id"], attempt, "" if attempt == 1 else "s")
            return msg
        if not er.is_success:
            log.warning("search_emails: enrich failed for %s (HTTP %s, %d attempt%s): %s",
                        msg["id"], er.status_code, attempt, "" if attempt == 1 else "s",
                        er.text[:200])
            return msg
        data = er.json()
        hdrs = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
        return {
            **msg,
            "from": hdrs.get("From", ""),
            "to": hdrs.get("To", ""),
            "subject": hdrs.get("Subject", ""),
            "date": hdrs.get("Date", ""),
            "snippet": data.get("snippet", ""),
            "labels": data.get("labelIds", []),
        }

    enriched = await asyncio.gather(*(_enrich(m) for m in to_enrich))

    return list(enriched) + rest


@mcp.tool
async def read_message(message_id: str) -> dict:
    """Read a Gmail message by ID. Returns headers, decoded body, and attachment
    metadata (filename/partId/mimeType/size) — use get_attachment to download
    an attachment's bytes."""
    data = await _call_json("GET", f"{GMAIL}/messages/{_enc(message_id)}", params={"format": "full"})

    payload = data.get("payload", {})
    bodies: dict[str, str] = {}
    attachments: list[dict] = []
    _find_bodies_and_attachments(payload, bodies, attachments)
    body = bodies.get("text/plain") or bodies.get("text/html", "")

    hdrs = {h["name"]: h["value"] for h in payload.get("headers", [])}
    return {
        "id": data["id"],
        "threadId": data["threadId"],
        "from": hdrs.get("From", ""),
        "to": hdrs.get("To", ""),
        "subject": hdrs.get("Subject", ""),
        "date": hdrs.get("Date", ""),
        "snippet": data.get("snippet", ""),
        "labels": data.get("labelIds", []),
        "body": body,
        "attachments": attachments,
    }


@mcp.tool
async def read_thread(thread_id: str) -> dict:
    """Read a full Gmail thread."""
    # Intentionally a raw passthrough (unlike read_message) — each message's raw
    # payload already contains attachment parts (filename/partId/size) in its MIME
    # tree; get_attachment works from any message's own "id" here.
    return await _call_json("GET", f"{GMAIL}/threads/{_enc(thread_id)}")


@mcp.tool
async def get_attachment(message_id: str, part_id: str) -> dict:
    """Download a Gmail attachment's bytes (as standard base64) by message_id and
    partId from read_message's attachments list. Rejects attachments larger than
    ATTACHMENT_MAX_MB without downloading them."""
    payload = (await _call_json("GET", f"{GMAIL}/messages/{_enc(message_id)}",
                                params={"format": "full"})).get("payload", {})

    part = _find_part_by_id(payload, part_id)
    if part is None or not part.get("filename") or not part.get("body", {}).get("attachmentId"):
        raise ValueError(f"no attachment with partId {part_id!r} found on message {message_id!r}")

    filename = part["filename"]
    mime_type = part.get("mimeType", "")
    meta_size = part.get("body", {}).get("size", 0)
    attachment_id = part["body"]["attachmentId"]

    if meta_size > ATTACHMENT_MAX_BYTES:
        raise ValueError(
            f"attachment {filename!r} is {meta_size} bytes, exceeds "
            f"ATTACHMENT_MAX_MB ({ATTACHMENT_MAX_MB}MB) limit"
        )

    att = await _call_json("GET", f"{GMAIL}/messages/{_enc(message_id)}/attachments/{_enc(attachment_id)}")
    raw_data = att.get("data")
    if raw_data is None:
        raise ValueError(f"attachment {filename!r} response from Gmail is missing its 'data' field")
    raw = base64.urlsafe_b64decode(raw_data + "==")
    if len(raw) > ATTACHMENT_MAX_BYTES:
        raise ValueError(
            f"attachment {filename!r} is {len(raw)} bytes, exceeds "
            f"ATTACHMENT_MAX_MB ({ATTACHMENT_MAX_MB}MB) limit"
        )

    return {
        "partId": part_id,
        "messageId": message_id,
        "filename": filename,
        "mimeType": mime_type,
        "size": len(raw),
        "data": base64.b64encode(raw).decode(),
    }


@mcp.tool
async def send_email(to: str, subject: str, body: str, cc: str = "",
                     reply_to_message_id: str = "") -> dict:
    """Send an email. Use reply_to_message_id to reply within a thread."""
    _require_write()
    thread_id = ""
    in_reply_to = ""
    references = ""
    if reply_to_message_id:
        # Fail loudly (via _call's raise_for_status) rather than silently sending
        # an unthreaded standalone email when the caller explicitly asked for a reply.
        msg = await _call_json("GET", f"{GMAIL}/messages/{_enc(reply_to_message_id)}",
                               params={"format": "metadata",
                                       "metadataHeaders": ["Message-ID", "References"]})
        thread_id = msg.get("threadId", "")
        hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        in_reply_to = hdrs.get("Message-ID", "")
        references = (hdrs.get("References", "") + " " + in_reply_to).strip()

    payload: dict = {"raw": _build_email(to, subject, body, cc, in_reply_to, references)}
    if thread_id:
        payload["threadId"] = thread_id
    return await _call_json("POST", f"{GMAIL}/messages/send", json=payload)


@mcp.tool
async def create_draft(to: str, subject: str, body: str, cc: str = "") -> dict:
    """Create a Gmail draft."""
    _require_write()
    return await _call_json("POST", f"{GMAIL}/drafts",
                            json={"message": {"raw": _build_email(to, subject, body, cc)}})


@mcp.tool
async def list_drafts(max_results: int = 10) -> list[dict]:
    """List Gmail drafts."""
    return await _call_list("GET", f"{GMAIL}/drafts", key="drafts", params={"maxResults": max_results})


@mcp.tool
async def send_draft(draft_id: str) -> dict:
    """Send an existing Gmail draft."""
    _require_write()
    return await _call_json("POST", f"{GMAIL}/drafts/send", json={"id": draft_id})


@mcp.tool
async def update_draft(draft_id: str, to: str, subject: str, body: str,
                       cc: str = "") -> dict:
    """Replace the content of an existing Gmail draft."""
    _require_write()
    return await _call_json("PUT", f"{GMAIL}/drafts/{_enc(draft_id)}",
                            json={"message": {"raw": _build_email(to, subject, body, cc)}})


@mcp.tool
async def delete_draft(draft_id: str) -> dict:
    """Permanently delete a Gmail draft."""
    _require_write()
    return await _call_delete("DELETE", f"{GMAIL}/drafts/{_enc(draft_id)}", draft_id)


@mcp.tool
async def list_labels() -> list[dict]:
    """List all Gmail labels."""
    return await _call_list("GET", f"{GMAIL}/labels", key="labels")


@mcp.tool
async def create_label(name: str, label_list_visibility: str = "labelShow",
                       message_list_visibility: str = "show") -> dict:
    """Create a new Gmail label."""
    _require_write()
    return await _call_json("POST", f"{GMAIL}/labels",
                            json={"name": name,
                                  "labelListVisibility": label_list_visibility,
                                  "messageListVisibility": message_list_visibility})


@mcp.tool
async def update_label(label_id: str, name: str | None = None,
                       label_list_visibility: str | None = None,
                       message_list_visibility: str | None = None) -> dict:
    """Rename or change visibility of an existing Gmail label."""
    _require_write()
    body = {}
    if name is not None:
        body["name"] = name
    if label_list_visibility is not None:
        body["labelListVisibility"] = label_list_visibility
    if message_list_visibility is not None:
        body["messageListVisibility"] = message_list_visibility
    return await _call_json("PATCH", f"{GMAIL}/labels/{_enc(label_id)}", json=body)


@mcp.tool
async def delete_label(label_id: str) -> dict:
    """Permanently delete a Gmail label."""
    _require_write()
    return await _call_delete("DELETE", f"{GMAIL}/labels/{_enc(label_id)}", label_id)


@mcp.tool
async def modify_labels(message_id: str, add: list[str] | None = None,
                        remove: list[str] | None = None) -> dict:
    """Add or remove labels on a Gmail message."""
    _require_write()
    return await _call_json("POST", f"{GMAIL}/messages/{_enc(message_id)}/modify",
                            json={"addLabelIds": add or [], "removeLabelIds": remove or []})


@mcp.tool
async def report_phishing(message_id: str) -> dict:
    """Mark a Gmail message as spam."""
    _require_write()
    return await _call_json("POST", f"{GMAIL}/messages/{_enc(message_id)}/modify",
                            json={"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]})


@mcp.tool
async def trash_message(message_id: str) -> dict:
    """Move a Gmail message to trash."""
    _require_write()
    return await _call_json("POST", f"{GMAIL}/messages/{_enc(message_id)}/trash")


@mcp.tool
async def list_calendars() -> list[dict]:
    """List all Google Calendars."""
    return await _call_list("GET", f"{GCAL}/users/me/calendarList", key="items")


@mcp.tool
async def list_events(calendar_id: str = "primary", time_min: str = "",
                      time_max: str = "", max_results: int = 20) -> list[dict]:
    """List calendar events. time_min/time_max in RFC3339 (e.g. 2026-05-20T00:00:00Z)."""
    params: dict = {"maxResults": max_results, "singleEvents": True, "orderBy": "startTime"}
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    return await _call_list("GET", f"{GCAL}/calendars/{_enc(calendar_id)}/events", key="items", params=params)


@mcp.tool
async def search_events(query: str, calendar_id: str = "primary",
                        max_results: int = 10) -> list[dict]:
    """Search calendar events by keyword."""
    return await _call_list("GET", f"{GCAL}/calendars/{_enc(calendar_id)}/events", key="items",
                            params={"q": query, "maxResults": max_results, "singleEvents": True})


@mcp.tool
async def get_event(event_id: str, calendar_id: str = "primary") -> dict:
    """Get a specific calendar event by ID."""
    return await _call_json("GET", f"{GCAL}/calendars/{_enc(calendar_id)}/events/{_enc(event_id)}")


# ── OAuth endpoints ────────────────────────────────────────────────────────────

def _base_oauth_metadata() -> dict:
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/authorize",
        "token_endpoint": f"{BASE_URL}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
    }


async def _oauth_server_metadata(req: Request) -> JSONResponse:
    return JSONResponse({**_base_oauth_metadata(), "scopes_supported": ["gmail"]})


async def _openid_configuration(req: Request) -> JSONResponse:
    return JSONResponse({**_base_oauth_metadata(), "scopes_supported": ["openid", "gmail"]})


async def _protected_resource(req: Request) -> JSONResponse:
    # The alias this was reached through (if any) — stashed into scope["state"] by
    # _App.__call__ before the alias gets stripped for routing. Echoed back here so
    # Claude's OAuth client round-trips it as the 'resource' param on /authorize,
    # letting _authorize tell which aliased connector is authenticating.
    alias = getattr(req.state, "alias", "")
    resource = f"{BASE_URL}/{alias}/mcp" if alias else f"{BASE_URL}/mcp"
    return JSONResponse({
        "resource": resource,
        "authorization_servers": [BASE_URL],
    })


async def _authorize(req: Request):
    p = req.query_params
    redirect_uri = p.get("redirect_uri", DEFAULT_REDIRECT_URI)
    if redirect_uri not in ALLOWED_REDIRECT_URIS:
        return Response("Unknown redirect_uri", status_code=400)
    if not p.get("code_challenge"):
        return Response("PKCE code_challenge is required", status_code=400)
    if p.get("code_challenge_method", "S256") != "S256":
        return Response("Only the S256 code_challenge_method is supported", status_code=400)

    # alias comes from server-side path routing (req.state.alias, set by
    # _App.__call__ from the actual URL this request came in through) — never from
    # the client-echoed 'resource' query parameter. A restricted alias must stay
    # restricted even if an OAuth client fails to echo 'resource' correctly (or
    # omits it, or a malicious client sends a wrong one) during a restricted-alias
    # authorization flow — otherwise the resulting Google grant gets full write
    # scope and the minted JWT gets read_only=False, and that JWT (not which alias
    # it was created for) is what travels with the token afterward.
    alias = getattr(req.state, "alias", "")
    resource = p.get("resource")
    read_only = alias in READ_ONLY_ALIASES
    log.info("authorize: alias=%r resource=%r -> %s", alias, resource,
              "read-only" if read_only else "read-write")

    our_state = secrets.token_urlsafe(16)
    _state_store[our_state] = {
        "client_state": p.get("state"),
        "client_redirect_uri": redirect_uri,
        "code_challenge": p.get("code_challenge"),
        "read_only": read_only,
        "created": time.time(),
    }
    return RedirectResponse(
        "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": f"{BASE_URL}/auth/callback",
            "response_type": "code",
            "scope": _google_scopes(read_only),
            "state": our_state,
            "access_type": "offline",
            "prompt": "consent",
        })
    )


async def _auth_callback(req: Request):
    error = req.query_params.get("error")
    if error:
        return Response(f"Google OAuth error: {error}", status_code=400)

    state_data = _state_store.pop(req.query_params.get("state", ""), None)
    if not state_data:
        return Response("Invalid or expired state", status_code=400)

    r = await _request_with_retry("POST", "https://oauth2.googleapis.com/token", data={
        "code": req.query_params.get("code"),
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": f"{BASE_URL}/auth/callback",
        "grant_type": "authorization_code",
    })
    tokens = r.json()

    if "error" in tokens:
        log.warning("Google token exchange failed: %s", tokens["error"])
        return Response(f"Token exchange failed: {tokens['error']}", status_code=400)

    ui = await _request_with_retry(
        "GET", "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    if not ui.is_success:
        log.warning("Google userinfo fetch failed (%s): %s", ui.status_code, ui.text[:200])
        return Response("Failed to fetch Google account info", status_code=502)
    userinfo = ui.json()

    jti = secrets.token_urlsafe(16)
    log.info("new session authenticated: %s (read_only=%s) jti=%s has_refresh_token=%s "
             "expires_in=%s",
             userinfo.get("email"), state_data.get("read_only", False), jti,
             tokens.get("refresh_token") is not None, tokens.get("expires_in"))
    _token_store[jti] = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expiry": time.time() + tokens.get("expires_in", 3600),
        "email": userinfo.get("email"),
        "read_only": state_data.get("read_only", False),
        # Provisional; replaced with the real 30-day expiry once /token mints the client JWT.
        # Ensures flows abandoned between here and /token still get purged.
        "jwt_exp": time.time() + STATE_TTL,
    }

    our_code = secrets.token_urlsafe(16)
    _code_store[our_code] = {
        "jti": jti,
        "email": userinfo.get("email"),
        "code_challenge": state_data["code_challenge"],
        "client_redirect_uri": state_data["client_redirect_uri"],
        "client_state": state_data["client_state"],
        "read_only": state_data.get("read_only", False),
        "created": time.time(),
    }

    params: dict = {"code": our_code}
    if state_data["client_state"]:
        params["state"] = state_data["client_state"]
    return RedirectResponse(f"{state_data['client_redirect_uri']}?{urlencode(params)}")


async def _token(req: Request) -> JSONResponse:
    form = await req.form()
    # Starlette form values are `UploadFile | str`. A client posting multipart (or a
    # scanner doing so) previously reached _pkce_ok with an UploadFile and crashed it on
    # .encode(); treat any non-string value as absent so those requests get a clean 400.
    data = {k: v for k, v in form.multi_items() if isinstance(v, str)}
    code_data = _code_store.pop(data.get("code", ""), None)
    if not code_data:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    verifier = data.get("code_verifier")
    if not verifier or not _pkce_ok(verifier, code_data["code_challenge"]):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    now = int(time.time())
    exp = now + 86400 * 30
    token = jwt.encode({
        "jti": code_data["jti"],
        "email": code_data["email"],
        "read_only": code_data.get("read_only", False),
        "iat": now,
        "exp": exp,
    }, JWT_SECRET, algorithm="HS256")

    if code_data["jti"] in _token_store:
        _token_store[code_data["jti"]]["jwt_exp"] = exp

    return JSONResponse({"access_token": token, "token_type": "Bearer",
                         "expires_in": 86400 * 30})


# ── Bearer auth middleware (raw ASGI — preserves ContextVar across await) ──────

def _www_auth_header(alias: str) -> bytes:
    metadata_path = (f"/{alias}/.well-known/oauth-protected-resource" if alias
                     else "/.well-known/oauth-protected-resource")
    return (
        f'Bearer realm="Gmail MCP", '
        f'resource_metadata="{BASE_URL}{metadata_path}"'
    ).encode("utf-8", errors="replace")

_OAUTH_PATHS = frozenset([
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/authorize",
    "/auth/callback",
    "/token",
])

_KNOWN_PATHS = _OAUTH_PATHS | {"/mcp"}

_SECURITY_HEADERS = [
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
]


def _with_security_headers(send):
    """Wraps an ASGI send() so every response — including ones from the mounted
    OAuth/FastMCP sub-apps — gets standard security headers. /authorize is the one
    point a real browser touches (the user's, round-tripping through Google's
    consent screen), so this is worth doing even though most traffic is API calls."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", [])) + _SECURITY_HEADERS
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


_MULTI_SLASH = re.compile(r"/+")


def _normalize_path(path: str) -> str:
    """Collapse repeated slashes (e.g. "//mcp" -> "/mcp") before _split_alias sees
    the path. Without this, a non-canonical path matches neither a known OAuth path
    nor the /mcp bearer-auth gate's exact-prefix check, so the request would fall
    through with NO auth check performed at all — relying entirely on whatever the
    downstream FastMCP/Starlette router does with the same non-canonical path
    (today it independently 404s rather than treating it as equivalent to /mcp, but
    that's downstream behavior this file has no control over, not a guarantee)."""
    return _MULTI_SLASH.sub("/", path)


def _split_alias(path: str) -> tuple[str, str]:
    """Strip a leading /<alias> segment so /personal/mcp, /work/.well-known/... etc.
    resolve the same as their unaliased routes — lets two Claude connectors share one
    server. Returns (alias, normalised_path); alias is "" when there wasn't one."""
    if path in _KNOWN_PATHS or path.startswith("/mcp/"):
        return "", path
    segments = path.lstrip("/").split("/", 1)
    if len(segments) == 2:
        candidate = "/" + segments[1]
        if candidate in _KNOWN_PATHS or candidate.startswith("/mcp/"):
            return segments[0], candidate
    return "", path


class _App:
    """Dispatches OAuth paths to Starlette, everything else to FastMCP."""

    def __init__(self) -> None:
        self._oauth = Starlette(routes=[
            Route("/.well-known/oauth-authorization-server", _oauth_server_metadata),
            Route("/.well-known/openid-configuration", _openid_configuration),
            Route("/.well-known/oauth-protected-resource", _protected_resource),
            Route("/authorize", _authorize),
            Route("/auth/callback", _auth_callback),
            Route("/token", _token, methods=["POST"]),
        ])
        self._mcp = mcp.http_app()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            global _http_client
            _http_client = httpx.AsyncClient(timeout=HTTPX_TIMEOUT)
            try:
                await self._mcp(scope, receive, send)
            finally:
                await _http_client.aclose()
                _http_client = None
            return

        if scope["type"] == "http":
            send = _with_security_headers(send)

            try:
                _purge_expired_states()
                _purge_expired_tokens()
            except Exception:
                log.exception("periodic cleanup failed")

            alias, path = _split_alias(_normalize_path(scope["path"]))
            if path != scope["path"]:
                scope = {**scope, "path": path, "raw_path": path.encode()}
            # Starlette route handlers (e.g. _protected_resource) read this via
            # req.state.alias to echo the alias back into OAuth discovery responses.
            scope["state"] = {**(scope.get("state") or {}), "alias": alias}

            # Auth check for MCP endpoint only
            if path == "/mcp" or path.startswith("/mcp/"):
                headers = dict(scope.get("headers", []))
                auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
                if not auth.startswith("Bearer "):
                    await self._send_401(send, alias)
                    return
                try:
                    payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
                    await _google_access_token(payload["jti"])  # fail fast on a dead/revoked session
                except jwt.PyJWTError as e:
                    log.info("rejected MCP request: invalid/expired JWT (%s)", e)
                    await self._send_401(send, alias)
                    return
                except ReauthRequired as e:
                    log.warning("MCP request needs re-auth: %s", e)
                    await self._send_401(send, alias)
                    return
                except Exception:
                    log.exception("unexpected error validating MCP request")
                    await self._send_401(send, alias)
                    return
                _session_jti.set(payload["jti"])
                _read_only.set(_effective_read_only(payload, alias))

            if path in _OAUTH_PATHS:
                await self._oauth(scope, receive, send)
                return

        await self._mcp(scope, receive, send)

    @staticmethod
    async def _send_401(send, alias: str = "") -> None:
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain"),
                                (b"www-authenticate", _www_auth_header(alias))]})
        await send({"type": "http.response.body", "body": b"Unauthorized"})


app = _App()

if __name__ == "__main__":
    import uvicorn
    # PaaS platforms (Railway, Render, etc.) inject PORT and route to whatever it's set to.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="info")
