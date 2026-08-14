import base64

import httpx
import pytest
import respx

import server


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.fixture(autouse=True)
async def http_client():
    server._http_client = httpx.AsyncClient(timeout=5)
    server._google_token.set("fake-token")
    server._read_only.set(False)
    yield
    await server._http_client.aclose()
    server._http_client = None


@respx.mock
async def test_search_emails_degrades_gracefully_on_network_error():
    # Regression test for the bug found in code review: a network-level exception
    # enriching one message used to fail the whole search instead of falling back
    # to bare id/threadId for that one message.
    respx.get(f"{server.GMAIL}/messages").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"id": "1", "threadId": "t1"}, {"id": "2", "threadId": "t2"}],
        })
    )
    respx.get(f"{server.GMAIL}/messages/1").mock(
        return_value=httpx.Response(200, json={
            "snippet": "hi",
            "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": "From", "value": "a@example.com"}]},
        })
    )
    respx.get(f"{server.GMAIL}/messages/2").mock(side_effect=httpx.ConnectTimeout("boom"))

    results = await server.search_emails("test query")

    assert len(results) == 2
    enriched = next(r for r in results if r["id"] == "1")
    assert enriched["from"] == "a@example.com"
    degraded = next(r for r in results if r["id"] == "2")
    assert "from" not in degraded


@respx.mock
async def test_search_emails_degrades_on_http_error_status():
    respx.get(f"{server.GMAIL}/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "1", "threadId": "t1"}]})
    )
    respx.get(f"{server.GMAIL}/messages/1").mock(return_value=httpx.Response(500))

    results = await server.search_emails("test query")

    assert results == [{"id": "1", "threadId": "t1"}]


@respx.mock
async def test_search_emails_retries_and_recovers_from_transient_5xx():
    respx.get(f"{server.GMAIL}/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "1", "threadId": "t1"}]})
    )
    route = respx.get(f"{server.GMAIL}/messages/1").mock(side_effect=[
        httpx.Response(500),
        httpx.Response(200, json={
            "snippet": "hi",
            "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": "From", "value": "a@example.com"}]},
        }),
    ])

    results = await server.search_emails("test query")

    assert route.call_count == 2
    assert results[0]["from"] == "a@example.com"


@respx.mock
async def test_search_emails_gives_up_after_one_retry_on_persistent_failure():
    respx.get(f"{server.GMAIL}/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "1", "threadId": "t1"}]})
    )
    route = respx.get(f"{server.GMAIL}/messages/1").mock(return_value=httpx.Response(503))

    results = await server.search_emails("test query")

    assert route.call_count == 2
    assert results == [{"id": "1", "threadId": "t1"}]


@respx.mock
async def test_search_emails_does_not_retry_permanent_4xx():
    respx.get(f"{server.GMAIL}/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "1", "threadId": "t1"}]})
    )
    route = respx.get(f"{server.GMAIL}/messages/1").mock(return_value=httpx.Response(404))

    results = await server.search_emails("test query")

    assert route.call_count == 1
    assert results == [{"id": "1", "threadId": "t1"}]


@respx.mock
async def test_read_message_prefers_plain_over_html():
    payload = {
        "id": "123", "threadId": "t123", "snippet": "hi", "labelIds": ["INBOX"],
        "payload": {
            "headers": [{"name": "From", "value": "a@example.com"}],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("plain body")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>html body</p>")}},
            ],
        },
    }
    respx.get(f"{server.GMAIL}/messages/123").mock(return_value=httpx.Response(200, json=payload))

    result = await server.read_message("123")

    assert result["body"] == "plain body"


@respx.mock
async def test_read_message_falls_back_to_html_when_no_plain_part():
    payload = {
        "id": "123", "threadId": "t123", "snippet": "hi", "labelIds": ["INBOX"],
        "payload": {
            "headers": [],
            "mimeType": "text/html",
            "body": {"data": _b64("<p>only html</p>")},
        },
    }
    respx.get(f"{server.GMAIL}/messages/123").mock(return_value=httpx.Response(200, json=payload))

    result = await server.read_message("123")

    assert result["body"] == "<p>only html</p>"


@respx.mock
async def test_delete_draft_handles_204_no_content():
    respx.delete(f"{server.GMAIL}/drafts/abc").mock(return_value=httpx.Response(204))

    result = await server.delete_draft("abc")

    assert result == {"deleted": "abc"}


@respx.mock
async def test_delete_label_handles_204_no_content():
    respx.delete(f"{server.GMAIL}/labels/Label_1").mock(return_value=httpx.Response(204))

    result = await server.delete_label("Label_1")

    assert result == {"deleted": "Label_1"}


async def test_write_tools_reject_read_only_sessions():
    server._read_only.set(True)
    with pytest.raises(PermissionError):
        await server.delete_draft("abc")
