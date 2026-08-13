import base64
import hashlib

import pytest

import server


def test_pkce_ok_matches_valid_verifier():
    verifier = "test-verifier-1234567890abcdefghijklmno"
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert server._pkce_ok(verifier, challenge) is True


def test_pkce_ok_rejects_wrong_verifier():
    assert server._pkce_ok("wrong-verifier", "some-unrelated-challenge") is False


def test_alias_from_resource_extracts_alias():
    assert server._alias_from_resource("https://host/work/mcp") == "work"


def test_alias_from_resource_returns_empty_for_unaliased():
    assert server._alias_from_resource("https://host/mcp") == ""


def test_alias_from_resource_returns_empty_for_none():
    assert server._alias_from_resource(None) == ""


def test_alias_from_resource_returns_empty_for_unparseable():
    assert server._alias_from_resource("https://host/a/b/c") == ""


def test_split_alias_strips_known_alias():
    assert server._split_alias("/work/mcp") == ("work", "/mcp")


def test_split_alias_leaves_unaliased_mcp_path():
    assert server._split_alias("/mcp") == ("", "/mcp")


def test_split_alias_strips_alias_from_oauth_path():
    assert server._split_alias("/personal/.well-known/oauth-protected-resource") == (
        "personal", "/.well-known/oauth-protected-resource",
    )


def test_split_alias_leaves_unrecognised_path_untouched():
    assert server._split_alias("/something/else") == ("", "/something/else")


def test_build_email_encodes_basic_fields():
    raw = server._build_email("a@example.com", "Hi", "body text")
    decoded = base64.urlsafe_b64decode(raw + "==").decode()
    assert "a@example.com" in decoded
    assert "Hi" in decoded
    assert "body text" in decoded


def test_build_email_includes_threading_headers():
    raw = server._build_email(
        "a@example.com", "Hi", "body",
        in_reply_to="<msg1@mail>", references="<msg0@mail> <msg1@mail>",
    )
    decoded = base64.urlsafe_b64decode(raw + "==").decode()
    assert "In-Reply-To: <msg1@mail>" in decoded
    assert "References: <msg0@mail> <msg1@mail>" in decoded


def test_build_email_omits_optional_headers_when_absent():
    raw = server._build_email("a@example.com", "Hi", "body")
    decoded = base64.urlsafe_b64decode(raw + "==").decode()
    assert "In-Reply-To" not in decoded
    assert "References" not in decoded
    assert "Cc" not in decoded


def test_client_raises_clear_error_before_lifespan_starts():
    original = server._http_client
    server._http_client = None
    try:
        with pytest.raises(RuntimeError, match="lifespan"):
            server._client()
    finally:
        server._http_client = original


def test_effective_read_only_true_when_jwt_says_so():
    assert server._effective_read_only({"read_only": True}, "") is True


def test_effective_read_only_true_when_alias_restricted_even_if_jwt_says_false():
    # Regression test: a READ_ONLY_ALIASES-restricted connector must stay
    # restricted even if the JWT was minted with read_only=False (e.g. the
    # OAuth client never echoed the 'resource' param during /authorize).
    original = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    try:
        assert server._effective_read_only({"read_only": False}, "work") is True
    finally:
        server.READ_ONLY_ALIASES = original


def test_effective_read_only_false_for_unrestricted_alias():
    original = server.READ_ONLY_ALIASES
    server.READ_ONLY_ALIASES = frozenset({"work"})
    try:
        assert server._effective_read_only({"read_only": False}, "personal") is False
    finally:
        server.READ_ONLY_ALIASES = original
