"""RED-suite unit tests for po_materials/rfq_poll.py + the R2 lane contracts
(ADR-0004 Lane 2, PR-C: verify → per-vendor price-free render → Box → RFQ_Log +
RFQ_Pending_Review → mark-filed once → status mirror).

Fully mocked at the module seams, the tests/test_estimate_poll.py house idiom (no
live Smartsheet / Box / Worker). Every daemon test here is a PROVE-THE-CONTROL-
BITES test: it asserts the CONTROL fires (rfq:v1 integrity refusal, per-vendor
fence, dark-ship gate, idempotent replay, receipt-last) and would fail if the
control were deleted.

Contract pins exercised (the PR-C shared contract):
  * rfq:v1 HMAC — signatures in these tests are computed IN-TEST from the pinned
    canonical math (recompute-from-fields: fixed header/line key order + compact
    JSON + "rfq:v1"\\n id\\n number\\n json), independent of shared.portal_hmac —
    a daemon verifying a drifted canonical fails the happy path here.
  * Tampered canonical (any signed field mutated after signing) → one-shot flag +
    security Review-Queue row + CRITICAL; NEVER rendered, NEVER filed, NO receipt.
  * Unknown vendor → per-vendor Review-Queue fence; the OTHER vendors still render
    + file + stage, and the receipt carries only the filed vendors.
  * ALL vendors unknown → receipt WITHHELD + one-shot flag (never a silent drain).
  * polling gate false → dark-ship no-op (zero Worker calls).
  * Idempotent replay (a crash after filing but before the receipt): a re-served
    rfq whose ledger + review rows already exist appends NOTHING new and still
    posts the receipt.
  * The review-row Workstream tag is the DISTINCT 'po_materials_rfq' lane value —
    hard-populated, registered, and ≠ po_send's 'po_materials' (cross-lane
    dispatch impossibility).
  * Renderer escaping (red-team #11): hostile reportlab markup in operator/vendor
    strings renders escaped — a deliberately BROKEN tag would crash the paraparser
    if escaping were removed. Price-free is source-pinned.

Run with: pytest -q tests/test_rfq_poll.py
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import inspect
import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from po_materials import rfq_generate, rfq_poll, rfq_review
from shared.error_log import Severity

SECRET = "rfq-test-secret"

HEADER_KEYS = (
    "rfq_number", "job_no", "job_name",
    "ship_to_name", "ship_to_address", "ship_to_city", "ship_to_state", "ship_to_zip",
    "delivery_contact_name", "delivery_contact_phone", "delivery_contact_email",
    "scope_text", "due_date",
)
LINE_KEYS = ("position", "part_number", "description", "qty", "unit", "line_note")

VENDOR_1 = {
    "Vendor Name": "Platt Electric Supply",
    "Vendor Key": "VEN-000001",
    "Address": "123 Supply Rd, Portland, OR 97201",
    "Contact Name": "Sam Seller",
    "Contact Email": "sam@platt.example",
    "Contact Phone": "503-555-0100",
}
VENDOR_2 = {**VENDOR_1, "Vendor Name": "Nassau Electric", "Vendor Key": "VEN-000007",
            "Contact Email": "quotes@nassau.example"}
PURCHASER = {
    "entity": "Evergreen Renewables LLC",
    "address_lines": ["500 Solar Way", "Rockford, IL 61101"],
    "phone": "815-555-0100",
}


# ---- row builders (rfq:v1 canonical computed IN-TEST — the golden math) ------------


def _golden_canonical_json(
    rfq: dict[str, Any], lines: list[dict[str, Any]], vendor_keys: list[str]
) -> str:
    obj: dict[str, Any] = {k: rfq.get(k) for k in HEADER_KEYS}
    obj["line_items"] = [{k: ln.get(k) for k in LINE_KEYS} for ln in lines]
    obj["vendor_keys"] = sorted(vendor_keys)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sign_rfq(
    secret: str, rfq_id: int, rfq: dict[str, Any],
    lines: list[dict[str, Any]], vendor_keys: list[str],
) -> str:
    canonical = "\n".join([
        "rfq:v1", str(rfq_id), str(rfq.get("rfq_number") or ""),
        _golden_canonical_json(rfq, lines, vendor_keys),
    ])
    return _hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _vendor_rows(keys: list[str], status: str = "pending") -> list[dict[str, Any]]:
    """The Worker-joined rfq_vendors rows the pending route serves."""
    return [
        {"vendor_key": k, "status": status, "box_pdf_file_id": None,
         "box_form_file_id": None, "review_row_id": None, "sent_at": None}
        for k in keys
    ]


def _rfq_row(**over: Any) -> dict[str, Any]:
    """A pending rfqs row signed EXACTLY as the Worker would (rfq:v1). Overrides
    applied AFTER signing = deliberate tampering in tests; overrides that should
    be signed go through `signed`; the vendor fan-out via `vendor_keys`."""
    rfq_id = int(over.pop("id", 7))
    signed: dict[str, Any] = over.pop("signed", {})
    vendor_keys: list[str] = over.pop("vendor_keys", ["VEN-000001"])
    lines: list[dict[str, Any]] = over.pop("lines", [
        {"position": 1, "part_number": "RK-100", "description": "Rail 100",
         "qty": 10, "unit": "EA", "line_note": "black"},
    ])
    row: dict[str, Any] = {
        "id": rfq_id,
        "rfq_uuid": f"u-rfq-{rfq_id}",
        "rfq_number": f"RFQ-2026.001-{rfq_id:03d}",
        "job_no": "2026.001",
        "job_name": "Sunrise Solar",
        "ship_to_name": "Sunrise Solar Laydown",
        "ship_to_address": "100 Array Rd",
        "ship_to_city": "Rockford",
        "ship_to_state": "IL",
        "ship_to_zip": "61101",
        "delivery_contact_name": "Dana Field",
        "delivery_contact_phone": "815-555-0101",
        "delivery_contact_email": "dana@evergreen.example",
        "scope_text": "Supply-only racking package.",
        "due_date": "2026-08-14",
        "status": "queued",
        "line_items": lines,
        "vendors": _vendor_rows(vendor_keys),
        **signed,
    }
    lines_for_sig = row["line_items"] if isinstance(row["line_items"], list) else []
    keys_for_sig = [str(v["vendor_key"]) for v in row["vendors"]]
    row["hmac"] = _sign_rfq(SECRET, rfq_id, row, lines_for_sig, keys_for_sig)
    row.update(over)  # post-signing overrides = tampering
    return row


# ---- fixture (the estimate_poll _patch idiom) --------------------------------------


@pytest.fixture
def _patch(mocker):
    r_log = mocker.patch("po_materials.rfq_poll.rfq_log")
    r_log.find_row.return_value = None
    r_log.append_row.return_value = 1
    r_log.update_status.return_value = True
    r_log.sheet_id.return_value = 777  # the flat RFQ_Log (ledger-attach target)
    r_log.STATUS_FILED = "filed"
    r_log.STATUS_SENT = "sent"
    r_log.COL_STATUS = "Status"
    r_log.SETTLED_STATUSES = frozenset({"sent", "responded", "closed", "canceled"})

    r_review = mocker.patch("po_materials.rfq_poll.rfq_review")
    r_review.find_row_by_rfq_vendor.return_value = None
    r_review.add_rfq_review_row.return_value = 9001
    r_review.rfq_email_body_template.return_value = "seed body"
    r_review.notes_for_review_row.side_effect = (
        lambda rfq_id, rfq_number, vendor_key, form_box_file_id="":
        f"rfq_id={rfq_id}; rfq_number={rfq_number}; vendor_key={vendor_key}"
        + (f"; form_box_file_id={form_box_file_id}" if form_box_file_id else "")
    )
    r_review.sheet_id.return_value = 555
    r_review.WORKSTREAM_TAG = rfq_review.WORKSTREAM_TAG
    r_review.COL_WORKSTREAM = rfq_review.COL_WORKSTREAM
    r_review.COL_SEND_STATUS = rfq_review.COL_SEND_STATUS
    r_review.STATUS_SENT = rfq_review.STATUS_SENT
    r_review.row_rfq_id.side_effect = rfq_review.row_rfq_id
    r_review.row_rfq_number.side_effect = rfq_review.row_rfq_number
    r_review.row_vendor_key.side_effect = rfq_review.row_vendor_key

    upload = mocker.patch(
        "po_materials.rfq_poll.box_client.upload_bytes_or_new_version",
        return_value={"id": "f-rfq-1", "name": "x", "size": 9},
    )

    seams = {
        "gate": mocker.patch("po_materials.rfq_poll._polling_enabled", return_value=True),
        "resolve_cfg": mocker.patch("po_materials.rfq_poll.resolve_and_log", return_value={}),
        "creds": mocker.patch(
            "po_materials.rfq_poll._resolve_credentials",
            return_value=SimpleNamespace(
                base_url="https://portal.example", bearer="tok", secret=SECRET
            ),
        ),
        "purchaser": mocker.patch(
            "po_materials.rfq_poll.terms_lib.load_purchaser_config",
            return_value=PURCHASER,
        ),
        # Worker I/O (the pinned portal_client contract functions).
        "pending": mocker.patch(
            "po_materials.rfq_poll.portal_client.get_rfqs_pending", return_value=[]
        ),
        "mark_filed": mocker.patch(
            "po_materials.rfq_poll.portal_client.post_rfq_mark_filed", return_value=True
        ),
        "status_sync": mocker.patch(
            "po_materials.rfq_poll.portal_client.post_rfq_status_sync",
            return_value={"ok": True, "updated": 1},
        ),
        # SoR + render + Box seams.
        "vendor": mocker.patch(
            "po_materials.rfq_poll.vendors.get_vendor_by_key", return_value=VENDOR_1
        ),
        "render": mocker.patch(
            "po_materials.rfq_poll.rfq_generate.render_rfq_pdf",
            return_value=b"%PDF-rfq",
        ),
        # R4: the fillable xlsx quote form (lazy `from po_materials import quote_form`).
        # Mocked deterministically so the second Box upload is predictable.
        "render_form": mocker.patch(
            "po_materials.quote_form.render_quote_form",
            return_value=b"PK\x03\x04xlsx-form",
        ),
        "upload": upload,
        "box_folder": mocker.patch(
            "po_materials.rfq_poll._resolve_rfq_box_folder", return_value="folder-rfq"
        ),
        "attach": mocker.patch(
            "po_materials.rfq_poll.smartsheet_client.attach_pdf_to_row", return_value=1
        ),
        # Status-pass review-sheet read.
        "get_rows": mocker.patch(
            "po_materials.rfq_poll.smartsheet_client.get_rows", return_value=[]
        ),
        "rfq_log": r_log,
        "rfq_review": r_review,
        # Per-job mirror (Feature A parity) — mocked here; dedicated tests below run the
        # REAL helper with job_sheet mocked (the test_po_poll idiom).
        "perjob": mocker.patch(
            "po_materials.rfq_poll._append_perjob_rfq_row_best_effort", return_value=None
        ),
        "review_q": mocker.patch("po_materials.rfq_poll.review_queue.add", return_value=1),
        "anomaly": mocker.patch(
            "po_materials.rfq_poll.anomaly_logger.check", return_value=None
        ),
        # Observability + flag-state seams.
        "log": mocker.patch("po_materials.rfq_poll.error_log.log", return_value=None),
        "hb": mocker.patch("po_materials.rfq_poll._write_heartbeat", return_value=None),
        "hb_row": mocker.patch("po_materials.rfq_poll._write_heartbeat_row", return_value=None),
        "marker": mocker.patch("po_materials.rfq_poll._write_watchdog_marker", return_value=None),
        "flags_load": mocker.patch("po_materials.rfq_poll._load_flags", return_value={}),
        "flags_persist": mocker.patch("po_materials.rfq_poll._persist_flags", return_value=None),
        "circuit": mocker.patch(
            "po_materials.rfq_poll.circuit_breaker.is_open", return_value=False
        ),
    }
    return seams


def _run(_patch) -> Any:
    """One cycle inside the (mocked-out) lock — the estimate_poll test idiom."""
    return rfq_poll._poll_inside_lock()


def _logged_codes(_patch) -> list[str]:
    return [kw.get("error_code") for _, kw in _patch["log"].call_args_list]


# ---- dark-ship gate ----------------------------------------------------------------


def test_polling_gate_false_is_total_noop(_patch):
    """Dark-ship: gate false → ZERO Worker calls (no pull, no receipt, no sync)."""
    _patch["gate"].return_value = False
    stats = rfq_poll.poll_once()
    assert stats.skipped_disabled is True
    _patch["pending"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    _patch["status_sync"].assert_not_called()
    _patch["hb"].assert_not_called()
    _patch["marker"].assert_not_called()


# ---- credential resolution (transient ≠ missing) -----------------------------------


def test_transient_base_url_warns_and_skips_without_paging(_patch):
    # A Smartsheet blip on the base-URL read is NOT a missing credential. Live on
    # 2026-07-20 04:42Z a single GET failure fell back to "" and fired a CRITICAL
    # saying PO credentials were unset; both Keychain entries were fine and the
    # daemon self-healed 90s later. That page was false, and it aimed the §43
    # repair at re-provisioning secrets (a high-capability-class action) at a
    # condition needing none. Transient => WARN + skip, never the misconfig CRITICAL.
    _patch["creds"].return_value = rfq_poll.TransientUnavailable(
        reason="SmartsheetError: (<PreparedRequest [GET]>, None)"
    )
    stats = _run(_patch)
    assert stats.halted_transient is True
    assert stats.halted_no_creds is False
    _patch["pending"].assert_not_called()  # still FAIL-CLOSED — it does not poll
    codes = _logged_codes(_patch)
    assert "rfq_creds_transient" in codes
    assert "rfq_creds_missing" not in codes, "a transient read failure must NOT page"
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "WARN"  # not ERROR — nothing is misconfigured


# ---- rfq:v1 integrity (tampered canonical) -----------------------------------------


def test_tampered_canonical_one_shot_flag_never_rendered(_patch):
    """PROVE-THE-CONTROL-BITES: a signed field mutated AFTER signing (here the
    scope text — the recompute-from-fields canonical covers it) → CRITICAL +
    security Review-Queue row + one-shot flag; NEVER rendered, NEVER uploaded,
    NO receipt (the row stays queued in D1 for forensics). Delete the verify and
    this test fails."""
    _patch["pending"].return_value = [_rfq_row(scope_text="tampered after signing")]

    stats = _run(_patch)

    assert stats.rejected == 1
    _patch["render"].assert_not_called()
    _patch["upload"].assert_not_called()
    _patch["mark_filed"].assert_not_called()
    _patch["review_q"].assert_called_once()
    rq = _patch["review_q"].call_args.kwargs
    assert rq["security_flag"] is True
    assert rq["severity"] == Severity.CRITICAL
    assert "rfq_hmac_failure" in _logged_codes(_patch)
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"7": "hmac"}


def test_tampered_vendor_list_is_rejected(_patch):
    """The vendor fan-out list is signature-covered: appending a vendors row after
    signing (recipient poisoning) fails the verify — nothing renders."""
    _patch["pending"].return_value = [
        _rfq_row(vendors=_vendor_rows(["VEN-000001", "VEN-666666"]))
    ]

    stats = _run(_patch)

    assert stats.rejected == 1
    _patch["render"].assert_not_called()
    _patch["mark_filed"].assert_not_called()


# ---- per-vendor fence ---------------------------------------------------------------


def test_unknown_vendor_fenced_other_vendors_still_filed(_patch):
    """A vendor missing from ITS_Vendors is fenced to the Review Queue while the
    OTHER vendors render + file + stage; the receipt carries ONLY the filed
    vendors. Remove the per-vendor fence and this test fails (the whole rfq
    aborts or the unknown vendor silently files)."""
    _patch["pending"].return_value = [
        _rfq_row(vendor_keys=["VEN-000001", "VEN-000007"])
    ]
    _patch["vendor"].side_effect = (
        lambda key: VENDOR_1 if key == "VEN-000001" else None
    )

    stats = _run(_patch)

    assert stats.vendors_fenced == 1
    assert stats.vendors_filed == 1
    assert stats.filed == 1
    _patch["render"].assert_called_once()  # only the known vendor rendered
    assert "rfq_vendor_unknown" in _logged_codes(_patch)
    _patch["review_q"].assert_called_once()  # the fence row
    _patch["mark_filed"].assert_called_once()
    receipt_vendors = _patch["mark_filed"].call_args.kwargs["vendor_results"]
    assert [v["vendor_key"] for v in receipt_vendors] == ["VEN-000001"]
    assert receipt_vendors[0]["box_pdf_file_id"] == "f-rfq-1"
    assert receipt_vendors[0]["review_row_id"] == "9001"  # string per the Worker shape


def test_all_vendors_unknown_withholds_receipt(_patch):
    """EVERY vendor fenced → the receipt is WITHHELD (a receipt with zero
    artifacts would silently drain the rfq) and the rfq is one-shot flagged."""
    _patch["pending"].return_value = [_rfq_row()]
    _patch["vendor"].return_value = None

    stats = _run(_patch)

    assert stats.filed == 0
    _patch["mark_filed"].assert_not_called()
    assert "rfq_all_vendors_fenced" in _logged_codes(_patch)
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"7": "vendors_fenced"}


# ---- idempotent replay --------------------------------------------------------------


def test_replay_after_lost_receipt_appends_nothing_and_reposts_receipt(_patch):
    """The mark-filed-crash contract: a re-served rfq whose RFQ_Log row AND
    review row already exist (the prior cycle filed them, then the receipt was
    lost) appends NO duplicate rows — and still posts the receipt with the
    EXISTING review row id. Remove either find-or-skip and this test fails."""
    _patch["pending"].return_value = [_rfq_row()]
    _patch["rfq_log"].find_row.return_value = {"_row_id": 42, "Status": "filed"}
    _patch["rfq_review"].find_row_by_rfq_vendor.return_value = {"_row_id": 9001}

    stats = _run(_patch)

    _patch["rfq_log"].append_row.assert_not_called()
    _patch["rfq_review"].add_rfq_review_row.assert_not_called()
    _patch["mark_filed"].assert_called_once()
    receipt_vendors = _patch["mark_filed"].call_args.kwargs["vendor_results"]
    assert receipt_vendors[0]["review_row_id"] == "9001"
    assert stats.filed == 1
    # Every-service self-heal (wiring audit 2026-08-12): the replay STILL attaches
    # the RFQ PDF on the EXISTING review row (9001) — the attach used to be
    # fresh-append-only, so a crash between row-add and attach was permanent.
    # (attach mock is the raw attach_pdf_to_row: args = (sheet_id, row_id, ...).)
    assert any(c.args[1] == 9001 for c in _patch["attach"].call_args_list)


def test_happy_path_files_ledger_review_and_receipts_last(_patch):
    """Clean rfq: render → Box → review row (tagged lane) → ledger row → receipt.
    The receipt is LAST and carries the collected artifacts."""
    _patch["pending"].return_value = [_rfq_row()]

    stats = _run(_patch)

    assert stats.filed == 1 and stats.vendors_filed == 1
    _patch["render"].assert_called_once()
    _patch["render_form"].assert_called_once()  # R4: the fillable xlsx quote form too
    # R4: TWO Box uploads per vendor — the RFQ PDF + the quote form.
    assert _patch["upload"].call_count == 2
    all_uploaded = [b for call in _patch["upload"].call_args_list for b in call.args]
    assert b"%PDF-rfq" in all_uploaded and b"PK\x03\x04xlsx-form" in all_uploaded
    _patch["rfq_log"].append_row.assert_called_once()
    log_kwargs = _patch["rfq_log"].append_row.call_args.kwargs
    assert log_kwargs["vendor_key"] == "VEN-000001"
    assert log_kwargs["status"] == "filed"
    _patch["rfq_review"].add_rfq_review_row.assert_called_once()
    _patch["mark_filed"].assert_called_once()
    assert _patch["mark_filed"].call_args.kwargs["rfq_id"] == 7
    # R4: the mark-filed vendor_results carries the form Box file id (→ rfq_vendors.box_form_file_id).
    vendor_results = _patch["mark_filed"].call_args.kwargs["vendor_results"]
    assert vendor_results[0]["box_form_file_id"] == "f-rfq-1"
    # No fence, no flag, no security row on the happy path.
    _patch["review_q"].assert_not_called()
    _patch["flags_persist"].assert_not_called()


# ---- bearer 401 (cycle-stop + catch-order; the PR-A downgrade bug class) ------------


def test_bearer_401_mid_cycle_stops_and_persists_earned_flag(_patch):
    """PROVE-THE-CONTROL-BITES: a 401 (PortalAuthError) raised mid-cycle — here on
    the mark-filed POST of a LATER row, after an EARLIER (tampered) row already
    earned a one-shot flag — STOPS the cycle (rfq_bearer_rejected CRITICAL; the
    status pass never runs) AND still persists the earned flag (the finally-persist,
    FIX 2). Everything stays queued in D1 for a safe re-attempt once the token is
    fixed.

    This RED-lights two ways: (a) if the flag-persist moved back out of the finally
    into a return-value dirty bool, the mid-loop raise would skip it → a duplicate
    CRITICAL/Review-Queue re-alert next cycle for the already-flagged row; (b) if
    _rfq_pass (or _process_pending_rfq) caught PortalTransportError BEFORE
    PortalAuthError, the 401 — a PortalTransportError SUBCLASS — would be swallowed
    as a per-row transient: no _BearerRejectedError, no cycle-stop, no
    rfq_bearer_rejected, and the status pass would run. The load-bearing catch order
    is PortalAuthError first."""
    tampered = _rfq_row(id=8, scope_text="tampered after signing")  # bad HMAC → flag "hmac"
    clean = _rfq_row(id=7)  # verifies + files, then the mark-filed receipt 401s
    _patch["pending"].return_value = [tampered, clean]
    _patch["mark_filed"].side_effect = rfq_poll.portal_client.PortalAuthError("401")

    stats = _run(_patch)

    # Cycle STOPPED on the 401.
    assert stats.bearer_rejected is True
    assert "rfq_bearer_rejected" in _logged_codes(_patch)
    # The whole cycle aborted — not just one row — so the status pass never ran.
    _patch["status_sync"].assert_not_called()
    # FIX 2: the flag the EARLIER (tampered) row earned this cycle still reached disk
    # despite the mid-loop bearer abort (finally-persist, not a return-value bool).
    _patch["flags_persist"].assert_called_once()
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"8": "hmac"}


# ---- status pass (forward-only SENT mirror) -----------------------------------------


def test_status_pass_syncs_sent_rows_then_stamps_ledger(_patch):
    """A SENT review row status-syncs per (rfq, vendor) and stamps the RFQ_Log
    mirror AFTER the POST (D1 first). A settled ledger row generates nothing."""
    sent_row = {
        "_row_id": 1,
        rfq_review.COL_WORKSTREAM: rfq_review.WORKSTREAM_TAG,
        rfq_review.COL_SEND_STATUS: rfq_review.STATUS_SENT,
        rfq_review.COL_JOB_ID: "VEN-000001",
        rfq_review.COL_NOTES: "rfq_id=7; rfq_number=RFQ-2026.001-007; vendor_key=VEN-000001",
    }
    _patch["get_rows"].return_value = [sent_row]
    _patch["rfq_log"].find_row.return_value = {"_row_id": 42, "Status": "filed"}

    stats = _run(_patch)

    _patch["status_sync"].assert_called_once()
    sync_kwargs = _patch["status_sync"].call_args.kwargs
    assert sync_kwargs == {"rfq_id": 7, "vendor_key": "VEN-000001", "status": "sent"}
    _patch["rfq_log"].update_status.assert_called_once_with(
        "RFQ-2026.001-007", "VEN-000001", "sent"
    )
    assert stats.status_synced == 1


def test_status_pass_ignores_foreign_workstream_tag(_patch):
    """P1b: a foreign-tagged row on the RFQ review sheet is never status-synced."""
    foreign = {
        "_row_id": 2,
        rfq_review.COL_WORKSTREAM: "po_materials",  # the PO lane's tag ≠ ours
        rfq_review.COL_SEND_STATUS: rfq_review.STATUS_SENT,
        rfq_review.COL_JOB_ID: "VEN-000001",
        rfq_review.COL_NOTES: "rfq_id=7; rfq_number=X; vendor_key=VEN-000001",
    }
    _patch["get_rows"].return_value = [foreign]

    _run(_patch)

    _patch["status_sync"].assert_not_called()
    assert "rfq_status_foreign_tag" in _logged_codes(_patch)


# ---- lane-tag contracts (cross-lane dispatch impossibility) -------------------------


def test_review_row_workstream_tag_is_distinct_lane_value(mocker):
    """The twin-shape pin: every review row is hard-populated with the DISTINCT
    'po_materials_rfq' lane tag (non-empty — red-team #8), which differs from
    po_send's SendConfig tag so the Stage-2b contamination guard HARD-HELDs an
    RFQ row on any other lane (cross-lane dispatch impossible)."""
    from po_materials import po_review

    add = mocker.patch("po_materials.rfq_review.wsr_review.add_wsr_row", return_value=1)
    mocker.patch("po_materials.rfq_review.sheet_id", return_value=123)
    rfq_review.add_rfq_review_row(
        job_project="2026.001 — Sunrise Solar",
        vendor_key="VEN-000001",
        rfq_date=date(2026, 7, 19),
        pdf_link="https://app.box.com/file/f-rfq-1",
        recipient_to="sam@platt.example",
        cc_display="",
        email_body="body",
        notes="rfq_id=7; rfq_number=N; vendor_key=VEN-000001",
    )
    tag = add.call_args.kwargs["workstream"]
    assert tag == "po_materials_rfq"
    assert tag.strip()  # non-empty — the fail-open-on-absent path can never apply
    assert tag != po_review.WORKSTREAM_TAG  # ≠ the PO lane's SendConfig tag


def test_lane_tag_registered_in_picklist_registry():
    """Registry parity (HOUSE_REFLEXES §4): the new PICKLIST value is registered
    in shared/picklist_validation in the SAME change that writes it."""
    from shared import picklist_validation

    assert "po_materials_rfq" in picklist_validation._RFQ_WORKSTREAM_VALUES
    assert picklist_validation._RFQ_WORKSTREAM_VALUES == {"po_materials_rfq"}
    # The ledger keeps the parent tag; the two vocabularies must not be conflated.
    assert "po_materials_rfq" not in picklist_validation._PO_WORKSTREAM_VALUES


def test_vendor_key_slot_mismatch_returns_none():
    """A review row whose 'Job ID' slot disagrees with the Notes vendor_key copy
    resolves NO vendor (a spliced/hand-edited row must never pick a recipient)."""
    row = {
        rfq_review.COL_JOB_ID: "VEN-000001",
        rfq_review.COL_NOTES: "rfq_id=7; rfq_number=N; vendor_key=VEN-000002",
    }
    assert rfq_review.row_vendor_key(row) is None


# ---- renderer: escaping RED + price-free + determinism ------------------------------

_HOSTILE_LINES = [
    {"position": 1, "part_number": "<b>PN-1</b>",
     "description": '<font color="#ff0000">RED</font> <i>unclosed <broken',
     "qty": 3, "unit": "<u>EA", "line_note": "<onDraw name='x'"},
]
_HOSTILE_RFQ = {
    "rfq_number": "RFQ-2026.001.0009",
    "job_no": "2026.001",
    "job_name": "Sunrise <script>Solar",
    "scope_text": "Line one <b>bold?</b>\n- bullet <i>broken",
    "due_date": "2026-08-14",
}


def test_render_survives_hostile_markup_escaped():
    """PROVE-THE-CONTROL-BITES (red-team #11): deliberately BROKEN reportlab
    markup in every untrusted string slot renders fine BECAUSE form_pdf's
    escaping neutralises it — strip the escaping (raw Paragraph(text)) and the
    paraparser raises on the malformed tags, failing this test."""
    pdf = rfq_generate.render_rfq_pdf(
        _HOSTILE_RFQ, _HOSTILE_LINES, VENDOR_1, PURCHASER,
        rfq_date=date(2026, 7, 19), due_date=date(2026, 8, 14),
    )
    assert pdf.startswith(b"%PDF") and len(pdf) > 500


def test_render_is_byte_deterministic():
    """invariant=1 contract: identical inputs → identical bytes (§47 idempotent
    version-on-conflict crash-retry filing depends on it)."""
    kwargs: dict[str, Any] = dict(rfq_date=date(2026, 7, 19), due_date=date(2026, 8, 14))
    a = rfq_generate.render_rfq_pdf(_HOSTILE_RFQ, _HOSTILE_LINES, VENDOR_1, PURCHASER, **kwargs)
    b = rfq_generate.render_rfq_pdf(_HOSTILE_RFQ, _HOSTILE_LINES, VENDOR_1, PURCHASER, **kwargs)
    assert a == b


def test_renderer_source_is_price_free():
    """NO money columns anywhere (the R2 contract): the renderer's source never
    touches a cents field, the money formatter, or a price/cost column — a money
    column can only be added by editing this pinned surface."""
    src = inspect.getsource(rfq_generate)
    assert "cents" not in src.lower()
    assert "_money" not in src
    assert "format_total" not in src
    for banned in ("Per Unit Cost", "Subtotal Amounts", "Price per Watt", "TOTAL"):
        assert banned not in src


def test_rfq_filename_and_title_are_vendor_scoped():
    from po_materials import rfq_naming

    assert rfq_naming.rfq_pdf_filename("RFQ-1", "Platt Electric Supply").endswith(
        "_RFQ_RFQ-1.pdf"
    )
    assert rfq_naming.rfq_pdf_filename("RFQ-1", None) == "RFQ RFQ-1.pdf"
    assert "Platt" in rfq_naming.rfq_pdf_title("RFQ-1", "Platt Electric Supply")


# ---- RFQ_Log inline attachments + per-job mirror (PO-lane parity, 2026-07-20) ------

# The REAL per-job helper, captured at import time (the fixture replaces the module
# attribute) — used by the end-to-end fence test below (the test_po_poll idiom).
_REAL_RFQ_PERJOB = rfq_poll._append_perjob_rfq_row_best_effort


def test_filed_ledger_row_carries_both_inline_attachments(_patch):
    """A FRESH RFQ_Log append attaches the RFQ PDF and the xlsx quote form to the
    ledger row too (sheet_id=RFQ_Log) — the review row already carried them; the
    operator's 'attached in the Smartsheet row' parity ask."""
    _patch["pending"].return_value = [_rfq_row()]

    _run(_patch)

    ledger_calls = [
        c for c in _patch["attach"].call_args_list if c.args[0] == 777
    ]
    assert len(ledger_calls) == 2
    names = [c.args[2] for c in ledger_calls]
    assert any(n.endswith(".pdf") for n in names)
    assert any(n.endswith(".xlsx") for n in names)
    # The review row keeps its own two attachments (sheet 555) — parity, not a move.
    review_calls = [c for c in _patch["attach"].call_args_list if c.args[0] == 555]
    assert len(review_calls) == 2


def test_crash_retried_filing_self_heals_attachments_without_reappending(_patch):
    """A re-served RFQ whose ledger row already exists (crash between append and
    mark-filed) does NOT re-append the row — but the ledger attaches DO re-fire on
    the existing row (replace-safe deterministic filenames), so an attach that
    failed alongside a lost receipt SELF-HEALS on the retry. The per-job mirror
    also still runs (its own find-or-skip is the duplicate guard)."""
    _patch["rfq_log"].find_row.return_value = {"_row_id": "42"}
    _patch["rfq_review"].find_row_by_rfq_vendor.return_value = {"_row_id": "9001"}
    _patch["pending"].return_value = [_rfq_row()]

    _run(_patch)

    _patch["rfq_log"].append_row.assert_not_called()
    ledger_calls = [c for c in _patch["attach"].call_args_list if c.args[0] == 777]
    assert len(ledger_calls) == 2  # PDF + form, retargeted at the EXISTING row
    assert all(c.args[1] == 42 for c in ledger_calls)
    _patch["perjob"].assert_called_once()  # the mirror's self-heal path stays live


@pytest.mark.parametrize(
    "rfq_number",
    [
        "RFQ-2026.001.1-007",  # migration 0070 site-bearing form (sitePhase > 0)
        "RFQ-2026.001-007",    # sitePhase == 0 — still emitted today, and permanent on pre-0070 drafts
    ],
    ids=["site_bearing", "site_less"],
)
def test_happy_path_mirrors_ledger_row_to_perjob_sheet(_patch, rfq_number):
    """The filing path hands the SAME ledger-row kwargs to the per-job mirror,
    keyed by the job name (the Box/PO per-job folder's name source).

    `rfq_number` is asserted EXACTLY, never by prefix. The previous
    `startswith("RFQ-2026.001")` matched BOTH shapes below and every mangling of
    them — a dropped migration-0070 site segment, a truncation, a reformat — so it
    could not detect a regression in the one thing this hand-off is responsible for.

    Both shapes are parametrized because both are legitimate at HEAD:
    `worker/rfq.ts:713` composes `RFQ-{job_no}.{sitePhase}-{NNN}` only when
    `sitePhase > 0`, and falls back to `RFQ-{job_no}-{NNN}` otherwise. A
    format-validating regex here would be wrong twice over — it would reject the
    site-less form, and it would re-assert a contract the Python side does not own.
    `rfq_poll` never composes this number, so what it owes is byte-exact
    pass-through of whichever shape the Worker allocated.
    """
    _patch["pending"].return_value = [_rfq_row(signed={"rfq_number": rfq_number})]

    _run(_patch)

    _patch["perjob"].assert_called_once()
    job_name, row_kwargs, _corr = _patch["perjob"].call_args.args
    assert job_name == "Sunrise Solar"
    assert row_kwargs["rfq_number"] == rfq_number
    assert row_kwargs["vendor_key"] == "VEN-000001"
    assert row_kwargs["status"] == "filed"


def test_perjob_failure_never_fails_the_filing(_patch, mocker):
    """END-TO-END fence proof: run the REAL helper with ensure_job_sheet raising —
    the filing still completes, the receipt still posts, and the stable WARN
    error_code is logged (Box + the flat RFQ_Log are the SoR)."""
    _patch["perjob"].side_effect = _REAL_RFQ_PERJOB
    mocker.patch(
        "po_materials.rfq_poll.job_sheet.ensure_job_sheet",
        side_effect=RuntimeError("boom"),
    )
    _patch["pending"].return_value = [_rfq_row()]

    _run(_patch)

    _patch["mark_filed"].assert_called_once()
    assert "rfq_perjob_sheet_failed" in _logged_codes(_patch)


def test_perjob_helper_ensures_and_appends_to_target_sheet(mocker):
    """The helper wires FOLDER_PO_JOBS + the flat RFQ_Log as template + the
    sanitized job folder name + the fixed "RFQs" sheet name, then appends with
    sheet_id=<per-job> (independently idempotent per target sheet) and attaches
    BOTH inline files on the fresh row (2026-08-11 parity)."""
    from shared import sheet_ids as si

    ensure = mocker.patch(
        "po_materials.rfq_poll.job_sheet.ensure_job_sheet", return_value=666
    )
    find = mocker.patch("po_materials.rfq_log.find_row", return_value=None)
    append = mocker.patch("po_materials.rfq_log.append_row", return_value=91)
    attach = mocker.patch("po_materials.rfq_poll._attach_file_best_effort")
    row_kwargs = {
        "rfq_number": "RFQ-2026.001-001", "vendor_key": "VEN-000001",
        "job_no": "2026.001", "vendor_name": "Platt", "status": "filed",
    }

    rfq_poll._append_perjob_rfq_row_best_effort(
        "Sunrise Solar", row_kwargs, "corr-1",
        pdf_filename="Platt_RFQ_RFQ-2026.001-001.pdf", pdf_bytes=b"%PDF-rfq",
        form_filename="RFQ-2026.001-001 - Platt - Quote Form.xlsx", form_bytes=b"PK-xlsx",
    )

    ensure.assert_called_once_with(
        si.FOLDER_PO_JOBS,
        si.SHEET_RFQ_LOG,
        "Sunrise Solar",
        rfq_poll.PERJOB_RFQ_SHEET_NAME,
        workspace_id=si.WORKSPACE_PURCHASE_ORDERS,
        workstream="po_materials",
        correlation_id="corr-1",
    )
    find.assert_called_once_with("RFQ-2026.001-001", "VEN-000001", sheet_id=666)
    append.assert_called_once_with(sheet_id=666, **row_kwargs)
    assert attach.call_args_list == [
        mocker.call(91, "Platt_RFQ_RFQ-2026.001-001.pdf", b"%PDF-rfq", "corr-1",
                    sheet_id=666),
        mocker.call(91, "RFQ-2026.001-001 - Platt - Quote Form.xlsx", b"PK-xlsx", "corr-1",
                    content_type=rfq_poll._XLSX_MIME, sheet_id=666),
    ]


def test_perjob_helper_skips_the_form_attach_when_degraded_to_pdf_only(mocker):
    """form_bytes=None (the R4 PDF-only degrade) attaches ONLY the RFQ PDF — a
    None form must never become a zero-byte xlsx attachment."""
    mocker.patch("po_materials.rfq_poll.job_sheet.ensure_job_sheet", return_value=666)
    mocker.patch("po_materials.rfq_log.find_row", return_value=None)
    mocker.patch("po_materials.rfq_log.append_row", return_value=91)
    attach = mocker.patch("po_materials.rfq_poll._attach_file_best_effort")

    rfq_poll._append_perjob_rfq_row_best_effort(
        "Sunrise Solar",
        {"rfq_number": "RFQ-2026.001-001", "vendor_key": "VEN-000001"},
        "corr-1",
        pdf_filename="x.pdf", pdf_bytes=b"%PDF", form_filename="x.xlsx", form_bytes=None,
    )

    attach.assert_called_once_with(91, "x.pdf", b"%PDF", "corr-1", sheet_id=666)


def test_perjob_helper_is_idempotent_against_target_sheet(mocker):
    """The (rfq, vendor) already present in the TARGET sheet → appends NOTHING —
    the duplicate guard behind the 'independently idempotent' claim (a crash
    between the flat append and the mirror re-runs cleanly). Mutation-proven:
    dropping the None-check ships silent duplicate rows into a §51 sheet. The
    inline attaches STILL run against the existing row (every-service self-heal;
    deterministic filenames replace, never duplicate)."""
    mocker.patch("po_materials.rfq_poll.job_sheet.ensure_job_sheet", return_value=666)
    mocker.patch("po_materials.rfq_log.find_row", return_value={"_row_id": "1"})
    append = mocker.patch("po_materials.rfq_log.append_row", return_value=1)
    attach = mocker.patch("po_materials.rfq_poll._attach_file_best_effort")

    rfq_poll._append_perjob_rfq_row_best_effort(
        "Sunrise Solar",
        {"rfq_number": "RFQ-2026.001-001", "vendor_key": "VEN-000001"},
        "corr-1",
        pdf_filename="x.pdf", pdf_bytes=b"%PDF", form_filename="x.xlsx", form_bytes=b"PK",
    )

    append.assert_not_called()
    assert attach.call_count == 2
    assert attach.call_args_list[0].args[0] == 1


def test_rfq_box_resolver_reads_the_lanes_own_root(mocker):
    """The 2026-08-11 split: the resolver reads po_naming.CFG_BOX_PORTAL_ROOT under
    Workstream='po_materials' and files under ROOT→<job>→'RFQs' — the intermediate
    'Purchase Orders' level is gone."""
    from po_materials import po_naming

    read = mocker.patch(
        "po_materials.rfq_poll._read_str_setting", return_value="root-po"
    )
    ensure = mocker.patch(
        "po_materials.rfq_poll.box_client.get_or_create_folder",
        side_effect=["job-9", "rfqs-3"],
    )

    assert rfq_poll._resolve_rfq_box_folder("Sunrise Solar") == "rfqs-3"

    read.assert_called_once_with(
        po_naming.CFG_BOX_PORTAL_ROOT, "",
        workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM,
    )
    assert ensure.call_args_list == [
        mocker.call("root-po", "Sunrise Solar"),
        mocker.call("job-9", "RFQs"),
    ]


# ---- Fence durability: a failed Review-Queue ticket must NEVER cost the flag -------
#
# 2026-08-10 incident. Every fence wrote its Review-Queue row BEFORE the one-shot flag,
# and `review_queue.add` propagates SmartsheetError by contract. So a Smartsheet blip at
# fence time skipped the flag, the RFQ re-served next cycle, and a PERMANENT one-shot
# fence degraded into unbounded per-cycle re-ticketing: 4 PENDING rows for ONE RFQ across
# two cycles — and the first ticket had actually COMMITTED (the non-idempotent
# unknown-commit case), so the "retry" duplicated a row that was already there.


def test_all_vendors_fenced_flags_even_when_the_ticket_write_fails(_patch):
    """The rfq-level fence one-shot-flags the RFQ even if its Review-Queue write raises,
    and escalates the lost ticket to CRITICAL (the item IS fenced — an operator with no
    ticket would otherwise never learn it). Revert the fix and the flag is never written,
    which is exactly the unbounded re-fence loop."""
    _patch["pending"].return_value = [_rfq_row(vendor_keys=["VEN-000007"])]
    _patch["vendor"].return_value = None  # the sole vendor is unknown -> all fenced
    _patch["review_q"].side_effect = rfq_poll.smartsheet_client.SmartsheetError("boom")

    stats = _run(_patch)

    # The flag reached disk despite BOTH ticket writes failing.
    _patch["flags_persist"].assert_called_once()
    persisted = _patch["flags_persist"].call_args.args[0]
    assert persisted == {"7": "vendors_fenced"}
    # ... and the lost tickets are loud, not silent.
    codes = _logged_codes(_patch)
    assert "rfq_fence_ticket_failed" in codes
    assert stats.fenced == 1
    # The RFQ was NOT receipted (nothing filed) — unchanged behaviour.
    _patch["mark_filed"].assert_not_called()


def test_hmac_fence_flags_even_when_the_ticket_write_fails(_patch):
    """Same durability for the SECURITY fence. Its docstring promises the CRITICAL fires
    'only on the FIRST sighting' — a lost flag breaks that promise and re-fires a
    security CRITICAL + security-flagged row every 120 s."""
    row = _rfq_row(vendor_keys=["VEN-000001"])
    row["hmac"] = "deadbeef" * 8  # wrong signature
    _patch["pending"].return_value = [row]
    _patch["review_q"].side_effect = rfq_poll.smartsheet_client.SmartsheetError("boom")

    _run(_patch)

    _patch["flags_persist"].assert_called_once()
    assert _patch["flags_persist"].call_args.args[0] == {"7": "hmac"}
    codes = _logged_codes(_patch)
    assert "rfq_hmac_failure" in codes
    assert "rfq_fence_ticket_failed" in codes
    _patch["render"].assert_not_called()  # never rendered, never filed
    _patch["mark_filed"].assert_not_called()


def test_standing_flagged_item_keeps_the_daemon_off_ok(_patch):
    """An item flag-skipped every cycle is a PERMANENTLY WEDGED item, so the heartbeat
    must not read OK. `total_flagged` omitted `skipped_flagged`, so the cycle AFTER a
    fence flipped Last Cycle Status back to OK and the daemon looked healthy while the
    RFQ sat wedged out of the queue forever."""
    _patch["pending"].return_value = [_rfq_row(vendor_keys=["VEN-000001"])]
    _patch["flags_load"].return_value = {"7": "vendors_fenced"}

    stats = _run(_patch)

    assert stats.skipped_flagged == 1
    assert stats.filed == 0
    _patch["render"].assert_not_called()
    status = _patch["hb_row"].call_args.kwargs["status"]
    assert status == "WARN", f"a standing fence must not read OK (got {status!r})"
    summary = _patch["hb_row"].call_args.kwargs["error_summary"]
    assert summary is not None and "standing=1" in summary
