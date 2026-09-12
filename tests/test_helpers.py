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


def test_normalize_path_collapses_repeated_slashes():
    # Regression test: a non-canonical path like "//mcp" matched neither a known
    # OAuth path nor the /mcp bearer-auth gate's exact-prefix check, so the request
    # fell through with no auth check performed at all.
    assert server._normalize_path("//mcp") == "/mcp"
    assert server._normalize_path("/work//mcp") == "/work/mcp"


def test_normalize_path_leaves_normal_paths_untouched():
    assert server._normalize_path("/mcp") == "/mcp"
    assert server._normalize_path("/work/mcp") == "/work/mcp"


def test_normalize_then_split_alias_treats_double_slash_mcp_as_unaliased_mcp():
    alias, path = server._split_alias(server._normalize_path("//mcp"))
    assert (alias, path) == ("", "/mcp")


def test_enc_percent_encodes_path_unsafe_characters():
    assert server._enc("a/b") == "a%2Fb"
    assert server._enc("plain-id_123") == "plain-id_123"


def test_parse_read_only_aliases_basic():
    assert server._parse_read_only_aliases("work,personal") == frozenset({"work", "personal"})


def test_parse_read_only_aliases_strips_whitespace_and_slashes():
    assert server._parse_read_only_aliases(" /work/ , personal ") == frozenset({"work", "personal"})


def test_parse_read_only_aliases_ignores_empty_and_slash_only_tokens():
    # Regression test: the old implementation's filter predicate (a.strip()) and
    # yielded value (a.strip().strip("/")) diverged — a slash-only token like "/"
    # passed the filter (non-empty after whitespace-strip) but collapsed to "" once
    # slashes were also stripped, silently inserting "" (the unaliased connector's
    # own alias) into the set.
    assert server._parse_read_only_aliases("work,/,,  ") == frozenset({"work"})
    assert "" not in server._parse_read_only_aliases("work,/")


def test_parse_read_only_aliases_empty_string():
    assert server._parse_read_only_aliases("") == frozenset()


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
