"""Track 6 — the archive pass inside `field_ops/fieldops_sync.py`.

What is under test is the wiring, not the relocation: `job_archive` owns moving folders and has its
own suite. Here the questions are the ones that decide whether a failed archive is RESUMABLE or
silently lost — does the pass drain its own queue, does it honour the retry cap, does a per-job
failure leave the other jobs alone, and does a failed commit-point post get treated as self-healing
rather than as data loss.
"""
from __future__ import annotations

from typing import Any

import pytest

from field_ops import fieldops_sync, job_archive
from shared import portal_client


@pytest.fixture
def _seams(mocker):
    """Patch every edge the pass touches. Nothing here reaches a live API."""
    return {
        "pending": mocker.patch.object(
            portal_client, "get_fieldops_pending_archives", return_value=[]
        ),
        "post": mocker.patch.object(
            portal_client, "post_fieldops_archive_progress",
            return_value={"ok": True, "updated": 1, "skipped": []},
        ),
        "capability": mocker.patch.object(
            job_archive, "verify_archive_capability", return_value=True
        ),
        "run": mocker.patch.object(job_archive, "run_archive_pass"),
        "log": mocker.patch.object(fieldops_sync.error_log, "log", return_value=None),
    }


def _queued(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": "JOB-000017",
        "project_name": "Coker",
        "archive_folder_key": "Coker",
        "archive_direction": "archive",
        "archive_state": "requested",
        "archive_attempts": 0,
    }
    base.update(over)
    return base


def _results(moved: int, total: int = 6) -> list[job_archive.ContainerResult]:
    return [
        job_archive.ContainerResult(f"k{i}", f"L{i}", moved=i < moved)
        for i in range(total)
    ]


# ---- the empty case ------------------------------------------------------


def test_an_empty_queue_costs_nothing(_seams):
    # The ADMIN pre-flight is five workspace reads. An idle daemon must not pay for them every
    # cycle just to discover there is no work.
    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out == {"complete": 0, "partial": 0, "failed": 0, "capped": 0, "errors": 0}
    _seams["capability"].assert_not_called()
    _seams["post"].assert_not_called()


def test_the_admin_preflight_runs_once_per_cycle_not_once_per_job(_seams):
    _seams["pending"].return_value = [_queued(job_id=f"JOB-{i}") for i in range(5)]
    _seams["run"].return_value = _results(6)

    fieldops_sync._archive_pass("https://portal.example", "tok")

    assert _seams["capability"].call_count == 1


def test_a_failed_preflight_skips_the_whole_pass_without_burning_attempts(_seams):
    """Unlike the per-container fences INSIDE the relocation, skipping wholesale is right here.

    Every job would 403 on the same permissions shortfall, so attempting them would produce N
    identical failures and burn N attempts against a retry cap that no retry can clear.
    """
    _seams["pending"].return_value = [_queued(), _queued(job_id="JOB-2")]
    _seams["capability"].return_value = False

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["errors"] == 1
    _seams["run"].assert_not_called()
    _seams["post"].assert_not_called()


# ---- draining ------------------------------------------------------------


def test_each_queued_job_is_relocated_and_reported(_seams):
    _seams["pending"].return_value = [_queued()]
    _seams["run"].return_value = _results(6)

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["complete"] == 1
    updates = _seams["post"].call_args.args[2]
    assert len(updates) == 1
    assert updates[0]["job_id"] == "JOB-000017"
    assert updates[0]["direction"] == "archive"
    assert updates[0]["state"] == "complete"
    assert len(updates[0]["containers"]) == 6


def test_the_row_direction_is_forwarded_verbatim_not_assumed(_seams):
    # The Worker's UPDATE is forward-only on (state, direction). Posting 'archive' for a row the
    # operator flipped to un-archive would be silently skipped server-side — and reporting the
    # wrong direction is how an un-archive gets marked complete without happening.
    _seams["pending"].return_value = [_queued(archive_direction="unarchive")]
    _seams["run"].return_value = _results(6)

    fieldops_sync._archive_pass("https://portal.example", "tok")

    assert _seams["post"].call_args.args[2][0]["direction"] == "unarchive"


def test_a_partial_is_reported_as_partial_not_complete(_seams):
    _seams["pending"].return_value = [_queued()]
    _seams["run"].return_value = _results(4)

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["partial"] == 1 and out["complete"] == 0
    assert _seams["post"].call_args.args[2][0]["state"] == "partial"


def test_one_jobs_failure_never_blocks_the_others(_seams):
    _seams["pending"].return_value = [_queued(job_id="A"), _queued(job_id="B"), _queued(job_id="C")]
    _seams["run"].side_effect = [_results(6), RuntimeError("boom"), _results(6)]

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["complete"] == 2 and out["errors"] == 1
    reported = [u["job_id"] for u in _seams["post"].call_args.args[2]]
    assert reported == ["A", "C"]
    assert any(c.kwargs.get("error_code") == "fieldops_archive_job_failed"
               for c in _seams["log"].call_args_list)


def test_the_pass_never_raises(_seams):
    _seams["pending"].return_value = [_queued()]
    _seams["run"].side_effect = RuntimeError("unexpected")
    fieldops_sync._archive_pass("https://portal.example", "tok")  # must not raise


# ---- the retry cap -------------------------------------------------------


def test_a_job_past_the_retry_cap_is_skipped(_seams):
    """Without the cap a PERMANENT condition re-fires the six-container sequence forever."""
    _seams["pending"].return_value = [
        _queued(job_id="A", archive_attempts=job_archive.MAX_ARCHIVE_ATTEMPTS),
        _queued(job_id="B", archive_attempts=0),
    ]
    _seams["run"].return_value = _results(6)

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["capped"] == 1 and out["complete"] == 1
    assert _seams["run"].call_count == 1
    assert [u["job_id"] for u in _seams["post"].call_args.args[2]] == ["B"]


def test_an_all_capped_queue_posts_nothing(_seams):
    # The Worker REFUSES an empty updates array (400). Posting one would turn a quiet no-op into a
    # per-cycle error.
    _seams["pending"].return_value = [_queued(archive_attempts=job_archive.MAX_ARCHIVE_ATTEMPTS)]

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["capped"] == 1
    _seams["post"].assert_not_called()


def test_a_malformed_attempts_value_does_not_crash_the_pass(_seams):
    _seams["pending"].return_value = [_queued(archive_attempts="not-a-number")]
    _seams["run"].return_value = _results(6)

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["complete"] == 1


# ---- fetch + commit-point failures --------------------------------------


def test_a_401_on_the_queue_is_critical_and_relocates_nothing(_seams):
    _seams["pending"].side_effect = portal_client.PortalAuthError("401")

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["errors"] == 1
    _seams["run"].assert_not_called()
    assert any(c.kwargs.get("error_code") == "fieldops_archive_fetch_auth_failed"
               and c.args[0] is fieldops_sync.Severity.CRITICAL
               for c in _seams["log"].call_args_list)


def test_a_transport_failure_on_the_queue_is_error_not_critical(_seams):
    # The queue re-serves next cycle; a transient fetch failure is not a wake-someone event.
    _seams["pending"].side_effect = portal_client.PortalTransportError("boom")

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["errors"] == 1
    assert any(c.kwargs.get("error_code") == "fieldops_archive_fetch_failed"
               and c.args[0] is fieldops_sync.Severity.ERROR
               for c in _seams["log"].call_args_list)


def test_a_failed_progress_post_is_warn_because_the_relocation_self_heals(_seams):
    """The folders ALREADY MOVED; only the report failed.

    That is not data loss and must not page anyone: the queue re-serves the job next cycle and
    every container operation is idempotent, so the re-run reports the same terminal state.
    """
    _seams["pending"].return_value = [_queued()]
    _seams["run"].return_value = _results(6)
    _seams["post"].side_effect = portal_client.PortalTransportError("post failed")

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["errors"] == 1
    warn = [c for c in _seams["log"].call_args_list
            if c.kwargs.get("error_code") == "fieldops_archive_progress_failed"]
    assert warn and warn[0].args[0] is fieldops_sync.Severity.WARN


def test_a_skipped_job_is_surfaced_as_a_changed_request_not_an_error(_seams):
    # Forward-only UPDATE: a row that no longer matches means the operator changed direction while
    # the pass was working. Treating that as an error would train the operator to ignore it.
    _seams["pending"].return_value = [_queued()]
    _seams["run"].return_value = _results(6)
    _seams["post"].return_value = {"ok": True, "updated": 0, "skipped": ["JOB-000017"]}

    out = fieldops_sync._archive_pass("https://portal.example", "tok")

    assert out["errors"] == 0
    assert any(c.kwargs.get("error_code") == "fieldops_archive_progress_skipped"
               for c in _seams["log"].call_args_list)


# ---- the gate ------------------------------------------------------------


def test_the_gate_ships_off(_seams):
    assert fieldops_sync.DEFAULT_ARCHIVE_ENABLED is False


def test_the_gate_is_declared_for_startup_observability():
    # #336: a key resolved at runtime but undeclared is invisible in the startup log and absent
    # from the config dictionary the operator reads.
    declared = {(k.setting, k.workstream) for k in fieldops_sync.REQUIRED_CONFIG}
    assert (fieldops_sync.CFG_ARCHIVE_ENABLED, fieldops_sync.WORKSTREAM) in declared


def test_the_gate_row_is_seeded_even_though_it_ships_false():
    """A boolean gate read with default=False treats a MISSING row identically to `false`.

    So a capability that "ships dark" with no row at all leaves the operator hunting for a switch
    that does not exist — activation must be a visible cell-flip. (HOUSE_REFLEXES §5.)
    """
    import sys
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "scripts" / "migrations"
    if str(migrations) not in sys.path:
        sys.path.insert(0, str(migrations))
    import seed_daemon_gate_config as seeder  # noqa: PLC0415

    row = next(r for r in seeder.CONFIG_ROWS if r["Setting"] == fieldops_sync.CFG_ARCHIVE_ENABLED)
    assert row["Workstream"] == fieldops_sync.WORKSTREAM
    assert row["Value"] == "false"


def test_the_gate_is_operator_editable_and_cutover_verified():
    from operator_dashboard.act import registry  # noqa: PLC0415

    sys_path_key = (fieldops_sync.CFG_ARCHIVE_ENABLED, fieldops_sync.WORKSTREAM)
    assert sys_path_key in registry.REGISTRY

    import sys
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import verify_cutover as vc  # noqa: PLC0415

    row = next(r for r in vc.CONFIG_ROWS
               if r.key == fieldops_sync.CFG_ARCHIVE_ENABLED
               and r.workstream == fieldops_sync.WORKSTREAM)
    # non_empty, never forced true — demanding "true" would force archiving ON at cutover, which
    # is an operator decision, not a cutover gate's.
    assert row.requirement == "non_empty"
