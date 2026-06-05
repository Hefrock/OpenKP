"""Tests for scrapers/upcoming_orders.py.

Fixtures are fabricated. No PHI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.csrf import CSRF_PATH
from openkp.scrapers.upcoming_orders import (
    DETAIL_PATH,
    HOME_REFERER,
    MIXED_ITEM_FEED_PATH,
    UpcomingOrdersResponse,
    _extract_order_ids,
    _html_to_text,
    _parse_upcoming_orders_detail,
    fetch_upcoming_order_instructions,
    fetch_upcoming_orders,
)


_FAKE_CSRF = "fake-csrf-token-abc123"
_FAKE_NONCE = "abcdef1234567890abcdef1234567890"
_ORDER_ID = "WP-24fake-order-id"
_PROVIDER_ID = "WP-24fake-provider-id"
_GROUP_ID = "0"


def _csrf_html(token: str = _FAKE_CSRF) -> str:
    return f'<input name="__RequestVerificationToken" type="hidden" value="{token}" />'


def _nonce_html(nonce: str = _FAKE_NONCE) -> str:
    return f"<html><head><style nonce='{nonce}'></style></head></html>"


def _home_feed(order_id: str = _ORDER_ID) -> str:
    return (
        '<div class="item">'
        f'<a href="/mychartcn/app/upcoming-orders?ordid={order_id}">View instructions</a>'
        "</div>"
    )


def _home_feed_payload(order_id: str = _ORDER_ID) -> dict:
    return {
        "SingleItemFeedViewModels": [
            {
                "FeedItems": [
                    {
                        "PrimaryAction": {
                            "Uri": f"app/upcoming-orders?ordid={order_id}",
                            "UriDisplayText": "View instructions",
                        },
                        "DefaultAction": {
                            "Uri": f"epichttp://app/upcoming-orders?ordid={order_id}",
                            "UriDisplayText": "View instructions",
                        },
                    }
                ]
            }
        ]
    }


def _detail_payload(order_id: str = _ORDER_ID) -> dict:
    return {
        "orderGroupList": {
            _GROUP_ID: {
                "orderIDs": [order_id],
                "visitName": "Example Ancillary Orders",
                "encProviderID": _PROVIDER_ID,
                "dueDateISO": "2026-07-01",
                "encDateISO": "2026-04-26",
                "isSnoozed": False,
                "snoozedUntilDateISO": "2026-06-20",
                "schedulingTicketID": "",
                "apptCSN": "",
                "apptDateISO": "",
                "ticketAvailableDateISO": "",
            }
        },
        "orderList": {
            order_id: {
                "id": order_id,
                "name": "COMPLETE BLOOD COUNT",
                "orderedDateISO": "2026-04-26",
                "actionableDateISO": "2026-07-01",
                "actionableDateType": "expire",
                "expectedDateISO": "",
                "expiredDateISO": "2026-07-01",
                "lastPerformedDateISO": "",
                "comments": ["Use any Kaiser lab."],
                "expectedDateComment": "",
                "instructions": (
                    "<div><p>Schedule a lab appointment or walk into any lab.</p>"
                    "<p>Bring your member ID.</p></div>"
                ),
                "isPRN": False,
                "isStanding": False,
                "standingOccurrences": "",
                "originalStandingOccurrences": "",
                "orderInterval": "",
            }
        },
        "providerList": {
            _PROVIDER_ID: {
                "id": _PROVIDER_ID,
                "name": "EXAMPLE PROVIDER MD",
                "photoURL": "/mychartcn/Image/Load?fileName=example",
                "photoBlobKey": "",
                "photoToken": "",
                "photoIsOnBlob": False,
            }
        },
        "upcomingOrdersSettings": {
            "canHideOrUnhideReminders": True,
            "selectedOrderGroup": _GROUP_ID,
        },
    }


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
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org/mychartcn")
    for response in responses:
        response.request = req
    return responses


def _patch_http(responses: list[httpx.Response]):
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=_bind_request(responses))
    patched = patch("openkp.scrapers.request.httpx.AsyncClient")
    client_cls = patched.start()
    client_cls.return_value.__aenter__.return_value = mock_client
    client_cls.return_value.__aexit__.return_value = None
    return mock_client, patched


def test_extract_order_ids_dedupes_preserving_order():
    payload = {
        "html": _home_feed("one") + _home_feed("one"),
        "items": [
            {"PrimaryAction": {"Uri": "app/upcoming-orders?ordid=two"}},
            {"DefaultAction": {"Uri": "epichttp://app/upcoming-orders?ordid=two"}},
        ],
    }
    assert _extract_order_ids(payload) == ["one", "two"]


def test_html_to_text_preserves_instruction_paragraphs():
    text = _html_to_text("<div><p>First line</p><p>Second<br>line</p></div>")
    assert text == "First line\n\nSecond\nline"


def test_parse_upcoming_orders_detail_happy_path():
    detail = _parse_upcoming_orders_detail(_detail_payload())

    assert detail.group_count == 1
    assert detail.provider_count == 1
    assert detail.can_hide_or_unhide_reminders is True
    assert len(detail.orders) == 1
    order = detail.orders[0]
    assert order.id == _ORDER_ID
    assert order.name == "COMPLETE BLOOD COUNT"
    assert order.group_name == "Example Ancillary Orders"
    assert order.ordering_provider_name == "EXAMPLE PROVIDER MD"
    assert order.due_date_iso == "2026-07-01"
    assert order.has_instructions is True
    assert "Schedule a lab appointment" in (order.instructions_preview or "")

    instructions = detail.instructions_by_id[_ORDER_ID]
    assert instructions.order_id == _ORDER_ID
    assert instructions.instructions_html is not None
    assert instructions.instructions_text == (
        "Schedule a lab appointment or walk into any lab.\n\nBring your member ID."
    )


def test_parse_upcoming_orders_detail_missing_required_map_raises():
    payload = _detail_payload()
    payload.pop("orderList")
    with pytest.raises(ValueError, match="orderList"):
        _parse_upcoming_orders_detail(payload)


@pytest.mark.asyncio
async def test_fetch_upcoming_orders_posts_home_feed_then_fetches_detail():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, patched = _patch_http(
        [
            httpx.Response(200, text=_csrf_html()),
            httpx.Response(200, json=_home_feed_payload()),
            httpx.Response(200, text=_nonce_html()),
            httpx.Response(200, text=_csrf_html()),
            httpx.Response(200, json=_detail_payload()),
        ]
    )
    try:
        response = await fetch_upcoming_orders(KaiserRequest(store))
    finally:
        patched.stop()

    assert isinstance(response, UpcomingOrdersResponse)
    assert response.total_count == 1
    assert response.orders[0].id == _ORDER_ID
    assert response.orders[0].ordering_provider_name == "EXAMPLE PROVIDER MD"
    assert response.warnings == []

    assert mock_client.request.await_count == 5

    feed_csrf_call = mock_client.request.await_args_list[0]
    assert feed_csrf_call.args[0] == "GET"
    assert CSRF_PATH in feed_csrf_call.args[1]

    home_call = mock_client.request.await_args_list[1]
    assert home_call.args[0] == "POST"
    assert MIXED_ITEM_FEED_PATH in home_call.args[1]
    assert home_call.kwargs["headers"]["Referer"] == HOME_REFERER
    assert home_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF
    assert home_call.kwargs["data"] == {}

    page_call = mock_client.request.await_args_list[2]
    assert page_call.args[0] == "GET"
    assert _ORDER_ID in page_call.args[1]

    csrf_call = mock_client.request.await_args_list[3]
    assert csrf_call.args[0] == "GET"
    assert CSRF_PATH in csrf_call.args[1]

    detail_call = mock_client.request.await_args_list[4]
    assert detail_call.args[0] == "POST"
    assert DETAIL_PATH in detail_call.args[1]
    assert detail_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF
    assert detail_call.kwargs["json"] == {
        "selectedOrderID": _ORDER_ID,
        "PageNonce": _FAKE_NONCE,
    }


@pytest.mark.asyncio
async def test_fetch_upcoming_orders_empty_home_feed_returns_empty_response():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, patched = _patch_http(
        [
            httpx.Response(200, text=_csrf_html()),
            httpx.Response(200, json={"SingleItemFeedViewModels": []}),
        ]
    )
    try:
        response = await fetch_upcoming_orders(KaiserRequest(store))
    finally:
        patched.stop()

    assert response.total_count == 0
    assert response.orders == []
    assert mock_client.request.await_count == 2


@pytest.mark.asyncio
async def test_fetch_upcoming_order_instructions_returns_full_text():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    mock_client, patched = _patch_http(
        [
            httpx.Response(200, text=_nonce_html()),
            httpx.Response(200, text=_csrf_html()),
            httpx.Response(200, json=_detail_payload()),
        ]
    )
    try:
        instructions = await fetch_upcoming_order_instructions(
            KaiserRequest(store),
            _ORDER_ID,
        )
    finally:
        patched.stop()

    assert instructions is not None
    assert instructions.order_id == _ORDER_ID
    assert instructions.order_name == "COMPLETE BLOOD COUNT"
    assert "Bring your member ID." in (instructions.instructions_text or "")
    assert mock_client.request.await_count == 3


@pytest.mark.asyncio
async def test_fetch_upcoming_order_instructions_empty_id_short_circuits():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    result = await fetch_upcoming_order_instructions(KaiserRequest(store), " ")
    assert result is None
    store.get_session.assert_not_called()
