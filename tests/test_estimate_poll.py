"""RED-suite unit tests for po_materials/estimate_poll.py — the vendor-estimate
pull daemon (ADR-0004 Lane 1, PR-A: screen → classify → refuse invoices → Box →
Estimate_Log, everything else → needs_review).

Fully mocked at the module seams, the tests/test_po_poll.py house idiom (no live
Smartsheet / Box / Worker). Every test here is a PROVE-THE-CONTROL-BITES test:
it asserts the CONTROL fires (integrity refusal, §34 screen, doc-type gate,
dark-ship gate, per-row fence, sandbox degrade) and would fail if the control
were deleted.

Contract pins exercised (the PR-A shared contract):
  * est:v1 HMAC — signatures in these tests are computed IN-TEST from the pinned
    canonical string ("est:v1"\\n est_uuid\\n job_no\\n filename\\n declared_mime
    \\n str(size_bytes)\\n sha256), independent of shared.portal_hmac — a daemon
    verifying a drifted canonical fails the happy path here.
  * Integrity failures (tampered bytes vs the SIGNED sha256, chunk-index gaps)
    → one-shot flag + security Review-Queue row, NO result post (forensics),
    NO Box upload, and the §34 screen never touches the unverified bytes.
  * screen_attachment MALICIOUS → CRITICAL naming the uploading account +
    security-flagged Review-Queue row + result 'refused'; NO Box.
  * classifier invoice/ap_report → result 'refused' detail wrong_doc_type:<t> +
    Estimate_Log 'refused' + POLICY_EDGE review row; NO Box (visible, never silent).
  * clean quote → Box upload (original bytes) + Estimate_Log 'needs_review' +
    page previews posted + result 'needs_review' carrying box_file_id.
  * polling gate false → dark-ship no-op (zero Worker calls).
  * per-row fence — one hostile row never aborts the batch (estimate_service_failed).
  * sandbox degrade (run_sandboxed → None ⇒ no pages / no previews) still lands
    the doc needs_review; the daemon NEVER dies from a hostile document.

Run with: pytest -q tests/test_estimate_poll.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
from types import SimpleNamespace
from typing import Any

import pytest

from po_materials import estimate_poll, po_attach_screen
from shared import portal_client
from shared.error_log import Severity
from shared.review_queue import ReviewReason

SECRET = "est-test-secret"

_MINIMAL_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

CLEAN = po_attach_screen.ScreenResult("clean", "L2", "ok")
MALICIOUS = po_attach_screen.ScreenResult("malicious", "L3", "clamav:Eicar-Test-Signature")


# ---- row / chunk builders (est:v1 canonical computed IN-TEST — the golden math) ----


def _sign_est(secret: str, row: dict[str, Any]) -> str:
    canonical = "\n".join([
        "est:v1",
        row["est_uuid"],
        row["job_no"],
        row["filename"],
        row["declared_mime"],
        str(row["size_bytes"]),
        row["sha256"],
    ])
    return _hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _est_row(data: bytes, **over: Any) -> dict[str, Any]:
    """A pending po_estimates row signed EXACTLY as the Worker would (est:v1)."""
    est_id = int(over.pop("id", 41))
    row: dict[str, Any] = {
        "id": est_id,
        "est_uuid": f"u-est-{est_id}",
        "job_no": str(over.pop("job_no", "2026.001")),
        "job_name": "Sunrise Solar",
        "filename": str(over.pop("filename", "Platt Quote 4471.pdf")),
        "declared_mime": str(over.pop("declared_mime", "application/pdf")),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": "pending",
        "uploaded_by": "office.admin",
        "created_at": 1234,
    }
    row["hmac"] = _sign_est(SECRET, row)
    row.update(over)  # post-signing overrides = deliberate tampering in tests
    return row


def _one_chunk(data: bytes) -> list[dict[str, Any]]:
    return [{"chunk_index": 0, "chunk_total": 1,
             "chunk_b64": base64.b64encode(data).decode()}]


def _gapped_chunks(data: bytes) -> list[dict[str, Any]]:
    """Indexes 0 and 2 with a declared total of 3 — index 1 is missing (the gap)."""
    half = len(data) // 2
    return [
        {"chunk_index": 0, "chunk_total": 3,
         "chunk_b64": base64.b64encode(data[:half]).decode()},
        {"chunk_index": 2, "chunk_total": 3,
         "chunk_b64": base64.b64encode(data[half:]).decode()},
    ]


# ---- fixture (the po_poll _patch idiom) --------------------------------------------


@pytest.fixture
def _patch(mocker):
    est_log = mocker.patch("po_materials.estimate_poll.estimate_log")
    est_log.find_row_by_uuid.return_value = None
    est_log.append_row.return_value = 1
    est_log.update_status.return_value = True
    # The daemon writes the ledger via these module constants — mirror the real
    # lowercase D1 vocabulary so status assertions see the true strings.
    est_log.STATUS_RECEIVED = "received"
    est_log.STATUS_REFUSED = "refused"
    est_log.STATUS_NEEDS_REVIEW = "needs_review"

    # Function-level Box patch (NOT the whole module — the daemon's transient
    # except clause catches box_client.BoxError, which must stay a real class).
    upload = mocker.patch(
        "po_materials.estimate_poll.box_client.upload_bytes_or_new_version",
        return_value={"id": "f-est-1", "name": "x", "size": 9},
    )
    # The ledger-row inline attach (#79 parity, wiring audit 2026-08-12).
    attach = mocker.patch(
        "po_materials.estimate_poll.smartsheet_client.attach_pdf_to_row", return_value=1
    )

    seams = {
        "attach": attach,
        "gate": mocker.patch(
            "po_materials.estimate_poll._polling_enabled", return_value=True
        ),
        "resolve_cfg": mocker.patch(
            "po_materials.estimate_poll.resolve_and_log", return_value={}
        ),
        "creds": mocker.patch(
            "po_materials.estimate_poll._resolve_credentials",
            return_value=SimpleNamespace(
                base_url="https://portal.example", bearer="tok", secret=SECRET
            ),
        ),
        # ITS_Config-backed knobs (would otherwise hit live Smartsheet).
        "clamav": mocker.patch(
            "po_materials.estimate_poll._attach_clamav_enabled", return_value=False
        ),
        "max_pages": mocker.patch(
            "po_materials.estimate_poll._max_pages_preview", return_value=12
        ),
        # PR-B: the per-cycle extraction-ladder config snapshot — pinned DARK here
        # (all tier gates false) so this suite keeps asserting the PR-A pipeline;
        # the ladder's own behavior is covered by tests/test_estimate_ladder_wiring.py.
        "tiers": mocker.patch(
            "po_materials.estimate_poll._resolve_tier_config",
            return_value=estimate_poll._TierConfig(
                tier1_enabled=False, tier1_xlsx_enabled=False, tier2_enabled=False,
                ocr_enabled=False,
                model="qwen3.5:9b", ollama_base_url="http://127.0.0.1:11434",
                confidence_threshold=0.75, timeout_seconds=600,
            ),
        ),
        # Worker I/O (the pinned portal_client contract functions).
        "pending": mocker.patch(
            "po_materials.estimate_poll.portal_client.get_estimates_pending",
            return_value=[],
        ),
        "claim": mocker.patch(
            "po_materials.estimate_poll.portal_client.claim_estimate",
            return_value=True,
        ),
        "chunks": mocker.patch(
            "po_materials.estimate_poll.portal_client.get_estimate_chunks",
            return_value=[],
        ),
        "result": mocker.patch(
            "po_materials.estimate_poll.portal_client.post_estimate_result",
            return_value=True,
        ),
        "preview_post": mocker.patch(
            "po_materials.estimate_poll.portal_client.post_estimate_preview",
            return_value=True,
        ),
        # §34 screen + doc-type classifier + preview render + sandbox (source-module
        # patches so the daemon sees them however it reaches them).
        "screen": mocker.patch(
            "po_materials.estimate_poll.po_attach_screen.screen_attachment",
            return_value=CLEAN,
        ),
        "extract": mocker.patch(
            "po_materials.estimate_classify.extract_pages_text",
            return_value=["QUOTE # 4471\nPlatt Electric Supply\nTotal $1,234.50"],
        ),
        "classify": mocker.patch(
            "po_materials.estimate_classify.classify_doc_type",
            return_value=("quote", 0.95),
        ),
        "render": mocker.patch(
            "po_materials.estimate_preview.render_page_pngs",
            return_value=[b"\x89PNG-page-1", b"\x89PNG-page-2"],
        ),
        "sandbox": mocker.patch(
            "po_materials.estimate_sandbox.run_sandboxed",
            return_value=None,
        ),
        # Box folder resolver + ledger + review seams.
        "upload": upload,
        "box_folder": mocker.patch(
            "po_materials.estimate_poll._resolve_quotes_box_folder",
            return_value="folder-est",
        ),
        "est_log": est_log,
        "review_q": mocker.patch(
            "po_materials.estimate_poll.review_queue.add", return_value=1
        ),
        "anomaly": mocker.patch(
            "po_materials.estimate_poll.anomaly_logger.check", return_value=None
        ),
        # Observability + flag-state seams.
        "log": mocker.patch("po_materials.estimate_poll.error_log.log", return_value=None),
        "hb": mocker.patch(
            "po_materials.estimate_poll._write_heartbeat", return_value=None
        ),
        "hb_row": mocker.patch(
            "po_materials.estimate_poll._write_heartbeat_row", return_value=None
        ),
        "marker": mocker.patch(
            "po_materials.estimate_poll._write_watchdog_marker", return_value=None
        ),
        "flags_load": mocker.patch(
            "po_materials.estimate_poll._load_flags", return_value={}
        ),
        "flags_persist": mocker.patch(
            "po_materials.estimate_poll._persist_flags", return_value=None
        ),
        "circuit": mocker.patch(
            "po_materials.estimate_poll.circuit_breaker.is_open", return_value=False
        ),
    }
    return seams


def _run(_patch) -> Any:
    """One cycle inside the (mocked-out) lock — the po_poll test idiom."""
    return estimate_poll._poll_inside_lock()


def _logged_codes(_patch) -> list[str]:
    return [kw.get("error_code") for _, kw in _patch["log"].call_args_list]


def _result_kwargs(_patch) -> dict[str, Any]:
    assert _patch["result"].call_args is not None, "expected a result post"
    return dict(_patch["result"].call_args.kwargs)


def _est_log_values(_patch) -> str:
    """Every positional + keyword value across every ledger call, stringified —
    naming-drift-tolerant containment assertions (e.g. the written Status)."""
    out: list[str] = []
    for call in _patch["est_log"].mock_calls:
        out.extend(str(a) for a in call.args)
        out.extend(f"{k}={v}" for k, v in call.kwargs.items())
    return " | ".join(out)


def _critical_log_text(_patch) -> str:
    """Concatenated text of every CRITICAL error_log.log call."""
    out: list[str] = []
    for call in _patch["log"].call_args_list:
        sev = call.args[0] if call.args else call.kwargs.get("severity")
        if sev == Severity.CRITICAL:
            out.append(" ".join(str(a) for a in call.args))
            out.append(" ".join(f"{k}={v}" for k, v in call.kwargs.items()))
    return " | ".join(out)


# ---- 6. dark-ship gate --------------------------------------------------------------


def test_polling_gate_false_is_total_noop(_patch):
    """Dark-ship: gate false → ZERO Worker calls (no pull, no claim, no post)."""
    _patch["gate"].return_value = False
    stats = estimate_poll.poll_once()
    assert stats.skipped_disabled is True
    _patch["pending"].assert_not_called()
    _patch["claim"].assert_not_called()
    _patch["result"].assert_not_called()
    _patch["preview_post"].assert_not_called()
    _patch["hb"].assert_not_called()
    _patch["marker"].assert_not_called()


# ---- 7. credential resolution: transient-vs-absent ---------------------------------


def test_no_creds_fail_closed_still_pages(_patch):
    # The COMPANION to the transient test below, and the reason both must exist:
    # the transient fix must not silence the REAL misconfig it was hiding behind.
    # A genuinely absent base URL / bearer / secret does not self-heal, so it must
    # still stop the daemon AND page CRITICAL.
    _patch["creds"].return_value = None
    stats = _run(_patch)
    assert stats.halted_no_creds is True
    assert stats.halted_transient is False
    _patch["pending"].assert_not_called()
    assert "estimate_creds_missing" in _logged_codes(_patch)
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "ERROR"


def test_transient_base_url_warns_and_skips_without_paging(_patch):
    # A Smartsheet blip on the base-URL read is NOT a missing credential. Live on
    # 2026-07-20 04:42Z a single GET failure in po_poll fell back to "" and fired a
    # CRITICAL saying PO credentials were unset; both Keychain entries were fine and
    # the daemon self-healed 90s later. That page was false, and it aimed the §43
    # repair at re-provisioning secrets (a high-capability-class action) at a
    # condition needing none. Every puller shared the flaw.
    # Transient => WARN + skip, never the misconfig CRITICAL.
    _patch["creds"].return_value = estimate_poll.TransientUnavailable(
        reason="SmartsheetError: (<PreparedRequest [GET]>, None)"
    )
    stats = _run(_patch)
    assert stats.halted_transient is True
    assert stats.halted_no_creds is False
    _patch["pending"].assert_not_called()  # still FAIL-CLOSED — it does not poll
    codes = _logged_codes(_patch)
    assert "estimate_creds_transient" in codes
    assert "estimate_creds_missing" not in codes, "a transient read failure must NOT page"
    _, hb_kwargs = _patch["hb_row"].call_args
    assert hb_kwargs["status"] == "WARN"  # not ERROR — nothing is misconfigured


# ---- 1./2. integrity path (tampered bytes / malformed chunks) ----------------------


def _assert_integrity_refusal(_patch):
    """The pinned integrity posture: one-shot flag persisted (state_io-backed
    seam), security-flagged Review-Queue row, NO result post (bytes stay in D1
    for forensics), NO Box upload, and the §34 screen never judged the bytes."""
    _patch["upload"].assert_not_called()
    _patch["result"].assert_not_called()
    _patch["screen"].assert_not_called()
    _patch["review_q"].assert_called_once()
    assert _patch["review_q"].call_args.kwargs["security_flag"] is True
    assert "estimate_integrity_failure" in _logged_codes(_patch)
    _patch["flags_persist"].assert_called_once()
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"41": "integrity"}


def test_tampered_bytes_vs_signed_sha_hits_integrity_path(_patch):
    """Chunks reassembling to bytes whose sha256 differs from the SIGNED digest
    are an integrity failure — the est:v1 signature extends to the content."""
    _patch["pending"].return_value = [_est_row(_MINIMAL_PDF)]
    _patch["chunks"].return_value = _one_chunk(b"%PDF-1.4 mutated after signing %%EOF")

    _run(_patch)

    _assert_integrity_refusal(_patch)


def test_malformed_chunk_gap_hits_integrity_path(_patch):
    """A gap in chunk indexes (0,2 of 3) can never reassemble to the signed
    sha256 — same one-shot integrity posture, never a crash."""
    _patch["pending"].return_value = [_est_row(_MINIMAL_PDF)]
    _patch["chunks"].return_value = _gapped_chunks(_MINIMAL_PDF)

    _run(_patch)  # must not raise

    _assert_integrity_refusal(_patch)


# ---- 3. §34 screen: malicious -------------------------------------------------------


def test_screen_malicious_refused_named_and_never_filed(_patch):
    """PROVE-THE-CONTROL-BITES: a MALICIOUS §34 verdict → CRITICAL naming the
    uploading account + security-flagged Review-Queue row + result 'refused';
    the bytes NEVER reach Box or the ledger's filed path. Stub the screener to
    clean (or skip it) and this test fails."""
    _patch["pending"].return_value = [_est_row(_MINIMAL_PDF)]
    _patch["chunks"].return_value = _one_chunk(_MINIMAL_PDF)
    _patch["screen"].return_value = MALICIOUS

    _run(_patch)

    _patch["upload"].assert_not_called()
    rq = _patch["review_q"].call_args.kwargs
    assert rq["security_flag"] is True
    assert rq["severity"] == Severity.CRITICAL
    # CRITICAL names the account (error-log leg and the review-row summary).
    assert "office.admin" in rq["summary"]
    assert "office.admin" in _critical_log_text(_patch)
    assert "estimate_malicious" in _logged_codes(_patch)
    assert _result_kwargs(_patch)["status"] == "refused"
    # One-shot flagged — a transient post-back failure can never re-fire this
    # CRITICAL every 120s.
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"41": "refused"}


# ---- 4. doc-type gate: invoice refused ---------------------------------------------


def test_classifier_invoice_refused_visibly_never_filed(_patch):
    """invoice/ap_report must be classified OUT of the PO path — result 'refused'
    with detail wrong_doc_type:invoice, an Estimate_Log 'refused' row, and a
    POLICY_EDGE review row (visible, never silently dropped). NO Box upload."""
    _patch["pending"].return_value = [_est_row(_MINIMAL_PDF, filename="Nassau Inv 8891.pdf")]
    _patch["chunks"].return_value = _one_chunk(_MINIMAL_PDF)
    _patch["classify"].return_value = ("invoice", 0.98)

    _run(_patch)

    _patch["upload"].assert_not_called()
    result = _result_kwargs(_patch)
    assert result["status"] == "refused"
    assert "wrong_doc_type:invoice" in str(result.get("detail", ""))
    assert "refused" in _est_log_values(_patch)
    _patch["review_q"].assert_called_once()
    assert _patch["review_q"].call_args.kwargs["reason"] == ReviewReason.POLICY_EDGE
    assert "estimate_wrong_doc_type" in _logged_codes(_patch)


# ---- 5. clean quote happy path ------------------------------------------------------


def test_clean_quote_filed_logged_previewed_and_resulted(_patch):
    """Clean quote: verify → screen clean → classify quote → Box upload (original
    bytes) → Estimate_Log 'needs_review' → page previews posted → result
    'needs_review' carrying the Box file id (PR-A: everything extractable still
    lands needs_review — no extraction tier yet)."""
    _patch["pending"].return_value = [_est_row(_MINIMAL_PDF)]
    _patch["chunks"].return_value = _one_chunk(_MINIMAL_PDF)

    _run(_patch)

    _patch["upload"].assert_called_once()
    upload_call = _patch["upload"].call_args
    upload_values = list(upload_call.args) + list(upload_call.kwargs.values())
    assert _MINIMAL_PDF in upload_values  # the ORIGINAL bytes, never a re-encode
    assert "needs_review" in _est_log_values(_patch)
    # Both rendered pages posted, 1-based per the preview contract.
    assert _patch["preview_post"].call_count == 2
    pages = [c.kwargs.get("page") for c in _patch["preview_post"].call_args_list]
    assert pages == [1, 2]
    assert all(c.kwargs.get("png_b64") for c in _patch["preview_post"].call_args_list)
    result = _result_kwargs(_patch)
    assert result["status"] == "needs_review"
    assert result.get("box_file_id") == "f-est-1"
    # Ledger-row inline attach (#79 parity, wiring audit 2026-08-12): the filed
    # ORIGINAL rides the Estimate_Log row, content-typed by the verified MIME —
    # the office reads the quote from the sheet without a Box round-trip.
    _patch["attach"].assert_called_once()
    attach_call = _patch["attach"].call_args
    assert attach_call.args[1] == 1  # append_row's returned row id
    assert _MINIMAL_PDF in list(attach_call.args) + list(attach_call.kwargs.values())
    assert attach_call.kwargs.get("content_type") == "application/pdf"
    # A clean doc is not a security event and needs no one-shot flag.
    _patch["review_q"].assert_not_called()
    _patch["flags_persist"].assert_not_called()


# ---- 7. per-row fence ---------------------------------------------------------------


def test_per_row_fence_second_row_survives_first_row_crash(_patch):
    """A crash inside row 1's processing (chunk fetch explodes) is fenced with
    error_code estimate_service_failed; row 2 STILL processes to completion.
    Remove the per-row fence and this test fails (the batch aborts)."""
    row1 = _est_row(_MINIMAL_PDF, id=41)
    row2 = _est_row(_MINIMAL_PDF, id=42)
    _patch["pending"].return_value = [row1, row2]
    _patch["chunks"].side_effect = [RuntimeError("boom"), _one_chunk(_MINIMAL_PDF)]

    _run(_patch)  # must not raise

    assert "estimate_service_failed" in _logged_codes(_patch)
    # Row 2 genuinely completed: filed + resulted.
    _patch["upload"].assert_called_once()
    result = _result_kwargs(_patch)
    assert result["status"] == "needs_review"
    assert result["estimate_id"] == 42
    # The cycle COMPLETED — heartbeat + watchdog marker written (a crash skips them).
    _patch["hb"].assert_called()
    _patch["marker"].assert_called()


# ---- 8. sandbox degrade (timeout/kill/crash) ----------------------------------------


def test_sandbox_timeout_degrades_doc_daemon_survives(_patch):
    """The sandbox contract: run_sandboxed returns None on timeout/kill/crash
    (never raises) → text extraction yields no pages, classify degrades to
    ('other', …), previews render empty — and the doc STILL lands needs_review.
    The daemon NEVER dies from a hostile document (no exception escapes)."""
    _patch["pending"].return_value = [_est_row(_MINIMAL_PDF)]
    _patch["chunks"].return_value = _one_chunk(_MINIMAL_PDF)
    # Every sandboxed stage timed out: no extracted text, no rendered pages.
    _patch["sandbox"].return_value = None
    _patch["extract"].return_value = []
    _patch["classify"].return_value = ("other", 0.0)  # the degrade verdict
    _patch["render"].return_value = []

    _run(_patch)  # the control: no exception escapes

    result = _result_kwargs(_patch)
    assert result["status"] == "needs_review"
    assert result.get("box_file_id") == "f-est-1"
    _patch["preview_post"].assert_not_called()  # degraded: explicit no-preview path
    assert "estimate_preview_empty" in _logged_codes(_patch)
    assert "estimate_service_failed" not in _logged_codes(_patch)


# ---- 9. bearer rejection (401) must STOP the cycle, never be fence-swallowed --------


def test_bearer_401_during_refused_post_stops_cycle_and_persists_flag(_patch):
    """PROVE-THE-CONTROL-BITES (ops-stds review): a 401 while posting the refused
    disposition is re-raised by _post_refused_result as _BearerRejectedError and
    must reach the cycle-stop handler — NOT be swallowed by the generic per-row
    fence as estimate_service_failed (which would leave bearer_rejected False and
    re-alert every 120s). Also pins the flag-persistence half: the one-shot flag
    the refusal wrote BEFORE the aborting post still reaches _persist_flags (the
    finally path), so the row is skipped — not re-alerted — next cycle."""
    row1 = _est_row(_MINIMAL_PDF, id=41)
    row2 = _est_row(_MINIMAL_PDF, id=42)
    _patch["pending"].return_value = [row1, row2]
    _patch["chunks"].return_value = _one_chunk(_MINIMAL_PDF)
    _patch["screen"].return_value = MALICIOUS
    _patch["result"].side_effect = portal_client.PortalAuthError("401 unauthorized")

    stats = _run(_patch)  # must not raise — the cycle stops, the daemon survives

    assert stats.bearer_rejected is True
    assert "estimate_bearer_rejected" in _logged_codes(_patch)
    # The control: the 401 never degrades into the generic fence's error code.
    assert "estimate_service_failed" not in _logged_codes(_patch)
    # The cycle STOPPED at row 1 — row 2 was never claimed (same bearer, no point).
    assert _patch["claim"].call_count == 1
    # Flag-persistence half: the refusal's one-shot flag SURVIVED the abort, so a
    # later-fixed bearer does not replay this row's CRITICAL/Review-Queue alert.
    _patch["flags_persist"].assert_called_once()
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"41": "refused"}


def test_bearer_401_during_preview_post_stops_cycle_and_persists_prior_flag(_patch):
    """Same control through the OTHER helper: a 401 during preview posting (row 2,
    clean) aborts the cycle via _BearerRejectedError — and the one-shot flag row
    1's refusal wrote EARLIER in the same cycle still reaches _persist_flags."""
    row1 = _est_row(_MINIMAL_PDF, id=41)
    row2 = _est_row(_MINIMAL_PDF, id=42)
    _patch["pending"].return_value = [row1, row2]
    _patch["chunks"].return_value = _one_chunk(_MINIMAL_PDF)
    _patch["screen"].side_effect = [MALICIOUS, CLEAN]  # row 1 refused, row 2 filed
    _patch["preview_post"].side_effect = portal_client.PortalAuthError("401")

    stats = _run(_patch)  # must not raise

    assert stats.bearer_rejected is True
    assert "estimate_bearer_rejected" in _logged_codes(_patch)
    assert "estimate_service_failed" not in _logged_codes(_patch)
    # Row 2 aborted mid-service: its needs_review result post never happened —
    # the only result post this cycle is row 1's refused disposition.
    statuses = [c.kwargs.get("status") for c in _patch["result"].call_args_list]
    assert statuses == ["refused"]
    # Row 1's flag persisted despite the abort landing during row 2.
    _patch["flags_persist"].assert_called_once()
    (persisted,), _ = _patch["flags_persist"].call_args
    assert persisted == {"41": "refused"}


# ---- Box folder resolution (the 2026-08-11 PO-lane root split) ----------------------


def test_quotes_box_resolver_reads_the_po_lanes_own_root(mocker):
    """The resolver reads po_naming.CFG_BOX_PORTAL_ROOT under Workstream='po_materials'
    (NOT the shared safety root) and files under ROOT→<job>→'Vendor Quotes' — the
    intermediate 'Purchase Orders' level is gone."""
    from po_materials import po_naming

    read = mocker.patch(
        "po_materials.estimate_poll._read_str_setting", return_value="root-po"
    )
    ensure = mocker.patch(
        "po_materials.estimate_poll.box_client.get_or_create_folder",
        side_effect=["job-9", "quotes-3"],
    )

    assert estimate_poll._resolve_quotes_box_folder("Sunrise Solar") == "quotes-3"

    read.assert_called_once_with(
        po_naming.CFG_BOX_PORTAL_ROOT, "",
        workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM,
    )
    assert ensure.call_args_list == [
        mocker.call("root-po", "Sunrise Solar"),
        mocker.call("job-9", "Vendor Quotes"),
    ]
