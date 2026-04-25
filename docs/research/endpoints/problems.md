# Problem list endpoints

Source: `docs/research/captures/recon-problems-summary.json` and `recon-problems-drillin.json`, captured 2026-04-25 by `openkp/scripts/recon_problems_allergies.py`.

The "Problem List" in Kaiser's UI is reached two ways:

- The **Health Summary** widget on `/mychartcn/app/health-summary` shows a condensed view.
- Clicking through to **Health Issues** at `/mychartcn/Clinical/HealthIssues` shows the dedicated page.

Both paths fire POSTs to similarly-named endpoints. We use the drill-in form because it's slightly richer.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Problem list (Health Summary widget) | `POST /mychartcn/api/HealthIssues/LoadHealthIssuesData` body `{"isHealthSummary": true}` | ⚪ Not used. Same per-item shape as the drill-in but with a `camelCase` envelope. |
| Problem list (dedicated Clinical page) | `POST /mychartcn/Clinical/HealthIssues/LoadListData?csn=undefined&ComponentNumber=4&noCache=...` body `{}` | ✅ Used by `list_problems`. |

MCP tool registered (v1): `list_problems`. Single round trip, no pagination needed (Kaiser returns the entire active problem list in one call, around 22.7KB in our recon data).

## Required headers

Same contract as `messages.py` and the rest of the legacy `/mychartcn/` family:

```
Accept: application/json
Content-Type: application/json
Origin: https://healthy.kaiserpermanente.org
Referer: https://healthy.kaiserpermanente.org/mychartcn/Clinical/HealthIssues
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <CSRF token, fetched per call from /mychartcn/Home/CSRFToken>
```

CSRF token must match the request's `Referer`. Reuse `csrf.fetch_csrf_token`.

## `POST /mychartcn/Clinical/HealthIssues/LoadListData`

**Query parameters (sent verbatim by Kaiser's front end):**

- `csn=undefined` — literal string, not a real CSN. The endpoint doesn't fail when present.
- `ComponentNumber=4` — Epic component identifier. Hardcode.
- `noCache=<random float>` — cache-buster.

**Request body:** `{}` (empty JSON object).

**Response shape (recon data, active problems):**

```json
{
  "DataList": [
    {
      "HealthIssueItem": {
        "Name": "<problem name>",
        "ID": "<Kaiser internal ID>",
        "EdgID": null,
        "FormattedDateNoted": "M/D/YYYY",
        "Organization": { /* KP-internal metadata, ignored */ },
        "UpdateInformation": null,
        "Action": 0,
        "ReferenceID": null,
        "Comments": null,
        "IsReadOnly": false,
        "TempID": null
      },
      "LocalItem": { /* identical to HealthIssueItem in every record we observed */ },
      "ExternalItems": [],
      "ExternalOrgs": [],
      "ContentLinkURL": "...",
      "ContentLinkPath": "...",
      "Target": "...",
      "HasLocalInstance": true
    }
  ],
  "DateOfBirth": "M/D/YYYY",
  "HealthIssuesUrl": "Clinical/HealthIssues",
  "HasUpdateSecurity": false,
  "HasStandAloneUpdateSecurity": false,
  "AlwaysShowSearchMore": false,
  "ShowDxrRefreshBanner": false,
  "ShowDxrBannerAction": false,
  "LoadingOrgNames": "",
  "ErrorOrgNames": "",
  "ManualOrgNames": "",
  "PreTextStringKey": "..."
}
```

## Per-item field map

The interesting fields, all on `HealthIssueItem` (== `LocalItem` in practice):

| Kaiser field | Our `Problem` field | Coercion |
| --- | --- | --- |
| `Name` | `name` | Pass through. |
| `ID` | `id` | Pass through. |
| `FormattedDateNoted` | `date_noted` | Pass through (display string `M/D/YYYY`, not zero-padded, not ISO). |
| `Action` | `action_code` | Raw int. `0` is the only value seen — likely "active." See "Action enum" below. |
| `IsReadOnly` | `is_read_only` | bool. |
| `Comments` | `comments` | Pass through. Null in every record we observed. |
| `EdgID` | (dropped) | Always null in our recon data. Likely a Care Everywhere external ID for problems imported from outside organizations. |
| `Organization` | (dropped) | KP-internal metadata, no clinical signal. |
| `UpdateInformation`, `ReferenceID`, `TempID` | (dropped) | All null. |

Top-level fields we drop:

- `DateOfBirth` — already covered by `get_profile`.
- `HealthIssuesUrl`, `PreTextStringKey`, `LoadingOrgNames`, `ErrorOrgNames`, `ManualOrgNames` — UI scaffolding.
- `HasUpdateSecurity`, `ShowDxrRefreshBanner`, etc. — UI feature flags.

We surface `total_count` derived from the parsed list length.

## Quirks

- **`HealthIssueItem` and `LocalItem` are byte-identical** for every record we observed. The two-key structure presumably exists to let Kaiser distinguish a Care Everywhere aggregate copy from the local KP-region copy when they actually differ. We pick `LocalItem` if present, fall back to `HealthIssueItem`. Don't invent a "different" record by reading both.
- **`Action` enum is partially known.** `0` for every active problem we observed. We surface as raw `action_code` int and assume `0 == active` for v1. If a `1` or other value shows up live (resolved problem? recently removed?), revisit the assumption.
- **`Comments` is always null in our recon data.** The field exists in the schema but isn't exercised against real content. The parser will pass through any string Kaiser returns.
- **Date is a display string, not ISO.** `"1/15/2024"` not `"2024-01-15"`. Single-digit months/days are not zero-padded. We pass through verbatim — the LLM caller can interpret. (Lab results do the same; we're consistent.)
- **No clinical depth.** No ICD codes, no severity, no resolved-date, no problem onset. Just name + date noted. This matches what KP shows on the Health Summary page in the UI — the "rich problem list" is a clinician-side feature, not patient-facing.

## Open questions

1. What other `Action` values exist? (Need to see a resolved problem or a recently-removed one.)
2. Does `EdgID` populate for members who use Care Everywhere across health systems? (Our recon data is all in-network, so we have no test case.)
3. Are there sub-types of problem records (e.g. "patient-reported" vs "diagnosed") that surface different field combinations? Not seen in our recon data.

## MCP tool surface

```python
@mcp.tool()
async def list_problems() -> dict:
    """List active health issues from the patient's problem list."""
```

Returns a dict shaped like `ProblemsResponse`:

```json
{
  "problems": [
    {
      "id": "...",
      "name": "...",
      "date_noted": "M/D/YYYY",
      "action_code": 0,
      "is_read_only": false,
      "comments": null
    }
  ],
  "total_count": 3
}
```

No `read_problem` tool in v1. The list response carries every field we have.
