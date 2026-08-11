"""Tests for shared/review_queue.py.

All Smartsheet calls are mocked at the boundary — these tests never
hit the live ITS_Review_Queue. Run with:
    pytest -q tests/test_review_queue.py
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from shared import review_queue, sheet_ids
from shared.error_log import Severity
from shared.review_queue import (
    VALID_WORKSTREAMS,
    ItemNotFoundError,
    ReviewQueueError,
    ReviewReason,
    ReviewStatus,
    SlaTier,
)


@pytest.fixture
def add_rows_mock(mocker):
    """Mock the Smartsheet write boundary. Returns row IDs in input order."""
    mock = mocker.patch(
        "shared.review_queue.smartsheet_client.add_rows",
        return_value=[9001],
    )
    return mock


@pytest.fixture
def get_rows_mock(mocker):
    """Mock the Smartsheet read boundary."""
    return mocker.patch("shared.review_queue.smartsheet_client.get_rows")


# ---- Enum / picklist parity ----------------------------------------------


def test_review_status_values_match_live_picklist():
    # Values must match the ITS_Review_Queue.Status picklist exactly
    # (verified live 2026-05-18). Symbolic guard against drift.
    expected = {"PENDING", "IN_REVIEW", "APPROVED", "REJECTED", "ESCALATED"}
    assert {s.value for s in ReviewStatus} == expected


def test_review_reason_values_match_live_picklist():
    # Three values added 2026-05-23 with the trusted-contacts cluster
    # (header-soft-fail-trusted / sender-pending-verification /
    # project-out-of-scope) await operator UI add to the live picklist.
    # Smartsheet accepts unknown picklist values as plain strings — writes
    # succeed pre-UI-add; pivot views just don't bucket them until then.
    expected = {
        "low-confidence-extraction", "ambiguous-classification",
        "structured-output-edge", "zero-data-window", "mismatched-reference",
        "security-trigger", "policy-edge", "manual", "other",
        "header-soft-fail-trusted", "sender-pending-verification",
        "project-out-of-scope",
    }
    assert {r.value for r in ReviewReason} == expected


def test_sla_tier_values_match_live_picklist():
    expected = {"4h", "24h", "48h"}
    assert {t.value for t in SlaTier} == expected


def test_valid_workstreams_match_live_picklist():
    # `progress_reports` added at P5 — the progress weekly compile's per-job fence and
    # the recipient_health send-time hook both enqueue with workstream="progress_reports".
    # `field_ops` added 2026-08-11: manifest_poll tickets under it, and the FIRST live
    # refusal through the lane (Bradley 1 Customer BOM, §34 SUSPICIOUS) lost its ticket
    # to the omission — the same latent class, caught by a real document instead of CI;
    # the parity test below now enumerates the ticketing daemons so the next one fails
    # here by name.
    # Smartsheet accepts an unknown picklist value as a plain string (the write succeeds),
    # but the operator should keep the live ITS_Review_Queue Workstream picklist in step
    # so pivot views bucket it (same pattern noted in the ReviewReason docstring).
    expected = {
        "safety_reports", "progress_reports", "po_materials", "subcontracts",
        "field_ops", "email_triage", "ai_employee", "global",
    }
    assert set(VALID_WORKSTREAMS) == expected


# ---- add() payload + write -----------------------------------------------


def test_add_writes_correct_cell_payload(add_rows_mock):
    row_id = review_queue.add(
        workstream="safety_reports",
        summary="WPR extraction had two job-ID matches",
        payload={"candidates": ["2025.201.a", "2025.201.b"]},
        sla_tier=SlaTier.SAFETY_INTAKE,
        reason=ReviewReason.AMBIGUOUS_CLASSIFICATION,
        severity=Severity.WARN,
        source_file="box://reports/2026-05-18-WPR-001.pdf",
        security_flag=False,
    )

    assert row_id == 9001
    add_rows_mock.assert_called_once()
    sheet_id, rows = add_rows_mock.call_args.args
    assert sheet_id == sheet_ids.SHEET_REVIEW_QUEUE
    assert len(rows) == 1

    row = rows[0]
    assert row["Workstream"] == "safety_reports"
    assert row["Summary"] == "WPR extraction had two job-ID matches"
    assert row["Reason"] == "ambiguous-classification"
    assert row["Severity"] == "WARN"
    assert row["SLA Tier"] == "4h"
    assert row["Source File"] == "box://reports/2026-05-18-WPR-001.pdf"
    assert row["Status"] == "PENDING"
    assert row["Security Flag"] is False
    assert row["Created At"] == date.today().isoformat()

    # Item ID: <workstream>-<YYYYMMDD>-<HHMMSS>
    assert row["Item ID"].startswith("safety_reports-")
    parts = row["Item ID"].split("-")
    assert len(parts) == 3
    assert len(parts[1]) == 8   # YYYYMMDD
    assert len(parts[2]) == 6   # HHMMSS

    # Payload: compact JSON encoding.
    parsed = json.loads(row["Payload"])
    assert parsed == {"candidates": ["2025.201.a", "2025.201.b"]}
    assert " " not in row["Payload"]  # compact separators=(",", ":")


def test_add_security_flag_true_sets_checkbox(add_rows_mock):
    review_queue.add(
        workstream="safety_reports",
        summary="anomaly_logger sentinel: instructions-in-pdf",
        payload={"sentinel": "instructions-in-pdf", "match": "Ignore previous"},
        sla_tier=SlaTier.SAFETY_INTAKE,
        reason=ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        security_flag=True,
    )

    row = add_rows_mock.call_args.args[1][0]
    assert row["Security Flag"] is True
    assert row["Severity"] == "CRITICAL"
    assert row["Reason"] == "security-trigger"


def test_add_defaults_reason_to_other_and_severity_to_warn(add_rows_mock):
    review_queue.add(
        workstream="po_materials",
        summary="x",
        payload={},
        sla_tier=SlaTier.RFQ_DRAFT,
    )

    row = add_rows_mock.call_args.args[1][0]
    assert row["Reason"] == "other"
    assert row["Severity"] == "WARN"
    assert row["Source File"] == ""


@pytest.mark.parametrize("sla", list(SlaTier))
def test_add_accepts_all_sla_tiers(add_rows_mock, sla):
    review_queue.add(
        workstream="subcontracts",
        summary="s", payload={}, sla_tier=sla,
    )
    row = add_rows_mock.call_args.args[1][0]
    assert row["SLA Tier"] == sla.value


@pytest.mark.parametrize("reason", list(ReviewReason))
def test_add_accepts_all_reasons(add_rows_mock, reason):
    review_queue.add(
        workstream="safety_reports",
        summary="s", payload={}, sla_tier=SlaTier.SAFETY_INTAKE,
        reason=reason,
    )
    row = add_rows_mock.call_args.args[1][0]
    assert row["Reason"] == reason.value


@pytest.mark.parametrize("severity", list(Severity))
def test_add_accepts_all_severities(add_rows_mock, severity):
    review_queue.add(
        workstream="safety_reports",
        summary="s", payload={}, sla_tier=SlaTier.SAFETY_INTAKE,
        severity=severity,
    )
    row = add_rows_mock.call_args.args[1][0]
    assert row["Severity"] == severity.value


@pytest.mark.parametrize("ws", sorted(VALID_WORKSTREAMS))
def test_add_accepts_all_workstreams(add_rows_mock, ws):
    review_queue.add(
        workstream=ws,
        summary="s", payload={}, sla_tier=SlaTier.SAFETY_INTAKE,
    )
    row = add_rows_mock.call_args.args[1][0]
    assert row["Workstream"] == ws


def test_add_rejects_invalid_workstream(add_rows_mock):
    with pytest.raises(ValueError, match="not in"):
        review_queue.add(
            workstream="not_a_real_workstream",
            summary="s", payload={}, sla_tier=SlaTier.SAFETY_INTAKE,
        )
    add_rows_mock.assert_not_called()


def test_add_handles_nested_payload_structures(add_rows_mock):
    nested = {
        "candidates": [
            {"job_id": "2025.201.a", "confidence": 0.62},
            {"job_id": "2025.201.b", "confidence": 0.55},
        ],
        "extracted_text": "first 300 chars...",
        "metadata": {"document_hash": "abc123"},
    }
    review_queue.add(
        workstream="safety_reports",
        summary="nested test", payload=nested,
        sla_tier=SlaTier.SAFETY_INTAKE,
    )
    row = add_rows_mock.call_args.args[1][0]
    assert json.loads(row["Payload"]) == nested


def test_add_propagates_smartsheet_errors(add_rows_mock):
    # The brief mandates failure propagation — workstream callers need to
    # know the queue write failed so they can fire the triple-fire CRITICAL.
    from shared.smartsheet_client import SmartsheetError
    add_rows_mock.side_effect = SmartsheetError("HTTP 503: unavailable")

    with pytest.raises(SmartsheetError, match="503"):
        review_queue.add(
            workstream="safety_reports",
            summary="s", payload={}, sla_tier=SlaTier.SAFETY_INTAKE,
        )


# ---- get_status() --------------------------------------------------------


def test_get_status_returns_enum_from_matching_row(get_rows_mock):
    get_rows_mock.return_value = [
        {"_row_id": 1, "Item ID": "safety_reports-20260518-153022",
         "Status": "IN_REVIEW"},
    ]

    status = review_queue.get_status("safety_reports-20260518-153022")

    assert status is ReviewStatus.IN_REVIEW
    get_rows_mock.assert_called_once_with(
        sheet_ids.SHEET_REVIEW_QUEUE,
        filters={"Item ID": "safety_reports-20260518-153022"},
    )


@pytest.mark.parametrize("status_str,expected", [
    ("PENDING", ReviewStatus.PENDING),
    ("IN_REVIEW", ReviewStatus.IN_REVIEW),
    ("APPROVED", ReviewStatus.APPROVED),
    ("REJECTED", ReviewStatus.REJECTED),
    ("ESCALATED", ReviewStatus.ESCALATED),
])
def test_get_status_parses_all_picklist_values(get_rows_mock, status_str, expected):
    get_rows_mock.return_value = [
        {"_row_id": 1, "Item ID": "x", "Status": status_str},
    ]
    assert review_queue.get_status("x") is expected


def test_get_status_raises_item_not_found(get_rows_mock):
    get_rows_mock.return_value = []

    with pytest.raises(ItemNotFoundError, match="no ITS_Review_Queue row"):
        review_queue.get_status("does-not-exist")


def test_get_status_raises_when_status_cell_is_non_string(get_rows_mock):
    get_rows_mock.return_value = [
        {"_row_id": 1, "Item ID": "x", "Status": None},
    ]

    with pytest.raises(ReviewQueueError, match="non-string Status"):
        review_queue.get_status("x")


def test_get_status_propagates_smartsheet_errors(get_rows_mock):
    from shared.smartsheet_client import SmartsheetError
    get_rows_mock.side_effect = SmartsheetError("HTTP 500: server error")

    with pytest.raises(SmartsheetError, match="500"):
        review_queue.get_status("x")


# ---- Item ID generation --------------------------------------------------


def test_item_id_format_is_workstream_yyyymmdd_hhmmss():
    item_id = review_queue._generate_item_id("safety_reports")
    parts = item_id.split("-")
    assert parts[0] == "safety_reports"
    assert len(parts) == 3
    # YYYYMMDD
    assert datetime.strptime(parts[1], "%Y%m%d")
    # HHMMSS
    assert datetime.strptime(parts[2], "%H%M%S")


def test_item_id_uses_utc(mocker):
    # Lock the time and verify the formatted Item ID matches UTC.
    fake_now = datetime(2026, 5, 18, 23, 45, 7, tzinfo=UTC)
    mocker.patch("shared.review_queue.datetime").now.return_value = fake_now

    item_id = review_queue._generate_item_id("po_materials")
    assert item_id == "po_materials-20260518-234507"


# ---- get_pending() -------------------------------------------------------


def test_get_pending_no_workstream_filter_is_status_only(get_rows_mock):
    get_rows_mock.return_value = []
    review_queue.get_pending()
    get_rows_mock.assert_called_once_with(
        sheet_ids.SHEET_REVIEW_QUEUE,
        filters={"Status": "PENDING"},
    )


def test_get_pending_with_workstream_adds_filter(get_rows_mock):
    get_rows_mock.return_value = []
    review_queue.get_pending(workstream="safety_reports")
    get_rows_mock.assert_called_once_with(
        sheet_ids.SHEET_REVIEW_QUEUE,
        filters={"Status": "PENDING", "Workstream": "safety_reports"},
    )


def test_get_pending_rejects_invalid_workstream(get_rows_mock):
    with pytest.raises(ValueError, match="not in"):
        review_queue.get_pending(workstream="not_a_real_workstream")
    get_rows_mock.assert_not_called()


def test_get_pending_propagates_smartsheet_errors(get_rows_mock):
    from shared.smartsheet_client import SmartsheetError
    get_rows_mock.side_effect = SmartsheetError("HTTP 500: server error")
    with pytest.raises(SmartsheetError, match="500"):
        review_queue.get_pending()


# ---- is_past_sla() -------------------------------------------------------


def test_4h_sla_same_day_not_past():
    today = date(2026, 5, 19)
    row = {"Created At": today.isoformat(), "SLA Tier": "4h"}
    assert review_queue.is_past_sla(row, now=today) is False


def test_4h_sla_next_day_past():
    row = {"Created At": "2026-05-18", "SLA Tier": "4h"}
    assert review_queue.is_past_sla(row, now=date(2026, 5, 19)) is True


def test_24h_sla_one_day_not_past():
    # Threshold is 1 day; need delta > 1 → strictly more than 1.
    row = {"Created At": "2026-05-18", "SLA Tier": "24h"}
    assert review_queue.is_past_sla(row, now=date(2026, 5, 19)) is False


def test_24h_sla_two_days_past():
    row = {"Created At": "2026-05-17", "SLA Tier": "24h"}
    assert review_queue.is_past_sla(row, now=date(2026, 5, 19)) is True


def test_48h_sla_three_days_not_past():
    row = {"Created At": "2026-05-16", "SLA Tier": "48h"}
    assert review_queue.is_past_sla(row, now=date(2026, 5, 19)) is False


def test_48h_sla_four_days_past():
    row = {"Created At": "2026-05-15", "SLA Tier": "48h"}
    assert review_queue.is_past_sla(row, now=date(2026, 5, 19)) is True


def test_unknown_sla_tier_raises_value_error():
    row = {"Created At": "2026-05-19", "SLA Tier": "bogus"}
    with pytest.raises(ValueError, match="unknown SLA tier"):
        review_queue.is_past_sla(row)


def test_missing_created_at_raises_key_error():
    row = {"SLA Tier": "4h"}
    with pytest.raises(KeyError, match="Created At"):
        review_queue.is_past_sla(row)


def test_missing_sla_tier_raises_key_error():
    row = {"Created At": "2026-05-19"}
    with pytest.raises(KeyError, match="SLA Tier"):
        review_queue.is_past_sla(row)


def test_invalid_iso_date_raises_value_error():
    row = {"Created At": "not-a-date", "SLA Tier": "4h"}
    with pytest.raises(ValueError):
        review_queue.is_past_sla(row)


def test_now_override_used():
    # Brief-specified case: Created At 4 days ago + 24h SLA + now override.
    row = {"Created At": "2026-05-15", "SLA Tier": "24h"}
    assert review_queue.is_past_sla(row, now=date(2026, 5, 19)) is True


# ---- safe_add — the never-raise fence ticket ---------------------------------------


def test_safe_add_returns_true_and_forwards_every_field(mocker):
    add = mocker.patch("shared.review_queue.add", return_value=123)
    ok = review_queue.safe_add(
        script_name="po_materials.rfq_poll",
        workstream="po_materials",
        summary="s",
        payload={"k": "v"},
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        source_file="rfq:2",
    )
    assert ok is True
    kw = add.call_args.kwargs
    assert kw["workstream"] == "po_materials"
    assert kw["summary"] == "s"
    assert kw["sla_tier"] is review_queue.SlaTier.RFQ_DRAFT
    assert kw["source_file"] == "rfq:2"


def test_safe_add_swallows_the_raise_and_fires_critical(mocker):
    """The whole point: `add` propagates by contract, and every fence writes its ticket
    BEFORE its one-shot flag — so a propagating ticket cost the flag and turned a
    PERMANENT fence into an unbounded per-cycle re-ticketing loop. safe_add must return
    (never raise) so the caller's flag write is reachable, and must escalate the lost
    ticket to CRITICAL — the item is fenced either way, so a silent loss would leave the
    operator with no signal at all."""
    mocker.patch(
        "shared.review_queue.add",
        side_effect=review_queue.smartsheet_client.SmartsheetError("flaked"),
    )
    log = mocker.patch("shared.review_queue.error_log.log")

    ok = review_queue.safe_add(
        script_name="po_materials.rfq_poll",
        workstream="po_materials",
        summary="RFQ-1 refused",
        payload={},
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        source_file="rfq:2",
        error_code="rfq_fence_ticket_failed",
    )

    assert ok is False  # the caller can branch; most just proceed to flag
    (severity, script, message), kwargs = log.call_args
    assert severity is Severity.CRITICAL
    assert script == "po_materials.rfq_poll"
    assert kwargs["error_code"] == "rfq_fence_ticket_failed"
    # The message must tell the operator the row's fate is UNKNOWN — a Smartsheet write
    # that raises may still have committed, so "re-ticket by hand" needs a check first.
    assert "rfq:2" in message and "non-idempotent" in message


def test_safe_add_swallows_even_a_non_smartsheet_error(mocker):
    """A ValueError from a bad workstream (or anything else) must not escape either —
    the flag write behind it is what keeps the fence one-shot."""
    mocker.patch("shared.review_queue.add", side_effect=ValueError("bad workstream"))
    log = mocker.patch("shared.review_queue.error_log.log")
    assert review_queue.safe_add(
        script_name="x", workstream="po_materials", summary="s", payload={},
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.POLICY_EDGE,
    ) is False
    assert log.call_args.args[0] is Severity.CRITICAL


def test_every_ticketing_daemon_workstream_is_in_the_valid_set():
    """STRUCTURAL parity — the latent-until-first-live-refusal class, twice now.

    review_queue.add validates `workstream` against VALID_WORKSTREAMS, and a daemon whose
    module-level WORKSTREAM is absent passes every mocked test and then loses its FIRST
    LIVE ticket to a ValueError (progress_reports at P5; field_ops on 2026-08-11 — the
    first real manifest's §34 refusal, Bradley 1 Customer BOM, fenced with no ticket and
    a CRITICAL). Enumerating the ticketing daemons here makes the NEXT one fail in CI by
    name instead. The tenant picklist column must carry each value too — that half is a
    Smartsheet column option, asserted live by §30, not here."""
    from field_ops import fieldops_sync, manifest_poll  # noqa: PLC0415
    from po_materials import estimate_poll, po_poll, rfq_poll  # noqa: PLC0415
    from subcontracts import subcontract_poll  # noqa: PLC0415

    for mod in (manifest_poll, fieldops_sync, estimate_poll, po_poll, rfq_poll, subcontract_poll):
        ws = mod.WORKSTREAM
        assert ws in review_queue.VALID_WORKSTREAMS, (
            f"{mod.__name__}.WORKSTREAM={ws!r} is not in review_queue.VALID_WORKSTREAMS — "
            "its first live ticket will be LOST (ValueError at add). Add it to BOTH "
            "VALID_WORKSTREAMS and picklist_validation._WORKSTREAM_VALUES_GLOBAL, and add "
            "the option to the live ITS_Review_Queue Workstream picklist."
        )

    from shared import picklist_validation  # noqa: PLC0415

    # The two sets have drifted apart before (the P4/P5 latency documented in both files).
    missing = review_queue.VALID_WORKSTREAMS - picklist_validation._WORKSTREAM_VALUES_GLOBAL
    assert not missing, f"VALID_WORKSTREAMS values absent from the write gate: {missing}"
