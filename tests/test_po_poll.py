"""Unit tests for po_materials/po_poll.py — the multi-pass PO pull daemon.

Fully mocked (no live Smartsheet / Box / Worker): the data-plane seams
(portal_client PO calls, vendors, po_log, po_review, render, Box) and the
heartbeat / marker / flag-state seams are patched. Covers: all-gates-off no-op,
the drafts happy path (verify → assert → render → file → receipt-LAST), the
bad-HMAC one-shot reject (CRITICAL + security Review-Queue + flag, never
rendered/filed/marked), the totals-mismatch fence, the collision fence, the
unknown-vendor fence, transient-failure-leaves-queued (and never marks), the
crash-retry idempotency, fail-closed no-creds, the 401 cycle stop, both vendor
passes (incl. the empty-projection refusal + the watermark commit), and the
status pass (ordered approved→sent updates, PO_Log stamps + the superseded
mirror, settled-row no-op, deferred stamps on POST failure).

Run with: pytest -q tests/test_po_poll.py
"""
from __future__ import annotations

from typing import Any

import pytest

from po_materials import po_generate, po_log, po_naming, po_poll, po_review, vendors
from shared import picklist_validation, portal_client, portal_hmac, sheet_ids
from shared.box_client import BoxError
from shared.smartsheet_client import SmartsheetError

SECRET = "po-test-secret"

PURCHASER: dict[str, Any] = {
    "config_version": 1,
    "entity": "Evergreen Renewables LLC",
    "address_lines": ["100 Spectrum Center Dr. STE 570", "Irvine, CA. 92618"],
    "phone": "888-303-6424",
    "invoice_routing": {
        "to": "invoices@example.com",
        "cc": ["ap-lead@example.com"],
    },
}
TAX_CONFIG: dict[str, Any] = {
    "config_version": 1,
    "rates_bp": {"IL": 900, "OR": 0},
    "state_names": {"IL": "Illinois", "OR": "Oregon"},
}

VENDOR_ROW: dict[str, Any] = {
    "_row_id": 100,
    vendors.COL_VENDOR_NAME: "Chint Power Systems",
    vendors.COL_VENDOR_KEY: "VEN-000001",
    vendors.COL_ADDRESS: "2801 N State Hwy 78 Ste 100, Wylie TX",
    vendors.COL_CONTACT_NAME: "Jordan Lee",
    vendors.COL_CONTACT_EMAIL: "orders@chint.example",
    vendors.COL_CONTACT_PHONE: "555-0101",
    vendors.COL_ACTIVE: "Active",
}

LINES: list[dict[str, Any]] = [
    {"position": 1, "part_number": "RK-100", "description": "Rail 100", "qty": 10,
     "unit": "ea", "unit_cost_cents": 12_345, "extended_cents": 123_450,
     "watts": None, "panels": None, "pallets": None, "price_per_watt_microcents": None},
    {"position": 2, "part_number": "RK-200", "description": "Clamp kit", "qty": 2.5,
     "unit": "box", "unit_cost_cents": 1_000, "extended_cents": 2_500,
     "watts": None, "panels": None, "pallets": None, "price_per_watt_microcents": None},
]


def _po_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 7,
        "po_uuid": "u-1",
        "status": "queued",
        "draft_version": 1,
        "po_number": "2026.001.2.0.0",
        "job_no": "2026.001",
        "site_phase": 2,
        "supersede_seq": 0,
        "revision": 0,
        "vendor_key": "VEN-000001",
        "job_id": "JOB-000017",
        "job_name": "Sunrise Solar",
        "ship_to_name": "Evergreen Renewables LLC",
        "ship_to_address": "100 Array Rd",
        "ship_to_city": "Rockford",
        "ship_to_state": "IL",
        "ship_to_zip": "61101",
        "delivery_contact_name": "Dana Field",
        "delivery_contact_phone": "555-0100",
        "delivery_contact_email": "dana@example.com",
        "sow_text": "Supply and deliver racking components.",
        "delivery_instructions": "Call site lead ahead of delivery.",
        "payment_terms_text": "Net 30",
        "terms_profile_id": "standard_17",
        "terms_version": "1",
        "subtotal_cents": 125_950,
        "tax_mode": "auto",
        "tax_rate_bp": 900,
        "tax_cents": 11_336,
        "shipping_cents": 10_000,
        "total_cents": 147_286,
        "line_column_variant": "default",
        "supersedes_po_id": None,
        "approver_name": "Alex Approver",
        "approver_title": "Director of Procurement",
        "created_by": "admin.alex",
        "line_items": [dict(line) for line in LINES],
    }
    base.update(over)
    return base


def _signed_row(**over: Any) -> dict[str, Any]:
    """A pending row whose hmac verifies — signed EXACTLY as the Worker would."""
    row = _po_row(**over)
    canonical = portal_hmac.po_canonical_json(row, row["line_items"])
    row["hmac"] = portal_hmac.sign_po(
        SECRET, po_id=row["id"], po_number=str(row["po_number"]), canonical_json=canonical
    )
    return row


@pytest.fixture
def _patch(mocker):
    seams = {
        # Gates: drafts ON, vendors/status OFF by default (tests flip per case).
        "gate_drafts": mocker.patch("po_materials.po_poll._polling_enabled", return_value=True),
        "gate_vendors": mocker.patch(
            "po_materials.po_poll._vendors_sync_enabled", return_value=False
        ),
        "gate_status": mocker.patch(
            "po_materials.po_poll._status_sync_enabled", return_value=False
        ),
        "resolve_cfg": mocker.patch("po_materials.po_poll.resolve_and_log", return_value={}),
        "creds": mocker.patch(
            "po_materials.po_poll._resolve_credentials",
            return_value=po_poll._PoCreds(
                base_url="https://portal.example", bearer="tok", secret=SECRET
            ),
        ),
        # Worker I/O.
        "pending": mocker.patch(
            "po_materials.po_poll.portal_client.get_pending_pos", return_value=[]
        ),
        "mark_filed": mocker.patch(
            "po_materials.po_poll.portal_client.mark_po_filed", return_value=True
        ),
        # Attachment pass (Feature B) — dark by default (no pending attachments).
        "att_pending": mocker.patch(
            "po_materials.po_poll.portal_client.get_po_attachments_pending", return_value=[]
        ),
        "att_claim": mocker.patch(
            "po_materials.po_poll.portal_client.claim_po_attachment", return_value=True
        ),
        "att_chunks": mocker.patch(
            "po_materials.po_poll.portal_client.get_po_attachment_chunks", return_value=[]
        ),
        "att_result": mocker.patch(
            "po_materials.po_poll.portal_client.post_po_attachment_result", return_value=True
        ),
        "att_clamav": mocker.patch(
            "po_materials.po_poll._attach_clamav_enabled", return_value=False
        ),
        "att_log_attach": mocker.patch(
            "po_materials.po_poll._attach_bytes_to_po_log_best_effort", return_value=None
        ),
        "vendors_sync": mocker.patch(
            "po_materials.po_poll.portal_client.vendors_sync",
            return_value={"ok": True, "upserted": 1, "skipped_dirty": 0},
        ),
        "vendors_pending": mocker.patch(
            "po_materials.po_poll.portal_client.get_pending_vendors", return_value=[]
        ),
        "mark_mirrored": mocker.patch(
            "po_materials.po_poll.portal_client.mark_vendors_mirrored",
            return_value={"ok": True, "flipped": 1, "stale": 0},
        ),
        "status_sync": mocker.patch(
            "po_materials.po_poll.portal_client.po_status_sync",
            return_value={"ok": True, "updated": 1},
        ),
        # Config files.
        "purchaser": mocker.patch(
            "po_materials.po_poll.terms_lib.load_purchaser_config", return_value=PURCHASER
        ),
        "tax": mocker.patch(
            "po_materials.po_poll.terms_lib.load_tax_config", return_value=TAX_CONFIG
        ),
        # Vendors / render / Box / ledger / review seams.
        "get_vendor": mocker.patch(
            "po_materials.po_poll.vendors.get_vendor_by_key", return_value=VENDOR_ROW
        ),
        "down_payload": mocker.patch(
            "po_materials.po_poll.vendors.build_down_sync_payload",
            return_value=vendors.DownSyncPayload(vendors=[], skipped=[]),
        ),
        "upsert_vendor": mocker.patch(
            "po_materials.po_poll.vendors.upsert_vendor", return_value=777
        ),
        "resolve_terms": mocker.patch(
            "po_materials.po_poll.po_generate.resolve_terms",
            return_value=po_generate.TermsRender(kind="library", text="terms body"),
        ),
        "render": mocker.patch(
            "po_materials.po_poll.po_generate.render_po_pdf", return_value=b"%PDF-fake"
        ),
        "box_folder": mocker.patch(
            "po_materials.po_poll._resolve_po_box_folder", return_value="folder-1"
        ),
        "upload": mocker.patch(
            "po_materials.po_poll.box_client.upload_bytes_or_new_version",
            return_value={"id": "f-9", "name": "PO 2026.001.2.0.0.pdf", "size": 9},
        ),
        # po_log patched at ITS module so numbering.check_collision (late import)
        # sees the same mocks.
        "log_find": mocker.patch(
            "po_materials.po_log.find_row_by_po_number", return_value=None
        ),
        "log_append": mocker.patch(
            "po_materials.po_log.append_filed_row", return_value=1
        ),
        "log_stamp": mocker.patch(
            "po_materials.po_log.stamp_status", return_value=True
        ),
        "log_by_d1": mocker.patch(
            "po_materials.po_log.find_po_number_by_d1_id", return_value=None
        ),
        "review_find": mocker.patch(
            "po_materials.po_poll.po_review.find_row_by_po_id", return_value=None
        ),
        "review_add": mocker.patch(
            "po_materials.po_poll.po_review.add_po_review_row", return_value=321
        ),
        "attach": mocker.patch(
            "po_materials.po_poll._attach_pdf_best_effort", return_value=None
        ),
        "perjob": mocker.patch(
            "po_materials.po_poll._append_perjob_row_best_effort",
            return_value=None,
        ),
        # Fences + observability.
        "review_q": mocker.patch("po_materials.po_poll.review_queue.add", return_value=1),
        "anomaly": mocker.patch(
            "po_materials.po_poll.anomaly_logger.check", return_value=None
        ),
        "log": mocker.patch("po_materials.po_poll.error_log.log", return_value=None),
        "hb": mocker.patch("po_materials.po_poll._write_heartbeat", return_value=None),
        "hb_row": mocker.patch("po_materials.po_poll._write_heartbeat_row", return_value=None),
        "marker": mocker.patch("po_materials.po_poll._write_watchdog_marker", return_value=None),
        "flags_load": mocker.patch("po_materials.po_poll._load_flags", return_value={}),
        "flags_persist": mocker.patch("po_materials.po_poll._persist_flags", return_value=None),
        "circuit": mocker.patch(
            "po_materials.po_poll.circuit_breaker.is_open", return_value=False
        ),
        # Status-pass sheet reads (review sheet + ledger) — per-test side_effect.
        "get_rows": mocker.patch(
            "po_materials.po_poll.smartsheet_client.get_rows", return_value=[]
        ),
    }
    return seams


def _run(_patch) -> po_poll.PoPollStats:
    return po_poll._poll_inside_lock(
        _patch["gate_drafts"].return_value,
        _patch["gate_vendors"].return_value,
        _patch["gate_status"].return_value,
    )


def _logged_codes(_patch) -> list[str]:
    return [kw.get("error_code") for _, kw in _patch["log"].call_args_list]


# ---- gates ----------------------------------------------------------------------


def test_all_gates_off_is_noop(mocker, _patch):
    _patch["gate_drafts"].return_value = False
    stats = po_poll.poll_once()
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
    _patch["upload"].assert_called_once()
    (folder, name, pdf), _ = _patch["upload"].call_args
    assert folder == "folder-1" and name == "Sunrise Solar_PO_2026.001.2.0.0.pdf" and pdf == b"%PDF-fake"
    _patch["log_append"].assert_called_once()
    _patch["review_add"].assert_called_once()
    # The inline PDF attach lands on BOTH the ledger row (2026-08-11 parity) and the
    # review row; the per-job mirror's copy rides the (mocked) helper below.
    assert _patch["attach"].call_count == 2
    ledger_call, review_call = _patch["attach"].call_args_list
    assert ledger_call.kwargs.get("sheet_id") == sheet_ids.SHEET_PO_LOG
    assert ledger_call.args[1] == "Sunrise Solar_PO_2026.001.2.0.0.pdf"
    assert review_call.args[0] == 321 and "sheet_id" not in review_call.kwargs
    # The per-job mirror receives the SAME bytes + filename for its own inline copy.
    _patch["perjob"].assert_called_once()
    assert _patch["perjob"].call_args.kwargs["pdf_filename"] == "Sunrise Solar_PO_2026.001.2.0.0.pdf"
    assert _patch["perjob"].call_args.kwargs["pdf_bytes"] == b"%PDF-fake"
    # The receipt is LAST and carries the structural Box file id.
    _patch["mark_filed"].assert_called_once()
    _, mark_kwargs = _patch["mark_filed"].call_args
    assert mark_kwargs["po_id"] == 7 and mark_kwargs["box_file_id"] == "f-9"
    # Review row maps the protocol slots off the SoR vendor + purchaser config.
    _, add_kwargs = _patch["review_add"].call_args
    assert add_kwargs["vendor_key"] == "VEN-000001"
    assert add_kwargs["recipient_to"] == "orders@chint.example"
    assert "ap-lead@example.com" in add_kwargs["cc_display"]
    assert "po_id=7" in add_kwargs["notes"]
    _patch["hb"].assert_called_once()
    _patch["marker"].assert_called_once()


def test_vendor_snapshot_resolved_from_sor_at_render_time(_patch):
    """#494: the render consumes the ITS_Vendors row (the SoR), keyed by the
    HMAC-covered vendor_key — never a client/cache-supplied identity."""
    _patch["pending"].return_value = [_signed_row()]
    _run(_patch)
    _patch["get_vendor"].assert_called_once_with("VEN-000001")
    (po_arg, lines_arg, vendor_arg, purchaser_arg, _terms), _ = _patch["render"].call_args
    assert vendor_arg is VENDOR_ROW
    assert purchaser_arg is PURCHASER


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
    assert "po_hmac_failure" in _logged_codes(_patch)
    # One-shot flag persisted.
    _patch["flags_persist"].assert_called_once()
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"7": "hmac"}


def test_tampered_field_fails_hmac(_patch):
    """A single mutated signed field (the money) breaks verification."""
    row = _signed_row()
    row["total_cents"] = 999_999_99  # tamper AFTER signing
    _patch["pending"].return_value = [row]
    stats = _run(_patch)
    assert stats.rejected == 1
    _patch["mark_filed"].assert_not_called()


def test_flagged_row_is_skipped_silently(_patch):
    _patch["flags_load"].return_value = {"7": "hmac"}
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.skipped_flagged == 1 and stats.rejected == 0
    _patch["review_q"].assert_not_called()
    _patch["mark_filed"].assert_not_called()


def test_totals_mismatch_fences_never_files(_patch):
    """HMAC-valid but the recompute disagrees (Worker↔Mac version skew, e.g. a tax
    table drift) → fence + one-shot flag; never file, never mark."""
    # Sign OVER skewed values so the HMAC passes and only the recompute refuses.
    row = _signed_row(tax_cents=11_000, total_cents=146_950)
    _patch["pending"].return_value = [row]

    stats = _run(_patch)

    assert stats.fenced == 1 and stats.filed == 0
    _patch["upload"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    _, rq_kwargs = _patch["review_q"].call_args
    assert rq_kwargs["reason"] == po_poll.review_queue.ReviewReason.MISMATCHED_REFERENCE
    assert "po_totals_mismatch" in _logged_codes(_patch)
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"7": "totals"}


def test_malformed_signed_field_fences_one_row_without_aborting_the_batch(_patch):
    """Regression (PR #498 review BLOCKER): an HMAC-valid row carrying a malformed
    numeric field (a Worker bug / schema drift / D1 tampering — the HMAC proves the
    Worker signed THIS value, not that it is well-typed) must FENCE one-shot, NOT
    raise. Step 2 (`totals_mismatches`) is the FIRST guard, outside the per-row fence's
    try, so a raise there would crash the whole batch, skip the heartbeat/marker writes
    (stale daemon), and re-crash every ~90s. Two-row batch: the malformed row fences,
    the clean row STILL files, and the cycle completes (heartbeat + marker written)."""
    bad = _signed_row(id=7, po_uuid="u-1", tax_rate_bp="bad-not-a-number")
    good = _signed_row(id=8, po_uuid="u-2", po_number="2026.001.2.0.1")
    _patch["pending"].return_value = [bad, good]

    stats = _run(_patch)

    # One fenced, one filed — the malformed row did NOT abort the batch.
    assert stats.fenced == 1 and stats.filed == 1
    # The malformed row fenced permanently (one-shot flag), routed via the totals fence.
    assert "po_totals_mismatch" in _logged_codes(_patch)
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"7": "totals"}
    # The clean row genuinely filed + receipted.
    assert _patch["upload"].call_count == 1
    _patch["mark_filed"].assert_called_once()
    assert _patch["mark_filed"].call_args.kwargs["po_id"] == 8
    # The cycle COMPLETED — heartbeat + watchdog marker written (a crash would skip them).
    _patch["hb"].assert_called()
    _patch["marker"].assert_called()


def test_po_number_collision_fences(_patch):
    """A PO_Log row with this number that is NOT ours (hand-issued in transition)
    refuses the filing — never a duplicate contractual number."""
    _patch["log_find"].return_value = {"_row_id": 1, po_log.COL_NOTES: "hand-issued"}
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.fenced == 1
    _patch["upload"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    assert "po_number_collision" in _logged_codes(_patch)


def test_unknown_vendor_fences(_patch):
    _patch["get_vendor"].return_value = None
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.fenced == 1
    _patch["render"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    assert "po_vendor_unknown" in _logged_codes(_patch)


def test_transient_box_failure_leaves_queued_and_never_marks(_patch):
    """mark-filed fires ONLY after a successful file — a Box blip leaves the row
    queued (no flag, no Review Queue) and the next cycle retries."""
    _patch["upload"].side_effect = BoxError("box 503")
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.draft_errors == 1 and stats.filed == 0 and stats.fenced == 0
    _patch["mark_filed"].assert_not_called()
    _patch["review_q"].assert_not_called()
    _patch["flags_persist"].assert_not_called()


def test_crash_retry_is_idempotent(_patch):
    """A re-pulled row whose PO_Log + review rows already exist (a lost receipt)
    re-uploads a byte-identical version and ONLY re-posts the receipt — while the
    inline attaches STILL run on BOTH the ledger and the existing review row
    (every-service self-heal, wiring audit 2026-08-12: the review attach used to be
    fresh-create-only, so a crash between row-add and attach left the approver's
    row permanently bare)."""
    _patch["log_find"].return_value = {
        "_row_id": 1, po_log.COL_NOTES: po_log.notes_for_filed_row(7),
    }
    _patch["review_find"].return_value = {"_row_id": 321, po_review.COL_NOTES: "po_id=7"}
    _patch["pending"].return_value = [_signed_row()]

    stats = _run(_patch)

    assert stats.filed == 1
    _patch["log_append"].assert_not_called()
    _patch["review_add"].assert_not_called()
    _patch["mark_filed"].assert_called_once()
    assert _patch["attach"].call_count == 2
    ledger_call, review_call = _patch["attach"].call_args_list
    assert ledger_call.args[0] == 1 and "sheet_id" in ledger_call.kwargs
    assert review_call.args[0] == 321 and "sheet_id" not in review_call.kwargs


def test_unresolved_box_root_leaves_queued(_patch):
    _patch["box_folder"].return_value = None
    _patch["pending"].return_value = [_signed_row()]
    stats = _run(_patch)
    assert stats.draft_errors == 1 and stats.filed == 0
    _patch["mark_filed"].assert_not_called()
    assert "po_box_root_unresolved" in _logged_codes(_patch)


# ---- cycle-level failure modes -----------------------------------------------------


def test_no_creds_fail_closed(_patch):
    _patch["creds"].return_value = None
    stats = _run(_patch)
    assert stats.halted_no_creds is True
    _patch["pending"].assert_not_called()
    assert "po_creds_missing" in _logged_codes(_patch)
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "ERROR"


def test_transient_base_url_warns_and_skips_without_paging(_patch):
    # A Smartsheet blip on the base-URL read is NOT a missing credential. Live on
    # 2026-07-20 04:42Z a single GET failure fell back to "" and fired a CRITICAL
    # saying PO credentials were unset; both Keychain entries were fine and the
    # daemon self-healed 90s later. That page was false, and it aimed the §43
    # repair at re-provisioning secrets (a high-capability-class action) at a
    # condition needing none. Transient => WARN + skip, never the misconfig CRITICAL.
    _patch["creds"].return_value = po_poll.TransientUnavailable(
        reason="SmartsheetError: (<PreparedRequest [GET]>, None)"
    )
    stats = _run(_patch)
    assert stats.halted_transient is True
    assert stats.halted_no_creds is False
    _patch["pending"].assert_not_called()  # still FAIL-CLOSED — it does not poll
    codes = _logged_codes(_patch)
    assert "po_creds_transient" in codes
    assert "po_creds_missing" not in codes, "a transient read failure must NOT page"
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "WARN"  # not ERROR — nothing is misconfigured


def test_bearer_rejected_stops_cycle(_patch):
    _patch["gate_vendors"].return_value = True  # would run after drafts — must not
    _patch["pending"].side_effect = portal_client.PortalAuthError("401")
    stats = _run(_patch)
    assert stats.bearer_rejected is True
    _patch["down_payload"].assert_not_called()  # the cycle stopped at the 401
    assert "po_bearer_rejected" in _logged_codes(_patch)
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "ERROR"


def test_transient_pending_fetch_still_writes_heartbeat(_patch):
    _patch["pending"].side_effect = portal_client.PortalTransportError("blip")
    stats = _run(_patch)
    assert stats.draft_errors == 1
    _patch["hb"].assert_called_once()
    _patch["marker"].assert_called_once()


# ---- ② vendor down-sync -------------------------------------------------------------


def test_down_sync_posts_full_projection(_patch):
    _patch["gate_drafts"].return_value = False
    _patch["gate_vendors"].return_value = True
    projection = [{"vendor_key": "VEN-000001", "vendor_name": "Chint", "active": 1}]
    _patch["down_payload"].return_value = vendors.DownSyncPayload(
        vendors=projection, skipped=[(55, "malformed vendor_key ''")],
    )
    stats = _run(_patch)
    assert stats.vendors_downsynced == 1
    (base, tok, sent), _ = _patch["vendors_sync"].call_args
    assert sent == projection
    assert "po_vendor_row_skipped" in _logged_codes(_patch)


def test_down_sync_refuses_empty_projection(_patch):
    """An empty ITS_Vendors projection must never be POSTed — a read-miss would
    otherwise wipe the portal cache."""
    _patch["gate_drafts"].return_value = False
    _patch["gate_vendors"].return_value = True
    _patch["down_payload"].return_value = vendors.DownSyncPayload(vendors=[], skipped=[])
    _run(_patch)
    _patch["vendors_sync"].assert_not_called()
    assert "po_vendors_empty_projection" in _logged_codes(_patch)


# ---- ③ vendor up-sync ---------------------------------------------------------------


def _portal_vendor(**over: Any) -> dict[str, Any]:
    base = {
        "vendor_key": "VEN-000002", "vendor_name": "VSUN", "address": "",
        "contact_name": "", "contact_email": "", "contact_phone": "",
        "region": "West", "supply_categories": ["modules"],
        "default_terms_profile": "negotiated_gtc", "gtc_reference": "",
        "active": 1, "notes": "", "origin": "portal",
        "mirror_version": 3, "mirrored_version": 1,
    }
    base.update(over)
    return base


def test_up_sync_upserts_then_marks_with_read_watermark(_patch):
    _patch["gate_drafts"].return_value = False
    _patch["gate_vendors"].return_value = True
    _patch["vendors_pending"].return_value = [_portal_vendor()]
    stats = _run(_patch)
    assert stats.vendors_upsynced == 1
    _patch["upsert_vendor"].assert_called_once()
    (base, tok, updates), _ = _patch["mark_mirrored"].call_args
    assert updates == [{"vendor_key": "VEN-000002", "mirrored_version": 3}]


def test_up_sync_permanent_failure_fences_and_never_marks(_patch):
    _patch["gate_drafts"].return_value = False
    _patch["gate_vendors"].return_value = True
    _patch["vendors_pending"].return_value = [_portal_vendor()]
    _patch["upsert_vendor"].side_effect = picklist_validation.PicklistViolationError(
        1, "Region", "Mars", frozenset({"West"})
    )
    stats = _run(_patch)
    assert stats.vendors_reviewed == 1 and stats.vendors_upsynced == 0
    _patch["mark_mirrored"].assert_not_called()
    _patch["review_q"].assert_called_once()


def test_up_sync_malformed_watermark_skips(_patch):
    _patch["gate_drafts"].return_value = False
    _patch["gate_vendors"].return_value = True
    _patch["vendors_pending"].return_value = [_portal_vendor(mirror_version="three")]
    stats = _run(_patch)
    assert stats.vendor_errors == 1
    _patch["upsert_vendor"].assert_not_called()
    _patch["mark_mirrored"].assert_not_called()


# ---- ④ status pass ------------------------------------------------------------------


def _review_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "_row_id": 400,
        po_review.COL_WORKSTREAM: "po_materials",
        po_review.COL_NOTES: "po_id=7; po_number=2026.001.2.0.0",
        po_review.COL_SEND_STATUS: po_review.STATUS_PENDING,
        po_review.COL_APPROVE_SCHEDULED: False,
        po_review.COL_SEND_NOW: False,
        po_review.COL_APPROVED_BY: None,
        po_review.COL_SENT_AT: None,
    }
    base.update(over)
    return base


def _ledger_row(status: str, number: str = "2026.001.2.0.0") -> dict[str, Any]:
    return {"_row_id": 500, po_log.COL_PO_NUMBER: number, po_log.COL_STATUS: status}


def _arm_status_pass(_patch, review_rows, ledger_rows) -> None:
    _patch["gate_drafts"].return_value = False
    _patch["gate_status"].return_value = True
    _patch["get_rows"].side_effect = [review_rows, ledger_rows]


def test_status_pass_sent_row_walks_machine_in_order_and_stamps_ledger(_patch):
    sent_row = _review_row(
        **{
            po_review.COL_SEND_STATUS: po_review.STATUS_SENT,
            po_review.COL_SENT_AT: "2026-07-10T09:00:00",
            po_review.COL_NOTES: "po_id=7; po_number=2026.001.2.1.0; supersedes_po_id=5",
        }
    )
    _arm_status_pass(_patch, [sent_row], [_ledger_row("approved", "2026.001.2.1.0")])
    _patch["log_by_d1"].return_value = "2026.001.2.0.0"

    stats = _run(_patch)

    assert stats.status_synced == 1
    (base, tok, updates), _ = _patch["status_sync"].call_args
    # Ordered approved-then-sent for the same PO (the Worker's guarded batch).
    assert updates == [
        {"po_id": 7, "status": "approved"},
        {"po_id": 7, "status": "sent"},
    ]
    stamp_calls = _patch["log_stamp"].call_args_list
    assert stamp_calls[0].args == ("2026.001.2.1.0", po_log.STATUS_SENT)
    assert stamp_calls[0].kwargs == {"sent_at_iso": "2026-07-10"}
    # The superseded mirror onto the predecessor (resolved via the d1_id join).
    assert stamp_calls[1].args == ("2026.001.2.0.0", po_log.STATUS_SUPERSEDED)
    assert stamp_calls[1].kwargs == {"superseded_by": "2026.001.2.1.0"}


def test_status_pass_approved_only_row(_patch):
    approved = _review_row(**{po_review.COL_APPROVED_BY: "buyer@evergreen.example"})
    _arm_status_pass(_patch, [approved], [_ledger_row(po_log.STATUS_PENDING_REVIEW)])
    stats = _run(_patch)
    assert stats.status_synced == 1
    (base, tok, updates), _ = _patch["status_sync"].call_args
    assert updates == [{"po_id": 7, "status": "approved"}]
    _patch["log_stamp"].assert_called_once_with("2026.001.2.0.0", po_log.STATUS_APPROVED)


def test_status_pass_settled_ledger_rows_generate_no_post(_patch):
    sent_row = _review_row(**{po_review.COL_SEND_STATUS: po_review.STATUS_SENT})
    _arm_status_pass(_patch, [sent_row], [_ledger_row(po_log.STATUS_SENT)])
    stats = _run(_patch)
    assert stats.status_synced == 0
    _patch["status_sync"].assert_not_called()
    _patch["log_stamp"].assert_not_called()


def test_status_pass_post_failure_defers_ledger_stamps(_patch):
    """D1 first, ledger second: a failed POST defers the PO_Log stamps whole — the
    next cycle retries both (never a ledger ahead of D1)."""
    sent_row = _review_row(**{po_review.COL_SEND_STATUS: po_review.STATUS_SENT})
    _arm_status_pass(_patch, [sent_row], [_ledger_row(po_log.STATUS_APPROVED)])
    _patch["status_sync"].side_effect = portal_client.PortalTransportError("blip")
    stats = _run(_patch)
    assert stats.status_errors == 1 and stats.status_synced == 0
    _patch["log_stamp"].assert_not_called()


def test_status_pass_ignores_foreign_workstream_rows(_patch):
    foreign = _review_row(**{po_review.COL_WORKSTREAM: "safety"})
    _arm_status_pass(_patch, [foreign], [_ledger_row(po_log.STATUS_PENDING_REVIEW)])
    _run(_patch)
    _patch["status_sync"].assert_not_called()
    assert "po_status_foreign_tag" in _logged_codes(_patch)


# ---- ①b attachment pass (Feature B) ----------------------------------------------

import base64  # noqa: E402 — grouped with the attachment-pass tests they serve
import hashlib  # noqa: E402
import io  # noqa: E402
import zipfile  # noqa: E402

_MINIMAL_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def _macro_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<doc/>")
        zf.writestr("word/vbaProject.bin", "macro-bytes")
    return buf.getvalue()


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _att_row(data: bytes, **over: Any) -> dict[str, Any]:
    filename = str(over.pop("filename", "spec.pdf"))
    mime = str(over.pop("declared_mime", "application/pdf"))
    att_id = int(over.pop("id", 41))
    po_id = int(over.pop("po_id", 7))
    sha = hashlib.sha256(data).hexdigest()
    row: dict[str, Any] = {
        "id": att_id,
        "att_uuid": f"u-att-{att_id}",
        "po_id": po_id,
        "po_number": "2026.001.2.0.0",
        "job_name": "Sunrise Solar",
        "filename": filename,
        "declared_mime": mime,
        "size_bytes": len(data),
        "sha256": sha,
        "status": "pending",
        "hmac": portal_hmac.sign_po_attachment(
            SECRET, att_uuid=f"u-att-{att_id}", po_id=po_id, filename=filename,
            declared_mime=mime, size_bytes=len(data), sha256=sha,
        ),
        "uploaded_by": "office.admin",
        "created_at": 1234,
    }
    row.update(over)
    return row


def _one_chunk(data: bytes) -> list[dict[str, Any]]:
    return [{"chunk_index": 0, "chunk_total": 1,
             "chunk_b64": base64.b64encode(data).decode()}]


def test_attachment_clean_filed_to_box_and_log(_patch):
    """Clean attachment: claim → verify → screen → Box (original bytes) → PO_Log
    attach → result 'filed' with the Box id. The filed name is the po_naming helper's."""
    _patch["att_pending"].return_value = [_att_row(_MINIMAL_PDF)]
    _patch["att_chunks"].return_value = _one_chunk(_MINIMAL_PDF)
    _patch["upload"].return_value = {"id": "f-att-1", "name": "x", "size": 1}

    stats = _run(_patch)
    assert stats.attachments_filed == 1
    assert stats.attachments_refused == 0
    _patch["att_claim"].assert_called_once()
    args, kwargs = _patch["upload"].call_args
    assert args[1] == po_poll.po_naming.po_attachment_filename("2026.001.2.0.0", 41, "spec.pdf")
    assert args[2] == _MINIMAL_PDF  # the ORIGINAL bytes, not a re-encode
    _patch["att_log_attach"].assert_called_once()
    _patch["att_result"].assert_called_once()
    assert _patch["att_result"].call_args.kwargs["status"] == "filed"
    assert _patch["att_result"].call_args.kwargs["box_file_id"] == "f-att-1"


def test_attachment_same_named_uploads_file_under_distinct_names(_patch):
    """REVIEW BLOCKER regression: two attachments on ONE PO sharing an original
    filename must land as TWO distinct Box files + TWO distinct PO_Log attachments —
    the attachment id disambiguates the filed name. Without it the 2nd upload
    versions-over the 1st in Box AND replaces its PO_Log inline copy (silent loss).
    Remove the id from po_attachment_filename and this test fails."""
    _patch["att_pending"].return_value = [
        _att_row(_MINIMAL_PDF, id=41),
        _att_row(_MINIMAL_PDF, id=42),  # same filename "spec.pdf", same PO
    ]
    _patch["att_chunks"].return_value = _one_chunk(_MINIMAL_PDF)
    _patch["upload"].return_value = {"id": "f-att-x", "name": "x", "size": 1}

    stats = _run(_patch)
    assert stats.attachments_filed == 2
    box_names = [call.args[1] for call in _patch["upload"].call_args_list]
    log_names = [call.args[1] for call in _patch["att_log_attach"].call_args_list]
    assert len(box_names) == 2 and len(set(box_names)) == 2, box_names
    assert len(log_names) == 2 and len(set(log_names)) == 2, log_names
    for names in (box_names, log_names):
        assert all("spec.pdf" in n and "2026.001.2.0.0" in n for n in names)


def test_attachment_malicious_refused_never_filed(_patch):
    """PROVE-THE-CONTROL-BITES (wiring): a macro-carrying docx NEVER reaches Box —
    CRITICAL naming the account + a security-flagged Review-Queue row + a 'refused'
    disposition. Stub the screener to 'clean' (or skip it) and this test fails."""
    docx = _macro_docx()
    _patch["att_pending"].return_value = [
        _att_row(docx, filename="specs.docx", declared_mime=_DOCX_MIME)
    ]
    _patch["att_chunks"].return_value = _one_chunk(docx)

    stats = _run(_patch)
    assert stats.attachments_filed == 0
    assert stats.attachments_refused == 1
    _patch["upload"].assert_not_called()          # never filed to Box
    _patch["att_log_attach"].assert_not_called()  # never attached to the ledger
    assert _patch["att_result"].call_args.kwargs["status"] == "refused"
    rq = _patch["review_q"].call_args.kwargs
    assert rq["security_flag"] is True
    assert rq["severity"] == po_poll.Severity.CRITICAL
    assert "office.admin" in rq["summary"]  # CRITICAL names the uploading account
    assert "po_attachment_malicious" in _logged_codes(_patch)
    # One-shot flag set + persisted (review fix #3): a later transient failure can
    # never re-fire this CRITICAL / duplicate the Review-Queue row every cycle.
    persisted = _patch["flags_persist"].call_args.args[0]
    assert persisted.get("att-41") == "refused"


def test_attachment_refused_postback_failure_flags_once_no_alert_storm(_patch):
    """Review fix #3: when the refused-disposition POST-BACK fails transiently AFTER
    the CRITICAL + Review-Queue row fired, the one-shot flag still persists — the
    next cycle SKIPS the row instead of re-screening + re-firing (the ~90s alert
    storm on the cap-sensitive sheets)."""
    docx = _macro_docx()
    _patch["att_pending"].return_value = [
        _att_row(docx, filename="specs.docx", declared_mime=_DOCX_MIME)
    ]
    _patch["att_chunks"].return_value = _one_chunk(docx)
    _patch["att_result"].side_effect = portal_client.PortalTransportError("worker 500")

    stats = _run(_patch)
    assert stats.attachments_refused == 1
    assert stats.attachment_errors == 1  # the failed post-back is still surfaced
    assert "po_attachment_result_post_failed" in _logged_codes(_patch)
    persisted = _patch["flags_persist"].call_args.args[0]
    assert persisted.get("att-41") == "refused"
    assert _patch["review_q"].call_count == 1  # exactly ONE Review-Queue record

    # Next cycle: the flagged row is skipped silently — no re-screen, no re-alert.
    _patch["flags_load"].return_value = dict(persisted)
    _patch["review_q"].reset_mock()
    _patch["att_result"].reset_mock()
    _run(_patch)
    _patch["review_q"].assert_not_called()
    _patch["att_result"].assert_not_called()


def test_attachment_suspicious_pdf_active_content_security_flagged(_patch):
    """A /JavaScript-bearing PDF is refused-to-review with the security flag
    (structural active content), WARN not CRITICAL; the PO pipeline is untouched."""
    bad_pdf = b"%PDF-1.4\n<< /JavaScript (app.alert(1)) >>\ntrailer\n%%EOF\n"
    _patch["att_pending"].return_value = [_att_row(bad_pdf)]
    _patch["att_chunks"].return_value = _one_chunk(bad_pdf)

    stats = _run(_patch)
    assert stats.attachments_refused == 1
    _patch["upload"].assert_not_called()
    assert _patch["att_result"].call_args.kwargs["status"] == "refused"
    rq = _patch["review_q"].call_args.kwargs
    assert rq["security_flag"] is True
    assert rq["severity"] == po_poll.Severity.WARN
    assert "po_attachment_suspicious" in _logged_codes(_patch)


def test_attachment_bad_hmac_flagged_never_disposed(_patch):
    """A tampered row (filename flipped after signing) fails verify: CRITICAL +
    security Review-Queue row + one-shot flag; NO disposition post (bytes stay in
    D1 for forensics) and nothing reaches Box."""
    row = _att_row(_MINIMAL_PDF)
    row["filename"] = "renamed-after-signing.pdf"  # breaks the signature
    _patch["att_pending"].return_value = [row]
    _patch["att_chunks"].return_value = _one_chunk(_MINIMAL_PDF)

    stats = _run(_patch)
    assert stats.attachments_refused == 1
    _patch["upload"].assert_not_called()
    _patch["att_result"].assert_not_called()  # never disposed — forensics
    assert _patch["review_q"].call_args.kwargs["security_flag"] is True
    assert "po_attachment_integrity_failure" in _logged_codes(_patch)
    # One-shot flag persisted under the att- key.
    persisted = _patch["flags_persist"].call_args.args[0]
    assert persisted.get("att-41") == "integrity"


def test_attachment_digest_mismatch_flagged(_patch):
    """Chunks that reassemble to different bytes than the SIGNED sha256 are an
    integrity failure — same posture as a bad HMAC."""
    row = _att_row(_MINIMAL_PDF)
    _patch["att_pending"].return_value = [row]
    _patch["att_chunks"].return_value = _one_chunk(b"%PDF-1.4 swapped bytes %%EOF")

    stats = _run(_patch)
    assert stats.attachments_refused == 1
    _patch["upload"].assert_not_called()
    _patch["att_result"].assert_not_called()
    assert "po_attachment_integrity_failure" in _logged_codes(_patch)


def test_attachment_pass_failure_never_blocks_filing(_patch):
    """The whole attachment pass is FENCED: an unexpected crash inside it is caught
    at the call site (po_attachment_service_failed) and the cycle — including the
    already-completed PO filing — finishes normally."""
    _patch["pending"].return_value = [_signed_row()]
    _patch["att_pending"].side_effect = RuntimeError("boom")

    stats = _run(_patch)
    assert stats.filed == 1  # the PO itself still filed + receipted
    assert stats.attachment_errors == 1
    assert "po_attachment_service_failed" in _logged_codes(_patch)


def test_attachment_transient_box_failure_stays_serviceable(_patch):
    """A BoxError mid-file is TRANSIENT: no disposition post, the claimed row
    re-serves next cycle, the cycle completes."""
    _patch["att_pending"].return_value = [_att_row(_MINIMAL_PDF)]
    _patch["att_chunks"].return_value = _one_chunk(_MINIMAL_PDF)
    _patch["upload"].side_effect = BoxError("box down")

    stats = _run(_patch)
    assert stats.attachment_errors == 1
    assert stats.attachments_filed == 0
    _patch["att_result"].assert_not_called()
    assert "po_attachment_transient" in _logged_codes(_patch)


def test_attachment_flagged_row_skipped(_patch):
    """A previously integrity-flagged attachment is skipped silently (no re-flag spam)."""
    _patch["flags_load"].return_value = {"att-41": "integrity"}
    _patch["att_pending"].return_value = [_att_row(_MINIMAL_PDF)]

    stats = _run(_patch)
    assert stats.attachments_refused == 0
    assert stats.attachments_filed == 0
    _patch["att_claim"].assert_not_called()
    _patch["att_result"].assert_not_called()


# ---- per-job tracking sheet mirror (Feature A) -------------------------------------

# The REAL helper, captured at import time (the `_patch` fixture replaces the module
# attribute) — used by the end-to-end fence test below.
_REAL_PERJOB = po_poll._append_perjob_row_best_effort


def test_happy_path_mirrors_ledger_row_to_perjob_sheet(_patch):
    """The filing path hands the SAME ledger row kwargs to the per-job mirror,
    keyed by the job name (the Box per-job folder's name source)."""
    _patch["pending"].return_value = [_signed_row()]

    _run(_patch)

    _patch["perjob"].assert_called_once()
    job_name, row_kwargs, _corr = _patch["perjob"].call_args.args
    assert job_name == "Sunrise Solar"
    assert row_kwargs["po_number"] == "2026.001.2.0.0"
    assert row_kwargs["notes"] == "d1_id=7"  # the §19 join rides the mirror row too
    # The mirror is best-effort ON TOP of the flat append — both fired.
    _patch["log_append"].assert_called_once()


def test_perjob_failure_never_fails_the_filing(_patch, mocker):
    """END-TO-END fence proof: run the REAL helper with ensure_job_sheet raising —
    the filing still completes, the receipt still posts, and the stable WARN
    error_code is logged (Box + the flat PO_Log are the SoR)."""
    _patch["perjob"].side_effect = _REAL_PERJOB
    mocker.patch(
        "po_materials.po_poll.job_sheet.ensure_job_sheet",
        side_effect=SmartsheetError("boom"),
    )
    _patch["pending"].return_value = [_signed_row()]

    stats = _run(_patch)

    assert stats.filed == 1
    assert stats.fenced == 0 and stats.draft_errors == 0
    _patch["mark_filed"].assert_called_once()
    assert "po_perjob_sheet_failed" in _logged_codes(_patch)


def test_perjob_helper_ensures_and_appends_to_target_sheet(mocker):
    """The helper wires FOLDER_PO_JOBS + the flat Log as template + the sanitized
    job folder name + the fixed sheet name, then appends with sheet_id=<per-job>
    and attaches the inline PDF on the fresh row (2026-08-11 parity)."""
    ensure = mocker.patch(
        "po_materials.po_poll.job_sheet.ensure_job_sheet", return_value=555
    )
    find = mocker.patch(
        "po_materials.po_log.find_row_by_po_number", return_value=None
    )
    append = mocker.patch(
        "po_materials.po_log.append_filed_row", return_value=91
    )
    attach = mocker.patch("po_materials.po_poll._attach_pdf_best_effort")
    row_kwargs = {"po_number": "2026.001.2.0.0", "job_project": "2026.001 — Sunrise Solar"}

    po_poll._append_perjob_row_best_effort(
        "Sunrise Solar", row_kwargs, "corr-1",
        pdf_filename="Sunrise Solar_PO_2026.001.2.0.0.pdf", pdf_bytes=b"%PDF-fake",
    )

    ensure.assert_called_once_with(
        sheet_ids.FOLDER_PO_JOBS,
        sheet_ids.SHEET_PO_LOG,
        "Sunrise Solar",
        po_poll.PERJOB_SHEET_NAME,
        workspace_id=sheet_ids.WORKSPACE_PURCHASE_ORDERS,  # §51 A1 margin-check target
        workstream=po_poll.WORKSTREAM,
        correlation_id="corr-1",
    )
    find.assert_called_once_with("2026.001.2.0.0", sheet_id=555)
    append.assert_called_once_with(sheet_id=555, **row_kwargs)
    attach.assert_called_once_with(
        91, "Sunrise Solar_PO_2026.001.2.0.0.pdf", b"%PDF-fake", "corr-1", sheet_id=555
    )


def test_perjob_helper_is_idempotent_against_target_sheet(mocker):
    """A crash between the flat append and the mirror re-runs cleanly: the PO number
    already present in the TARGET sheet suppresses the duplicate append — but the
    inline attach STILL runs against the existing row (the every-service self-heal
    posture; attach_pdf_to_row replaces a same-filename attachment, so no dupes)."""
    mocker.patch(
        "po_materials.po_poll.job_sheet.ensure_job_sheet", return_value=555
    )
    mocker.patch(
        "po_materials.po_log.find_row_by_po_number",
        return_value={"_row_id": 1},
    )
    append = mocker.patch("po_materials.po_log.append_filed_row")
    attach = mocker.patch("po_materials.po_poll._attach_pdf_best_effort")

    po_poll._append_perjob_row_best_effort(
        "Sunrise Solar", {"po_number": "2026.001.2.0.0"}, "corr-1",
        pdf_filename="f.pdf", pdf_bytes=b"%PDF",
    )

    append.assert_not_called()
    attach.assert_called_once_with(1, "f.pdf", b"%PDF", "corr-1", sheet_id=555)


def test_perjob_helper_fences_generic_exception(mocker):
    """The fence is broad (mirror the best-effort attach idiom): even a non-Smartsheet
    exception is reduced to the WARN error_code, never raised to the filing path."""
    mocker.patch(
        "po_materials.po_poll.job_sheet.ensure_job_sheet",
        side_effect=RuntimeError("weird"),
    )
    log = mocker.patch("po_materials.po_poll.error_log.log")

    po_poll._append_perjob_row_best_effort(
        "Sunrise Solar", {"po_number": "2026.001.2.0.0"}, "corr-1",
        pdf_filename="f.pdf", pdf_bytes=b"%PDF",
    )  # must not raise

    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "po_perjob_sheet_failed" in codes


def test_po_box_resolver_reads_the_lanes_own_root(mocker):
    """The 2026-08-11 split: the resolver reads po_naming.CFG_BOX_PORTAL_ROOT under
    Workstream='po_materials' (NOT the shared safety root) and files DIRECTLY in the
    per-job folder — no intermediate 'Purchase Orders' level survives."""
    read = mocker.patch(
        "po_materials.po_poll._read_str_setting", return_value="root-po"
    )
    ensure = mocker.patch(
        "po_materials.po_poll.box_client.get_or_create_folder", return_value="job-9"
    )

    assert po_poll._resolve_po_box_folder("Sunrise Solar") == "job-9"

    read.assert_called_once_with(
        po_naming.CFG_BOX_PORTAL_ROOT, "",
        workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM,
    )
    ensure.assert_called_once_with("root-po", "Sunrise Solar")


# ---- config-read transient fence (error-hygiene 2026-07-19) ---------------------


def test_read_str_setting_transient_error_falls_open_with_warn(mocker):
    """A generic SmartsheetError from get_setting (read-timeout / 5xx) must NOT escape
    to @its_error_log as a spurious CRITICAL — WARN `config_read_error` + fallback,
    same disposition as the circuit-open branch."""
    mocker.patch(
        "po_materials.po_poll.smartsheet_client.get_setting",
        side_effect=SmartsheetError("read timeout"),
    )
    log = mocker.patch("po_materials.po_poll.error_log.log")

    result = po_poll._read_str_setting("po_materials.some_key", "fallback-val")  # must not raise

    assert result == "fallback-val"
    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "config_read_error" in codes


def test_polling_gate_transient_error_resolves_to_default(mocker):
    """Cycle-entry proof: the polling gate read survives a transient and resolves to the
    ships-dark default (False) instead of crashing the cycle."""
    mocker.patch(
        "po_materials.po_poll.smartsheet_client.get_setting",
        side_effect=SmartsheetError("HTTP 502"),
    )
    mocker.patch("po_materials.po_poll.error_log.log")

    assert po_poll._polling_enabled() is po_poll.DEFAULT_POLLING_ENABLED


def test_hmac_fence_flags_even_when_the_ticket_write_fails(_patch):
    """A fence must STOP the item (flag) and TELL the operator (ticket). The ticket write
    used to come first and `review_queue.add` propagates, so a Smartsheet blip at fence
    time cost the flag — the PO re-served next cycle and re-ticketed, forever. Now the
    ticket rides `review_queue.safe_add` (never raises), so the flag still lands and the
    lost ticket escalates to CRITICAL instead of vanishing."""
    row = _signed_row()
    row["hmac"] = "0" * 64
    _patch["pending"].return_value = [row]
    _patch["review_q"].side_effect = SmartsheetError("smartsheet flaked")

    stats = _run(_patch)

    assert stats.rejected == 1 and stats.filed == 0
    _patch["flags_persist"].assert_called_once()
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"7": "hmac"}
    codes = _logged_codes(_patch)
    assert "po_hmac_failure" in codes
    assert "review_queue_ticket_failed" in codes
    _patch["mark_filed"].assert_not_called()
