# Allergy list endpoints

Source: `docs/research/captures/recon-allergies-summary.json` and `recon-allergies-drillin.json`, captured 2026-04-25 by `openkp/scripts/recon_problems_allergies.py`.

The "Allergies" page parallels Health Issues structurally — same envelope shape, same widget vs drill-in dual API, same CSRF + Referer pattern.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Allergy list (Health Summary widget) | `POST /mychartcn/api/allergies/LoadAllergies` body `{"isHealthSummary": true}` | ⚪ Not used. About 297 bytes when there are no recorded allergies. Essentially empty. |
| Allergy list (dedicated Clinical page) | `POST /mychartcn/Clinical/Allergies/LoadListData?csn=undefined&ComponentNumber=4&noCache=...` body `{}` | ✅ Used by `list_allergies`. Carries the `ReactionList` dropdown and the `AllergiesStatus` int even when `DataList` is empty. |

MCP tool registered (v1): `list_allergies`. Single round trip.

## Required headers

Same as `problems.md` — generic legacy MyChart contract:

```
Accept: application/json
Content-Type: application/json
Origin: https://healthy.kaiserpermanente.org
Referer: https://healthy.kaiserpermanente.org/mychartcn/Clinical/Allergies
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <CSRF, fetched per call>
```

## `POST /mychartcn/Clinical/Allergies/LoadListData`

**Query parameters:** same `csn=undefined&ComponentNumber=4&noCache=...` shape as the problems endpoint. Hardcode `ComponentNumber=4`.

**Request body:** `{}`.

**Response shape (recon data, no known allergies):**

```json
{
  "DataList": [],
  "ReactionList": [
    {
      "Value": "3",
      "Title": "Asthma and/or Shortness of Breath",
      "Number": null,
      "Abbreviation": null,
      "Abbr": null,
      "Comment": null,
      "IsInactive": false,
      "TitleUtf8": null,
      "AbbreviationUtf8": null
    }
    /* ... 59 entries total, the master list of reaction types Kaiser offers ... */
  ],
  "DateOfBirth": "M/D/YYYY",
  "AllergiesUrl": "Clinical/Allergies",
  "AllergiesStatus": 0,
  "HasUpdateSecurity": false,
  "HasStandAloneUpdateSecurity": false,
  "ShowDxrRefreshBanner": false,
  "ShowDxrBannerAction": false,
  "LoadingOrgNames": "",
  "ErrorOrgNames": "",
  "ManualOrgNames": "",
  "PreTextStringKey": "..."
}
```

## Per-item field map (inferred — no live example)

**Caveat:** the test patient has no recorded allergies, so `DataList` is empty and we can't observe the per-item shape. We assume parity with `problems.md` based on identical envelope structure (same top-level scaffolding, same drill-in URL pattern, same CSRF contract). If the real shape diverges, the parser tolerates missing/null fields per ADR-005 and we update the model when we see a populated allergy.

Likely fields (based on Epic MyChart conventions and the problems-endpoint mirror):

| Inferred Kaiser field | Our `Allergy` field | Coercion |
| --- | --- | --- |
| `AllergyItem.Name` (or `LocalItem.Name`) | `name` | Pass through. |
| `AllergyItem.ID` | `id` | Pass through. |
| `AllergyItem.FormattedDateNoted` | `date_noted` | Pass through (display string). |
| `AllergyItem.Action` | `action_code` | Raw int. |
| `AllergyItem.IsReadOnly` | `is_read_only` | bool. |
| `AllergyItem.Comments` | `comments` | Pass through. |
| `AllergyItem.Reactions` (list of reaction strings) | `reactions` | List of str — **highly likely** based on `ReactionList` existing as a dropdown menu. |
| `AllergyItem.Severity` | `severity` | Pass through. **Speculative.** |

**`ReactionList` is NOT patient data.** It's the master list of 59 reaction types Kaiser offers in the "Add an allergy → Reactions" dropdown (e.g. "Asthma and/or Shortness of Breath", "Hives", "Anaphylaxis"). Useful as a static reference but should NOT appear in our tool's primary output. We drop it unless an explicit need arises.

## Top-level fields

| Kaiser field | Our `AllergiesResponse` field | Notes |
| --- | --- | --- |
| `DataList` | `allergies` | Parsed array. Empty list = "no recorded allergies." |
| `AllergiesStatus` | `status_code` | Raw int. `0` observed for "no known allergies" patient. Enum unknown otherwise. |
| (derived) | `status` | Human-readable interpretation: `"no_known_allergies"` when `DataList` is empty AND `status_code == 0`; otherwise `"recorded"`. |
| (derived) | `total_count` | `len(allergies)`. |

Dropped:

- `DateOfBirth` — already covered by `get_profile`.
- `ReactionList` — dropdown options, not patient data.
- All UI scaffolding (`AllergiesUrl`, `PreTextStringKey`, etc.).

## Quirks

- **The most common live state is empty.** "No known allergies" is the typical adult patient state. `DataList: []` is a valid, safe outcome — not an error.
- **`AllergiesStatus: 0` is observed only for the empty-list case** in our recon. Whether `0` always means "no known allergies" or whether populated lists also use `0`, we don't know. Surfacing as raw int + derived label keeps the truth explicit.
- **The drill-in carries 9.8KB even with zero allergies** — almost all of that is the 59-entry `ReactionList`. The summary endpoint returns 297 bytes for the same patient.
- **No live verification of the populated-list path.** Tests use a synthetic fixture modeled on the problems shape. First time a real allergy lands in the record, we should re-run recon and confirm field names.

## Open questions

1. What does `DataList[i]` look like when populated? (Top question — blocks confident field naming.)
2. Does `AllergiesStatus` shift to a different value when allergies are present, or does it stay `0`?
3. Are reactions stored on each allergy as a list of dropdown `Value` strings (referencing `ReactionList`), or as free text?

## MCP tool surface

```python
@mcp.tool()
async def list_allergies() -> dict:
    """List recorded drug, food, and environmental allergies."""
```

Returns a dict shaped like `AllergiesResponse`:

```json
{
  "allergies": [],
  "total_count": 0,
  "status": "no_known_allergies",
  "status_code": 0
}
```
