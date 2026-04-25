"""Tests for scrapers/refill.py — request_refill + helpers.

Fixtures use fabricated patient, prescriber, and medication names. No PHI.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.safety import DRY_RUN_ENV
from openkp.scrapers.profile import Address, Phone, Profile
from openkp.scrapers.refill import (
    DELIVERY_METHOD_MAIL,
    PLACE_ORDER_MAIL_URL,
    SUCCESS_STATUS_CODE,
    CardOnFile,
    OrderConfirmation,
    RefillPreview,
    _build_preview,
    _digits_only,
    _estimated_mail_copay,
    _find_card_with_token,
    _find_delivery_window,
    _format_money,
    _parse_place_order_response,
    _primary_address,
    _primary_mobile_number,
    _shape_summary,
    _short_address_label,
    _wallet_headers,
    request_refill,
)

_FAKE_GUID = "9999999"
_FAKE_RX = "111111111111"
_FAKE_NHIN = "900000000"
_FAKE_MRN = "0001234567"


# --- fabricated payloads (no PHI) ---


def _user_payload() -> dict:
    """Minimal /mycare/v1.0/user response carrying the demographics we need."""
    return {
        "UserAccountData": {
            "userIdentityInfo": {"guid": _FAKE_GUID, "email": "fake@example.com"},
            "ebizAccountsWithPersonInfos": {
                "personalInformation": {
                    "personName": {
                        "firstName": "Fake",
                        "surname": "Person",
                    },
                },
                "contactInfo": {
                    "addressInfos": [{
                        "street1": "1 Fake Street",
                        "city": "Faketown",
                        "state": "CA",
                        "postalCode": "90210",
                        "addressType": {"value": "MAILING"},
                        "primaryIndicator": True,
                    }],
                    "phoneInfos": [{
                        "phoneNumber": {"area": "415", "exchange": "555", "subscriber": "0123"},
                        "phoneType": {"value": "MOBILE"},
                        "primaryIndicator": True,
                    }],
                    "emailAddresseInfos": [],
                },
                "areaOfCareInfos": [{"guid": _FAKE_GUID, "mrn": _FAKE_MRN}],
            },
        }
    }


def _rx_row(
    *,
    rxnbr: str = _FAKE_RX,
    name: str = "Fake Drug 10 Mg Tab",
    mailable: str = "true",
    refill_eligible: bool = True,
    rar_status: bool = False,
    rar_code: str = "",
    estimated_mail_copay: float = 0.0,
) -> dict:
    """One fillable[] row, mirroring the real Kaiser shape."""
    return {
        "lastRefillDate": "01/26/2026",
        "lastSoldDate": "01/26/2026",
        "rxReadyDate": None,
        "prescribedOn": "10/31/2025",
        "nhinId": _FAKE_NHIN,
        "statusCodes": None,
        "mailable": mailable,
        "consumerName": "DR. FAKE PRESCRIBER MD",
        "refillsRemaining": "2",
        "medicineName": name,
        "consumerInstructions": "Take 1 tablet by mouth daily",
        "drugEncyclopediaLink": "/health-wellness/drug-encyclopedia/drug.fake.123",
        "isNewPrescription": False,
        "commonBrandName": "FAKE BRAND",
        "afcInfo": {
            "mrn": _FAKE_MRN,
            "mrnrgn": "NCA",
            "rxnbr": rxnbr,
            "drugnm": name,
            "dispenseddayssupply": 100,
            "rxsold": "01/26/2026",
            "copay": 11.67,
            "phrmcyid": _FAKE_NHIN,
            "deacode": "6",
            "nextFillEligibleDate": "04/16/2026",
            "fillable": True,
            "lastdispensedndc": "00000111111",
            "prn": False,
            "sigText": "Take 1 tablet by mouth daily",
        },
        "fillOptions": [
            {
                "deliveryMethod": "L",
                "daysSupply": 100,
                "quantity": 100,
                "ucCharge": 11.67,
                "planPay": 11.67,
                "estimatedCopay": 0,
                "copaystts": "000",
                "copaysttsdtl": "Approved",
            },
            {
                "deliveryMethod": "M",
                "daysSupply": 100,
                "quantity": 100,
                "ucCharge": 11.67,
                "planPay": 11.67,
                "estimatedCopay": estimated_mail_copay,
                "copaystts": "000",
                "copaysttsdtl": "Approved",
            },
        ],
        "RxCustomIndicators": [{"key": "PRN", "value": "false"}],
        "refillEligible": refill_eligible,
        "rarStatus": rar_status,
        "rarCodeKey": rar_code,
        "rarCodeValue": "",
        "daysSupplyThreshold": "200",
        "rxNumber": rxnbr,
        "selections": {"M": {"selectedDaysSupply": "100", "selectedQuantity": "100", "selectedEstimatedCopay": 0}},
        "autoRefillEligible": True,
        "autoRefill": False,
    }


def _rx_details_payload(rows: list[dict], non_fillable: list[dict] | None = None) -> dict:
    return {
        "executionContext": {"statusCode": "000", "statusDetails": "Success"},
        "prescriptions": {
            "rxRefillResponse": True,
            "fillable": rows,
            "nonFillable": non_fillable or [],
            "totalRefillableCount": len(rows),
            "recentRefillableCount": len(rows),
        },
    }


def _wallet_payload() -> dict:
    """Plausible /walletV3 shape carrying one card. Real shape unknown — see refill.md."""
    return {
        "walletDetails": [
            {
                "isDefault": True,
                "cardHolderName": {"firstName": "Fake", "lastName": "Person", "middleName": ""},
                "billingAddress": {"zipCode": "90210"},
                "expiryDate": "02/28",
                "last4Digit": "2000",
                "cardType": "AM",
                "walletPaymentToken": "fake-token-abcdef",
            }
        ]
    }


def _empty_wallet_payload() -> dict:
    return {"walletDetails": []}


def _shipping_date_payload() -> dict:
    return {
        "deliveryEstimate": "Estimated to arrive between Wednesday, May 6 and Friday, May 8.",
    }


def _place_order_success_payload() -> dict:
    return {
        "submittedBy": {
            "placerName": {"lastName": "PERSON", "firstName": "FAKE", "middleName": ""},
            "placerID": {"placerIdType": "MRN", "placerIDValue": _FAKE_MRN},
        },
        "Order": {
            "orderPlacedDate": "04/26/2026 10:00:00",
            "transactionControlRefNo": "transaction-uuid",
            "orderNumber": "030transaction-uuid20260426100000123",
        },
        "rxRefillArray": {
            "rxRefill": [{
                "rxNumber": _FAKE_RX,
                "mrn": _FAKE_MRN,
                "memberName": {"lastName": "Person", "firstName": "Fake", "middleName": ""},
                "rxOrderResponseCode": "000",
                "mrnRegion": "NCA",
            }]
        },
        "deliveryMethod": "M",
        "executionContext": {"statusDetails": "Success", "statusCode": "000"},
        "region": "NCA",
        "sourceApplication": "WPP",
    }


def _place_order_failure_payload() -> dict:
    return {
        "Order": {
            "orderPlacedDate": "04/26/2026 10:00:00",
            "orderNumber": "030xxx",
        },
        "rxRefillArray": {"rxRefill": [{"rxNumber": _FAKE_RX, "rxOrderResponseCode": "999"}]},
        "deliveryMethod": "M",
        "executionContext": {"statusDetails": "Backend error", "statusCode": "9999"},
    }


def _profile_with_full_contact() -> Profile:
    return Profile(
        first_name="Fake",
        last_name="Person",
        middle_name="",
        email="fake@example.com",
        guid=_FAKE_GUID,
        mrn="14776978",
        addresses=[
            Address(
                type="MAILING",
                street1="1 Fake Street",
                city="Faketown",
                state="CA",
                postal_code="90210",
                is_primary=True,
            )
        ],
        phones=[Phone(type="MOBILE", number="415-555-0123", is_primary=True)],
    )


def _wallet_card() -> CardOnFile:
    return CardOnFile(
        card_holder_first="Fake",
        card_holder_last="Person",
        card_holder_middle="",
        expiry_date="02/28",
        last_4_digit="2000",
        card_type="AM",
        wallet_payment_token="fake-token-abcdef",
        billing_zip="90210",
    )


# --- httpx patch helpers ---


def _make_store() -> MagicMock:
    from openkp.scrapers.auth import KaiserSession

    store = MagicMock()
    store.get_session = AsyncMock(
        return_value=KaiserSession(
            cookies=[{"name": "k", "value": "v", "domain": ".kaiserpermanente.org", "path": "/"}],
            user_agent="ua",
        )
    )
    store.invalidate = AsyncMock()
    return store


def _bind_request(responses: list[httpx.Response]) -> list[httpx.Response]:
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org/")
    for r in responses:
        r.request = req
    return responses


def _patch_http(responses: list[httpx.Response]):
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=_bind_request(responses))
    patched = patch("openkp.scrapers.request.httpx.AsyncClient")
    client_cls = patched.start()
    client_cls.return_value.__aenter__.return_value = mock_client
    client_cls.return_value.__aexit__.return_value = None
    return mock_client, patched


# --- primitives ---


def test_digits_only_strips_non_digits():
    assert _digits_only("415-555-0123") == "4155550123"
    assert _digits_only("(415) 555-0123 ext. 99") == "415555012399"


def test_digits_only_handles_none_and_empty():
    assert _digits_only(None) is None
    assert _digits_only("") is None
    assert _digits_only("---") is None


def test_format_money_two_decimals():
    assert _format_money(0) == "0.00"
    assert _format_money(11.6) == "11.60"
    assert _format_money(11.678) == "11.68"


def test_format_money_none_default():
    assert _format_money(None) == "0.00"


def test_short_address_label():
    p = _profile_with_full_contact()
    assert _short_address_label(p) == "Faketown, CA 90210"


def test_short_address_label_title_cases_uppercase_city():
    """Kaiser returns city ALL CAPS; we present it title-cased."""
    p = Profile(addresses=[Address(
        city="OAKLAND", state="CA", postal_code="90210", is_primary=True,
    )])
    assert _short_address_label(p) == "Oakland, CA 90210"


def test_short_address_label_no_address():
    assert _short_address_label(Profile()) is None


def test_short_address_label_partial_fields():
    p = Profile(addresses=[Address(state="CA", postal_code="90210", is_primary=True)])
    assert _short_address_label(p) == "CA 90210"


def test_primary_address_picks_primary_flag():
    p = Profile(addresses=[
        Address(state="CA", postal_code="00000", is_primary=False),
        Address(state="CA", postal_code="90210", is_primary=True),
    ])
    addr = _primary_address(p)
    assert addr is not None and addr.postal_code == "90210"


def test_primary_address_falls_back_to_first():
    p = Profile(addresses=[
        Address(state="CA", postal_code="11111", is_primary=False),
        Address(state="CA", postal_code="22222", is_primary=False),
    ])
    addr = _primary_address(p)
    assert addr is not None and addr.postal_code == "11111"


def test_primary_mobile_picks_mobile_type():
    p = Profile(phones=[
        Phone(type="HOME", number="510-111-1111"),
        Phone(type="MOBILE", number="415-555-0123"),
    ])
    assert _primary_mobile_number(p) == "415-555-0123"


def test_primary_mobile_falls_back_to_first():
    p = Profile(phones=[Phone(type="HOME", number="510-111-1111")])
    assert _primary_mobile_number(p) == "510-111-1111"


def test_estimated_mail_copay_finds_mail_option():
    row = _rx_row(estimated_mail_copay=12.34)
    assert _estimated_mail_copay(row) == 12.34


def test_estimated_mail_copay_falls_back_to_afc_copay():
    """When fillOptions[] has no Mail entry, fall back to afcInfo.copay."""
    row = _rx_row()
    row["fillOptions"] = [{"deliveryMethod": "L", "estimatedCopay": 5.0}]
    assert _estimated_mail_copay(row) == 11.67  # afcInfo.copay


def test_estimated_mail_copay_handles_missing():
    row = {"afcInfo": {}}
    assert _estimated_mail_copay(row) is None


# --- recursive walkers ---


def test_find_card_with_token_top_level():
    payload = {"walletPaymentToken": "x", "last4Digit": "2000"}
    assert _find_card_with_token(payload) is payload


def test_find_card_with_token_nested():
    nested = {"a": {"b": [{"c": {"walletPaymentToken": "x", "last4Digit": "2000"}}]}}
    found = _find_card_with_token(nested)
    assert found is not None and found.get("walletPaymentToken") == "x"


def test_find_card_with_token_returns_none_when_missing():
    assert _find_card_with_token({"a": "b"}) is None
    assert _find_card_with_token({"walletPaymentToken": ""}) is None  # empty token doesn't count


def test_find_card_with_token_accepts_alternative_key_names():
    """Defensive: walletV3 might use 'paymentToken' or 'token' instead."""
    by_payment_token = {"paymentToken": "x"}
    by_token = {"token": "x"}
    assert _find_card_with_token(by_payment_token) is by_payment_token
    assert _find_card_with_token(by_token) is by_token


def test_shape_summary_keys_only_no_values():
    """Diagnostic helper must NEVER expose values."""
    payload = {"top": [{"inner": "secret-token-value", "n": 42}]}
    summary = _shape_summary(payload)
    rendered = json.dumps(summary)
    assert "secret-token-value" not in rendered
    assert "top" in rendered
    assert "inner" in rendered


def test_shape_summary_caps_recursion_depth():
    deeply_nested = {"a": {"b": {"c": {"d": {"e": {"f": "value"}}}}}}
    summary = _shape_summary(deeply_nested, max_depth=2)
    rendered = json.dumps(summary)
    assert "value" not in rendered


def test_wallet_headers_match_har_capture_casing():
    """Header casing has to match exactly; KP's gateway has been case-sensitive."""
    h = _wallet_headers()
    assert "x-apiKey" in h  # lowercase x, what the browser sends
    assert "X-apiKey" not in h  # NOT the wrong casing
    assert h["X-region"] == "MRN"
    assert h["X-appName"] == "rwd"
    # X-idType is NOT in the captured walletV3 headers — make sure we don't add it.
    assert "X-idType" not in h
    assert "x-idType" not in h


def test_find_delivery_window_finds_string():
    payload = {"a": {"b": "Estimated to arrive between Wed, May 6 and Fri, May 8."}}
    assert _find_delivery_window(payload).startswith("Estimated to arrive")


def test_find_delivery_window_none_when_missing():
    assert _find_delivery_window({"a": "something else"}) is None


# --- _build_preview ---


def test_build_preview_happy_path():
    row = _rx_row(estimated_mail_copay=0.0)
    preview = _build_preview(_FAKE_RX, row, "fillable", _profile_with_full_contact(), _wallet_card())

    assert preview.medication_id == _FAKE_RX
    assert preview.name == "Fake Drug 10 Mg Tab"
    assert preview.brand_name == "FAKE BRAND"
    assert preview.estimated_copay == 0.0
    assert preview.days_supply == 100
    assert preview.refills_remaining == 2
    assert preview.next_fill_eligible_date == "04/16/2026"
    assert preview.delivery_method == DELIVERY_METHOD_MAIL
    assert preview.shipping_city_state_zip == "Faketown, CA 90210"
    assert preview.payment_method_on_file is True
    assert preview.can_confirm is True
    assert preview.warnings == []


def test_build_preview_when_rx_not_found():
    preview = _build_preview(_FAKE_RX, None, None, _profile_with_full_contact(), _wallet_card())
    assert preview.can_confirm is False
    assert preview.payment_method_on_file is True
    assert any("not found" in w for w in preview.warnings)


def test_build_preview_when_rx_in_nonfillable_bucket():
    """Recently-refilled or otherwise-blocked Rx lives in nonFillable[]."""
    row = _rx_row(refill_eligible=False)
    preview = _build_preview(_FAKE_RX, row, "nonFillable", _profile_with_full_contact(), _wallet_card())
    assert preview.can_confirm is False
    # Should explain WHY, not just say "not found".
    assert any("not currently accepting a refill" in w for w in preview.warnings)
    # And include the next-fill-eligible date in the not-eligible warning.
    assert any("04/16/2026" in w for w in preview.warnings)


def test_build_preview_when_no_refills_remaining_in_nonfillable():
    row = _rx_row()
    row["refillsRemaining"] = "0"
    preview = _build_preview(_FAKE_RX, row, "nonFillable", _profile_with_full_contact(), _wallet_card())
    assert preview.can_confirm is False
    assert any("no refills remaining" in w for w in preview.warnings)


def test_build_preview_when_wallet_missing():
    row = _rx_row()
    preview = _build_preview(_FAKE_RX, row, "fillable", _profile_with_full_contact(), None)
    assert preview.payment_method_on_file is False
    assert preview.can_confirm is False
    assert any("payment method" in w for w in preview.warnings)


def test_build_preview_when_rx_not_mailable():
    row = _rx_row(mailable="false")
    preview = _build_preview(_FAKE_RX, row, "fillable", _profile_with_full_contact(), _wallet_card())
    assert preview.can_confirm is False
    assert any("not mailable" in w for w in preview.warnings)


def test_build_preview_when_not_refill_eligible():
    row = _rx_row(refill_eligible=False)
    preview = _build_preview(_FAKE_RX, row, "fillable", _profile_with_full_contact(), _wallet_card())
    assert preview.can_confirm is False
    assert any("not currently refill-eligible" in w for w in preview.warnings)


def test_build_preview_blocked_by_rar_status():
    row = _rx_row(rar_status=True, rar_code="Rx_NoMoreRefills")
    preview = _build_preview(_FAKE_RX, row, "fillable", _profile_with_full_contact(), _wallet_card())
    assert preview.can_confirm is False
    assert any("Rx_NoMoreRefills" in w for w in preview.warnings)


def test_build_preview_deduplicates_warnings():
    """Don't surface the same warning twice from different paths."""
    row = _rx_row(refill_eligible=False)
    preview = _build_preview(_FAKE_RX, row, "nonFillable", _profile_with_full_contact(), _wallet_card())
    # Each unique warning string appears at most once.
    assert len(preview.warnings) == len(set(preview.warnings))


# --- _parse_place_order_response ---


def test_parse_place_order_success():
    confirmation = _parse_place_order_response(
        payload=_place_order_success_payload(),
        rx_number=_FAKE_RX,
        rx_name="Fake Drug 10 Mg Tab",
        delivery_window="Estimated to arrive ...",
    )
    assert confirmation.succeeded is True
    assert confirmation.execution_status_code == "000"
    assert confirmation.per_rx_response_code == "000"
    assert confirmation.order_number.startswith("030")
    assert confirmation.delivery_method == "M"
    assert confirmation.estimated_delivery_window == "Estimated to arrive ..."
    assert confirmation.dry_run is False


def test_parse_place_order_failure_status_codes():
    confirmation = _parse_place_order_response(
        payload=_place_order_failure_payload(),
        rx_number=_FAKE_RX,
        rx_name="Fake Drug 10 Mg Tab",
        delivery_window=None,
    )
    assert confirmation.succeeded is False
    assert confirmation.execution_status_code == "9999"
    assert confirmation.per_rx_response_code == "999"


def test_parse_place_order_malformed_returns_unsucceeded():
    confirmation = _parse_place_order_response(
        payload={},
        rx_number=_FAKE_RX,
        rx_name="x",
        delivery_window=None,
    )
    assert confirmation.succeeded is False
    assert confirmation.execution_status_code is None
    assert confirmation.rx_number == _FAKE_RX  # falls back to passed rx_number


# --- request_refill (integration) ---


@pytest.mark.asyncio
async def test_request_refill_preview_path(monkeypatch):
    """confirm=False does GUID + rxDetails + demographics + wallet, returns preview."""
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    responses = [
        httpx.Response(200, json=_user_payload()),                          # _fetch_user_guid
        httpx.Response(200, json=_rx_details_payload([_rx_row()])),         # _find_fillable_rx
        httpx.Response(200, json=_user_payload()),                          # fetch_demographics
        httpx.Response(200, json=_wallet_payload()),                        # _fetch_wallet
    ]
    mock_client, p = _patch_http(responses)
    try:
        result = await request_refill(KaiserRequest(store), _FAKE_RX, confirm=False)
    finally:
        p.stop()

    assert isinstance(result, RefillPreview)
    assert result.can_confirm is True
    assert result.payment_method_on_file is True
    assert result.shipping_city_state_zip == "Faketown, CA 90210"
    # 4 HTTP calls, no commit POSTs.
    assert mock_client.request.await_count == 4
    methods = [call.args[0] for call in mock_client.request.await_args_list]
    assert methods == ["GET", "GET", "GET", "GET"]


@pytest.mark.asyncio
async def test_request_refill_preview_when_rx_not_anywhere(monkeypatch):
    """Rx absent from BOTH fillable and nonFillable buckets."""
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    responses = [
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_rx_details_payload([], non_fillable=[])),  # both empty
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_wallet_payload()),
    ]
    _, p = _patch_http(responses)
    try:
        result = await request_refill(KaiserRequest(store), _FAKE_RX, confirm=False)
    finally:
        p.stop()
    assert isinstance(result, RefillPreview)
    assert result.can_confirm is False
    assert any("not found" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_request_refill_preview_when_rx_in_nonfillable_only(monkeypatch):
    """Recently-refilled scenario: Rx is in nonFillable bucket, not fillable.

    Regression for the live bug where the preview returned 'not found' even
    though the Rx existed (just not currently fillable, because it was
    mid-shipment from an earlier order).
    """
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    rx_row = _rx_row(refill_eligible=False)
    responses = [
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_rx_details_payload([], non_fillable=[rx_row])),
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_wallet_payload()),
    ]
    _, p = _patch_http(responses)
    try:
        result = await request_refill(KaiserRequest(store), _FAKE_RX, confirm=False)
    finally:
        p.stop()
    assert isinstance(result, RefillPreview)
    # Found, so we DO populate name/brand/copay (unlike "not found" path).
    assert result.name == "Fake Drug 10 Mg Tab"
    assert result.refills_remaining == 2
    # But blocked.
    assert result.can_confirm is False
    assert any("not currently accepting a refill" in w for w in result.warnings)
    # And we do NOT say "not found".
    assert not any("not found" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_request_refill_preview_when_wallet_has_no_token(monkeypatch):
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    responses = [
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_rx_details_payload([_rx_row()])),
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_empty_wallet_payload()),
    ]
    _, p = _patch_http(responses)
    try:
        result = await request_refill(KaiserRequest(store), _FAKE_RX, confirm=False)
    finally:
        p.stop()
    assert result.can_confirm is False
    assert result.payment_method_on_file is False


@pytest.mark.asyncio
async def test_request_refill_dry_run_short_circuits(tmp_path: Path, monkeypatch):
    """confirm=True under OPENKP_DRY_RUN=1 skips the 3 commit POSTs."""
    monkeypatch.setenv(DRY_RUN_ENV, "1")
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    responses = [
        httpx.Response(200, json=_user_payload()),                  # GUID
        httpx.Response(200, json=_rx_details_payload([_rx_row()])),  # rxDetails
        httpx.Response(200, json=_user_payload()),                  # demographics
        httpx.Response(200, json=_wallet_payload()),                # wallet
        # NO commit POSTs under dry-run.
    ]
    mock_client, p = _patch_http(responses)
    try:
        result = await request_refill(
            KaiserRequest(store),
            _FAKE_RX,
            confirm=True,
            data_dir=tmp_path,
        )
    finally:
        p.stop()

    assert isinstance(result, OrderConfirmation)
    assert result.dry_run is True
    assert result.succeeded is True
    assert result.order_number.startswith("dry-run-")
    assert result.execution_status_code == SUCCESS_STATUS_CODE
    # Only the 4 prep GETs ran.
    assert mock_client.request.await_count == 4
    # Audit log has both intent and result events.
    audit_lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 2
    intent = json.loads(audit_lines[0])
    result_event = json.loads(audit_lines[1])
    assert intent["phase"] == "intent"
    assert intent["dry_run"] is True
    assert result_event["phase"] == "result"
    assert result_event["succeeded"] is True


@pytest.mark.asyncio
async def test_request_refill_commit_happy_path(tmp_path: Path, monkeypatch):
    """confirm=True without dry-run runs the full pipeline."""
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    responses = [
        httpx.Response(200, json=_user_payload()),                          # GUID
        httpx.Response(200, json=_rx_details_payload([_rx_row()])),         # rxDetails
        httpx.Response(200, json=_user_payload()),                          # demographics
        httpx.Response(200, json=_wallet_payload()),                        # wallet
        httpx.Response(200, json={}),                                       # cart/prescription
        httpx.Response(200, json={}),                                       # rxdeliveryeligibility
        httpx.Response(200, json=_shipping_date_payload()),                 # shippingDate
        httpx.Response(200, json=_place_order_success_payload()),           # placeorderMail
    ]
    mock_client, p = _patch_http(responses)
    try:
        result = await request_refill(
            KaiserRequest(store),
            _FAKE_RX,
            confirm=True,
            data_dir=tmp_path,
        )
    finally:
        p.stop()

    assert isinstance(result, OrderConfirmation)
    assert result.succeeded is True
    assert result.dry_run is False
    assert result.execution_status_code == "000"
    assert result.per_rx_response_code == "000"
    assert result.order_number.startswith("030")
    assert result.estimated_delivery_window.startswith("Estimated to arrive")

    # 8 HTTP calls: 4 prep + cart + eligibility + shippingDate + placeorderMail.
    assert mock_client.request.await_count == 8
    # Last call is the placeorderMail POST.
    last_call = mock_client.request.await_args_list[-1]
    assert last_call.args[0] == "POST"
    assert last_call.args[1] == PLACE_ORDER_MAIL_URL
    body = last_call.kwargs["json"]
    assert body["placeOrderReq"]["deliveryMethod"] == DELIVERY_METHOD_MAIL
    assert body["placeOrderReq"]["placerDetails"]["placerID"]["placerIDValue"] == _FAKE_GUID
    assert body["placeOrderReq"]["creditCardDetails"]["walletPaymentToken"] == "fake-token-abcdef"
    assert body["placeOrderReq"]["shippingAddress"]["zip"] == "90210"
    # Mobile is digits-only.
    assert body["placeOrderReq"]["placerDetails"]["mobileNumber"] == "4155550123"

    # Audit log has intent + result, both with dry_run=False, and no card details leaked.
    audit_lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 2
    for line in audit_lines:
        event = json.loads(line)
        assert event["dry_run"] is False
        assert "walletPaymentToken" not in json.dumps(event) or "[redacted]" in json.dumps(event)


@pytest.mark.asyncio
async def test_request_refill_commit_records_error_on_failure(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    # cart/prescription returns 500 → raise_for_status raises → audit logs error and re-raises.
    responses = [
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_rx_details_payload([_rx_row()])),
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_wallet_payload()),
        httpx.Response(500, text="boom"),  # cart/prescription fails
    ]
    _, p = _patch_http(responses)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await request_refill(
                KaiserRequest(store),
                _FAKE_RX,
                confirm=True,
                data_dir=tmp_path,
            )
    finally:
        p.stop()

    audit_lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    phases = [json.loads(line)["phase"] for line in audit_lines]
    assert "intent" in phases
    assert "error" in phases


@pytest.mark.asyncio
async def test_request_refill_refuses_confirm_when_preview_blocks(tmp_path: Path, monkeypatch):
    """confirm=True must raise when the preview surfaced any blocker (e.g., not mailable)."""
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    responses = [
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_rx_details_payload([_rx_row(mailable="false")])),
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_wallet_payload()),
    ]
    _, p = _patch_http(responses)
    try:
        with pytest.raises(ValueError, match="Cannot confirm refill"):
            await request_refill(
                KaiserRequest(store),
                _FAKE_RX,
                confirm=True,
                data_dir=tmp_path,
            )
    finally:
        p.stop()
    # No audit events, since we refused before issuing the intent.
    log_path = tmp_path / "audit.log"
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_request_refill_confirm_requires_data_dir(monkeypatch):
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    responses = [
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_rx_details_payload([_rx_row()])),
        httpx.Response(200, json=_user_payload()),
        httpx.Response(200, json=_wallet_payload()),
    ]
    _, p = _patch_http(responses)
    try:
        with pytest.raises(ValueError, match="data_dir is required"):
            await request_refill(KaiserRequest(store), _FAKE_RX, confirm=True, data_dir=None)
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_request_refill_invalid_id_raises():
    from openkp.scrapers.request import KaiserRequest

    store = _make_store()
    with pytest.raises(ValueError, match="medication_id must be"):
        await request_refill(KaiserRequest(store), "")
    with pytest.raises(ValueError, match="medication_id must be"):
        await request_refill(KaiserRequest(store), "   ")
