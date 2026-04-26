"""Messaging scraper: list threads + read a single thread.

Kaiser's Message Center is Epic MyChart under the hood. The two endpoints we
use both live at `/mychartcn/api/conversations/*` and require a CSP nonce
extracted from the communication-center HTML page as a `PageNonce` field in
the JSON body.

Flow:

  1. GET /mychartcn/app/communication-center         -> HTML page with <style nonce="...">
  2. POST /mychartcn/api/conversations/GetConversationList {tag, searchQuery, ...}
     OR
     POST /mychartcn/api/conversations/GetConversationDetails {id, ...}

Response bodies are JSON. Message bodies themselves are HTML — we strip to
plain text via bs4 so Claude gets clean input.

PHI discipline: never log message bodies, subjects, or sender names. Never
put real content in test fixtures.

Docs: `docs/research/endpoints/messages.md`
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

# Page that hosts the CSP nonce we need to unlock the JSON APIs.
PAGE_PATH = "/mychartcn/app/communication-center"
LIST_PATH = "/mychartcn/api/conversations/GetConversationList"
DETAILS_PATH = "/mychartcn/api/conversations/GetConversationDetails"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/app/communication-center"

# Attachment-download chain. `Legacy` variant is what the message-center UI
# uses — distinct from the `GetDocumentDetails` that lab-result PDFs hit.
# Message attachments are static files (no on-demand generation step).
DOCDETAILS_LEGACY_PATH = "/mychartcn/api/documents/viewer/GetDocumentDetailsLegacy"

# Where attachment binaries land. Same directory as lab PDFs — fewer surprises
# for callers that handle both, and the displayName disambiguates.
DEFAULT_DOWNLOAD_DIR = Path.home() / ".openkp" / "downloads"

# Characters unsafe for a filesystem path across the common cases.
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Folder name → Kaiser integer tag. Observed empirically in
# `docs/research/captures/kp-messages-2.har` GetFoldersList response, matched
# against the sidebar labels in the UI.
FOLDER_TAGS: dict[str, int] = {
    "inbox": 1,          # Kaiser calls this "Conversations"
    "archive": 2,
    "bookmarked": 3,
    "automated": 6,      # "Automated messages"
    "appointments": 7,
}

# Kaiser returns at most 50 conversations per GetConversationList call.
MAX_PAGE_SIZE = 50

# Matches `nonce='...'` or `nonce="..."` attributes in the page HTML. The
# values are 32-char hex strings. We scope to 16+ chars for safety.
_NONCE_RE = re.compile(r"""nonce=['"]([a-f0-9]{16,})['"]""", re.IGNORECASE)


# --- models ---


class Attachment(BaseModel):
    """A file attached to a message.

    `dcs_id` is Kaiser's opaque document handle. Pass it to
    `download_message_attachment` to fetch the binary.
    """

    name: str | None = None
    file_extension: str | None = None
    dcs_id: str | None = None
    attachment_type: int | None = None
    organization_id: str | None = None


class Author(BaseModel):
    """Sender of a single message."""

    name: str | None = None


class Message(BaseModel):
    """One message within a thread."""

    id: str
    sent_at: str | None = None         # ISO timestamp
    is_unread: bool = False
    author: Author | None = None
    body_text: str | None = None       # HTML-stripped plain text
    attachments: list[Attachment] = Field(default_factory=list)


class MessageThread(BaseModel):
    """Summary of one conversation thread — used in list views."""

    id: str                            # Kaiser's `hthId`
    subject: str | None = None
    preview: str | None = None         # Short server-provided preview
    last_sender: str | None = None     # Display name resolved from user maps
    last_message_at: str | None = None
    is_unread: bool = False
    has_attachments: bool = False
    has_tasks: bool = False
    has_urgent: bool = False
    total_messages: int = 1
    folder_tag: int | None = None      # Which folder this was listed from
    organization_id: str | None = None


class MessageThreadDetail(MessageThread):
    """A full thread: all metadata plus every message's body."""

    can_reply: bool = False
    messages: list[Message] = Field(default_factory=list)


class MessageAttachmentDownload(BaseModel):
    """Outcome of a `download_message_attachment` call.

    Status values:
      - "downloaded" — file is on disk, see `path`.
      - "error"      — transport, IO, or missing-downloadUrl failure;
                       `reason` explains.

    Unlike lab PDFs, message attachments are static files that Kaiser stores
    once and serves directly — there is no `generation_in_progress` state.
    """

    status: str
    path: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    reason: str | None = None


# --- fetchers ---


async def fetch_messages(
    client: KaiserRequest,
    folder: str = "inbox",
    search: str | None = None,
    before_iso: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    deep_search: bool = False,
    max_pages: int = 30,
) -> list[MessageThread]:
    """List message threads in one folder.

    Single-page mode (default): one round trip to GetConversationList. Kaiser
    returns up to 50 threads. For older pages, pass `before_iso` as the cursor.

    Deep-search mode (`deep_search=True`): walk pagination using the
    `oldestSearchedInstantISO` cursor Kaiser returns in `localSummary`. Stops
    when Kaiser reports no more conversations, when the cursor stops
    advancing, or when `max_pages` is hit. Use this when searching for older
    threads — Kaiser's `searchQuery` only matches within the loaded page, so
    a single-page search misses anything older than the most recent ~50
    threads. Results are deduped by thread id.

    Args:
      folder: One of `FOLDER_TAGS` keys. Defaults to "inbox".
      search: Optional search string. Kaiser searches subject, body, sender.
      before_iso: Cursor for pagination. Empty = newest page. In deep-search
        mode, this is the starting cursor (default = newest).
      limit: Max threads to return. Clamped to 50. Ignored in deep-search
        mode (use `max_pages` to bound that walk instead).
      deep_search: If True, walk pagination across the full message history.
      max_pages: Hard cap on pages walked in deep-search mode. Default 30
        (≈ 1500 threads worth of history). Ignored in single-page mode.

    Returns an empty list if the folder is unknown or the response is malformed.
    """
    tag = FOLDER_TAGS.get(folder.lower())
    if tag is None:
        logger.warning("Unknown folder %r; valid: %s", folder, sorted(FOLDER_TAGS))
        return []

    # Same nonce + CSRF reused across every page in a deep walk — Kaiser's UI
    # does the same. Each call would otherwise spend two extra round trips.
    nonce = await _fetch_page_nonce(client)
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)

    if not deep_search:
        threads, _ = await _fetch_message_page(
            client, tag=tag, search=search, before_iso=before_iso, csrf=csrf, nonce=nonce,
        )
        clamped = max(1, min(limit, MAX_PAGE_SIZE))
        return threads[:clamped]

    seen_ids: set[str] = set()
    merged: list[MessageThread] = []
    cursor = before_iso or ""
    pages = max(1, max_pages)
    for _ in range(pages):
        threads, summary = await _fetch_message_page(
            client, tag=tag, search=search, before_iso=cursor, csrf=csrf, nonce=nonce,
        )
        for t in threads:
            if t.id and t.id not in seen_ids:
                seen_ids.add(t.id)
                merged.append(t)
        if not summary.get("hasMoreConversations"):
            break
        next_cursor = _str_or_none(summary.get("oldestSearchedInstantISO")) or ""
        # Guard against a malformed response that would loop forever.
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return merged


async def _fetch_message_page(
    client: KaiserRequest,
    *,
    tag: int,
    search: str | None,
    before_iso: str | None,
    csrf: str,
    nonce: str,
) -> tuple[list[MessageThread], dict[str, Any]]:
    """One GetConversationList POST. Returns (threads, localSummary).

    `localSummary` carries the deep-search contract:
      - `hasMoreConversations` (bool) — should we keep paginating?
      - `oldestSearchedInstantISO` — cursor for the next page (advances even
        when the current page returns zero matches).
    """
    payload = {
        "tag": tag,
        "localLoadParams": {
            "loadStartInstantISO": before_iso or "",
            "loadEndInstantISO": "",
            "pagingInfo": 1,
        },
        "externalLoadParams": {},
        "searchQuery": search or "",
        "PageNonce": nonce,
    }
    response = await client.post(LIST_PATH, headers=_api_headers(csrf), json=payload)
    response.raise_for_status()
    body = response.json() if response.content else {}
    threads = _parse_conversation_list(body, folder_tag=tag)
    summary_raw = body.get("localSummary") if isinstance(body, dict) else None
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    return threads, summary


async def fetch_message(client: KaiserRequest, thread_id: str) -> MessageThreadDetail | None:
    """Fetch a full thread by its id (the `id` field from `MessageThread`).

    Returns `None` if the thread can't be found or the response is malformed.
    Raises on HTTP errors.
    """
    if not thread_id:
        return None

    nonce = await _fetch_page_nonce(client)
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    payload = {
        "id": thread_id,
        "messageId": "",
        "organizationId": "",
        "PageNonce": nonce,
    }
    response = await client.post(DETAILS_PATH, headers=_api_headers(csrf), json=payload)
    response.raise_for_status()
    return _parse_conversation_details(response.json())


async def download_message_attachment(
    client: KaiserRequest,
    dcs_id: str,
    file_extension: str = "PDF",
    display_name: str | None = None,
    organization_id: str = "",
    download_dir: Path | None = None,
) -> MessageAttachmentDownload:
    """Save a message attachment binary to disk.

    Two-hop chain:
      1. POST GetDocumentDetailsLegacy(dcsId) → downloadUrl
      2. GET <downloadUrl> → binary bytes, saved to disk

    Args:
      dcs_id: The `dcs_id` field from a `read_message` attachment.
      file_extension: Kaiser's extension marker (e.g. "PDF", "JPG"). Pass
        through from the attachment metadata.
      display_name: Optional override for the saved filename. If omitted, we
        use Kaiser's `displayName` from GetDocumentDetailsLegacy.
      organization_id: Cross-region attachment marker. Default empty matches
        the same-region case.
      download_dir: Override the default `~/.openkp/downloads/` directory.

    Returns a `MessageAttachmentDownload` with `status='downloaded'` on
    success, or `status='error'` with a short `reason` if anything fails
    short of a raised HTTP error.
    """
    if not dcs_id:
        return MessageAttachmentDownload(status="error", reason="dcs_id is empty")

    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)

    det_response = await client.post(
        DOCDETAILS_LEGACY_PATH,
        headers=_api_headers(csrf),
        json={
            "dcsId": dcs_id,
            "fileExtension": file_extension,
            "organizationId": organization_id,
            "useOldMobileLink": False,
        },
    )
    det_response.raise_for_status()
    det = det_response.json() if det_response.content else {}
    download_url = _str_or_none(det.get("downloadUrl"))
    if not download_url:
        return MessageAttachmentDownload(
            status="error",
            reason="no downloadUrl in GetDocumentDetailsLegacy response",
        )
    name_for_file = display_name or _str_or_none(det.get("displayName")) or dcs_id

    # Kaiser returns downloadUrl as a relative path that omits the /mychartcn
    # prefix. Match the labs scraper's behavior: prepend it if missing.
    path = download_url if download_url.startswith("/mychartcn") else f"/mychartcn{download_url}"
    bin_response = await client.get(
        path,
        headers={"Accept": "application/pdf,*/*", "Referer": PAGE_REFERER},
    )
    bin_response.raise_for_status()
    body = bin_response.content
    if not body:
        return MessageAttachmentDownload(status="error", reason="empty response body")

    out_dir = download_dir or DEFAULT_DOWNLOAD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(name_for_file)
    suffix = f".{file_extension.lower().lstrip('.')}" if file_extension else ""
    if suffix and not safe.lower().endswith(suffix):
        safe += suffix
    out_path = out_dir / safe
    out_path.write_bytes(body)

    return MessageAttachmentDownload(
        status="downloaded",
        path=str(out_path),
        filename=safe,
        size_bytes=len(body),
    )


# --- private helpers ---


async def _fetch_page_nonce(client: KaiserRequest) -> str:
    """Fetch the communication-center HTML and extract the CSP nonce.

    Raises `ValueError` if the page doesn't contain a `nonce=...` attribute
    we recognize. In practice this would mean the page layout changed.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "X-Requested-With": "XMLHttpRequest",
    }
    response = await client.get(PAGE_PATH, headers=headers)
    response.raise_for_status()
    match = _NONCE_RE.search(response.text)
    if not match:
        raise ValueError("Page nonce not found in communication-center HTML")
    return match.group(1)


def _api_headers(csrf_token: str) -> dict[str, str]:
    """Shared headers for the /api/conversations/* POST calls.

    Kaiser's ASP.NET anti-forgery middleware 500s the request (caught by the
    /mychartcn/Home/FiveHundred error page redirect) without the token.
    """
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": PAGE_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


def _parse_conversation_list(payload: Any, *, folder_tag: int | None) -> list[MessageThread]:
    """Walk a GetConversationList response, produce `MessageThread` summaries."""
    if not isinstance(payload, dict):
        return []
    convs = payload.get("conversations")
    if not isinstance(convs, list):
        return []

    users = payload.get("users") if isinstance(payload.get("users"), dict) else {}
    viewers = payload.get("viewers") if isinstance(payload.get("viewers"), dict) else {}

    out: list[MessageThread] = []
    for conv in convs:
        if not isinstance(conv, dict):
            continue
        thread = _parse_thread_summary(
            conv,
            users=users,
            viewers=viewers,
            folder_tag=folder_tag,
        )
        if thread is not None:
            out.append(thread)
    return out


def _parse_conversation_details(payload: Any) -> MessageThreadDetail | None:
    """Walk a GetConversationDetails response, produce a full thread."""
    if not isinstance(payload, dict):
        return None
    thread_id = _str_or_none(payload.get("hthId"))
    if thread_id is None:
        return None

    users = payload.get("users") if isinstance(payload.get("users"), dict) else {}
    viewers = payload.get("viewers") if isinstance(payload.get("viewers"), dict) else {}
    overrides = payload.get("userOverrideNames") if isinstance(payload.get("userOverrideNames"), dict) else {}

    summary = _parse_thread_summary(payload, users=users, viewers=viewers, folder_tag=None)
    if summary is None:
        return None

    can_reply = False
    reply_flags = payload.get("replyFlags")
    if isinstance(reply_flags, dict):
        can_reply = bool(reply_flags.get("canReply"))

    messages_raw = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    messages = [
        m for m in (_parse_message(raw, users=users, viewers=viewers, overrides=overrides) for raw in messages_raw)
        if m is not None
    ]

    total = payload.get("totalMessages")
    total_int = total if isinstance(total, int) else len(messages) or 1

    return MessageThreadDetail(
        **summary.model_dump(),
        can_reply=can_reply,
        messages=messages,
    ).model_copy(update={"total_messages": total_int})


def _parse_thread_summary(
    conv: dict[str, Any],
    *,
    users: dict[str, Any],
    viewers: dict[str, Any],
    folder_tag: int | None,
) -> MessageThread | None:
    """Pull a `MessageThread` summary out of one conversation dict."""
    thread_id = _str_or_none(conv.get("hthId"))
    if thread_id is None:
        return None

    overrides = conv.get("userOverrideNames") if isinstance(conv.get("userOverrideNames"), dict) else {}

    tags = conv.get("tags")
    tag_set = set(tags.keys()) if isinstance(tags, dict) else set()

    # Last message timestamp + last sender come from the most recent message
    # Kaiser inlines into the conversation summary.
    last_sender = None
    last_message_at = None
    messages_raw = conv.get("messages")
    if isinstance(messages_raw, list) and messages_raw:
        latest = messages_raw[0] if isinstance(messages_raw[0], dict) else {}
        last_message_at = _str_or_none(latest.get("deliveryInstantISO"))
        author = latest.get("author") if isinstance(latest.get("author"), dict) else {}
        # Authors in the inline message carry a direct displayName, which is
        # the most reliable source. Fall back to user-key resolution.
        last_sender = _str_or_none(author.get("displayName"))
        if last_sender is None:
            user_keys = conv.get("userKeys")
            if isinstance(user_keys, list) and user_keys:
                last_sender = _resolve_display_name(
                    _str_or_none(user_keys[0]),
                    users=users,
                    viewers=viewers,
                    overrides=overrides,
                )

    total_messages = 1
    if isinstance(messages_raw, list):
        total_messages = max(len(messages_raw), 1)

    return MessageThread(
        id=thread_id,
        subject=_str_or_none(conv.get("subject")),
        preview=_str_or_none(conv.get("previewText")),
        last_sender=last_sender,
        last_message_at=last_message_at,
        is_unread="Unread" in tag_set,
        has_attachments=bool(conv.get("hasAttachments")),
        has_tasks=bool(conv.get("hasTasks")),
        has_urgent=bool(conv.get("hasUrgentMsgs")),
        total_messages=total_messages,
        folder_tag=folder_tag,
        organization_id=_str_or_none(conv.get("organizationId")),
    )


def _parse_message(
    raw: Any,
    *,
    users: dict[str, Any],
    viewers: dict[str, Any],
    overrides: dict[str, Any],
) -> Message | None:
    """One message dict → `Message` model (with body HTML stripped)."""
    if not isinstance(raw, dict):
        return None
    msg_id = _str_or_none(raw.get("wmgId"))
    if msg_id is None:
        return None

    author_name = None
    author_raw = raw.get("author")
    if isinstance(author_raw, dict):
        author_name = _str_or_none(author_raw.get("displayName"))
        if author_name is None:
            author_name = _resolve_display_name(
                _str_or_none(author_raw.get("empKey")),
                users=users,
                viewers=viewers,
                overrides=overrides,
            )

    return Message(
        id=msg_id,
        sent_at=_str_or_none(raw.get("deliveryInstantISO")),
        is_unread=bool(raw.get("isUnread")),
        author=Author(name=author_name) if author_name else None,
        body_text=_html_to_text(raw.get("body")),
        attachments=_parse_attachments(raw.get("attachments")),
    )


def _parse_attachments(raw: Any) -> list[Attachment]:
    if not isinstance(raw, list):
        return []
    out: list[Attachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _str_or_none(item.get("name"))
        ext = _str_or_none(item.get("fileExtension"))
        dcs_id = _str_or_none(item.get("dcsId"))
        if not name and not ext and not dcs_id:
            continue
        att_type = item.get("type")
        out.append(
            Attachment(
                name=name,
                file_extension=ext,
                dcs_id=dcs_id,
                attachment_type=att_type if isinstance(att_type, int) else None,
                organization_id=_str_or_none(item.get("organizationId")),
            )
        )
    return out


def _resolve_display_name(
    key: str | None,
    *,
    users: dict[str, Any],
    viewers: dict[str, Any],
    overrides: dict[str, Any],
) -> str | None:
    """Map an obfuscated Kaiser user key to a display name.

    Priority order:
      1. `userOverrideNames[key]` — already a plain string.
      2. `users[key].name` — provider/staff side.
      3. `viewers[key].name` — patient/viewer side.
    """
    if not key:
        return None
    if isinstance(overrides, dict):
        override = overrides.get(key)
        if isinstance(override, str):
            name = _str_or_none(override)
            if name:
                return name
    for pool in (users, viewers):
        if not isinstance(pool, dict):
            continue
        entry = pool.get(key)
        if isinstance(entry, dict):
            name = _str_or_none(entry.get("name")) or _str_or_none(entry.get("displayName"))
            if name:
                return name
    return None


_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre")
_MULTISPACE_RE = re.compile(r" +")


def _html_to_text(html: Any) -> str | None:
    """Strip HTML to plain text, preserving paragraph and line breaks.

    Strategy:
      1. Insert a blank line before each block-level tag so paragraphs stay
         visually separated in the output.
      2. Convert `<br>` to a newline.
      3. Use `get_text(separator=" ")` so inline text nodes stay cohesive
         within a paragraph.
      4. Collapse runs of spaces and blank lines for readability.
    """
    if not isinstance(html, str) or not html.strip():
        return None
    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before("\n\n")
    text = soup.get_text(separator=" ", strip=False)

    # Per-line: collapse runs of spaces, strip.
    lines = [_MULTISPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    # Collapse runs of blank lines down to a single blank.
    out: list[str] = []
    prev_blank = False
    for line in lines:
        if line:
            out.append(line)
            prev_blank = False
        elif not prev_blank:
            out.append("")
            prev_blank = True
    return "\n".join(out).strip() or None


def _safe_filename(name: str) -> str:
    """Produce a filesystem-safe name for a downloaded attachment.

    Replaces path separators, control chars, and Windows-reserved chars with
    underscores. Trims whitespace. Caps length at 180 to stay well under
    common filesystem limits when combined with the download directory path.
    """
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rstrip()
    return cleaned or "attachment"


def _str_or_none(value: Any) -> str | None:
    """Coerce to stripped string, or None if empty / missing."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None
