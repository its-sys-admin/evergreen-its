"""Mac-side config-editor actuator (§50 config editor, slice 2) — the SOLE privileged
config actuator.

Mirrors ``safety_reports/publish_daemon.py`` one-for-one against the config_requests
queue. The cloud Worker (``worker/config.ts``, slice 1) can only ENQUEUE a config request
(send-free); this launchd daemon is the trusted Mac side that actuates it — the privileged
commit/deploy capability lives on the Mac (with the operator's git + wrangler auth), never
on the cloud (the External Send Gate posture). Per claimed request it runs the publish-style
pipeline, stamping the config_requests state machine at each milestone:

    pull GET /api/internal/config/pending  (portal_client, bearer-gated)
      → atomically CLAIM one (portal_client.claim_config — lease; concurrent runs skip)
      → re-validate + WRITE vs LIVE git HEAD (config_apply.apply_config, C3)   → STAMP validated
      → commit the config file(s) on a per-request branch, open a PR, wait for CI,
        MERGE on green                                                          → STAMP tested
      → deploy via the operator's LOCAL wrangler + fast-forward the live ~/its tree +
        post-deploy health check (GET /config/pending)                          → STAMP live
      → (no archive analogue — the Worker BUNDLES the config at build time, so the
        `npm run deploy` above already re-bundled it) no-op terminal              → STAMP archived
    ANY stage failure → STAMP failed(stage, reason) + an operator CRITICAL triple-fire.

Why a deploy at all: the Worker imports purchaser.json / tax.json / terms/manifest.json at
BUILD time (worker/po.ts) — an edit's new values are STALE in the live Worker until a
redeploy re-bundles them. So the actuator MUST run the full commit → CI → merge → deploy
pipeline, exactly like publish_daemon. There is no Box "archive" analogue → the ``archived``
stamp is a no-op terminal (the Worker's terminal state, cloned from publish, is
``archived|failed``; reaching it releases the per-(workstream,artifact) in-flight lock).

Deploy gate (mirrors publish_daemon / forensic class #2): the daemon REFUSES to deploy the
Worker ahead of unapplied remote D1 migrations. Checked pre-claim each cycle with work (rows
stay pending → the operator's `migrations apply` unblocks the next cycle automatically) AND
authoritatively post-pull in _deploy_land_health. Refusal only — a live D1 apply is
operator-gated.

Capability gating (Invariant 1): enrolled in tests/test_capability_gating.py GATED_SCRIPTS —
it actuates code (commit/deploy) but performs ZERO external customer transmission and no LLM
step. Its HTTP egress to OUR Worker is shared/portal_client.py (F02-allowlisted); the
git/gh/wrangler/npm ops are subprocess to the operator's own toolchain.

launchd: `config_once()` is the public API; `__main__` calls it once and exits (StartInterval
handles cadence). HIGH-CAPABILITY + operator-gated activation — see the §43 runbook
docs/runbooks/config_actuator.md.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from po_materials import config_apply
from shared import (
    actuator_branches,
circuit_breaker,
    creds_resolution,
    error_log,
    keychain,
    portal_client,
    smartsheet_client,
)
from shared.creds_resolution import TransientUnavailable
from shared.error_log import Severity, its_error_log
from shared.heartbeat import HeartbeatReporter, HeartbeatStatus
from shared.kill_switch import require_active
from shared.redact import redact
from shared.required_config import ConfigKey, resolve_and_log

SCRIPT_NAME = "config_actuator"
WORKSTREAM = "po_materials"

# Creds (fail-closed). The config API is a SEPARATE privilege tier from the internal token:
# KC_BEARER is the Keychain mirror of the Worker's PORTAL_CONFIG_API_TOKEN (privilege-
# separated). The Worker base URL is the ONE shared portal Worker (the PO config editor
# reuses safety_reports.portal.worker_base_url — same key po_poll reads).
KC_BEARER = "ITS_PORTAL_CONFIG_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"
CFG_POLLING_ENABLED = "po_materials.config_actuator.polling_enabled"

# #336 — every ITS_Config key this daemon resolves at RUNTIME, declared for the startup
# observability pass (resolve_and_log). polling_enabled ships OFF (default False); the shared
# Worker base-URL reads to "" when unset.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_POLLING_ENABLED, WORKSTREAM, False, "bool"),
    ConfigKey(CFG_WORKER_BASE_URL, WORKSTREAM, "", "str"),
]

# ITS_Daemon_Health heartbeat. HEARTBEAT_ROW_STATE_PATH is SHARED with the other daemons —
# same JSON file, different daemon_name key (ARCH-2). POLL_INTERVAL_SECONDS mirrors the plist
# StartInterval (120s; config edits are infrequent + the cycle is heavy).
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "config_actuator_heartbeat.txt"
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"
DAEMON_NAME = "po_materials.config_actuator"
POLL_INTERVAL_SECONDS = 120

# A1 self-provision metadata (the ONLY per-daemon difference in the heartbeat helpers).
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/internal/config/pending"

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
# silently dead §50 actuator is invisible — privileged code-actuation simply stops and the
# portal Status Monitor just looks idle, indistinguishable from "nothing is queued".
WATCHDOG_MARKER_DIR = Path.home() / "its" / ".watchdog"
WATCHDOG_JOB_SLUG = "config_actuator"

_ROOT = Path(__file__).resolve().parent.parent
# The daemon-managed worktree paths (discarded in _reset_to_main, staged in _commit_test_merge).
_MANAGED_PATHS = (
    "po_materials/config", "po_materials/terms",
    "subcontracts/config", "subcontracts/terms",  # SC-S2: the config editor is workstream-generic
    "subcontracts/exhibit",  # PR-B2: the versioned per-trade Exhibit A Article II templates
)

# CI poll cadence for the config merge (auto-merge is DISABLED — the daemon waits for CI then
# merges synchronously, bounded so one stuck CI run can't wedge the daemon).
CI_POLL_S = 20.0
CI_TIMEOUT_S = 900.0  # 15 min

# Stale-row reclaim: a non-terminal config_requests row whose updated_at is older than this is
# swept to failed('stale_reclaimed') at the top of each cycle. MUST exceed CI_TIMEOUT_S + deploy
# slack + the Worker's LEASE_TTL_S(1800) so a legitimately in-progress config publish is never
# reclaimed. 3300s (55 min) keeps the strict inequality 3300 > 900 + 1800.
STALE_RECLAIM_S = 3300.0

# D1 pending-migrations deploy gate (forensic class #2). Same DB + migrations dir as
# publish_daemon — the PO config editor shares the ONE Safety Portal Worker/D1.
D1_DATABASE_NAME = "its-safety-portal-db"  # wrangler.jsonc d1_databases[0].database_name
_MIGRATIONS_DIR = _ROOT / "safety_portal" / "migrations"
ERR_PENDING_MIGRATIONS = "config_actuator.deploy_blocked_pending_migrations"
ERR_MIGRATION_CHECK = "config_actuator.migration_check_failed"


class PendingMigrationsError(RuntimeError):
    """A deploy would ship the Worker ahead of unapplied remote D1 migrations (refused)."""


@dataclass
class ConfigStats:
    """Summary of one config_once() invocation (for tests + logging)."""

    polled: int = 0
    actuated: int = 0
    failed: int = 0
    skipped_unclaimed: int = 0
    reclaimed: int = 0
    orphans_cleaned: int = 0
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
    """ITS_Config read, fail-soft to `fallback` (mirrors publish_daemon's reader).

    A MISSING row and an OPEN breaker are EXPECTED, quiet fallbacks: a missing declared
    key is already WARNed loudly at startup by ``resolve_and_log`` (this key is in
    REQUIRED_CONFIG), and the breaker logs when it opens. Any OTHER ``SmartsheetError``
    (a transient timeout / auth failure / rate-limit — e.g. the 2026-07-14 fleet-wide
    token flap) is a real hiccup: WARN once with a distinct code and fall back for THIS
    cycle, rather than letting it escape ``_polling_enabled`` (the first per-cycle caller,
    before ``@its_error_log``) and page as an ``unhandled`` CRITICAL. Mirrors
    ``required_config.resolve_and_log``'s transient branch (alert-hygiene)."""
    try:
        raw = smartsheet_client.get_setting(key, workstream=WORKSTREAM)
    except (smartsheet_client.SmartsheetNotFoundError, smartsheet_client.SmartsheetCircuitOpenError):
        return fallback
    except smartsheet_client.SmartsheetError as exc:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"ITS_Config read of {key} [{WORKSTREAM}] failed transiently — using fallback "
            f"{fallback!r} this cycle (fail-soft, retries next cycle): {type(exc).__name__}",
            error_code="config_actuator.config_read_error",
        )
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
    which is exactly how a network hiccup produced an alert blaming config-actuator
    *credentials* that were never missing (the live po_poll bug, 2026-07-20 04:42Z).
    Mirrors po_poll._resolve_credentials.

    NOTE the workstream scope is this daemon's own WORKSTREAM ("po_materials"), preserved
    byte-for-byte from the `_read_str_setting` call this replaced — po_poll reads the SAME
    key under an explicit `CFG_WORKER_BASE_URL_WORKSTREAM = "safety_reports"`. That
    divergence predates this change and is deliberately NOT altered here: a missing row
    still resolves to None and halts loudly exactly as it does today.
    """
    resolved = creds_resolution.read_base_url(CFG_WORKER_BASE_URL, WORKSTREAM)
    if isinstance(resolved, TransientUnavailable):
        return resolved
    base_url = (resolved or "").strip()
    if not base_url:
        return None
    try:
        bearer = keychain.get_secret(KC_BEARER)
    except Exception as exc:  # noqa: BLE001 — any keychain failure is a fail-closed halt
        # Still fail closed, but say WHICH failure. The caller's `creds_unresolved` reads
        # "missing Worker base URL or config bearer", which aims §43 remediation at
        # re-provisioning a secret that may well exist and simply could not be READ.
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"config bearer Keychain read FAILED (the secret is not necessarily absent): {exc!r}",
            error_code="config_actuator.keychain_read_failed",
        )
        return None
    if not bearer:
        return None
    return _Creds(base_url=base_url, bearer=bearer)


def _write_heartbeat() -> None:
    """Liveness file touch — thin delegator to the shared HeartbeatReporter (the canonical
    test mock seam; the suite patches this exact symbol). See shared/heartbeat.py (§42)."""
    _heartbeat_reporter.write_liveness()


def _write_heartbeat_row(
    *,
    status: HeartbeatStatus,
    items_processed: int,
    error_summary: str | None = None,
    correlation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """ITS_Daemon_Health per-cycle row update — thin delegator to the shared HeartbeatReporter
    (the canonical test mock seam). See shared/heartbeat.py (§42)."""
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


# ── privileged ops (subprocess to the operator's toolchain; mocked in tests) ──────────


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_ROOT), *args], check=True, capture_output=True, text=True
    ).stdout


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], cwd=_ROOT, check=True, capture_output=True, text=True).stdout


# The managed dirs where add_version writes NEW immutable version files — every `<ws>/terms` AND the
# versioned `subcontracts/exhibit` (PR-B2). Derived so a new workstream can't reintroduce the git-clean
# gap: each must be swept of an orphaned new version file (a stray from an interrupted add_version),
# not just po_materials's terms dir.
_MANAGED_NEW_FILE_DIRS = tuple(
    p for p in _MANAGED_PATHS if p.endswith("/terms") or p.endswith("/exhibit")
)


def _reset_to_main() -> None:
    """Start each actuation from a CLEAN, current main — recover from any interrupted prior
    cycle (a leftover branch + uncommitted config/terms edits, or a stray new terms _vN.md).
    Discards ONLY the daemon-managed paths (po_materials + subcontracts config/terms); the
    operator's untracked files elsewhere in ~/its are never touched."""
    _git("checkout", "--", *_MANAGED_PATHS)
    # Each managed terms/ dir holds only the tracked manifest + shipped version files, so any
    # UNTRACKED file there is a stray new _vN.md from an interrupted add_version/create_profile —
    # clean EVERY managed terms dir so an orphan can't ride into an unrelated request's next commit
    # (§50 per-request actuation integrity; the clean must cover the same dirs `git add` sweeps).
    _git("clean", "-fd", *_MANAGED_NEW_FILE_DIRS)
    _git("checkout", "main")
    _git("pull", "--ff-only", "origin", "main")


def _unstrand_if_needed() -> None:
    """Recover an IDLE-stranded tree at the top of every cycle (a leftover config/req-* branch
    from a prior failed run that no later request came to self-heal). Lighter than a blind
    per-cycle _reset_to_main: on main (the common idle case) it's a single rev-parse with NO
    network pull; only the genuinely-stranded case pays the full reset."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != "main":
        _reset_to_main()


_CI_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}

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
    job appears twice — surfacing 'test, test' is noise. Preserves first-seen order."""
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
    failing job's log, so the request's failure_reason shows the REAL reason, not a bare job
    name. Best-effort: any error falls back to the bare name."""
    name = _check_name(check)
    m = re.search(r"/job/(\d+)", str(check.get("detailsUrl") or ""))
    if not m:
        return name
    try:
        log = _gh("run", "view", "--job", m.group(1), "--log-failed")
    except Exception:  # noqa: BLE001 — the detail is a bonus, never load-bearing
        # Say the detail is MISSING rather than returning a bare job name that reads like a
        # diagnosis. Zero new error rows; the ambiguity just disappears from the row itself.
        return f"{name} (log detail unavailable)"
    for line in log.splitlines():
        if _LOG_SIGNAL_RE.search(line):
            msg = line.split("\t")[-1].strip()          # drop gh's 'job\tstep\t' prefix
            msg = re.sub(r"^\S+Z\s+", "", msg)           # drop a leading ISO-8601 timestamp
            return f"{name}: {msg[:160]}"
    return name


def _ci_failure_reason(bad: list[dict]) -> str:
    """De-duped, per-check detailed reason for a set of failing checks."""
    return "; ".join(_check_failure_detail(c) for c in _dedupe_checks(bad))


def _wait_for_ci(branch: str) -> None:
    """Poll the branch's PR until CI is green (mergeStateStatus == CLEAN), then return. Raises
    on a failing required check or a timeout. Polls mergeStateStatus (NOT `gh pr checks --watch`
    — a check can stick IN_PROGRESS while the PR is already mergeable); replaces
    `gh pr merge --auto` (repo auto-merge is OFF + async breaks deploy ordering)."""
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


# ---- Orphan actuator-branch cleanup (a failed actuation used to leave debris) ----
# The logic lives in shared/actuator_branches.py — see that module for WHY it is shared rather
# than hand-copied into both actuators. These wrappers are deliberately thin: they are the
# canonical seams the tests mock, and they bind this daemon's own `_gh`/`_git` at CALL time so a
# patched seam is honoured.
_BRANCH_CLEANUP = actuator_branches.BranchCleanup(
    script_name=SCRIPT_NAME,
    prefix="config",
    root=_ROOT,
    runbook="docs/runbooks/config_actuator.md",
    min_age_s=actuator_branches.DEFAULT_MIN_AGE_MULTIPLIER * CI_TIMEOUT_S,
)
#: Re-exported so the age floor is readable (and assertable) from this module.
ORPHAN_MIN_AGE_S = _BRANCH_CLEANUP.min_age_s


def _branch_name(request_id: int, workstream: str, artifact: str) -> str:
    """The per-request branch name — ONE definition, so the code that CREATES the branch and the
    code that DELETES it can never disagree about which ref is meant."""
    return f"config/req-{request_id}-{workstream}-{artifact}"


def _branch_request_id(branch: str) -> int | None:
    return actuator_branches.request_id_from_branch(branch, _BRANCH_CLEANUP)


def _branch_tip_epoch(branch: str) -> float | None:
    return actuator_branches.branch_tip_epoch(branch, _gh)


def _delete_branch_refs(branch: str) -> None:
    actuator_branches.delete_branch_refs(branch, _ROOT)


def _close_and_delete_branch(branch: str, reason: str) -> bool:
    return actuator_branches.close_and_delete_branch(
        branch, reason, cfg=_BRANCH_CLEANUP, gh=_gh, git=_git,
        delete_refs=_delete_branch_refs,
    )


def _sweep_orphaned_branches(creds: _Creds, stats: ConfigStats) -> None:
    """Remove branches stranded by terminally-failed actuations (see actuator_branches)."""
    def in_flight() -> set[Any]:
        # `older_than=0` returns EVERY non-terminal row, `queued` included, so this single read is
        # the whole in-flight set. It also must not consume the pending read — the cycle's own
        # pull is asserted to happen exactly once.
        return {
            row.get("id")
            for row in portal_client.get_config_stuck(creds.base_url, creds.bearer, older_than=0)
        }

    def describe(request_id: int, hours: int) -> str:
        return (
            f"Auto-closed by the ITS config actuator: config request {request_id} is terminal "
            f"(no longer in flight) and this branch has been orphaned for ~{hours}h. Its change "
            f"is superseded by whatever later actuation did land — re-submit from the operator "
            f"dashboard if this change is still wanted."
        )

    stats.orphans_cleaned += actuator_branches.sweep_orphaned_branches(
        cfg=_BRANCH_CLEANUP, git=_git, in_flight=in_flight,
        tip_epoch=_branch_tip_epoch, close_and_delete=_close_and_delete_branch,
        describe=describe,
    )


def _commit_test_merge(request_id: int, workstream: str, artifact: str, note: str) -> None:
    """Commit the worktree change on a per-request branch, open a PR, WAIT for CI, then MERGE
    on green — branch-protection-respecting, no repo auto-merge needed. Raises if CI red /
    merge blocked (the config does NOT go live). config_apply already wrote the file(s), so
    this only stages + commits + merges."""
    branch = _branch_name(request_id, workstream, artifact)
    # Idempotent: clear a stale local/remote branch from a prior failed run of this request.
    # These bare subprocess.run calls run OUTSIDE any CC session, so the block-dangerous-git
    # hook (which blocks them interactively) does not apply — it falls open for the daemon.
    subprocess.run(["git", "-C", str(_ROOT), "branch", "-D", branch], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(_ROOT), "push", "origin", "--delete", branch],
                   capture_output=True, text=True)
    _git("checkout", "-b", branch)
    _git("add", *_MANAGED_PATHS)
    # Defensive: a no-op apply stages nothing, and an unconditional `git commit` then exits 1
    # with a confusing "nothing to commit" message. Surface a clean reason instead.
    if subprocess.run(["git", "-C", str(_ROOT), "diff", "--cached", "--quiet"]).returncode == 0:
        raise RuntimeError("no config change to publish (artifact already in target state)")
    _git("commit", "-m", f"chore(po-config): {note} (req {request_id})")
    _git("push", "-u", "origin", branch)
    _gh("pr", "create", "--fill", "--head", branch)
    _wait_for_ci(branch)
    _gh("pr", "merge", branch, "--squash", "--delete-branch")


def _pending_migrations() -> list[str]:
    """On-disk D1 migrations (safety_portal/migrations/*.sql) NOT yet applied to the REMOTE
    database — the deploy gate's evidence (forensic class #2). Same invocation surface as the
    deploy stage (cwd safety_portal/, LOCAL wrangler via npx, operator's Cloudflare auth).
    `wrangler d1 migrations list … --remote` prints ONLY unapplied migrations, so cross-
    checking on-disk filenames against its output is robust to table-format drift. Raises on
    any wrangler failure — callers fail CLOSED: cannot verify ⇒ must not deploy."""
    disk = sorted(p.name for p in _MIGRATIONS_DIR.glob("*.sql"))
    out = subprocess.run(
        ["npx", "wrangler", "d1", "migrations", "list", D1_DATABASE_NAME, "--remote"],
        cwd=_ROOT / "safety_portal", check=True, capture_output=True, text=True,
    ).stdout
    return [name for name in disk if name in out]


def _deploy_land_health(creds: _Creds) -> None:
    """The merge has landed on main → land it locally (fast-forward ~/its so the render path +
    the Worker bundle both see the new config), deploy the Worker/SPA via the operator's LOCAL
    wrangler (re-bundling the config it imports at build time), then a post-deploy liveness
    check. Raises on failure.

    Refuses to deploy ahead of unapplied remote D1 migrations: the check runs AFTER the pull
    (new migrations can arrive WITH the pull — forensic class #2) and BEFORE `npm run deploy`.
    Refusal only, never auto-apply; the raise is fenced by _actuate's stage-3 handler."""
    _git("checkout", "main")
    _git("pull", "--ff-only", "origin", "main")
    pending = _pending_migrations()
    if pending:
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"deploy REFUSED: {len(pending)} unapplied remote D1 migration(s) {pending} — "
            f"apply them first (cd ~/its && git pull origin main && cd safety_portal && "
            f"npx wrangler d1 migrations apply {D1_DATABASE_NAME} --remote), then re-run the "
            f"deploy (npm run deploy) or re-submit. Deploying the Worker ahead of its "
            f"migrations 500s the live portal (forensic class #2).",
            error_code=ERR_PENDING_MIGRATIONS,
        )
        raise PendingMigrationsError(f"unapplied remote D1 migrations: {', '.join(pending)}")
    subprocess.run(["npm", "run", "deploy"], cwd=_ROOT / "safety_portal",
                   check=True, capture_output=True, text=True)
    portal_client.get_config_pending(creds.base_url, creds.bearer, limit=1)  # liveness ping


# ── orchestration ─────────────────────────────────────────────────────────────────────


def _apply_config(request: dict[str, Any]) -> str:
    """Stage-1 domain transform: validate + WRITE the config artifact vs live HEAD. A module-
    level seam so the orchestration tests mock it — the REAL file writes are exercised only in
    tests/test_config_apply.py against a tmp root, never against the live po_materials tree."""
    return config_apply.apply_config(request, _ROOT)


def _stamp(creds: _Creds, request_id: int, status: str) -> None:
    portal_client.stamp_config(creds.base_url, creds.bearer, request_id=request_id, status=status)


def _fail(
    creds: _Creds, request_id: int, stage: str, reason: str, *, code_stage: str | None = None
) -> None:
    """Terminal failure: stamp failed(stage, reason) + an operator CRITICAL (detect-and-alert).
    Both best-effort — a stamp/log failure must not mask the original error.

    `code_stage` splits the ITS_Errors error_code from the PORTAL stage when the two disagree.
    The Worker's stage enum is fixed, so a stage-0 git-sync failure must still stamp
    `failed_stage='validated'` — but routing it to `config_actuator.failed.validated` sent the
    operator to the runbook's "Tier-2: re-do the edit in the portal" entry, which is wrong and
    unsafe for a git/deploy-surface fault (Seth, high-capability class). The stamp keeps the
    Worker's vocabulary; the error_code tells the truth."""
    # §54 backstop: `reason` can carry a raw git/gh/wrangler stderr tail (see `_exc_reason`), and the
    # stamp_config leg lands `failure_reason` on the portal Status Monitor — a sink that BYPASSES
    # error_log's own redact choke point. Redact here so neither leg egresses an accidental token/PII.
    # (redact() is idempotent, so the error_log leg re-redacting is harmless.) publish_daemon._fail
    # applies the identical redact() — parity achieved (CE-1, docs/tech_debt.md).
    reason = redact(reason[:1800])
    try:
        portal_client.stamp_config(
            creds.base_url, creds.bearer, request_id=request_id,
            status="failed", failed_stage=stage, failure_reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        # NEVER silent. A failed stamp leaves the portal Status Monitor showing this request
        # still in flight forever with no trace anywhere. Logged AFTER the CRITICAL below would
        # be too late to read in order, so it goes here — but at WARN, so it can never outrank
        # or mask the original failure it is reporting alongside.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not stamp request {request_id} failed({stage}) on the portal — the Status "
            f"Monitor will keep showing it in flight: {exc!r}",
            error_code="config_actuator.stamp_failed",
        )
    error_log.log(
        Severity.CRITICAL, SCRIPT_NAME,
        f"config request {request_id} FAILED at stage {stage!r}: {reason}",
        error_code=f"config_actuator.failed.{code_stage or stage}",
    )


def _exc_reason(exc: Exception) -> str:
    """A bounded reason string; for a subprocess failure, surface its stderr tail."""
    if isinstance(exc, subprocess.CalledProcessError):
        tail = (exc.stderr or exc.stdout or "")[-600:]
        return f"{exc.cmd[0] if exc.cmd else 'cmd'} exit {exc.returncode}: {tail}"
    return f"{type(exc).__name__}: {exc}"


def _actuate(creds: _Creds, request: dict[str, Any], stats: ConfigStats) -> None:
    """Run the config pipeline for ONE claimed request, stamping each milestone. Every stage is
    fenced: a failure stamps failed(stage) + CRITICAL and returns (never raises out — one bad
    request must not wedge the cycle)."""
    request_id = request["id"]
    workstream = request.get("workstream") or WORKSTREAM
    artifact = request.get("artifact_key") or "unknown"

    # Stage 0 — sync to a clean, current main (recover from an interrupted prior cycle) so the
    # re-validation + the commit start from live HEAD.
    try:
        _reset_to_main()
    except Exception as exc:  # noqa: BLE001
        _fail(
            creds, request_id, "validated",
            f"could not sync to main: {_exc_reason(exc)}", code_stage="sync_main",
        )
        stats.failed += 1
        return

    # Stage 1 — re-validate + WRITE against live HEAD (C3) → validated.
    try:
        note = _apply_config(request)
        _stamp(creds, request_id, "validated")
    except (config_apply.ConfigApplyError, json.JSONDecodeError, KeyError) as exc:
        _fail(creds, request_id, "validated", f"{type(exc).__name__}: {exc}")
        stats.failed += 1
        return

    # Stage 2 — commit + CI gate + merge → tested.
    try:
        _commit_test_merge(request_id, workstream, artifact, note)
        _stamp(creds, request_id, "tested")
    except Exception as exc:  # noqa: BLE001 — any actuation failure is terminal+alerted
        reason = _exc_reason(exc)
        _fail(creds, request_id, "tested", reason)
        # Cleanup runs AFTER _fail has stamped the row and raised the CRITICAL, never before:
        # the audit trail must survive even if this cleanup itself dies. Stage 2 is the only
        # stage that can strand a ref — stage 1 precedes the branch, and stages 3-4 run after
        # `gh pr merge --delete-branch` has already removed it.
        _close_and_delete_branch(
            _branch_name(request_id, workstream, artifact),
            f"Auto-closed by the ITS config actuator: config request {request_id} failed at "
            f"stage 'tested', so this branch will never merge. Reason: {reason}",
        )
        stats.failed += 1
        return

    # Stage 3 — deploy + land + health check → live.
    try:
        _deploy_land_health(creds)
        _stamp(creds, request_id, "live")
    except Exception as exc:  # noqa: BLE001
        _fail(creds, request_id, "live", _exc_reason(exc))
        stats.failed += 1
        return

    # Stage 4 — no-op terminal → archived (the Worker BUNDLES config at build time, so the
    # stage-3 deploy already re-bundled it; there is no Box-archive analogue). Reaching the
    # terminal state releases the Worker's per-(workstream,artifact) in-flight lock.
    try:
        _stamp(creds, request_id, "archived")
    except Exception as exc:  # noqa: BLE001
        _fail(creds, request_id, "archived", _exc_reason(exc))
        stats.failed += 1
        return

    stats.actuated += 1
    stats.notes.append(note)


def _sweep_stale_rows(creds: _Creds, stats: ConfigStats) -> None:
    """Reclaim non-terminal config rows stalled past STALE_RECLAIM_S (a daemon that claimed-
    then-died, or a wedged stage): stamp failed('stale_reclaimed') + a CRITICAL once per row,
    so the parent (workstream,artifact) is unwedged and the original death is surfaced. Best-
    effort — a sweep failure logs + returns; it never blocks the cycle's real work."""
    try:
        stuck = portal_client.get_config_stuck(
            creds.base_url, creds.bearer, older_than=int(STALE_RECLAIM_S)
        )
    except Exception as exc:  # noqa: BLE001 — housekeeping; never wedge the cycle
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"config stale-row sweep could not fetch stuck rows: {_exc_reason(exc)}",
            error_code="config_actuator.sweep_fetch_failed",
        )
        return
    for row in stuck:
        rid = row.get("id")
        if not isinstance(rid, int):
            continue
        was = row.get("status")
        artifact = row.get("artifact_key")
        reason = (
            f"stale_reclaimed: non-terminal status {was!r} stalled > {int(STALE_RECLAIM_S)}s "
            f"(lease_owner={row.get('lease_owner')}); the config actuator likely died mid-"
            f"actuation. Artifact {artifact!r} is now unwedged — re-submit if still needed."
        )
        try:
            portal_client.stamp_config(
                creds.base_url, creds.bearer, request_id=rid,
                status="failed", failed_stage="stale_reclaimed", failure_reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"config stale-row sweep could not stamp row {rid} failed: {_exc_reason(exc)}",
                error_code="config_actuator.sweep_stamp_failed",
            )
            continue
        stats.reclaimed += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"config request {rid} reclaimed as STALE (was {was!r}, artifact {artifact!r}) — the "
            f"actuator likely died mid-publish; the artifact is now unwedged. {reason}",
            error_code="config_actuator.stale_reclaimed",
        )


@its_error_log(script_name=SCRIPT_NAME)
@require_active
def config_once() -> ConfigStats:
    """One actuation cycle: gate → creds → sweep → pull → claim → actuate each. Single-shot
    (launchd handles cadence). Serial: one request fully actuated before the next (the deploy
    mutates shared state; the Worker's per-(workstream,artifact) C8 serialization also prevents
    two in-flight for one artifact)."""
    # #336 startup observability (after @require_active, fail-open). Additive (§14).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    stats = ConfigStats()
    if not _polling_enabled():
        stats.halted = "polling_disabled"
        return stats
    # Recover an idle-stranded tree BEFORE anything else this cycle (after the kill-switch +
    # polling gate). A recovery failure is loud + halts the cycle — we cannot safely actuate
    # from a stranded tree.
    try:
        _unstrand_if_needed()
    except Exception as exc:  # noqa: BLE001
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"config actuator could not recover a stranded tree to main: {_exc_reason(exc)}",
            error_code="config_actuator.unstrand_failed",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="halted: could not recover stranded tree to main")
        stats.halted = "unstrand_failed"
        return stats
    creds = _resolve_creds()
    if isinstance(creds, TransientUnavailable):
        # Smartsheet was TEMPORARILY unreachable when reading the Worker base URL — NOT a
        # misconfig, and it self-heals next cycle. WARN + skip; do NOT report unresolved
        # CREDENTIALS (that aims the §43 repair at re-provisioning secrets — a
        # high-capability-class action — for a condition needing none). Skip the watchdog
        # freshness marker exactly like the no-creds path, so a SUSTAINED outage still
        # surfaces via the Check-C staleness floor, just without per-cycle alert spam.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"config Worker base URL temporarily unreadable ({creds.reason}) — skipping "
            f"this cycle; will retry next interval (transient, self-heals)",
            error_code="config_actuator.creds_transient",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="WARN", items_processed=0,
                             error_summary=f"base URL unreadable ({creds.reason}) — transient")
        stats.halted = "creds_transient"
        return stats
    if creds is None:
        # Fail-closed + loud: a missing bearer/URL must not silently drop config edits.
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            "config actuator halted: missing Worker base URL or config bearer (fail-closed)",
            error_code="config_actuator.creds_unresolved",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="halted: missing Worker base URL or config bearer")
        stats.halted = "creds_unresolved"
        return stats

    # Reclaim stale non-terminal rows BEFORE pulling new work, so a wedged artifact is freed
    # this cycle. Best-effort; never blocks the pull.
    _sweep_stale_rows(creds, stats)
    # Same cycle position and the same best-effort contract, but a different kind of debris:
    # _sweep_stale_rows reclaims stuck ROWS, this removes the BRANCHES a terminal failure
    # leaves on GitHub. Before any claim, so this cycle's own branch cannot exist yet.
    _sweep_orphaned_branches(creds, stats)

    rows = portal_client.get_config_pending(creds.base_url, creds.bearer)
    stats.polled = len(rows)
    if rows:
        # Pre-claim deploy gate (forensic class #2): if the remote D1 is missing on-disk
        # migrations, actuating ANY config edit would end in a refused deploy at stage 'live' —
        # so refuse the whole cycle BEFORE claiming. The rows stay `queued` on the Worker, the
        # next launchd cycle retries, and the operator's `migrations apply` unblocks publishing
        # automatically (no re-submit, no lease burned, no terminal-failed row). Checked only
        # when there is work. Fail-CLOSED on a check failure. The authoritative post-pull
        # re-check lives in _deploy_land_health.
        try:
            pending = _pending_migrations()
        except Exception as exc:  # noqa: BLE001 — any check failure is a fail-closed halt
            error_log.log(
                # CRITICAL, not ERROR: a sustained check failure blocks EVERY config edit
                # indefinitely — ERROR never pages and the watchdog only surfaces CRITICAL, so
                # this would be a silent stall. The Resend-leg alert_dedupe bounds the every-
                # 120s repetition to one page per window.
                Severity.CRITICAL, SCRIPT_NAME,
                "config actuation halted: could not verify remote D1 migration state (fail-"
                f"closed, retries next cycle): {_exc_reason(exc)}",
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
                f"config actuation halted: {len(pending)} unapplied remote D1 migration(s) "
                f"{pending} block the deploy stage — apply them (cd ~/its && git pull origin "
                f"main && cd safety_portal && npx wrangler d1 migrations apply {D1_DATABASE_NAME} "
                f"--remote). The {stats.polled} queued config request(s) stay queued and retry "
                f"next cycle automatically. Never deploy the Worker ahead of its migrations "
                f"(forensic class #2).",
                error_code=ERR_PENDING_MIGRATIONS,
            )
            # WARN, not ERROR: a deliberate, bounded refusal — rows stay queued; the operator's
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
        claimed = portal_client.claim_config(
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
    return stats


if __name__ == "__main__":  # pragma: no cover
    config_once()
