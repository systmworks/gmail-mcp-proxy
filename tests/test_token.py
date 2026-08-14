import time

import httpx
import pytest

import server


@pytest.fixture
def code():
    c = "test-code"
    server._code_store[c] = {
        "jti": "test-jti", "email": "x@example.com", "code_challenge": "test-challenge",
        "client_redirect_uri": "http://localhost/cb", "client_state": None,
        "read_only": False, "created": time.time(),
    }
    yield c
    server._code_store.pop(c, None)


async def test_token_rejects_multipart_form_values_cleanly(code):
    # Regression test: a client posting /token as multipart/form-data with
    # code_verifier as a file part (instead of a plain field) used to crash
    # _pkce_ok's verifier.encode() with an unhandled AttributeError (UploadFile
    # has no .encode()), returning a 500 instead of a clean 400.
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/token", data={"code": code}, files={"code_verifier": ("f.txt", b"data")})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_grant"}


async def test_token_rejects_missing_verifier(code):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/token", data={"code": code})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_grant"}


async def test_token_rejects_unknown_code():
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/token", data={"code": "does-not-exist", "code_verifier": "x"})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_grant"}
