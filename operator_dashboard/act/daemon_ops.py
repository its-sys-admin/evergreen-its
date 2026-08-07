"""Class-B daemon operations for the operator dashboard (WS2 D1-3b).

The interval-edit verb: change an interval daemon's poll cadence. The interval is
BAKED into the installed launchd plist (install.sh substitutes
__POLL_INTERVAL_SECONDS__), so this is NOT a hot-reload ITS_Config edit — it needs
a plist re-install. This verb keeps ITS_Config (the persistent source of truth
install.sh reads as the no-arg default) and the live plist consistent by doing
BOTH:
  1. update the daemon's `<ws>.<daemon>.poll_interval_seconds` ITS_Config row, then
  2. `install.sh load <label> <interval>` to re-render + re-bootstrap the plist so
     the new cadence takes effect now (the EXPLICIT <interval> arg is passed, so
     there is no read-after-write race on the row just written).

LABEL-ALLOWLISTED to the interval daemons install.sh knows (it mirrors that file's
poll_interval_config_key table) — a label not in the allowlist is refused, so the
verb can never touch the dashboard itself, a calendar daemon (weekly-generate), or
a non-ITS label. NOTE since 2026-08-07: the WATCHDOG is now interval-driven too
(`StartInterval 3600`), but it is deliberately still refused here — its cadence is
NOT operator-tunable, because the hourly/daily tier split in
`scripts/watchdog.py DAILY_ONLY_CHECKS` is calibrated against that specific 3600s
period (a retuned interval would silently re-price the daily tier's 20h marker gate).
Changing it is a code change, not a console edit; `install.sh`'s interval tables
deliberately omit the label, which is what makes this refusal automatic.
The interval is bounds-validated. The
elevated-confirm ceremony (re-PIN + typed label) is enforced by the router BEFORE
this runs — same weight as a Class-B config edit (it mutates launchctl AND
ITS_Config). No secret is read or rotated here. Ships DARK behind the ACT surface
(fail-closed until ITS_OPERATOR_PIN is provisioned).

§43: symptoms + Tier-2 repairs in docs/runbooks/operator_dashboard_config_editor.md.
"""
from __future__ import annotations

import importlib
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from operator_dashboard.config import ITS_HOME, LAUNCHD_DIR

_INSTALL_SH = ITS_HOME / "scripts" / "launchd" / "install.sh"
_INSTALL_TIMEOUT = 60
# Poll-interval bounds (seconds). A config surface without bounds is an outage
# surface: too small hammers the upstream APIs, too large starves the cadence.
MIN_INTERVAL = 10
MAX_INTERVAL = 86_400  # 1 day

# --- daemon control (start / stop / kickstart via install.sh / launchctl) ------
_LABEL_PREFIX = "org.solutionsmith.its."
DASHBOARD_LABEL = "org.solutionsmith.its.dashboard"  # excluded: a service must not stop itself
CONTROL_ACTIONS = ("start", "stop", "kickstart")
_LAUNCHCTL_TIMEOUT = 30


@dataclass(frozen=True)
class IntervalDaemon:
    label: str        # launchd label (org.solutionsmith.its.<name>)
    config_key: str   # ITS_Config Setting — the poll_interval_seconds row
    workstream: str   # ITS_Config Workstream
    default: int       # install.sh per-daemon default (for display / reference)


# The interval daemons install.sh knows (its poll_interval_config_key table).
# tests/test_daemon_ops.py::test_interval_daemons_match_install_sh_table PARSES
# that table and asserts exact parity, so a daemon added there can never silently
# become un-retunable from here (how the ADR-0004 lane drifted out).
_DAEMONS: list[IntervalDaemon] = [
    IntervalDaemon("org.solutionsmith.its.weekly-send", "safety_reports.weekly_send.poll_interval_seconds", "safety_reports", 900),
    IntervalDaemon("org.solutionsmith.its.portal-poll", "safety_reports.portal_poll.poll_interval_seconds", "safety_reports", 60),
    IntervalDaemon("org.solutionsmith.its.compile-now-poll", "safety_reports.compile_now_poll.poll_interval_seconds", "safety_reports", 90),
    IntervalDaemon("org.solutionsmith.its.progress-send", "progress_reports.progress_send.poll_interval_seconds", "progress_reports", 900),
    IntervalDaemon("org.solutionsmith.its.fieldops-sync", "field_ops.fieldops_sync.poll_interval_seconds", "field_ops", 90),
    IntervalDaemon("org.solutionsmith.its.po-poll", "po_materials.po_poll.poll_interval_seconds", "po_materials", 90),
    IntervalDaemon("org.solutionsmith.its.po-send", "po_materials.po_send.poll_interval_seconds", "po_materials", 900),
    IntervalDaemon("org.solutionsmith.its.subcontract-poll", "subcontracts.subcontract_poll.poll_interval_seconds", "subcontracts", 120),
    IntervalDaemon("org.solutionsmith.its.subcontract-send", "subcontracts.subcontract_send.poll_interval_seconds", "subcontracts", 900),
    # RFQ / vendor-estimate lane (ADR-0004). Retuning the CADENCE is a plain
    # Class-B interval edit, independent of the runtime gate either way: a gated-off
    # daemon just runs its (still no-op) cycle on the new cadence.
    IntervalDaemon("org.solutionsmith.its.estimate-poll", "po_materials.estimate_poll.poll_interval_seconds", "po_materials", 120),
    IntervalDaemon("org.solutionsmith.its.rfq-poll", "po_materials.rfq_poll.poll_interval_seconds", "po_materials", 120),
    IntervalDaemon("org.solutionsmith.its.rfq-send", "po_materials.rfq_send.poll_interval_seconds", "po_materials", 900),
]
INTERVAL_DAEMONS: dict[str, IntervalDaemon] = {d.label: d for d in _DAEMONS}


@dataclass
class IntervalOutcome:
    kind: str  # applied | noop | rejected | not_editable | error (also CSS class + test assertion)
    message: str
    label: str


def _load(name: str) -> Any:
    return importlib.import_module(name)


def is_interval_daemon(label: str) -> bool:
    return label in INTERVAL_DAEMONS


def read_interval_state() -> list[dict[str, Any]]:
    """Every registered interval daemon + its current poll_interval from ITS_Config, for
    the editor UI. A daemon whose row is unseeded shows present=False. Fail-soft
    per-daemon (a read error degrades that one row to 'unavailable')."""
    from operator_dashboard.act.config_write import read_current

    out: list[dict[str, Any]] = []
    for d in _DAEMONS:
        try:
            _row_id, current = read_current(d.config_key, d.workstream)
            present = current is not None
        except Exception:
            current, present = None, False
        out.append(
            {
                "label": d.label,
                # CSS/URL-safe id for the per-row htmx swap target (dots in the
                # label would be read as class selectors by htmx's hx-target).
                "slug": d.label.replace(".", "-"),
                "config_key": d.config_key,
                "workstream": d.workstream,
                "default": d.default,
                "value": current if current is not None else "",
                "present": present,
                "min": MIN_INTERVAL,
                "max": MAX_INTERVAL,
            }
        )
    return out


def _validate_interval(raw: str) -> int:
    """A positive whole number of seconds in [MIN, MAX]. ASCII-only (rejects a
    smuggled Unicode digit) and no sign/float. The message IS the rejection reason."""
    s = raw.strip()
    if not (s.isascii() and s.isdigit()):  # rejects '', '-5', '1.0', '9x', Unicode digits
        raise ValueError(f"must be a whole number of seconds (got {raw!r})")
    if len(s) > 6:  # guard int() against a pathological giant string (> MAX anyway)
        raise ValueError(f"interval too large (max {MAX_INTERVAL})")
    n = int(s)
    if n < MIN_INTERVAL or n > MAX_INTERVAL:
        raise ValueError(f"must be {MIN_INTERVAL}..{MAX_INTERVAL} seconds (got {n})")
    return n


def edit_interval(label: str, new_interval_raw: str, operator: str) -> IntervalOutcome:
    """Change an interval daemon's cadence: validate → persist the ITS_Config
    poll_interval row → reinstall the plist with the explicit interval. The
    elevated-confirm ceremony is verified by the router before this is called."""
    daemon = INTERVAL_DAEMONS.get(label)
    if daemon is None:
        # label allowlist bites — never a non-interval service or a non-ITS label
        return IntervalOutcome("not_editable", f"{label!r} is not an editable interval daemon", label)
    try:
        interval = _validate_interval(new_interval_raw)
    except ValueError as exc:
        return IntervalOutcome("rejected", str(exc), label)

    from operator_dashboard.act.config_write import read_current

    try:
        row_id, current = read_current(daemon.config_key, daemon.workstream)
    except Exception as exc:
        return IntervalOutcome("error", f"could not read current interval: {type(exc).__name__}: {exc}", label)
    if row_id is None:
        # seeding a MISSING ITS_Config row is a §44 high-class action (Seth), not Tier-2
        return IntervalOutcome(
            "not_editable",
            f"no ITS_Config row for {daemon.config_key} — seed it first (Developer-Operator)",
            label,
        )
    if (current or "").strip() == str(interval):
        return IntervalOutcome("noop", f"already {interval}s — no change made", label)

    # 1. persist to ITS_Config FIRST (the source of truth install.sh reads no-arg)
    try:
        ss = _load("shared.smartsheet_client")
        sid = _load("shared.sheet_ids")
        ss.update_rows(sid.SHEET_CONFIG, [{"_row_id": row_id, "Value": str(interval)}])
    except Exception as exc:
        return IntervalOutcome("error", f"ITS_Config write failed: {type(exc).__name__}: {exc}", label)

    # 2. re-render + re-bootstrap the plist with the EXPLICIT interval (no
    #    read-after-write race on the row just written)
    rc, err = _run_install_sh(label, interval)
    if rc != 0:
        # ITS_Config is now AHEAD of the live plist — durably audit the desync so
        # the cadence-vs-row mismatch is never silent; the operator retries.
        # install.sh boots the daemon OUT before re-bootstrapping, so a failed
        # reinstall may have left it UNLOADED, not merely on the old cadence —
        # report that honestly (§55) so the operator checks + reloads.
        _audit_desync(operator, label, daemon, current, interval, rc)
        return IntervalOutcome(
            "error",
            f"ITS_Config updated to {interval}s but plist reinstall failed (exit {rc}) — the daemon "
            f"may now be UNLOADED (install.sh boots it out before re-bootstrapping). Run "
            f"`install.sh status {label}` to check, then `install.sh load {label} {interval}` to reload.",
            label,
        )
    _audit(operator, label, daemon, current, interval)
    return IntervalOutcome(
        "applied", f"{label}: {current!r} → {interval}s (ITS_Config updated + plist reinstalled)", label
    )


def _run_install_sh(label: str, interval: int) -> tuple[int, str]:
    """`install.sh load <label> <interval>` — re-render + bootstrap the plist.
    Returns (returncode, short stderr). Never raises (partial failure is handled
    by the caller's desync audit)."""
    if not _INSTALL_SH.is_file():
        return 127, "install.sh not found"
    try:
        proc = subprocess.run(
            [str(_INSTALL_SH), "load", label, str(interval)],
            cwd=str(_INSTALL_SH.parent),
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT,
            check=False,
        )
    except Exception as exc:
        return 1, type(exc).__name__
    return proc.returncode, (proc.stderr or "")[:200]


def _audit(operator: str, label: str, daemon: IntervalDaemon, old: str | None, new: int) -> None:
    try:
        el = _load("shared.error_log")
        ts = datetime.now(UTC).isoformat()
        el.log(
            el.Severity.WARN,
            "operator_dashboard.config_editor",
            f"daemon interval edited: {label} ({daemon.config_key}) {old!r} -> {new}s by {operator} "
            f"(elevated-confirm; ITS_Config + plist reinstall) at {ts}",
            error_code="config_interval_edited",
            alert=False,
        )
    except Exception:
        pass


def _audit_desync(operator: str, label: str, daemon: IntervalDaemon, old: str | None, new: int, rc: int) -> None:
    try:
        el = _load("shared.error_log")
        ts = datetime.now(UTC).isoformat()
        el.log(
            el.Severity.WARN,
            "operator_dashboard.config_editor",
            f"daemon interval DESYNC: {label} ITS_Config set to {new}s but install.sh reinstall FAILED "
            f"(exit {rc}) by {operator} at {ts} — the daemon may now be UNLOADED (install.sh does "
            f"bootout-then-bootstrap); verify with `install.sh status {label}` and reload",
            error_code="config_interval_reinstall_desync",
            alert=False,
        )
    except Exception:
        pass


# --- daemon control verb (Class B): start / stop / kickstart -------------------
@dataclass
class ControlOutcome:
    kind: str  # ok | rejected | not_editable | error (also CSS class + test assertion)
    message: str
    label: str


def controllable_labels() -> set[str]:
    """ITS daemon labels the operator may start / stop / kickstart: any
    `org.solutionsmith.its.*.plist` present in scripts/launchd/, MINUS the
    dashboard's own label (a service must not stop itself via its own UI). This
    is the allowlist — a label not in it is refused before any launchctl call."""
    labels: set[str] = set()
    try:
        for p in LAUNCHD_DIR.glob(f"{_LABEL_PREFIX}*.plist"):
            labels.add(p.stem)
    except Exception:
        pass
    labels.discard(DASHBOARD_LABEL)
    return labels


def read_control_state() -> list[dict[str, Any]]:
    """The controllable daemons (labels) for the control UI. Live loaded/running
    state is shown by the separate read-only daemons panel; this lists what can
    be controlled."""
    return [{"label": lbl, "slug": lbl.replace(".", "-")} for lbl in sorted(controllable_labels())]


def control_daemon(label: str, action: str, operator: str) -> ControlOutcome:
    """start (install.sh load) / stop (install.sh unload) / kickstart (launchctl
    kickstart -k) an allowlisted ITS daemon. The elevated-confirm ceremony is
    verified by the router before this runs. No ITS_Config write — pure launchctl
    process management (the runtime ITS_Config gates still apply on the daemon's
    next cycle, so starting a dark daemon does nothing until its gate is on)."""
    if label not in controllable_labels():
        # allowlist bites — never a non-ITS label, an absent plist, or the dashboard itself
        return ControlOutcome("not_editable", f"{label!r} is not a controllable ITS daemon", label)
    if action not in CONTROL_ACTIONS:
        return ControlOutcome("rejected", f"action must be one of {CONTROL_ACTIONS} (got {action!r})", label)
    if action == "start":
        rc, _err = _run_install_sh_cmd("load", label)
    elif action == "stop":
        rc, _err = _run_install_sh_cmd("unload", label)
    else:  # kickstart
        rc, _err = _run_kickstart(label)
    ok = rc == 0
    _audit_control(operator, label, action, rc, ok=ok)
    if not ok:
        return ControlOutcome("error", f"{action} {label} failed (exit {rc}) — check install.sh status {label}", label)
    return ControlOutcome("ok", f"{label}: {action} ok", label)


def _run_install_sh_cmd(cmd: str, label: str) -> tuple[int, str]:
    """`install.sh <cmd> <label>` (load / unload). Returns (rc, short stderr).
    Never raises."""
    if not _INSTALL_SH.is_file():
        return 127, "install.sh not found"
    try:
        proc = subprocess.run(
            [str(_INSTALL_SH), cmd, label],
            cwd=str(_INSTALL_SH.parent),
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT,
            check=False,
        )
    except Exception as exc:
        return 1, type(exc).__name__
    return proc.returncode, (proc.stderr or "")[:200]


def _run_kickstart(label: str) -> tuple[int, str]:
    """`launchctl kickstart -k gui/<uid>/<label>` — restart a loaded daemon (kill
    the running instance, start fresh). Never raises."""
    try:
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT,
            check=False,
        )
    except Exception as exc:
        return 1, type(exc).__name__
    return proc.returncode, (proc.stderr or "")[:200]


def _audit_control(operator: str, label: str, action: str, rc: int, *, ok: bool) -> None:
    try:
        el = _load("shared.error_log")
        ts = datetime.now(UTC).isoformat()
        verb = f"{action} ok" if ok else f"{action} FAILED (exit {rc})"
        el.log(
            el.Severity.WARN,
            "operator_dashboard.config_editor",
            f"daemon control: {label} {verb} by {operator} (elevated-confirm) at {ts}",
            error_code="config_daemon_control",
            alert=False,
        )
    except Exception:
        pass
