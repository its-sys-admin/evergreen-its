"""Mac-side Form-editor publish daemon (Phase-2 slice 3b) — the SOLE privileged actuator.

The cloud Worker can only ENQUEUE a publish request (send-free, slice 3a). This launchd
daemon is the trusted Mac side that actuates it — mirroring the External Send Gate: the
privileged commit/deploy capability lives on the Mac (with the operator's git + wrangler
auth), never on the cloud. Per claimed request it runs the C12=A pipeline, stamping the
publish_requests state machine at each milestone so the admin Status Monitor tracks it:

    pull GET /api/internal/publish/pending  (portal_client, bearer-gated)
      → atomically CLAIM one (portal_client.claim_publish — lease; concurrent runs skip)
      → re-validate vs LIVE git HEAD (meta-schema + publish_manifest.apply_publish, C3)
      → STAMP validated
      → apply to the worktree (write the form file(s) + catalog.json), commit, open a PR,
        wait for CI (the 3-renderer render smoke, slice 3c), MERGE on green   → STAMP tested
      → deploy via the operator's LOCAL wrangler + fast-forward the live ~/its tree +
        post-deploy health check (GET the live form)                          → STAMP live
      → regenerate the Box blank archive                                      → STAMP archived
    ANY stage failure → STAMP failed(stage, reason) + an operator CRITICAL triple-fire
    (detect-and-alert, C12 mandate: never a silent stall an idle-logged-out admin can't see).

Deploy gate (Slice 1, R3-F1): the daemon REFUSES to deploy the Worker ahead of unapplied
remote D1 migrations (forensic class #2 — publish #434 shipped the Worker while
0030/0031/0032 sat unapplied, 500ing live routes). Checked pre-claim each cycle with work
(rows stay pending → the operator's `migrations apply` unblocks the next cycle
automatically) AND authoritatively post-pull in _deploy_land_health. Refusal only — a live
D1 apply is operator-gated (mirrors .claude/hooks/block-stale-cloudflare-deploy.sh).

Capability gating (Invariant 1): enrolled in tests/test_capability_gating.py — it actuates
code (commit/deploy) but performs ZERO external customer transmission, so it imports no
send capability (anthropic / send_mail / resend / smtplib / email.mime). The privileged
git/wrangler operations are subprocess calls to the operator's own toolchain.

launchd: `publish_once()` is the public API; `__main__` calls it once and exits. launchd
handles the cadence (StartInterval). High-capability + operator-gated activation; see the
§43 runbook in safety_reports/README.md.

The privileged ops (`_apply_to_worktree`, `_commit_test_merge`, `_deploy_land_health`,
`_regenerate_archive`) are isolated module-level functions: the orchestration + stamping
+ error handling are fully unit-tested with them mocked; their subprocess bodies run
against the operator's live git/wrangler/Box and are validated by the operator's smoke
(the SDK-vs-live discipline, Op Stds §30).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from safety_reports.publish_manifest import PublishApplyError, apply_publish
from shared import (
    circuit_breaker,
    creds_resolution,
    error_log,
    form_category,
    keychain,
    portal_client,
    smartsheet_client,
    sustained_failure,
)
from shared.creds_resolution import TransientUnavailable
from shared.error_log import Severity, its_error_log
from shared.heartbeat import HeartbeatReporter, HeartbeatStatus
from shared.kill_switch import require_active
from shared.redact import redact
from shared.required_config import ConfigKey, resolve_and_log

SCRIPT_NAME = "publish_daemon"
WORKSTREAM = "safety_reports"

# Creds (fail-closed): same internal bearer + Worker base URL as portal_poll.
KC_BEARER = "ITS_PORTAL_INTERNAL_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"
CFG_POLLING_ENABLED = "safety_reports.publish_daemon.polling_enabled"

# #336 — every ITS_Config key this daemon resolves at RUNTIME, declared for the startup
# observability pass (resolve_and_log). polling_enabled ships OFF (default False); the shared
# Worker base-URL reads to "" when unset.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_POLLING_ENABLED, WORKSTREAM, False, "bool"),
    ConfigKey(CFG_WORKER_BASE_URL, WORKSTREAM, "", "str"),
]

# ITS_Daemon_Health heartbeat (R4-F1 — the operator-visibility row the other pollers
# self-provision). HEARTBEAT_ROW_STATE_PATH is SHARED with the other daemons — same JSON
# file, different daemon_name key (ARCH-2). POLL_INTERVAL_SECONDS mirrors the plist
# StartInterval (120s; publishes are infrequent + the cycle is heavy).
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "publish_daemon_heartbeat.txt"
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"
DAEMON_NAME = "safety_reports.publish_daemon"
POLL_INTERVAL_SECONDS = 120

# A1 self-provision metadata (the ONLY per-daemon difference in the heartbeat helpers).
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/internal/publish/pending"

# Shared ITS_Daemon_Health reporter for this daemon (mirrors fieldops_sync / portal_poll).
_heartbeat_reporter = HeartbeatReporter(
    script_name=SCRIPT_NAME,
    daemon_name=DAEMON_NAME,
    workstream=WORKSTREAM,
    liveness_path=HEARTBEAT_PATH,
    interval_seconds=POLL_INTERVAL_SECONDS,
    source_id=_REGISTRATION_SOURCE_ID,
    row_state_path=HEARTBEAT_ROW_STATE_PATH,  # shared file — make the contract explicit
)

# Watchdog Check-C liveness marker (scripts/watchdog.py TRACKED_JOBS). The heartbeat row
# above is a PUSH surface this daemon writes about itself; Check C is the independent
# staleness floor that notices when the daemon stops writing anything at all. Without it a
# silently dead publish actuator is invisible — approved form definitions simply stop
# reaching the live portal and the queue just looks empty.
WATCHDOG_MARKER_DIR = Path.home() / "its" / ".watchdog"
WATCHDOG_JOB_SLUG = "publish_daemon"

_ROOT = Path(__file__).resolve().parent.parent
_CATALOG_PATH = _ROOT / "safety_portal" / "catalog.json"
_FORMS_DIR = _ROOT / "safety_portal" / "forms"
_META_SCHEMA_PATH = _FORMS_DIR / "meta-schema.json"
_REQUIRED_CONTENT_PATH = _ROOT / "safety_portal" / "required-content.json"

# A request still carrying a composed definition (vs delete/rollback which flip the manifest).
_DEFINITION_OPS = frozenset({"create", "edit", "add_version"})

# CI poll cadence for the publish merge. The repo has auto-merge DISABLED, so the daemon
# waits for the render-smoke gate (C12) ITSELF and merges synchronously — bounded so one
# stuck CI run can't wedge the daemon.
CI_POLL_S = 20.0
CI_TIMEOUT_S = 900.0  # 15 min — generous vs the ~3-5 min portal CI

# Stale-row reclaim (PR-2): a non-terminal publish_requests row whose updated_at is older than this
# is swept to failed('stale_reclaimed') at the top of each cycle — recovering a publish whose daemon
# claimed-then-died (or stalled mid-stage), which otherwise wedges the parent forever via the
# Worker's C8 in-flight check. MUST exceed CI_TIMEOUT_S + deploy slack (and the Worker's LEASE_TTL_S)
# so a legitimately in-progress publish is never reclaimed — every stamp bumps updated_at, so a
# healthy publish never looks stale.
STALE_RECLAIM_S = 2700.0  # 45 min

# D1 pending-migrations deploy gate (Complete-State Slice 1, R3-F1 — forensic class #2).
# The publish deploy must NEVER ship the Worker ahead of unapplied remote D1 migrations:
# publish #434 auto-deployed the Worker while 0030/0031/0032 sat unapplied, 500ing the
# daily-requirements + expected-materials routes on the live portal (the 2026-06-28
# stale-tree universal lockout was the same class through a different door —
# .claude/hooks/block-stale-cloudflare-deploy.sh guards CC sessions; THIS guards the
# daemon's own §50 actuator, which runs outside any CC session). Posture mirrors the hook:
# REFUSE, never auto-apply — a live D1 `migrations apply` is operator-gated (README
# punch-list order: pull → apply → deploy).
D1_DATABASE_NAME = "its-safety-portal-db"  # wrangler.jsonc d1_databases[0].database_name
_MIGRATIONS_DIR = _ROOT / "safety_portal" / "migrations"
ERR_PENDING_MIGRATIONS = "publish_daemon.deploy_blocked_pending_migrations"
ERR_MIGRATION_CHECK = "publish_daemon.migration_check_failed"

# Pre-work Smartsheet-read fence (2026-07-21). A 30 s ReadTimeout inside `get_setting`
# (via `_resolve_creds`) escaped the pass at 14:37Z and `@its_error_log` stamped it
# CRITICAL `uncaught_exception`; the daemon was healthy again 120 s later. Threshold 5
# (fast cadence, operator decision D2 — the plist StartInterval is 120 s).
#
# TIME-TO-PAGE IN WALL CLOCK, not in cycles. 5 cycles is NOT 10 minutes: a FAILING cycle
# costs more than the interval, because a timeout-class failure re-spends the 30 s SDK
# network timeout on each bounded retry (~97 s worst case for the read) and launchd will
# not start the next fire while one is in flight. So the realistic band is ~10-15 min,
# not 10 min flat, and it is bounded above by the fleet-level circuit-open escalation
# (`sustained_failure.CIRCUIT_OPEN_SUSTAINED_SECONDS`, ~10-15 min) once the breaker trips
# and converts these into circuit-open observations.
_CONFIG_READ_FENCE = sustained_failure.TransientFence(
    SCRIPT_NAME,
    state_path=STATE_DIR / "publish_daemon_config_read_failures.json",
    transient_error_code="publish_daemon.config_read_transient",
    sustained_error_code="publish_daemon.config_read_sustained",
    threshold=sustained_failure.DEFAULT_CRITICAL_THRESHOLD,
    runbook="docs/runbooks/safety_portal_forms.md",
)


class PendingMigrationsError(RuntimeError):
    """A deploy would ship the Worker ahead of unapplied remote D1 migrations (refused)."""


@dataclass
class PublishStats:
    """Summary of one publish_once() invocation (for tests + logging)."""

    polled: int = 0
    actuated: int = 0
    failed: int = 0
    skipped_unclaimed: int = 0
    reclaimed: int = 0
    halted: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Creds:
    base_url: str
    bearer: str


def _lease_owner() -> str:
    """A stable-ish lease identifier for this host/process (audit + reclaim)."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _read_str_setting(key: str, fallback: str) -> str:
    """ITS_Config read, fail-soft to `fallback` ONLY when the row is genuinely absent.

    Every failure that means "we could not READ" — transient AND circuit-open —
    deliberately PROPAGATES rather than collapsing into `fallback`: silently reading the
    polling gate as "false" during a Smartsheet outage is the unobservable-config-
    resolution anti-pattern. `publish_once` fences it instead, so the halt is an ERROR row
    naming the real cause (and a CRITICAL once it is sustained).

    CIRCUIT-OPEN PROPAGATES TOO (2026-07-21 re-review; it used to be swallowed here). Two
    things went wrong while it was caught. (1) The cycle reported
    `halted="polling_disabled"` — a lie: nobody paused the daemon, Smartsheet was down —
    and the daemon contributed NOTHING to the fleet outage picture despite being its
    fastest potential observer at 120 s. (2) Because the call returned normally, the cycle
    then executed the "the read succeeded" path, which at the time also cleared the SHARED
    fleet circuit-open window; at StartInterval 120 that wiped the window every 2 minutes,
    so the fleet escalation could never mature to its 600 s threshold and was unreachable
    in production. A fail-open fallback is not evidence of reachability, and this reader
    must not manufacture one. `SmartsheetNotFoundError` still falls back — a missing row
    genuinely IS "unset", which for a polling gate means false.
    """
    try:
        raw = smartsheet_client.get_setting(key, workstream=WORKSTREAM)
    except smartsheet_client.SmartsheetNotFoundError:
        return fallback
    return raw if isinstance(raw, str) and raw else fallback


def _polling_enabled() -> bool:
    return _read_str_setting(CFG_POLLING_ENABLED, "false").strip().lower() in ("1", "true", "yes", "on")


def _resolve_creds() -> _Creds | TransientUnavailable | None:
    """Resolve (base_url, bearer) fail-CLOSED, three ways.

    `TransientUnavailable` means the base-URL row could not be READ this cycle
    (Smartsheet blip / circuit open) — self-heals, so the caller WARNs and skips.
    `None` means a credential is genuinely absent — a misconfig that halts loudly.

    The base-URL read deliberately does NOT go through `_read_str_setting`: that helper
    swallows both circuit-open and transient errors into its fallback, so a one-cycle
    Smartsheet blip arrived here as `""` and was indistinguishable from an unset row —
    which is exactly how a network hiccup produced an alert blaming publish-daemon
    *credentials* that were never missing (the live po_poll bug, 2026-07-20 04:42Z).
    Mirrors po_poll._resolve_credentials.
    """
    resolved = creds_resolution.read_base_url(CFG_WORKER_BASE_URL, WORKSTREAM)
    if isinstance(resolved, TransientUnavailable):
        return resolved
    base_url = (resolved or "").strip()
    if not base_url:
        return None
    try:
        bearer = keychain.get_secret(KC_BEARER)
    except Exception:  # noqa: BLE001 — any keychain failure is a fail-closed halt
        return None
    if not bearer:
        return None
    return _Creds(base_url=base_url.strip(), bearer=bearer)


def _halt_transient(stats: PublishStats, what: str) -> None:
    """Common tail for a transient-read halt: heartbeat + typed halted marker.

    Defined AFTER the fence has already logged the ERROR/CRITICAL row, so this only
    records liveness — the daemon is alive and deliberately idle, not wedged.
    """
    _write_heartbeat()
    _write_heartbeat_row(
        status="ERROR", items_processed=0,
        error_summary=f"halted: transient Smartsheet failure {what}",
    )
    stats.halted = "smartsheet_transient"


def _write_heartbeat() -> None:
    """Liveness file touch — thin delegator to the shared HeartbeatReporter.

    Kept as a module-level function because it is the canonical test mock seam
    (the suite patches this exact symbol). See shared/heartbeat.py (§42).
    """
    _heartbeat_reporter.write_liveness()


def _write_heartbeat_row(
    *,
    status: HeartbeatStatus,
    items_processed: int,
    error_summary: str | None = None,
    correlation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """ITS_Daemon_Health per-cycle row update — thin delegator to the shared
    HeartbeatReporter (the canonical test mock seam). See shared/heartbeat.py (§42)."""
    _heartbeat_reporter.write_row(
        status=status,
        items_processed=items_processed,
        error_summary=error_summary,
        correlation_id=correlation_id,
        notes=notes,
    )


def _write_watchdog_marker() -> None:
    """Touch the Check C freshness marker for this run (mirror po_poll / fieldops_sync)."""
    try:
        WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker = WATCHDOG_MARKER_DIR / f"{WATCHDOG_JOB_SLUG}.last_run"
        marker.write_text(datetime.now(UTC).isoformat())
    except OSError as exc:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"watchdog marker write failed: {exc!r}",
            error_code="watchdog_marker_failed",
        )


def _load_catalog() -> dict:
    return json.loads(_CATALOG_PATH.read_text())


def _load_required_content() -> dict:
    """The per-identity legal floor (Brief 1 PR-1), read from live HEAD so apply_publish's
    re-check (C3) uses the same manifest CI gates."""
    return json.loads(_REQUIRED_CONTENT_PATH.read_text())


def _validate_definition(definition: Any) -> None:
    """Structural re-validation against the live meta-schema (the daemon's half of the C3
    authoritative re-check; the manifest-level check is apply_publish). Raises on invalid."""
    schema = json.loads(_META_SCHEMA_PATH.read_text())
    jsonschema.validate(definition, schema)


# ── privileged ops (subprocess to the operator's toolchain; mocked in tests) ──────────
# Each raises on failure so the orchestration stamps failed(stage) + CRITICAL. The bodies
# run live under the operator's git/wrangler/Box auth and are validated by operator smoke.


def _apply_to_worktree(manifest: dict, files: dict[str, Any]) -> None:
    """Write the new catalog.json + each new form file to the repo worktree (append-only:
    never deletes a prior form file; design C1)."""
    # ensure_ascii=False: the committed catalog/form files hold literal UTF-8 (em-dashes,
    # curly quotes), so the default ensure_ascii=True re-escapes them on every daemon write
    # and each publish diff carries dozens of \uXXXX churn lines that hide the real change —
    # and it ping-pongs, because human-authored edits put the literal characters back. With
    # this, a daemon write of an unchanged manifest is byte-identical to what is on disk.
    _CATALOG_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    for form_code, definition in files.items():
        (_FORMS_DIR / f"{form_code}.json").write_text(
            json.dumps(definition, indent=2, ensure_ascii=False) + "\n"
        )


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_ROOT), *args], check=True, capture_output=True, text=True
    ).stdout


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], cwd=_ROOT, check=True, capture_output=True, text=True).stdout


def _reset_to_main() -> None:
    """Start each actuation from a CLEAN, current main — recover from any interrupted prior
    cycle (a leftover branch + uncommitted catalog/forms edits, e.g. the failed-at-merge that
    motivated this fix). Discards ONLY the daemon-managed paths (catalog + forms); the
    operator's untracked files elsewhere in ~/its are never touched."""
    _git("checkout", "--", "safety_portal/catalog.json")
    # forms/ holds only tracked shipped forms + the meta-schema, so any UNTRACKED file here is
    # a stray from an interrupted cycle — clean it so it can't ride into the next commit.
    _git("clean", "-fd", "safety_portal/forms")
    _git("checkout", "main")
    _git("pull", "--ff-only", "origin", "main")


def _unstrand_if_needed() -> None:
    """Resilience: recover an IDLE-stranded tree at the top of every cycle.

    `_actuate`'s Stage-0 `_reset_to_main` only runs when a request is CLAIMED, so a daemon
    that fails a publish and then finds nothing to actuate leaves `~/its` on the leftover
    `publish/req-*` branch INDEFINITELY (the "self-heal" never fires because no later publish
    comes). This recovers without waiting for one.

    Lighter than a blind per-cycle `_reset_to_main`: when HEAD is already on `main` (the
    common idle case) this is a single `rev-parse` with NO network pull; only the genuinely-
    stranded case pays the full reset. The full pull-to-current still happens in `_actuate`
    when a request is actually claimed."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != "main":
        _reset_to_main()


_CI_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}

# Log lines worth surfacing as the failure reason (the first match in a job's failing-step
# log). Ordered widest-first; we only need ONE actionable line per check.
_LOG_SIGNAL_RE = re.compile(
    r"(AssertionError|Error:|FAILED|expected .+ to be|✗|×|##\[error\])", re.IGNORECASE
)

# A check-run that is still running (Actions uses `status`, legacy commit statuses `state`).
_LIVE_CHECK_STATUSES = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"}


def _check_name(check: dict) -> str:
    """A check's stable name — Actions check-runs carry `name`, legacy statuses `context`."""
    return str(check.get("name") or check.get("context") or "check")


def _conclusion(check: dict) -> str:
    """A finished check's outcome, upper-cased ("" while it is still running)."""
    return str(check.get("conclusion") or "").upper()


def _check_status(check: dict) -> str:
    """A check's run state, upper-cased (Actions uses `status`, legacy statuses `state`)."""
    return str(check.get("status") or check.get("state") or "").upper()


def _supersedes_a_cancellation(other: dict) -> bool:
    """True for a run that makes an older same-named CANCELLED entry irrelevant — it is
    either still going or has already come back green."""
    return _check_status(other) in _LIVE_CHECK_STATUSES or _conclusion(other) == "SUCCESS"


def _is_superseded_cancellation(check: dict, rollup: list[dict]) -> bool:
    """True for a CANCELLED check-run that a LIVE (or already-SUCCEEDED) run of the SAME
    name supersedes — i.e. GitHub's concurrency cancel, not a real CI failure.

    `.github/workflows/ci.yml` sets `concurrency: ci-${{ github.ref }}` with
    `cancel-in-progress: true`, and this daemon's own push-then-`pr create` sequence
    routinely lands two runs on one ref, so GitHub cancels the older one and its jobs sit in
    the PR's rollup as CANCELLED beside the live ones. Counting those as failure aborts a
    HEALTHY publish on the first poll (CI_POLL_S) while the real run still has minutes to
    run, and reports bare job names with no detail — a cancelled job has no failing step for
    `_check_failure_detail` to quote, so the reason degrades to e.g. "test; portal; secrets"
    and points the operator at three jobs that never actually failed.

    A cancellation with NO successor still fails CLOSED: an operator cancelling a run, or a
    whole workflow cancelled outright, must not read as green.
    """
    if _conclusion(check) != "CANCELLED":
        return False
    name = _check_name(check)
    return any(
        _supersedes_a_cancellation(other)
        for other in rollup
        if other is not check and _check_name(other) == name
    )


def _dedupe_checks(checks: list[dict]) -> list[dict]:
    """One entry per check NAME. CI double-fires on push + pull_request, so a single failing
    job appears twice — surfacing 'portal, portal' is noise. Preserves first-seen order."""
    seen: set[str] = set()
    out: list[dict] = []
    for c in checks:
        name = _check_name(c)
        if name not in seen:
            seen.add(name)
            out.append(c)
    return out


def _check_failure_detail(check: dict) -> str:
    """A one-line actionable excerpt for ONE failing check — the first signal line of its
    failing job's log (e.g. 'expected 11 to be 10'), so the request's failure_reason (and
    the editor's 'Edit & re-publish') shows the REAL reason, not a bare job name. Best-
    effort: any error falls back to the bare name so a detail-fetch failure never masks the
    CI-failure signal."""
    name = _check_name(check)
    m = re.search(r"/job/(\d+)", str(check.get("detailsUrl") or ""))
    if not m:
        return name
    try:
        log = _gh("run", "view", "--job", m.group(1), "--log-failed")
    except Exception:  # noqa: BLE001 — the detail is a bonus, never load-bearing
        return name
    for line in log.splitlines():
        if _LOG_SIGNAL_RE.search(line):
            msg = line.split("\t")[-1].strip()          # drop gh's 'job\tstep\t' prefix
            msg = re.sub(r"^\S+Z\s+", "", msg)           # drop a leading ISO-8601 timestamp
            return f"{name}: {msg[:160]}"
    return name


def _ci_failure_reason(bad: list[dict]) -> str:
    """De-duped, per-check detailed reason for a set of failing checks (D2)."""
    return "; ".join(_check_failure_detail(c) for c in _dedupe_checks(bad))


def _wait_for_ci(branch: str) -> None:
    """Poll the branch's PR until CI is green (mergeStateStatus == CLEAN), then return. Raises
    on a failing required check or a timeout. This REPLACES `gh pr merge --auto` (which needs
    the repo's auto-merge setting AND merges ASYNCHRONOUSLY, which broke the deploy ordering):
    the daemon waits for the C12 render-smoke gate itself, then merges synchronously. Polls
    mergeStateStatus (NOT `gh pr checks --watch` — a check can stick IN_PROGRESS while the PR
    is already mergeable)."""
    deadline = time.monotonic() + CI_TIMEOUT_S
    while time.monotonic() < deadline:
        data = json.loads(_gh("pr", "view", branch, "--json", "mergeStateStatus,statusCheckRollup"))
        if data.get("mergeStateStatus") == "CLEAN":
            return
        rollup = data.get("statusCheckRollup") or []
        bad = [
            c for c in rollup
            if _conclusion(c) in _CI_FAIL_CONCLUSIONS
            and not _is_superseded_cancellation(c, rollup)
        ]
        if bad:
            raise RuntimeError(f"CI failed for {branch}: {_ci_failure_reason(bad)}")
        if data.get("mergeStateStatus") == "BEHIND":
            _gh("pr", "update-branch", branch)
        time.sleep(CI_POLL_S)
    raise RuntimeError(f"CI did not pass for {branch} within {int(CI_TIMEOUT_S)}s")


def _commit_test_merge(request_id: int, identity: str, note: str) -> None:
    """Commit the worktree change on a per-request branch, open a PR, WAIT for CI (the
    3-renderer render-smoke gate, C12), then MERGE on green — branch-protection-respecting,
    no repo auto-merge needed. Raises if CI red / merge blocked (the form does NOT go live)."""
    branch = f"publish/req-{request_id}-{identity}"
    # Idempotent: clear a stale local/remote branch from a prior failed run of this request.
    subprocess.run(["git", "-C", str(_ROOT), "branch", "-D", branch], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(_ROOT), "push", "origin", "--delete", branch],
                   capture_output=True, text=True)
    _git("checkout", "-b", branch)
    _git("add", "safety_portal/catalog.json", "safety_portal/forms")
    # Defensive: a no-op apply (manifest already in the target state — e.g. retiring an
    # already-retired form, or a rollback to the current version) stages nothing, and an
    # unconditional `git commit` then exits 1 with a confusing "nothing added to commit /
    # untracked files present" message. Surface a clean reason instead. (apply_publish
    # rejects the common already-retired case earlier at validate; this is the backstop.)
    if subprocess.run(["git", "-C", str(_ROOT), "diff", "--cached", "--quiet"]).returncode == 0:
        raise RuntimeError("no catalog/forms change to publish (manifest already in target state)")
    _git("commit", "-m", f"chore(safety-portal): publish {note} (req {request_id})")
    _git("push", "-u", "origin", branch)
    _gh("pr", "create", "--fill", "--head", branch)
    _wait_for_ci(branch)
    _gh("pr", "merge", branch, "--squash", "--delete-branch")


def _pending_migrations() -> list[str]:
    """On-disk D1 migrations (safety_portal/migrations/*.sql) NOT yet applied to the REMOTE
    database — the deploy gate's evidence (Slice 1, R3-F1 / forensic class #2, publish #434).

    Reuses the deploy stage's exact invocation surface: same cwd (safety_portal/), the LOCAL
    wrangler via npx (`npm run deploy` resolves the same node_modules/.bin binary), under the
    operator's Cloudflare auth — the daemon already deploys, so the credential is present.
    `wrangler d1 migrations list … --remote` prints ONLY unapplied migrations, so
    cross-checking the on-disk filenames against its output is robust to its table-format
    drift (we only look for our own filenames). Raises on any wrangler failure — callers
    fail CLOSED: cannot verify ⇒ must not deploy."""
    disk = sorted(p.name for p in _MIGRATIONS_DIR.glob("*.sql"))
    out = subprocess.run(
        ["npx", "wrangler", "d1", "migrations", "list", D1_DATABASE_NAME, "--remote"],
        cwd=_ROOT / "safety_portal", check=True, capture_output=True, text=True,
    ).stdout
    return [name for name in disk if name in out]


def _deploy_land_health(creds: _Creds, current_form_code: str) -> None:
    """The merge has landed on main → land it locally (fast-forward ~/its so load_definition
    sees the new file), deploy the Worker/SPA via the operator's LOCAL wrangler (the CF
    credential never leaves the Mac), then a post-deploy liveness check. Raises on failure.

    Refuses to deploy ahead of unapplied remote D1 migrations (Slice 1, R3-F1): the check
    runs AFTER the pull (so the on-disk migration set is current main's — new migrations can
    arrive WITH the pull, exactly forensic class #2) and BEFORE `wrangler deploy`. Refusal
    only, never auto-apply; the raise is fenced by _actuate's stage-3 handler (stamps
    failed('live') + CRITICAL), so a refused deploy never wedges the daemon."""
    _git("checkout", "main")
    _git("pull", "--ff-only", "origin", "main")
    pending = _pending_migrations()
    if pending:
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"deploy REFUSED: {len(pending)} unapplied remote D1 migration(s) {pending} — "
            f"apply them first (cd ~/its && git pull origin main && cd safety_portal && "
            f"npx wrangler d1 migrations apply {D1_DATABASE_NAME} --remote), then re-run the "
            f"deploy (npm run deploy) or re-publish. Deploying the Worker ahead of its "
            f"migrations 500s the live portal (forensic class #2; publish #434).",
            error_code=ERR_PENDING_MIGRATIONS,
        )
        raise PendingMigrationsError(f"unapplied remote D1 migrations: {', '.join(pending)}")
    subprocess.run(["npm", "run", "deploy"], cwd=_ROOT / "safety_portal",
                   check=True, capture_output=True, text=True)
    portal_client.get_publish_pending(creds.base_url, creds.bearer, limit=1)  # liveness ping


def _regenerate_archive() -> None:
    """Regenerate the Box blank-form archive (the DR storage of record) so it reflects the
    new active set. Raises on failure.

    Uses sys.executable (the venv interpreter already running this daemon), NOT a bare
    "python" — launchd's minimal PATH has no `python` (macOS ships only `python3`, and the
    real interpreter is ~/its/.venv/bin/python), so a bare "python" raised
    FileNotFoundError and failed every publish at the `archived` stage AFTER it had already
    gone live. sys.executable also guarantees the same venv (with boxsdk/smartsheet deps).

    Renders into a throwaway tempdir (`--out-dir`) instead of the default `form_archive_out/`
    under `~/its` — the daemon runs on the live tree, and the on-disk mirror is a throwaway
    (the Box upload consumes the in-memory render, not the local copy). Cleaned up in `finally`."""
    out_dir = tempfile.mkdtemp(prefix="its_form_archive_")
    try:
        subprocess.run(
            [sys.executable, "-m", "scripts.generate_form_archive", "--upload", "--out-dir", out_dir],
            cwd=_ROOT, check=True, capture_output=True, text=True,
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ── orchestration ─────────────────────────────────────────────────────────────────────


def _stamp(creds: _Creds, request_id: int, status: str) -> None:
    portal_client.stamp_publish(creds.base_url, creds.bearer, request_id=request_id, status=status)


def _fail(creds: _Creds, request_id: int, stage: str, reason: str) -> None:
    """Terminal failure: stamp failed(stage, reason) + an operator CRITICAL (detect-and-
    alert, C12). Both best-effort — a stamp/log failure must not mask the original error."""
    # §54 backstop (CE-1 parity with config_actuator._fail): `reason` can carry a raw
    # git/gh/wrangler stderr tail (see `_exc_reason`), and the stamp_publish leg lands
    # `failure_reason` on the portal Status Monitor — a sink that BYPASSES error_log's redact
    # choke point. Redact here so neither the stamp nor the CRITICAL egresses an accidental
    # token/PII. (redact() is idempotent, so the error_log leg re-redacting is harmless.)
    reason = redact(reason[:1800])
    try:
        portal_client.stamp_publish(
            creds.base_url, creds.bearer, request_id=request_id,
            status="failed", failed_stage=stage, failure_reason=reason,
        )
    except Exception:  # noqa: BLE001
        pass
    error_log.log(
        Severity.CRITICAL, SCRIPT_NAME,
        f"publish request {request_id} FAILED at stage {stage!r}: {reason}",
        error_code=f"publish_daemon.failed.{stage}",
    )


def _actuate(creds: _Creds, request: dict[str, Any], stats: PublishStats) -> None:
    """Run the publish pipeline for ONE claimed request, stamping each milestone. Every
    stage is fenced: a failure stamps failed(stage) + CRITICAL and returns (never raises
    out — one bad request must not wedge the cycle)."""
    request_id = request["id"]
    op = request["op"]
    identity = request["identity"]
    parent = request["parent_form_code"]
    target = request.get("target_form_code")
    category = request.get("category")  # set only for create(new-parent) + recategorize

    # Stage 0 — sync to a clean, current main (recover from an interrupted prior cycle) so the
    # catalog re-check + the commit start from live HEAD.
    try:
        _reset_to_main()
    except Exception as exc:  # noqa: BLE001
        _fail(creds, request_id, "validated", f"could not sync to main: {_exc_reason(exc)}")
        stats.failed += 1
        return

    # Stage 1 — re-validate against live HEAD (C3) → validated.
    try:
        definition = None
        if op in _DEFINITION_OPS:
            definition = json.loads(request["definition_json"])
            _validate_definition(definition)
        new_manifest, files, note = apply_publish(
            _load_catalog(), op=op, identity=identity, parent_form_code=parent,
            target_form_code=target, definition=definition,
            required_content=_load_required_content(),
            category=category, valid_categories=form_category.workflow_ids(),
        )
        _stamp(creds, request_id, "validated")
    except (PublishApplyError, jsonschema.ValidationError, json.JSONDecodeError, KeyError) as exc:
        _fail(creds, request_id, "validated", f"{type(exc).__name__}: {exc}")
        stats.failed += 1
        return

    # Stage 2 — commit + CI render-smoke gate + merge → tested.
    try:
        _apply_to_worktree(new_manifest, files)
        _commit_test_merge(request_id, identity, note)
        _stamp(creds, request_id, "tested")
    except Exception as exc:  # noqa: BLE001 — any actuation failure is terminal+alerted
        _fail(creds, request_id, "tested", _exc_reason(exc))
        stats.failed += 1
        return

    # Stage 3 — deploy + land + health check → live.
    try:
        current_code = next(iter(files), None) or identity
        _deploy_land_health(creds, current_code)
        _stamp(creds, request_id, "live")
    except Exception as exc:  # noqa: BLE001
        _fail(creds, request_id, "live", _exc_reason(exc))
        stats.failed += 1
        return

    # Stage 4 — Box archive → archived (terminal success).
    try:
        _regenerate_archive()
        _stamp(creds, request_id, "archived")
    except Exception as exc:  # noqa: BLE001
        _fail(creds, request_id, "archived", _exc_reason(exc))
        stats.failed += 1
        return

    stats.actuated += 1
    stats.notes.append(note)


def _exc_reason(exc: Exception) -> str:
    """A bounded reason string; for a subprocess failure, surface its stderr tail."""
    if isinstance(exc, subprocess.CalledProcessError):
        tail = (exc.stderr or exc.stdout or "")[-600:]
        return f"{exc.cmd[0] if exc.cmd else 'cmd'} exit {exc.returncode}: {tail}"
    return f"{type(exc).__name__}: {exc}"


def _sweep_stale_rows(creds: _Creds, stats: PublishStats) -> None:
    """Reclaim non-terminal publish rows stalled past STALE_RECLAIM_S (a daemon that
    claimed-then-died, or a wedged stage): stamp failed('stale_reclaimed') + a CRITICAL once per
    row, so the parent is unwedged (the Worker's C8 in-flight check) and the original death is
    surfaced. Best-effort — a sweep failure logs + returns; it never blocks the cycle's real
    work. The Worker's stamp guard accepts non-terminal → failed, so this can't revert a
    terminal row. (PR-2 — makes the migration-0010 / index.ts 'stuck row is reclaimed' note true.)"""
    try:
        stuck = portal_client.get_publish_stuck(
            creds.base_url, creds.bearer, older_than=int(STALE_RECLAIM_S)
        )
    except Exception as exc:  # noqa: BLE001 — housekeeping; never wedge the cycle
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"publish stale-row sweep could not fetch stuck rows: {_exc_reason(exc)}",
            error_code="publish_daemon.sweep_fetch_failed",
        )
        return
    for row in stuck:
        rid = row.get("id")
        if not isinstance(rid, int):
            continue
        was = row.get("status")
        parent = row.get("parent_form_code")
        reason = (
            f"stale_reclaimed: non-terminal status {was!r} stalled > {int(STALE_RECLAIM_S)}s "
            f"(lease_owner={row.get('lease_owner')}); the publish daemon likely died mid-actuation. "
            f"Parent {parent!r} is now unwedged — re-publish if still needed."
        )
        try:
            portal_client.stamp_publish(
                creds.base_url, creds.bearer, request_id=rid,
                status="failed", failed_stage="stale_reclaimed", failure_reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"publish stale-row sweep could not stamp row {rid} failed: {_exc_reason(exc)}",
                error_code="publish_daemon.sweep_stamp_failed",
            )
            continue
        stats.reclaimed += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"publish request {rid} reclaimed as STALE (was {was!r}, parent {parent!r}) — the "
            f"daemon likely died mid-publish; the parent is now unwedged. {reason}",
            error_code="publish_daemon.stale_reclaimed",
        )


@its_error_log(script_name=SCRIPT_NAME)
@require_active
def publish_once() -> PublishStats:
    """One actuation cycle: gate → creds → pull → claim → actuate each. Single-shot
    (launchd handles cadence). Serial: one request fully actuated before the next (the
    deploy mutates shared state). The Worker's per-parent serialization (C8) already
    prevents two in-flight publishes for one form."""
    # #336 startup observability (after @require_active, fail-open). Additive (§14).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    stats = PublishStats()
    try:
        enabled = _polling_enabled()
    except Exception as exc:  # noqa: BLE001 — classified by the fence; non-transient re-raises
        if not _CONFIG_READ_FENCE.handle(exc):
            raise
        _halt_transient(stats, "reading the polling gate")
        return stats
    # The gate read SUCCEEDED — clear the ladder here, not after the creds branch. Resetting
    # only on the full-success path left a stale count behind every `polling_disabled` /
    # `unstrand_failed` return, so a counter that reached 4 before the operator paused
    # polling would fire CRITICAL on the very first transient after resume.
    _CONFIG_READ_FENCE.reset()
    if not enabled:
        stats.halted = "polling_disabled"
        return stats
    # Recover an idle-stranded tree BEFORE doing anything else this cycle (after the kill-
    # switch + polling gate, so a PAUSED/disabled daemon never mutates the tree). A recovery
    # failure is loud + halts the cycle — we cannot safely actuate from a stranded tree.
    try:
        _unstrand_if_needed()
    except Exception as exc:  # noqa: BLE001
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"publish daemon could not recover a stranded tree to main: {_exc_reason(exc)}",
            error_code="publish_daemon.unstrand_failed",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="halted: could not recover stranded tree to main")
        stats.halted = "unstrand_failed"
        return stats
    try:
        creds = _resolve_creds()
    except Exception as exc:  # noqa: BLE001 — classified by the fence; non-transient re-raises
        # read_base_url already absorbs the transient classes into its sentinel and
        # re-raises only auth/permission, so this arm is the defensive one.
        if not _CONFIG_READ_FENCE.handle(exc):
            raise
        _halt_transient(stats, "reading the Worker base URL")
        return stats
    if isinstance(creds, TransientUnavailable):
        # RE-CLASSIFY, do not inherit. `read_base_url`'s "transient" bucket is deliberately
        # BROAD for the five portal pullers — it swallows 429 and any bodied SmartsheetError
        # too. This daemon's own reader never did: it caught only NotFound + CircuitOpen, so
        # a 429/400 here propagated to an immediate CRITICAL. Routing the read through the
        # shared sentinel must not SOFTEN that by accident, so anything that is not
        # circuit-open and not the precisely-typed transient class is re-raised unchanged.
        if (
            not creds.circuit_open
            and creds.exc is not None
            and not smartsheet_client.is_transient_error(creds.exc)
        ):
            raise creds.exc
        # Smartsheet was TEMPORARILY unreachable when reading the Worker base URL — NOT a
        # misconfig, and it self-heals next cycle. WARN + skip; do NOT report unresolved
        # CREDENTIALS (that aims the §43 repair at re-provisioning secrets — a
        # high-capability-class action — for a condition needing none). Skip the watchdog
        # freshness marker exactly like the no-creds path, so a SUSTAINED outage still
        # surfaces via the Check-C staleness floor, just without per-cycle alert spam.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"publish Worker base URL temporarily unreadable ({creds.reason}) — skipping "
            f"this cycle; will retry next interval (transient, self-heals)",
            error_code="publish_daemon.creds_transient",
        )
        # The fence ALSO counts it, so a sustained outage escalates ERROR→CRITICAL on the
        # ladder instead of staying a per-cycle WARN nobody reads. Counted unless the breaker
        # is open, in which case the fence routes the observation to the shared FLEET counter
        # instead of this daemon's.
        _CONFIG_READ_FENCE.note_transient(
            f"could not read the Worker base URL ({creds.reason})",
            count=not creds.circuit_open,
        )
        _write_heartbeat()
        _write_heartbeat_row(status="WARN", items_processed=0,
                             error_summary=f"base URL unreadable ({creds.reason}) — transient")
        stats.halted = "creds_transient"
        return stats
    if creds is None:
        # Fail-closed + loud: a missing bearer/URL must not silently drop publishes.
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            "publish daemon halted: missing Worker base URL or internal bearer (fail-closed)",
            error_code="publish_daemon.creds_unresolved",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="halted: missing Worker base URL or internal bearer")
        stats.halted = "creds_unresolved"
        return stats

    # PR-2: reclaim stale non-terminal rows (crashed/stalled publishes) BEFORE pulling new work,
    # so a wedged parent is freed this cycle. Best-effort; never blocks the pull.
    _sweep_stale_rows(creds, stats)

    rows = portal_client.get_publish_pending(creds.base_url, creds.bearer)
    stats.polled = len(rows)
    if rows:
        # Pre-claim deploy gate (Slice 1, R3-F1 / forensic class #2, publish #434): if the
        # remote D1 is missing on-disk migrations, actuating ANY publish would end in a
        # refused deploy at stage 'live' — so refuse the whole cycle BEFORE claiming. The
        # rows stay `pending` on the Worker, the next launchd cycle retries, and the
        # operator's `migrations apply` unblocks publishing automatically (no re-publish
        # needed, no lease burned, no terminal-failed row). Checked only when there is work
        # (an idle cycle never deploys, so it never shells out to wrangler). Fail-CLOSED on
        # a check failure: cannot verify ⇒ must not deploy. The authoritative post-pull
        # re-check lives in _deploy_land_health — this pre-claim copy reads the CURRENT
        # tree, which can be behind the pull _actuate performs mid-cycle.
        try:
            pending = _pending_migrations()
        except Exception as exc:  # noqa: BLE001 — any check failure is a fail-closed halt
            error_log.log(
                # CRITICAL, not ERROR (ops review): a sustained check failure (expired CF auth,
                # network fault) blocks EVERY publish indefinitely — ERROR never pages and the
                # watchdog only surfaces CRITICAL, so this would be a silent stall. The Resend-leg
                # alert_dedupe on (script, error_code) bounds the every-120s repetition to one page
                # per window; the §43 runbook's both-codes-page promise is now true.
                Severity.CRITICAL, SCRIPT_NAME,
                "publish halted: could not verify remote D1 migration state (fail-closed, "
                f"retries next cycle): {_exc_reason(exc)}",
                error_code=ERR_MIGRATION_CHECK,
            )
            _write_heartbeat()
            _write_heartbeat_row(status="ERROR", items_processed=0,
                                 error_summary="halted: could not verify remote D1 migration state")
            stats.halted = "migration_check_failed"
            return stats
        if pending:
            error_log.log(
                Severity.CRITICAL, SCRIPT_NAME,
                f"publish halted: {len(pending)} unapplied remote D1 migration(s) {pending} "
                f"block the deploy stage — apply them (cd ~/its && git pull origin main && "
                f"cd safety_portal && npx wrangler d1 migrations apply {D1_DATABASE_NAME} "
                f"--remote). The {stats.polled} pending publish request(s) stay queued and "
                f"retry next cycle automatically. Never deploy the Worker ahead of its "
                f"migrations (forensic class #2; publish #434).",
                error_code=ERR_PENDING_MIGRATIONS,
            )
            # WARN, not ERROR: a deliberate, bounded refusal (the portal_poll
            # halted_transient precedent) — rows stay queued; the operator's
            # `migrations apply` unblocks the next cycle automatically.
            _write_heartbeat()
            _write_heartbeat_row(
                status="WARN", items_processed=0,
                error_summary=f"deploy blocked: {len(pending)} unapplied remote D1 migration(s)",
            )
            stats.halted = "pending_migrations"
            return stats
    owner = _lease_owner()
    for row in rows:
        request_id = row.get("id")
        if not isinstance(request_id, int):
            continue
        claimed = portal_client.claim_publish(
            creds.base_url, creds.bearer, request_id=request_id, lease_owner=owner
        )
        if claimed is None:
            stats.skipped_unclaimed += 1
            continue
        _actuate(creds, claimed, stats)

    _write_heartbeat()
    if stats.failed > 0:
        cycle_status: HeartbeatStatus = "DEGRADED"
    elif stats.reclaimed > 0:
        cycle_status = "WARN"
    else:
        cycle_status = "OK"
    if circuit_breaker.is_open():
        cycle_status = "CIRCUIT_OPEN"

    try:
        _write_heartbeat_row(
            status=cycle_status,
            items_processed=stats.actuated,
            error_summary=(
                None
                if stats.failed == 0 and stats.reclaimed == 0
                else f"failed={stats.failed} reclaimed={stats.reclaimed}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — heartbeat must never block
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"heartbeat write outer-catch tripped: {exc!r}",
            error_code="daemon_health_write_failed",
        )
    # Check-C liveness: written ONLY here, at the end of a cycle that actually reached the
    # pull+claim work. Every `halted` early-return above (gate off, stranded tree, transient
    # / absent creds, migration gate) deliberately leaves the marker untouched — a cycle that
    # never polled must let Check C go stale rather than fake freshness (po_poll's semantics).
    _write_watchdog_marker()
    _CONFIG_READ_FENCE.flush_retry_recovery()
    return stats


if __name__ == "__main__":  # pragma: no cover
    publish_once()
