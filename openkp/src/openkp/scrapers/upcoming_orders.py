"""Kaiser upcoming orders scraper.

Two MCP tools surface from this module:

- `list_upcoming_orders` — pending tests/procedures and summary metadata.
- `read_upcoming_order_instructions` — full prep/instruction text for one order.

Source: Epic MyChart's `/mychartcn/api/upcoming-orders/GetUpcomingOrders`.
The home feed (`/mychartcn/MixedItemFeed`) exposes opaque upcoming-order IDs;
the detail endpoint returns all order/provider/group maps for that selected
order. Instructions are embedded as HTML in `orderList[*].instructions`.

Docs: `docs/research/endpoints/upcoming_orders.md`
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

MIXED_ITEM_FEED_PATH = "/mychartcn/MixedItemFeed"
UPCOMING_PAGE_PATH = "/mychartcn/app/upcoming-orders"
DETAIL_PATH = "/mychartcn/api/upcoming-orders/GetUpcomingOrders"

HOME_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Home"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/app/upcoming-orders"

_ORDER_LINK_RE = re.compile(
    r"""(?:/mychartcn/|epichttp://)?app/upcoming-orders\?ordid=([^"'&<>\s]+)""",
    re.IGNORECASE,
)
_NONCE_RE = re.compile(r"""nonce=['"]([a-f0-9]{16,})['"]""", re.IGNORECASE)
_MULTISPACE_RE = re.compile(r"[ \t]+")
_BLOCK_TAGS = (
    "address",
    "article",
    "aside",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "ul",
)


# --- models ---


class UpcomingOrder(BaseModel):
    """One pending test/procedure order."""

    id: str
    name: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    ordering_provider_id: str | None = None
    ordering_provider_name: str | None = None

    ordered_date_iso: str | None = None
    due_date_iso: str | None = None
    actionable_date_iso: str | None = None
    actionable_date_type: str | None = None
    expected_date_iso: str | None = None
    expired_date_iso: str | None = None
    last_performed_date_iso: str | None = None

    comments: list[str] = Field(default_factory=list)
    expected_date_comment: str | None = None
    is_prn: bool = False
    is_standing: bool = False
    standing_occurrences: str | None = None
    original_standing_occurrences: str | None = None
    order_interval: str | None = None

    has_instructions: bool = False
    instructions_preview: str | None = None

    is_snoozed: bool = False
    snoozed_until_date_iso: str | None = None
    scheduling_ticket_id: str | None = None
    scheduled_visit_csn: str | None = None
    scheduled_visit_date_iso: str | None = None
    ticket_available_date_iso: str | None = None


class UpcomingOrdersResponse(BaseModel):
    """Response from `list_upcoming_orders`."""

    orders: list[UpcomingOrder] = Field(default_factory=list)
    total_count: int = 0
    group_count: int = 0
    provider_count: int = 0
    can_hide_or_unhide_reminders: bool | None = None
    selected_order_group: str | None = None
    warnings: list[str] = Field(default_factory=list)


class UpcomingOrderInstructions(BaseModel):
    """Full instructions for one pending order."""

    order_id: str
    order_name: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    ordering_provider_name: str | None = None
    ordered_date_iso: str | None = None
    due_date_iso: str | None = None
    expired_date_iso: str | None = None
    instructions_html: str | None = None
    instructions_text: str | None = None
    comments: list[str] = Field(default_factory=list)
    expected_date_comment: str | None = None


class _UpcomingOrderDetail(BaseModel):
    orders: list[UpcomingOrder] = Field(default_factory=list)
    instructions_by_id: dict[str, UpcomingOrderInstructions] = Field(default_factory=dict)
    group_count: int = 0
    provider_count: int = 0
    can_hide_or_unhide_reminders: bool | None = None
    selected_order_group: str | None = None


# --- public ---


async def fetch_upcoming_orders(client: KaiserRequest) -> UpcomingOrdersResponse:
    """Fetch pending upcoming tests/procedures.

    The home feed is used only to discover at least one opaque order id. The
    canonical order data comes from `GetUpcomingOrders`.
    """
    order_ids = await _fetch_order_ids_from_home_feed(client)
    if not order_ids:
        return UpcomingOrdersResponse()

    detail = await _fetch_upcoming_orders_detail(client, order_ids[0])
    warnings: list[str] = []
    feed_ids = set(order_ids)
    detail_ids = {order.id for order in detail.orders}
    missing_from_detail = feed_ids - detail_ids
    if missing_from_detail:
        warnings.append(
            "Kaiser home feed referenced upcoming orders that were not present "
            "in the GetUpcomingOrders response; results may be incomplete."
        )

    return UpcomingOrdersResponse(
        orders=detail.orders,
        total_count=len(detail.orders),
        group_count=detail.group_count,
        provider_count=detail.provider_count,
        can_hide_or_unhide_reminders=detail.can_hide_or_unhide_reminders,
        selected_order_group=detail.selected_order_group,
        warnings=warnings,
    )


async def fetch_upcoming_order_instructions(
    client: KaiserRequest,
    order_id: str,
) -> UpcomingOrderInstructions | None:
    """Fetch the full instructions for one upcoming order id.

    `order_id` should come from `list_upcoming_orders().orders[i].id`.
    Returns None if the response is valid but the requested order is absent.
    Raises ValueError when Kaiser omits required response maps.
    """
    cleaned = _str_or_none(order_id)
    if cleaned is None:
        return None
    detail = await _fetch_upcoming_orders_detail(client, cleaned)
    return detail.instructions_by_id.get(cleaned)


# --- network helpers ---


async def _fetch_order_ids_from_home_feed(client: KaiserRequest) -> list[str]:
    csrf = await fetch_csrf_token(client, referer=HOME_REFERER)
    response = await client.post(
        MIXED_ITEM_FEED_PATH,
        params={"noCache": f"{random.random()}"},
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://healthy.kaiserpermanente.org",
            "Referer": HOME_REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "__RequestVerificationToken": csrf,
        },
        data={},
    )
    response.raise_for_status()
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    return _extract_order_ids(payload)


async def _fetch_upcoming_orders_detail(
    client: KaiserRequest,
    selected_order_id: str,
) -> _UpcomingOrderDetail:
    referer = _detail_referer(selected_order_id)
    nonce = await _fetch_page_nonce(client, referer)
    csrf = await fetch_csrf_token(client, referer=referer)
    response = await client.post(
        DETAIL_PATH,
        headers=_api_headers(csrf, referer),
        json={"selectedOrderID": selected_order_id, "PageNonce": nonce},
    )
    response.raise_for_status()
    return _parse_upcoming_orders_detail(response.json())


async def _fetch_page_nonce(client: KaiserRequest, referer: str) -> str:
    response = await client.get(referer)
    response.raise_for_status()
    match = _NONCE_RE.search(response.text)
    if not match:
        raise ValueError("Page nonce not found in upcoming-orders HTML")
    return match.group(1)


def _api_headers(csrf_token: str, referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


def _detail_referer(order_id: str) -> str:
    return f"{PAGE_REFERER}?ordid={order_id}"


# --- parsing ---


def _extract_order_ids(value: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for match in _ORDER_LINK_RE.finditer(node):
                order_id = match.group(1)
                if order_id and order_id not in seen:
                    seen.add(order_id)
                    out.append(order_id)
            return
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
            return
        if isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return out


def _parse_upcoming_orders_detail(payload: Any) -> _UpcomingOrderDetail:
    if not isinstance(payload, dict):
        raise ValueError("Kaiser upcoming-orders response was not a JSON object")

    order_groups = _require_dict(payload, "orderGroupList")
    orders = _require_dict(payload, "orderList")
    providers = _require_dict(payload, "providerList")
    settings = payload.get("upcomingOrdersSettings")
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("Kaiser upcoming-orders response had malformed upcomingOrdersSettings")

    parsed_orders: list[UpcomingOrder] = []
    instructions_by_id: dict[str, UpcomingOrderInstructions] = {}

    for group_id, raw_group in order_groups.items():
        if not isinstance(raw_group, dict):
            continue
        order_ids = _str_list(raw_group.get("orderIDs"))
        provider = _provider_for_group(raw_group, providers)
        for order_id in order_ids:
            raw_order = orders.get(order_id)
            if not isinstance(raw_order, dict):
                continue
            parsed = _parse_order(
                order_id=order_id,
                group_id=str(group_id),
                raw_order=raw_order,
                raw_group=raw_group,
                raw_provider=provider,
            )
            parsed_orders.append(parsed)
            instructions_by_id[parsed.id] = _parse_instructions(parsed, raw_order)

    settings_dict = settings if isinstance(settings, dict) else {}
    return _UpcomingOrderDetail(
        orders=parsed_orders,
        instructions_by_id=instructions_by_id,
        group_count=len(order_groups),
        provider_count=len(providers),
        can_hide_or_unhide_reminders=_bool_or_none(
            settings_dict.get("canHideOrUnhideReminders")
        ),
        selected_order_group=_str_or_none(settings_dict.get("selectedOrderGroup")),
    )


def _parse_order(
    *,
    order_id: str,
    group_id: str,
    raw_order: dict[str, Any],
    raw_group: dict[str, Any],
    raw_provider: dict[str, Any] | None,
) -> UpcomingOrder:
    instructions_text = _html_to_text(raw_order.get("instructions"))
    return UpcomingOrder(
        id=order_id,
        name=_str_or_none(raw_order.get("name")),
        group_id=group_id,
        group_name=_str_or_none(raw_group.get("visitName")),
        ordering_provider_id=_str_or_none(raw_group.get("encProviderID")),
        ordering_provider_name=_str_or_none(raw_provider.get("name")) if raw_provider else None,
        ordered_date_iso=_str_or_none(raw_order.get("orderedDateISO")),
        due_date_iso=_str_or_none(raw_group.get("dueDateISO")),
        actionable_date_iso=_str_or_none(raw_order.get("actionableDateISO")),
        actionable_date_type=_str_or_none(raw_order.get("actionableDateType")),
        expected_date_iso=_str_or_none(raw_order.get("expectedDateISO")),
        expired_date_iso=_str_or_none(raw_order.get("expiredDateISO")),
        last_performed_date_iso=_str_or_none(raw_order.get("lastPerformedDateISO")),
        comments=_str_list(raw_order.get("comments")),
        expected_date_comment=_str_or_none(raw_order.get("expectedDateComment")),
        is_prn=bool(raw_order.get("isPRN")),
        is_standing=bool(raw_order.get("isStanding")),
        standing_occurrences=_str_or_none(raw_order.get("standingOccurrences")),
        original_standing_occurrences=_str_or_none(
            raw_order.get("originalStandingOccurrences")
        ),
        order_interval=_str_or_none(raw_order.get("orderInterval")),
        has_instructions=instructions_text is not None,
        instructions_preview=_preview(instructions_text),
        is_snoozed=bool(raw_group.get("isSnoozed")),
        snoozed_until_date_iso=_str_or_none(raw_group.get("snoozedUntilDateISO")),
        scheduling_ticket_id=_str_or_none(raw_group.get("schedulingTicketID")),
        scheduled_visit_csn=_str_or_none(raw_group.get("apptCSN")),
        scheduled_visit_date_iso=_str_or_none(raw_group.get("apptDateISO")),
        ticket_available_date_iso=_str_or_none(raw_group.get("ticketAvailableDateISO")),
    )


def _parse_instructions(
    order: UpcomingOrder,
    raw_order: dict[str, Any],
) -> UpcomingOrderInstructions:
    html = _str_or_none(raw_order.get("instructions"))
    return UpcomingOrderInstructions(
        order_id=order.id,
        order_name=order.name,
        group_id=order.group_id,
        group_name=order.group_name,
        ordering_provider_name=order.ordering_provider_name,
        ordered_date_iso=order.ordered_date_iso,
        due_date_iso=order.due_date_iso,
        expired_date_iso=order.expired_date_iso,
        instructions_html=html,
        instructions_text=_html_to_text(html),
        comments=order.comments,
        expected_date_comment=order.expected_date_comment,
    )


def _provider_for_group(
    raw_group: dict[str, Any],
    providers: dict[str, Any],
) -> dict[str, Any] | None:
    provider_id = _str_or_none(raw_group.get("encProviderID"))
    if provider_id is None:
        return None
    provider = providers.get(provider_id)
    return provider if isinstance(provider, dict) else None


def _require_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Kaiser upcoming-orders response missing {key}")
    return value


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = _str_or_none(item)
        if s is not None:
            out.append(s)
    return out


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _preview(text: str | None, limit: int = 240) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _html_to_text(html: Any) -> str | None:
    """Strip Epic-rendered instructions HTML to plain text."""
    if not isinstance(html, str) or not html.strip():
        return None
    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before("\n\n")
    text = soup.get_text(separator=" ", strip=False)

    lines = [_MULTISPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    prev_blank = False
    for line in lines:
        blank = not line
        if blank and prev_blank:
            continue
        out.append(line)
        prev_blank = blank
    return "\n".join(out).strip() or None
