"""Tests for scrapers/messages.py: parser + two-step HTTP integration.

Fixtures use fabricated subjects, bodies, and sender names. No PHI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.csrf import CSRF_PATH
from openkp.scrapers.messages import (
    DETAILS_PATH,
    DOCDETAILS_LEGACY_PATH,
    FOLDER_TAGS,
    LIST_PATH,
    MAX_PAGE_SIZE,
    PAGE_PATH,
    Attachment,
    Message,
    MessageAttachmentDownload,
    MessageThread,
    MessageThreadDetail,
    _fetch_page_nonce,
    _html_to_text,
    _parse_attachments,
    _parse_conversation_details,
    _parse_conversation_list,
    _parse_message,
    _parse_thread_summary,
    _resolve_display_name,
    _safe_filename,
    download_message_attachment,
    fetch_message,
    fetch_messages,
)


# --- fake data (non-PHI) ---

_FAKE_NONCE = "abc123def456abc123def456abc12345"
_FAKE_CSRF = "fake-csrf-token-abc123"


def _page_html(nonce: str = _FAKE_NONCE) -> str:
    return (
        '<html><head>'
        f'<style nonce="{nonce}" type="text/css">.x {{display:none}}</style>'
        '</head><body>stub</body></html>'
    )


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _sample_list_payload() -> dict:
    return {
        "legacyXUnreadCount": 1,
        "conversations": [
            {
                "subject": "Your test results",
                "previewText": "Everything looks normal. No action needed.",
                "tags": {"Unread": True},
                "hasAttachments": False,
                "hasTasks": False,
                "hasUrgentMsgs": False,
                "hthId": "thread-abc-1",
                "userKeys": ["user-key-1"],
                "viewerKeys": ["viewer-key-1"],
                "organizationId": "org-nca-1",
                "userOverrideNames": {"user-key-1": "DR. FAKE PROVIDER"},
                "messages": [
                    {
                        "wmgId": "msg-1",
                        "isUnread": True,
                        "deliveryInstantISO": "2025-10-02T12:30:00Z",
                        "body": "<p>Everything looks normal.</p>",
                        "author": {"displayName": "DR. FAKE PROVIDER", "empKey": "user-key-1"},
                        "attachments": [],
                        "tasks": [],
                    }
                ],
            },
            {
                "subject": "Appointment reminder",
                "previewText": "Your visit is tomorrow at 10am.",
                "tags": {"System": True},
                "hasAttachments": True,
                "hasTasks": False,
                "hasUrgentMsgs": False,
                "hthId": "thread-abc-2",
                "userKeys": ["user-key-2"],
                "organizationId": "org-nca-1",
                "messages": [
                    {
                        "wmgId": "msg-2",
                        "isUnread": False,
                        "deliveryInstantISO": "2025-10-01T08:00:00Z",
                        "body": "Plain text body, no tags.",
                        "author": {"displayName": "APPOINTMENT BOT"},
                        "attachments": [
                            {
                                "name": "reminder",
                                "fileExtension": "pdf",
                                "dcsId": "opaque",
                                "etxId": "opaque",
                            }
                        ],
                    }
                ],
            },
        ],
        "users": {
            "user-key-2": {"name": "APPOINTMENT BOT", "photoUrl": "", "providerId": "p2"},
        },
        "viewers": {
            "viewer-key-1": {"name": "Patient Name", "isSelf": True},
        },
    }


def _sample_details_payload() -> dict:
    return {
        "hthId": "thread-abc-1",
        "subject": "Your test results",
        "previewText": "Everything looks normal. No action needed.",
        "tags": {"Messages": True},
        "hasAttachments": False,
        "totalMessages": 2,
        "hasUrgentMsgs": False,
        "replyFlags": {"canReply": True, "cannotReplyReason": ""},
        "userKeys": ["user-key-1"],
        "viewerKeys": ["viewer-key-1"],
        "userOverrideNames": {"user-key-1": "DR. FAKE PROVIDER"},
        "organizationId": "org-nca-1",
        "users": {
            "user-key-1": {"name": "DR. FAKE PROVIDER", "empId": "e1"},
        },
        "viewers": {
            "viewer-key-1": {"name": "Patient Name", "isSelf": True},
        },
        "messages": [
            {
                "wmgId": "msg-1b",
                "isUnread": False,
                "deliveryInstantISO": "2025-10-03T09:15:00Z",
                "body": "<p>Follow-up note from the provider.</p><p>See you next month.</p>",
                "author": {"displayName": "DR. FAKE PROVIDER", "empKey": "user-key-1"},
                "attachments": [],
            },
            {
                "wmgId": "msg-1a",
                "isUnread": False,
                "deliveryInstantISO": "2025-10-02T12:30:00Z",
                "body": "<p>Original question from the patient.</p>",
                "author": {"displayName": "Patient Name", "empKey": "viewer-key-1"},
                "attachments": [],
            },
        ],
    }


# --- _html_to_text ---


def test_html_to_text_strips_basic_tags():
    text = _html_to_text("<p>Hello <b>world</b>!</p>")
    assert text == "Hello world !"


def test_html_to_text_preserves_paragraph_breaks():
    text = _html_to_text("<p>First line.</p><p>Second line.</p>")
    assert "First line." in text
    assert "Second line." in text
    # Should have a blank line separating them
    assert "\n\n" in text


def test_html_to_text_handles_br_as_newline():
    text = _html_to_text("Line one<br>Line two")
    assert "Line one" in text and "Line two" in text
    assert "\n" in text


def test_html_to_text_decodes_entities():
    text = _html_to_text("<p>Tom &amp; Jerry</p>")
    assert text == "Tom & Jerry"


def test_html_to_text_plain_text_passes_through():
    text = _html_to_text("Just plain text.")
    assert text == "Just plain text."


def test_html_to_text_collapses_whitespace():
    text = _html_to_text("<p>Too    many    spaces</p>")
    assert "Too many spaces" == text


def test_html_to_text_none_and_empty():
    assert _html_to_text(None) is None
    assert _html_to_text("") is None
    assert _html_to_text("   ") is None
    assert _html_to_text(42) is None


# --- _resolve_display_name ---


def test_resolve_prefers_override():
    users = {"k": {"name": "from users"}}
    viewers = {"k": {"name": "from viewers"}}
    overrides = {"k": "from overrides"}
    assert _resolve_display_name("k", users=users, viewers=viewers, overrides=overrides) == "from overrides"


def test_resolve_falls_back_to_users():
    users = {"k": {"name": "from users"}}
    viewers = {"k": {"name": "from viewers"}}
    overrides = {}
    assert _resolve_display_name("k", users=users, viewers=viewers, overrides=overrides) == "from users"


def test_resolve_falls_back_to_viewers():
    users = {}
    viewers = {"k": {"name": "from viewers"}}
    overrides = {}
    assert _resolve_display_name("k", users=users, viewers=viewers, overrides=overrides) == "from viewers"


def test_resolve_none_key():
    assert _resolve_display_name(None, users={}, viewers={}, overrides={}) is None
    assert _resolve_display_name("", users={}, viewers={}, overrides={}) is None


def test_resolve_missing_key():
    assert _resolve_display_name("no-such-key", users={}, viewers={}, overrides={}) is None


def test_resolve_handles_non_dict_pools():
    assert _resolve_display_name("k", users="not a dict", viewers={"k": {"name": "v"}}, overrides={}) == "v"


# --- _parse_attachments ---


def test_parse_attachments_basic():
    raw = [
        {
            "type": 2,
            "name": "lab-report",
            "fileExtension": "pdf",
            "dcsId": "dcs-x",
            "etxId": "etx-y",
            "organizationId": "org-1",
        },
        {"name": "photo", "fileExtension": "jpg"},
    ]
    attachments = _parse_attachments(raw)
    assert attachments == [
        Attachment(
            name="lab-report",
            file_extension="pdf",
            dcs_id="dcs-x",
            attachment_type=2,
            organization_id="org-1",
        ),
        Attachment(name="photo", file_extension="jpg"),
    ]


def test_parse_attachments_keeps_item_with_only_dcs_id():
    """A real attachment may have a dcs_id even when name/ext are absent;
    keep it so the caller can still download by handle."""
    raw = [{"dcsId": "dcs-z"}]
    attachments = _parse_attachments(raw)
    assert len(attachments) == 1
    assert attachments[0].dcs_id == "dcs-z"
    assert attachments[0].name is None


def test_parse_attachments_ignores_non_int_type():
    raw = [{"name": "x.pdf", "fileExtension": "pdf", "type": "weird"}]
    attachments = _parse_attachments(raw)
    assert attachments[0].attachment_type is None


def test_parse_attachments_empty_and_invalid():
    assert _parse_attachments([]) == []
    assert _parse_attachments(None) == []
    assert _parse_attachments("garbage") == []


def test_parse_attachments_skips_empty_items():
    raw = [{"name": "keep.pdf", "fileExtension": "pdf"}, "not a dict", {"name": None}]
    attachments = _parse_attachments(raw)
    assert len(attachments) == 1
    assert attachments[0].name == "keep.pdf"


# --- _parse_message ---


def test_parse_message_happy_path():
    raw = {
        "wmgId": "m1",
        "isUnread": True,
        "deliveryInstantISO": "2025-10-02T12:00:00Z",
        "body": "<p>Short note.</p>",
        "author": {"displayName": "DR. FAKE PROVIDER", "empKey": "k1"},
        "attachments": [],
    }
    msg = _parse_message(raw, users={}, viewers={}, overrides={})
    assert msg is not None
    assert msg.id == "m1"
    assert msg.sent_at == "2025-10-02T12:00:00Z"
    assert msg.is_unread is True
    assert msg.author is not None and msg.author.name == "DR. FAKE PROVIDER"
    assert msg.body_text == "Short note."


def test_parse_message_falls_back_to_key_resolution_for_author():
    raw = {
        "wmgId": "m2",
        "deliveryInstantISO": "",
        "body": "text",
        "author": {"empKey": "k1"},
    }
    overrides = {"k1": "RESOLVED NAME"}
    msg = _parse_message(raw, users={}, viewers={}, overrides=overrides)
    assert msg is not None
    assert msg.author is not None and msg.author.name == "RESOLVED NAME"


def test_parse_message_missing_wmg_id_returns_none():
    assert _parse_message({"body": "text"}, users={}, viewers={}, overrides={}) is None


def test_parse_message_non_dict_returns_none():
    assert _parse_message(None, users={}, viewers={}, overrides={}) is None
    assert _parse_message("garbage", users={}, viewers={}, overrides={}) is None


# --- _parse_thread_summary ---


def test_parse_thread_summary_happy_path():
    conv = _sample_list_payload()["conversations"][0]
    summary = _parse_thread_summary(
        conv,
        users={},
        viewers={},
        folder_tag=1,
    )
    assert summary is not None
    assert summary.id == "thread-abc-1"
    assert summary.subject == "Your test results"
    assert summary.preview == "Everything looks normal. No action needed."
    assert summary.is_unread is True
    assert summary.has_attachments is False
    assert summary.last_sender == "DR. FAKE PROVIDER"
    assert summary.last_message_at == "2025-10-02T12:30:00Z"
    assert summary.folder_tag == 1
    assert summary.organization_id == "org-nca-1"


def test_parse_thread_summary_without_unread_tag():
    conv = _sample_list_payload()["conversations"][1]
    summary = _parse_thread_summary(conv, users={}, viewers={}, folder_tag=7)
    assert summary is not None
    assert summary.is_unread is False
    assert summary.has_attachments is True


def test_parse_thread_summary_missing_hth_id_returns_none():
    assert _parse_thread_summary({"subject": "x"}, users={}, viewers={}, folder_tag=1) is None


def test_parse_thread_summary_resolves_sender_from_user_keys_when_inline_missing():
    conv = {
        "hthId": "thread-1",
        "subject": "Ping",
        "userKeys": ["k1"],
        "messages": [
            {
                "wmgId": "m1",
                "deliveryInstantISO": "2025-10-01T00:00:00Z",
                "author": {},  # no displayName
            }
        ],
    }
    users = {"k1": {"name": "FROM USERS MAP"}}
    summary = _parse_thread_summary(conv, users=users, viewers={}, folder_tag=1)
    assert summary is not None
    assert summary.last_sender == "FROM USERS MAP"


# --- _parse_conversation_list ---


def test_parse_conversation_list_happy_path():
    threads = _parse_conversation_list(_sample_list_payload(), folder_tag=1)
    assert len(threads) == 2
    assert threads[0].id == "thread-abc-1"
    assert threads[1].id == "thread-abc-2"
    # folder_tag propagated
    assert all(t.folder_tag == 1 for t in threads)


def test_parse_conversation_list_skips_non_dict_items():
    payload = {"conversations": ["garbage", None, _sample_list_payload()["conversations"][0]]}
    threads = _parse_conversation_list(payload, folder_tag=1)
    assert len(threads) == 1


def test_parse_conversation_list_malformed_returns_empty():
    assert _parse_conversation_list({}, folder_tag=1) == []
    assert _parse_conversation_list(None, folder_tag=1) == []
    assert _parse_conversation_list({"conversations": "not a list"}, folder_tag=1) == []


# --- _parse_conversation_details ---


def test_parse_conversation_details_happy_path():
    detail = _parse_conversation_details(_sample_details_payload())
    assert detail is not None
    assert detail.id == "thread-abc-1"
    assert detail.subject == "Your test results"
    assert detail.can_reply is True
    assert detail.total_messages == 2
    assert len(detail.messages) == 2
    assert detail.messages[0].id == "msg-1b"
    assert detail.messages[0].body_text == "Follow-up note from the provider.\n\nSee you next month."


def test_parse_conversation_details_missing_hth_id():
    assert _parse_conversation_details({"subject": "x"}) is None


def test_parse_conversation_details_no_reply_flags():
    payload = _sample_details_payload()
    payload.pop("replyFlags")
    detail = _parse_conversation_details(payload)
    assert detail is not None
    assert detail.can_reply is False


def test_parse_conversation_details_non_dict_returns_none():
    assert _parse_conversation_details(None) is None
    assert _parse_conversation_details([]) is None


def test_parse_conversation_details_no_messages():
    payload = _sample_details_payload()
    payload["messages"] = []
    detail = _parse_conversation_details(payload)
    assert detail is not None
    assert detail.messages == []


# --- HTTP integration: shared mock plumbing ---


def _make_store() -> MagicMock:
    from openkp.scrapers.auth import KaiserSession

    store = MagicMock()
    store.get_session = AsyncMock(
        return_value=KaiserSession(
            cookies=[{"name": "k", "value": "v", "domain": ".kp.org", "path": "/"}],
            user_agent="ua",
        )
    )
    store.invalidate = AsyncMock()
    return store


def _bind_request(responses: list[httpx.Response]) -> list[httpx.Response]:
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org" + PAGE_PATH)
    for r in responses:
        r.request = req
    return responses


def _patch_http(responses: list[httpx.Response]):
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=_bind_request(responses))
    patched = patch("openkp.scrapers.request.httpx.AsyncClient")
    client_cls = patched.start()
    client_cls.return_value.__aenter__.return_value = mock_client
    client_cls.return_value.__aexit__.return_value = None
    return mock_client, patched


# --- _fetch_page_nonce ---


@pytest.mark.asyncio
async def test_fetch_page_nonce_extracts_value():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, text=_page_html("abcdef1234567890"))])
    try:
        nonce = await _fetch_page_nonce(KaiserRequest(store))
    finally:
        p.stop()

    assert nonce == "abcdef1234567890"
    call = mock_client.request.await_args
    assert call.args[0] == "GET"
    assert PAGE_PATH in call.args[1]


@pytest.mark.asyncio
async def test_fetch_page_nonce_raises_when_missing():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([httpx.Response(200, text="<html><body>no nonce anywhere</body></html>")])
    try:
        with pytest.raises(ValueError, match="Page nonce"):
            await _fetch_page_nonce(KaiserRequest(store))
    finally:
        p.stop()


# --- fetch_messages (list) ---


@pytest.mark.asyncio
async def test_fetch_messages_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_list_payload()),
    ])
    try:
        threads = await fetch_messages(KaiserRequest(store), folder="inbox")
    finally:
        p.stop()

    assert len(threads) == 2
    assert threads[0].subject == "Your test results"
    # 3 calls: page nonce, CSRF token, list POST
    assert mock_client.request.await_count == 3

    # First call = page nonce GET
    nonce_call = mock_client.request.await_args_list[0]
    assert nonce_call.args[0] == "GET"
    assert PAGE_PATH in nonce_call.args[1]

    # Second call = CSRF GET
    csrf_call = mock_client.request.await_args_list[1]
    assert csrf_call.args[0] == "GET"
    assert CSRF_PATH in csrf_call.args[1]

    # Third call = list POST with nonce in body AND CSRF in header
    list_call = mock_client.request.await_args_list[2]
    assert list_call.args[0] == "POST"
    assert LIST_PATH in list_call.args[1]
    body = list_call.kwargs["json"]
    assert body["tag"] == FOLDER_TAGS["inbox"]
    assert body["PageNonce"] == _FAKE_NONCE
    assert body["searchQuery"] == ""
    assert body["localLoadParams"]["pagingInfo"] == 1
    headers = list_call.kwargs["headers"]
    assert headers["__RequestVerificationToken"] == _FAKE_CSRF


@pytest.mark.asyncio
async def test_fetch_messages_passes_search_and_cursor():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"conversations": [], "users": {}, "viewers": {}}),
    ])
    try:
        await fetch_messages(
            KaiserRequest(store),
            folder="archive",
            search="lab",
            before_iso="2024-01-01T00:00:00Z",
        )
    finally:
        p.stop()

    body = mock_client.request.await_args_list[2].kwargs["json"]
    assert body["tag"] == FOLDER_TAGS["archive"]
    assert body["searchQuery"] == "lab"
    assert body["localLoadParams"]["loadStartInstantISO"] == "2024-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_fetch_messages_unknown_folder_returns_empty_and_skips_http():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, text=_page_html())])
    try:
        threads = await fetch_messages(KaiserRequest(store), folder="nonsense")
    finally:
        p.stop()

    assert threads == []
    assert mock_client.request.await_count == 0  # Never even fetched the nonce


@pytest.mark.asyncio
async def test_fetch_messages_clamps_limit():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    # Build a 60-item conversation list
    oversize = {
        "conversations": [
            {"hthId": f"t-{i}", "subject": f"Subject {i}", "messages": [], "userKeys": []}
            for i in range(60)
        ],
        "users": {},
        "viewers": {},
    }
    _, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=oversize),
    ])
    try:
        threads = await fetch_messages(KaiserRequest(store), folder="inbox", limit=999)
    finally:
        p.stop()

    assert len(threads) == MAX_PAGE_SIZE


# --- fetch_messages deep_search (pagination walk) ---


def _deep_page(threads: list[dict], *, has_more: bool, oldest_searched: str = "") -> dict:
    """Build a GetConversationList payload shaped like the real responses
    we see in kp-messages-deepsearch-1.har, with the localSummary contract."""
    return {
        "conversations": threads,
        "users": {},
        "viewers": {},
        "localSummary": {
            "hasMoreConversations": has_more,
            "oldestSearchedInstantISO": oldest_searched,
            "newestLoadedInstantISO": "",
            "oldestLoadedInstantISO": threads[-1].get("messages", [{}])[0].get("deliveryInstantISO", "") if threads else "",
            "numberLoaded": len(threads),
            "pagingInfo": 0,
        },
    }


def _deep_thread(hth_id: str, sent_at: str = "2024-01-01T00:00:00Z") -> dict:
    return {
        "hthId": hth_id,
        "subject": f"Subject {hth_id}",
        "userKeys": [],
        "messages": [{"wmgId": f"m-{hth_id}", "deliveryInstantISO": sent_at, "body": "x"}],
    }


@pytest.mark.asyncio
async def test_fetch_messages_deep_search_walks_pagination():
    """The genetics-thread scenario: search returns nothing on the first page,
    then hits a match on a later page reachable only via the
    oldestSearchedInstantISO cursor in localSummary."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    page1 = _deep_page([], has_more=True, oldest_searched="2024-05-09T18:34:09Z")
    page2 = _deep_page(
        [_deep_thread("genetics-2023-06-24", "2023-06-24T01:56:39Z")],
        has_more=True,
        oldest_searched="2023-04-12T16:53:57Z",
    )
    page3 = _deep_page([], has_more=False)
    mock_client, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
        httpx.Response(200, json=page3),
    ])
    try:
        threads = await fetch_messages(
            KaiserRequest(store),
            folder="inbox",
            search="genetic",
            deep_search=True,
        )
    finally:
        p.stop()

    assert len(threads) == 1
    assert threads[0].id == "genetics-2023-06-24"

    # 2 setup calls (nonce, csrf) + 3 list POSTs.
    assert mock_client.request.await_count == 5

    # Each list POST should reuse the same nonce + CSRF.
    list_calls = mock_client.request.await_args_list[2:]
    assert all(call.kwargs["json"]["PageNonce"] == _FAKE_NONCE for call in list_calls)
    assert all(
        call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF
        for call in list_calls
    )
    # Cursor advances using oldestSearchedInstantISO from the previous page.
    assert list_calls[0].kwargs["json"]["localLoadParams"]["loadStartInstantISO"] == ""
    assert list_calls[1].kwargs["json"]["localLoadParams"]["loadStartInstantISO"] == "2024-05-09T18:34:09Z"
    assert list_calls[2].kwargs["json"]["localLoadParams"]["loadStartInstantISO"] == "2023-04-12T16:53:57Z"
    # Search query stays constant.
    assert all(call.kwargs["json"]["searchQuery"] == "genetic" for call in list_calls)


@pytest.mark.asyncio
async def test_fetch_messages_deep_search_stops_when_no_more():
    """hasMoreConversations=false should terminate the walk even before
    max_pages is exhausted."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    page = _deep_page([_deep_thread("only")], has_more=False)
    mock_client, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=page),
    ])
    try:
        threads = await fetch_messages(
            KaiserRequest(store), folder="inbox", deep_search=True, max_pages=99,
        )
    finally:
        p.stop()

    assert len(threads) == 1
    # Only 1 list POST despite max_pages=99 — because hasMoreConversations was False.
    assert mock_client.request.await_count == 3


@pytest.mark.asyncio
async def test_fetch_messages_deep_search_respects_max_pages():
    """When Kaiser keeps saying hasMoreConversations=true, the walk must
    bail at max_pages so we don't loop forever."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    # Each page advances the cursor and claims more results exist.
    pages = [
        _deep_page(
            [_deep_thread(f"thread-{i}")],
            has_more=True,
            oldest_searched=f"2024-{12 - i:02d}-01T00:00:00Z",
        )
        for i in range(5)
    ]
    responses = [httpx.Response(200, text=_page_html()), httpx.Response(200, text=_csrf_html())]
    responses += [httpx.Response(200, json=p) for p in pages]
    mock_client, patched = _patch_http(responses)
    try:
        threads = await fetch_messages(
            KaiserRequest(store), folder="inbox", deep_search=True, max_pages=3,
        )
    finally:
        patched.stop()

    # 3 pages walked, 3 threads collected. Pages 4 and 5 not requested.
    assert len(threads) == 3
    assert mock_client.request.await_count == 2 + 3


@pytest.mark.asyncio
async def test_fetch_messages_deep_search_dedupes_by_id():
    """If Kaiser returns the same thread on two consecutive pages (boundary
    artifact), we should only count it once."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    dup_thread = _deep_thread("dup-1")
    page1 = _deep_page([dup_thread], has_more=True, oldest_searched="2024-01-01T00:00:00Z")
    page2 = _deep_page([dup_thread, _deep_thread("unique-2")], has_more=False)
    _, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ])
    try:
        threads = await fetch_messages(
            KaiserRequest(store), folder="inbox", deep_search=True,
        )
    finally:
        p.stop()

    ids = [t.id for t in threads]
    assert ids == ["dup-1", "unique-2"]


@pytest.mark.asyncio
async def test_fetch_messages_deep_search_breaks_on_stuck_cursor():
    """Defense against a malformed response that would cause an infinite
    loop: if oldestSearchedInstantISO doesn't advance, stop walking."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    stuck = _deep_page(
        [_deep_thread("page1")],
        has_more=True,
        oldest_searched="2024-05-09T18:34:09Z",
    )
    same_cursor = _deep_page(
        [_deep_thread("page2")],
        has_more=True,
        oldest_searched="2024-05-09T18:34:09Z",  # same as previous
    )
    mock_client, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=stuck),
        httpx.Response(200, json=same_cursor),
    ])
    try:
        threads = await fetch_messages(
            KaiserRequest(store), folder="inbox", deep_search=True, max_pages=10,
        )
    finally:
        p.stop()

    # Two pages walked: page1 normal, page2 with stuck cursor (caught and stopped).
    # Should NOT walk page 3 because cursor didn't advance.
    assert len(threads) == 2
    assert mock_client.request.await_count == 2 + 2


@pytest.mark.asyncio
async def test_fetch_messages_single_page_default_unchanged():
    """Sanity check: deep_search=False (the default) preserves single-page
    behavior — no localSummary inspection, no cursor walking."""
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_list_payload()),
    ])
    try:
        threads = await fetch_messages(KaiserRequest(store), folder="inbox")
    finally:
        p.stop()

    assert len(threads) == 2
    # Exactly one list POST in single-page mode.
    list_posts = [
        c for c in mock_client.request.await_args_list
        if c.args[0] == "POST" and LIST_PATH in c.args[1]
    ]
    assert len(list_posts) == 1


# --- fetch_message (single thread read) ---


@pytest.mark.asyncio
async def test_fetch_message_happy_path():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_sample_details_payload()),
    ])
    try:
        detail = await fetch_message(KaiserRequest(store), "thread-abc-1")
    finally:
        p.stop()

    assert isinstance(detail, MessageThreadDetail)
    assert detail.id == "thread-abc-1"
    assert detail.can_reply is True
    assert len(detail.messages) == 2

    # Third call = details POST with id + nonce + CSRF header
    details_call = mock_client.request.await_args_list[2]
    assert details_call.args[0] == "POST"
    assert DETAILS_PATH in details_call.args[1]
    body = details_call.kwargs["json"]
    assert body["id"] == "thread-abc-1"
    assert body["PageNonce"] == _FAKE_NONCE
    assert details_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF


@pytest.mark.asyncio
async def test_fetch_message_empty_id_returns_none_without_http():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, text=_page_html())])
    try:
        detail = await fetch_message(KaiserRequest(store), "")
    finally:
        p.stop()

    assert detail is None
    assert mock_client.request.await_count == 0


@pytest.mark.asyncio
async def test_fetch_message_returns_none_on_malformed_response():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"unexpected": "shape"}),
    ])
    try:
        detail = await fetch_message(KaiserRequest(store), "thread-x")
    finally:
        p.stop()

    assert detail is None


@pytest.mark.asyncio
async def test_fetch_message_propagates_http_errors():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_page_html()),
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(500, text="kaboom"),
    ])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_message(KaiserRequest(store), "thread-x")
    finally:
        p.stop()


# --- _safe_filename ---


def test_safe_filename_replaces_unsafe_chars():
    assert _safe_filename('a/b\\c:d*e?"f<g>h|i') == "a_b_c_d_e__f_g_h_i"


def test_safe_filename_caps_length():
    assert len(_safe_filename("x" * 500)) <= 180


def test_safe_filename_falls_back_when_blank():
    assert _safe_filename("   ") == "attachment"


# --- download_message_attachment ---


@pytest.mark.asyncio
async def test_download_message_attachment_happy_path(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    pdf_bytes = b"%PDF-1.7\nFAKE PDF BYTES\n%%EOF"
    download_url_relative = (
        "/Documents/ViewDocument/Download?dcsid=dcs-1&displayName=Report&dcsExt=PDF"
    )
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"downloadUrl": download_url_relative, "displayName": "Report"}),
        httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"}),
    ])
    try:
        outcome = await download_message_attachment(
            KaiserRequest(store),
            "dcs-1",
            file_extension="PDF",
            download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert isinstance(outcome, MessageAttachmentDownload)
    assert outcome.status == "downloaded"
    assert outcome.filename == "Report.pdf"
    assert outcome.size_bytes == len(pdf_bytes)
    saved = Path(outcome.path)
    assert saved.exists()
    assert saved.read_bytes() == pdf_bytes

    # 3 calls: CSRF GET, GetDocumentDetailsLegacy POST, binary GET
    assert mock_client.request.await_count == 3
    det_call = mock_client.request.await_args_list[1]
    assert det_call.args[0] == "POST"
    assert DOCDETAILS_LEGACY_PATH in det_call.args[1]
    body = det_call.kwargs["json"]
    assert body["dcsId"] == "dcs-1"
    assert body["fileExtension"] == "PDF"
    assert body["organizationId"] == ""
    assert body["useOldMobileLink"] is False

    # The binary GET should hit /mychartcn-prefixed path (Kaiser's downloadUrl
    # arrives without that prefix and we have to add it back).
    bin_call = mock_client.request.await_args_list[2]
    assert bin_call.args[0] == "GET"
    assert "/mychartcn/Documents/ViewDocument/Download" in bin_call.args[1]


@pytest.mark.asyncio
async def test_download_message_attachment_empty_dcs_id_skips_http(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([httpx.Response(200, text=_csrf_html())])
    try:
        outcome = await download_message_attachment(
            KaiserRequest(store), "", download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert outcome.status == "error"
    assert "empty" in (outcome.reason or "").lower()
    assert mock_client.request.await_count == 0


@pytest.mark.asyncio
async def test_download_message_attachment_no_download_url(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"displayName": "X"}),  # no downloadUrl
    ])
    try:
        outcome = await download_message_attachment(
            KaiserRequest(store), "dcs-1", download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert outcome.status == "error"
    assert "downloadUrl" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_download_message_attachment_uses_display_name_override(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    pdf_bytes = b"%PDF-1.7\n"
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"downloadUrl": "/Documents/ViewDocument/Download?x=1", "displayName": "kaiser-name"}),
        httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"}),
    ])
    try:
        outcome = await download_message_attachment(
            KaiserRequest(store),
            "dcs-1",
            file_extension="PDF",
            display_name="my-override",
            download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert outcome.filename == "my-override.pdf"


@pytest.mark.asyncio
async def test_download_message_attachment_propagates_http_errors(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(500, text="kaboom"),
    ])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await download_message_attachment(
                KaiserRequest(store), "dcs-1", download_dir=tmp_path,
            )
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_download_message_attachment_empty_body_returns_error(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"downloadUrl": "/Documents/ViewDocument/Download?x=1", "displayName": "X"}),
        httpx.Response(200, content=b""),
    ])
    try:
        outcome = await download_message_attachment(
            KaiserRequest(store), "dcs-1", download_dir=tmp_path,
        )
    finally:
        p.stop()

    assert outcome.status == "error"
    assert "empty" in (outcome.reason or "").lower()


@pytest.mark.asyncio
async def test_download_message_attachment_passes_organization_id(tmp_path: Path):
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    pdf_bytes = b"%PDF\n"
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"downloadUrl": "/Documents/ViewDocument/Download?x=1", "displayName": "X"}),
        httpx.Response(200, content=pdf_bytes),
    ])
    try:
        await download_message_attachment(
            KaiserRequest(store),
            "dcs-1",
            organization_id="org-cross-region",
            download_dir=tmp_path,
        )
    finally:
        p.stop()

    body = mock_client.request.await_args_list[1].kwargs["json"]
    assert body["organizationId"] == "org-cross-region"
