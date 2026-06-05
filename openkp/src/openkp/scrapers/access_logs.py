"""Kaiser access-log scraper.

One MCP tool surfaces from this module:

- `list_access_log` — the patient's portal or third-party access history.

Source: legacy MyChart `/mychartcn/api/access-logs/*` endpoints. Same auth +
CSRF contract as other `/mychartcn/api/` reads. Kaiser returns pages of 50
entries with a `startingLine` cursor. The cursor can repeat on the
third-party endpoint, so the walker always has a hard page cap and reports
why it stopped.

Docs: `docs/research/endpoints/access_logs.md`
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

PORTAL_PATH = "/mychartcn/api/access-logs/GetPortalAccessLogEntries"
THIRD_PARTY_PATH = "/mychartcn/api/access-logs/GetThirdPartyAccessLogEntries"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/app/access-logs?lang=en-US&from=landingpage"

AccessLogKind = Literal["portal", "third_party"]

# Defensive cap against endpoint or cursor regressions. Default public tool
# calls use 5 pages, but callers can ask for more within this ceiling.
MAX_PAGES_HARD_CAP = 20


# --- models ---


class AccessLogEntry(BaseModel):
    """One Kaiser access-log entry."""

    kind: AccessLogKind
    accessor: str | None = None
    access_time: str | None = None

    # Third-party endpoint fields.
    action: str | None = None
    access_method: int | None = None

    # Portal-self endpoint fields.
    entry_type: int | None = None
    ccd_action: int | None = None


class AccessLogResponse(BaseModel):
    """Paginated access-log response."""

    kind: AccessLogKind
    entries: list[AccessLogEntry] = Field(default_factory=list)
    total_count: int = 0
    pages_walked: int = 0
    has_more: bool = False
    next_cursor: int | None = None
    stop_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class _AccessLogPage(BaseModel):
    entries: list[AccessLogEntry] = Field(default_factory=list)
    next_cursor: int | None = None


# --- public ---


async def fetch_access_log(
    client: KaiserRequest,
    *,
    kind: str = "third_party",
    max_pages: int = 5,
) -> AccessLogResponse:
    """Fetch portal or third-party access-log entries.

    The endpoint page size is fixed by Kaiser at 50 entries in observed
    traffic. `max_pages` bounds the walker. If Kaiser returns a repeated cursor,
    the response includes `has_more=True`, `stop_reason="cursor_repeated"`, and
    a warning instead of silently claiming the log was exhausted.
    """
    normalized_kind = _normalize_kind(kind)
    if max_pages < 1:
        max_pages = 1
    if max_pages > MAX_PAGES_HARD_CAP:
        max_pages = MAX_PAGES_HARD_CAP

    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    path = THIRD_PARTY_PATH if normalized_kind == "third_party" else PORTAL_PATH

    entries: list[AccessLogEntry] = []
    warnings: list[str] = []
    cursor = -1
    next_cursor: int | None = None
    stop_reason: str | None = None
    has_more = False
    pages_walked = 0

    while pages_walked < max_pages:
        response = await client.post(
            path,
            headers=_api_headers(csrf),
            json={"startingLine": cursor},
        )
        response.raise_for_status()

        page = _parse_access_log_page(response.json(), normalized_kind)
        entries.extend(page.entries)
        pages_walked += 1
        next_cursor = page.next_cursor

        if next_cursor is None:
            stop_reason = "no_next_cursor"
            has_more = False
            break
        if not page.entries:
            stop_reason = "empty_page"
            has_more = False
            break
        if next_cursor == cursor:
            stop_reason = "cursor_repeated"
            has_more = True
            warnings.append(
                "Kaiser returned the same access-log cursor twice; results may be incomplete."
            )
            break

        cursor = next_cursor
    else:
        stop_reason = "max_pages_reached"
        has_more = next_cursor is not None

    return AccessLogResponse(
        kind=normalized_kind,
        entries=entries,
        total_count=len(entries),
        pages_walked=pages_walked,
        has_more=has_more,
        next_cursor=next_cursor,
        stop_reason=stop_reason,
        warnings=warnings,
    )


# --- private ---


def _api_headers(csrf_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": PAGE_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


def _normalize_kind(kind: str) -> AccessLogKind:
    normalized = kind.strip().lower().replace("-", "_")
    if normalized in {"third_party", "thirdparty", "apps", "app"}:
        return "third_party"
    if normalized in {"portal", "self"}:
        return "portal"
    raise ValueError('kind must be "third_party" or "portal"')


def _parse_access_log_page(payload: Any, kind: AccessLogKind) -> _AccessLogPage:
    if not isinstance(payload, dict):
        return _AccessLogPage()

    raw_entries = payload.get("entries")
    entries: list[AccessLogEntry] = []
    if isinstance(raw_entries, list):
        for raw in raw_entries:
            entry = _parse_access_log_entry(raw, kind)
            if entry is not None:
                entries.append(entry)

    return _AccessLogPage(
        entries=entries,
        next_cursor=_int_or_none(payload.get("nextLineToParse")),
    )


def _parse_access_log_entry(raw: Any, kind: AccessLogKind) -> AccessLogEntry | None:
    if not isinstance(raw, dict):
        return None

    if kind == "third_party":
        return AccessLogEntry(
            kind=kind,
            accessor=_str_or_none(raw.get("accessor")),
            access_time=_str_or_none(raw.get("accessTime")),
            action=_str_or_none(raw.get("action")),
            access_method=_int_or_none(raw.get("accessMethod")),
        )

    return AccessLogEntry(
        kind=kind,
        accessor=_str_or_none(raw.get("accessor")),
        access_time=_str_or_none(raw.get("accessTime")),
        entry_type=_int_or_none(raw.get("entryType")),
        ccd_action=_int_or_none(raw.get("ccdAction")),
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
