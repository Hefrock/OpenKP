# Access logs endpoints

Source HAR: `docs/research/captures/problems-allergies-documents-and-more.har`,
2026-04-25.

Response bodies are preserved for both access-log endpoints. The capture
contains no request-body fields beyond the pagination cursor.

Live verification: 2026-06-05 via Claude Desktop MCP. A
`list_access_log(kind="third_party", max_pages=1)` call returned 50 entries,
`has_more: true`, `stop_reason: "max_pages_reached"`, `next_cursor: 1`, and no
warnings. The first page consisted of Fasten Connect entries clustered around a
single OAuth-authorized sync burst.

Pagination live check: `list_access_log(kind="third_party", max_pages=2)`
returned 100 entries and did fetch a real second page of older Fasten Connect
events, but then stopped with `stop_reason: "cursor_repeated"` because Kaiser
returned `nextLineToParse: 1` again after page 2. The walker correctly reports
`has_more: true` plus a warning. Getting beyond that second page likely needs a
different request parameter or filter, not simply a larger `max_pages`.

## What this is

The MyChart **Access Logs** page:

`/mychartcn/app/access-logs?lang=en-US&from=landingpage`

Kaiser exposes two related logs:

- Portal access: the patient's own portal accesses.
- Third-party access: connected apps and outside data-access events.

The third-party log is the higher-value OpenKP surface because it helps the
patient see which external apps accessed which data classes and when. OpenKP
does not infer whether access was appropriate.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Portal/self access log | `POST /mychartcn/api/access-logs/GetPortalAccessLogEntries` body `{"startingLine": <cursor>}` | ✅ Mapped, implemented as `list_access_log(kind="portal")`. |
| Third-party access log | `POST /mychartcn/api/access-logs/GetThirdPartyAccessLogEntries` body `{"startingLine": <cursor>}` | ✅ Mapped, implemented as `list_access_log(kind="third_party")`. |

## Auth / anti-forgery

Same classic `/mychartcn/` API pattern:

1. Fetch a CSRF anti-forgery token from `/mychartcn/Home/CSRFToken` using the
   access-log page as the referer.
2. POST to the access-log endpoint with:
   - `__RequestVerificationToken: <token>` header
   - `Content-Type: application/json`
   - `X-Requested-With: XMLHttpRequest`
   - `Referer: https://healthy.kaiserpermanente.org/mychartcn/app/access-logs?lang=en-US&from=landingpage`

No CSP page nonce was present in the observed request bodies.

## Pagination

Initial request body:

```json
{"startingLine": -1}
```

Response envelope:

```json
{
  "entries": [],
  "nextLineToParse": 1
}
```

Observed page size is 50 entries. No request-side page-size field was captured.

`nextLineToParse` is the cursor for the next request:

```json
{"startingLine": 1}
```

Important wrinkle: the third-party endpoint returned `nextLineToParse: 1`
again in the preserved HAR after requesting `startingLine: 1`, and the same
cursor-repeat behavior was reproduced live on 2026-06-05 after the second page.
The second page still contained new older entries, so the scraper appends that
page, then stops if the cursor repeats and returns:

- `has_more: true`
- `stop_reason: "cursor_repeated"`
- a warning that results may be incomplete

This keeps the tool bounded and makes incomplete Kaiser pagination visible.

## Portal response shape

Each `GetPortalAccessLogEntries` entry has:

```json
{
  "entryType": 1,
  "ccdAction": 0,
  "accessor": "Patient Portal",
  "accessTime": "2026-04-25T07:42:43-07:00"
}
```

Observed values in Hugo's capture were all `entryType: 1` and `ccdAction: 0`,
so OpenKP exposes the raw numeric fields without assigning labels yet.

## Third-party response shape

Each `GetThirdPartyAccessLogEntries` entry has:

```json
{
  "action": "Test Result Details",
  "accessMethod": 2,
  "accessor": "Example Health App",
  "accessTime": "2026-04-18T16:38:27-07:00"
}
```

Observed `action` examples include:

- `OAuth2 Access Token Generation`
- `Immunizations`
- `Test Result Details`

Only `accessMethod: 2` was observed in the capture. OpenKP exposes the raw
numeric field until more values are captured.

## MCP surface

`list_access_log(kind="third_party", max_pages=5)`:

- `kind`: `"third_party"` (default) or `"portal"`.
- `max_pages`: number of 50-entry pages to walk. Default 5, hard-capped at 20.

Returned response fields:

- `kind`
- `entries`
- `total_count`
- `pages_walked`
- `has_more`
- `next_cursor`
- `stop_reason`
- `warnings`

Entry fields:

- Common: `kind`, `accessor`, `access_time`
- Third-party: `action`, `access_method`
- Portal: `entry_type`, `ccd_action`

## Open questions

1. What do `entryType`, `ccdAction`, and `accessMethod` map to beyond the
   observed values?
2. Does `nextLineToParse` repeat live today, or was that caused by the captured
   page state / front-end retry behavior?
3. Is there a hidden page-size or date-range filter endpoint behind the UI?
