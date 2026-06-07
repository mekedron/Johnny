"""Fetch Google Docs / Sheets / Drive content linked from a calendar event (Johnny-4da).

Calendar event descriptions are the meeting's free-form agenda; hosts
routinely paste Google Docs and Sheets URLs into them as the pre-meeting
context. The voice pipeline already merges the description verbatim into
the bot's system prompt (Johnny-ckz.3) — but the bot only sees the URL
string, not the document body, so a question like "what's on slide 3 of
the deck?" reduces it to guesswork.

This module closes that gap. Given a description string plus the calendar
account's :class:`GoogleApiClient`, it:

1. Detects Google Docs / Sheets / Drive URLs by regex
2. Fetches the latest Drive ``modifiedTime`` per linked file (cheap
   metadata call — caps blast radius even when bodies haven't changed)
3. Compares to the cached ``modifiedTime`` map; if every file still
   matches, signals "skip" so the caller can reuse the existing body
   cache without burning Docs/Sheets API quota
4. Otherwise, fetches each file's body via the appropriate API and
   returns the concatenated text capped at
   :data:`MAX_ATTACHMENT_CHARS_TOTAL` so a 500-page doc can't blow the
   prompt budget

The functions here intentionally do not touch the DB — the polling
worker stitches them together with :class:`~app.db.models.CalendarEvent`
upserts so one polling pass remains one transaction.

OAuth scope notes
-----------------
Reading Drive content requires ``drive.readonly`` (or
``drive``/``drive.file`` superset). The legacy ``DEFAULT_SCOPES`` set
only included ``calendar.readonly`` — accounts authorised before this
landed will get HTTP 403 on every Drive call. We catch that, log it
once per event, and the description URL still rides through unmodified
as plain text. Per the bead's acceptance: "Drive permission denied →
logged + the link continues to ride as plain text (no crash)."
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.google_client import GoogleApiClient

logger = logging.getLogger(__name__)


# Per the bead: cap total fetched text at ~20k chars so a long doc
# doesn't blow the pipeline's token budget. The router + answer LLMs
# already account for this via ``context_token_budget``, but a hard cap
# at the resolver layer means we never put a single attachment into a
# state where it crowds out everything else (recent transcripts,
# instructions) on its own.
MAX_ATTACHMENT_CHARS_TOTAL = 20_000

# Drive's response can include arbitrarily nested layout — we strip
# trailing whitespace per line and skip runs of empty lines so the
# concatenated body stays compact in the prompt. 8 KB per file keeps
# any one attachment under ~2k tokens before the total-cap clamps.
MAX_PER_FILE_CHARS = 8_000


# Patterns matching the canonical share URLs Google emits. We accept
# both ``/d/<id>/`` and ``/d/<id>`` (some clients drop the trailing
# slash) and the optional ``/edit`` / ``/view`` suffix. The capture
# group is the file id which the Drive APIs key on.
#
# Drive's ``/file/d/<id>/`` form covers PDFs / images / arbitrary
# uploads — we currently only extract Docs + Sheets bodies, but the
# metadata fetch is uniform so we route Drive-file links through too
# and skip body fetch on unsupported mime types.
_DOCS_URL_RE = re.compile(
    r"https?://docs\.google\.com/document/d/([a-zA-Z0-9_-]{8,})"
)
_SHEETS_URL_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]{8,})"
)
_DRIVE_FILE_URL_RE = re.compile(
    r"https?://drive\.google\.com/file/d/([a-zA-Z0-9_-]{8,})"
)


DRIVE_METADATA_URL = "https://www.googleapis.com/drive/v3/files/{file_id}"
DRIVE_EXPORT_URL = (
    "https://www.googleapis.com/drive/v3/files/{file_id}/export"
)
SHEETS_VALUES_URL = (
    "https://sheets.googleapis.com/v4/spreadsheets/{file_id}"
    "?fields=properties.title,sheets.properties.title,"
    "sheets.data.rowData.values.formattedValue"
)


DOCS_MIME = "application/vnd.google-apps.document"
SHEETS_MIME = "application/vnd.google-apps.spreadsheet"


@dataclass(frozen=True)
class _ParsedLink:
    """A Drive link discovered in the event description."""

    file_id: str
    url: str
    """Original URL as it appeared in the description (used for log lines)."""


@dataclass(frozen=True)
class _FileMeta:
    """Outcome of the cheap Drive metadata call for one file."""

    file_id: str
    name: str | None
    mime_type: str | None
    modified_time: str | None
    permission_denied: bool = False
    unavailable: bool = False
    """True when metadata fetch failed with a non-403 error (5xx, timeout,
    file not found). Distinct from ``permission_denied`` so the cache
    invalidation logic can still treat the file as "present but
    unavailable" rather than dropping it from the etag map."""


@dataclass(frozen=True)
class ResolutionOutcome:
    """What :func:`resolve_event_attachments` produced.

    * ``text`` is None when the caller should reuse the existing
      cached body (every file's ``modifiedTime`` matched the cached
      etag map). When set, the caller persists it alongside
      :attr:`etags` so subsequent calls can short-circuit.
    * ``etags`` is the freshly observed ``{file_id: modifiedTime}``
      map; the caller persists it regardless so a transient body
      fetch failure still updates the cache key for the next pass.
    * ``links_found`` is the count of Drive URLs we detected — useful
      for log lines and metrics. Empty descriptions return zero.
    * ``links_skipped`` records URLs we recognised but couldn't act
      on (permission denied, unsupported mime type, body fetch
      failure). Each entry is a human-readable reason — the
      description URL still rides through unchanged as plain text.
    """

    text: str | None
    etags: dict[str, str]
    links_found: int
    links_skipped: list[str]

    @property
    def cache_reused(self) -> bool:
        """``True`` when no fetch was needed because etags matched."""
        return self.text is None and self.links_found > 0


def extract_drive_links(description: str | None) -> list[_ParsedLink]:
    """Return every Google Docs / Sheets / Drive link in ``description``.

    Deduplicates by ``file_id`` while preserving first-seen order so the
    concatenated body reads in the same order the host pasted the
    links. A description like ``"Read https://docs.google.com/...AAA/edit
    then https://docs.google.com/...AAA/preview"`` resolves to a single
    entry even though the same file_id appeared twice.
    """
    if not description:
        return []
    seen: set[str] = set()
    out: list[_ParsedLink] = []
    for pattern in (_DOCS_URL_RE, _SHEETS_URL_RE, _DRIVE_FILE_URL_RE):
        for match in pattern.finditer(description):
            file_id = match.group(1)
            if file_id in seen:
                continue
            seen.add(file_id)
            out.append(_ParsedLink(file_id=file_id, url=match.group(0)))
    return out


def _strip_trailing_blanks(text: str) -> str:
    """Collapse runs of blank lines and trim trailing whitespace per line.

    Drive's Docs export emits a lot of trailing whitespace and double
    blank lines around headings; the prompt token budget is precious so
    we compact aggressively. Two blank lines max anywhere.
    """
    lines: list[str] = []
    blank_run = 0
    for raw in text.splitlines():
        stripped = raw.rstrip()
        if not stripped:
            blank_run += 1
            if blank_run > 2:
                continue
            lines.append("")
        else:
            blank_run = 0
            lines.append(stripped)
    return "\n".join(lines).strip()


def _clip(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    # Cut at the last sentence boundary we can find inside the budget so
    # the truncated body doesn't read as garbage. Falls back to a hard cut
    # when no boundary fits.
    head = text[:limit]
    for sep in ("\n\n", ". ", "\n"):
        idx = head.rfind(sep)
        if idx > limit // 2:
            return head[:idx].rstrip() + "\n[…truncated…]"
    return head + "\n[…truncated…]"


async def _fetch_metadata(
    client: GoogleApiClient, *, file_id: str
) -> _FileMeta:
    """Look up the file's ``name``, ``mimeType``, ``modifiedTime`` on Drive.

    The cheapest call we can make per linked file. Returns a structured
    record with ``permission_denied`` / ``unavailable`` flags set when
    the call fails so the caller can decide whether to skip the file
    silently or update the etag map.
    """
    url = DRIVE_METADATA_URL.format(file_id=file_id)
    try:
        response = await client.request(
            "GET",
            url,
            params={"fields": "id,name,mimeType,modifiedTime"},
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "calendar attachment metadata fetch failed file_id=%s: %s",
            file_id,
            exc,
        )
        return _FileMeta(
            file_id=file_id,
            name=None,
            mime_type=None,
            modified_time=None,
            unavailable=True,
        )
    if response.status_code == 403:
        return _FileMeta(
            file_id=file_id,
            name=None,
            mime_type=None,
            modified_time=None,
            permission_denied=True,
        )
    if response.status_code == 404:
        return _FileMeta(
            file_id=file_id,
            name=None,
            mime_type=None,
            modified_time=None,
            unavailable=True,
        )
    if not response.is_success:
        logger.warning(
            "calendar attachment metadata HTTP %d file_id=%s body=%r",
            response.status_code,
            file_id,
            response.text[:200],
        )
        return _FileMeta(
            file_id=file_id,
            name=None,
            mime_type=None,
            modified_time=None,
            unavailable=True,
        )
    try:
        payload = response.json()
    except ValueError:
        return _FileMeta(
            file_id=file_id,
            name=None,
            mime_type=None,
            modified_time=None,
            unavailable=True,
        )
    if not isinstance(payload, dict):
        return _FileMeta(
            file_id=file_id,
            name=None,
            mime_type=None,
            modified_time=None,
            unavailable=True,
        )
    return _FileMeta(
        file_id=file_id,
        name=str(payload.get("name")) if payload.get("name") else None,
        mime_type=(
            str(payload.get("mimeType")) if payload.get("mimeType") else None
        ),
        modified_time=(
            str(payload.get("modifiedTime"))
            if payload.get("modifiedTime")
            else None
        ),
    )


async def _fetch_doc_body(
    client: GoogleApiClient, *, file_id: str
) -> str | None:
    """Export a Google Doc as plain text via Drive's export endpoint.

    Returns ``None`` on any non-success response. Drive's ``export``
    endpoint is the canonical way to read a Docs body without negotiating
    the v1 Docs API's structured response.
    """
    url = DRIVE_EXPORT_URL.format(file_id=file_id)
    try:
        response = await client.request(
            "GET",
            url,
            params={"mimeType": "text/plain"},
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "calendar attachment Docs export failed file_id=%s: %s",
            file_id,
            exc,
        )
        return None
    if not response.is_success:
        logger.warning(
            "calendar attachment Docs export HTTP %d file_id=%s body=%r",
            response.status_code,
            file_id,
            response.text[:200],
        )
        return None
    return response.text


async def _fetch_sheet_body(
    client: GoogleApiClient, *, file_id: str
) -> str | None:
    """Read every tab of a Google Sheet via the Sheets v4 API.

    Drive's CSV export only returns the first tab; the bead requires
    multi-tab support so we go via the Sheets API. We request only the
    ``formattedValue`` field per cell so the response is bounded and
    we don't have to parse the rich-formatting payload.
    """
    url = SHEETS_VALUES_URL.format(file_id=file_id)
    try:
        response = await client.request("GET", url)
    except httpx.HTTPError as exc:
        logger.warning(
            "calendar attachment Sheets fetch failed file_id=%s: %s",
            file_id,
            exc,
        )
        return None
    if not response.is_success:
        logger.warning(
            "calendar attachment Sheets fetch HTTP %d file_id=%s body=%r",
            response.status_code,
            file_id,
            response.text[:200],
        )
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    spreadsheet_title_raw = (payload.get("properties") or {}).get("title")
    spreadsheet_title = (
        str(spreadsheet_title_raw)
        if isinstance(spreadsheet_title_raw, str)
        else None
    )
    sheets = payload.get("sheets")
    if not isinstance(sheets, list):
        return None
    parts: list[str] = []
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        props = sheet.get("properties") or {}
        tab_title = props.get("title") if isinstance(props, dict) else None
        tab_label = str(tab_title) if isinstance(tab_title, str) else "Sheet"
        rendered_rows = _render_sheet_rows(sheet.get("data"))
        if not rendered_rows:
            continue
        parts.append(f"### {tab_label}")
        parts.extend(rendered_rows)
        parts.append("")
    if not parts:
        return None
    header = (
        f"# {spreadsheet_title}\n\n" if spreadsheet_title else ""
    )
    return header + "\n".join(parts).rstrip()


def _render_sheet_rows(data: Any) -> list[str]:
    """Turn the ``sheets[i].data[].rowData`` payload into TSV-style lines.

    The Sheets v4 response nests as ``data: [{rowData: [{values: [{...}]}]}]``.
    We only request ``formattedValue`` (the display text per cell) so
    every cell collapses to a single string. Tab-separated keeps the
    output readable to both the LLM and a human eyeballing the prompt.
    """
    if not isinstance(data, list):
        return []
    rows: list[str] = []
    for data_block in data:
        if not isinstance(data_block, dict):
            continue
        row_data = data_block.get("rowData")
        if not isinstance(row_data, list):
            continue
        for row in row_data:
            if not isinstance(row, dict):
                continue
            values = row.get("values")
            if not isinstance(values, list):
                continue
            cells: list[str] = []
            for cell in values:
                if not isinstance(cell, dict):
                    cells.append("")
                    continue
                rendered = cell.get("formattedValue")
                cells.append(str(rendered) if rendered is not None else "")
            # Skip rows where every cell is empty — common in Sheets with
            # rendered-but-empty trailing rows.
            if any(c.strip() for c in cells):
                rows.append("\t".join(cells))
    return rows


async def _fetch_body(
    client: GoogleApiClient, *, meta: _FileMeta
) -> str | None:
    """Dispatch to the right body fetcher based on the file's mime type."""
    if meta.mime_type == DOCS_MIME:
        return await _fetch_doc_body(client, file_id=meta.file_id)
    if meta.mime_type == SHEETS_MIME:
        return await _fetch_sheet_body(client, file_id=meta.file_id)
    # Drive `/file/d/<id>` URLs cover arbitrary uploads (PDFs, images,
    # etc.). V1 doesn't extract text from those — surface the file name
    # only so the bot at least knows the link's there.
    return None


def _format_attachment(meta: _FileMeta, body: str | None) -> str | None:
    """Render one resolved attachment as a labelled section."""
    if body is None:
        return None
    body = _strip_trailing_blanks(body)
    if not body:
        return None
    label = meta.name or meta.file_id
    body = _clip(body, limit=MAX_PER_FILE_CHARS)
    return f"--- {label} ---\n{body}"


async def resolve_event_attachments(
    *,
    client: GoogleApiClient,
    description: str | None,
    cached_etags: dict[str, str] | None = None,
) -> ResolutionOutcome:
    """Fetch attachment bodies for every Drive link in ``description``.

    Returns a :class:`ResolutionOutcome` describing what we found. When
    every file's ``modifiedTime`` matches ``cached_etags`` the outcome's
    :attr:`ResolutionOutcome.text` is ``None`` and the caller should
    reuse the existing cached body — no Docs/Sheets API calls were made.

    ``cached_etags`` defaults to ``None`` meaning "no cache" — every link
    is treated as new and bodies are fetched. Pass the prior
    :attr:`CalendarEvent.attachments_etags` to enable the skip path.

    The function never raises for individual link failures. Network or
    permission errors per file are logged and contribute to
    :attr:`ResolutionOutcome.links_skipped`; the rest still attempt.
    """
    links = extract_drive_links(description)
    if not links:
        return ResolutionOutcome(
            text=None, etags={}, links_found=0, links_skipped=[]
        )

    # Phase 1: cheap metadata pass to compute the fresh etag map.
    metas: list[_FileMeta] = []
    skipped: list[str] = []
    for link in links:
        meta = await _fetch_metadata(client, file_id=link.file_id)
        metas.append(meta)
        if meta.permission_denied:
            skipped.append(
                f"{link.url}: permission denied (Drive scope missing or "
                f"file not shared with the calendar account)"
            )
        elif meta.unavailable:
            skipped.append(f"{link.url}: metadata fetch failed")

    fresh_etags: dict[str, str] = {
        m.file_id: _etag_value(m) for m in metas
    }

    # Phase 2: cache reuse check. Identical etag maps → caller keeps
    # cached body. The comparison is exact: any new / removed key OR
    # any changed modifiedTime triggers re-fetch.
    if cached_etags is not None and _etag_maps_equal(
        cached_etags, fresh_etags
    ):
        return ResolutionOutcome(
            text=None,
            etags=fresh_etags,
            links_found=len(links),
            links_skipped=skipped,
        )

    # Phase 3: fetch bodies for usable files. We aggregate sections in
    # the description order so the prompt reads in the order the host
    # pasted the links.
    sections: list[str] = []
    total = 0
    for meta in metas:
        if meta.permission_denied or meta.unavailable:
            continue
        if meta.mime_type not in (DOCS_MIME, SHEETS_MIME):
            # Drive files we can't extract bodies from still appear in
            # the etag map (so we don't re-fetch) but contribute no text.
            skipped.append(
                f"file_id={meta.file_id}: unsupported mime type "
                f"{meta.mime_type or 'unknown'} (not Docs / Sheets)"
            )
            continue
        body = await _fetch_body(client, meta=meta)
        section = _format_attachment(meta, body)
        if section is None:
            skipped.append(
                f"file_id={meta.file_id}: body fetch failed or empty"
            )
            continue
        # Total-cap clamp: once we cross the cap, stop adding sections
        # so the prompt budget is respected.
        if total + len(section) > MAX_ATTACHMENT_CHARS_TOTAL:
            remaining = MAX_ATTACHMENT_CHARS_TOTAL - total
            if remaining > 200:  # only worth clipping if room for the label
                sections.append(_clip(section, limit=remaining))
            skipped.append(
                f"file_id={meta.file_id}: dropped — total cap "
                f"{MAX_ATTACHMENT_CHARS_TOTAL} chars reached"
            )
            break
        sections.append(section)
        total += len(section) + 2  # +2 for the separator

    text = "\n\n".join(sections) if sections else ""
    return ResolutionOutcome(
        text=text or None,
        etags=fresh_etags,
        links_found=len(links),
        links_skipped=skipped,
    )


def _etag_value(meta: _FileMeta) -> str:
    """Map a metadata record to its etag cache value.

    Successful fetches use ``modifiedTime``; permission-denied files use
    a sentinel so the etag map round-trips identically across polling
    passes and we don't repeatedly skip-and-then-refetch a file the user
    will never grant access to.
    """
    if meta.permission_denied:
        return "permission_denied"
    if meta.unavailable:
        return "unavailable"
    return meta.modified_time or ""


def _etag_maps_equal(
    a: dict[str, str], b: dict[str, str]
) -> bool:
    """Compare two etag maps tolerating dict ordering.

    SQLAlchemy round-trips JSON dicts with whatever ordering the
    underlying driver produces — comparing on the dict objects directly
    is sensitive to insertion order on some Python versions. Normalise
    to sorted key tuples for a stable comparison.
    """
    if set(a.keys()) != set(b.keys()):
        return False
    for key in a:
        if a[key] != b[key]:
            return False
    return True


def has_drive_links(description: str | None) -> bool:
    """Cheap check before scheduling a resolution pass."""
    if not description:
        return False
    return any(
        pattern.search(description)
        for pattern in (_DOCS_URL_RE, _SHEETS_URL_RE, _DRIVE_FILE_URL_RE)
    )


def normalise_skip_log(skipped: Iterable[str]) -> str:
    """Render a list of skip reasons as a single log-friendly line."""
    return "; ".join(skipped) or "none"


__all__ = [
    "DOCS_MIME",
    "MAX_ATTACHMENT_CHARS_TOTAL",
    "MAX_PER_FILE_CHARS",
    "ResolutionOutcome",
    "SHEETS_MIME",
    "extract_drive_links",
    "has_drive_links",
    "normalise_skip_log",
    "resolve_event_attachments",
]
