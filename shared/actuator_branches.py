"""Orphan-branch cleanup shared by the two privileged git actuators (Op Stds §50).

`safety_reports/publish_daemon` and `po_materials/config_actuator` run the SAME branch
lifecycle: commit on a per-request branch → open a PR → wait for CI → `gh pr merge --squash
--delete-branch`. The branch is therefore removed ONLY on the success path, so every
`<prefix>/req-<id>-*` branch whose request is no longer in flight is debris from a terminal
failure. Nothing removed that debris before this module existed: requests 5 and 6 for
`erosion-inspection-v1` each stranded a branch plus an open PR, and
`config/req-1-po_materials-purchaser` sat on the remote for 40 days carrying no PR at all.

The hazard is not untidiness. A stranded PR keeps a SUPERSEDED definition mergeable — both
stranded publish PRs conflicted with the definition that actually shipped, and the conflict hunk
was the form's legal attestation, so resolving it the obvious way would have silently dropped a
legal certification from a live safety form.

WHY SHARED rather than duplicated per daemon: the two actuators already carry a hand-copied
`_wait_for_ci` family, and that copy DRIFTED — `_check_failure_detail` returns a bare job name in
one and an explanatory string in the other, so the module that produced the confusing incident
message is the one that never got the fix. Writing this logic once removes that failure mode for
the destructive half, and the repo's duplicate-block ratchet independently rejects a second copy.

Seams (`gh`, `git`, `delete_refs`) are passed IN at call time rather than captured in the config,
so each daemon's thin wrapper stays the canonical mock point for its own tests (the
`shared/heartbeat.py` delegator pattern).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shared import error_log
from shared.error_log import Severity

#: How much older than a legal actuation a branch must be before it is considered debris.
#: Callers pass ``2 * CI_TIMEOUT_S``: no actuation can run longer than its CI wait plus a deploy.
DEFAULT_MIN_AGE_MULTIPLIER = 2


@dataclass(frozen=True)
class BranchCleanup:
    """The per-daemon DATA (no callables — see the module docstring on seams)."""

    script_name: str
    """ITS_Errors `Script` value, and the error-code namespace (`<script_name>.orphan_*`)."""

    prefix: str
    """Branch namespace: `publish` or `config`, giving `<prefix>/req-<id>-...`."""

    root: Path
    """Repo root, for `git -C`."""

    runbook: str
    """§43 runbook path, named in the WARN when a delete does not take."""

    min_age_s: float
    """Age floor before a branch may be deleted."""

    def code(self, suffix: str) -> str:
        return f"{self.script_name}.{suffix}"

    def branch_glob(self) -> str:
        return f"{self.prefix}/req-*"

    def branch_re(self) -> re.Pattern[str]:
        # Anchored, numeric id, trailing separator required. This predicate gates a DELETE, so a
        # name that is not exactly ours must fail to match rather than be guessed at.
        return re.compile(rf"^{re.escape(self.prefix)}/req-(\d+)-")


def request_id_from_branch(branch: str, cfg: BranchCleanup) -> int | None:
    """Request id parsed from a daemon-owned branch name, or None when the name is not ours.

    An operator branch that merely begins with the same prefix is not ours and is never touched.
    """
    match = cfg.branch_re().match(branch)
    return int(match.group(1)) if match else None


def branch_tip_epoch(branch: str, gh: Callable[..., str]) -> float | None:
    """Unix time of the REMOTE branch tip, or None when it cannot be established.

    None is the fail-closed answer: the caller skips a branch of unknown age rather than deleting
    it, so a GitHub hiccup can only under-clean, never over-delete.
    """
    try:
        raw = gh(
            "api", f"repos/{{owner}}/{{repo}}/branches/{branch}",
            "--jq", ".commit.commit.committer.date",
        ).strip()
    except Exception:  # noqa: BLE001 — an unknown age is a skip, not an error
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def delete_branch_refs(branch: str, root: Path) -> None:
    """Drop the remote then the local ref.

    Both are idempotent-by-failure (removing an absent ref merely exits non-zero), so neither
    return code is inspected — the caller re-reads the REMOTE to establish the truth instead of
    inferring it from an exit status. These bare `subprocess.run` calls execute OUTSIDE any CC
    session, so the `block-dangerous-git` hook does not apply; it falls open for the daemon.
    """
    subprocess.run(["git", "-C", str(root), "push", "origin", "--delete", branch],
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "branch", "-D", branch],
                   capture_output=True, text=True)


def close_and_delete_branch(
    branch: str,
    reason: str,
    *,
    cfg: BranchCleanup,
    gh: Callable[..., str],
    git: Callable[..., str],
    delete_refs: Callable[[str], None],
) -> bool:
    """Post `reason`, close the branch's PR, and delete the branch. True when the ref is gone.

    NEVER raises: it runs immediately after a failure has already been RECORDED, so a cleanup
    problem must not mask the original fault or wedge the cycle.

    REFUSES when the PR is merged. That branch belongs to the success path, which already deleted
    it; a merged PR arriving here means the caller mis-identified the branch, and deleting on a
    mistaken premise is exactly what must not happen silently.
    """
    open_pr: int | None = None
    try:
        rows = json.loads(
            gh("pr", "list", "--head", branch, "--state", "all",
               "--json", "number,state,mergedAt")
        )
    except Exception:  # noqa: BLE001 — no PR data: fall through to a branch-only delete
        rows = []
    for row in rows if isinstance(rows, list) else []:
        if row.get("mergedAt") or row.get("state") == "MERGED":
            error_log.log(
                Severity.WARN, cfg.script_name,
                f"orphan cleanup REFUSED for {branch}: PR #{row.get('number')} is MERGED, so the "
                f"success path owns this branch. Nothing was deleted.",
                error_code=cfg.code("orphan_refused_merged"),
            )
            return False
        if row.get("state") == "OPEN" and isinstance(row.get("number"), int):
            open_pr = row["number"]

    if open_pr is not None:
        # Comment BEFORE closing: a bare closure leaves a reader of the closed PR with nowhere to
        # learn why it went away.
        try:
            gh("pr", "comment", str(open_pr), "--body", reason)
        except Exception:  # noqa: BLE001 — the close matters more than the annotation
            error_log.log(
                Severity.WARN, cfg.script_name,
                f"orphan cleanup could not annotate PR #{open_pr} ({branch}); closing it anyway",
                error_code=cfg.code("orphan_comment_failed"),
            )
        try:
            gh("pr", "close", str(open_pr))
        except Exception:  # noqa: BLE001 — still drop the ref below
            error_log.log(
                Severity.WARN, cfg.script_name,
                f"orphan cleanup could not close PR #{open_pr} ({branch}); still removing the ref",
                error_code=cfg.code("orphan_close_failed"),
            )

    delete_refs(branch)
    try:
        still_there = bool(git("ls-remote", "--heads", "origin", branch).strip())
    except Exception:  # noqa: BLE001 — cannot verify ⇒ report failure, never a false success
        still_there = True
    if still_there:
        error_log.log(
            Severity.WARN, cfg.script_name,
            f"orphan cleanup ran for {branch} but the remote ref is still present — it needs the "
            f"manual ref removal in {cfg.runbook}",
            error_code=cfg.code("orphan_delete_failed"),
        )
        return False
    error_log.log(
        Severity.INFO, cfg.script_name,
        f"orphan cleanup removed branch {branch}"
        + (f" and closed PR #{open_pr}" if open_pr else ""),
        error_code=cfg.code("orphan_cleaned"),
    )
    return True


def sweep_orphaned_branches(
    *,
    cfg: BranchCleanup,
    git: Callable[..., str],
    in_flight: Callable[[], set[Any]],
    tip_epoch: Callable[[str], float | None],
    close_and_delete: Callable[[str, str], bool],
    describe: Callable[[int, int], str],
) -> int:
    """Remove branches stranded by terminally-failed actuations. Returns how many went.

    Best-effort; never raises. Callers run it at cycle start BEFORE any claim, so the branch that
    cycle is about to create cannot yet exist.

    A branch is debris only when BOTH hold: its request is absent from the in-flight set, AND its
    tip is older than `cfg.min_age_s`. Either test alone would suffice in the normal case;
    demanding both means a wrong answer from one cannot delete live work.
    """
    try:
        live = in_flight()
    except Exception as exc:  # noqa: BLE001 — cannot establish what is live ⇒ delete NOTHING
        error_log.log(
            Severity.ERROR, cfg.script_name,
            f"orphan-branch sweep could not establish which requests are in flight, so it "
            f"deleted nothing this cycle: {exc!r}",
            error_code=cfg.code("orphan_sweep_fetch_failed"),
        )
        return 0
    try:
        refs = git("ls-remote", "--heads", "origin", cfg.branch_glob())
    except Exception as exc:  # noqa: BLE001 — housekeeping must never wedge the cycle
        error_log.log(
            Severity.ERROR, cfg.script_name,
            f"orphan-branch sweep could not list remote branches: {exc!r}",
            error_code=cfg.code("orphan_sweep_list_failed"),
        )
        return 0
    removed: list[str] = []
    now = time.time()
    for line in refs.splitlines():
        if "refs/heads/" not in line:
            continue
        branch = line.split("refs/heads/", 1)[1].strip()
        request_id = request_id_from_branch(branch, cfg)
        if request_id is None or request_id in live:
            continue
        tip = tip_epoch(branch)
        if tip is None or (now - tip) < cfg.min_age_s:
            continue
        if close_and_delete(branch, describe(request_id, int((now - tip) // 3600))):
            removed.append(branch)
    if removed:
        # WARN, not INFO, and deliberately so: deleting a ref is DESTRUCTIVE, and INFO rows are
        # env-gated (`ITS_ERROR_LOG_INFO=1`, default off) so they would normally reach no sink at
        # all. The per-branch PR comment is the audit trail when a PR exists — but the orphan
        # shape that actually occurred in production (`config/req-1-po_materials-purchaser`) had
        # NO PR, so without this line that deletion would leave no default-visible record
        # anywhere. One aggregate line per cycle, emitted only when something was removed, keeps
        # that quiet in the normal case where there is nothing to clean.
        error_log.log(
            Severity.WARN, cfg.script_name,
            f"orphan-branch sweep removed {len(removed)} stranded branch(es) from terminally-"
            f"failed request(s): {', '.join(removed)}. This is routine cleanup, not a fault — "
            f"see {cfg.runbook}.",
            error_code=cfg.code("orphan_sweep_removed"),
        )
    return len(removed)
