# Visit notes + after-visit-summary endpoints

Source: `docs/research/captures/kp-capture-notes-avs.har` (HAR, request side only — Chrome stripped most response bodies as the network panel evicted older entries) and `docs/research/captures/recon-visit-*.json` (full responses, captured 2026-05-04 by `openkp/scripts/recon_visit_notes.py`).

The visit-detail page at `/mychartcn/app/visits/past-details?csn=<CSN>` is the entry point. From there, kp.org fires a chain of XHRs to load metadata, list clinical notes, render note content, render the After Visit Summary, and (when available) prepare the AVS PDF download.

## Summary

| Endpoint | Purpose | Used by OpenKP |
| --- | --- | --- |
| `POST /mychartcn/api/visits/past-details/GetVisitDetailsPast` | Visit metadata + AVS dcsId | ✅ Used by `read_visit_notes` and `download_visit_avs_pdf`. |
| `POST /mychartcn/api/visit-notes/GetVisitNotes` | List of clinical notes for the visit | ✅ Used by `read_visit_notes`. |
| `POST /mychartcn/api/visit-notes/ValidateVisitNote` | Per-note access check before content load | ✅ Used by `read_visit_notes`. |
| `POST /mychartcn/api/report-content/LoadReportContent` (HNO) | Rendered clinical-note HTML | ✅ Used by `read_visit_notes`. |
| `POST /mychartcn/api/report-content/LoadReportContent` (AMB_AVS) | Rendered AVS HTML | ✅ Used by `read_visit_notes`. |
| `POST /mychartcn/api/documents/viewer/GetDocumentDetails` | Resolve dcsId to a download URL | ✅ Used by `download_visit_avs_pdf`. Same endpoint as labs PDFs. |
| `GET /mychartcn/Documents/ViewDocument/DownloadOrStream` | Binary AVS PDF | ✅ Used by `download_visit_avs_pdf`. Same endpoint as message attachments. |
| `POST /mychartcn/api/visits/log-avs/LogViewAfterVisitSummary` | Telemetry beacon (UI fires when user opens AVS) | ⚪ Not used. Returns 204, no functional purpose. |

MCP tools registered (v1): `read_visit_notes(csn)` and `download_visit_avs_pdf(csn)`.

## Required headers

Same anti-forgery contract as the rest of the legacy `/mychartcn/api/...` family:

```
Accept: application/json
Content-Type: application/json
Origin: https://healthy.kaiserpermanente.org
Referer: <past-details OR note URL — see below>
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <CSRF token, fetched per page from /mychartcn/Home/CSRFToken>
```

**Two distinct referers, two distinct CSRF tokens:**

- For `GetVisitDetailsPast`, `GetVisitNotes`, `LoadReportContent` (AVS variant), `GetDocumentDetails`:
  `https://healthy.kaiserpermanente.org/mychartcn/app/visits/past-details?csn=<CSN>`
- For `ValidateVisitNote` and `LoadReportContent` (HNO variant):
  `https://healthy.kaiserpermanente.org/mychartcn/app/visits/note?csn=<CSN>`

Kaiser scopes CSRF tokens by referer page. Reusing a token from the wrong page bounces the request to `/mychartcn/Home/FiveHundred`. `fetch_visit_notes` fetches one CSRF for past-details up front, and a second CSRF for the note referer only when there's at least one note to validate.

## `POST /mychartcn/api/visits/past-details/GetVisitDetailsPast`

**Request body:**

```json
{ "csn": "<CSN>", "eorgID": "" }
```

**Response shape (recon data):**

```json
{
  "encounterType": "ambulatory",
  "csn": "<CSN>",
  "dat": "WP-...",
  "externalStatus": "NotExternalVisit",
  "notesInfo": {
    "isAtLeastOneNoteShareable": true,
    "linkedAdmissionCSN": "",
    "notesReport": { "reportMnemonic": "", "reportID": "", "reportContext": "" }
  },
  "avsInfo": {
    "canShowDischargeInstr": false,
    "isDischargeInstrEnabled": true,
    "avsLiveReport": { "reportMnemonic": "", "reportID": "", "reportContext": "" },
    "avsSnapshots": [
      { "dcsID": "WP-...", /* + a few timestamp / shareability fields */ }
    ],
    "hasShareableAvs": true,
    "isAdmissionActive": false
  },
  "visitSummaryInfo": {
    "summaryType": "PastAppointment",
    "department": "Department Of Cardiology",
    "provider": "DR. EXAMPLE PROVIDER",
    "encounterDate": "Jan 01, 2025",
    "visitType": "Office Visit",
    "visitDetailsURL": ""
  },
  "externalDocUrl": "",
  "orgID": "",
  "isEncounterSensitive": false
}
```

The AVS PDF dcsId lives at `avsInfo.avsSnapshots[0].dcsID`. When `avsSnapshots` is empty (refills, walk-ins, anesthesia events without a paper AVS), the visit has no PDF and `download_visit_avs_pdf` returns `status="no_pdf_available"`.

## `POST /mychartcn/api/visit-notes/GetVisitNotes`

**Request body:** `{"CSN": "<CSN>", "FromPvdPage": true}` (note the uppercase `CSN` — different from `GetVisitDetailsPast` which uses lowercase `csn`).

**Response shape:**

```json
{
  "lrpID": "WP-...",
  "depPhoneNumber": "<area-code-formatted>",
  "isAtLeastOneNoteSensitive": false,
  "noteList": [
    {
      "hnoID": "WP-...",
      "hnoDAT": "WP-...",
      "displayName": "Progress Notes",
      "iso": "2025-01-01T10:00:00-08:00",
      "isAddendum": false,
      "provider": { /* may be empty {} */ },
      "isNoteSensitive": false
    }
  ]
}
```

Two important quirks:

- **`lrpID` is at the top level**, not per-note. It's the same value for every note on a given visit and feeds the `reportID` field of every subsequent `LoadReportContent` call.
- **`provider` is sometimes an empty `{}`** even when the note attribution is non-empty (the provider name is in the rendered note HTML instead). `_provider_name` returns `None` for empty dicts; the LLM caller can extract the provider name from the note text.

## `POST /mychartcn/api/visit-notes/ValidateVisitNote`

**Request body:**

```json
{
  "csn":         "<CSN>",
  "hnoID":       "<from noteList[i].hnoID>",
  "hnoDAT":      "<from noteList[i].hnoDAT>",
  "lrpID":       "<top-level lrpID from GetVisitNotes>",
  "fromPvdPage": true
}
```

**Response (small, ~157 bytes):**

```json
{
  "isAddendum": false,
  "success": true,
  "noteISO": "2025-01-01T10:00:00-08:00",
  "displayName": "Progress Notes",
  "isNoteSensitive": false,
  "isEncounterSensitive": false
}
```

This is an access check. We don't read the response body for routing — we just call it and proceed to `LoadReportContent`. If the call ever returns `success: false`, we'd need to handle that; not observed in any of our recon data.

## `POST /mychartcn/api/report-content/LoadReportContent`

This endpoint serves both clinical notes AND the AVS, with two distinct request shapes.

### Clinical-note variant (`contextINI: "HNO"`)

**Request body:**

```json
{
  "reportID":         "<top-level lrpID from GetVisitNotes>",
  "contextID":        "<noteList[i].hnoID>",
  "contextDAT":       "<noteList[i].hnoDAT>",
  "contextINI":       "HNO",
  "csn":              "<CSN>",
  "isFullReportPage": false,
  "uniqueClass":      "EID-1",
  "nonce":            "<random hex, regenerated per call>"
}
```

### AVS variant (`reportMnemonic: "AMB_AVS"`)

**Request body (no note IDs needed):**

```json
{
  "reportMnemonic":   "AMB_AVS",
  "reportID":         "",
  "csn":              "<CSN>",
  "isFullReportPage": false,
  "uniqueClass":      "EID-avs",
  "nonce":            "<random hex>"
}
```

`AMB_AVS` is the literal string for the ambulatory After-Visit Summary. Inpatient AVS variants probably exist (`INP_AVS`?) but we haven't observed any.

### Response shape (both variants)

```json
{
  "reportContent": "<HTML — Epic-rendered, can be 5KB to 90KB>",
  "reportCss":     "<style>...</style>",
  "baseFontSize":  0,
  "stylesheets":   ["/mychartcn/en-US/styles/report/epicbase.css?v=...", ...]
}
```

The HTML uses Epic's report-rendering classes (`.rpt`, `.pgHeaderFooter`, `.docHeader`, `.bothColumns`, `.singleColWide`, `.sectionHeader`, etc.) and embeds a `data-copy-context` attribute on the outer div carrying internal patient/encounter IDs. **`_html_to_text` strips both tags and attributes**, so the plain-text output never carries those IDs.

## `POST /mychartcn/api/documents/viewer/GetDocumentDetails`

Same endpoint shape as the labs PDF download flow.

**Request body:**

```json
{
  "dcsId":            "<from avsInfo.avsSnapshots[0].dcsID>",
  "fileExtension":    "PDF",
  "organizationId":   "",
  "useOldMobileLink": false
}
```

**Response (~763 bytes):**

```json
{
  "dcsId":                   "<echoed>",
  "token":                   "<short-lived signed blob>",
  "orgId":                   "",
  "displayName":             "After Visit Summary <Date>",
  "userFriendlyDisplayName": "",
  "legacyEncryption":        false,
  "isMobile":                false,
  "fileDescription":         "After Visit Summary",
  "allowPreview":            false,
  "downloadUrl":             "/Documents/ViewDocument/DownloadOrStream?dcsid=...&displayName=...&dcsExt=PDF",
  "previewUrl":              "",
  "mimeType":                "application/pdf"
}
```

`downloadUrl` is a relative path starting with `/Documents`. We prefix `/mychartcn` (mirrors the lab PDF flow) before the GET.

## `GET /mychartcn/Documents/ViewDocument/DownloadOrStream`

Binary endpoint, returns `application/pdf`. No body, no extra headers required beyond the session cookies.

## What we don't capture

- **`LogViewAfterVisitSummary`** — telemetry-only, returns 204. Skipped.
- **Discharge instructions** — `avsInfo.canShowDischargeInstr` and `isDischargeInstrEnabled` flags exist in the response. Inpatient stays may have a separate document set; we haven't recon'd one.
- **Inpatient AVS variants** — only `AMB_AVS` observed. Inpatient/ED visits may use a different mnemonic.
- **`isAtLeastOneNoteSensitive: true` paths** — none observed. If Kaiser blocks sensitive notes from API access, the parser may need to surface an explicit "redacted" status for the affected note.

## Capture / re-recon

To regenerate the recon JSONs:

```
.venv/bin/python openkp/scripts/recon_visit_notes.py
```

The script picks the most recent past visit that has both clinical notes and a visit summary, walks the full chain, and dumps responses to `docs/research/captures/recon-visit-*.json` (gitignored, contain PHI).
