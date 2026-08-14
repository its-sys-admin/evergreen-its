"""Unit tests for safety_reports/portal_poll.py.

All external services mocked. Exercises the verify → dispatch → receipt cycle,
the fail-closed credential gate, the HMAC-reject path, and the seen-set
fast-paths. Structure mirrors tests/test_weekly_send_poll.py.

The G1 item-photo screening-pass tests (bottom section) run REAL crypto
(shared.portal_hmac item-photo protocol) + the REAL photo_screen pipeline on real
bytes — only transport, Box, and state files are mocked.
"""
from __future__ import annotations

import base64 as _b64lib
import io as _io
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as _PILImage

from safety_reports import portal_poll
from safety_reports.intake import ProcessResult
from safety_reports.photo_screen import PhotoScreenResult
from safety_reports.portal_poll import DAEMON_NAME, _poll_inside_lock, poll_once
from shared import portal_hmac as _portal_hmac
from shared.error_log import Severity as _Sev


def _row(uuid: str = "u1") -> dict[str, Any]:
    return {
        "submission_uuid": uuid,
        "job_id": "JOB-1",
        "form_code": "jha-v1",
        "work_date": "2026-06-05",
        "payload_json": '{"work_location": "A"}',
        "amends_uuid": None,
        "hmac": "deadbeef",
        "created_at": 1_717_600_000,
    }


def _processed(
    uuid: str = "u1", link: str = "https://app.box.com/file/f9", file_id: str = "f9"
) -> ProcessResult:
    return ProcessResult(
        status="processed", message_id=uuid, correlation_id="c",
        box_link=link, box_file_id=file_id,
    )


@pytest.fixture
def _patch_all(mocker):
    """Default mock surface covering the whole poll cycle."""
    return {
        "creds": mocker.patch.object(
            portal_poll, "_resolve_credentials",
            return_value=portal_poll._PortalCreds(
                base_url="https://portal.example.com", bearer="bearer", secret="secret",
            ),
        ),
        "get_pending": mocker.patch.object(
            portal_poll.portal_client, "get_pending", return_value=[]
        ),
        "mark_filed": mocker.patch.object(
            portal_poll.portal_client, "mark_filed", return_value=True
        ),
        "mark_rejected": mocker.patch.object(
            portal_poll.portal_client, "mark_rejected", return_value=True
        ),
        "process": mocker.patch.object(
            portal_poll.intake, "process_portal_submission", return_value=_processed()
        ),
        "verify": mocker.patch.object(portal_poll, "_verify_row_hmac", return_value=True),
        "load_seen": mocker.patch.object(portal_poll, "_load_seen", return_value={}),
        "persist_seen": mocker.patch.object(portal_poll, "_persist_seen"),
        "hb": mocker.patch.object(portal_poll, "_write_heartbeat"),
        "hb_row": mocker.patch.object(portal_poll, "_write_heartbeat_row"),
        "wd": mocker.patch.object(portal_poll, "_write_watchdog_marker"),
        # A4 unfiled-backlog marker — mocked so existing cycle tests neither touch real
        # state nor assert on it; dedicated tests below exercise the real helper on a tmp path.
        "backlog": mocker.patch.object(portal_poll, "_record_pending_backlog"),
        # Consecutive-fetch-failure counter (sustained-outage escalation) — mocked so tests
        # neither touch real state nor depend on the on-disk counter; default count=1 (first
        # failure → ERROR). A test raises the return to the threshold to exercise CRITICAL.
        "rec_fail": mocker.patch.object(portal_poll, "_record_fetch_failure", return_value=1),
        "reset_fail": mocker.patch.object(portal_poll, "_reset_fetch_failures"),
        "is_open": mocker.patch.object(portal_poll.circuit_breaker, "is_open", return_value=False),
        "log": mocker.patch.object(portal_poll.error_log, "log"),
        "review": mocker.patch.object(portal_poll.review_queue, "add"),
        "anomaly": mocker.patch.object(portal_poll.anomaly_logger, "check"),
        # Job-sync push leg: default to an empty set so the push short-circuits and
        # the existing cycle tests neither hit Smartsheet nor POST anything.
        "list_all_jobs": mocker.patch.object(
            portal_poll.active_jobs, "list_all_jobs", return_value=[]
        ),
        "push_jobs": mocker.patch.object(
            portal_poll.portal_client, "push_jobs",
            return_value={"ok": True, "upserted": 0, "deactivated": 0},
        ),
        # PR-4 request-driven PDF cache servicing pass: default to NO pending PDF
        # requests so the existing cycle tests neither hit Box nor POST any chunk.
        "get_pdf_requests": mocker.patch.object(
            portal_poll.portal_client, "get_pdf_requests", return_value=[]
        ),
        "upload_filed_pdf": mocker.patch.object(
            portal_poll.portal_client, "upload_filed_pdf",
            return_value={"ok": True, "ready": True, "stored": True, "received": 1},
        ),
        "download_file": mocker.patch.object(
            portal_poll.box_client, "download_file", return_value=b"%PDF-1.4 bytes"
        ),
        # G1 item-photo screening pass: default to an EMPTY pending queue so the
        # existing cycle tests neither screen nor file anything.
        "get_item_photos": mocker.patch.object(
            portal_poll.portal_client, "get_item_photos_pending", return_value=[]
        ),
        "post_photo_result": mocker.patch.object(
            portal_poll.portal_client, "post_item_photo_result", return_value=True
        ),
        "box_folder": mocker.patch.object(
            portal_poll.box_client, "get_or_create_folder",
            side_effect=["FOLD-1", "FOLD-2", "FOLD-3"] * 50,
        ),
        "box_upload": mocker.patch.object(
            portal_poll.box_client, "upload_bytes_or_new_version",
            return_value={"id": "box-9", "name": "photo_5.jpg", "size": 100},
        ),
        # Bad-HMAC one-shot flag state — mocked so tests never touch real state files.
        "photo_flags_load": mocker.patch.object(
            portal_poll, "_load_item_photo_flags", return_value={}
        ),
        "photo_flags_persist": mocker.patch.object(
            portal_poll, "_persist_item_photo_flags"
        ),
        # DR-photo-pool daily-pool screening pass: default to an EMPTY pending queue
        # so the existing cycle tests neither screen nor file anything.
        "get_daily_photos": mocker.patch.object(
            portal_poll.portal_client, "get_daily_photos_pending", return_value=[]
        ),
        "post_daily_result": mocker.patch.object(
            portal_poll.portal_client, "post_daily_photo_result", return_value=True
        ),
        "daily_flags_load": mocker.patch.object(
            portal_poll, "_load_daily_photo_flags", return_value={}
        ),
        "daily_flags_persist": mocker.patch.object(
            portal_poll, "_persist_daily_photo_flags"
        ),
        # ITS_Config reads (ClamAV gate + portal Box root): default to the declared
        # fallback ("" → clamav default-OFF, Box root unset). Tests override per-key.
        "read_setting": mocker.patch.object(
            portal_poll, "_read_str_setting",
            side_effect=lambda key, fallback: fallback,
        ),
    }


# ---- happy path + receipt ------------------------------------------------


def test_empty_queue_ok_heartbeat(_patch_all):
    result = _poll_inside_lock()
    assert result.scanned == 0 and result.filed == 0
    _patch_all["hb"].assert_called_once()
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "OK"


def test_verified_processed_row_is_filed_and_receipted(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    result = _poll_inside_lock()
    assert result.filed == 1 and result.scanned == 1
    _patch_all["verify"].assert_called_once()
    _patch_all["process"].assert_called_once()
    _patch_all["mark_filed"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        submission_uuid="u1", box_link="https://app.box.com/file/f9",
        box_file_id="f9",  # PR-4: the structural id threaded to the receipt
    )
    # Recorded as filed in the seen-set for a future lost-receipt re-post.
    persisted = _patch_all["persist_seen"].call_args.args[0]
    assert persisted["u1"] == {
        "status": "filed", "box_link": "https://app.box.com/file/f9", "box_file_id": "f9",
    }


def test_review_queue_result_is_drained_and_counted(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["process"].return_value = ProcessResult(
        status="review_queue", message_id="u1", correlation_id="c", box_link=None
    )
    result = _poll_inside_lock()
    assert result.reviewed == 1 and result.filed == 0
    # Drained with an empty link (the Review Queue entry is the durable record);
    # no box_file_id on the review path → box_file_id=None.
    _patch_all["mark_filed"].assert_called_once_with(
        "https://portal.example.com", "bearer", submission_uuid="u1", box_link="",
        box_file_id=None,
    )
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "WARN"


def test_already_filed_result_is_receipted(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["process"].return_value = ProcessResult(
        status="already_filed", message_id="u1", correlation_id="c",
        box_link="https://app.box.com/file/old",
    )
    result = _poll_inside_lock()
    assert result.filed == 1
    assert _patch_all["mark_filed"].call_args.kwargs["box_link"] == "https://app.box.com/file/old"


# ---- HMAC reject (downgrade defense) -------------------------------------


def test_hmac_failure_rejects_without_dispatch_or_receipt(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["verify"].return_value = False
    result = _poll_inside_lock()
    assert result.rejected == 1 and result.filed == 0
    _patch_all["process"].assert_not_called()   # NEVER handed to intake
    _patch_all["mark_filed"].assert_not_called()  # NEVER drained (kept for forensics)
    _patch_all["mark_rejected"].assert_called_once()  # M4 (PR-4): flipped box_verified=-1 (stops the forever re-pull)
    _patch_all["review"].assert_called_once()     # flagged to Review Queue
    assert _patch_all["review"].call_args.kwargs["security_flag"] is True
    _patch_all["anomaly"].assert_called_once()    # tripwire fired
    # Recorded rejected so subsequent cycles don't re-flag (no 60s spam).
    persisted = _patch_all["persist_seen"].call_args.args[0]
    assert persisted["u1"] == {"status": "rejected"}
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "WARN"


# ---- transient intake error → NOT drained --------------------------------


def test_intake_error_is_not_receipted(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["process"].return_value = ProcessResult(
        status="error", message_id="u1", correlation_id="c"
    )
    result = _poll_inside_lock()
    assert result.errors == 1 and result.filed == 0
    _patch_all["mark_filed"].assert_not_called()  # re-pull retries
    # NOT recorded in seen → re-processed next cycle.
    persisted = _patch_all["persist_seen"].call_args.args[0]
    assert "u1" not in persisted
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "DEGRADED"


# ---- seen-set fast-paths -------------------------------------------------


def test_seen_filed_reposts_receipt_without_refiling(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["load_seen"].return_value = {
        "u1": {
            "status": "filed", "box_link": "https://app.box.com/file/keep",
            "box_file_id": "keep",
        }
    }
    result = _poll_inside_lock()
    assert result.remarked == 1 and result.filed == 0
    _patch_all["verify"].assert_not_called()     # already verified before
    _patch_all["process"].assert_not_called()    # NOT re-filed
    # PR-4: the stored box_file_id rides the lost-receipt re-post too.
    _patch_all["mark_filed"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        submission_uuid="u1", box_link="https://app.box.com/file/keep",
        box_file_id="keep",
    )


def test_seen_filed_repost_recovers_box_file_id_from_link_when_absent(_patch_all):
    # A seen-set record written BEFORE PR-4 has no box_file_id — recover it from the
    # stored link (digits after /file/) so the cache handle survives the re-post.
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["load_seen"].return_value = {
        "u1": {"status": "filed", "box_link": "https://app.box.com/file/12345"}
    }
    _poll_inside_lock()
    assert _patch_all["mark_filed"].call_args.kwargs["box_file_id"] == "12345"


def test_seen_rejected_is_skipped_silently(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["load_seen"].return_value = {"u1": {"status": "rejected"}}
    result = _poll_inside_lock()
    assert result.rejected == 0 and result.filed == 0  # not re-counted, not re-flagged
    _patch_all["verify"].assert_not_called()
    _patch_all["process"].assert_not_called()
    _patch_all["mark_filed"].assert_not_called()
    _patch_all["review"].assert_not_called()


# ---- per-row fence -------------------------------------------------------


def test_per_row_exception_does_not_kill_cycle(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1"), _row("u2")]
    _patch_all["process"].side_effect = [RuntimeError("boom"), _processed("u2")]
    result = _poll_inside_lock()
    assert result.errors == 1 and result.filed == 1  # u2 still processed
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "DEGRADED"


def test_row_missing_uuid_is_flagged_not_dispatched(_patch_all):
    _patch_all["get_pending"].return_value = [_row("")]
    result = _poll_inside_lock()
    assert result.errors == 1
    _patch_all["process"].assert_not_called()


# ---- fail-closed credentials ---------------------------------------------


def test_missing_credentials_halts_without_polling(_patch_all):
    _patch_all["creds"].return_value = None
    result = _poll_inside_lock()
    assert result.halted_no_creds is True
    _patch_all["get_pending"].assert_not_called()  # FAIL-CLOSED: no poll
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "ERROR"
    # A non-polling cycle must NOT fake the Check-C freshness marker (so a sustained no-creds
    # state surfaces via the staleness floor) — and missing creds won't self-heal → CRITICAL.
    _patch_all["wd"].assert_not_called()
    assert any(
        c.args and c.args[0] == portal_poll.Severity.CRITICAL
        and c.kwargs.get("error_code") == "portal_creds_missing"
        for c in _patch_all["log"].call_args_list
    )


def test_transient_circuit_open_warns_and_skips_without_paging(_patch_all):
    # base URL unreadable because the Smartsheet circuit is OPEN → CREDS_TRANSIENT, NOT None.
    _patch_all["creds"].return_value = portal_poll.CREDS_TRANSIENT
    result = _poll_inside_lock()
    assert result.halted_transient is True
    assert result.halted_no_creds is False
    _patch_all["get_pending"].assert_not_called()  # FAIL-CLOSED: no poll
    # transient → WARN heartbeat (NOT ERROR), and NO watchdog marker (a SUSTAINED outage still
    # surfaces via the Check-C staleness floor — same as the no-creds path).
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "WARN"
    _patch_all["wd"].assert_not_called()
    # It WARNs (portal_creds_transient) and must NOT fire the misconfig CRITICAL.
    assert any(
        c.args and c.args[0] == portal_poll.Severity.WARN
        and c.kwargs.get("error_code") == "portal_creds_transient"
        for c in _patch_all["log"].call_args_list
    )
    assert not any(
        c.kwargs.get("error_code") == "portal_creds_missing"
        for c in _patch_all["log"].call_args_list
    )


def test_transient_raw_error_warns_and_skips_without_paging(_patch_all):
    # FF4: a raw pre-trip Smartsheet blip (rate-limit/5xx BEFORE the breaker opens) gets the
    # SAME transient treatment as circuit-open — WARN + skip, never the misconfig CRITICAL —
    # and the sentinel's reason is surfaced in the WARN message and heartbeat summary.
    _patch_all["creds"].return_value = portal_poll._TransientUnavailable(
        reason="SmartsheetRateLimitError: SmartsheetRateLimitError('429')"
    )
    result = _poll_inside_lock()
    assert result.halted_transient is True
    assert result.halted_no_creds is False
    _patch_all["get_pending"].assert_not_called()  # FAIL-CLOSED: no poll
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "WARN"
    assert "SmartsheetRateLimitError" in _patch_all["hb_row"].call_args.kwargs["error_summary"]
    _patch_all["wd"].assert_not_called()  # sustained outage still surfaces via Check-C staleness
    warns = [
        c for c in _patch_all["log"].call_args_list
        if c.kwargs.get("error_code") == "portal_creds_transient"
    ]
    assert len(warns) == 1
    assert warns[0].args[0] == portal_poll.Severity.WARN
    assert "SmartsheetRateLimitError" in warns[0].args[2]  # reason named in the WARN
    assert not any(
        c.kwargs.get("error_code") == "portal_creds_missing"
        for c in _patch_all["log"].call_args_list
    )


def test_transient_recovers_next_cycle(_patch_all):
    # FF4 recovery: cycle 1 transient → clean skip; cycle 2 Smartsheet back → normal poll.
    # Self-heals with NO operator action (the CRITICAL would have implied one).
    good = portal_poll._PortalCreds(
        base_url="https://portal.example.com", bearer="bearer", secret="secret",
    )
    _patch_all["creds"].side_effect = [portal_poll.CREDS_TRANSIENT, good]
    first = _poll_inside_lock()
    assert first.halted_transient is True
    _patch_all["get_pending"].assert_not_called()
    second = _poll_inside_lock()
    assert second.halted_transient is False and second.halted_no_creds is False
    _patch_all["get_pending"].assert_called_once()  # recovered cycle polls normally


# ---- _resolve_credentials 3-state (transient vs genuinely-absent) ----------


def test_resolve_credentials_circuit_open_returns_transient(mocker):
    # A Smartsheet circuit-open on the base-URL read is TRANSIENT — never confused with a misconfig.
    mocker.patch.object(
        portal_poll.smartsheet_client, "get_setting",
        side_effect=portal_poll.smartsheet_client.SmartsheetCircuitOpenError("open"),
    )
    assert portal_poll._resolve_credentials() is portal_poll.CREDS_TRANSIENT


def test_resolve_credentials_raw_transient_returns_transient(mocker):
    # FF4: a raw SmartsheetError subclass (rate-limit/5xx BEFORE the breaker trips — the breaker
    # needs failure_threshold consecutive failures, so early-outage cycles raise the raw class)
    # is TRANSIENT too. Previously it propagated → @its_error_log CRITICAL `uncaught_exception`.
    mocker.patch.object(
        portal_poll.smartsheet_client, "get_setting",
        side_effect=portal_poll.smartsheet_client.SmartsheetRateLimitError("429"),
    )
    creds = portal_poll._resolve_credentials()
    assert isinstance(creds, portal_poll._TransientUnavailable)
    assert "SmartsheetRateLimitError" in creds.reason  # named condition for the WARN/heartbeat


def test_resolve_credentials_auth_error_propagates(mocker):
    # Auth/permission failures are DETERMINISTIC misconfigs (the breaker's own ignore-list) —
    # they will NOT self-heal, so they must NEVER read as transient; propagate → page.
    mocker.patch.object(
        portal_poll.smartsheet_client, "get_setting",
        side_effect=portal_poll.smartsheet_client.SmartsheetAuthError("401"),
    )
    with pytest.raises(portal_poll.smartsheet_client.SmartsheetAuthError):
        portal_poll._resolve_credentials()


def test_read_str_setting_transient_warns_and_falls_back(mocker):
    # FF4: the polling-gate config read gets the same transient classification — WARN-loud
    # (observable config resolution) + fallback, instead of propagating to a CRITICAL.
    mocker.patch.object(
        portal_poll.smartsheet_client, "get_setting",
        side_effect=portal_poll.smartsheet_client.SmartsheetError("503 backend"),
    )
    log = mocker.patch.object(portal_poll.error_log, "log")
    assert portal_poll._read_str_setting("some.key", "fb") == "fb"
    assert any(
        c.args[0] == portal_poll.Severity.WARN
        and c.kwargs.get("error_code") == "portal_config_transient"
        for c in log.call_args_list
    )


def test_read_str_setting_auth_error_propagates(mocker):
    # Deterministic misconfig on the config read still pages (never collapses to fallback).
    mocker.patch.object(
        portal_poll.smartsheet_client, "get_setting",
        side_effect=portal_poll.smartsheet_client.SmartsheetPermissionError("403"),
    )
    with pytest.raises(portal_poll.smartsheet_client.SmartsheetPermissionError):
        portal_poll._read_str_setting("some.key", "fb")


def test_resolve_credentials_missing_row_returns_none(mocker):
    # base-URL row genuinely absent (NotFound) → None (misconfig), NOT transient.
    mocker.patch.object(
        portal_poll.smartsheet_client, "get_setting",
        side_effect=portal_poll.smartsheet_client.SmartsheetNotFoundError("nope"),
    )
    mocker.patch.object(portal_poll.keychain, "get_secret", return_value="x")
    assert portal_poll._resolve_credentials() is None


def test_resolve_credentials_missing_keychain_returns_none(mocker):
    # base URL fine, but the Keychain bearer/secret are absent → None (misconfig), NOT transient.
    mocker.patch.object(
        portal_poll.smartsheet_client, "get_setting", return_value="https://portal.example.com",
    )
    mocker.patch.object(
        portal_poll.keychain, "get_secret",
        side_effect=portal_poll.keychain.KeychainError("missing"),
    )
    assert portal_poll._resolve_credentials() is None


def test_resolve_credentials_all_present_returns_creds(mocker):
    mocker.patch.object(
        portal_poll.smartsheet_client, "get_setting", return_value="https://portal.example.com",
    )
    mocker.patch.object(portal_poll.keychain, "get_secret", return_value="secret-val")
    creds = portal_poll._resolve_credentials()
    assert isinstance(creds, portal_poll._PortalCreds)
    assert creds.base_url == "https://portal.example.com"


# ---- pending fetch failure ------------------------------------------------


def test_pending_fetch_failure_writes_error_heartbeat(_patch_all):
    _patch_all["get_pending"].side_effect = portal_poll.portal_client.PortalTransportError("500")
    result = _poll_inside_lock()
    assert result.errors == 1
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "ERROR"
    _patch_all["wd"].assert_not_called()  # failed cycle must NOT fake Check-C freshness
    _patch_all["rec_fail"].assert_called_once()  # consecutive-failure counter bumped
    # First failure (count=1 < threshold) → ERROR, not CRITICAL.
    assert any(
        c.args and c.args[0] == portal_poll.Severity.ERROR
        and c.kwargs.get("error_code") == "portal_pending_fetch_failed"
        for c in _patch_all["log"].call_args_list
    )


def test_sustained_fetch_failure_escalates_to_critical(_patch_all):
    _patch_all["get_pending"].side_effect = portal_poll.portal_client.PortalTransportError("500")
    _patch_all["rec_fail"].return_value = portal_poll.FETCH_FAIL_CRITICAL_THRESHOLD  # sustained
    result = _poll_inside_lock()
    assert result.errors == 1
    _patch_all["wd"].assert_not_called()
    assert any(
        c.args and c.args[0] == portal_poll.Severity.CRITICAL
        and c.kwargs.get("error_code") == "portal_pending_fetch_failed"
        for c in _patch_all["log"].call_args_list
    )


def test_pending_fetch_auth_failure_is_critical_immediately(_patch_all):
    # 401 = bad/rotated bearer → won't self-heal → CRITICAL on the FIRST failure (not via the
    # transient counter), and no faked Check-C marker.
    _patch_all["get_pending"].side_effect = portal_poll.portal_client.PortalAuthError("401")
    result = _poll_inside_lock()
    assert result.errors == 1
    _patch_all["wd"].assert_not_called()
    _patch_all["rec_fail"].assert_not_called()  # auth bypasses the transient counter
    assert any(
        c.args and c.args[0] == portal_poll.Severity.CRITICAL
        and c.kwargs.get("error_code") == "portal_pending_auth_failed"
        for c in _patch_all["log"].call_args_list
    )


# ---- job-sync push leg ----------------------------------------------------


def _job(job_id="JOB-000001", project_name="Bradley 1", *, is_active=True, address=""):
    return SimpleNamespace(job_id=job_id, project_name=project_name, is_active=is_active, address=address)


def test_job_sync_pushes_full_set_after_drain(_patch_all):
    _patch_all["list_all_jobs"].return_value = [
        _job("JOB-000001", "Bradley 1", is_active=True, address="100 Array Rd"),
        _job("JOB-000007", "Atlantis", is_active=False),
    ]
    result = _poll_inside_lock()
    assert result.halted_no_creds is False
    _patch_all["push_jobs"].assert_called_once()
    args = _patch_all["push_jobs"].call_args.args
    # (base_url, bearer, payload) — the full set with active flags 1/0 + the C1 job address.
    assert args[0] == "https://portal.example.com" and args[1] == "bearer"
    assert args[2] == [
        {"job_id": "JOB-000001", "project_name": "Bradley 1", "active": 1, "address": "100 Array Rd"},
        {"job_id": "JOB-000007", "project_name": "Atlantis", "active": 0, "address": ""},
    ]


def test_job_sync_empty_set_short_circuits_without_push(_patch_all):
    _patch_all["list_all_jobs"].return_value = []  # Smartsheet read miss / empty sheet
    _poll_inside_lock()
    _patch_all["push_jobs"].assert_not_called()  # never wipe the dropdown


def test_job_sync_failure_is_swallowed_and_does_not_affect_intake(_patch_all):
    # A filed submission + a failing job-sync push: the cycle still completes, the
    # intake drain stats are intact, and the failure is a WARN (not an intake error).
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["list_all_jobs"].return_value = [_job()]
    _patch_all["push_jobs"].side_effect = portal_poll.portal_client.PortalTransportError("500")
    result = _poll_inside_lock()
    assert result.filed == 1            # intake drain unaffected
    assert result.errors == 0           # the push failure is NOT an intake error
    codes = [c.kwargs.get("error_code") for c in _patch_all["log"].call_args_list]
    assert "portal_job_sync_failed" in codes


# ---- PR-4 request-driven PDF cache servicing pass ------------------------


def _pdf_req(uuid="u1", file_id="f9"):
    return {
        "submission_uuid": uuid, "box_file_id": file_id,
        "form_code": "jha-v1", "work_date": "2026-06-05",
    }


def test_pdf_service_downloads_chunks_and_uploads(_patch_all):
    _patch_all["get_pdf_requests"].return_value = [_pdf_req("u1", "f9")]
    # 1.5 chunks worth of bytes → exactly 2 chunks at PDF_CHUNK_BYTES.
    pdf = b"A" * (portal_poll.PDF_CHUNK_BYTES + 10)
    _patch_all["download_file"].return_value = pdf

    result = _poll_inside_lock()

    assert result.pdf_serviced == 1
    _patch_all["download_file"].assert_called_once_with("f9")
    calls = _patch_all["upload_filed_pdf"].call_args_list
    assert len(calls) == 2  # chunked across 2 POSTs
    assert [c.kwargs["chunk_index"] for c in calls] == [0, 1]
    assert all(c.kwargs["chunk_total"] == 2 for c in calls)
    assert all(c.kwargs["submission_uuid"] == "u1" for c in calls)
    # chunk_b64 round-trips back to the original PDF bytes (never logged raw).
    import base64 as _b64
    reassembled = b"".join(_b64.b64decode(c.kwargs["chunk_b64"]) for c in calls)
    assert reassembled == pdf


def test_pdf_service_single_chunk_for_small_pdf(_patch_all):
    _patch_all["get_pdf_requests"].return_value = [_pdf_req("u1", "f9")]
    _patch_all["download_file"].return_value = b"%PDF tiny"
    result = _poll_inside_lock()
    assert result.pdf_serviced == 1
    calls = _patch_all["upload_filed_pdf"].call_args_list
    assert len(calls) == 1 and calls[0].kwargs["chunk_total"] == 1


def test_pdf_service_empty_box_file_is_skipped_not_uploaded(_patch_all):
    # A zero-byte filed PDF is a DATA error → WARN skip, NEVER an empty-chunk upload
    # (the Worker would 400 it). The request stays unready for re-file.
    _patch_all["get_pdf_requests"].return_value = [_pdf_req("u1", "f9")]
    _patch_all["download_file"].return_value = b""
    result = _poll_inside_lock()
    assert result.pdf_serviced == 0
    _patch_all["upload_filed_pdf"].assert_not_called()
    codes = [c.kwargs.get("error_code") for c in _patch_all["log"].call_args_list]
    assert "portal_pdf_empty_file" in codes


def test_pdf_service_per_item_fence_one_bad_item_does_not_abort(_patch_all):
    _patch_all["get_pdf_requests"].return_value = [
        _pdf_req("u1", "f1"), _pdf_req("u2", "f2"), _pdf_req("u3", "f3"),
    ]
    # The middle item's Box download blows up; the other two still service.
    _patch_all["download_file"].side_effect = [b"one", RuntimeError("box boom"), b"three"]
    result = _poll_inside_lock()
    assert result.pdf_serviced == 2  # u1 + u3, not u2
    codes = [c.kwargs.get("error_code") for c in _patch_all["log"].call_args_list]
    assert "portal_pdf_request_item_failed" in codes


def test_pdf_service_skips_row_missing_box_file_id(_patch_all):
    _patch_all["get_pdf_requests"].return_value = [
        {"submission_uuid": "u1", "box_file_id": "", "form_code": "x", "work_date": "d"},
    ]
    result = _poll_inside_lock()
    assert result.pdf_serviced == 0
    _patch_all["download_file"].assert_not_called()
    codes = [c.kwargs.get("error_code") for c in _patch_all["log"].call_args_list]
    assert "portal_pdf_request_malformed" in codes


def test_pdf_service_failure_is_best_effort_and_does_not_block_intake(_patch_all):
    # A filed submission + a failing PDF-request pull: the cycle still completes, the
    # intake drain stats are intact, and the failure is a WARN (not an intake error).
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["get_pdf_requests"].side_effect = (
        portal_poll.portal_client.PortalTransportError("500")
    )
    result = _poll_inside_lock()
    assert result.filed == 1          # intake drain unaffected
    assert result.errors == 0         # the pass failure is NOT an intake error
    assert result.pdf_serviced == 0
    codes = [c.kwargs.get("error_code") for c in _patch_all["log"].call_args_list]
    assert "portal_pdf_service_failed" in codes


def test_pdf_service_no_requests_is_noop(_patch_all):
    _patch_all["get_pdf_requests"].return_value = []
    result = _poll_inside_lock()
    assert result.pdf_serviced == 0
    _patch_all["download_file"].assert_not_called()
    _patch_all["upload_filed_pdf"].assert_not_called()


# ---- G1 Slice 2: checklist item-photo screening pass ----------------------
#
# REAL crypto + REAL screening end-to-end: rows are signed with the actual
# shared.portal_hmac item-photo protocol (secret matches the fixture creds), and the
# happy/refused paths run the actual photo_screen pipeline on real bytes (a Pillow
# JPEG / junk bytes) — the §34-parity claim is exercised, not mocked. Only transport
# (portal_client), Box, and state files are mocked.

def _real_jpeg_b64() -> str:
    buf = _io.BytesIO()
    _PILImage.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="JPEG")
    return _b64lib.b64encode(buf.getvalue()).decode()


def _photo_row(
    photo_id=5, item_state_id=7, data_b64=None, uploaded_by="sub.sam",
    secret="secret", hmac_override=None,
):
    """One pulled item_photos row, signed with the REAL item-photo protocol
    (secret defaults to the fixture creds' 'secret')."""
    photo_json = json.dumps({
        "data": data_b64 if data_b64 is not None else _real_jpeg_b64(),
        "name": "site.jpg", "taken_at": "", "gps": "", "uploaded_by": uploaded_by,
    })
    return {
        "id": photo_id,
        "item_state_id": item_state_id,
        "photo_json": photo_json,
        "hmac": hmac_override if hmac_override is not None else _portal_hmac.sign_item_photo(
            secret, item_state_id=item_state_id, photo_json=photo_json,
        ),
        "created_at": 1_717_600_000,
    }


def _with_box_root(_patch_all):
    """Point the mocked ITS_Config reader at a configured portal Box root."""
    def _settings(key, fallback):
        if key == portal_poll.safety_naming.CFG_BOX_PORTAL_ROOT:
            return "ROOT-77"
        return fallback
    _patch_all["read_setting"].side_effect = _settings


def _log_codes(_patch_all):
    return [c.kwargs.get("error_code") for c in _patch_all["log"].call_args_list]


def test_item_photo_clean_screens_files_to_box_then_posts_back(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["get_item_photos"].return_value = [_photo_row(photo_id=5, item_state_id=7)]

    result = _poll_inside_lock()

    assert result.item_photos_screened == 1
    # Box chain: <root>/ITS Photos/checklist/<item_state_id>/ — find-or-create at
    # every level (intake._file_portal_photos' shape).
    assert _patch_all["box_folder"].call_args_list[0].args == ("ROOT-77", "ITS Photos")
    assert _patch_all["box_folder"].call_args_list[1].args == ("FOLD-1", "checklist")
    assert _patch_all["box_folder"].call_args_list[2].args == ("FOLD-2", "7")
    # Version-on-conflict upload of the SANITIZED RE-ENCODE (fresh baseline JPEG),
    # never the raw upload bytes.
    up_args = _patch_all["box_upload"].call_args.args
    assert up_args[0] == "FOLD-3" and up_args[1] == "photo_5.jpg"
    assert up_args[2][:3] == b"\xff\xd8\xff"  # JPEG magic — the re-encode output
    # Box FIRST, then the delete-on-screen post-back naming the Box record.
    _patch_all["post_photo_result"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        photo_id=5, status="clean", box_file_id="box-9",
    )
    # No refusal machinery fired.
    _patch_all["review"].assert_not_called()
    # The pass rides the heartbeat notes.
    assert "item_photos_screened=1" in (_patch_all["hb_row"].call_args.kwargs["notes"] or "")


def test_item_photo_clean_reencode_differs_from_raw(_patch_all):
    # The filed bytes are photo_screen's L2 re-encode, NOT the uploaded original —
    # metadata/appended payloads cannot survive into Box.
    _with_box_root(_patch_all)
    raw_b64 = _real_jpeg_b64()
    _patch_all["get_item_photos"].return_value = [_photo_row(data_b64=raw_b64)]
    _poll_inside_lock()
    uploaded = _patch_all["box_upload"].call_args.args[2]
    assert uploaded != _b64lib.b64decode(raw_b64)


def test_item_photo_suspicious_bad_magic_refused_and_reviewed(_patch_all):
    _with_box_root(_patch_all)
    junk = _b64lib.b64encode(b"\x00\x01\x02\x03 not an image").decode()
    _patch_all["get_item_photos"].return_value = [
        _photo_row(photo_id=6, item_state_id=9, data_b64=junk),
    ]

    result = _poll_inside_lock()

    assert result.item_photos_screened == 1  # refused IS a terminal disposition
    # The _portal_photo_refusal pattern: security-flagged Review-Queue row, WARN (no
    # page) for suspicious.
    _patch_all["review"].assert_called_once()
    kw = _patch_all["review"].call_args.kwargs
    assert kw["security_flag"] is True
    assert kw["severity"] == _Sev.WARN
    assert kw["payload"]["disposition"] == "suspicious"
    assert kw["payload"]["actor"] == "sub.sam"
    assert "item completion stands" in kw["summary"]
    assert "portal_item_photo_suspicious" in _log_codes(_patch_all)
    # Refused bytes are NEVER filed; the refused disposition drains the row.
    _patch_all["box_upload"].assert_not_called()
    _patch_all["post_photo_result"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        photo_id=6, status="refused", detail="L1:magic_mismatch",
    )


def test_item_photo_malicious_pages_critical_naming_the_account(_patch_all, mocker):
    _with_box_root(_patch_all)
    _patch_all["get_item_photos"].return_value = [
        _photo_row(photo_id=8, item_state_id=3, uploaded_by="evil.eve"),
    ]
    mocker.patch.object(
        portal_poll.photo_screen, "screen_photo",
        return_value=PhotoScreenResult("malicious", None, "L2", "decompression_bomb:99999x99999"),
    )

    result = _poll_inside_lock()

    assert result.item_photos_screened == 1
    crit = [
        c for c in _patch_all["log"].call_args_list
        if c.kwargs.get("error_code") == "portal_item_photo_malicious"
    ]
    assert len(crit) == 1
    assert crit[0].args[0] == _Sev.CRITICAL
    # Names the account (from the HMAC-covered photo_json) + the disable instruction.
    assert "'evil.eve'" in crit[0].args[2]
    assert "disable this portal account" in crit[0].args[2]
    kw = _patch_all["review"].call_args.kwargs
    assert kw["severity"] == _Sev.CRITICAL and kw["security_flag"] is True
    _patch_all["post_photo_result"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        photo_id=8, status="refused", detail="L2:decompression_bomb:99999x99999",
    )
    _patch_all["box_upload"].assert_not_called()


def test_item_photo_bad_hmac_one_shot_flag_never_screens(_patch_all, mocker):
    _with_box_root(_patch_all)
    screen = mocker.patch.object(portal_poll.photo_screen, "screen_photo")
    _patch_all["get_item_photos"].return_value = [
        _photo_row(photo_id=5, item_state_id=7, hmac_override="0" * 64),
    ]

    result = _poll_inside_lock()

    # NEVER screened or filed (downgrade defense) — and not counted as dispositioned.
    assert result.item_photos_screened == 0
    screen.assert_not_called()
    _patch_all["box_upload"].assert_not_called()
    # First sighting: anomaly tripwire + CRITICAL + security-flagged review row.
    _patch_all["anomaly"].assert_called_once()
    _patch_all["review"].assert_called_once()
    assert _patch_all["review"].call_args.kwargs["security_flag"] is True
    assert "portal_item_photo_hmac_failure" in _log_codes(_patch_all)
    # The refused post-back drains the row (delete-on-screen destroys tampered bytes).
    _patch_all["post_photo_result"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        photo_id=5, status="refused", detail="hmac_verification_failed",
    )
    # Flag persisted for the one-shot suppression.
    persisted = _patch_all["photo_flags_persist"].call_args.args[0]
    assert persisted == {"5": "flagged"}


def test_item_photo_bad_hmac_second_cycle_does_not_reflag_but_retries_drain(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["get_item_photos"].return_value = [
        _photo_row(photo_id=5, item_state_id=7, hmac_override="0" * 64),
    ]
    _patch_all["photo_flags_load"].return_value = {"5": "flagged"}

    _poll_inside_lock()

    # No re-flag spam (the one-shot), but the drain post RETRIES until it lands.
    _patch_all["review"].assert_not_called()
    _patch_all["anomaly"].assert_not_called()
    assert "portal_item_photo_hmac_failure" not in _log_codes(_patch_all)
    _patch_all["post_photo_result"].assert_called_once()


def test_item_photo_transport_error_is_fenced_and_never_blocks_drain(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["get_item_photos"].side_effect = (
        portal_poll.portal_client.PortalTransportError("500")
    )
    result = _poll_inside_lock()
    assert result.filed == 1            # submission drain unaffected
    assert result.errors == 0           # the pass failure is NOT an intake error
    assert result.item_photos_screened == 0
    assert "portal_item_photo_service_failed" in _log_codes(_patch_all)


def test_item_photo_result_found_false_is_benign_idempotent_rescreen(_patch_all):
    # A re-pulled row whose disposition already applied (lost ack): the post returns
    # found=False → WARN, no exception, still counted as dispositioned this cycle.
    _with_box_root(_patch_all)
    _patch_all["get_item_photos"].return_value = [_photo_row()]
    _patch_all["post_photo_result"].return_value = False
    result = _poll_inside_lock()
    assert result.item_photos_screened == 1
    assert "portal_item_photo_result_not_found" in _log_codes(_patch_all)


def test_item_photo_box_root_unset_row_stays_pending(_patch_all):
    # Box root unconfigured → the clean photo CANNOT be filed → the per-item fence
    # WARNs and NO result is posted (delete-on-screen must never destroy the only copy).
    _patch_all["get_item_photos"].return_value = [_photo_row()]
    result = _poll_inside_lock()
    assert result.item_photos_screened == 0
    _patch_all["post_photo_result"].assert_not_called()
    assert "portal_item_photo_item_failed" in _log_codes(_patch_all)


def test_item_photo_per_item_fence_one_bad_item_does_not_abort(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["get_item_photos"].return_value = [
        _photo_row(photo_id=1, item_state_id=11),
        _photo_row(photo_id=2, item_state_id=12),
        _photo_row(photo_id=3, item_state_id=13),
    ]
    _patch_all["box_upload"].side_effect = [
        {"id": "b1"}, RuntimeError("box boom"), {"id": "b3"},
    ]
    result = _poll_inside_lock()
    assert result.item_photos_screened == 2  # 1 + 3; the middle row stays pending
    assert "portal_item_photo_item_failed" in _log_codes(_patch_all)
    assert _patch_all["post_photo_result"].call_count == 2


def test_item_photo_malformed_row_is_skipped(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["get_item_photos"].return_value = [{"item_state_id": 7}]  # no id
    result = _poll_inside_lock()
    assert result.item_photos_screened == 0
    _patch_all["post_photo_result"].assert_not_called()
    assert "portal_item_photo_malformed" in _log_codes(_patch_all)


def test_item_photo_config_resolution_is_observable_and_gates_clamav(_patch_all, mocker):
    # Observable-config rule (forensic #7): the resolved ClamAV gate + its source are
    # logged, and the SAME flag value is what screen_photo actually receives.
    def _settings(key, fallback):
        if key == portal_poll.safety_naming.CFG_BOX_PORTAL_ROOT:
            return "ROOT-77"
        if key == portal_poll.intake.CFG_PHOTO_CLAMAV:
            return "true"
        return fallback
    _patch_all["read_setting"].side_effect = _settings
    screen = mocker.patch.object(
        portal_poll.photo_screen, "screen_photo",
        return_value=PhotoScreenResult("clean", b"\xff\xd8\xff-reencoded", "L3", "ok"),
    )
    _patch_all["get_item_photos"].return_value = [_photo_row()]

    _poll_inside_lock()

    assert screen.call_args.kwargs["clamav_enabled"] is True
    resolved = [
        c for c in _patch_all["log"].call_args_list
        if c.kwargs.get("error_code") == "portal_item_photo_config_resolved"
    ]
    assert len(resolved) == 1
    assert "clamav_enabled=True (source=ITS_Config" in resolved[0].args[2]


def test_item_photo_config_default_source_logged(_patch_all):
    # Key absent → default OFF, and the log SAYS it fell back (never silent).
    _with_box_root(_patch_all)
    _patch_all["get_item_photos"].return_value = [_photo_row()]
    _poll_inside_lock()
    resolved = [
        c for c in _patch_all["log"].call_args_list
        if c.kwargs.get("error_code") == "portal_item_photo_config_resolved"
    ]
    assert len(resolved) == 1
    assert "clamav_enabled=False (source=default" in resolved[0].args[2]


def test_item_photo_empty_queue_is_noop_and_logs_no_config(_patch_all):
    result = _poll_inside_lock()
    assert result.item_photos_screened == 0
    assert "portal_item_photo_config_resolved" not in _log_codes(_patch_all)
    _patch_all["post_photo_result"].assert_not_called()


# ---- DR-photo-pool Slice 2: daily-pool photo screening pass ----------------
#
# Same REAL-crypto + REAL-screening posture as the item-photo section above: rows
# are signed with the actual shared.portal_hmac daily-photo protocol and the
# happy/refused paths run the actual photo_screen pipeline on real bytes. Only
# transport (portal_client), Box, and state files are mocked.

def _daily_row(
    photo_id=11, job_id="JOB-1", work_date="2026-07-03", data_b64=None,
    uploaded_by="mgr.mo", secret="secret", hmac_override=None,
):
    """One pulled daily_photo_pool row, signed with the REAL daily-photo protocol
    (secret defaults to the fixture creds' 'secret')."""
    photo_json = json.dumps({
        "data": data_b64 if data_b64 is not None else _real_jpeg_b64(),
        "name": "extra.jpg", "taken_at": "", "gps": "", "uploaded_by": uploaded_by,
    })
    return {
        "id": photo_id,
        "job_id": job_id,
        "work_date": work_date,
        "photo_json": photo_json,
        "hmac": hmac_override if hmac_override is not None else _portal_hmac.sign_daily_photo(
            secret, job_id=job_id, work_date=work_date, photo_json=photo_json,
        ),
        "created_at": 1_717_600_000,
    }


def test_daily_photo_pass_runs_before_submission_fetch(_patch_all):
    # THE ORDERING CRUX: the pool screens BEFORE the /pending fetch, so the claim
    # manifest the Worker attaches reflects post-screen state (photos uploaded
    # before submit are clean by the time their referencing submission processes).
    order: list[str] = []
    _patch_all["get_daily_photos"].side_effect = lambda *a, **k: order.append("daily_pass") or []
    _patch_all["get_pending"].side_effect = lambda *a, **k: order.append("submission_fetch") or []
    _poll_inside_lock()
    assert order == ["daily_pass", "submission_fetch"]


def test_daily_photo_clean_screens_files_to_box_then_posts_back(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["box_folder"].side_effect = ["F1", "F2", "F3", "F4"] * 20
    _patch_all["get_daily_photos"].return_value = [
        _daily_row(photo_id=11, job_id="JOB-1", work_date="2026-07-03"),
    ]

    result = _poll_inside_lock()

    assert result.daily_photos_screened == 1
    # Box chain: <root>/ITS Photos/daily/<job_id>/<work_date>/ — find-or-create at
    # every level; every component HMAC-covered or served with the signed row.
    calls = _patch_all["box_folder"].call_args_list
    assert calls[0].args == ("ROOT-77", "ITS Photos")
    assert calls[1].args == ("F1", "daily")
    assert calls[2].args == ("F2", "JOB-1")
    assert calls[3].args == ("F3", "2026-07-03")
    # Version-on-conflict upload of the SANITIZED RE-ENCODE, never the raw upload.
    up_args = _patch_all["box_upload"].call_args.args
    assert up_args[0] == "F4" and up_args[1] == "photo_11.jpg"
    assert up_args[2][:3] == b"\xff\xd8\xff"
    # Box FIRST, then the delete-on-screen post-back naming the Box record.
    _patch_all["post_daily_result"].assert_called_once()
    kw = _patch_all["post_daily_result"].call_args.kwargs
    assert kw["photo_id"] == 11 and kw["status"] == "clean" and kw["box_file_id"] == "box-9"
    # 0074: the clean post carries a screened thumbnail derived from the re-encode —
    # a real JPEG, decoded-bounded, never the raw upload.
    import base64 as _b64
    thumb = _b64.b64decode(kw["thumb_b64"])
    assert thumb[:3] == b"\xff\xd8\xff" and len(thumb) <= 40_000
    _patch_all["review"].assert_not_called()
    # The pass rides the heartbeat notes.
    assert "daily_photos_screened=1" in (_patch_all["hb_row"].call_args.kwargs["notes"] or "")


def test_daily_photo_replay_onto_another_date_fails_hmac(_patch_all):
    # job_id + work_date are INSIDE the canonical: a validly-signed photo served
    # under a different date fails verification (the replay defense).
    _with_box_root(_patch_all)
    row = _daily_row(photo_id=12, work_date="2026-07-03")
    row["work_date"] = "2026-07-04"  # replayed onto another day
    _patch_all["get_daily_photos"].return_value = [row]

    result = _poll_inside_lock()

    assert result.daily_photos_screened == 0
    _patch_all["box_upload"].assert_not_called()
    assert "portal_daily_photo_hmac_failure" in _log_codes(_patch_all)
    _patch_all["post_daily_result"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        photo_id=12, status="refused", detail="hmac_verification_failed",
    )


def test_daily_photo_suspicious_bad_magic_refused_and_reviewed(_patch_all):
    _with_box_root(_patch_all)
    junk = _b64lib.b64encode(b"\x00\x01\x02\x03 not an image").decode()
    _patch_all["get_daily_photos"].return_value = [
        _daily_row(photo_id=13, data_b64=junk),
    ]

    result = _poll_inside_lock()

    assert result.daily_photos_screened == 1  # refused IS a terminal disposition
    _patch_all["review"].assert_called_once()
    kw = _patch_all["review"].call_args.kwargs
    assert kw["security_flag"] is True
    assert kw["severity"] == _Sev.WARN
    assert kw["payload"]["disposition"] == "suspicious"
    assert kw["payload"]["actor"] == "mgr.mo"
    assert "portal_daily_photo_suspicious" in _log_codes(_patch_all)
    _patch_all["box_upload"].assert_not_called()
    _patch_all["post_daily_result"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        photo_id=13, status="refused", detail="L1:magic_mismatch",
    )


def test_daily_photo_malicious_pages_critical_naming_the_account(_patch_all, mocker):
    _with_box_root(_patch_all)
    _patch_all["get_daily_photos"].return_value = [
        _daily_row(photo_id=14, uploaded_by="evil.eve"),
    ]
    mocker.patch.object(
        portal_poll.photo_screen, "screen_photo",
        return_value=PhotoScreenResult("malicious", None, "L3", "clamav:Eicar-Test"),
    )

    result = _poll_inside_lock()

    assert result.daily_photos_screened == 1
    crit = [
        c for c in _patch_all["log"].call_args_list
        if c.kwargs.get("error_code") == "portal_daily_photo_malicious"
    ]
    assert len(crit) == 1
    assert crit[0].args[0] == _Sev.CRITICAL
    # Names the account (from the HMAC-covered photo_json) + the disable instruction.
    assert "'evil.eve'" in crit[0].args[2]
    assert "disable this portal account" in crit[0].args[2]
    kw = _patch_all["review"].call_args.kwargs
    assert kw["severity"] == _Sev.CRITICAL and kw["security_flag"] is True
    _patch_all["post_daily_result"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        photo_id=14, status="refused", detail="L3:clamav:Eicar-Test",
    )
    _patch_all["box_upload"].assert_not_called()


def test_daily_photo_bad_hmac_one_shot_flag_never_screens(_patch_all, mocker):
    _with_box_root(_patch_all)
    screen = mocker.patch.object(portal_poll.photo_screen, "screen_photo")
    _patch_all["get_daily_photos"].return_value = [
        _daily_row(photo_id=15, hmac_override="0" * 64),
    ]

    result = _poll_inside_lock()

    # NEVER screened or filed (downgrade defense) — and not counted as dispositioned.
    assert result.daily_photos_screened == 0
    screen.assert_not_called()
    _patch_all["box_upload"].assert_not_called()
    _patch_all["anomaly"].assert_called_once()
    _patch_all["review"].assert_called_once()
    assert _patch_all["review"].call_args.kwargs["security_flag"] is True
    assert "portal_daily_photo_hmac_failure" in _log_codes(_patch_all)
    _patch_all["post_daily_result"].assert_called_once_with(
        "https://portal.example.com", "bearer",
        photo_id=15, status="refused", detail="hmac_verification_failed",
    )
    persisted = _patch_all["daily_flags_persist"].call_args.args[0]
    assert persisted == {"15": "flagged"}


def test_daily_photo_bad_hmac_second_cycle_does_not_reflag_but_retries_drain(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["get_daily_photos"].return_value = [
        _daily_row(photo_id=15, hmac_override="0" * 64),
    ]
    _patch_all["daily_flags_load"].return_value = {"15": "flagged"}

    _poll_inside_lock()

    _patch_all["review"].assert_not_called()
    _patch_all["anomaly"].assert_not_called()
    assert "portal_daily_photo_hmac_failure" not in _log_codes(_patch_all)
    _patch_all["post_daily_result"].assert_called_once()


def test_daily_photo_pass_failure_is_fenced_and_never_blocks_drain(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["get_daily_photos"].side_effect = (
        portal_poll.portal_client.PortalTransportError("500")
    )
    result = _poll_inside_lock()
    assert result.filed == 1            # submission drain unaffected
    assert result.errors == 0           # the pass failure is NOT an intake error
    assert result.daily_photos_screened == 0
    assert "portal_daily_photo_service_failed" in _log_codes(_patch_all)


def test_daily_photo_result_found_false_is_benign_idempotent_rescreen(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["get_daily_photos"].return_value = [_daily_row()]
    _patch_all["post_daily_result"].return_value = False
    result = _poll_inside_lock()
    assert result.daily_photos_screened == 1
    assert "portal_daily_photo_result_not_found" in _log_codes(_patch_all)


def test_daily_photo_box_root_unset_row_stays_pending(_patch_all):
    # Box root unconfigured → the clean photo CANNOT be filed → the per-item fence
    # WARNs and NO result is posted (delete-on-screen must never destroy the only copy).
    _patch_all["get_daily_photos"].return_value = [_daily_row()]
    result = _poll_inside_lock()
    assert result.daily_photos_screened == 0
    _patch_all["post_daily_result"].assert_not_called()
    assert "portal_daily_photo_item_failed" in _log_codes(_patch_all)


def test_daily_photo_per_item_fence_one_bad_item_does_not_abort(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["box_folder"].side_effect = ["F1", "F2", "F3", "F4"] * 20
    _patch_all["get_daily_photos"].return_value = [
        _daily_row(photo_id=21), _daily_row(photo_id=22), _daily_row(photo_id=23),
    ]
    _patch_all["box_upload"].side_effect = [
        {"id": "b1"}, RuntimeError("box boom"), {"id": "b3"},
    ]
    result = _poll_inside_lock()
    assert result.daily_photos_screened == 2  # 21 + 23; the middle row stays pending
    assert "portal_daily_photo_item_failed" in _log_codes(_patch_all)
    assert _patch_all["post_daily_result"].call_count == 2


def test_daily_photo_malformed_row_is_skipped(_patch_all):
    _with_box_root(_patch_all)
    _patch_all["get_daily_photos"].return_value = [
        {"id": 9, "work_date": "2026-07-03", "photo_json": "{}"},  # no job_id
    ]
    result = _poll_inside_lock()
    assert result.daily_photos_screened == 0
    _patch_all["post_daily_result"].assert_not_called()
    assert "portal_daily_photo_malformed" in _log_codes(_patch_all)


def test_daily_photo_config_resolution_is_observable(_patch_all):
    # Same observable-config rule as the item pass — the SAME shared ClamAV flag.
    _with_box_root(_patch_all)
    _patch_all["box_folder"].side_effect = ["F1", "F2", "F3", "F4"] * 20
    _patch_all["get_daily_photos"].return_value = [_daily_row()]
    _poll_inside_lock()
    resolved = [
        c for c in _patch_all["log"].call_args_list
        if c.kwargs.get("error_code") == "portal_daily_photo_config_resolved"
    ]
    assert len(resolved) == 1
    assert "clamav_enabled=False (source=default" in resolved[0].args[2]


# ---- DR-photo-pool Slice 2: the 'deferred' submission soft-fail ------------


def test_deferred_submission_not_drained_not_error(_patch_all):
    # intake defers a submission whose pool refs are still pending: the SAME no-drain
    # mechanics as 'error' (re-pull next cycle) but counted separately — the cycle
    # stays OK (an expected ordering race, not an infra failure).
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["process"].return_value = ProcessResult(
        status="deferred", message_id="u1", correlation_id="c",
        notes="deferred: 2 pool photo(s) pending screening",
    )
    result = _poll_inside_lock()
    assert result.deferred == 1
    assert result.errors == 0 and result.filed == 0
    _patch_all["mark_filed"].assert_not_called()
    # NOT seen-recorded: the re-pull must re-dispatch, not fast-path.
    persisted = _patch_all["persist_seen"].call_args.args[0]
    assert "u1" not in persisted
    # The cycle is healthy — deferral is not an error condition.
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "OK"
    assert "deferred=1" in (_patch_all["hb_row"].call_args.kwargs["notes"] or "")


# ---- poll_once outer gating ----------------------------------------------


def test_poll_once_skipped_when_polling_disabled(_patch_all, mocker):
    mocker.patch.object(portal_poll, "_polling_enabled", return_value=False)
    result = poll_once()
    assert result.skipped_disabled is True
    _patch_all["get_pending"].assert_not_called()


def test_poll_once_skipped_when_lock_held(_patch_all, mocker):
    mocker.patch.object(portal_poll, "_polling_enabled", return_value=True)

    @contextmanager
    def _held(_path):
        yield False

    mocker.patch.object(portal_poll, "_file_lock", _held)
    result = poll_once()
    assert result.skipped_locked is True
    _patch_all["get_pending"].assert_not_called()


def test_poll_once_summarizes_recovered_retries_at_the_pass_boundary(_patch_all, mocker):
    """D3 coverage beyond the two fenced daemons.

    A Smartsheet call that RECOVERED on retry is invisible by construction — nothing
    raises, nothing is logged — so a chronically flaky backend gets absorbed silently.
    Shipping the summary row only as a `TransientFence` method gave it to 2 daemons out of
    ~12; `portal_poll` (and po/rfq/estimate/subcontract/fieldops) flush at their pass exit
    too, which is the dashboard surface D3 exists to feed.
    """
    mocker.patch.object(portal_poll, "_polling_enabled", return_value=True)
    log = mocker.patch.object(portal_poll.error_log, "log")
    mocker.patch.object(
        portal_poll.sustained_failure.smartsheet_client, "drain_retry_recovery",
        return_value={"get_rows": {"sequences": 1, "attempts": 2}},
    )

    poll_once()

    rows = [
        c for c in log.call_args_list
        if c.kwargs.get("error_code") == "smartsheet_retry_recovered"
    ]
    assert len(rows) == 1
    assert rows[0].args[0] == portal_poll.Severity.WARN
    assert rows[0].args[1] == portal_poll.SCRIPT_NAME


# ---- wiring --------------------------------------------------------------


def test_daemon_name_is_stable():
    assert DAEMON_NAME == "safety_reports.portal_poll"


# ---- review-hardening: seen-set cap, mark-filed found=False, status precedence


def test_persist_seen_caps_to_max_seen(mocker):
    @contextmanager
    def _lock(_p):
        yield

    mocker.patch.object(portal_poll.state_io, "with_path_lock", _lock)
    captured: dict[str, Any] = {}
    mocker.patch.object(
        portal_poll.state_io, "atomic_write_json",
        side_effect=lambda path, data: captured.__setitem__("d", data),
    )
    big = {f"u{i}": {"status": "filed", "box_link": ""} for i in range(portal_poll.MAX_SEEN + 100)}
    portal_poll._persist_seen(big)
    written = captured["d"]
    assert len(written) == portal_poll.MAX_SEEN
    # kept the most-recent entries; dropped the oldest.
    assert f"u{portal_poll.MAX_SEEN + 99}" in written
    assert "u0" not in written


def test_mark_filed_found_false_logs_warn(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["mark_filed"].return_value = False
    _poll_inside_lock()
    warns = [
        c for c in _patch_all["log"].call_args_list
        if c.kwargs.get("error_code") == "portal_mark_filed_not_found"
    ]
    assert warns, "found=False from the Worker must WARN (not be silent)"


def test_per_row_exception_does_not_record_seen(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["process"].side_effect = RuntimeError("boom")
    _poll_inside_lock()
    persisted = _patch_all["persist_seen"].call_args.args[0]
    assert "u1" not in persisted  # a crashed row is NOT recorded → re-pull retries


def test_cycle_status_degraded_when_errors_and_reviewed(_patch_all):
    _patch_all["get_pending"].return_value = [_row("u1"), _row("u2")]
    _patch_all["process"].side_effect = [
        ProcessResult(status="review_queue", message_id="u1", correlation_id="c", box_link=None),
        RuntimeError("boom"),
    ]
    result = _poll_inside_lock()
    assert result.errors == 1 and result.reviewed == 1
    # errors (DEGRADED) takes precedence over reviewed (WARN).
    assert _patch_all["hb_row"].call_args.kwargs["status"] == "DEGRADED"


# ---- A4: _record_pending_backlog stuck-backlog marker --------------------


def _read_backlog(path):
    return json.loads(path.read_text())


def test_record_pending_backlog_saturated_no_drain_latches(monkeypatch, tmp_path):
    """A saturated page (>= PENDING_LIMIT) that drained nothing latches high_since_utc."""
    p = tmp_path / "backlog.json"
    monkeypatch.setattr(portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT, 0)
    data = _read_backlog(p)
    assert data["count"] == portal_poll.PENDING_LIMIT
    assert data["drained"] == 0
    assert data["high_since_utc"] is not None


def test_record_pending_backlog_progress_clears_latch(monkeypatch, tmp_path):
    """Any drain progress clears the latch even on a full page."""
    p = tmp_path / "backlog.json"
    monkeypatch.setattr(portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT, 3)
    assert _read_backlog(p)["high_since_utc"] is None


def test_record_pending_backlog_unsaturated_clears_latch(monkeypatch, tmp_path):
    """A page below the cap is not a stuck backlog regardless of drain."""
    p = tmp_path / "backlog.json"
    monkeypatch.setattr(portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT - 1, 0)
    assert _read_backlog(p)["high_since_utc"] is None


def test_record_pending_backlog_latch_persists_across_cycles(monkeypatch, tmp_path):
    """A sustained stuck backlog keeps the FIRST high_since_utc across cycles."""
    p = tmp_path / "backlog.json"
    monkeypatch.setattr(portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT, 0)
    first = _read_backlog(p)["high_since_utc"]
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT, 0)
    second = _read_backlog(p)
    assert second["high_since_utc"] == first  # latch not reset
    assert second["last_scan_utc"] >= first   # last_scan advances


def test_record_pending_backlog_relatches_after_recovery(monkeypatch, tmp_path):
    """Stuck → recovered (latch clears) → stuck again sets a NEW latch."""
    p = tmp_path / "backlog.json"
    monkeypatch.setattr(portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT, 0)
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT, 5)  # recovered → clears
    assert _read_backlog(p)["high_since_utc"] is None
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT, 0)  # stuck again
    assert _read_backlog(p)["high_since_utc"] is not None


def test_record_pending_backlog_failsoft_on_write_error(monkeypatch, tmp_path, mocker):
    """A marker write error is swallowed (WARN-logged), never raised into the drain."""
    p = tmp_path / "backlog.json"
    monkeypatch.setattr(portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    mocker.patch.object(portal_poll.state_io, "atomic_write_json", side_effect=OSError("disk full"))
    log = mocker.patch.object(portal_poll.error_log, "log")
    portal_poll._record_pending_backlog(portal_poll.PENDING_LIMIT, 0)  # must not raise
    assert log.called


# ── C1: job address in the down-sync payload ──────────────────────────────────


def test_push_active_jobs_includes_address(mocker):
    """_push_active_jobs projects the ITS_Active_Jobs `address` into the sync payload (C1) so the
    Worker can auto-fill the subcontract builder's Site address."""
    jobs = [
        SimpleNamespace(job_id="JOB-1", project_name="2026.001 Kendall", is_active=True, address="100 Array Rd"),
        SimpleNamespace(job_id="JOB-2", project_name="Idle Job", is_active=False, address=""),
    ]
    mocker.patch.object(portal_poll.active_jobs, "list_all_jobs", return_value=jobs)
    mocker.patch.object(portal_poll.error_log, "log")
    push = mocker.patch.object(
        portal_poll.portal_client, "push_jobs", return_value={"upserted": 2, "deactivated": 0}
    )
    portal_poll._push_active_jobs("https://base", "bearer")
    payload = push.call_args.args[2]  # push_jobs(base_url, bearer, payload)
    assert payload == [
        {"job_id": "JOB-1", "project_name": "2026.001 Kendall", "active": 1, "address": "100 Array Rd"},
        {"job_id": "JOB-2", "project_name": "Idle Job", "active": 0, "address": ""},
    ]


def test_push_active_jobs_empty_set_is_noop(mocker):
    """A Smartsheet read miss (list_all_jobs → []) never pushes — it must not wipe the dropdown."""
    mocker.patch.object(portal_poll.active_jobs, "list_all_jobs", return_value=[])
    push = mocker.patch.object(portal_poll.portal_client, "push_jobs")
    portal_poll._push_active_jobs("https://base", "bearer")
    push.assert_not_called()


# ---- 0074 site-photos bridge: the fenced register post ----------------------------


def _processed_with_registrations(uuid: str = "u1") -> ProcessResult:
    return ProcessResult(
        status="processed", message_id=uuid, correlation_id="c",
        box_link="https://app.box.com/file/f9", box_file_id="f9",
        site_photo_registrations=[
            {"box_file_id": "p1", "caption": "front.jpg", "thumb_b64": "AAAA"},
            {"box_file_id": "p2", "caption": ""},
        ],
    )


def test_processed_with_registrations_posts_register_after_mark_filed(_patch_all, mocker):
    reg = mocker.patch.object(
        portal_poll.portal_client, "post_daily_photos_register",
        return_value={"ok": True, "registered": 2, "skipped": 0},
    )
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["process"].return_value = _processed_with_registrations()

    _poll_inside_lock()

    _patch_all["mark_filed"].assert_called_once()
    reg.assert_called_once_with(
        "https://portal.example.com", "bearer",
        submission_uuid="u1",
        photos=[
            {"box_file_id": "p1", "caption": "front.jpg", "thumb_b64": "AAAA"},
            {"box_file_id": "p2", "caption": ""},
        ],
    )


def test_processed_without_registrations_never_posts_register(_patch_all, mocker):
    reg = mocker.patch.object(portal_poll.portal_client, "post_daily_photos_register")
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["process"].return_value = _processed()  # site_photo_registrations=None

    _poll_inside_lock()

    _patch_all["mark_filed"].assert_called_once()
    reg.assert_not_called()


def test_register_failure_warns_and_never_disturbs_the_filing(_patch_all, mocker):
    # §43 best-effort fence: the register post raises; the filing + receipt + seen record
    # all stand, the cycle stays OK, and a WARN names the miss.
    mocker.patch.object(
        portal_poll.portal_client, "post_daily_photos_register",
        side_effect=portal_poll.portal_client.PortalTransportError("register 503"),
    )
    _patch_all["get_pending"].return_value = [_row("u1")]
    _patch_all["process"].return_value = _processed_with_registrations()

    result = _poll_inside_lock()

    _patch_all["mark_filed"].assert_called_once()
    assert result.filed == 1 and result.errors == 0
    warn_codes = [
        c.kwargs.get("error_code") for c in _patch_all["log"].call_args_list
        if c.args and c.args[0] == _Sev.WARN
    ]
    assert "portal_site_photo_register_failed" in warn_codes
