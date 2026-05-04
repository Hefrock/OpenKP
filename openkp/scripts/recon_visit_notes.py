"""One-shot recon: dump visit-notes endpoint responses for inspection.

Why this exists: Chrome stripped response bodies from the HAR export
(again — same DevTools eviction we hit in sessions 9 / 14 / 15), so we
can't read the JSON shapes directly. This script reuses the persisted
Kaiser session, picks a recent past visit that has both clinical notes
and an after-visit summary, and walks the full server-side chain:

  GetVisitDetailsPast(csn)
  GetVisitNotes(CSN)
  for each note:
      ValidateVisitNote(csn, hnoID, hnoDAT, lrpID)
      LoadReportContent(reportID, contextID, contextDAT, contextINI=HNO, csn)
  LoadReportContent(reportMnemonic="AMB_AVS", csn)
  GetDocumentDetails(dcsId, fileExtension="PDF")  [if AVS doc available]

Each response is written to docs/research/captures/recon-visit-*.json.

Run from the repo root:

    .venv/bin/python openkp/scripts/recon_visit_notes.py

Outputs are PHI. Don't commit them (captures/ is gitignored).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

from openkp.config import load_config
from openkp.scrapers.appointments import fetch_past_visits
from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest
from openkp.scrapers.session import SessionStore

OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "research" / "captures"

# Two distinct referers — Kaiser scopes CSRF tokens by page.
PAST_DETAILS_REFERER_TPL = "https://healthy.kaiserpermanente.org/mychartcn/app/visits/past-details?csn={csn}"
NOTE_REFERER_TPL = "https://healthy.kaiserpermanente.org/mychartcn/app/visits/note?csn={csn}"


def _api_headers(csrf_token: str, referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


async def _save(name: str, response, *, ext: str = "json") -> None:
    out = OUT_DIR / f"recon-visit-{name}.{ext}"
    out.write_bytes(response.content)
    body_len = len(response.content)
    ct = response.headers.get("content-type", "?")
    print(
        f"{name:40} HTTP {response.status_code} "
        f"ct={ct:<40} {body_len:>7} bytes -> {out.name}"
    )


async def main() -> None:
    cfg = load_config()
    store = SessionStore(cfg.data_dir, cfg.username, cfg.password)
    client = KaiserRequest(store)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Pick a past visit with clinical-note + visit-summary flags. Walk a
    #    couple pages if needed.
    print("Looking for an eligible past visit...")
    past = await fetch_past_visits(client, max_pages=2, page_size=50)
    candidate = next(
        (
            v for v in past.visits
            if v.has_clinical_note and v.has_visit_summary
        ),
        None,
    )
    if candidate is None:
        # Fall back to anything with a visit summary
        candidate = next(
            (v for v in past.visits if v.has_visit_summary),
            None,
        )
    if candidate is None:
        print("No eligible past visit found in the most recent 2 pages. Aborting.")
        return

    csn = candidate.csn or candidate.id
    print(
        f"Target visit: {candidate.date_display} {candidate.visit_type or '<unknown>'} "
        f"(has_clinical_note={candidate.has_clinical_note}, "
        f"has_visit_summary={candidate.has_visit_summary})"
    )
    print(f"  csn (first 60): {csn[:60]}...")

    past_details_referer = PAST_DETAILS_REFERER_TPL.format(csn=csn)
    note_referer = NOTE_REFERER_TPL.format(csn=csn)

    # 2. GetVisitDetailsPast — base visit detail blob.
    csrf_pd = await fetch_csrf_token(client, referer=past_details_referer)
    r = await client.post(
        "/mychartcn/api/visits/past-details/GetVisitDetailsPast",
        headers=_api_headers(csrf_pd, past_details_referer),
        json={"csn": csn, "eorgID": ""},
    )
    await _save("01-getvisitdetailspast", r)

    # 3. GetVisitNotes — list of clinical notes for this visit.
    r = await client.post(
        "/mychartcn/api/visit-notes/GetVisitNotes",
        headers=_api_headers(csrf_pd, past_details_referer),
        json={"CSN": csn, "FromPvdPage": True},
    )
    await _save("02-getvisitnotes", r)
    try:
        notes_payload = r.json()
    except Exception:
        notes_payload = {}

    # 4. ValidateVisitNote + LoadReportContent for each note.
    # Kaiser's envelope: noteList (camelCase). lrpID is at the TOP level
    # (shared across all notes for the visit), not per-note.
    notes = []
    lrp_id = None
    if isinstance(notes_payload, dict):
        lrp_id = notes_payload.get("lrpID")
        v = notes_payload.get("noteList")
        if isinstance(v, list):
            notes = v

    print(f"\\nFound {len(notes)} note(s); top-level lrpID present: {bool(lrp_id)}")
    for i, note in enumerate(notes, start=1):
        if not isinstance(note, dict):
            continue
        hno_id = note.get("hnoID")
        hno_dat = note.get("hnoDAT")
        if not (hno_id and hno_dat and lrp_id):
            print(f"  note {i}: missing id triplet, skipping. keys={sorted(note.keys())}")
            continue

        csrf_n = await fetch_csrf_token(client, referer=note_referer)

        r = await client.post(
            "/mychartcn/api/visit-notes/ValidateVisitNote",
            headers=_api_headers(csrf_n, note_referer),
            json={
                "csn": csn,
                "hnoID": hno_id,
                "hnoDAT": hno_dat,
                "lrpID": lrp_id,
                "fromPvdPage": True,
            },
        )
        await _save(f"03-validatevisitnote-n{i}", r)

        # contextID = hnoID, contextDAT = hnoDAT in observed HAR.
        r = await client.post(
            "/mychartcn/api/report-content/LoadReportContent",
            headers=_api_headers(csrf_n, note_referer),
            json={
                "reportID": lrp_id,
                "contextID": hno_id,
                "contextDAT": hno_dat,
                "contextINI": "HNO",
                "csn": csn,
                "isFullReportPage": False,
                "uniqueClass": f"EID-{i:x}",
                "nonce": secrets.token_hex(16),
            },
        )
        await _save(f"04-loadreportcontent-hno-n{i}", r)

    # 5. LoadReportContent for the After Visit Summary (no note IDs needed).
    r = await client.post(
        "/mychartcn/api/report-content/LoadReportContent",
        headers=_api_headers(csrf_pd, past_details_referer),
        json={
            "reportMnemonic": "AMB_AVS",
            "reportID": "",
            "csn": csn,
            "isFullReportPage": False,
            "uniqueClass": "EID-avs",
            "nonce": secrets.token_hex(16),
        },
    )
    await _save("05-loadreportcontent-avs", r)

    # 6. If we can find an AVS dcsID in step 2 or 5's response, do the PDF
    #    document-details step too. Skip if we can't find one.
    dcs_id = None
    try:
        gvd_payload = json.loads(
            (OUT_DIR / "recon-visit-01-getvisitdetailspast.json").read_bytes()
        )
        # Heuristic: search the payload for any *dcsID*-shaped field.
        def _find_dcs(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if "dcs" in k.lower() and isinstance(v, str) and v.startswith("WP-"):
                        return v
                    found = _find_dcs(v)
                    if found:
                        return found
            elif isinstance(obj, list):
                for item in obj:
                    found = _find_dcs(item)
                    if found:
                        return found
            return None
        dcs_id = _find_dcs(gvd_payload)
    except Exception:
        pass

    if dcs_id:
        r = await client.post(
            "/mychartcn/api/documents/viewer/GetDocumentDetails",
            headers=_api_headers(csrf_pd, past_details_referer),
            json={
                "dcsId": dcs_id,
                "fileExtension": "PDF",
                "organizationId": "",
                "useOldMobileLink": False,
            },
        )
        await _save("06-getdocumentdetails", r)
    else:
        print("  (no dcsID found in GetVisitDetailsPast — skipping GetDocumentDetails)")


if __name__ == "__main__":
    asyncio.run(main())
