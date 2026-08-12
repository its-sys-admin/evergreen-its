"""Progress Reports weekly compile — the PROGRESS instantiation of the shared `generate_core`.

Purpose
-------
The progress twin of `safety_reports.weekly_generate` — the SAME deterministic compile engine
(`safety_reports.generate_core.run_generate`), a different config. This module is the thin
PROGRESS binding (`PROGRESS_GENERATE_CONFIG`, P4 parameterize-not-clone, Op Stds §14): it
iterates the progress workspace's own `ITS_Active_Jobs_Progress` sheet, compiles each Active
job's Sat→Fri week of submitted progress-form PDFs into a Box packet, and dual-writes a Rollup
snapshot row + a PENDING `WPR_human_review` row (the progress twin of safety's WSR). The
progress send (P5) reads the human-approved WPR row and transmits.

Invariants
----------
- GENERATION half of the External Send Gate (Foundation Mission v11 Invariant 1): **Zero send
  capability. Zero AI.** `tests/test_capability_gating.py::GATED_SCRIPTS` AST-forbids
  `anthropic` / `anthropic_client` / `graph_client` / `send_mail` / `resend` / `smtplib` /
  `email.mime`. Box egress is the audited `shared.box_client` (via generate_core).
- NO cross-workstream mix-up (operator requirement): the workstream binding is the ONE
  `PROGRESS_GENERATE_CONFIG` — the review row goes to the WPR sheet via `wpr_review.add_wpr_row`
  (which tags `Workstream=progress`), recipients resolve ONLY from `ITS_Active_Jobs_Progress`,
  and the packet files under the progress Box root (`box_legacy_fallback=False` — no safety
  fallback). A progress compile can never write a safety review row or resolve a safety recipient.
- Deterministic over already-HMAC-verified PDFs + typed Smartsheet cells — NO LLM step, so
  Invariant-2 Layer-2 untrusted-content tagging is N/A here.
- Trigger/idempotency (in generate_core): Friday 14:30 local launchd `StartCalendarInterval`,
  staggered 30 min after safety's 14:00 (both hold the host compile mutex);
  SKIP-if-already-compiled-and-no-new-docs; NEVER closes the week; empty week → STILL appends a
  Rollup + WPR row.

Failure modes
-------------
Per-job timeout / memory fences route a single job-week to `ITS_Review_Queue` and continue (one
bad job never tears down the run); an unset progress Box root surfaces a config gap to the
per-job fence (no silent safety-tree fallback, `box_legacy_fallback=False`); a missed Friday run
is operator-recovered by a manual re-run. Full successor-remediation fault tree:
`docs/runbooks/progress_weekly_generate.md` (Op Stds §43).

Consumers
---------
- launchd daemon `org.solutionsmith.its.progress-generate` (the Friday 14:30 trigger).
- The progress send poll (P5) reads the human-approved `WPR_human_review` rows this writes.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from progress_reports import wpr_data, wpr_review
from safety_reports import form_pdf, generate_core
from safety_reports.week_sheet import PROGRESS_WEEK_SHEET_CONFIG
from shared import active_jobs, keychain, portal_client, review_queue, smartsheet_client
from shared.active_jobs import ActiveJob
from shared.error_log import Severity, its_error_log
from shared.error_log import log as error_log_log
from shared.kill_switch import require_active
from shared.required_config import ConfigKey, resolve_and_log
from shared.safety_week import SafetyWeek

SCRIPT_NAME = "progress_reports.progress_weekly_generate"

# The progress Box portal-root ITS_Config key (the progress twin of
# safety_naming.CFG_BOX_PORTAL_ROOT). Unset → no Box mirror tree yet; progress has NO legacy
# project_routing fallback (box_legacy_fallback=False), so an unset root surfaces a config gap
# to the per-job fence rather than silently filing into a safety/legacy tree.
CFG_BOX_PORTAL_ROOT = "progress_reports.box.portal_root_folder_id"

# ── P6 rollup-numbers page: creds + provider closure ──────────────────────────────────
# The rollup page reads the SEND-FREE Worker route (GET /api/internal/progress-rollup) via the
# F02-allowlisted `shared.portal_client` — the SAME base_url + bearer `safety_reports.portal_poll`
# resolves (ITS_Config `safety_reports.portal.worker_base_url` + Keychain
# `ITS_PORTAL_INTERNAL_TOKEN`). The closure lives HERE (already in GATED_SCRIPTS), NOT in
# generate_core, so the shared engine gains no portal_client / keychain / form_pdf-render import
# and stays a pure, gate-clean engine (§42). The fetch is a READ of our own send-free Worker,
# structurally identical to the portal_poll pull — NOT an external transmission (Invariant 1).
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"
KC_BEARER = "ITS_PORTAL_INTERNAL_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
_PACIFIC = ZoneInfo("America/Los_Angeles")


def _week_epoch_window(week: SafetyWeek) -> tuple[int, int]:
    """The Sat→Fri week as a `[from, to)` epoch-seconds window at Pacific-local midnight
    boundaries (`from` = Saturday 00:00 Pacific inclusive; `to` = the following Saturday 00:00
    Pacific exclusive). D1 stores event/record times as `unixepoch()` (UTC seconds); anchoring
    the window on Pacific midnight (the workstream tz) makes it the operator's calendar week."""
    start = datetime.combine(week.start, time.min, tzinfo=_PACIFIC)
    end = datetime.combine(week.end + timedelta(days=1), time.min, tzinfo=_PACIFIC)
    return int(start.timestamp()), int(end.timestamp())


def _resolve_rollup_creds() -> tuple[str, str] | None:
    """Resolve (base_url, bearer) for the rollup read, fail-CLOSED. None when EITHER is unset —
    the progress workstream may not be fully cut over yet, so an UNWIRED rollup is a quiet no-op
    (no page), NOT an error. A wired-but-broken rollup (transport/500) instead RAISES from
    `get_progress_rollup` → the compile's rollup fence WARNs (never silent).

    Never-silent nuance: a config-row-absent read (`SmartsheetNotFoundError` = not cut over) stays
    quiet, but an OPEN circuit breaker (`SmartsheetCircuitOpenError` = Smartsheet degraded RIGHT
    NOW) is a LIVE signal, not the same as "not wired" — so it WARNs (`rollup_creds_circuit_open`)
    before failing closed, so an operator scanning ITS_Errors isn't left inferring silence."""
    try:
        raw = smartsheet_client.get_setting(CFG_WORKER_BASE_URL, workstream="progress_reports")
        base_url = raw.strip() if isinstance(raw, str) else ""
    except smartsheet_client.SmartsheetNotFoundError:
        # Config row absent — the progress workstream isn't cut over yet. Quiet no-op (no page).
        base_url = ""
    except smartsheet_client.SmartsheetCircuitOpenError:
        # DISTINCT from "not configured": Smartsheet is actively degraded (breaker open). WARN so
        # the live signal is observable, then fail closed (no page this cycle; self-heals when the
        # breaker closes and the next Friday compile re-reads). error_log is fail-soft, so this
        # never crashes the fenced compile even while Smartsheet is down.
        error_log_log(
            Severity.WARN, SCRIPT_NAME,
            "rollup creds unresolved: Smartsheet circuit breaker OPEN (degraded) — progress rollup "
            "numbers page SKIPPED this cycle; it returns when Smartsheet recovers.",
            error_code="rollup_creds_circuit_open",
        )
        base_url = ""
    try:
        bearer = keychain.get_secret(KC_BEARER)
    except keychain.KeychainError:
        bearer = ""
    if not (base_url and bearer):
        return None
    return base_url, bearer


def _rollup_page_provider(job: ActiveJob, week: SafetyWeek) -> bytes | None:
    """PROGRESS rollup-numbers page provider (P6). Bound into `PROGRESS_GENERATE_CONFIG`;
    `generate_core` calls it FENCED (`_maybe_rollup_page`) after the cover, before the index.
    Returns None (no page) when creds are unset; otherwise fetches the send-free Worker rollup
    aggregate and renders the numbers page. A transport error PROPAGATES (→ the fence WARNs
    `rollup_page_failed` and compiles WITHOUT the page) so a broken-but-wired rollup is loud."""
    creds = _resolve_rollup_creds()
    if creds is None:
        return None
    base_url, bearer = creds
    week_from, week_to = _week_epoch_window(week)
    numbers = portal_client.get_progress_rollup(
        base_url, bearer, job_id=job.job_id, week_from=week_from, week_to=week_to
    )
    return form_pdf.render_progress_rollup(job.project_name, generate_core._week_label(week), numbers)

def _client_report_provider(job: ActiveJob, week: SafetyWeek) -> bytes | None:
    """The CLIENT-FACING Weekly Production Report provider (0067).

    Same creds and the same fail-closed resolution as the rollup page — an unwired progress
    workstream produces no report rather than an error. `generate_core` calls this FENCED
    (`_maybe_client_report`), so a wired-but-broken report degrades to filing the field-records
    packet as the client's attachment and WARNs; it never costs the week its review row.

    The closure lives HERE, not in `generate_core`, for the reason the rollup provider does: the
    shared engine must gain no portal_client / keychain / renderer coupling and stay a pure,
    gate-clean engine (§42).
    """
    creds = _resolve_rollup_creds()
    if creds is None:
        return None
    base_url, bearer = creds
    return form_pdf.render_production_report(
        wpr_data.build(job, week, base_url=base_url, bearer=bearer)
    )


PROGRESS_GENERATE_CONFIG = generate_core.GenerateConfig(
    script_name=SCRIPT_NAME,
    workstream="progress_reports",
    week_sheet_config=PROGRESS_WEEK_SHEET_CONFIG,
    active_jobs_config=active_jobs.PROGRESS_ACTIVE_JOBS_CONFIG,
    review_sheet_id=wpr_review.SHEET_ID,
    # Deferred lookup (mockable at call time, like the safety binding); add_wpr_row bakes in the
    # WPR sheet id + Workstream=progress.
    add_review_row=lambda **kw: wpr_review.add_wpr_row(**kw),
    email_body_template=wpr_review.email_body_template,  # the generic seed body (re-export)
    box_root_setting_key=CFG_BOX_PORTAL_ROOT,
    box_legacy_fallback=False,  # progress has no legacy Box tree — require the portal root
    compile_mutex_role="progress",
    watchdog_slug="progress_weekly_generate",
    sla_tier=review_queue.SlaTier.SAFETY_INTAKE,  # the 4h review window; Workstream tag distinguishes
    cfg_job_timeout="progress_reports.progress_weekly_generate.job_timeout_seconds",
    default_job_timeout=600,
    cfg_memory_ceiling="progress_reports.progress_weekly_generate.merge_memory_ceiling_bytes",
    default_memory_ceiling=256 * 1024 * 1024,
    cfg_evergreen_contact="progress_reports.evergreen_contact_name",
    default_evergreen_contact="the Evergreen Renewables office",
    # P6: the optional rollup-numbers page (progress only). Safety binds nothing → byte-identical.
    rollup_page_provider=_rollup_page_provider,
    # The progress packet's cover names itself (the shared default is the SAFETY title —
    # unbound, every progress cover was mislabeled "WEEKLY SAFETY REPORT").
    cover_title="WEEKLY PROGRESS REPORT",
    # 0067: the client receives the synthesized 5-page Weekly Production Report; the compiled
    # packet of daily field records stays internal (filed to Box, on the week-sheet Rollup row,
    # linked from the review row's Notes). Safety binds neither and is byte-identical.
    client_report_provider=_client_report_provider,
    client_report_suffix="WPR",
    # The internal packet is NOT a WSR — it is this job-week's filed field records. Naming it so
    # ends the case where one Box week folder held `..._WPR.pdf` beside `..._WSR.pdf` and only
    # file size told an operator which was the client's document.
    packet_suffix="FieldRecords",
    # A progress week with no daily reports is HELD for the office to decide rather than
    # emailing a client a hollow document or failing quietly at send time.
    empty_week_hold=True,
)

# #336 — every ITS_Config key the progress compile resolves at RUNTIME: the four carried on the
# GenerateConfig (read by generate_core under config.workstream='progress_reports') PLUS the
# SHARED Worker base-URL read HERE under progress_reports for the P6 rollup page. Declared for
# the startup observability pass (resolve_and_log).
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(PROGRESS_GENERATE_CONFIG.box_root_setting_key, PROGRESS_GENERATE_CONFIG.workstream, "", "str"),
    ConfigKey(
        PROGRESS_GENERATE_CONFIG.cfg_job_timeout, PROGRESS_GENERATE_CONFIG.workstream,
        PROGRESS_GENERATE_CONFIG.default_job_timeout, "int",
    ),
    ConfigKey(
        PROGRESS_GENERATE_CONFIG.cfg_memory_ceiling, PROGRESS_GENERATE_CONFIG.workstream,
        PROGRESS_GENERATE_CONFIG.default_memory_ceiling, "int",
    ),
    ConfigKey(
        PROGRESS_GENERATE_CONFIG.cfg_evergreen_contact, PROGRESS_GENERATE_CONFIG.workstream,
        PROGRESS_GENERATE_CONFIG.default_evergreen_contact, "str",
    ),
    ConfigKey(
        CFG_WORKER_BASE_URL, "progress_reports", "", "str",
        description="Shared Worker base URL, read under progress_reports for the P6 rollup page.",
    ),
]


@its_error_log(SCRIPT_NAME)
@require_active
def main(week_start_override: date | None = None) -> dict[str, Any]:
    """Compile weekly progress packets + dual-write Rollup/WPR for each Active progress job.

    Args:
        week_start_override: any date inside the target Sat→Fri week (backfill). Defaults to
            the week containing today (Friday run → the just-closed week).
    """
    # #336 startup observability (after @require_active, fail-open). Additive (§14).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    return generate_core.run_generate(
        PROGRESS_GENERATE_CONFIG, week_start_override=week_start_override
    )


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="progress_reports.progress_weekly_generate",
        description="Compile weekly progress packets + dual-write Rollup/WPR for each Active job.",
    )
    parser.add_argument(
        "--week-start", type=lambda s: date.fromisoformat(s), default=None,
        help="Any date inside the target Sat→Fri week (backfill). Defaults to the week containing today.",
    )
    args = parser.parse_args(argv)
    main(week_start_override=args.week_start)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
