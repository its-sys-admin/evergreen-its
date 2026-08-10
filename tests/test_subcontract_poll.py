"""Unit tests for subcontracts/subcontract_poll.py — the multi-pass subcontract pull daemon.

Fully mocked (no live Smartsheet / Box / Worker): the data-plane seams
(portal_client subcontract calls, subcontractors, subcontract_log,
subcontract_review, render, Box) and the heartbeat / marker / flag-state seams are
patched. Faithful mirror of tests/test_po_poll.py with the subcontract deltas
(BUILD_DECISIONS): the sub:v1 HMAC verify (success + a tampered-hmac one-shot reject,
never rendered/filed/marked), the SOV-sums-to-price fence, the SoR-snapshot None fence,
the THREE-file Box filing (render_package dict → three uploads, the contract .docx id primary), the
agreement_ymd STABILITY regression (decision #1), the subcontractor down/up-sync passes,
and the status pass INCLUDING the operator-set `executed` transition (decision #4).

Run with: pytest -q tests/test_subcontract_poll.py
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from shared import picklist_validation, portal_client, portal_hmac, sheet_ids
from shared.box_client import BoxError
from shared.smartsheet_client import SmartsheetError
from subcontracts import (
    subcontract_log,
    subcontract_poll,
    subcontract_review,
    subcontractors,
)

SECRET = "sub-test-secret"
_PACIFIC = ZoneInfo("America/Los_Angeles")
CREATED_AT = 1_720_000_000  # immutable D1 unixepoch → the stable Pacific agreement date

CONTRACTOR: dict[str, Any] = {"entity": "Evergreen Renewables LLC"}

SUBCONTRACTOR_ROW: dict[str, Any] = {
    "_row_id": 100,
    subcontractors.COL_SUB_NAME: "Bright Spark Electric",
    subcontractors.COL_SUB_KEY: "SUB-000001",
    subcontractors.COL_ADDRESS: "12 Trade Way, Rockford IL",
    subcontractors.COL_CONTACT_NAME: "Sam Wire",
    subcontractors.COL_CONTACT_EMAIL: "sam@brightspark.example",
    subcontractors.COL_CONTACT_PHONE: "555-0101",
    subcontractors.COL_STATE: "VA",
    subcontractors.COL_ACTIVE: "Active",
}

# The degenerate corpus single-line SOV (qty=1, unit_price=contract_price) — sums to §2.1.
SOV_LINES: list[dict[str, Any]] = [
    {"position": 1, "item_number": "1", "description": "Lump-sum electrical scope",
     "qty": 1, "unit": "ls", "unit_price_cents": 1_000_000, "extended_cents": 1_000_000},
]


def _sub_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 42,
        "created_at": CREATED_AT,
        # ---- the 31 canonical-signed header keys ----
        "sc_number": "2026.001.2.0.0",
        "job_no": "2026.001",
        "site_phase": 2,
        "supersede_seq": 0,
        "revision": 0,
        "sub_key": "SUB-000001",
        "trade": "Electrical",
        "job_id": "JOB-000042",
        "job_name": "Sunrise Solar",
        "project_name": "Sunrise Solar Project",
        "owner_entity": "Sunrise Owner LLC",
        "prime_contractor": "Evergreen Renewables LLC",
        "site_name": "Sunrise Field",
        "site_address": "100 Array Rd, Rockford IL",
        "governing_law_state": "VA",
        "exhibit_a_template_id": "",
        "exhibit_a_template_version": "",
        "exhibit_a_work_text": "",
        "scope_summary": "Electrical scope",
        "price_basis": "fixed",
        "contract_price_cents": 1_000_000,
        "retainage_bp": 1_000,
        "subtotal_cents": 1_000_000,
        "start_date": "2026-08-01",
        "completion_date": "2026-10-01",
        "terms_profile_id": "standard_subcontract",
        "terms_version": "1",
        "template_family": "standard",
        "supersedes_sc_id": None,
        "approver_name": "Alex Approver",
        "approver_title": "Director",
        # ---- render-only / transport (NOT canonical) ----
        "created_by": "admin.alex",
        "sov_lines": [dict(line) for line in SOV_LINES],
    }
    base.update(over)
    return base


def _signed_row(**over: Any) -> dict[str, Any]:
    """A pending row whose hmac verifies — signed EXACTLY as the Worker would."""
    row = _sub_row(**over)
    canonical = portal_hmac.sub_canonical_json(row, row["sov_lines"])
    row["hmac"] = portal_hmac.sign_sub(
        SECRET, sc_id=row["id"], sc_number=str(row["sc_number"]), canonical_json=canonical
    )
    return row


@pytest.fixture
def _patch(mocker):
    seams = {
        # Gates: drafts ON, subcontractors/status OFF by default (tests flip per case).
        "gate_drafts": mocker.patch(
            "subcontracts.subcontract_poll._polling_enabled", return_value=True
        ),
        "gate_subs": mocker.patch(
            "subcontracts.subcontract_poll._subs_sync_enabled", return_value=False
        ),
        "gate_status": mocker.patch(
            "subcontracts.subcontract_poll._status_sync_enabled", return_value=False
        ),
        "resolve_cfg": mocker.patch(
            "subcontracts.subcontract_poll.resolve_and_log", return_value={}
        ),
        "creds": mocker.patch(
            "subcontracts.subcontract_poll._resolve_credentials",
            return_value=subcontract_poll._SubcontractCreds(
                base_url="https://portal.example", bearer="tok", secret=SECRET
            ),
        ),
        # Worker I/O.
        "pending": mocker.patch(
            "subcontracts.subcontract_poll.portal_client.get_pending_subcontracts",
            return_value=[],
        ),
        "mark_filed": mocker.patch(
            "subcontracts.subcontract_poll.portal_client.mark_subcontract_filed",
            return_value=True,
        ),
        "subs_sync": mocker.patch(
            "subcontracts.subcontract_poll.portal_client.subcontractors_sync",
            return_value={"ok": True, "upserted": 1, "skipped_dirty": 0},
        ),
        "subs_pending": mocker.patch(
            "subcontracts.subcontract_poll.portal_client.get_pending_subcontractors",
            return_value=[],
        ),
        "mark_mirrored": mocker.patch(
            "subcontracts.subcontract_poll.portal_client.mark_subcontractors_mirrored",
            return_value={"ok": True, "flipped": 1, "stale": 0},
        ),
        "status_sync": mocker.patch(
            "subcontracts.subcontract_poll.portal_client.subcontract_status_sync",
            return_value={"ok": True, "updated": 1},
        ),
        # Config files.
        "contractor": mocker.patch(
            "subcontracts.subcontract_poll.terms_lib.load_contractor_config",
            return_value=CONTRACTOR,
        ),
        # Subcontractors / render / Box / ledger / review seams.
        "get_sub": mocker.patch(
            "subcontracts.subcontract_poll.subcontractors.get_subcontractor_by_key",
            return_value=SUBCONTRACTOR_ROW,
        ),
        "down_payload": mocker.patch(
            "subcontracts.subcontract_poll.subcontractors.build_down_sync_payload",
            return_value=subcontractors.DownSyncPayload(subcontractors=[], skipped=[]),
        ),
        "upsert_sub": mocker.patch(
            "subcontracts.subcontract_poll.subcontractors.upsert_subcontractor",
            return_value=777,
        ),
        "render": mocker.patch(
            "subcontracts.subcontract_poll.subcontract_docx.render_package",
            return_value={
                "Subcontract.docx": b"%DOCX-fake",
                "Exhibit A.docx": b"%EXHIBIT-fake",
                "Annex C - Schedule of Values.xlsx": b"XLSX-fake",
            },
        ),
        "box_folder": mocker.patch(
            "subcontracts.subcontract_poll._resolve_subcontract_box_folder",
            return_value="folder-1",
        ),
        "upload": mocker.patch(
            "subcontracts.subcontract_poll.box_client.upload_bytes_or_new_version",
            return_value={"id": "f-9", "name": "Sunrise Solar_Subcontract_2026.001.2.0.0.docx",
                          "size": 9},
        ),
        # subcontract_log patched at the ITS module so numbering.check_collision (late
        # import) sees the same mocks.
        "log_find": mocker.patch(
            "subcontracts.subcontract_log.find_row_by_sc_number", return_value=None
        ),
        "log_append": mocker.patch(
            "subcontracts.subcontract_log.append_filed_row", return_value=1
        ),
        "log_stamp": mocker.patch(
            "subcontracts.subcontract_log.stamp_status", return_value=True
        ),
        "log_by_d1": mocker.patch(
            "subcontracts.subcontract_log.find_sc_number_by_d1_id", return_value=None
        ),
        "review_find": mocker.patch(
            "subcontracts.subcontract_poll.subcontract_review.find_row_by_sc_id",
            return_value=None,
        ),
        "review_add": mocker.patch(
            "subcontracts.subcontract_poll.subcontract_review.add_sc_review_row",
            return_value=321,
        ),
        "attach": mocker.patch(
            "subcontracts.subcontract_poll._attach_files_best_effort", return_value=None
        ),
        "perjob": mocker.patch(
            "subcontracts.subcontract_poll._append_perjob_row_best_effort",
            return_value=None,
        ),
        # Fences + observability.
        "review_q": mocker.patch(
            "subcontracts.subcontract_poll.review_queue.add", return_value=1
        ),
        "anomaly": mocker.patch(
            "subcontracts.subcontract_poll.anomaly_logger.check", return_value=None
        ),
        "log": mocker.patch("subcontracts.subcontract_poll.error_log.log", return_value=None),
        "hb": mocker.patch("subcontracts.subcontract_poll._write_heartbeat", return_value=None),
        "hb_row": mocker.patch(
            "subcontracts.subcontract_poll._write_heartbeat_row", return_value=None
        ),
        "marker": mocker.patch(
            "subcontracts.subcontract_poll._write_watchdog_marker", return_value=None
        ),
        "flags_load": mocker.patch(
            "subcontracts.subcontract_poll._load_flags", return_value={}
        ),
        "flags_persist": mocker.patch(
            "subcontracts.subcontract_poll._persist_flags", return_value=None
        ),
        "circuit": mocker.patch(
            "subcontracts.subcontract_poll.circuit_breaker.is_open", return_value=False
        ),
        # Status-pass sheet reads (review sheet + ledger) — per-test side_effect.
        "get_rows": mocker.patch(
            "subcontracts.subcontract_poll.smartsheet_client.get_rows", return_value=[]
        ),
    }
    return seams


def _run(_patch) -> subcontract_poll.SubcontractPollStats:
    return subcontract_poll._poll_inside_lock(
        _patch["gate_drafts"].return_value,
        _patch["gate_subs"].return_value,
        _patch["gate_status"].return_value,
    )


def _logged_codes(_patch) -> list[str]:
    return [kw.get("error_code") for _, kw in _patch["log"].call_args_list]


# ---- gates ----------------------------------------------------------------------


def test_all_gates_off_is_noop(_patch):
    _patch["gate_drafts"].return_value = False
    stats = subcontract_poll.poll_once()
    assert stats.skipped_disabled is True
    _patch["pending"].assert_not_called()
    _patch["hb"].assert_not_called()
    _patch["marker"].assert_not_called()


# ---- drafts pass: happy path ------------------------------------------------------


def test_happy_path_verifies_files_and_receipts_last(_patch):
    _patch["pending"].return_value = [_signed_row()]

    stats = _run(_patch)

    assert stats.filed == 1
    assert stats.rejected == 0 and stats.fenced == 0 and stats.draft_errors == 0
    _patch["render"].assert_called_once()
    _patch["log_append"].assert_called_once()
    _patch["review_add"].assert_called_once()
    _patch["attach"].assert_called_once()
    # The receipt is LAST and carries the structural Box file id (the .docx id).
    _patch["mark_filed"].assert_called_once()
    _, mark_kwargs = _patch["mark_filed"].call_args
    assert mark_kwargs["sc_id"] == 42 and mark_kwargs["box_file_id"] == "f-9"
    # Review row maps the protocol slots off the SoR subcontractor + contractor config.
    _, add_kwargs = _patch["review_add"].call_args
    assert add_kwargs["sub_key"] == "SUB-000001"
    assert add_kwargs["recipient_to"] == "sam@brightspark.example"
    assert "sc_id=42" in add_kwargs["notes"]
    # Compiled-PDF slot = the Subcontract.docx Box link (BUILD_DECISIONS #3 — no PDF render).
    assert add_kwargs["package_link"] == "https://app.box.com/file/f-9"
    _patch["hb"].assert_called_once()
    _patch["marker"].assert_called_once()


def test_subcontractor_snapshot_resolved_from_sor_at_render_time(_patch):
    """#494: the render consumes the ITS_Subcontractors row (the SoR), keyed by the
    HMAC-covered sub_key — the identity is never taken from the D1 cache. Migration 0050
    has no subcontractor_entity column, so the poll injects it before render."""
    _patch["pending"].return_value = [_signed_row()]
    _run(_patch)
    _patch["get_sub"].assert_called_once_with("SUB-000001")
    (sub_arg, lines_arg), render_kwargs = _patch["render"].call_args
    assert sub_arg["subcontractor_entity"] == "Bright Spark Electric"
    assert render_kwargs["terms_profile_id"] == "standard_subcontract"


def test_four_file_box_filing_zip_is_send_artifact_docx_is_primary(_patch):
    """render_package returns THREE editable files → THREE Box uploads (contract .docx + Exhibit A
    .docx + .xlsx SOV), PLUS a FOURTH upload: the combined Subcontract Package.zip (SC-S4 send
    artifact, 2026-07-15). The contract .docx id stays the primary ledger receipt
    (BUILD_DECISIONS #3), but the review row's "Compiled PDF" link points at the ZIP so the send
    engine transmits the whole package."""
    _patch["upload"].side_effect = [
        {"id": "docx-1", "name": "x.docx", "size": 9},
        {"id": "exhibit-2", "name": "e.docx", "size": 9},
        {"id": "xlsx-3", "name": "x.xlsx", "size": 9},
        {"id": "zip-4", "name": "pkg.zip", "size": 20},
    ]
    _patch["pending"].return_value = [_signed_row()]

    stats = _run(_patch)

    assert stats.filed == 1
    assert _patch["upload"].call_count == 4
    (c1, c2, c3, c4) = _patch["upload"].call_args_list
    (folder1, name1, bytes1), _ = c1
    (_, name2, bytes2), _ = c2
    (_, name3, bytes3), _ = c3
    (_, name4, bytes4), _ = c4
    assert folder1 == "folder-1" and "Subcontract" in name1 and bytes1 == b"%DOCX-fake"
    assert "Exhibit A" in name2 and bytes2 == b"%EXHIBIT-fake"
    assert name3.endswith(".xlsx") and bytes3 == b"XLSX-fake"
    # The 4th upload is the deterministic combined ZIP package (real bytes from zip_package).
    assert name4.endswith(".zip") and "Subcontract Package" in name4 and bytes4[:2] == b"PK"
    assert all("2026.001.2.0.0" in n for n in (name1, name2, name3, name4))
    # The contract .docx id (FIRST upload) is the ledger receipt (unchanged).
    _, mark_kwargs = _patch["mark_filed"].call_args
    assert mark_kwargs["box_file_id"] == "docx-1"
    # But the review row's "Compiled PDF" (the send source) links the ZIP, not the docx.
    _, review_kwargs = _patch["review_add"].call_args
    assert review_kwargs["package_link"] == "https://app.box.com/file/zip-4"


def test_agreement_ymd_is_stable_across_re_renders(_patch):
    """BUILD_DECISIONS #1: agreement_ymd derives from the immutable created_at (Pacific
    calendar date), NOT datetime.now() — two renders of the same row produce the same
    date, keeping the §47 OOXML-clock idempotency byte-identical."""
    expected_date = datetime.fromtimestamp(CREATED_AT, _PACIFIC).date()
    expected_ymd = [expected_date.year, expected_date.month, expected_date.day]

    _patch["pending"].return_value = [_signed_row()]
    _run(_patch)
    (sub_arg_1, _lines_1), _ = _patch["render"].call_args
    ymd_1 = list(sub_arg_1["agreement_ymd"])

    _patch["render"].reset_mock()
    _patch["pending"].return_value = [_signed_row()]
    _run(_patch)
    (sub_arg_2, _lines_2), _ = _patch["render"].call_args
    ymd_2 = list(sub_arg_2["agreement_ymd"])

    assert ymd_1 == expected_ymd
    assert ymd_2 == expected_ymd  # stable across re-renders — derived from created_at, not now()


def test_created_at_missing_fences(_patch):
    """A /pending row missing a usable created_at is a Worker SELECT defect — fence rather
    than render with a non-deterministic date (BUILD_DECISIONS #1)."""
    _patch["pending"].return_value = [_signed_row(created_at=None)]
    stats = _run(_patch)
    assert stats.fenced == 1 and stats.filed == 0
    _patch["render"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    assert "subcontract_created_at_missing" in _logged_codes(_patch)


# ---- drafts pass: refusals ---------------------------------------------------------


def test_bad_hmac_is_one_shot_rejected_never_filed_never_marked(_patch):
    row = _signed_row()
    row["hmac"] = "0" * 64  # tampered/forged signature
    _patch["pending"].return_value = [row]

    stats = _run(_patch)

    assert stats.rejected == 1 and stats.filed == 0
    _patch["render"].assert_not_called()
    _patch["upload"].assert_not_called()
    _patch["log_append"].assert_not_called()
    _patch["review_add"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    # CRITICAL + security-flagged Review-Queue row + anomaly tripwire.
    _patch["anomaly"].assert_called_once()
    _, rq_kwargs = _patch["review_q"].call_args
    assert rq_kwargs["security_flag"] is True
    assert "subcontract_hmac_failure" in _logged_codes(_patch)
    # One-shot flag persisted.
    _patch["flags_persist"].assert_called_once()
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"42": "hmac"}


def test_tampered_field_fails_hmac(_patch):
    """A single mutated signed field (the money) breaks verification."""
    row = _signed_row()
    row["contract_price_cents"] = 999_999_99  # tamper AFTER signing
    _patch["pending"].return_value = [row]
    stats = _run(_patch)
    assert stats.rejected == 1
    _patch["mark_filed"].assert_not_called()


def test_flagged_row_is_skipped_silently(_patch):
    _patch["flags_load"].return_value = {"42": "hmac"}
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.skipped_flagged == 1 and stats.rejected == 0
    _patch["review_q"].assert_not_called()
    _patch["mark_filed"].assert_not_called()


def test_sov_mismatch_fences_never_files(_patch):
    """HMAC-valid but the SOV recompute doesn't sum to the signed §2.1 Contract Price →
    fence + one-shot flag; never file, never mark (BUILD_DECISIONS #7)."""
    # Sign OVER a contract price the SOV lines (1_000_000) do NOT sum to, so the HMAC
    # passes and only the recompute refuses.
    row = _signed_row(contract_price_cents=999_999, subtotal_cents=999_999)
    _patch["pending"].return_value = [row]

    stats = _run(_patch)

    assert stats.fenced == 1 and stats.filed == 0
    _patch["upload"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    _, rq_kwargs = _patch["review_q"].call_args
    assert rq_kwargs["reason"] == subcontract_poll.review_queue.ReviewReason.MISMATCHED_REFERENCE
    assert "subcontract_sov_mismatch" in _logged_codes(_patch)
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"42": "sov"}


def test_malformed_signed_field_fences_one_row_without_aborting_the_batch(_patch):
    """An HMAC-valid row carrying a malformed numeric field (a Worker bug / schema drift —
    the HMAC proves the Worker signed THIS value, not that it is well-typed) must FENCE
    one-shot via the SOV guard (the FIRST guard, outside the per-row fence's try), NOT
    raise. Two-row batch: the malformed row fences, the clean row STILL files, and the
    cycle completes (heartbeat + marker written)."""
    bad = _signed_row(id=42, sc_number="2026.001.2.0.0",
                       sov_lines=[{**SOV_LINES[0], "unit_price_cents": "bad-not-a-number"}])
    good = _signed_row(id=43, sc_number="2026.001.2.0.1")
    _patch["pending"].return_value = [bad, good]

    stats = _run(_patch)

    # One fenced, one filed — the malformed row did NOT abort the batch.
    assert stats.fenced == 1 and stats.filed == 1
    assert "subcontract_sov_mismatch" in _logged_codes(_patch)
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"42": "sov"}
    # The clean row genuinely filed + receipted.
    _patch["mark_filed"].assert_called_once()
    assert _patch["mark_filed"].call_args.kwargs["sc_id"] == 43
    # The cycle COMPLETED — heartbeat + watchdog marker written (a crash would skip them).
    _patch["hb"].assert_called()
    _patch["marker"].assert_called()


def test_sc_number_collision_fences(_patch):
    """A Subcontract_Log row with this number that is NOT ours (hand-issued in transition)
    refuses the filing — never a duplicate contractual number."""
    _patch["log_find"].return_value = {"_row_id": 1, subcontract_log.COL_NOTES: "hand-issued"}
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.fenced == 1
    _patch["upload"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    assert "subcontract_number_collision" in _logged_codes(_patch)


def test_unknown_subcontractor_fences(_patch):
    """SoR-snapshot None → fence (the identity must come from ITS_Subcontractors, never
    the D1 cache)."""
    _patch["get_sub"].return_value = None
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.fenced == 1
    _patch["render"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    assert "subcontract_sub_unknown" in _logged_codes(_patch)


def test_transient_box_failure_leaves_queued_and_never_marks(_patch):
    """mark-filed fires ONLY after a successful file — a Box blip leaves the row queued
    (no flag, no Review Queue) and the next cycle retries."""
    _patch["upload"].side_effect = BoxError("box 503")
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.draft_errors == 1 and stats.filed == 0 and stats.fenced == 0
    _patch["mark_filed"].assert_not_called()
    _patch["review_q"].assert_not_called()
    _patch["flags_persist"].assert_not_called()


def test_crash_retry_is_idempotent(_patch):
    """A re-pulled row whose Subcontract_Log + review rows already exist (a lost receipt)
    re-uploads a byte-identical version and ONLY re-posts the receipt."""
    _patch["log_find"].return_value = {
        "_row_id": 1, subcontract_log.COL_NOTES: subcontract_log.notes_for_filed_row(42),
    }
    _patch["review_find"].return_value = {"_row_id": 321, subcontract_review.COL_NOTES: "sc_id=42"}
    _patch["pending"].return_value = [_signed_row()]

    stats = _run(_patch)

    assert stats.filed == 1
    _patch["log_append"].assert_not_called()
    _patch["review_add"].assert_not_called()
    _patch["mark_filed"].assert_called_once()


def test_unresolved_box_root_leaves_queued(_patch):
    _patch["box_folder"].return_value = None
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.draft_errors == 1 and stats.filed == 0
    _patch["mark_filed"].assert_not_called()
    assert "subcontract_box_root_unresolved" in _logged_codes(_patch)


# ---- cycle-level failure modes -----------------------------------------------------


def test_no_creds_fail_closed(_patch):
    _patch["creds"].return_value = None
    stats = _run(_patch)
    assert stats.halted_no_creds is True
    _patch["pending"].assert_not_called()
    assert "subcontract_creds_missing" in _logged_codes(_patch)
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "ERROR"


def test_transient_base_url_warns_and_skips_without_paging(_patch):
    # A Smartsheet blip on the base-URL read is NOT a missing credential. Live on
    # 2026-07-20 04:42Z a single GET failure fell back to "" and fired a CRITICAL
    # saying portal credentials were unset; both Keychain entries were fine and the
    # daemon self-healed a cycle later. That page was false, and it aimed the §43
    # repair at re-provisioning secrets (a high-capability-class action) at a
    # condition needing none. Transient => WARN + skip, never the misconfig CRITICAL.
    _patch["creds"].return_value = subcontract_poll.TransientUnavailable(
        reason="SmartsheetError: (<PreparedRequest [GET]>, None)"
    )
    stats = _run(_patch)
    assert stats.halted_transient is True
    assert stats.halted_no_creds is False
    _patch["pending"].assert_not_called()  # still FAIL-CLOSED — it does not poll
    codes = _logged_codes(_patch)
    assert "subcontract_creds_transient" in codes
    assert "subcontract_creds_missing" not in codes, "a transient read failure must NOT page"
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "WARN"  # not ERROR — nothing is misconfigured


def test_bearer_rejected_stops_cycle(_patch):
    _patch["gate_subs"].return_value = True  # would run after drafts — must not
    _patch["pending"].side_effect = portal_client.PortalAuthError("401")
    stats = _run(_patch)
    assert stats.bearer_rejected is True
    _patch["down_payload"].assert_not_called()  # the cycle stopped at the 401
    assert "subcontract_bearer_rejected" in _logged_codes(_patch)
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "ERROR"


def test_transient_pending_fetch_still_writes_heartbeat(_patch):
    _patch["pending"].side_effect = portal_client.PortalTransportError("blip")
    stats = _run(_patch)
    assert stats.draft_errors == 1
    _patch["hb"].assert_called_once()
    _patch["marker"].assert_called_once()


# ---- ② subcontractor down-sync -----------------------------------------------------


def test_down_sync_posts_full_projection(_patch):
    _patch["gate_drafts"].return_value = False
    _patch["gate_subs"].return_value = True
    projection = [{"sub_key": "SUB-000001", "subcontractor_name": "Bright Spark", "active": 1}]
    _patch["down_payload"].return_value = subcontractors.DownSyncPayload(
        subcontractors=projection, skipped=[(55, "malformed sub_key ''")],
    )
    stats = _run(_patch)
    assert stats.subs_downsynced == 1
    (base, tok, sent), _ = _patch["subs_sync"].call_args
    assert sent == projection
    assert "subcontract_sub_row_skipped" in _logged_codes(_patch)


def test_down_sync_refuses_empty_projection(_patch):
    """An empty ITS_Subcontractors projection must never be POSTed — a read-miss would
    otherwise wipe the portal cache."""
    _patch["gate_drafts"].return_value = False
    _patch["gate_subs"].return_value = True
    _patch["down_payload"].return_value = subcontractors.DownSyncPayload(
        subcontractors=[], skipped=[]
    )
    _run(_patch)
    _patch["subs_sync"].assert_not_called()
    assert "subcontract_subs_empty_projection" in _logged_codes(_patch)


# ---- ③ subcontractor up-sync -------------------------------------------------------


def _portal_subcontractor(**over: Any) -> dict[str, Any]:
    base = {
        "sub_key": "SUB-000002", "subcontractor_name": "Volt Works", "address": "",
        "contact_name": "", "contact_email": "", "contact_phone": "",
        "state": "VA", "trades": ["Electrical"],
        "default_terms_profile": "standard_subcontract", "active": 1, "notes": "",
        "origin": "portal", "mirror_version": 3, "mirrored_version": 1,
    }
    base.update(over)
    return base


def test_up_sync_upserts_then_marks_with_read_watermark(_patch):
    _patch["gate_drafts"].return_value = False
    _patch["gate_subs"].return_value = True
    _patch["subs_pending"].return_value = [_portal_subcontractor()]
    stats = _run(_patch)
    assert stats.subs_upsynced == 1
    _patch["upsert_sub"].assert_called_once()
    (base, tok, updates), _ = _patch["mark_mirrored"].call_args
    assert updates == [{"sub_key": "SUB-000002", "mirrored_version": 3}]


def test_up_sync_permanent_failure_fences_and_never_marks(_patch):
    _patch["gate_drafts"].return_value = False
    _patch["gate_subs"].return_value = True
    _patch["subs_pending"].return_value = [_portal_subcontractor()]
    _patch["upsert_sub"].side_effect = picklist_validation.PicklistViolationError(
        1, "State", "ZZ", frozenset({"VA"})
    )
    stats = _run(_patch)
    assert stats.subs_reviewed == 1 and stats.subs_upsynced == 0
    _patch["mark_mirrored"].assert_not_called()
    _patch["review_q"].assert_called_once()


def test_up_sync_malformed_watermark_skips(_patch):
    _patch["gate_drafts"].return_value = False
    _patch["gate_subs"].return_value = True
    _patch["subs_pending"].return_value = [_portal_subcontractor(mirror_version="three")]
    stats = _run(_patch)
    assert stats.sub_errors == 1
    _patch["upsert_sub"].assert_not_called()
    _patch["mark_mirrored"].assert_not_called()


# ---- ④ status pass ------------------------------------------------------------------


def _review_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "_row_id": 400,
        subcontract_review.COL_WORKSTREAM: "subcontracts",
        subcontract_review.COL_NOTES: "sc_id=42; sc_number=2026.100.2.1.0",
        subcontract_review.COL_SEND_STATUS: subcontract_review.STATUS_PENDING,
        subcontract_review.COL_APPROVE_SCHEDULED: False,
        subcontract_review.COL_SEND_NOW: False,
        subcontract_review.COL_APPROVED_BY: None,
        subcontract_review.COL_SENT_AT: None,
    }
    base.update(over)
    return base


def _ledger_row(status: str, number: str = "2026.100.2.1.0") -> dict[str, Any]:
    return {
        "_row_id": 500,
        subcontract_log.COL_SC_NUMBER: number,
        subcontract_log.COL_STATUS: status,
    }


def _arm_status_pass(_patch, review_rows, ledger_rows) -> None:
    _patch["gate_drafts"].return_value = False
    _patch["gate_status"].return_value = True
    _patch["get_rows"].side_effect = [review_rows, ledger_rows]


def test_status_pass_sent_row_walks_machine_in_order_and_stamps_ledger(_patch):
    sent_row = _review_row(
        **{
            subcontract_review.COL_SEND_STATUS: subcontract_review.STATUS_SENT,
            subcontract_review.COL_SENT_AT: "2026-07-10T09:00:00",
            subcontract_review.COL_NOTES: "sc_id=42; sc_number=2026.100.2.1.0; supersedes_sc_id=5",
        }
    )
    _arm_status_pass(_patch, [sent_row], [_ledger_row("approved")])
    _patch["log_by_d1"].return_value = "2026.100.1.0.0"

    stats = _run(_patch)

    assert stats.status_synced == 1
    (base, tok, updates), _ = _patch["status_sync"].call_args
    # Ordered approved-then-sent for the same subcontract (the Worker's guarded batch).
    assert updates == [
        {"sc_id": 42, "status": "approved"},
        {"sc_id": 42, "status": "sent"},
    ]
    stamp_calls = _patch["log_stamp"].call_args_list
    assert stamp_calls[0].args == ("2026.100.2.1.0", subcontract_log.STATUS_SENT)
    assert stamp_calls[0].kwargs == {"sent_at_iso": "2026-07-10"}
    # The superseded mirror onto the predecessor (resolved via the d1_id join).
    assert stamp_calls[1].args == ("2026.100.1.0.0", subcontract_log.STATUS_SUPERSEDED)
    assert stamp_calls[1].kwargs == {"superseded_by": "2026.100.2.1.0"}


def test_status_pass_approved_only_row(_patch):
    approved = _review_row(**{subcontract_review.COL_APPROVED_BY: "buyer@evergreen.example"})
    _arm_status_pass(_patch, [approved], [_ledger_row(subcontract_log.STATUS_PENDING_REVIEW)])
    stats = _run(_patch)
    assert stats.status_synced == 1
    (base, tok, updates), _ = _patch["status_sync"].call_args
    assert updates == [{"sc_id": 42, "status": "approved"}]
    _patch["log_stamp"].assert_called_once_with("2026.100.2.1.0", subcontract_log.STATUS_APPROVED)


def test_status_pass_executed_mirrors_terminal_without_ledger_stamp(_patch):
    """BUILD_DECISIONS #4: an operator-set `executed` Status on Subcontract_Log has no
    portal signal — the status pass mirrors it into the D1 display cache (guarded
    server-side from `sent`), with NO ledger stamp (the ledger is the source here)."""
    executed = _review_row(**{subcontract_review.COL_SEND_STATUS: subcontract_review.STATUS_SENT})
    _arm_status_pass(_patch, [executed], [_ledger_row(subcontract_log.STATUS_EXECUTED)])
    _run(_patch)
    (base, tok, updates), _ = _patch["status_sync"].call_args
    assert updates == [{"sc_id": 42, "status": "executed"}]
    _patch["log_stamp"].assert_not_called()


def test_status_pass_settled_ledger_rows_generate_no_post(_patch):
    sent_row = _review_row(**{subcontract_review.COL_SEND_STATUS: subcontract_review.STATUS_SENT})
    _arm_status_pass(_patch, [sent_row], [_ledger_row(subcontract_log.STATUS_SENT)])
    stats = _run(_patch)
    assert stats.status_synced == 0
    _patch["status_sync"].assert_not_called()
    _patch["log_stamp"].assert_not_called()


def test_status_pass_post_failure_defers_ledger_stamps(_patch):
    """D1 first, ledger second: a failed POST defers the Subcontract_Log stamps whole —
    the next cycle retries both (never a ledger ahead of D1)."""
    sent_row = _review_row(**{subcontract_review.COL_SEND_STATUS: subcontract_review.STATUS_SENT})
    _arm_status_pass(_patch, [sent_row], [_ledger_row(subcontract_log.STATUS_APPROVED)])
    _patch["status_sync"].side_effect = portal_client.PortalTransportError("blip")
    stats = _run(_patch)
    assert stats.status_errors == 1 and stats.status_synced == 0
    _patch["log_stamp"].assert_not_called()


def test_status_pass_ignores_foreign_workstream_rows(_patch):
    foreign = _review_row(**{subcontract_review.COL_WORKSTREAM: "safety"})
    _arm_status_pass(_patch, [foreign], [_ledger_row(subcontract_log.STATUS_PENDING_REVIEW)])
    _run(_patch)
    _patch["status_sync"].assert_not_called()
    assert "subcontract_status_foreign_tag" in _logged_codes(_patch)


# ---- per-job tracking sheet mirror (Feature A) -------------------------------------

# The REAL helper, captured at import time (the `_patch` fixture replaces the module
# attribute) — used by the end-to-end fence test below.
_REAL_PERJOB = subcontract_poll._append_perjob_row_best_effort


def test_happy_path_mirrors_ledger_row_to_perjob_sheet(_patch):
    """The filing path hands the SAME ledger row kwargs to the per-job mirror,
    keyed by the job name (the Box per-job folder's name source)."""
    _patch["pending"].return_value = [_signed_row()]

    _run(_patch)

    _patch["perjob"].assert_called_once()
    job_name, row_kwargs, _corr = _patch["perjob"].call_args.args
    assert job_name == "Sunrise Solar"
    assert row_kwargs["sc_number"] == "2026.001.2.0.0"
    assert row_kwargs["notes"] == "d1_id=42"  # the §19 join rides the mirror row too
    # The mirror is best-effort ON TOP of the flat append — both fired.
    _patch["log_append"].assert_called_once()


def test_perjob_failure_never_fails_the_filing(_patch, mocker):
    """END-TO-END fence proof: run the REAL helper with ensure_job_sheet raising —
    the filing still completes, the receipt still posts, and the stable WARN
    error_code is logged (Box + the flat Subcontract_Log are the SoR)."""
    _patch["perjob"].side_effect = _REAL_PERJOB
    mocker.patch(
        "subcontracts.subcontract_poll.job_sheet.ensure_job_sheet",
        side_effect=SmartsheetError("boom"),
    )
    _patch["pending"].return_value = [_signed_row()]

    stats = _run(_patch)

    assert stats.filed == 1
    assert stats.fenced == 0 and stats.draft_errors == 0
    _patch["mark_filed"].assert_called_once()
    assert "subcontract_perjob_sheet_failed" in _logged_codes(_patch)


def test_perjob_helper_ensures_and_appends_to_target_sheet(mocker):
    """The helper wires FOLDER_SC_JOBS + the flat Log as template + the sanitized
    job folder name + the fixed sheet name, then appends with sheet_id=<per-job>."""
    ensure = mocker.patch(
        "subcontracts.subcontract_poll.job_sheet.ensure_job_sheet", return_value=555
    )
    find = mocker.patch(
        "subcontracts.subcontract_log.find_row_by_sc_number", return_value=None
    )
    append = mocker.patch(
        "subcontracts.subcontract_log.append_filed_row", return_value=1
    )
    row_kwargs = {"sc_number": "2026.001.2.0.0", "job_project": "2026.001 — Sunrise Solar"}

    subcontract_poll._append_perjob_row_best_effort("Sunrise Solar", row_kwargs, "corr-1")

    ensure.assert_called_once_with(
        sheet_ids.FOLDER_SC_JOBS,
        sheet_ids.SHEET_SUBCONTRACT_LOG,
        "Sunrise Solar",
        subcontract_poll.PERJOB_SHEET_NAME,
        workspace_id=sheet_ids.WORKSPACE_SUBCONTRACTS,  # §51 A1 margin-check target
        workstream=subcontract_poll.WORKSTREAM,
        correlation_id="corr-1",
    )
    find.assert_called_once_with("2026.001.2.0.0", sheet_id=555)
    append.assert_called_once_with(sheet_id=555, **row_kwargs)


def test_perjob_helper_is_idempotent_against_target_sheet(mocker):
    """A crash between the flat append and the mirror re-runs cleanly: the SC number
    already present in the TARGET sheet suppresses the duplicate append."""
    mocker.patch(
        "subcontracts.subcontract_poll.job_sheet.ensure_job_sheet", return_value=555
    )
    mocker.patch(
        "subcontracts.subcontract_log.find_row_by_sc_number",
        return_value={"_row_id": 1},
    )
    append = mocker.patch("subcontracts.subcontract_log.append_filed_row")

    subcontract_poll._append_perjob_row_best_effort(
        "Sunrise Solar", {"sc_number": "2026.001.2.0.0"}, "corr-1"
    )

    append.assert_not_called()


def test_perjob_helper_fences_generic_exception(mocker):
    """The fence is broad (mirror the best-effort attach idiom): even a non-Smartsheet
    exception is reduced to the WARN error_code, never raised to the filing path."""
    mocker.patch(
        "subcontracts.subcontract_poll.job_sheet.ensure_job_sheet",
        side_effect=RuntimeError("weird"),
    )
    log = mocker.patch("subcontracts.subcontract_poll.error_log.log")

    subcontract_poll._append_perjob_row_best_effort(
        "Sunrise Solar", {"sc_number": "2026.001.2.0.0"}, "corr-1"
    )  # must not raise

    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "subcontract_perjob_sheet_failed" in codes


# ---- config-read transient fence (error-hygiene 2026-07-19) ---------------------


def test_read_str_setting_transient_error_falls_open_with_warn(mocker):
    """A generic SmartsheetError from get_setting (read-timeout / 5xx) must NOT escape
    to @its_error_log as a spurious CRITICAL — WARN `config_read_error` + fallback,
    same disposition as the circuit-open branch."""
    mocker.patch(
        "subcontracts.subcontract_poll.smartsheet_client.get_setting",
        side_effect=SmartsheetError("read timeout"),
    )
    log = mocker.patch("subcontracts.subcontract_poll.error_log.log")

    result = subcontract_poll._read_str_setting("subcontracts.some_key", "fallback-val")  # must not raise

    assert result == "fallback-val"
    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "config_read_error" in codes


def test_polling_gate_transient_error_resolves_to_default(mocker):
    """Cycle-entry proof: the polling gate read survives a transient and resolves to the
    ships-dark default (False) instead of crashing the cycle."""
    mocker.patch(
        "subcontracts.subcontract_poll.smartsheet_client.get_setting",
        side_effect=SmartsheetError("HTTP 502"),
    )
    mocker.patch("subcontracts.subcontract_poll.error_log.log")

    assert subcontract_poll._polling_enabled() is subcontract_poll.DEFAULT_POLLING_ENABLED


def test_hmac_fence_flags_even_when_the_ticket_write_fails(_patch):
    """Twin of the po_poll/rfq_poll durability test: a failed Review-Queue ticket must
    never cost the one-shot flag, or the PERMANENT fence degrades into unbounded
    per-cycle re-ticketing."""
    row = _signed_row()
    row["hmac"] = "0" * 64
    _patch["pending"].return_value = [row]
    _patch["review_q"].side_effect = SmartsheetError("smartsheet flaked")

    _run(_patch)

    _patch["flags_persist"].assert_called_once()
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"42": "hmac"}
    codes = _logged_codes(_patch)
    assert "subcontract_hmac_failure" in codes or "sc_hmac_failure" in codes
    assert "review_queue_ticket_failed" in codes
