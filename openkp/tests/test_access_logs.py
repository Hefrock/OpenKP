"""Tests for scrapers/access_logs.py: parser + bounded pagination.

Fixtures use fabricated app names and timestamps. No PHI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.access_logs import (
    PAGE_REFERER,
    PORTAL_PATH,
    THIRD_PARTY_PATH,
    AccessLogResponse,
    _int_or_none,
    _normalize_kind,
    _parse_access_log_entry,
    _parse_access_log_page,
    _str_or_none,
    fetch_access_log,
)
from openkp.scrapers.csrf import CSRF_PATH


# --- fake data (non-PHI) ---


_FAKE_CSRF = "fake-csrf-token-abc123"


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _third_party_entry(**overrides) -> dict:
    base = {
        "action": "Test Result Details",
        "accessMethod": 2,
        "accessor": "Example Health App",
        "accessTime": "2026-04-18T16:38:27-07:00",
    }
    base.update(overrides)
    return base


def _portal_entry(**overrides) -> dict:
    base = {
        "entryType": 1,
        "ccdAction": 0,
        "accessor": "Patient Portal",
        "accessTime": "2026-04-25T07:42:43-07:00",
    }
    base.update(overrides)
    return base


def _page(entries: list[dict] | None = None, *, next_cursor: int | None = None) -> dict:
    return {
        "entries": entries if entries is not None else [_third_party_entry()],
        "nextLineToParse": next_cursor,
    }


# --- helpers ---


def test_str_or_none_strips_and_handles_empty():
    assert _str_or_none("  hello  ") == "hello"
    assert _str_or_none("") is None
    assert _str_or_none("   ") is None
    assert _str_or_none(None) is None
    assert _str_or_none(42) == "42"


def test_int_or_none_accepts_int_rejects_bool():
    assert _int_or_none(0) == 0
    assert _int_or_none(7) == 7
    assert _int_or_none(True) is None
    assert _int_or_none(False) is None
    assert _int_or_none("7") is None
    assert _int_or_none(None) is None


def test_normalize_kind_accepts_aliases():
    assert _normalize_kind("third_party") == "third_party"
    assert _normalize_kind("third-party") == "third_party"
    assert _normalize_kind("apps") == "third_party"
    assert _normalize_kind("portal") == "portal"
    assert _normalize_kind("self") == "portal"


def test_normalize_kind_rejects_unknown():
    with pytest.raises(ValueError, match="third_party"):
        _normalize_kind("billing")


# --- _parse_access_log_entry ---


def test_parse_third_party_entry_full_field_extraction():
    entry = _parse_access_log_entry(_third_party_entry(accessor="  Example App  "), "third_party")
    assert entry is not None
    assert entry.kind == "third_party"
    assert entry.accessor == "Example App"
    assert entry.access_time == "2026-04-18T16:38:27-07:00"
    assert entry.action == "Test Result Details"
    assert entry.access_method == 2
    assert entry.entry_type is None
    assert entry.ccd_action is None


def test_parse_portal_entry_full_field_extraction():
    entry = _parse_access_log_entry(_portal_entry(accessor="  Patient Portal  "), "portal")
    assert entry is not None
    assert entry.kind == "portal"
    assert entry.accessor == "Patient Portal"
    assert entry.access_time == "2026-04-25T07:42:43-07:00"
    assert entry.entry_type == 1
    assert entry.ccd_action == 0
    assert entry.action is None
    assert entry.access_method is None


def test_parse_access_log_entry_missing_optional_fields_yield_none():
    entry = _parse_access_log_entry({}, "third_party")
    assert entry is not None
    assert entry.kind == "third_party"
    assert entry.accessor is None
    assert entry.access_time is None
    assert entry.action is None
    assert entry.access_method is None


def test_parse_access_log_entry_non_dict_returns_none():
    assert _parse_access_log_entry(None, "third_party") is None
    assert _parse_access_log_entry("garbage", "portal") is None
    assert _parse_access_log_entry(42, "portal") is None


# --- _parse_access_log_page ---


def test_parse_access_log_page_happy_path():
    response = _parse_access_log_page(
        _page([
            _third_party_entry(accessor="App A"),
            _third_party_entry(accessor="App B", action="Immunizations"),
        ], next_cursor=12),
        "third_party",
    )
    assert response.next_cursor == 12
    assert len(response.entries) == 2
    assert response.entries[0].accessor == "App A"
    assert response.entries[1].action == "Immunizations"


def test_parse_access_log_page_skips_unparseable_entries():
    response = _parse_access_log_page(
        {"entries": [_portal_entry(), "garbage", None], "nextLineToParse": 99},
        "portal",
    )
    assert response.next_cursor == 99
    assert len(response.entries) == 1
    assert response.entries[0].kind == "portal"


def test_parse_access_log_page_malformed_payload_returns_empty():
    assert _parse_access_log_page(None, "third_party").entries == []
    assert _parse_access_log_page("garbage", "third_party").entries == []
    assert _parse_access_log_page({"entries": "not a list"}, "third_party").entries == []


# --- HTTP integration ---


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
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org" + THIRD_PARTY_PATH)
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


@pytest.mark.asyncio
async def test_fetch_access_log_third_party_walks_pages():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_page([_third_party_entry(accessor="App A")], next_cursor=1)),
        httpx.Response(200, json=_page([_third_party_entry(accessor="App B")], next_cursor=None)),
    ])
    try:
        response = await fetch_access_log(KaiserRequest(store), kind="third_party", max_pages=5)
    finally:
        p.stop()

    assert isinstance(response, AccessLogResponse)
    assert response.kind == "third_party"
    assert response.total_count == 2
    assert [e.accessor for e in response.entries] == ["App A", "App B"]
    assert response.pages_walked == 2
    assert response.has_more is False
    assert response.stop_reason == "no_next_cursor"

    assert mock_client.request.await_count == 3
    csrf_call = mock_client.request.await_args_list[0]
    assert csrf_call.args[0] == "GET"
    assert CSRF_PATH in csrf_call.args[1]

    first_page = mock_client.request.await_args_list[1]
    assert first_page.args[0] == "POST"
    assert THIRD_PARTY_PATH in first_page.args[1]
    assert first_page.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF
    assert first_page.kwargs["headers"]["Referer"] == PAGE_REFERER
    assert first_page.kwargs["json"] == {"startingLine": -1}

    second_page = mock_client.request.await_args_list[2]
    assert second_page.kwargs["json"] == {"startingLine": 1}


@pytest.mark.asyncio
async def test_fetch_access_log_portal_uses_portal_endpoint():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json={"entries": [_portal_entry()], "nextLineToParse": None}),
    ])
    try:
        response = await fetch_access_log(KaiserRequest(store), kind="portal")
    finally:
        p.stop()

    assert response.kind == "portal"
    assert response.total_count == 1
    assert response.entries[0].entry_type == 1
    list_call = mock_client.request.await_args_list[1]
    assert PORTAL_PATH in list_call.args[1]


@pytest.mark.asyncio
async def test_fetch_access_log_repeated_cursor_warns_and_stops():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_page([_third_party_entry(accessor="App A")], next_cursor=1)),
        httpx.Response(200, json=_page([_third_party_entry(accessor="App B")], next_cursor=1)),
    ])
    try:
        response = await fetch_access_log(KaiserRequest(store), kind="third_party", max_pages=5)
    finally:
        p.stop()

    assert response.total_count == 2
    assert response.pages_walked == 2
    assert response.has_more is True
    assert response.next_cursor == 1
    assert response.stop_reason == "cursor_repeated"
    assert response.warnings == [
        "Kaiser returned the same access-log cursor twice; results may be incomplete."
    ]


@pytest.mark.asyncio
async def test_fetch_access_log_max_pages_sets_has_more():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    _, p = _patch_http([
        httpx.Response(200, text=_csrf_html()),
        httpx.Response(200, json=_page([_third_party_entry()], next_cursor=7)),
    ])
    try:
        response = await fetch_access_log(KaiserRequest(store), kind="third_party", max_pages=1)
    finally:
        p.stop()

    assert response.total_count == 1
    assert response.pages_walked == 1
    assert response.has_more is True
    assert response.next_cursor == 7
    assert response.stop_reason == "max_pages_reached"


@pytest.mark.asyncio
async def test_fetch_access_log_rejects_unknown_kind_before_http():
    from openkp.scrapers.request import KaiserRequest

    with pytest.raises(ValueError, match="third_party"):
        await fetch_access_log(KaiserRequest(_make_store()), kind="billing")
