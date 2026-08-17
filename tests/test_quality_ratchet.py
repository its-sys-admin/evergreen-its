"""Tests for the quality ratchet — the slope control (audit 2026-08-16).

Why this file exists
--------------------
The ratchet's whole value is that it cannot be quietly relaxed. Every test below
attacks that property directly rather than checking that the happy path prints a
number:

- a bound moved the wrong way with no declaration must FAIL,
- a partial relaxation (reason but no expiry) must FAIL,
- an EXPIRED relaxation must FAIL, and keep failing,
- an unmeasurable metric must FAIL rather than pass quietly,
- and the CI invocation must pass neither `--skip` nor `--today`, the two flags
  that would let a human turn any of the above off.

That last one matters most. Six of this audit's findings were controls that had
each been individually, reasonably silenced. A ratchet with a convenient bypass
in the CI command line is the seventh.

Failure modes
-------------
Pure filesystem + subprocess assertions over temp ratchet files. The whole-repo
run is exercised once, with the slow metrics skipped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import check_quality_ratchet as qr  # noqa: E402 — sys.path-driven, house idiom

RATCHET = REPO_ROOT / ".quality-ratchet.json"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

TODAY = date(2026, 8, 17)
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
NEXT_MONTH = (TODAY + timedelta(days=30)).isoformat()


def _live() -> dict:
    return json.loads(RATCHET.read_text(encoding="utf-8"))


# ---- the committed file --------------------------------------------------------


def test_committed_ratchet_is_structurally_valid() -> None:
    assert qr.check_shape(_live()) == []


def test_committed_ratchet_carries_no_expired_relaxation() -> None:
    """A relaxation that outlives its expiry re-arms the control by failing CI.
    This asserts the committed file is not currently in that state."""
    assert qr.check_relaxations(_live(), date.today()) == []


def test_every_metric_has_an_entry_and_every_entry_has_a_metric() -> None:
    """Both directions. An unmeasured key is a floor nobody defends; a missing key
    is a metric nobody bounded."""
    assert set(qr.entries(_live())) == set(qr.SPECS_BY_KEY)


def test_floors_and_ceilings_declare_the_matching_direction() -> None:
    for key, entry in qr.entries(_live()).items():
        spec = qr.SPECS_BY_KEY[key]
        assert spec.kind in entry, f"{key} has no {spec.kind}"
        assert entry["direction"] == spec.direction


# ---- the relaxation rule -------------------------------------------------------


def _entry(**over) -> dict:
    base = {"ceiling": 19, "direction": "lower-only"}
    base.update(over)
    return {"functions_over_cc30": base}


def test_relaxation_missing_expires_fails() -> None:
    """The single most important case. Without an expiry the control never
    re-arms, which is precisely how six of this audit's findings came to be."""
    problems = qr.check_relaxations(
        _entry(regression_reason="mid-migration", tech_debt_ref="docs/tech_debt.md#x"),
        TODAY,
    )
    assert len(problems) == 1
    assert "expires" in problems[0]


def test_relaxation_missing_reason_fails() -> None:
    problems = qr.check_relaxations(
        _entry(tech_debt_ref="docs/tech_debt.md#x", expires=NEXT_MONTH), TODAY
    )
    assert len(problems) == 1
    assert "regression_reason" in problems[0]


def test_relaxation_missing_tech_debt_ref_fails() -> None:
    problems = qr.check_relaxations(
        _entry(regression_reason="mid-migration", expires=NEXT_MONTH), TODAY
    )
    assert len(problems) == 1
    assert "tech_debt_ref" in problems[0]


def test_expired_relaxation_fails() -> None:
    problems = qr.check_relaxations(
        _entry(
            regression_reason="mid-migration",
            tech_debt_ref="docs/tech_debt.md#x",
            expires=YESTERDAY,
        ),
        TODAY,
    )
    assert len(problems) == 1
    assert "EXPIRED" in problems[0]
    # The reason is quoted back, so whoever hits this knows what was promised.
    assert "mid-migration" in problems[0]


def test_complete_unexpired_relaxation_passes() -> None:
    assert qr.check_relaxations(
        _entry(
            regression_reason="mid-migration",
            tech_debt_ref="docs/tech_debt.md#x",
            expires=NEXT_MONTH,
        ),
        TODAY,
    ) == []


def test_expiry_on_exactly_today_still_passes() -> None:
    """Boundary: the relaxation is good THROUGH its expiry date, not up to it."""
    assert qr.check_relaxations(
        _entry(
            regression_reason="r",
            tech_debt_ref="t",
            expires=TODAY.isoformat(),
        ),
        TODAY,
    ) == []


def test_malformed_expiry_fails() -> None:
    problems = qr.check_relaxations(
        _entry(regression_reason="r", tech_debt_ref="t", expires="next tuesday"), TODAY
    )
    assert len(problems) == 1
    assert "not an ISO date" in problems[0]


# ---- undeclared wrong-way moves ------------------------------------------------


def test_lowering_a_ceiling_needs_no_ceremony() -> None:
    base = {"functions_over_cc30": {"ceiling": 19, "direction": "lower-only"}}
    tighter = {"functions_over_cc30": {"ceiling": 12, "direction": "lower-only"}}
    assert qr.check_undeclared_relaxations(tighter, base) == []


def test_raising_a_floor_needs_no_ceremony() -> None:
    base = {"coverage_percent": {"floor": 84, "direction": "raise-only"}}
    tighter = {"coverage_percent": {"floor": 90, "direction": "raise-only"}}
    assert qr.check_undeclared_relaxations(tighter, base) == []


def test_raising_a_ceiling_without_declaring_it_fails() -> None:
    base = {"functions_over_cc30": {"ceiling": 19, "direction": "lower-only"}}
    looser = {"functions_over_cc30": {"ceiling": 25, "direction": "lower-only"}}
    problems = qr.check_undeclared_relaxations(looser, base)
    assert len(problems) == 1
    assert "moved the wrong way" in problems[0]


def test_lowering_a_floor_without_declaring_it_fails() -> None:
    base = {"coverage_percent": {"floor": 84, "direction": "raise-only"}}
    looser = {"coverage_percent": {"floor": 60, "direction": "raise-only"}}
    problems = qr.check_undeclared_relaxations(looser, base)
    assert len(problems) == 1
    assert "84 -> 60" in problems[0]


def test_a_declared_wrong_way_move_is_allowed() -> None:
    base = {"functions_over_cc30": {"ceiling": 19, "direction": "lower-only"}}
    looser = {
        "functions_over_cc30": {
            "ceiling": 25,
            "direction": "lower-only",
            "regression_reason": "schedule lane split mid-flight",
            "tech_debt_ref": "docs/tech_debt.md#cc30",
            "expires": NEXT_MONTH,
        }
    }
    assert qr.check_undeclared_relaxations(looser, base) == []


def test_unresolvable_baseline_ref_is_an_error_not_a_skip() -> None:
    """Fail closed. Skipping the comparison silently would make the entire
    relaxation rule advisory."""
    base, err = qr.baseline_entries("refs/heads/no-such-branch-abcdef")
    assert base is None
    assert err and "does not resolve" in err


# ---- fail-closed measurement ---------------------------------------------------


def test_missing_coverage_json_is_a_failure_not_a_pass(tmp_path) -> None:
    m = qr._measure_coverage(tmp_path / "absent.json")
    assert m.value is None and "does not exist" in m.error

    violations, _ = qr.compare(_live(), {"coverage_percent": m})
    assert any("NOT MEASURED" in v for v in violations)


def test_no_coverage_json_flag_at_all_is_also_a_failure() -> None:
    m = qr._measure_coverage(None)
    assert m.value is None and "no --coverage-json" in m.error


def test_unknown_skip_target_fails(tmp_path, monkeypatch, capsys) -> None:
    """A typo'd --skip would silently enforce a metric the author meant to skip,
    or worse, look like it skipped one it did not."""
    monkeypatch.setattr(qr, "RATCHET_PATH", RATCHET)
    rc = qr.main(["--skip", "coverage_percent,typo_metric", "--today", TODAY.isoformat()])
    assert rc == 1
    assert "unknown metric" in capsys.readouterr().out


def test_unknown_key_in_the_file_fails() -> None:
    data = dict(qr.entries(_live()))
    data["invented_metric"] = {"ceiling": 1, "direction": "lower-only"}
    problems = qr.check_shape(data)
    assert any("unknown metric key" in p for p in problems)


def test_missing_key_in_the_file_fails() -> None:
    data = dict(qr.entries(_live()))
    del data["mypy_errors"]
    problems = qr.check_shape(data)
    assert any("missing from .quality-ratchet.json" in p for p in problems)


def test_wrong_bound_kind_fails() -> None:
    data = dict(qr.entries(_live()))
    data["coverage_percent"] = {"ceiling": 84, "direction": "raise-only"}
    problems = qr.check_shape(data)
    assert any("is a floor metric" in p or "missing a numeric `floor`" in p for p in problems)


# ---- the CI invocation ---------------------------------------------------------


def test_ci_reads_the_coverage_floor_from_the_ratchet_file() -> None:
    """Single source. A second copy of the floor in the workflow is a copy that
    drifts, and the drifted one wins silently."""
    ci = CI_YML.read_text(encoding="utf-8")
    assert "COVERAGE_FLOOR:" not in ci, (
        "ci.yml still declares its own coverage floor — it must read "
        ".quality-ratchet.json instead"
    )
    assert "quality-ratchet.json')" in ci and "coverage_percent" in ci


def test_ci_emits_the_coverage_json_the_ratchet_reads() -> None:
    ci = CI_YML.read_text(encoding="utf-8")
    assert "--cov-report=json:coverage.json" in ci, (
        "without the JSON report the ratchet's coverage metric goes UNMEASURED "
        "and the step fails closed"
    )


def _test_job_steps() -> list[dict]:
    """The `test` job's steps, parsed as YAML.

    Deliberately NOT a regex over the raw text. The first version of these guards
    searched for `check_quality_ratchet.py` and matched a COMMENT in the pytest
    step, so every assertion below ran against prose and passed no matter what the
    real invocation said. The mutation battery caught it; the green run did not.
    """
    ci = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    return ci["jobs"]["test"]["steps"]


def _step_running(fragment: str) -> dict:
    matches = [s for s in _test_job_steps() if fragment in (s.get("run") or "")]
    assert len(matches) == 1, (
        f"expected exactly one `test` step whose run block contains {fragment!r}, "
        f"found {len(matches)}"
    )
    return matches[0]


def test_ci_runs_the_ratchet_as_a_blocking_step() -> None:
    run = _step_running("scripts/check_quality_ratchet.py")["run"]
    assert "|| true" not in run and "|| echo" not in run, (
        "the ratchet step is swallowed — it must block"
    )


def test_ci_passes_neither_skip_nor_today() -> None:
    """The two flags that would turn the ratchet off.

    `--skip` leaves a metric unmeasured; `--today` lets an expired relaxation
    pass. Either in the CI command line is a bypass sitting in plain sight, which
    is exactly the shape of the six silenced controls this ratchet answers.
    """
    run = _step_running("scripts/check_quality_ratchet.py")["run"]
    assert "--skip" not in run, "CI must measure every metric"
    assert "--today" not in run, "CI must not override the expiry clock"


def test_ci_passes_the_baseline_ref_so_wrong_way_moves_are_detected() -> None:
    run = _step_running("scripts/check_quality_ratchet.py")["run"]
    assert "--baseline-ref" in run, (
        "without a baseline the undeclared-relaxation check never runs and the "
        "whole relaxation rule is advisory"
    )
    assert "--coverage-json" in run, "coverage would go UNMEASURED and fail closed"


def test_ci_fetches_the_baseline_without_swallowing_failure() -> None:
    ci = CI_YML.read_text(encoding="utf-8")
    fetch = [ln for ln in ci.splitlines() if "git fetch --depth=1 origin main" in ln]
    assert fetch, "ci.yml does not fetch the ratchet baseline"
    assert not any("|| true" in ln for ln in fetch), (
        "a swallowed baseline fetch makes the wrong-way-move check silently no-op"
    )


# ---- the enrolment set the excluded-checks metric counts against ---------------


def test_every_enrolled_verify_check_exists() -> None:
    """`VERIFY_RUNNER_ENROLLED` is a declaration, so a typo would silently inflate
    the 'enrolled' count and understate exclusions — the unsafe direction."""
    import verify_cutover
    import watchdog

    known = {spec.check_id for spec in verify_cutover.CHECKS}
    unknown = sorted(watchdog.VERIFY_RUNNER_ENROLLED - known)
    assert not unknown, f"VERIFY_RUNNER_ENROLLED names non-existent check(s): {unknown}"


def test_excluded_verify_check_count_is_derived_not_hardcoded() -> None:
    import verify_cutover
    import watchdog

    m = qr._measure_excluded_verify_checks()
    assert m.value == len(verify_cutover.CHECKS) - len(watchdog.VERIFY_RUNNER_ENROLLED)


# ---- end to end ----------------------------------------------------------------


def test_live_repo_passes_the_committed_ratchet() -> None:
    """The bounds in this commit hold against this commit's code.

    Coverage is skipped: producing coverage.json means a full instrumented test
    run, which CI does once and this suite must not do again.
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "check_quality_ratchet.py"),
            "--skip",
            "coverage_percent",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RATCHET OK" in proc.stdout


def test_a_deliberately_regressed_bound_fails_the_run(tmp_path, monkeypatch) -> None:
    """Prove it bites: pin a ceiling below reality and the run must fail."""
    tightened = dict(qr.entries(_live()))
    tightened["functions_over_cc30"] = {"ceiling": 0, "direction": "lower-only"}
    path = tmp_path / ".quality-ratchet.json"
    path.write_text(json.dumps(tightened), encoding="utf-8")
    monkeypatch.setattr(qr, "RATCHET_PATH", path)

    rc = qr.main(["--skip", "coverage_percent", "--today", TODAY.isoformat()])
    assert rc == 1
