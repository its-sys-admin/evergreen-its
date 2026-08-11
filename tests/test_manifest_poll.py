"""RED-suite unit tests for field_ops/manifest_poll.py — the materials-manifest import
daemon (PR3b / ADR-0005: verify → screen → sandboxed extract → parse → Box → grid post).

Fully mocked at the module seams, the tests/test_estimate_poll.py house idiom (no live
Smartsheet / Box / Worker). Every test here is a PROVE-THE-CONTROL-BITES test: it asserts
the CONTROL fires and would fail if that control were deleted.

Contract pins exercised:
  * manifest:v1 HMAC — signatures are computed IN-TEST from the pinned canonical string
    ("manifest:v1"\\n manifest_uuid\\n job_id\\n filename\\n declared_mime\\n
    str(size_bytes)\\n sha256), independent of shared.portal_hmac, so a daemon verifying a
    drifted canonical fails the happy path here.
  * Integrity failures (tampered bytes vs the SIGNED digest, a chunk gap, a bad signature)
    → one-shot flag + security Review-Queue row, NO result post (the bytes stay in D1 for
    forensics), NO Box upload, and the §34 screen never touches unverified bytes.
  * screen MALICIOUS → CRITICAL naming the uploading account + security-flagged review row
    + result 'refused'; NO Box, NO parse.
  * A CLEAN but unreadable document (sandbox degrade, or a grid with no importable rows)
    → 'refused' with an ORDINARY review row, not a security one.
  * clean + readable → Box upload of the ORIGINAL bytes + paged row posts + result
    'parsed' carrying box_file_id, profile and the proposed column map.
  * The RESULT POST IS LAST — a failure at any earlier step leaves the row serviceable.
  * polling gate false → dark-ship no-op (zero Worker calls, no heartbeat, no marker).
  * per-row fence — one hostile row never aborts the batch.
  * 401 anywhere → the cycle STOPS once (a dead bearer must not burn the whole pool).
  * An unresolved Box root is a CONFIG gap: ERROR, row left claimed and UNFLAGGED.

Run with: pytest -q tests/test_manifest_poll.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
from types import SimpleNamespace
from typing import Any

import pytest

from field_ops import manifest_poll
from po_materials import po_attach_screen
from shared import portal_client
from shared.error_log import Severity
from shared.review_queue import ReviewReason

SECRET = "manifest-test-secret"

_MINIMAL_XLSX = b"PK\x03\x04" + b"\x00" * 40
_MINIMAL_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

CLEAN = po_attach_screen.ScreenResult("clean", "L2", "ok")
SUSPICIOUS = po_attach_screen.ScreenResult("suspicious", "L2", "pdf:embedded_js")
MALICIOUS = po_attach_screen.ScreenResult("malicious", "L3", "clamav:Eicar-Test-Signature")

# One header + one data row. THREE recognisable header tokens is the floor: the parser
# requires MIN_HEADER_HITS to call a row a header at all, deliberately, because a data row
# whose description happens to read "Description" would otherwise qualify. A two-column
# fixture parses as a metadata block and is (correctly) refused as having no importable
# rows — which is what the first draft of this suite got wrong.
_GRID = {
    "sheets": [
        {"name": "Export", "rows": [["Part Number", "Description", "Qty"], ["7006955", "Pile", 4]]}
    ]
}


# ---- row / chunk builders (manifest:v1 canonical computed IN-TEST) -----------------


def _sign(secret: str, row: dict[str, Any]) -> str:
    canonical = "\n".join([
        "manifest:v1",
        row["manifest_uuid"],
        row["job_id"],
        row["filename"],
        row["declared_mime"],
        str(row["size_bytes"]),
        row["sha256"],
    ])
    return _hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _row(data: bytes, **over: Any) -> dict[str, Any]:
    """A pending job_manifests row signed EXACTLY as the Worker would."""
    manifest_id = int(over.pop("id", 41))
    row: dict[str, Any] = {
        "id": manifest_id,
        "manifest_uuid": f"u-man-{manifest_id}",
        "job_id": str(over.pop("job_id", "JOB-2026-014")),
        "filename": str(over.pop("filename", "Customer BOM.xlsx")),
        "declared_mime": str(over.pop("declared_mime", manifest_poll.MIME_XLSX)),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": "pending",
        "uploaded_by": "office.admin",
        "created_at": 1234,
    }
    row["hmac"] = _sign(SECRET, row)
    row.update(over)  # post-signing overrides = deliberate tampering in tests
    return row


def _one_chunk(data: bytes) -> list[dict[str, Any]]:
    return [{"chunk_index": 0, "chunk_total": 1, "chunk_b64": base64.b64encode(data).decode()}]


def _gapped_chunks(data: bytes) -> list[dict[str, Any]]:
    """Indexes 0 and 2 with a declared total of 3 — index 1 is missing (the gap)."""
    half = len(data) // 2
    return [
        {"chunk_index": 0, "chunk_total": 3, "chunk_b64": base64.b64encode(data[:half]).decode()},
        {"chunk_index": 2, "chunk_total": 3, "chunk_b64": base64.b64encode(data[half:]).decode()},
    ]


@pytest.fixture
def _patch(mocker):
    upload = mocker.patch(
        "field_ops.manifest_poll.box_client.upload_bytes_or_new_version",
        return_value={"id": "f-man-1", "name": "x", "size": 9},
    )
    seams = {
        "gate": mocker.patch("field_ops.manifest_poll._polling_enabled", return_value=True),
        "resolve_cfg": mocker.patch("field_ops.manifest_poll.resolve_and_log", return_value={}),
        "creds": mocker.patch(
            "field_ops.manifest_poll._resolve_credentials",
            return_value=SimpleNamespace(
                base_url="https://portal.example", bearer="tok", secret=SECRET
            ),
        ),
        "clamav": mocker.patch(
            "field_ops.manifest_poll._attach_clamav_enabled", return_value=False
        ),
        "box_folder": mocker.patch(
            "field_ops.manifest_poll._resolve_manifests_box_folder", return_value="fld-1"
        ),
        "upload": upload,
        "screen": mocker.patch(
            "field_ops.manifest_poll.po_attach_screen.screen_attachment", return_value=CLEAN
        ),
        "sandbox": mocker.patch(
            "field_ops.manifest_poll.estimate_sandbox.run_sandboxed",
            return_value=json.dumps(_GRID).encode(),
        ),
        "pending": mocker.patch("field_ops.manifest_poll.portal_client.get_manifests_pending"),
        "claim": mocker.patch(
            "field_ops.manifest_poll.portal_client.claim_manifest", return_value=True
        ),
        "chunks": mocker.patch("field_ops.manifest_poll.portal_client.get_manifest_chunks"),
        "post_rows": mocker.patch(
            "field_ops.manifest_poll.portal_client.post_manifest_rows", return_value=2
        ),
        "post_preview": mocker.patch(
            "field_ops.manifest_poll.portal_client.post_manifest_preview", return_value=True
        ),
        "post_result": mocker.patch(
            "field_ops.manifest_poll.portal_client.post_manifest_result", return_value=True
        ),
        "review": mocker.patch("field_ops.manifest_poll.review_queue.add", return_value=1),
        "log": mocker.patch("field_ops.manifest_poll.error_log.log"),
        # State seams — the suite must never touch ~/its/state.
        "load_flags": mocker.patch("field_ops.manifest_poll._load_flags", return_value={}),
        "persist_flags": mocker.patch("field_ops.manifest_poll._persist_flags"),
        "hb": mocker.patch("field_ops.manifest_poll._write_heartbeat"),
        "hb_row": mocker.patch("field_ops.manifest_poll._write_heartbeat_row"),
        "marker": mocker.patch("field_ops.manifest_poll._write_watchdog_marker"),
        "fetch_fails": mocker.patch("field_ops.manifest_poll._FETCH_FAILS"),
        "flush": mocker.patch("field_ops.manifest_poll.sustained_failure.flush_retry_recovery"),
        "breaker": mocker.patch(
            "field_ops.manifest_poll.circuit_breaker.is_open", return_value=False
        ),
    }
    return seams


def _logs(seams, severity=None, code=None) -> list[Any]:
    out = []
    for call in seams["log"].call_args_list:
        sev = call.args[0] if call.args else None
        ec = call.kwargs.get("error_code")
        if (severity is None or sev == severity) and (code is None or ec == code):
            out.append(call)
    return out


# ---- the dark-ship gate -----------------------------------------------------------


def test_gate_false_is_a_pure_no_op(_patch):
    """A dark lane is an intentional state, not an anomaly: zero Worker calls, and
    deliberately NO heartbeat and NO watchdog marker (which is exactly why Check C WARNs
    until the operator both loads the plist AND flips the gate)."""
    _patch["gate"].return_value = False
    stats = manifest_poll.poll_once()
    assert stats.skipped_disabled is True
    _patch["pending"].assert_not_called()
    _patch["hb"].assert_not_called()
    _patch["marker"].assert_not_called()


# ---- the happy path ---------------------------------------------------------------


def test_clean_readable_manifest_files_posts_the_grid_and_reports_parsed(_patch):
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data)]
    _patch["chunks"].return_value = _one_chunk(data)

    stats = manifest_poll.poll_once()

    assert stats.filed == 1
    assert stats.refused == 0 and stats.integrity_failures == 0
    # The ORIGINAL bytes are filed, never a re-encode.
    assert _patch["upload"].call_args.args[2] == data
    # The uuid prefix turns a crash-retry into a Box VERSION, not a duplicate file.
    assert _patch["upload"].call_args.args[1].startswith("u-man-41 - ")
    kwargs = _patch["post_result"].call_args.kwargs
    assert kwargs["status"] == "parsed"
    assert kwargs["box_file_id"] == "f-man-1"
    assert kwargs["profile"]  # the parser's document classification rode back
    assert "qty_default" in kwargs["column_map"]
    assert stats.rows_posted == 2


def test_result_post_is_last_so_an_earlier_failure_leaves_the_row_serviceable(_patch):
    """The result post is the COMMIT POINT. If Box fails, nothing is reported and the row
    stays claimed — a crash before the commit re-serves it and every prior step is
    idempotent."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["upload"].side_effect = manifest_poll.box_client.BoxError("box down")

    stats = manifest_poll.poll_once()

    assert stats.filed == 0
    assert stats.errors == 1
    _patch["post_result"].assert_not_called()
    _patch["persist_flags"].assert_not_called()  # NOT flagged — it must retry


# ---- integrity (Invariant 2) ------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("tampered bytes", "bytes"),
        ("tampered signature", "hmac"),
        ("chunk gap", "gap"),
    ],
)
def test_integrity_failure_flags_and_never_posts_a_result(_patch, label, mutate):
    """No result post is deliberate: the bytes stay in D1 for forensics. The screen must
    also never run — unverified bytes are never handed to a parser."""
    data = _MINIMAL_XLSX
    row = _row(data)
    if mutate == "bytes":
        _patch["chunks"].return_value = _one_chunk(data + b"tampered")
    elif mutate == "hmac":
        row["hmac"] = "0" * 64
        _patch["chunks"].return_value = _one_chunk(data)
    else:
        _patch["chunks"].return_value = _gapped_chunks(data)
    _patch["pending"].return_value = [row]

    stats = manifest_poll.poll_once()

    assert stats.integrity_failures == 1, label
    _patch["post_result"].assert_not_called()
    _patch["upload"].assert_not_called()
    _patch["screen"].assert_not_called()
    assert _logs(_patch, Severity.CRITICAL, "manifest_integrity_failed")
    assert _patch["review"].call_args.kwargs["security_flag"] is True
    _patch["persist_flags"].assert_called_once()


def test_a_non_integer_size_fails_the_signature_closed(_patch):
    """str(48213.0) is "48213.0" in Python and "48213" in JS, so a float size_bytes would
    silently break every canonical. The daemon coerces to -1 and fails CLOSED."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data, size_bytes=float(len(data)))]
    _patch["chunks"].return_value = _one_chunk(data)

    stats = manifest_poll.poll_once()

    assert stats.integrity_failures == 1
    _patch["upload"].assert_not_called()


# ---- §34 screening ----------------------------------------------------------------


def test_malicious_screen_names_the_account_and_refuses_before_filing(_patch):
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["screen"].return_value = MALICIOUS

    stats = manifest_poll.poll_once()

    assert stats.refused == 1
    _patch["upload"].assert_not_called()
    _patch["sandbox"].assert_not_called()  # never parsed
    crit = _logs(_patch, Severity.CRITICAL, "manifest_malicious")
    assert crit and "office.admin" in crit[0].args[2]
    rq = _patch["review"].call_args.kwargs
    assert rq["security_flag"] is True
    assert rq["reason"] is ReviewReason.SECURITY_TRIGGER
    assert _patch["post_result"].call_args.kwargs["status"] == "refused"


def test_suspicious_screen_imports_with_a_warning_not_a_refusal(_patch):
    """DELIBERATELY REWRITTEN 2026-08-11 (operator decision — the rewrite is the point).

    This test used to pin suspicious → refused. The first real BOM through the lane
    (Bradley 1 Customer BOM) refused on L2:pdf_active_content:OpenAction — an artifact
    virtually every vendor-exported PDF carries — and the operator dropped the refusal
    for THIS lane: an office upload's bytes are only ever opened by ITS inside the
    killable sandbox, so the friction bought protection against a reader that is
    already contained. The verdict is NEVER silent: a WARN (`manifest_active_content`)
    and a parse note the validate screen shows beside the grid. Malicious still refuses
    (the test above), and the PO/estimate lanes keep the refuse posture."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["screen"].return_value = SUSPICIOUS

    stats = manifest_poll.poll_once()

    assert stats.refused == 0
    assert stats.filed == 1  # imported: filed to Box, grid posted, result parsed
    assert _logs(_patch, Severity.WARN, "manifest_active_content")
    assert not _logs(_patch, Severity.WARN, "manifest_suspicious")
    assert not _logs(_patch, Severity.CRITICAL, "manifest_malicious")
    _patch["upload"].assert_called_once()  # the original DOES reach Box now
    # The warning reaches the validate screen as a parse note.
    notes = _patch["post_result"].call_args.kwargs.get("parse_notes") or ""
    assert "ACTIVE CONTENT" in notes
    assert "pdf:embedded_js" in notes
    # No Review-Queue ticket for a mere warning — the note + WARN are the surfaces.
    _patch["review"].assert_not_called()


# ---- unreadable documents (ordinary, NOT a security event) ------------------------


def test_sandbox_degrade_refuses_as_unreadable_not_as_a_security_event(_patch):
    """A hostile or corrupt container reaps the child and run_sandboxed returns None. The
    daemon must degrade the DOCUMENT, never die — and must not cry wolf: this is an
    ordinary review item asking the office for a better copy."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["sandbox"].return_value = None

    stats = manifest_poll.poll_once()

    assert stats.refused == 1
    assert _logs(_patch, Severity.WARN, "manifest_unreadable")
    assert not _logs(_patch, Severity.CRITICAL)
    assert _patch["review"].call_args.kwargs["reason"] is ReviewReason.POLICY_EDGE
    # Falsy, not absent: the fence now routes through review_queue.safe_add, which
    # forwards security_flag explicitly (default False). The assertion's intent —
    # "this is not a security event" — is unchanged.
    assert not _patch["review"].call_args.kwargs.get("security_flag")
    assert _patch["post_result"].call_args.kwargs["detail"] == "extract_failed"
    _patch["upload"].assert_not_called()


def test_a_grid_with_no_importable_rows_is_refused_visibly(_patch):
    """An empty export parses fine and yields nothing. Refusing VISIBLY beats filing a
    manifest whose grid the office would open to find blank."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["sandbox"].return_value = json.dumps({"sheets": [{"name": "E", "rows": []}]}).encode()

    stats = manifest_poll.poll_once()

    assert stats.refused == 1
    assert _patch["post_result"].call_args.kwargs["detail"] == "no_importable_rows"
    _patch["upload"].assert_not_called()


# ---- config gap vs per-row defect -------------------------------------------------


def test_unresolved_box_root_leaves_the_row_claimed_and_unflagged(_patch):
    """A missing Box root is a CONFIG gap, not a per-row defect: flagging it would wedge
    a good document forever. The row self-heals the moment the root is configured."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["box_folder"].return_value = None

    stats = manifest_poll.poll_once()

    assert stats.errors == 1 and stats.filed == 0
    assert _logs(_patch, Severity.ERROR, "manifest_box_root_unresolved")
    _patch["post_result"].assert_not_called()
    _patch["persist_flags"].assert_not_called()  # deliberately UNFLAGGED


# ---- fences ------------------------------------------------------------------------


def test_one_hostile_row_never_aborts_the_batch(_patch):
    data = _MINIMAL_XLSX
    good = _row(data, id=7)
    _patch["pending"].return_value = [_row(data, id=41), good]
    _patch["chunks"].side_effect = [RuntimeError("boom"), _one_chunk(data)]

    stats = manifest_poll.poll_once()

    assert stats.errors == 1
    assert stats.filed == 1  # the second row still landed
    assert _logs(_patch, Severity.ERROR, "manifest_service_failed")


def test_a_401_stops_the_cycle_once_rather_than_burning_the_pool(_patch):
    """The SAME bearer fails every route, so continuing would mint one CRITICAL per row."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data, id=1), _row(data, id=2), _row(data, id=3)]
    _patch["chunks"].side_effect = portal_client.PortalAuthError("401")

    stats = manifest_poll.poll_once()

    assert stats.bearer_rejected is True
    crit = _logs(_patch, Severity.CRITICAL, "manifest_bearer_rejected")
    assert len(crit) == 1
    _patch["post_result"].assert_not_called()


def test_pending_fetch_failure_escalates_on_the_shared_capped_ladder(_patch):
    """Hand-rolling `n >= THRESHOLD` is AST-forbidden; the shared helper is what keeps a
    sustained outage from minting thousands of unreclaimable CRITICAL rows."""
    _patch["fetch_fails"].record.return_value = 5
    _patch["pending"].side_effect = portal_client.PortalTransportError("worker down")

    stats = manifest_poll.poll_once()

    assert stats.errors == 1
    assert _logs(_patch, Severity.CRITICAL, "manifest_pending_fetch_sustained")


def test_a_successful_but_empty_poll_clears_the_sustained_counter(_patch):
    """Reset sits BEFORE the empty early-out — an empty-but-successful poll is a healthy
    cycle and must not leave the outage counter armed."""
    _patch["pending"].return_value = []

    manifest_poll.poll_once()

    _patch["fetch_fails"].reset.assert_called_once()


def test_an_already_flagged_row_is_skipped_without_re_alerting(_patch):
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data)]
    _patch["load_flags"].return_value = {"41": "refused"}

    stats = manifest_poll.poll_once()

    assert stats.skipped_flagged == 1
    _patch["claim"].assert_not_called()
    _patch["review"].assert_not_called()


# ---- credentials -------------------------------------------------------------------


def test_missing_credentials_fail_closed_without_a_watchdog_marker(_patch):
    """Skipping the marker is deliberate: a sustained outage must still trip the Check-C
    staleness floor rather than looking healthy because the daemon 'ran'."""
    _patch["creds"].return_value = None

    stats = manifest_poll.poll_once()

    assert stats.halted_no_creds is True
    assert _logs(_patch, Severity.CRITICAL, "manifest_creds_missing")
    _patch["pending"].assert_not_called()
    _patch["marker"].assert_not_called()


def test_a_transient_base_url_blip_is_a_warn_not_a_credentials_critical(_patch):
    """The distinction that fired a false CRITICAL on po_poll live: a circuit-open blip
    must not be reported as missing credentials."""
    _patch["creds"].return_value = manifest_poll.TransientUnavailable(reason="circuit_open")

    stats = manifest_poll.poll_once()

    assert stats.halted_transient is True
    assert _logs(_patch, Severity.WARN, "manifest_creds_transient")
    assert not _logs(_patch, Severity.CRITICAL)
    _patch["marker"].assert_not_called()


# ---- chunk reassembly (the pure function) ------------------------------------------


@pytest.mark.parametrize(
    ("label", "chunks"),
    [
        ("empty set", []),
        ("inconsistent total", [
            {"chunk_index": 0, "chunk_total": 1, "chunk_b64": "QUJD"},
            {"chunk_index": 1, "chunk_total": 2, "chunk_b64": "QUJD"},
        ]),
        ("bool index", [{"chunk_index": True, "chunk_total": 1, "chunk_b64": "QUJD"}]),
        ("duplicate index", [
            {"chunk_index": 0, "chunk_total": 2, "chunk_b64": "QUJD"},
            {"chunk_index": 0, "chunk_total": 2, "chunk_b64": "QUJD"},
        ]),
        ("non-strict base64", [{"chunk_index": 0, "chunk_total": 1, "chunk_b64": "not!b64"}]),
        ("empty b64", [{"chunk_index": 0, "chunk_total": 1, "chunk_b64": ""}]),
    ],
)
def test_reassembly_refuses_every_malformation(label, chunks):
    """The chunk set was written atomically with its parent row, so a broken set is tamper
    or a serving defect — never a benign partial."""
    with pytest.raises(ValueError):
        manifest_poll._reassemble_chunks(chunks)


def test_reassembly_accepts_out_of_order_chunks():
    """Out-of-order is NOT an error — it is fixed by the sort before concatenation."""
    data = b"abcdefgh"
    chunks = [
        {"chunk_index": 1, "chunk_total": 2, "chunk_b64": base64.b64encode(data[4:]).decode()},
        {"chunk_index": 0, "chunk_total": 2, "chunk_b64": base64.b64encode(data[:4]).decode()},
    ]
    assert manifest_poll._reassemble_chunks(chunks) == data


# ---- previews (best-effort by design) ---------------------------------------------


def test_a_preview_failure_never_costs_the_import(_patch):
    """Previews are the validate screen's fidelity aid, not its correctness gate."""
    data = _MINIMAL_PDF
    _patch["pending"].return_value = [_row(data, declared_mime=manifest_poll.MIME_PDF,
                                           filename="Customer BOM.pdf")]
    _patch["chunks"].return_value = _one_chunk(data)
    # PDFs go through parse_native (tables), then render_page_pngs for previews.
    _patch["sandbox"].side_effect = [
        json.dumps({"tables": [[[["Part Number", "Description", "Qty"], ["7006955", "Pile", "4"]]]]}).encode(),
        json.dumps({"pngs": ["QUJD"]}).encode(),
    ]
    _patch["post_preview"].side_effect = portal_client.PortalTransportError("nope")

    stats = manifest_poll.poll_once()

    assert stats.filed == 1  # the import still landed
    assert _patch["post_result"].call_args.kwargs["status"] == "parsed"
    assert _logs(_patch, Severity.WARN, "manifest_preview_post_failed")


# ---- A5 (2026-08-11): Worker 4xx is PERMANENT, and the grid clamps to Worker bounds ----


def test_a_worker_4xx_is_permanent_flag_plus_ticket_not_an_eternal_retry(_patch):
    """The audit-A5 wedge: a 400-class rejection re-serves identically every cycle, so
    'transient' meant ~720 ERROR rows/day, no CRITICAL, no ticket, item never draining.
    Now: one-shot flag + Review-Queue ticket, and the row is terminal this cycle."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data, id=9)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["post_rows"].side_effect = portal_client.PortalTransportError(
        "POST rows unexpected status 422", status_code=422
    )

    stats = manifest_poll.poll_once()

    assert stats.errors == 1
    assert _logs(_patch, Severity.ERROR, "manifest_worker_rejected")
    assert not _logs(_patch, Severity.ERROR, "manifest_transient")
    _patch["review"].assert_called_once()
    # The one-shot flag reached the persisted set — next cycle skips, never re-tickets.
    flagged = _patch["persist_flags"].call_args.args[0]
    assert flagged == {"9": "worker_rejected"}


def test_a_worker_5xx_stays_transient_and_unflagged(_patch):
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data, id=9)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["post_rows"].side_effect = portal_client.PortalTransportError(
        "POST rows unexpected status 502", status_code=502
    )

    stats = manifest_poll.poll_once()

    assert stats.errors == 1
    assert _logs(_patch, Severity.ERROR, "manifest_transient")
    assert not _logs(_patch, Severity.ERROR, "manifest_worker_rejected")
    _patch["persist_flags"].assert_not_called()
    _patch["review"].assert_not_called()


def test_a_statusless_transport_error_stays_transient(_patch):
    """Connection drops / decode failures carry no status — genuinely transient."""
    data = _MINIMAL_XLSX
    _patch["pending"].return_value = [_row(data, id=9)]
    _patch["chunks"].return_value = _one_chunk(data)
    _patch["post_rows"].side_effect = portal_client.PortalTransportError("connection reset")

    stats = manifest_poll.poll_once()

    assert stats.errors == 1
    assert _logs(_patch, Severity.ERROR, "manifest_transient")
    _patch["persist_flags"].assert_not_called()


def _mk_parsed(rows):
    from field_ops import manifest_parse

    return manifest_parse.ParsedManifest(
        profile="customer_bom",
        column_map=manifest_parse.ColumnMap(mapping={}, labels={}, qty_candidates=[], qty_default=None),
        rows=rows,
        meta={},
        notes=["existing note"],
    )


def _mk_row(index, cells):
    from field_ops import manifest_parse

    return manifest_parse.ParsedRow(index=index, cells=cells, kind="data")


def test_clamp_passes_a_clean_grid_through_untouched():
    parsed = _mk_parsed([_mk_row(1, ["a", "b"])])
    assert manifest_poll._clamp_to_worker_bounds(parsed) is parsed  # zero-copy fast path


def test_clamp_truncates_rows_cells_and_chars_with_visible_notes():
    wide = _mk_row(1, ["c"] * (manifest_poll.WORKER_MAX_ROW_CELLS + 50))
    long_cell = _mk_row(2, ["x" * (manifest_poll.WORKER_MAX_CELL_CHARS + 100)])
    parsed = _mk_parsed([wide, long_cell])

    out = manifest_poll._clamp_to_worker_bounds(parsed)

    assert len(out.rows[0].cells) == manifest_poll.WORKER_MAX_ROW_CELLS
    assert len(out.rows[1].cells[0]) == manifest_poll.WORKER_MAX_CELL_CHARS
    # VISIBLE, never silent: each clamp appends a note the validate screen displays.
    assert "existing note" in out.notes
    assert any("columns" in n for n in out.notes)
    assert any("characters" in n for n in out.notes)


def test_clamp_caps_the_row_count_at_the_worker_ceiling():
    rows = [_mk_row(i + 1, ["v"]) for i in range(manifest_poll.WORKER_MAX_ROWS_TOTAL + 5)]
    out = manifest_poll._clamp_to_worker_bounds(_mk_parsed(rows))
    assert len(out.rows) == manifest_poll.WORKER_MAX_ROWS_TOTAL
    assert any("TRUNCATED: the document has" in n for n in out.notes)
