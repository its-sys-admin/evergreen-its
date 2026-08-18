"""The quality ratchet — one file holds every numeric quality floor, and CI defends it.

Purpose
-------
Enforces ``.quality-ratchet.json``. Each metric carries a bound that may move in
exactly one direction for free; moving it the WRONG way requires the entry to
carry a reason, a tech-debt reference, and an expiry date, and CI fails once that
date passes.

Why a ratchet and not thresholds
--------------------------------
The 2026-08-16 audit's structural finding was not that any one control was wrong.
It was that **six slope-controls had each been individually, reasonably relaxed**:
coverage measured 2 of 9 packages with no floor, the doc-conventions lint sat
warn-only 23 days past its own sunset, 6 of 10 verify checks were excluded, there
was no dependency scanning at all, and five guard hooks failed open. Every single
relaxation was defensible in isolation. The aggregate was a system whose safety
story was narrated rather than enforced — Op Stds §52, applied to the guardrails
themselves.

A threshold that can be lowered under pressure will be, and each lowering will be
locally defensible. What breaks that loop is not better judgment; it is making
the relaxation carry an expiry that re-arms the control automatically.
*SlopCodeBench* (arXiv:2603.24755) is the same finding from the other side: prose
guidance moves the intercept by up to a third and leaves the slope untouched.
Ratchets move slopes.

Invariants
----------
- **Fail closed.** A metric that cannot be measured FAILS; it never passes
  quietly. Skipping one requires naming it on the command line (``--skip``),
  which is visible in the CI log and in review.
- **Every metric in the file is checked, and every checked metric is in the
  file.** An unknown key fails; a missing known key fails. Nothing is silently
  ignored, in either direction.
- **The file is the single source.** ``ci.yml`` reads the coverage floor FROM
  this file rather than holding its own copy — two copies of a floor is a floor
  that drifts.
- **Relaxation is all-or-nothing.** ``regression_reason`` + ``tech_debt_ref`` +
  ``expires``, together or not at all. A partial relaxation fails.

Failure modes
-------------
- Baseline ref unresolvable -> FAIL. Silently skipping the
  did-a-bound-move-the-wrong-way comparison would make the whole relaxation rule
  advisory, which is the pattern this file exists to end.
- ``.quality-ratchet.json`` absent from the baseline ref -> not an error. That is
  the first landing, and there is genuinely nothing to compare against.

Consumers
---------
- ``.github/workflows/ci.yml`` — blocking step.
- ``docs/operations/complexity_budget.md`` — names this as its enforcement surface.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RATCHET_PATH = REPO_ROOT / ".quality-ratchet.json"

_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# All three are required together on a relaxed entry, or none may be present.
RELAXATION_FIELDS = ("regression_reason", "tech_debt_ref", "expires")

# Keys beginning with `_` are prose for the human reader (JSON has no comments)
# and are ignored by the checker.
_COMMENT_PREFIX = "_"


@dataclass(frozen=True)
class MetricSpec:
    """One enforced number: which way it may move, and how to measure it."""

    key: str
    kind: str  # "floor" (raise-only) | "ceiling" (lower-only)
    unit: str
    what: str
    # True when a LOCAL measurement is not comparable to the CI one, so the bound
    # may only be re-set from a CI run. --suggest refuses to recommend tightening
    # these, because a local number that reads BETTER than CI's would hand someone
    # a bound that red-lines main on the next push.
    ci_authoritative: bool = False

    @property
    def direction(self) -> str:
        return "raise-only" if self.kind == "floor" else "lower-only"


@dataclass
class Measured:
    value: float | None
    detail: str = ""
    error: str = ""


# ---- measurement --------------------------------------------------------------


def _measure_coverage(coverage_json: Path | None) -> Measured:
    if coverage_json is None:
        return Measured(
            None,
            error="no --coverage-json given; run pytest with "
            "`--cov-report=json:coverage.json` first (CI always does)",
        )
    if not coverage_json.exists():
        return Measured(None, error=f"{coverage_json} does not exist")
    try:
        data = json.loads(coverage_json.read_text(encoding="utf-8"))
        pct = float(data["totals"]["percent_covered"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Measured(None, error=f"{coverage_json} unreadable: {exc!r}")
    total = data["totals"]
    return Measured(
        pct,
        detail=f"{total['covered_lines']}/{total['num_statements']} statements",
    )


def _code_metrics() -> Any:
    import check_code_quality_metrics as ccqm  # noqa: PLC0415 — heavy, only when needed

    return ccqm.collect()


def _measure_doc_warnings() -> Measured:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.lint_doc_conventions"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if proc.returncode not in (0, 1):
        return Measured(None, error=f"lint_doc_conventions exited {proc.returncode}")
    count = len(re.findall(r"^\s*\[warn\]", proc.stdout, re.MULTILINE))
    return Measured(
        float(count),
        detail="lint_doc_conventions [warn] lines (CI-authoritative: mtime "
               "grandfathering makes a local count read LOW)",
    )


def _measure_mypy_errors() -> Measured:
    """Run mypy and read the error count off its own summary line.

    CI runs `mypy .` immediately before this step, so `.mypy_cache` is warm and
    this second pass is incremental. Parsing mypy's summary rather than a
    hand-rolled log keeps one source of truth for what "an error" is.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "."],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    out = proc.stdout
    if "Success: no issues found" in out:
        return Measured(0.0, detail="mypy: no issues")
    mo = re.search(r"Found (\d+) errors? in", out)
    if mo:
        return Measured(float(mo.group(1)), detail="mypy summary line")
    return Measured(
        None,
        error=f"could not parse mypy output (exit {proc.returncode}): "
        f"{out.strip().splitlines()[-1] if out.strip() else '<empty>'}",
    )


def _measure_excluded_verify_checks() -> Measured:
    """How many verify_cutover checks the daily watchdog runner does NOT run.

    Counted from watchdog's own declared enrolment set rather than inferred, so
    the number is auditable in one place. `tests/test_quality_ratchet.py` pins
    that every enrolled id exists in `verify_cutover.CHECKS`.
    """
    try:
        import verify_cutover  # noqa: PLC0415 — bare import; scripts/ is not a package
        import watchdog  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return Measured(None, error=f"could not import the verify runner: {exc!r}")
    total = len(verify_cutover.CHECKS)
    enrolled = len(watchdog.VERIFY_RUNNER_ENROLLED)
    return Measured(
        float(total - enrolled),
        detail=f"{enrolled} of {total} verify_cutover checks enrolled in the watchdog runner",
    )


SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("coverage_percent", "floor", "%", "pytest line coverage, 8 production packages"),
    MetricSpec("structural_erosion", "ceiling", "", "complexity mass in CC>10 functions / total"),
    MetricSpec("verbosity", "ceiling", "", "statements in a duplicated 6-statement block / total"),
    MetricSpec("functions_over_cc30", "ceiling", " functions", "the extraction roster"),
    # CI-AUTHORITATIVE. `lint_doc_conventions` grandfathers an evergreen doc whose
    # FILESYSTEM MTIME predates 2026-05-24 (its own `_doc_likely_grandfathered`,
    # which the function's docstring already calls "slightly imperfect"). A CI
    # checkout stamps every file with the checkout time, so nothing is grandfathered
    # there and the count is both HIGHER and deterministic; a developer tree keeps
    # real mtimes and reads LOW. Measured 2026-08-18 on one commit: CI 89, local 88.
    # Harmless for the GATE, which runs in CI — dangerous for --suggest, which would
    # otherwise recommend tightening the ceiling to a number CI cannot meet.
    MetricSpec("doc_convention_warnings", "ceiling", " warnings",
               "lint_doc_conventions violations", ci_authoritative=True),
    MetricSpec("mypy_errors", "ceiling", " errors", "mypy . error count"),
    MetricSpec("excluded_verify_checks", "ceiling", " checks", "verify_cutover checks the watchdog skips"),
)
SPECS_BY_KEY = {s.key: s for s in SPECS}


def measure_all(coverage_json: Path | None, skip: frozenset[str]) -> dict[str, Measured]:
    """Measure every metric not explicitly skipped."""
    out: dict[str, Measured] = {}
    needs_code_metrics = {"structural_erosion", "verbosity", "functions_over_cc30"} - skip
    code = _code_metrics() if needs_code_metrics else None

    for spec in SPECS:
        if spec.key in skip:
            continue
        if spec.key == "coverage_percent":
            out[spec.key] = _measure_coverage(coverage_json)
        elif spec.key == "structural_erosion":
            assert code is not None
            out[spec.key] = (
                Measured(None, error="; ".join(code.parse_errors))
                if code.parse_errors
                else Measured(code.structural_erosion, detail="check_code_quality_metrics")
            )
        elif spec.key == "verbosity":
            assert code is not None
            out[spec.key] = (
                Measured(None, error="; ".join(code.parse_errors))
                if code.parse_errors
                else Measured(code.verbosity, detail="check_code_quality_metrics")
            )
        elif spec.key == "functions_over_cc30":
            assert code is not None
            out[spec.key] = Measured(
                float(code.functions_over_cc30), detail="check_code_quality_metrics"
            )
        elif spec.key == "doc_convention_warnings":
            out[spec.key] = _measure_doc_warnings()
        elif spec.key == "mypy_errors":
            out[spec.key] = _measure_mypy_errors()
        elif spec.key == "excluded_verify_checks":
            out[spec.key] = _measure_excluded_verify_checks()
    return out


# ---- the ratchet file ---------------------------------------------------------


def load_ratchet(path: Path = RATCHET_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} root is not an object")
    return data


def entries(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The metric entries, with `_`-prefixed prose keys dropped."""
    return {
        k: v for k, v in data.items() if not k.startswith(_COMMENT_PREFIX)
    }


def _bound(entry: dict[str, Any], spec: MetricSpec) -> float | None:
    raw = entry.get(spec.kind)
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def check_shape(data: dict[str, Any]) -> list[str]:
    """Structural problems with the file itself."""
    problems: list[str] = []
    present = entries(data)

    unknown = sorted(set(present) - set(SPECS_BY_KEY))
    if unknown:
        problems.append(
            f"unknown metric key(s) {unknown} — a key nothing measures is a floor "
            "nobody defends; remove it or add a spec in SPECS"
        )
    missing = sorted(set(SPECS_BY_KEY) - set(present))
    if missing:
        problems.append(
            f"metric(s) missing from .quality-ratchet.json: {missing} — every "
            "enforced metric must carry its bound in the file"
        )

    for key, entry in sorted(present.items()):
        spec = SPECS_BY_KEY.get(key)
        if spec is None:
            continue
        if not isinstance(entry, dict):
            problems.append(f"{key}: entry must be an object")
            continue
        if _bound(entry, spec) is None:
            problems.append(f"{key}: missing a numeric `{spec.kind}`")
        wrong_kind = "ceiling" if spec.kind == "floor" else "floor"
        if wrong_kind in entry:
            problems.append(
                f"{key}: has a `{wrong_kind}` but is a {spec.kind} metric "
                f"({spec.direction})"
            )
        if entry.get("direction") != spec.direction:
            problems.append(
                f"{key}: direction is {entry.get('direction')!r}, must be "
                f"{spec.direction!r}"
            )
    return problems


def check_relaxations(data: dict[str, Any], today: date) -> list[str]:
    """The relaxation rule: all three fields together, and not expired."""
    problems: list[str] = []
    for key, entry in sorted(entries(data).items()):
        if not isinstance(entry, dict):
            continue
        present = [f for f in RELAXATION_FIELDS if entry.get(f) not in (None, "")]
        if not present:
            continue
        absent = [f for f in RELAXATION_FIELDS if f not in present]
        if absent:
            problems.append(
                f"{key}: relaxed entry is missing {absent} — a relaxation carries "
                f"all of {list(RELAXATION_FIELDS)} or none of them. Without an "
                f"expiry the control never re-arms, which is the failure this "
                f"whole file exists to prevent."
            )
            continue
        raw = str(entry["expires"])
        try:
            expires = date.fromisoformat(raw)
        except ValueError:
            problems.append(f"{key}: expires={raw!r} is not an ISO date (YYYY-MM-DD)")
            continue
        if expires < today:
            problems.append(
                f"{key}: relaxation EXPIRED on {expires.isoformat()} (today is "
                f"{today.isoformat()}). Restore the bound, or extend the expiry "
                f"deliberately with a fresh reason. Reason on record: "
                f"{entry['regression_reason']!r} ({entry['tech_debt_ref']})."
            )
    return problems


# ---- baseline comparison ------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def baseline_entries(ref: str) -> tuple[dict[str, Any] | None, str | None]:
    """The ratchet file as of ``ref``. Returns (entries, error).

    (None, None) means "the file did not exist at that ref" — the first landing,
    legitimately nothing to compare. (None, msg) is a hard error.
    """
    if _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        return None, (
            f"baseline ref {ref!r} does not resolve. CI must fetch it before this "
            f"step (`git fetch --depth=1 origin main:refs/remotes/origin/main`). "
            f"Skipping the comparison would make the relaxation rule advisory."
        )
    show = _git("show", f"{ref}:.quality-ratchet.json")
    if show.returncode != 0:
        return None, None  # not present at the baseline — first landing
    try:
        return entries(json.loads(show.stdout)), None
    except ValueError as exc:
        return None, f"baseline .quality-ratchet.json at {ref} is not valid JSON: {exc}"


def check_undeclared_relaxations(
    data: dict[str, Any], base: dict[str, Any]
) -> list[str]:
    """A bound that moved the WRONG way must carry the three relaxation fields."""
    problems: list[str] = []
    current = entries(data)
    for key, spec in sorted(SPECS_BY_KEY.items()):
        cur_entry, base_entry = current.get(key), base.get(key)
        if not isinstance(cur_entry, dict) or not isinstance(base_entry, dict):
            continue
        cur, old = _bound(cur_entry, spec), _bound(base_entry, spec)
        if cur is None or old is None:
            continue
        loosened = cur < old if spec.kind == "floor" else cur > old
        if not loosened:
            continue
        if all(cur_entry.get(f) not in (None, "") for f in RELAXATION_FIELDS):
            continue
        problems.append(
            f"{key}: {spec.kind} moved the wrong way ({old:g} -> {cur:g}, "
            f"{spec.direction}) without a declared relaxation. Add "
            f"{list(RELAXATION_FIELDS)} to the entry, or restore the bound. "
            f"Tightening it needs no ceremony; loosening it needs an expiry."
        )
    return problems


# ---- verdict ------------------------------------------------------------------


def compare(
    data: dict[str, Any], measured: dict[str, Measured]
) -> tuple[list[str], list[str]]:
    """(violations, report-lines) for the measured metrics."""
    violations: list[str] = []
    lines: list[str] = []
    current = entries(data)
    for spec in SPECS:
        entry = current.get(spec.key)
        if not isinstance(entry, dict):
            continue
        bound = _bound(entry, spec)
        m = measured.get(spec.key)
        if m is None:
            lines.append(f"  {spec.key:<24} SKIPPED (explicitly, via --skip)")
            continue
        if m.value is None:
            violations.append(f"{spec.key}: NOT MEASURED — {m.error}")
            lines.append(f"  {spec.key:<24} UNMEASURED — {m.error}")
            continue
        if bound is None:
            continue
        ok = m.value >= bound if spec.kind == "floor" else m.value <= bound
        mark = "ok  " if ok else "FAIL"
        rel = " [relaxed]" if entry.get("expires") else ""
        lines.append(
            f"  {spec.key:<24} {mark}  {m.value:g}{spec.unit} "
            f"({spec.kind} {bound:g}{spec.unit}){rel}"
            + (f"  — {m.detail}" if m.detail else "")
        )
        if not ok:
            comparison = "below its floor" if spec.kind == "floor" else "above its ceiling"
            violations.append(
                f"{spec.key}: {m.value:g}{spec.unit} is {comparison} of "
                f"{bound:g}{spec.unit} ({spec.what})"
            )
    return violations, lines


def suggest(data: dict[str, Any], measured: dict[str, Measured]) -> list[str]:
    """Bounds that could be tightened for free, for a human to paste."""
    out: list[str] = []
    current = entries(data)
    for spec in SPECS:
        entry = current.get(spec.key)
        m = measured.get(spec.key)
        if not isinstance(entry, dict) or m is None or m.value is None:
            continue
        bound = _bound(entry, spec)
        if bound is None:
            continue
        tighter = m.value > bound if spec.kind == "floor" else m.value < bound
        if not tighter:
            continue
        if spec.ci_authoritative:
            out.append(
                f'  # {spec.key}: measured {m.value:g} here, but this metric is '
                f'CI-AUTHORITATIVE — a local run reads better than CI does. '
                f'Re-set it from a CI log, never from this output.'
            )
            continue
        new = int(m.value) if spec.unit.strip() else round(m.value, 4)
        out.append(f'  "{spec.key}": {{ "{spec.kind}": {new}, ... }}   (was {bound:g})')
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Enforce .quality-ratchet.json.")
    ap.add_argument("--coverage-json", type=Path, default=None,
                    help="coverage.json from `pytest --cov-report=json:...`")
    ap.add_argument("--baseline-ref", default=None,
                    help="git ref to compare bounds against (CI: origin/main)")
    ap.add_argument("--skip", default="",
                    help="comma-separated metrics to skip measuring (visible in the log)")
    ap.add_argument("--today", default=None,
                    help="override today's date for expiry checks (tests only; CI must not pass it)")
    ap.add_argument("--suggest", action="store_true",
                    help="also print bounds that could be tightened for free")
    args = ap.parse_args(argv)

    skip = frozenset(s.strip() for s in args.skip.split(",") if s.strip())
    today = date.fromisoformat(args.today) if args.today else date.today()

    # Read the module global at CALL time, not at def time: the default
    # argument would freeze the path at import and make RATCHET_PATH
    # unpatchable, which is how the prove-it-bites test passed vacuously.
    try:
        data = load_ratchet(RATCHET_PATH)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    problems += check_shape(data)
    problems += check_relaxations(data, today)

    unknown_skips = sorted(skip - set(SPECS_BY_KEY))
    if unknown_skips:
        problems.append(f"--skip names unknown metric(s): {unknown_skips}")

    if args.baseline_ref:
        base, err = baseline_entries(args.baseline_ref)
        if err:
            problems.append(err)
        elif base is None:
            print(
                f"note: .quality-ratchet.json is not present at {args.baseline_ref} "
                "— first landing, no bound comparison to make.\n"
            )
        else:
            problems += check_undeclared_relaxations(data, base)

    measured = measure_all(args.coverage_json, skip)
    violations, lines = compare(data, measured)

    print("quality ratchet")
    print("\n".join(lines))
    if skip:
        print(f"\n  skipped by request: {sorted(skip)}")

    if args.suggest:
        tighter = suggest(data, measured)
        print("\n  could be tightened for free (raising a floor / lowering a "
              "ceiling needs no ceremony):")
        print("\n".join(tighter) if tighter else "    nothing — every bound is at "
              "or beyond the measured value")

    failures = problems + violations
    if failures:
        print(f"\nRATCHET FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nRATCHET OK — every bound held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
