import base64
import time

import httpx
import pytest
import respx

import server


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.fixture(autouse=True)
async def http_client():
    server._http_client = httpx.AsyncClient(timeout=5)
    server._session_jti.set("fake-jti")
    server._token_store["fake-jti"] = {
        "access_token": "fake-token",
        "refresh_token": "fake-refresh",
        "expiry": time.time() + 3600,
        "email": "test@example.com",
        "read_only": False,
        "jwt_exp": time.time() + 86400,
    }
    server._read_only.set(False)
    yield
    await server._http_client.aclose()
    server._http_client = None
    server._token_store.pop("fake-jti", None)


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
async def test_search_emails_respects_configured_attempt_count():
    # Regression coverage for SEARCH_ENRICH_ATTEMPTS: a deployment that sees more
    # transient failures than the default (2 total attempts) recovers can raise
    # this to retry further before degrading.
    original = server.SEARCH_ENRICH_ATTEMPTS
    server.SEARCH_ENRICH_ATTEMPTS = 3
    try:
        respx.get(f"{server.GMAIL}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "1", "threadId": "t1"}]})
        )
        route = respx.get(f"{server.GMAIL}/messages/1").mock(side_effect=[
            httpx.Response(500),
            httpx.Response(503),
            httpx.Response(200, json={
                "snippet": "hi",
                "labelIds": ["INBOX"],
                "payload": {"headers": [{"name": "From", "value": "a@example.com"}]},
            }),
        ])

        results = await server.search_emails("test query")

        assert route.call_count == 3
        assert results[0]["from"] == "a@example.com"
    finally:
        server.SEARCH_ENRICH_ATTEMPTS = original


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
async def test_read_message_includes_attachment_metadata():
    payload = {
        "id": "123", "threadId": "t123", "snippet": "hi", "labelIds": ["INBOX"],
        "payload": {
            "headers": [],
            "mimeType": "multipart/mixed",
            "parts": [
                {"partId": "0", "mimeType": "text/plain", "body": {"data": _b64("body text")}},
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att1", "size": 4096},
                },
            ],
        },
    }
    respx.get(f"{server.GMAIL}/messages/123").mock(return_value=httpx.Response(200, json=payload))

    result = await server.read_message("123")

    # Both extracted from the same single tree walk (_find_bodies_and_attachments) —
    # confirms merging what used to be two separate passes didn't drop either.
    assert result["body"] == "body text"
    assert result["attachments"] == [
        {"partId": "1", "filename": "invoice.pdf", "mimeType": "application/pdf", "size": 4096},
    ]


@respx.mock
async def test_read_message_attachments_empty_when_none():
    payload = {
        "id": "123", "threadId": "t123", "snippet": "hi", "labelIds": ["INBOX"],
        "payload": {
            "headers": [],
            "mimeType": "text/plain",
            "body": {"data": _b64("just text, no attachments")},
        },
    }
    respx.get(f"{server.GMAIL}/messages/123").mock(return_value=httpx.Response(200, json=payload))

    result = await server.read_message("123")

    assert result["attachments"] == []


@respx.mock
async def test_get_attachment_returns_metadata_and_reencoded_base64():
    # Raw bytes chosen so the standard-base64 encoding differs from the urlsafe one
    # (contains "+"/"/"), proving get_attachment actually re-encodes rather than
    # passing Gmail's base64url straight through.
    raw = bytes([0xFB, 0xEF, 0xBE, 0xFF, 0x3E, 0x3F])
    assert "+" in base64.b64encode(raw).decode() or "/" in base64.b64encode(raw).decode()
    urlsafe_data = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    message_payload = {
        "payload": {
            "headers": [],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att1", "size": len(raw)},
                },
            ],
        },
    }
    respx.get(f"{server.GMAIL}/messages/123").mock(
        return_value=httpx.Response(200, json=message_payload)
    )
    respx.get(f"{server.GMAIL}/messages/123/attachments/att1").mock(
        return_value=httpx.Response(200, json={"size": len(raw), "data": urlsafe_data})
    )

    result = await server.get_attachment("123", "1")

    assert result["filename"] == "invoice.pdf"
    assert result["mimeType"] == "application/pdf"
    assert result["size"] == len(raw)
    assert base64.b64decode(result["data"]) == raw


@respx.mock
async def test_get_attachment_resolves_fresh_attachment_id_each_call():
    # Regression test: Gmail's attachmentId has been observed in production to
    # differ across separate messages.get calls for the very same message/part —
    # only partId is documented immutable. get_attachment must resolve
    # attachmentId fresh from its own fetch rather than trusting one a caller
    # cached from an earlier read_message call.
    raw = b"pdf bytes"

    def _payload_with_id(attachment_id: str) -> dict:
        return {
            "id": "123", "threadId": "t123", "snippet": "hi", "labelIds": [],
            "payload": {
                "headers": [],
                "mimeType": "multipart/mixed",
                "parts": [{
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "f.pdf",
                    "body": {"attachmentId": attachment_id, "size": len(raw)},
                }],
            },
        }

    route = respx.get(f"{server.GMAIL}/messages/123").mock(side_effect=[
        httpx.Response(200, json=_payload_with_id("stale-id")),
        httpx.Response(200, json=_payload_with_id("fresh-id")),
    ])
    # Simulate a prior read_message call that saw "stale-id" for this part.
    await server.read_message("123")

    attachment_route = respx.get(f"{server.GMAIL}/messages/123/attachments/fresh-id").mock(
        return_value=httpx.Response(200, json={
            "size": len(raw),
            "data": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        })
    )

    result = await server.get_attachment("123", "1")

    assert route.call_count == 2
    assert attachment_route.called
    assert base64.b64decode(result["data"]) == raw


@respx.mock
async def test_get_attachment_rejects_oversized_without_downloading():
    original = server.ATTACHMENT_MAX_BYTES
    server.ATTACHMENT_MAX_BYTES = 100
    try:
        message_payload = {
            "payload": {
                "headers": [],
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "partId": "1",
                        "mimeType": "application/pdf",
                        "filename": "big.pdf",
                        "body": {"attachmentId": "att1", "size": 1000},
                    },
                ],
            },
        }
        respx.get(f"{server.GMAIL}/messages/123").mock(
            return_value=httpx.Response(200, json=message_payload)
        )
        # Deliberately not mocking the attachments/{id} route — an unexpected call
        # to it raises a respx error, proving the bytes were never downloaded.

        with pytest.raises(ValueError, match="exceeds"):
            await server.get_attachment("123", "1")
    finally:
        server.ATTACHMENT_MAX_BYTES = original


@respx.mock
async def test_get_attachment_raises_when_id_not_found():
    message_payload = {
        "payload": {
            "headers": [],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "partId": "2",
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att-other", "size": 10},
                },
            ],
        },
    }
    respx.get(f"{server.GMAIL}/messages/123").mock(
        return_value=httpx.Response(200, json=message_payload)
    )

    with pytest.raises(ValueError, match="no attachment"):
        await server.get_attachment("123", "1")


@respx.mock
async def test_get_attachment_rejects_oversized_after_download_if_metadata_size_wrong():
    original = server.ATTACHMENT_MAX_BYTES
    server.ATTACHMENT_MAX_BYTES = 10
    try:
        raw = b"this is more than ten bytes of data"
        message_payload = {
            "payload": {
                "headers": [],
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "partId": "1",
                        "mimeType": "application/pdf",
                        "filename": "mislabeled.pdf",
                        # Metadata size understates the real size (e.g. stale/wrong).
                        "body": {"attachmentId": "att1", "size": 5},
                    },
                ],
            },
        }
        respx.get(f"{server.GMAIL}/messages/123").mock(
            return_value=httpx.Response(200, json=message_payload)
        )
        respx.get(f"{server.GMAIL}/messages/123/attachments/att1").mock(
            return_value=httpx.Response(200, json={
                "size": len(raw),
                "data": base64.urlsafe_b64encode(raw).decode().rstrip("="),
            })
        )

        with pytest.raises(ValueError, match="exceeds"):
            await server.get_attachment("123", "1")
    finally:
        server.ATTACHMENT_MAX_BYTES = original


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


@respx.mock
async def test_modify_labels_retries_and_recovers_from_transient_5xx():
    # Regression coverage for API_RETRY_ATTEMPTS: bulk label operations otherwise
    # fail outright the moment Gmail rate-limits a single call.
    route = respx.post(f"{server.GMAIL}/messages/msg1/modify").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(200, json={"id": "msg1", "labelIds": ["INBOX", "IMPORTANT"]}),
    ])

    result = await server.modify_labels("msg1", add=["IMPORTANT"])

    assert route.call_count == 2
    assert result == {"id": "msg1", "labelIds": ["INBOX", "IMPORTANT"]}


@respx.mock
async def test_modify_labels_raises_after_exhausting_retries_on_persistent_failure():
    route = respx.post(f"{server.GMAIL}/messages/msg1/modify").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await server.modify_labels("msg1", add=["IMPORTANT"])

    assert route.call_count == server.API_RETRY_ATTEMPTS


@respx.mock
async def test_modify_labels_does_not_retry_permanent_4xx():
    route = respx.post(f"{server.GMAIL}/messages/msg1/modify").mock(
        return_value=httpx.Response(404)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await server.modify_labels("msg1", add=["IMPORTANT"])

    assert route.call_count == 1


@respx.mock
async def test_read_message_retries_transient_5xx_and_recovers():
    # Regression coverage: read tools (get_profile, read_message, read_thread,
    # list_drafts, list_labels, list_calendars, list_events, search_events,
    # get_event) used to go straight through _client(), bypassing the retry
    # helper entirely — a transient 429/503 failed the call outright with no
    # recovery, even though GETs are the safest calls to retry (idempotent).
    route = respx.get(f"{server.GMAIL}/messages/123").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(200, json={
            "id": "123", "threadId": "t123", "snippet": "hi", "labelIds": [],
            "payload": {"headers": [], "mimeType": "text/plain", "body": {"data": _b64("hi")}},
        }),
    ])

    result = await server.read_message("123")

    assert route.call_count == 2
    assert result["body"] == "hi"


@respx.mock
async def test_get_profile_retries_transient_5xx_and_recovers():
    route = respx.get(f"{server.GMAIL}/profile").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(200, json={"emailAddress": "a@example.com"}),
    ])

    result = await server.get_profile()

    assert route.call_count == 2
    assert result == {"emailAddress": "a@example.com"}


@respx.mock
async def test_read_message_url_encodes_message_id():
    # Regression test: ids were interpolated raw into REST URL path segments with
    # no encoding. Google's own generated ids are base64url (no "/" by
    # construction) so this is low-risk for message_id specifically, but the fix
    # (_enc) is applied uniformly — confirm it actually takes effect here.
    route = respx.get(f"{server.GMAIL}/messages/a%2Fb").mock(
        return_value=httpx.Response(200, json={
            "id": "a/b", "threadId": "t1", "snippet": "", "labelIds": [],
            "payload": {"headers": [], "mimeType": "text/plain", "body": {"data": _b64("x")}},
        })
    )

    result = await server.read_message("a/b")

    assert route.called
    assert result["id"] == "a/b"


@respx.mock
async def test_get_event_url_encodes_calendar_id():
    # calendar_id is caller-supplied and can be an arbitrary string (e.g. an email
    # address used as a calendar id) rather than a Google-generated opaque id.
    route = respx.get(
        f"{server.GCAL}/calendars/someone%40example.com/events/evt1"
    ).mock(return_value=httpx.Response(200, json={"id": "evt1"}))

    result = await server.get_event("evt1", calendar_id="someone@example.com")

    assert route.called
    assert result == {"id": "evt1"}


@respx.mock
async def test_get_attachment_raises_clear_error_when_data_field_missing():
    # Regression test (lower-priority cleanup): get_attachment used r2.json()["data"]
    # (direct key access), which would raise an unhandled KeyError instead of a
    # clear error if a Gmail attachment response were ever missing 'data'.
    message_payload = {
        "payload": {
            "headers": [],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att1", "size": 10},
                },
            ],
        },
    }
    respx.get(f"{server.GMAIL}/messages/123").mock(
        return_value=httpx.Response(200, json=message_payload)
    )
    respx.get(f"{server.GMAIL}/messages/123/attachments/att1").mock(
        return_value=httpx.Response(200, json={"size": 10})  # no "data" field
    )

    with pytest.raises(ValueError, match="missing"):
        await server.get_attachment("123", "1")


@respx.mock
async def test_modify_labels_does_not_retry_network_error():
    # Regression test: _request_with_retry used to catch httpx.HTTPError (network
    # errors — timeouts, connection resets, not just HTTP status) and retry
    # unconditionally. Whether the original POST already landed server-side before
    # the network error is ambiguous, so blindly retrying a non-idempotent write
    # (modify_labels here, but also send_email/create_draft/etc.) risked silently
    # duplicating it. Network errors now propagate on the first attempt instead.
    route = respx.post(f"{server.GMAIL}/messages/msg1/modify").mock(
        side_effect=httpx.ConnectTimeout("boom")
    )

    with pytest.raises(httpx.ConnectTimeout):
        await server.modify_labels("msg1", add=["IMPORTANT"])

    assert route.call_count == 1


@respx.mock
async def test_read_message_retries_network_error_and_recovers():
    # GET/HEAD are safe to retry even on a network-level error (unlike the write
    # case above) — retrying can't duplicate an effect. _request_with_retry derives
    # this from the HTTP method itself (_IDEMPOTENT_METHODS), so no call site needs
    # to opt in explicitly.
    route = respx.get(f"{server.GMAIL}/messages/123").mock(side_effect=[
        httpx.ConnectTimeout("boom"),
        httpx.Response(200, json={
            "id": "123", "threadId": "t123", "snippet": "hi", "labelIds": [],
            "payload": {"headers": [], "mimeType": "text/plain", "body": {"data": _b64("hi")}},
        }),
    ])

    result = await server.read_message("123")

    assert route.call_count == 2
    assert result["body"] == "hi"


@respx.mock
async def test_trash_message_requests_compact_fields():
    # Ported concern from sibling project outlook-mcp-proxy: its Graph API ignores
    # $select on POST/action endpoints, so move/trash calls always echoed back the
    # full message body. Gmail's `fields` system parameter (unlike Graph's $select)
    # does filter write-endpoint responses too — assert it's actually requested, so
    # trash/modify/draft calls don't pull a full MIME payload into context for
    # nothing.
    route = respx.post(f"{server.GMAIL}/messages/msg1/trash",
                       params={"fields": server._COMPACT_MESSAGE_FIELDS}).mock(
        return_value=httpx.Response(200, json={"id": "msg1", "threadId": "t1", "labelIds": ["TRASH"]})
    )

    result = await server.trash_message("msg1")

    assert route.called
    assert result == {"id": "msg1", "threadId": "t1", "labelIds": ["TRASH"]}


@respx.mock
async def test_modify_labels_requests_compact_fields():
    route = respx.post(f"{server.GMAIL}/messages/msg1/modify",
                       params={"fields": server._COMPACT_MESSAGE_FIELDS}).mock(
        return_value=httpx.Response(200, json={"id": "msg1", "labelIds": ["IMPORTANT"]})
    )

    await server.modify_labels("msg1", add=["IMPORTANT"])

    assert route.called


@respx.mock
async def test_create_draft_requests_compact_fields():
    route = respx.post(f"{server.GMAIL}/drafts",
                       params={"fields": server._COMPACT_DRAFT_FIELDS}).mock(
        return_value=httpx.Response(200, json={"id": "d1", "message": {"id": "m1", "threadId": "t1"}})
    )

    await server.create_draft("to@example.com", "subj", "body")

    assert route.called


@respx.mock
async def test_send_email_requests_compact_fields():
    route = respx.post(f"{server.GMAIL}/messages/send",
                       params={"fields": server._COMPACT_MESSAGE_FIELDS}).mock(
        return_value=httpx.Response(200, json={"id": "m1", "threadId": "t1", "labelIds": ["SENT"]})
    )

    await server.send_email("to@example.com", "subj", "body")

    assert route.called


@respx.mock
async def test_list_calendars_requests_compact_fields():
    # Calendar's CalendarList/Event resources carry a lot a listing tool never uses
    # (conferenceData, extendedProperties, attachments, notificationSettings, ...) —
    # assert the trimmed `fields` param is actually sent, not just defined.
    route = respx.get(f"{server.GCAL}/users/me/calendarList",
                      params={"fields": server._CALENDAR_LIST_FIELDS}).mock(
        return_value=httpx.Response(200, json={"items": [{"id": "primary", "summary": "Me"}]})
    )

    result = await server.list_calendars()

    assert route.called
    assert result == [{"id": "primary", "summary": "Me"}]


@respx.mock
async def test_list_events_requests_compact_fields():
    route = respx.get(f"{server.GCAL}/calendars/primary/events",
                      params={"fields": server._EVENT_LIST_FIELDS}).mock(
        return_value=httpx.Response(200, json={"items": [{"id": "evt1", "summary": "Standup"}]})
    )

    result = await server.list_events()

    assert route.called
    assert result == [{"id": "evt1", "summary": "Standup"}]


@respx.mock
async def test_search_events_requests_compact_fields():
    route = respx.get(f"{server.GCAL}/calendars/primary/events",
                      params={"fields": server._EVENT_LIST_FIELDS}).mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    await server.search_events("standup")

    assert route.called


@respx.mock
async def test_read_message_raises_after_exhausting_network_error_retries():
    route = respx.get(f"{server.GMAIL}/messages/123").mock(
        side_effect=httpx.ConnectTimeout("boom")
    )

    with pytest.raises(httpx.ConnectTimeout):
        await server.read_message("123")

    assert route.call_count == server.API_RETRY_ATTEMPTS
