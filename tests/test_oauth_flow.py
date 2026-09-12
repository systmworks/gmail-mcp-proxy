import asyncio
import time
import urllib.parse

import httpx
import jwt
import pytest
import respx

import server


@pytest.fixture(autouse=True)
async def http_client():
    server._http_client = httpx.AsyncClient(timeout=5)
    yield
    await server._http_client.aclose()
    server._http_client = None


@pytest.fixture
async def asgi_client():
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _state_query(location: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)


def _make_jwt(jti: str, email: str = "a@example.com", read_only: bool = False) -> str:
    now = int(time.time())
    return jwt.encode({
        "jti": jti, "email": email, "read_only": read_only,
        "iat": now, "exp": now + 3600,
    }, server.JWT_SECRET, algorithm="HS256")


# ── _authorize ───────────────────────────────────────────────────────────────

async def test_authorize_rejects_unknown_redirect_uri(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://evil.example.com/cb",
        "code_challenge": "test-challenge",
    })
    assert r.status_code == 400
    assert r.text == "Unknown redirect_uri"


async def test_authorize_rejects_missing_code_challenge(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    })
    assert r.status_code == 400
    assert r.text == "PKCE code_challenge is required"


async def test_authorize_rejects_plain_code_challenge_method(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": "test-challenge",
        "code_challenge_method": "plain",
    })
    assert r.status_code == 400
    assert "S256" in r.text


async def test_authorize_accepts_explicit_s256_method(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": "test-challenge",
        "code_challenge_method": "S256",
    })
    assert 300 <= r.status_code < 400


async def test_authorize_happy_path_redirects_to_google_and_stores_state(asgi_client):
    r = await asgi_client.get("/authorize", params={
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": "test-challenge",
        "state": "client-state-xyz",
    })
    assert 300 <= r.status_code < 400
    location = r.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    our_state = _state_query(location)["state"][0]
    stored = server._state_store.pop(our_state)
    assert stored["client_redirect_uri"] == "https://claude.ai/api/mcp/auth_callback"
    assert stored["client_state"] == "client-state-xyz"
    assert stored["code_challenge"] == "test-challenge"
    assert stored["read_only"] is False


async def test_authorize_sets_read_only_for_restricted_alias_reached_via_url_path(asgi_client):
    # alias/read_only comes from the server-verified URL path (/work/authorize,
    # split by _split_alias in _App.__call__), not the client-echoed 'resource'
    # query param — see the security regression test below for why that matters.
    original = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    try:
        r = await asgi_client.get("/work/authorize", params={
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": "test-challenge",
            "resource": "http://test/work/mcp",
        })
        assert 300 <= r.status_code < 400
        our_state = _state_query(r.headers["location"])["state"][0]
        stored = server._state_store.pop(our_state)
        assert stored["read_only"] is True
    finally:
        server.READ_ONLY_ALIASES = original


async def test_authorize_stays_read_only_even_if_client_never_echoes_resource(asgi_client):
    # SECURITY regression test for the cross-alias token replay bug: _authorize
    # used to decide read_only from the client-supplied 'resource' query parameter
    # (via the now-removed _alias_from_resource) instead of the server-verified
    # alias the request actually came in through. A request hitting /work/authorize
    # — a READ_ONLY_ALIASES-restricted connector — with NO 'resource' param at all
    # (an OAuth client that fails to echo it, or simply doesn't send one) used to
    # silently get read_only=False stored, meaning the resulting Google grant got
    # full write scope and the minted JWT could be replayed anywhere to bypass the
    # restriction. It must stay read_only=True regardless of what (if anything)
    # the client sends as 'resource'.
    original = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    try:
        r = await asgi_client.get("/work/authorize", params={
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": "test-challenge",
            # Deliberately omitted: "resource" — or could point anywhere else.
        })
        assert 300 <= r.status_code < 400
        our_state = _state_query(r.headers["location"])["state"][0]
        stored = server._state_store.pop(our_state)
        assert stored["read_only"] is True
    finally:
        server.READ_ONLY_ALIASES = original


# ── _auth_callback ──────────────────────────────────────────────────────────

@pytest.fixture
def state():
    s = "test-state"
    server._state_store[s] = {
        "client_state": "client-xyz",
        "client_redirect_uri": "http://localhost/cb",
        "code_challenge": "test-challenge",
        "read_only": False,
        "created": time.time(),
    }
    yield s
    server._state_store.pop(s, None)


async def test_auth_callback_returns_400_on_google_error(asgi_client):
    r = await asgi_client.get("/auth/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert r.text == "Google OAuth error: access_denied"


async def test_auth_callback_rejects_invalid_or_expired_state(asgi_client):
    r = await asgi_client.get("/auth/callback", params={"state": "does-not-exist", "code": "abc"})
    assert r.status_code == 400
    assert r.text == "Invalid or expired state"


@respx.mock
async def test_auth_callback_returns_400_on_token_exchange_failure(asgi_client, state):
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "google-code"})
    assert r.status_code == 400
    assert r.text == "Token exchange failed: invalid_grant"


@respx.mock
async def test_auth_callback_returns_502_on_userinfo_failure(asgi_client, state):
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "gtok", "expires_in": 3600})
    )
    respx.get("https://www.googleapis.com/oauth2/v3/userinfo").mock(
        return_value=httpx.Response(500, text="error")
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "google-code"})
    assert r.status_code == 502


@respx.mock
async def test_auth_callback_token_exchange_retries_transient_5xx(asgi_client, state):
    # Regression coverage: the token-exchange call used a raw c.post(), bypassing
    # _request_with_retry entirely — a transient 5xx during this one-time
    # authorization-code exchange used to fail the whole login instead of
    # transparently retrying like every other outbound call in the file.
    route = respx.post("https://oauth2.googleapis.com/token").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(200, json={
            "access_token": "gtok", "refresh_token": "rtok", "expires_in": 3600,
        }),
    ])
    respx.get("https://www.googleapis.com/oauth2/v3/userinfo").mock(
        return_value=httpx.Response(200, json={"email": "a@example.com"})
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "google-code"})
    assert route.call_count == 2
    assert 300 <= r.status_code < 400


@respx.mock
async def test_auth_callback_userinfo_retries_transient_5xx(asgi_client, state):
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "gtok", "refresh_token": "rtok", "expires_in": 3600,
        })
    )
    route = respx.get("https://www.googleapis.com/oauth2/v3/userinfo").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(200, json={"email": "a@example.com"}),
    ])
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "google-code"})
    assert route.call_count == 2
    assert 300 <= r.status_code < 400


@respx.mock
async def test_auth_callback_happy_path_creates_session_and_redirects(asgi_client, state):
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "gtok", "refresh_token": "rtok", "expires_in": 3600,
        })
    )
    respx.get("https://www.googleapis.com/oauth2/v3/userinfo").mock(
        return_value=httpx.Response(200, json={"email": "a@example.com"})
    )
    r = await asgi_client.get("/auth/callback", params={"state": state, "code": "google-code"})
    assert 300 <= r.status_code < 400
    location = r.headers["location"]
    assert location.startswith("http://localhost/cb?")
    parsed = _state_query(location)
    assert parsed["state"][0] == "client-xyz"
    code_data = server._code_store.pop(parsed["code"][0])
    assert code_data["email"] == "a@example.com"
    token_data = server._token_store.pop(code_data["jti"])
    assert token_data["refresh_token"] == "rtok"
    assert token_data["email"] == "a@example.com"


# ── _refresh ────────────────────────────────────────────────────────────────

async def test_refresh_raises_reauth_required_for_unknown_jti():
    jti = "does-not-exist-jti"
    try:
        with pytest.raises(server.ReauthRequired):
            await server._refresh(jti)
    finally:
        server._refresh_locks.pop(jti, None)


@respx.mock
async def test_refresh_pops_session_and_raises_on_failed_refresh():
    jti = "jti-fail"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() - 100, "email": "a@example.com", "jwt_exp": time.time() + 1000,
    }
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(server.ReauthRequired):
        await server._refresh(jti)
    assert jti not in server._token_store
    assert jti not in server._refresh_locks


@respx.mock
async def test_refresh_retries_transient_5xx_and_recovers():
    # Regression coverage: _refresh used a raw c.post(), bypassing
    # _request_with_retry — a single transient 5xx during a routine access-token
    # refresh used to force a full re-auth flow, even though the identical error
    # on a write-tool call would have been retried.
    jti = "jti-retry"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() - 100, "email": "a@example.com", "jwt_exp": time.time() + 1000,
    }
    route = respx.post("https://oauth2.googleapis.com/token").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600}),
    ])
    try:
        result = await server._refresh(jti)
        assert result == "new-token"
        assert route.call_count == 2
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


@respx.mock
async def test_refresh_updates_access_token_on_success():
    jti = "jti-ok"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() - 100, "email": "a@example.com", "jwt_exp": time.time() + 1000,
    }
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})
    )
    try:
        result = await server._refresh(jti)
        assert result == "new-token"
        assert server._token_store[jti]["access_token"] == "new-token"
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


@respx.mock
async def test_refresh_concurrent_calls_for_same_session_issue_one_http_request():
    # Regression coverage for the per-jti asyncio.Lock in _refresh: two requests
    # racing an expired token should only ever fire one refresh_token grant.
    jti = "jti-concurrent"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() - 100, "email": "a@example.com", "jwt_exp": time.time() + 1000,
    }
    route = respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})
    )
    try:
        results = await asyncio.gather(server._refresh(jti), server._refresh(jti))
        assert results == ["new-token", "new-token"]
        assert route.call_count == 1
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


# ── Bearer-auth middleware (/mcp) ───────────────────────────────────────────

async def test_mcp_endpoint_rejects_missing_authorization_header(asgi_client):
    r = await asgi_client.get("/mcp")
    assert r.status_code == 401
    assert "www-authenticate" in r.headers


async def test_mcp_endpoint_rejects_non_bearer_scheme(asgi_client):
    r = await asgi_client.get("/mcp", headers={"Authorization": "Basic abc123"})
    assert r.status_code == 401


async def test_mcp_endpoint_rejects_malformed_non_utf8_header_cleanly():
    # Regression test: non-UTF-8 bytes in the Authorization header used to raise
    # an uncaught UnicodeDecodeError instead of the clean 401 every other
    # malformed-input case in this function gets. Drives the raw ASGI scope
    # directly — httpx's own header encoding won't reproduce invalid UTF-8 bytes.
    scope = {"type": "http", "path": "/mcp", "headers": [(b"authorization", b"Bearer \xff\xfe")]}
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await server.app(scope, receive, send)

    assert sent[0]["status"] == 401


async def test_mcp_endpoint_rejects_invalid_jwt(asgi_client):
    r = await asgi_client.get("/mcp", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert r.status_code == 401


async def test_mcp_endpoint_rejects_valid_jwt_with_unknown_session(asgi_client):
    token = _make_jwt("unknown-jti")
    r = await asgi_client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def _stub_mcp(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_mcp_endpoint_accepts_valid_jwt_with_known_session(asgi_client):
    # FastMCP's real mounted app needs its session manager started via the ASGI
    # lifespan protocol, which httpx.ASGITransport doesn't drive — swap in a
    # trivial stub so this only exercises the bearer-auth branch in _App.__call__,
    # not FastMCP's internals.
    jti = "known-jti"
    server._token_store[jti] = {
        "access_token": "gtok", "refresh_token": "rtok",
        "expiry": time.time() + 3600, "email": "a@example.com",
        "jwt_exp": time.time() + 86400,
    }
    token = _make_jwt(jti)
    original_mcp = server.app._mcp
    server.app._mcp = _stub_mcp
    try:
        r = await asgi_client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert "www-authenticate" not in r.headers
    finally:
        server.app._mcp = original_mcp
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


async def test_mcp_endpoint_alias_routing_authenticates_through_split_alias(asgi_client):
    # End-to-end wiring check for _split_alias + bearer-auth working together on
    # an aliased path. Fine-grained read-only enforcement itself is covered by
    # _effective_read_only's unit tests (test_helpers.py) and
    # test_write_tools_reject_read_only_sessions (test_server.py).
    original_aliases = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    jti = "alias-jti"
    server._token_store[jti] = {
        "access_token": "gtok", "refresh_token": "rtok",
        "expiry": time.time() + 3600, "email": "a@example.com",
        "jwt_exp": time.time() + 86400,
    }
    token = _make_jwt(jti, read_only=False)
    original_mcp = server.app._mcp
    server.app._mcp = _stub_mcp
    try:
        r = await asgi_client.get("/work/mcp", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
    finally:
        server.app._mcp = original_mcp
        server.READ_ONLY_ALIASES = original_aliases
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


# ── _purge_expired_tokens ───────────────────────────────────────────────────

@respx.mock
async def test_purge_does_not_evict_session_with_in_flight_refresh():
    # Regression test for the token-purge race: _purge_expired_tokens runs on
    # every incoming HTTP request (not just ones for the session being purged).
    # If a session's jwt_exp elapses while _refresh() is mid-flight for that same
    # session — awaiting Google's response — an unconditional pop used to remove
    # the entry out from under it: _refresh then writes the new token into a dict
    # no longer referenced by _token_store, silently losing the session despite
    # _refresh reporting success.
    jti = "jti-purge-race"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() - 10,     # Google access token needs refresh now
        "email": "a@example.com",
        "jwt_exp": time.time() + 0.3,   # about to elapse while refresh is in-flight
    }
    refresh_started = asyncio.Event()

    async def slow_google_response(request):
        refresh_started.set()
        await asyncio.sleep(0.6)
        return httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})

    respx.post("https://oauth2.googleapis.com/token").mock(side_effect=slow_google_response)

    try:
        refresh_task = asyncio.create_task(server._refresh(jti))
        await refresh_started.wait()

        await asyncio.sleep(0.4)  # now past jwt_exp; refresh still sleeping (0.6s)
        assert time.time() >= server._token_store[jti]["jwt_exp"]
        server._purge_expired_tokens()

        # The fix: purge must skip a session whose refresh lock is currently held.
        assert jti in server._token_store

        result = await refresh_task
        assert result == "new-token"
        assert server._token_store[jti]["access_token"] == "new-token"
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)


async def test_purge_evicts_expired_session_with_no_in_flight_refresh():
    jti = "jti-purge-normal"
    server._token_store[jti] = {
        "access_token": "old", "refresh_token": "rtok",
        "expiry": time.time() + 3600, "email": "a@example.com",
        "jwt_exp": time.time() - 1,  # already expired, no refresh in progress
    }
    try:
        server._purge_expired_tokens()
        assert jti not in server._token_store
    finally:
        server._token_store.pop(jti, None)
        server._refresh_locks.pop(jti, None)
