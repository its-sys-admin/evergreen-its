"""Parameterized WSR-style send-dispatch polling core (P1c).

§42 — WHY this module exists (parameterize-not-clone, Op Stds §14 informed
deviation). `weekly_send_poll` was a safety-hardcoded dispatch daemon: discover
approved rows on a review sheet, run the F22 attestation gate, stamp the verified
approver, and hand each to a send handler. The progress workstream needs the SAME
daemon shape over its own ``WPR_human_review`` sheet + its own ``progress_send``.
Rather than clone the ~250-line daemon (and its F22 gate, its no-double-send
exclusion, its fail-closed approver read — the security-critical parts), the body
is extracted here parameterized by a required **no-default** ``DaemonConfig``; the
thin per-workstream entry (``weekly_send_poll`` for safety) binds the one config.

§42 — load-bearing invariants PRESERVED byte-equivalent across the extraction:
  * ``_load_authorized_approvers`` is **fail-CLOSED**: any membership-read failure aborts
    the cycle before dispatch, ZERO sends — never fail-open, never a fallback-to-empty
    that would let an unverified row through. It is the F08 contrast to the config-read
    fail-open. AMENDED 2026-07-21 (operator decision D1): the read is now wrapped by a
    ``sustained_failure.TransientFence`` so a *precisely-typed transient* infra failure
    (5xx / timeout) logs ERROR and halts instead of paging CRITICAL, escalating to
    CRITICAL only once it is SUSTAINED. Fail-closed is untouched — only the severity of
    the halt changed. Auth / permission / rate-limit / any unrecognised exception still
    propagate immediately to CRITICAL. Do NOT add a fallback-to-empty catch.
  * ``DaemonConfig.dispatch_statuses`` EXCLUDES the SENDING write-ahead marker — a
    row left SENDING (post-send stamp failure) must NEVER be re-dispatched or the
    customer is double-sent. The safety bind passes ``{PENDING, FAILED}``.
  * The F22 ``verify_approval`` gate on the DRIVING column, the per-row fence, the
    verified-approver stamp, and CRITICAL-only-for-``wake_reasons`` are unchanged.

§42 — contamination gate: NO ``DaemonConfig`` field defaults to a safety value
(``__post_init__`` rejects a missing send_fn / poll_sheet_id / f22_workspace_id),
so a progress entry that forgets a binding fails at construction, never silently
dispatches a progress row through safety's sheet/recipients. The SDK layer is safe
by construction (explicit ids per call); risk collapses onto this required config.

§42 — capability gating: this core dispatches a send via ``config.send_fn`` and is
enrolled in ``tests/test_capability_gating.py`` SEND_SCRIPTS (``anthropic`` /
``anthropic_client`` AST-forbidden). It imports no AI surface; ``send_fn`` carries
the (already capability-gated) ``weekly_send.send_one_row``.
"""
from __future__ import annotations

import fcntl
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# SendResult is the handler's return shape; only used for typing the send_fn.
from safety_reports.weekly_send import SendResult
from shared import (
    approval_verification,
    circuit_breaker,
    error_log,
    smartsheet_client,
    sustained_failure,
)
from shared.error_log import Severity
from shared.heartbeat import HeartbeatStatus


@dataclass(frozen=True)
class DaemonConfig:
    """Required (no-default) binding for one send-dispatch daemon.

    EVERY field is required — a missing field is a ``TypeError`` at construction
    (the contamination gate: a workstream that forgets a binding cannot silently
    dispatch through another workstream's sheet / recipients).
    """

    # --- identity / wrappers ---
    script_name: str            # error_log actor + kill-switch identity
    config_workstream: str      # ITS_Config get_setting scope
    daemon_name: str            # ITS_Daemon_Health primary key (= heartbeat.daemon_name)
    # --- per-cycle fence + watchdog ---
    lock_path: Path             # ~/its/state/<daemon>.lock
    watchdog_marker_dir: Path   # ~/its/.watchdog/
    watchdog_job_slug: str      # Check C marker slug (must be in watchdog.TRACKED_JOBS)
    # --- ITS_Config keys ---
    cfg_polling_enabled: str
    default_polling_enabled: bool
    cfg_scheduled_send_local: str
    default_scheduled_send_local: str
    send_tz: str                # IANA tz for the scheduled-window check (Pacific)
    # --- sheets / F22 ---
    poll_sheet_id: int          # the review sheet polled + gated + stamped
    f22_workspace_id: int       # approval authority = membership of THIS workspace
    # --- review-sheet schema (column titles) ---
    col_send_now: str
    col_approve_scheduled: str
    col_send_status: str
    col_notes: str
    col_approved_by: str
    col_approved_at: str
    # --- dispatch / send contract ---
    dispatch_statuses: frozenset[str]   # MUST exclude SENDING (no double-send)
    status_pending: str
    status_failed: str
    max_send_retries: int
    parse_retry_count: Callable[[str | None], int]  # weekly_send._parse_retry_count
    to_datetime: Callable[[Any], str]               # wsr_review.to_wsr_datetime
    wake_reasons: frozenset[approval_verification.VerdictReason]
    # --- the bound sender (heartbeat + the other I/O seams are injected into
    #     poll_once/poll_inside_lock by the entry, not carried here). ---
    send_fn: Callable[[int], SendResult]    # bound send_one_row (cfg already partial'd)
    # OPT-IN dark-ship allowance (default OFF — strict for every existing daemon). A send
    # daemon that ships DARK before its review sheet is built (builder-precedes-seed — the
    # RFQ send lane, whose SHEET_RFQ_PENDING_REVIEW is a 0 placeholder until the operator
    # runs the builder + flips the id) sets this True so its module-level CONFIG can be
    # CONSTRUCTED/IMPORTED with poll_sheet_id == 0. This is RUNTIME-SAFE only because
    # poll_once short-circuits on the `polling_enabled=false` gate BEFORE ever reading the
    # sheet, so a dark daemon never touches sheet 0; a real id lands at go-live (the operator
    # builds the sheet + flips the id, the SAME step that flips the send gate). A NEGATIVE id
    # is never permitted, and the strict positive-id gate stays for every other daemon.
    allow_placeholder_sheet: bool = False

    def __post_init__(self) -> None:
        # Construction-time contamination gate — fail LOUD on a missing binding.
        if not callable(self.send_fn):
            raise TypeError("DaemonConfig.send_fn must be callable (the bound sender).")
        if not isinstance(self.poll_sheet_id, int) or self.poll_sheet_id < 0:
            raise ValueError("DaemonConfig.poll_sheet_id must be a non-negative sheet id.")
        if self.poll_sheet_id == 0 and not self.allow_placeholder_sheet:
            raise ValueError(
                "DaemonConfig.poll_sheet_id must be a positive sheet id (or set "
                "allow_placeholder_sheet=True for a dark daemon whose review sheet is not "
                "built yet — the send gate keeps it a no-op until the id is flipped)."
            )
        if not isinstance(self.f22_workspace_id, int) or self.f22_workspace_id <= 0:
            raise ValueError("DaemonConfig.f22_workspace_id must be a positive workspace id.")
        if "SENDING" in {s.upper() for s in self.dispatch_statuses}:
            # The load-bearing no-double-send exclusion (see module §42).
            raise ValueError("dispatch_statuses must NOT include SENDING (no-double-send).")


@dataclass(frozen=True)
class PollStats:
    """Summary of one poll_once() invocation. Returned for caller logging."""
    skipped_disabled: bool = False
    skipped_locked: bool = False
    rows_scanned: int = 0
    dispatched: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    errors: int = 0
    blocked: int = 0


# ---- Config readers ------------------------------------------------------


def _read_str_setting(config: DaemonConfig, key: str, fallback: str) -> str:
    try:
        raw = smartsheet_client.get_setting(key, workstream=config.config_workstream)
    except smartsheet_client.SmartsheetNotFoundError:
        return fallback
    except smartsheet_client.SmartsheetCircuitOpenError:
        # F08: an OPEN breaker short-circuits this control-plane config read.
        # Fail OPEN to the fallback so a degraded Smartsheet cannot crash the cycle
        # BEFORE it surfaces CIRCUIT_OPEN in its heartbeat. (Op Stds §3.1.)
        return fallback
    except smartsheet_client.SmartsheetError as exc:
        # Transient read failure (timeout / 5xx) — a single-cycle blip must not
        # escape to @its_error_log as a spurious CRITICAL. WARN + fail OPEN to
        # the fallback, same disposition as the circuit-open branch above.
        # (Contrast _load_authorized_approvers below — the F22 SECURITY gate —
        # which stays deliberately fail-CLOSED.)
        error_log.log(
            Severity.WARN,
            config.script_name,
            f"config read failed for {key}: {exc!r} — using fallback {fallback!r}",
            error_code="config_read_error",
        )
        return fallback
    return raw if isinstance(raw, str) and raw else fallback


def _read_bool_setting(config: DaemonConfig, key: str, fallback: bool) -> bool:
    raw = _read_str_setting(config, key, str(fallback).lower())
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _polling_enabled(config: DaemonConfig) -> bool:
    return _read_bool_setting(config, config.cfg_polling_enabled, config.default_polling_enabled)


def _load_authorized_approvers(config: DaemonConfig) -> frozenset[str]:
    """F22 authorized-approver set = membership of the config's workspace.

    F08 CONTRAST — deliberately NO try/except HERE. Unlike ``_read_str_setting`` (which
    catches SmartsheetCircuitOpenError and fails OPEN to a scheduling fallback), this is
    the SECURITY gate: every failure propagates, the cycle aborts, ZERO sends —
    fail-CLOSED. Do NOT add a fallback-to-empty / fail-open catch. An empty set (no
    individual shares) is the legitimate fail-closed case (verify_approval →
    EMPTY_ALLOWLIST → block all sends).

    CONTRACT AMENDED 2026-07-21 (operator decision D1) — what changed, and what did NOT.
    The CALLER (`poll_inside_lock`) now classifies the propagated exception through a
    ``sustained_failure.TransientFence`` instead of letting every class land as
    ``@its_error_log`` CRITICAL ``uncaught_exception``:

      * a precisely-typed transient (5xx / ReadTimeout / ConnectionError — the class that
        paged progress_send_poll at 05:36Z on 2026-07-21 and had self-healed by the next
        cycle) → ERROR + a consecutive counter, escalating to CRITICAL at 3 consecutive
        cycles, so a REAL sustained outage still wakes the operator;
      * circuit-OPEN → WARN, uncounted (the breaker's own prolonged-open CRITICAL owns
        that page; counting it would storm one outage into N daemon pages);
      * auth / permission / rate-limit / ANY unrecognised exception → re-raised unchanged,
        immediate CRITICAL. A transient blip is not a security event; a revoked token or a
        genuine bug still is.

    FAIL-CLOSED IS UNCHANGED in every branch: the cycle returns before the dispatch loop,
    so ZERO sends happen on any approver-load failure. `tests/test_send_poll_core.py`
    proves that per failure class rather than by inspection.
    """
    return smartsheet_client.list_workspace_share_emails(config.f22_workspace_id)


def _transient_fence(config: DaemonConfig, *, kind: str) -> sustained_failure.TransientFence:
    """Build the per-daemon, per-call-site transient fence.

    State lives beside the daemon's own lock file so the five bound send pollers keep
    INDEPENDENT counters (a flaky safety workspace must never page for progress).
    Threshold 3, not the shared 5: every send poller runs a 15-minute cadence, so 5
    consecutive cycles would be ~75 min of silence before the page (operator decision D2).
    """
    return sustained_failure.TransientFence(
        config.script_name,
        state_path=config.lock_path.parent / f"{config.script_name}_{kind}_failures.json",
        transient_error_code=f"{config.script_name}.{kind}_transient",
        sustained_error_code=f"{config.script_name}.{kind}_sustained",
        threshold=sustained_failure.SLOW_CADENCE_CRITICAL_THRESHOLD,
        runbook="docs/runbooks/safety_weekly_send.md",
    )


# ---- State / lock helpers ------------------------------------------------


@contextmanager
def _file_lock(path: Path) -> Iterator[bool]:
    """Acquire exclusive non-blocking lock; yield True on success, False if held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001 — cleanup best-effort
            pass
        handle.close()


# ---- Watchdog Check C marker ---------------------------------------------


def _write_watchdog_marker(config: DaemonConfig) -> None:
    """Touch the Check C freshness marker for this run."""
    try:
        config.watchdog_marker_dir.mkdir(parents=True, exist_ok=True)
        marker = config.watchdog_marker_dir / f"{config.watchdog_job_slug}.last_run"
        marker.write_text(datetime.now(UTC).isoformat())
    except OSError as exc:
        error_log.log(
            Severity.WARN,
            config.script_name,
            f"watchdog marker write failed: {exc!r}",
            error_code="watchdog_marker_failed",
        )


# ---- Row filtering -------------------------------------------------------


def _filter_dispatch_candidates(
    config: DaemonConfig, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return rows that need send-attention this cycle (see weekly_send_poll docs)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if not (bool(row.get(config.col_send_now)) or bool(row.get(config.col_approve_scheduled))):
            continue
        status = row.get(config.col_send_status) or config.status_pending
        if status not in config.dispatch_statuses:
            continue
        if status == config.status_failed:
            if config.parse_retry_count(row.get(config.col_notes)) >= config.max_send_retries:
                continue
        out.append(row)
    return out


_WEEKDAY_MAP = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


#: What a malformed window falls back to. Named so the WARN can say it out loud.
DEFAULT_SCHEDULED_WINDOW = (0, time(7, 0))  # MON 07:00


def _parse_scheduled_spec(spec: str) -> tuple[int, time]:
    """Parse `MON 07:00` → (0, time(7, 0)). Falls back to MON 07:00 on parse failure.

    Fails OPEN to the default deliberately — a bad window must never wedge a send lane — but it is
    no longer SILENT. This engine is shared by all FIVE external-send daemons (weekly_send,
    progress_send, po_send, rfq_send, subcontract_send), so a typo in any one lane's
    `scheduled_send_local` used to move that lane's approval window with no trace: the operator
    sets `TUE 09:00`, mistypes it, and the lane quietly keeps dispatching Monday 07:00 with nothing
    to distinguish that from a deliberate Monday setting.
    """
    weekday, tod, _ = parse_scheduled_spec_checked(spec)
    return weekday, tod


def parse_scheduled_spec_checked(spec: str) -> tuple[int, time, str]:
    """`_parse_scheduled_spec`, plus a machine reason when the spec did not parse.

    Returns `(weekday, time, reason)`; `reason` is `""` on a clean parse. It names WHAT was wrong,
    because an unknown weekday token and a malformed time are different typos with the same silent
    outcome today, and the operator has to fix a specific cell.
    """
    raw = (spec or "").strip()
    if not raw:
        return (*DEFAULT_SCHEDULED_WINDOW, "empty value")
    parts = raw.split()
    if len(parts) != 2:
        return (*DEFAULT_SCHEDULED_WINDOW, f"expected '<DAY> <HH:MM>', got {raw!r}")
    wd_token, hhmm = parts
    weekday = _WEEKDAY_MAP.get(wd_token.upper())
    if weekday is None:
        return (
            *DEFAULT_SCHEDULED_WINDOW,
            f"unknown weekday {wd_token!r} (expected one of {', '.join(_WEEKDAY_MAP)})",
        )
    try:
        hour_s, minute_s = hhmm.split(":")
        tod = time(int(hour_s), int(minute_s))
    except ValueError:
        return (*DEFAULT_SCHEDULED_WINDOW, f"malformed time {hhmm!r} (expected HH:MM, 24-hour)")
    return weekday, tod, ""


def warn_if_scheduled_spec_malformed(config: DaemonConfig, spec: str) -> None:
    """Log ONCE per cycle when the configured send window did not parse.

    WARN, not ERROR: the lane still runs on the default, so this is a misconfiguration to correct,
    not an outage to page for. The error_code is stable so a runbook can key on it.
    """
    _, _, reason = parse_scheduled_spec_checked(spec)
    if not reason:
        return
    weekday, tod = DEFAULT_SCHEDULED_WINDOW
    day = next(k for k, v in _WEEKDAY_MAP.items() if v == weekday)
    error_log.log(
        Severity.WARN,
        config.script_name,
        f"{config.cfg_scheduled_send_local} = {spec!r} did not parse ({reason}); this lane is "
        f"falling back to {day} {tod.strftime('%H:%M')} — fix the ITS_Config cell, because the "
        f"approval window actually in effect is NOT the one configured",
        error_code="scheduled_send_window_malformed",
    )


def _is_scheduled_window(now_local: datetime, spec: str) -> bool:
    """True iff `now_local` is on/after the configured weekday + time (e.g. Mon ≥07:00)."""
    weekday, tod = _parse_scheduled_spec(spec)
    return now_local.weekday() == weekday and now_local.timetz().replace(tzinfo=None) >= tod


# ---- F22 approval-attestation handling -----------------------------------


def _handle_unverified(
    config: DaemonConfig, row_id: int, verdict: approval_verification.ApprovalVerdict
) -> None:
    """Record a blocked send for an unverified approval (fail-closed).

    Always writes a forensic ITS_Errors row. Wakes the operator (CRITICAL → the
    triple-fire path, dedupe-gated) only for ``config.wake_reasons``; a benign race
    (NOT_CURRENTLY_APPROVED) gets WARN, a transient read failure ERROR — neither pages.
    """
    reason = verdict.reason
    if reason in config.wake_reasons:
        severity: Severity = Severity.CRITICAL
    elif reason == approval_verification.VerdictReason.NOT_CURRENTLY_APPROVED:
        severity = Severity.WARN
    else:
        severity = Severity.ERROR

    correlation_id = str(uuid.uuid4())
    error_log.log(
        severity,
        config.script_name,
        (
            f"approval attestation FAILED for row_id={row_id}; send BLOCKED "
            f"(fail-closed). reason={reason.value} actor={verdict.actor!r} "
            f"detail={verdict.detail}"
        ),
        error_code="approval_unverified",
        correlation_id=correlation_id,
    )
    # log(CRITICAL) fires the triple-fire alert itself (severity is CRITICAL
    # exactly when reason ∈ wake_reasons) — no explicit _alert_critical (would
    # double-fire the Sentry leg).


def _stamp_approval(
    config: DaemonConfig, row_id: int, verdict: approval_verification.ApprovalVerdict
) -> None:
    """Stamp the verified approver (Approved By/At). Best-effort AUDIT — a stamp
    failure must NEVER block a send the F22 gate already verified (→ WARN + proceed)."""
    approved_at = config.to_datetime(verdict.modified_at)
    try:
        smartsheet_client.update_rows(
            config.poll_sheet_id,
            [{
                "_row_id": row_id,
                config.col_approved_by: verdict.actor or "",
                config.col_approved_at: approved_at,
            }],
        )
    except Exception as exc:  # noqa: BLE001 — best-effort audit; never block a verified send
        error_log.log(
            Severity.WARN, config.script_name,
            f"approval stamp failed for row_id={row_id} (non-fatal; send proceeds): {exc!r}",
            error_code="weekly_send_poll.stamp_failed",
        )


# ---- Public API ----------------------------------------------------------
#
# poll_once / poll_inside_lock take `config` + INJECTED seams (the per-daemon
# heartbeat-liveness + heartbeat-row + watchdog-marker + stamp + scheduled-window
# callables). The thin entry passes its own module-level functions so the daemon's
# existing unit-test mock seams (patched on the entry module) stay valid.


def poll_once(
    config: DaemonConfig,
    *,
    write_liveness: Callable[[], None],
    write_row: Callable[..., None],
    write_watchdog_marker: Callable[[], None],
    stamp_approval: Callable[[int, approval_verification.ApprovalVerdict], None],
    is_scheduled_window: Callable[[datetime, str], bool],
) -> PollStats:
    """Run one poll cycle. Idempotent across crashes. (Wrap in @its_error_log +
    @require_active at the entry.)"""
    if not _polling_enabled(config):
        error_log.log(
            Severity.INFO, config.script_name,
            "polling disabled via ITS_Config; exiting cycle",
            error_code="polling_disabled",
        )
        return PollStats(skipped_disabled=True)

    with _file_lock(config.lock_path) as acquired:
        if not acquired:
            error_log.log(
                Severity.INFO, config.script_name,
                "another poll cycle holds the lock; skipping this cycle",
                error_code="poll_lock_held",
            )
            return PollStats(skipped_locked=True)
        return poll_inside_lock(
            config,
            write_liveness=write_liveness,
            write_row=write_row,
            write_watchdog_marker=write_watchdog_marker,
            stamp_approval=stamp_approval,
            is_scheduled_window=is_scheduled_window,
        )


def poll_inside_lock(
    config: DaemonConfig,
    *,
    write_liveness: Callable[[], None],
    write_row: Callable[..., None],
    write_watchdog_marker: Callable[[], None],
    stamp_approval: Callable[[int, approval_verification.ApprovalVerdict], None],
    is_scheduled_window: Callable[[datetime, str], bool],
) -> PollStats:
    """Body of poll_once running under the file lock."""
    read_fence = _transient_fence(config, kind="review_read")
    try:
        rows = smartsheet_client.get_rows(config.poll_sheet_id)
    except smartsheet_client.SmartsheetError as exc:
        # Already loud + halting before 2026-07-21. The fence adds the two things this
        # site lacked: (a) sustained-outage escalation — an every-cycle ERROR is invisible
        # on every CRITICAL-keyed fire surface; (b) a split by class — a NON-transient
        # (auth / 404-sheet-gone / rate-limit) now re-raises to CRITICAL instead of being
        # absorbed as a same-shaped ERROR forever, because it will not self-heal.
        # The single hardcoded `weekly_send_poll.read_failed` code (which all five bound
        # daemons wrote, including progress) is replaced by per-script codes.
        #
        # LIVENESS IS WRITTEN IN BOTH BRANCHES, before any re-raise. On origin/main every
        # SmartsheetError here refreshed the Check-C marker and wrote an ERROR heartbeat
        # row; re-raising ahead of them would make one root cause (a persistent 404 /
        # revoked token on the review sheet) ALSO trip watchdog Check C and leave
        # ITS_Daemon_Health stale — a second, misleading "this daemon is dead" CRITICAL
        # pointing the §43 repair at the wrong fault. The daemon is alive and correctly
        # refusing to work; say exactly that, then let the real cause propagate.
        transient = read_fence.handle(exc)
        write_liveness()
        breaker_open = circuit_breaker.is_open()
        read_fail_status: HeartbeatStatus = "CIRCUIT_OPEN" if breaker_open else "ERROR"
        write_row(
            status=read_fail_status,
            items_processed=0,
            error_summary=(None if breaker_open else f"read failed: {type(exc).__name__}: {exc!r}"),
        )
        write_watchdog_marker()
        if not transient:
            raise
        return PollStats(errors=1)
    read_fence.reset()

    candidates = _filter_dispatch_candidates(config, rows)

    # F22 approver load — fail-CLOSED. EVERY branch below returns BEFORE the dispatch
    # loop, so an approver-load failure of any class performs ZERO sends.
    approver_fence = _transient_fence(config, kind="approver_read")
    try:
        authorized_actors = _load_authorized_approvers(config)
    except Exception as exc:  # noqa: BLE001 — classified by the fence; non-transient re-raises
        # Liveness in BOTH branches, before any re-raise — see the review-read site above.
        transient = approver_fence.handle(exc)
        write_liveness()
        breaker_open = circuit_breaker.is_open()
        approver_fail_status: HeartbeatStatus = "CIRCUIT_OPEN" if breaker_open else "ERROR"
        write_row(
            status=approver_fail_status,
            items_processed=0,
            error_summary=(
                None if breaker_open
                else f"approver read failed: {type(exc).__name__}: {exc!r} (zero sends)"
            ),
        )
        write_watchdog_marker()
        if not transient:
            raise
        return PollStats(rows_scanned=len(rows), errors=1)
    approver_fence.reset()

    counters = {"dispatched": 0, "sent": 0, "skipped": 0, "failed": 0, "errors": 0, "blocked": 0}

    now_local = datetime.now(ZoneInfo(config.send_tz))
    scheduled_spec = _read_str_setting(
        config, config.cfg_scheduled_send_local, config.default_scheduled_send_local
    )
    # Say so ONCE per cycle if the window did not parse. Placed here, not inside the parser, so a
    # malformed spec produces one WARN per cycle rather than one per candidate row.
    warn_if_scheduled_spec_malformed(config, scheduled_spec)

    for row in candidates:
        row_id = row["_row_id"]

        if bool(row.get(config.col_send_now)):
            approval_column = config.col_send_now
        elif is_scheduled_window(now_local, scheduled_spec):
            approval_column = config.col_approve_scheduled
        else:
            counters["skipped"] += 1
            continue

        verdict = approval_verification.verify_approval(
            config.poll_sheet_id, row_id, approval_column, authorized_actors=authorized_actors,
        )
        if not verdict.verified:
            counters["blocked"] += 1
            _handle_unverified(config, row_id, verdict)
            continue

        stamp_approval(row_id, verdict)

        counters["dispatched"] += 1
        try:
            result = config.send_fn(row_id)
        except smartsheet_client.SmartsheetError as exc:
            counters["errors"] += 1
            error_log.log(
                Severity.ERROR, config.script_name,
                f"per-row SmartsheetError dispatching row_id={row_id}: {exc!r}",
                error_code="weekly_send_poll.dispatch_failed",
            )
            continue
        except Exception as exc:  # noqa: BLE001 — per-row fence
            counters["errors"] += 1
            error_log.log(
                Severity.ERROR, config.script_name,
                f"per-row unexpected exception dispatching row_id={row_id}: "
                f"{type(exc).__name__}: {exc!r}",
                error_code="weekly_send_poll.dispatch_failed",
            )
            continue

        if result.status == "sent":
            counters["sent"] += 1
        elif result.status.startswith("skipped"):
            counters["skipped"] += 1
        else:
            counters["failed"] += 1

    write_liveness()

    if counters["errors"] > 0:
        cycle_status: HeartbeatStatus = "DEGRADED"
    elif counters["failed"] > 0 or counters["blocked"] > 0:
        cycle_status = "WARN"
    else:
        cycle_status = "OK"

    if circuit_breaker.is_open():
        cycle_status = "CIRCUIT_OPEN"

    try:
        write_row(
            status=cycle_status,
            items_processed=counters["dispatched"],
            error_summary=(
                None
                if counters["errors"] == 0 and counters["failed"] == 0 and counters["blocked"] == 0
                else f"errors={counters['errors']} failed={counters['failed']} blocked={counters['blocked']}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — heartbeat must never block
        error_log.log(
            Severity.WARN, config.script_name,
            f"heartbeat write outer-catch tripped: {exc!r}",
            error_code="daemon_health_write_failed",
        )

    write_watchdog_marker()

    error_log.log(
        Severity.INFO, config.script_name,
        f"poll cycle: scanned={len(rows)} dispatched={counters['dispatched']} "
        f"sent={counters['sent']} skipped={counters['skipped']} "
        f"failed={counters['failed']} errors={counters['errors']} blocked={counters['blocked']}",
        error_code="poll_cycle_summary",
    )
    # D3 — one summarized WARN row if any Smartsheet call in this pass RECOVERED on retry,
    # so a chronically flaky backend is visible on the dashboard and not silently absorbed.
    approver_fence.flush_retry_recovery()
    return PollStats(
        rows_scanned=len(rows),
        dispatched=counters["dispatched"],
        sent=counters["sent"],
        skipped=counters["skipped"],
        failed=counters["failed"],
        errors=counters["errors"],
        blocked=counters["blocked"],
    )
