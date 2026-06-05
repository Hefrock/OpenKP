# Upcoming orders endpoint

Source HARs:
- `docs/research/captures/kp-capture-noreply.har`, 2026-05-06 (homepage list-context call).
- `docs/research/captures/kp-capture-various-with-phi.har`, 2026-05-06 (drill-in call with `selectedOrderID`).
- Live redacted probe, 2026-06-05 (in-app browser + OpenKP-side endpoint
  request; printed only status/count/key shape, no values).

The original Chrome HARs stripped `GetUpcomingOrders` response bodies. On
2026-06-05, the in-app browser confirmed the rendered route/resource list, and
a separate redacted OpenKP-side probe captured the response envelope:
`orderGroupList`, `orderList`, `providerList`, and
`upcomingOrdersSettings`. Instructions are embedded in
`orderList[*].instructions`; expanding "More details" does **not** call a
separate instructions endpoint.

## What this is

The "You have new instructions to review for your requested POTASSIUM" /
"View instructions" card on the MyChart homepage. Pending lab, imaging, or
procedure orders that the doctor has placed but the patient hasn't completed
yet — with patient prep instructions attached.

This is a **new data class** OpenKP doesn't yet expose. It pairs with
`list_lab_results` / `read_lab_result` (results you've already received) but
sits at the opposite end of the lifecycle: orders the doctor placed that are
awaiting your action.

The home feed currently exposes at least one "View instructions" card; older
HARs showed counts such as "View all (5)" when multiple pending orders were
present.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Discover upcoming-order IDs | `POST /mychartcn/MixedItemFeed?noCache=<random>` | ✅ Used by `list_upcoming_orders`; CSRF required. |
| Get upcoming order list/details/instructions | `POST /mychartcn/api/upcoming-orders/GetUpcomingOrders` body `{selectedOrderID, PageNonce}` | ✅ Used by both upcoming-order tools. |

Page route: `/mychartcn/app/upcoming-orders?ordid=<encrypted-orderID>`.

## `POST /mychartcn/MixedItemFeed?noCache=<random>`

The homepage mixed feed returns JSON feed/action models. The rendered home
page turns those into "View instructions" links, but the raw endpoint carries
the route in action URI fields:

```json
{
  "SingleItemFeedViewModels": [
    {
      "FeedItems": [
        {
          "PrimaryAction": {
            "Uri": "app/upcoming-orders?ordid=WP-24...",
            "UriDisplayText": "View instructions"
          },
          "DefaultAction": {
            "Uri": "epichttp://app/upcoming-orders?ordid=WP-24...",
            "UriDisplayText": "View instructions"
          }
        }
      ]
    }
  ]
}
```

Important: this endpoint is a **POST**, not a GET. Calling it as GET returned
Kaiser's `/mychartcn/Home/FourOhFour` route in the live probe.

OpenKP uses it only to discover opaque order IDs, deduped in page order. The
canonical order data comes from `GetUpcomingOrders`.

Headers:

```text
Accept: */*
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Origin: https://healthy.kaiserpermanente.org
Referer: https://healthy.kaiserpermanente.org/mychartcn/Home
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <token from /mychartcn/Home/CSRFToken>
```

Live correction, 2026-06-05: omitting the CSRF token caused Kaiser to redirect
to `/mychartcn/Home/FiveHundred?aspxerrorpath=/mychartcn/MixedItemFeed`.
Adding the token returned `200 application/json`.

## `POST /mychartcn/api/upcoming-orders/GetUpcomingOrders`

The body carries one `selectedOrderID`. In the live 2026-06-05 probe, posting
one selected order returned all required maps for that page state:
`orderGroupList`, `orderList`, `providerList`, and `upcomingOrdersSettings`.

**Request body:**

```json
{
  "selectedOrderID": "WP-24...",
  "PageNonce": "<32-char hex>"
}
```

The `selectedOrderID` is a long URL-encoded encrypted Epic handle (`WP-24...`
prefix followed by ~150 chars of percent-encoded base64-ish payload) — the
same opaque ID that appears in the page route's `?ordid=` parameter.

**Response:** JSON. Live redacted probe returned ~2.5 KB for one pending
order. Shape:

```json
{
  "orderGroupList": {
    "<group id>": {
      "orderIDs": ["<order id>"],
      "visitName": "string",
      "encProviderID": "<provider id>",
      "dueDateISO": "YYYY-MM-DD",
      "encDateISO": "YYYY-MM-DD",
      "isSnoozed": false,
      "snoozedUntilDateISO": "YYYY-MM-DD",
      "schedulingTicketID": "",
      "apptCSN": "",
      "apptDateISO": "",
      "ticketAvailableDateISO": ""
    }
  },
  "orderList": {
    "<order id>": {
      "id": "<order id>",
      "name": "string",
      "orderedDateISO": "YYYY-MM-DD",
      "actionableDateISO": "YYYY-MM-DD",
      "actionableDateType": "expire",
      "expectedDateISO": "",
      "expiredDateISO": "YYYY-MM-DD",
      "lastPerformedDateISO": "",
      "comments": [],
      "expectedDateComment": "",
      "instructions": "<html>",
      "isPRN": false,
      "isStanding": false,
      "standingOccurrences": "",
      "originalStandingOccurrences": "",
      "orderInterval": ""
    }
  },
  "providerList": {
    "<provider id>": {
      "id": "<provider id>",
      "name": "string",
      "photoURL": "string",
      "photoBlobKey": "",
      "photoToken": "",
      "photoIsOnBlob": false
    }
  },
  "upcomingOrdersSettings": {
    "canHideOrUnhideReminders": true,
    "selectedOrderGroup": "0"
  }
}
```

**Headers:** standard `/mychartcn/` family — CSRF token + PageNonce body
field, same contract as the messages endpoints. See `messages.md` "Auth /
anti-forgery" for the two-token dance.

**PageNonce:** GET `/mychartcn/app/upcoming-orders?ordid=<id>` and extract the
CSP nonce from a `nonce="..."` attribute with the same regex used by labs and
messages: `nonce=['"]([a-f0-9]{16,})['"]`.

## Tool implementation

Shipped:

- `list_upcoming_orders()` — fetches a CSRF token, POSTs `MixedItemFeed` to
  discover order ids from feed action URIs, then calls `GetUpcomingOrders`
  with the first id and parses the returned maps.
- `read_upcoming_order_instructions(order_id)` — calls `GetUpcomingOrders`
  for one order id and returns full `instructions_html` plus stripped
  `instructions_text`.

Parser failure rule: if Kaiser omits any required top-level map
(`orderGroupList`, `orderList`, `providerList`), OpenKP raises a clear
`ValueError` instead of returning a misleading partial list. Optional fields
inside an order are tolerated as `None`, empty string, or empty list.

## Live notes

- The Codex in-app browser can show the authenticated page and resource names,
  but it cannot export a full HAR or expose XHR response bodies.
- A redacted OpenKP-side probe verified `GetUpcomingOrders` with status 200,
  `application/json`, and the response map shape above.
- `selectedOrderID: ""` did not work in the HTTP probe because loading
  `/mychartcn/app/upcoming-orders` without an order id redirected; use the
  homepage `MixedItemFeed` POST first.
- Claude MCP live test on 2026-06-05 confirmed both `list_upcoming_orders`
  and `read_upcoming_order_instructions` after the CSRF-gated feed fix.
