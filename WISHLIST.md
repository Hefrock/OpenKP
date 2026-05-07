# OpenKP — Wishlist

Ideas worth building when there's appetite, not blocking v1. Each entry should explain the use case and how it fits the bare-bones-substrate framing — OpenKP exposes structure, callers and contributors build on top.

---

## Multi-user support (multi-profile on one Mac)

**Use case:** A Kaiser member (Hugo) wants to occasionally help a family member (e.g., sister-in-law) read her own KP data through Claude — without mixing accounts, leaking her PHI into his audit log, or running her tools against his session.

**Shape:** No code refactor needed. The substrate is already there.

- `OPENKP_DATA_DIR` env var already exists (defaults to `~/.openkp`). Each MCP server subprocess gets its own data dir → its own `session.json`, audit log, downloads folder.
- `KP_USERNAME` env var per process picks the keyring entry.
- Two `claude_desktop_config.json` server entries (e.g., `openkp-hugo`, `openkp-sil`) with different env blocks → two fully isolated profiles. The LLM sees them as distinct tool families (`mcp__openkp-hugo__list_messages` vs `mcp__openkp-sil__list_messages`), so cross-contamination is structurally impossible.

**What's missing:**
- README section showing the two-server config pattern with annotated env blocks.
- Optional: namespace the keyring service as `openkp:<profile>` to make export/audit cleaner. Debatable whether this is worth the migration.

**Non-goal:** Don't build a "switch profile" tool inside the MCP. Process boundaries are the right isolation primitive — anything finer-grained inside one process is more code, more risk, less elegant.

---

## No-stored-credentials login mode

**Use case:** When OpenKP is hosted on a machine the Kaiser member does not own (e.g., the helper scenario above), the member shouldn't have to entrust her password to the host. Today's flow stores the password in the host's keyring, which is fine for the single-user case but uncomfortable for the helper case.

**Shape:** A flag (e.g., `OPENKP_INTERACTIVE_LOGIN=1`) that changes `auth.py`'s first-run flow:

- Skip the autofill step.
- Playwright opens Kaiser's login page and waits for the user to type her password directly into Kaiser's form.
- OpenKP captures the resulting session cookies on redirect to `/mychartcn/Home` (existing behavior).

The password never touches OpenKP — not in keyring, not in env, not in memory. Kaiser sees it because Kaiser has to. The browser sees it because Kaiser's login is in the browser. OpenKP doesn't.

**Trade-off:** She has to re-type at session expiry (probably weekly, based on what we know of KP cookie lifetimes). For the helper case, that's a feature, not a bug.

**What's missing:** Implementation in `auth.py`, plus a README note explaining when to use this mode.

---

## `list_refill_orders(start_date, end_date)` — pharmacy order history

**Use case:** `track_refill_order` looks up one order if you already know its order number. There's no way today to ask "what refills have I placed in the last 12 months, and what did each cost?" That's the natural read companion — same domain as `request_refill` and `track_refill_order`, scoped to a list across time.

**Shape:** Sibling tool in `scrapers/refill.py`. Likely one GET against an `apims` BFF endpoint we haven't mapped yet (kp.org's Pharmacy → Order History page is the entry point). Returns a list of order summaries with `order_number`, `placed_at`, `order_status`, `copay_total`, and probably an Rx-count or per-Rx list. Reuses the same response-shape vocabulary as `track_refill_order` so callers can pivot from list → detail with one tool call.

**What's missing:**
- HAR capture: open DevTools, browse to kp.org pharmacy order history, save a focused HAR to `docs/research/captures/kp-order-history-1.har`. This is the gating step — without it, the request body / pagination shape is unknown.
- Endpoint map in `docs/research/endpoints/refill.md` (new "GET /orderHistory" or similar section).
- `fetch_refill_orders()` in `scrapers/refill.py`, MCP tool registration, tests modeled on `test_fetch_refill_order_*`.

**Adjacent endpoints already named in captures but unmapped:** `/orderStatus` (rx-order-management-bff, captured but body elided — may or may not be relevant), `/rxnotificationpreferences`, `/paytoprovider`, `/medGuide`, `/drugImage`, `/rxTransferDetails`. Worth checking during the same DevTools session whether any of these surface order-list data we'd otherwise miss.

**Non-goal:** Don't filter / aggregate / summarize on the OpenKP side. Return Kaiser's structured data and let the caller's Claude conversation do the "compare against last year" or "spot the copay outlier" work.

---

## `set_results_release_preferences(...)` — control test-result auto-release timing

**Use case:** Kaiser auto-releases lab and imaging results to MyChart on a schedule that doesn't always match the patient's preference. Some patients want results delayed until their doctor has had a chance to review and contextualize them — a critical-value lab seen at 11pm with no clinician available is more anxiety than information. MyChart already exposes a per-result-type release-timing toggle; the patient just has to find it. A Claude-driven flow ("delay my pathology results until my doctor reviews them") would surface a control most members don't know exists.

**Shape:** Phase 3 write tool with the confirm-before-act pattern. Read sibling `get_results_release_preferences()` returns the current settings; the write call submits the change.

- `POST /mychartcn/api/test-results/GetResultsReleasePreferences` body `{}` — read side, ~163 B response.
- `POST /mychartcn/api/test-results/SetResultsReleasePreferences` — write side, body shape unknown (HAR captured 2026-05-06 had bodies stripped).
- Audit log records intent + result with no PHI in the message.

**What's missing:**
- Fresh HAR with response bodies preserved to learn the preference shape (per-result-category? per-result-type? boolean delay vs. duration?).
- Endpoint map in `docs/research/endpoints/labs.md` (an "Adjacent endpoints worth noting" stub already exists, 2026-05-06).
- Scraper + tool registration + tests modeled on `request_refill`'s confirm pattern.

**Non-goal:** Don't editorialize about whether delayed release is "better." Just expose the control.

---

## `download_appointment_ics(csn)` — calendar file for one appointment

**Use case:** Kaiser already generates `.ics` files for appointments — they're behind the "Add to calendar" button on the visit-details page. Surfacing this as a tool means a member can ask "add my next two appointments to my calendar" and Claude can save the files into `~/.openkp/downloads/` (or write them straight into a calendar via a future MCP integration). Tiny but useful.

**Shape:** Single GET against an endpoint that returns text/calendar bytes. No CSRF, no nonce — same `/mychartcn/Visits/...` family.

- `GET /mychartcn/Visits/VisitDetails/GetCalendarFile?csn=<csn>&details=true`
- Saves to `~/.openkp/downloads/appointment-<csn-prefix>-<date>.ics`.
- The `csn` is already exposed by `list_appointments` and `list_past_visits`, so callers can chain naturally.

**What's missing:**
- Implementation in `scrapers/appointments.py` (or a new `calendar.py` if the file grows). Follows the existing PDF-download pattern from `download_lab_result_pdf`.
- Tests modeled on `test_download_lab_result_pdf`.
- One line of MCP tool registration.

**Non-goal:** Don't parse the .ics into a structured object — Kaiser's bytes are the source of truth, and downstream calendar apps already know how to consume the format.

---

## Adding to the wishlist

Keep entries tight. Use case + shape + what's missing + any non-goals. If an idea is just "would be nice if..." with no concrete shape, leave it out — the discipline is the point.
