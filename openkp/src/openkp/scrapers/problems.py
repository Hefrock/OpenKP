"""Problem list scraper.

One MCP tool surfaces from this module:

- `list_problems` — the patient's active health-issues list (name, date noted,
  active/read-only flags). What KP shows on the Health Summary "Problem List"
  widget and the dedicated /Clinical/HealthIssues page.

Source: legacy MyChart `/mychartcn/Clinical/HealthIssues/LoadListData` endpoint.
Same auth + CSRF contract as `messages.py`. No pagination — Kaiser returns
the entire active list in one call.

Docs: `docs/research/endpoints/problems.md`
"""

from __future__ import annotations

import logging
import random
from typing import Any

from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

LIST_PATH = "/mychartcn/Clinical/HealthIssues/LoadListData"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Clinical/HealthIssues"

# Epic component identifier observed in the captured request. Hardcoded by
# Kaiser's front end — value is stable.
COMPONENT_NUMBER = "4"

# Action enum: only `0` observed live (every active problem in our recon
# data). We treat 0 as "active" but expose the raw int so callers can see
# if Kaiser starts returning other values for resolved/removed problems.
ACTION_ACTIVE = 0


# --- models ---


class Problem(BaseModel):
    """One entry on the patient's problem list."""

    id: str
    name: str | None = None
    date_noted: str | None = None       # Kaiser display string, e.g. "1/15/2024"
    action_code: int | None = None      # 0 == active (all observed); enum partially known
    is_read_only: bool = False
    comments: str | None = None         # Free text from clinician; null in observed data


class ProblemsResponse(BaseModel):
    """Wrapper around the problem list with summary count."""

    problems: list[Problem] = Field(default_factory=list)
    total_count: int = 0


# --- public ---


async def fetch_problems(client: KaiserRequest) -> ProblemsResponse:
    """Fetch the patient's active problem list. One round trip + one CSRF fetch.

    Returns a `ProblemsResponse`. An empty `problems` list is a valid outcome
    (patient has no active problems). Per ADR-005, never raise on missing
    fields — return whatever we can parse, leave the rest as null.
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    params = {
        "csn": "undefined",
        "ComponentNumber": COMPONENT_NUMBER,
        "noCache": f"{random.random()}",
    }
    response = await client.post(
        LIST_PATH,
        params=params,
        headers=_api_headers(csrf),
        json={},
    )
    response.raise_for_status()
    return _parse_problems_response(response.json())


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


def _parse_problems_response(payload: Any) -> ProblemsResponse:
    """Walk the LoadListData response, produce a `ProblemsResponse`."""
    if not isinstance(payload, dict):
        return ProblemsResponse()

    raw_list = payload.get("DataList")
    if not isinstance(raw_list, list):
        return ProblemsResponse()

    problems: list[Problem] = []
    for entry in raw_list:
        problem = _parse_problem(entry)
        if problem is not None:
            problems.append(problem)

    return ProblemsResponse(problems=problems, total_count=len(problems))


def _parse_problem(entry: Any) -> Problem | None:
    """One DataList entry → `Problem`.

    Each entry wraps the actual problem in a `LocalItem` and `HealthIssueItem`
    pair. In all observed live data they're byte-identical. We prefer
    `LocalItem`, fall back to `HealthIssueItem`, fall back to the entry
    itself if neither wrapper is present (defensive — not observed but
    cheap to handle).
    """
    if not isinstance(entry, dict):
        return None

    item = entry.get("LocalItem")
    if not isinstance(item, dict):
        item = entry.get("HealthIssueItem")
    if not isinstance(item, dict):
        item = entry

    problem_id = _str_or_none(item.get("ID"))
    if problem_id is None:
        return None

    return Problem(
        id=problem_id,
        name=_str_or_none(item.get("Name")),
        date_noted=_str_or_none(item.get("FormattedDateNoted")),
        action_code=_int_or_none(item.get("Action")),
        is_read_only=bool(item.get("IsReadOnly")),
        comments=_str_or_none(item.get("Comments")),
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        # bools are ints in Python; we don't want True → 1 here.
        return None
    if isinstance(value, int):
        return value
    return None
