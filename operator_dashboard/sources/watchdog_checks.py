"""Panel: watchdog scheduled-job markers (a read-only mirror of Check C).

The real watchdog module owns the tracked-job list, per-job freshness
windows, and the marker directory. We import it lazily and read those
constants so this panel can never drift from what the watchdog actually
checks. A failed import degrades only this panel (fail-soft base wrapper).
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from operator_dashboard.config import ITS_HOME
from operator_dashboard.sources.base import (
    SEV_ERROR,
    SEV_INFO,
    SEV_OK,
    SEV_WARN,
    DataSource,
    PanelResult,
    clean,
    fmt_timedelta,
    worst_sev,
)
from operator_dashboard.system_map import NODE_BY_MARKER


class WatchdogChecksSource(DataSource):
    panel_id = "watchdog"
    title = "Watchdog markers (Check C)"

    def _fetch(self, detail: bool = False) -> PanelResult:
        # `scripts/` is not a Python package (no __init__; absent from the
        # editable-install package list), so `import scripts.watchdog` resolves
        # only when a tree root is on sys.path. Pin it to the OBSERVATION root
        # (~/its) at sys.path[0] — CWD-independent, and always the LIVE tree's
        # tracked-jobs/windows/marker-dir, not whatever tree happens to be on
        # the path. A failed import still degrades only this panel (base wrapper).
        its_home = str(ITS_HOME)
        if its_home not in sys.path:
            sys.path.insert(0, its_home)
        wd: Any = importlib.import_module("scripts.watchdog")
        marker_dir: Path = wd.WATCHDOG_MARKER_DIR
        tracked: list[str] = list(wd.TRACKED_JOBS)
        windows: dict[str, timedelta] = dict(wd.TRACKED_JOB_WINDOWS)
        default_window: timedelta = wd.DEFAULT_TRACKED_JOB_WINDOW

        now = datetime.now(UTC)
        rows: list[dict[str, str]] = []
        worst = SEV_OK
        stale = 0
        for job in tracked:
            window = windows.get(job, default_window)
            last_run, age, sev, state = self._read_marker(
                marker_dir / f"{job}.last_run", now, window
            )
            if sev != SEV_OK:
                stale += 1
            worst = worst_sev(worst, sev)
            row = {
                "job": clean(job),
                "last run (UTC)": clean(last_run),
                "age": clean(age),
                "window": clean(fmt_timedelta(window)),
                "state": clean(state),
                "_sev": sev,
            }
            # Deep-link the marker to its system-map node, like the launchd and
            # heartbeat panels do. A stale marker is the first thing an operator
            # sees; the node rail is where the runbook + gate live.
            node_id = NODE_BY_MARKER.get(job)
            if node_id:
                row["_link_job"] = f"/system?focus={node_id}"
            rows.append(row)
        # The watchdog's own marker is intentionally NOT tracked (a daemon
        # can't detect its own death) — surface it as informational only.
        self_run, self_age, _, self_state = self._read_marker(
            marker_dir / "watchdog.last_run", now, default_window
        )
        rows.append(
            {
                "job": clean("watchdog (self)"),
                "last run (UTC)": clean(self_run),
                "age": clean(self_age),
                "window": clean("—"),
                "state": clean("ran" if self_state in ("fresh", "stale") else self_state),
                "_sev": SEV_INFO,
            }
        )
        return PanelResult(
            panel_id=self.panel_id,
            title=self.title,
            summary=f"{stale} stale / {len(tracked)} tracked",
            severity=worst,
            columns=["job", "last run (UTC)", "age", "window", "state"],
            rows=rows,
        )

    def _read_marker(
        self, marker: Path, now: datetime, window: timedelta
    ) -> tuple[str, str, str, str]:
        try:
            raw = marker.read_text().strip()
        except FileNotFoundError:
            return "—", "—", SEV_WARN, "missing"
        except OSError:
            return "—", "—", SEV_WARN, "unreadable"
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            return raw, "—", SEV_WARN, "unparseable"
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = now - ts
        if age > window:
            return ts.isoformat(), fmt_timedelta(age), SEV_WARN, "stale"
        return ts.isoformat(), fmt_timedelta(age), SEV_OK, "fresh"


class WatchdogSweepSource(DataSource):
    """Panel: the LAST SWEEP's per-check results (letters + verdicts).

    Reads the results file the watchdog writes at the end of every run
    (`wd.WATCHDOG_RESULTS_PATH`, owned by scripts/watchdog.py so the path can
    never drift). Before this panel, "did last night's sweep run, and which
    checks passed" was only inferable from ITS_Errors rows — a green sweep was
    invisible by construction.
    """

    panel_id = "watchdog_sweep"
    title = "Watchdog sweep (per-check results)"

    # A sweep older than this is itself a warning. Was 26h for the old daily 07:00 cadence;
    # tightened to 3h when the watchdog went HOURLY (2026-08-07, StartInterval 3600). The
    # whole point of the cadence change was to shrink the "nobody looked" window, and leaving
    # this at 26h would have kept the DASHBOARD blind for a day even while sweeps ran fine.
    # 3h = one hourly cadence + 2h slack, so a host that naps through a single sweep does not
    # flap the panel.
    STALE_AFTER = timedelta(hours=3)

    # Check-result severity (shared.error_log Severity names) -> panel severity.
    # INFO is a PASSING check (the watchdog logs INFO on healthy), so it renders ok.
    _SEV_MAP = {"INFO": SEV_OK, "WARN": SEV_WARN, "CRITICAL": SEV_ERROR, "ERROR": SEV_ERROR}

    def _fetch(self, detail: bool = False) -> PanelResult:
        its_home = str(ITS_HOME)
        if its_home not in sys.path:
            sys.path.insert(0, its_home)
        wd: Any = importlib.import_module("scripts.watchdog")
        # getattr fallback: the observed LIVE tree may briefly predate the
        # constant (deploy window); the fallback IS the same canonical path.
        results_path: Path = getattr(
            wd, "WATCHDOG_RESULTS_PATH", ITS_HOME / "state" / "watchdog_results.json"
        )

        try:
            payload = json.loads(results_path.read_text())
        except FileNotFoundError:
            return PanelResult(
                panel_id=self.panel_id,
                title=self.title,
                summary="no sweep results recorded yet — the file appears after the next daily run",
                severity=SEV_INFO,
                columns=["check", "result", "summary"],
                rows=[],
            )
        except (OSError, ValueError) as exc:
            return PanelResult(
                panel_id=self.panel_id,
                title=self.title,
                summary=f"results file unreadable ({type(exc).__name__})",
                severity=SEV_WARN,
                columns=["check", "result", "summary"],
                rows=[],
            )

        now = datetime.now(UTC)
        run_at_raw = str(payload.get("run_at") or "")
        run_age = "unknown age"
        run_sev = SEV_WARN
        try:
            run_at = datetime.fromisoformat(run_at_raw)
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=UTC)
            age = now - run_at
            run_age = f"{fmt_timedelta(age)} ago"
            run_sev = SEV_WARN if age > self.STALE_AFTER else SEV_OK
        except ValueError:
            pass

        rows: list[dict[str, str]] = []
        worst = run_sev
        n_warn = 0
        n_crit = 0
        for rec in payload.get("results") or []:
            severity = str(rec.get("severity") or "?")
            sev = self._SEV_MAP.get(severity, SEV_WARN)
            if severity == "WARN":
                n_warn += 1
            elif severity in ("CRITICAL", "ERROR"):
                n_crit += 1
            worst = worst_sev(worst, sev)
            name = str(rec.get("check") or "").removeprefix("_check_")
            rows.append(
                {
                    "check": clean(f"{rec.get('letter', '?')} · {name}"),
                    "result": clean("ok" if severity == "INFO" else severity),
                    "summary": clean(str(rec.get("summary") or "")),
                    "_sev": sev,
                }
            )

        maintenance = " · MAINTENANCE sweep (alerts were suppressed)" if payload.get(
            "alerts_suppressed"
        ) else ""
        summary = (
            f"ran {run_age} · {len(rows)} checks · {n_warn} WARN · "
            f"{n_crit} CRITICAL/ERROR{maintenance}"
        )
        return PanelResult(
            panel_id=self.panel_id,
            title=self.title,
            summary=summary,
            severity=worst,
            columns=["check", "result", "summary"],
            rows=rows,
        )
