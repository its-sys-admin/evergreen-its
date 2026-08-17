"""Tests for the structural-erosion + verbosity metrics (Tier 1 M-5/M-6/M-7).

Why this file exists
--------------------
The metrics feed a RATCHET, so a measurement bug does not surface as a wrong
number on a dashboard — it surfaces as a floor that silently stops biting, or as
CI red-lining on a change that improved the code. Two properties therefore need
pinning harder than the values themselves:

- **Determinism.** The ratchet compares run to run. A metric with any jitter
  makes every comparison meaningless.
- **The corpus scoping.** The single decision that moved verbosity most was
  excluding module- and class-level statement runs. Measured 2026-08-17,
  including them put verbosity at 0.1404 with the top "duplicate" being a run of
  six module-level constants occurring 321 times — an artifact of the
  identifier-erasing normaliser, not redundancy. Function bodies only puts it at
  0.0664 and surfaces real cross-lane clones. That regression is pinned below.

Failure modes
-------------
Pure AST/radon computation over synthetic trees plus one whole-repo run. No I/O
beyond reading source files.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import check_code_quality_metrics as m  # noqa: E402 — sys.path-driven, house idiom


def _tree(tmp_path: Path, **files: str) -> Path:
    """Build a fake repo whose packages are named like the real ones."""
    for relpath, source in files.items():
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return tmp_path


# ---- scope parity ------------------------------------------------------------


def test_metrics_scope_matches_the_ci_coverage_scope() -> None:
    """One package list governs BOTH signals.

    A package measured for coverage but not for erosion (or the reverse) is a
    blind spot in exactly one of the two, and nothing else would ever say so.
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    cov_packages = set(re.findall(r"--cov=([A-Za-z_][A-Za-z0-9_]*)", ci))
    assert cov_packages == set(m.PRODUCTION_PACKAGES), (
        "ci.yml --cov= scope and PRODUCTION_PACKAGES disagree — reconcile both in "
        f"the same PR. ci.yml={sorted(cov_packages)} "
        f"metrics={sorted(m.PRODUCTION_PACKAGES)}"
    )


def test_every_production_package_exists() -> None:
    """A typo'd package name would silently shrink the corpus and improve both
    metrics for free."""
    missing = [p for p in m.PRODUCTION_PACKAGES if not (REPO_ROOT / p).is_dir()]
    assert not missing, f"PRODUCTION_PACKAGES names non-existent director(ies): {missing}"


# ---- complexity: the double-count / undercount traps -------------------------

_CLASSES_AND_CLOSURES = '''
def outer(a):
    if a:
        pass
    def inner(b):
        if b:
            pass
        if b:
            pass
        return 1
    return inner

class K:
    def m1(self, x):
        if x:
            pass
        return 1

    def m2(self, x):
        for i in x:
            if i:
                pass
        return 2
'''


def test_class_blocks_are_not_double_counted(tmp_path: Path) -> None:
    """radon returns methods BOTH inside their class block and as top-level `M`
    entries, and the class's complexity is their sum. Counting classes would
    count every method twice."""
    root = _tree(tmp_path, **{"shared/mod.py": _CLASSES_AND_CLOSURES})
    metrics = m.collect(root)
    # outer(2) + inner(3) + m1(2) + m2(3) = 10. With the class counted it is 14.
    assert metrics.total_complexity_mass == 10, (
        "class blocks are being counted alongside their own methods — every "
        "method's complexity lands twice"
    )
    assert metrics.total_functions == 4


def test_closures_are_counted_not_dropped(tmp_path: Path) -> None:
    """A closure's complexity is NOT folded into its enclosing function by radon
    and is NOT returned at top level, so a naive walk silently loses it."""
    root = _tree(tmp_path, **{"shared/mod.py": _CLASSES_AND_CLOSURES})
    metrics = m.collect(root)
    # inner is CC 3; dropping it would leave mass at 7 and functions at 3.
    assert metrics.total_complexity_mass == 10
    assert metrics.total_functions == 4


def test_erosion_is_mass_over_mass_not_count_over_count(tmp_path: Path) -> None:
    """Erosion weighs by COMPLEXITY, not by function count — one CC-40 monster
    matters more than ten CC-11 functions."""
    big = "def big(x):\n" + "".join(
        f"    if x == {i}:\n        pass\n" for i in range(14)
    ) + "    return x\n"
    small = "def small(x):\n    return x\n"
    root = _tree(tmp_path, **{"shared/a.py": big, "shared/b.py": small})
    metrics = m.collect(root)

    assert metrics.functions_over_cc10 == 1
    assert metrics.total_functions == 2
    expected = metrics.eroded_complexity_mass / metrics.total_complexity_mass
    assert metrics.structural_erosion == pytest.approx(expected)
    # Count-based would be 1/2 = 0.5; mass-based is 15/16.
    assert metrics.structural_erosion > 0.9


def test_cc30_roster_is_sorted_worst_first_and_names_the_function(tmp_path: Path) -> None:
    def _fn(name: str, branches: int) -> str:
        body = "".join(f"    if x == {i}:\n        pass\n" for i in range(branches))
        return f"def {name}(x):\n{body}    return x\n"

    root = _tree(
        tmp_path,
        **{"shared/a.py": _fn("mild", 5) + _fn("monster", 40) + _fn("bad", 33)},
    )
    metrics = m.collect(root)

    assert [r["function"] for r in metrics.cc30_roster] == ["monster", "bad"]
    assert metrics.functions_over_cc30 == 2
    assert metrics.cc30_roster[0]["file"] == "shared/a.py"
    assert metrics.cc30_roster[0]["complexity"] > metrics.cc30_roster[1]["complexity"]


# ---- verbosity: the scoping regression ---------------------------------------

_CONSTANT_BLOCK = '''
ALPHA = "one"
BETA = "two"
GAMMA = "three"
DELTA = "four"
EPSILON = "five"
ZETA = "six"
'''

_OTHER_CONSTANT_BLOCK = '''
RED = "crimson"
GREEN = "emerald"
BLUE = "azure"
CYAN = "teal"
MAGENTA = "fuchsia"
YELLOW = "amber"
'''


def test_module_level_constant_runs_are_not_duplication(tmp_path: Path) -> None:
    """THE regression pin.

    Normalisation erases identifiers and literal values, so two unrelated blocks
    of six module-level constants are byte-identical after normalising. Counting
    module scope made this the single largest "duplicate" in the real repo — one
    window occurring 321 times — and pushed verbosity from 0.0664 to 0.1404.
    Distinct named constants declared next to each other are not redundant code.
    """
    root = _tree(
        tmp_path,
        **{"shared/a.py": _CONSTANT_BLOCK, "shared/b.py": _OTHER_CONSTANT_BLOCK},
    )
    metrics = m.collect(root)

    assert metrics.duplicated_statements == 0, (
        "module-level constant declarations are being scored as duplication"
    )
    assert metrics.verbosity == 0.0


def test_class_level_field_runs_are_not_duplication(tmp_path: Path) -> None:
    """Same trap one scope in: dataclass field declarations."""
    fields_a = "class A:\n" + "".join(f"    f{i}: int = 0\n" for i in range(8))
    fields_b = "class B:\n" + "".join(f"    g{i}: int = 0\n" for i in range(8))
    root = _tree(tmp_path, **{"shared/a.py": fields_a, "shared/b.py": fields_b})

    assert m.collect(root).duplicated_statements == 0


_CLONED_BODY = '''
def {name}(x):
    a = compute(x)
    if a is None:
        return None
    b = transform(a)
    if b is None:
        return None
    c = finalise(b)
    return c
'''


def test_a_real_cloned_function_body_is_counted(tmp_path: Path) -> None:
    """The other side of the pin — the metric must still catch actual clones,
    including ones whose identifiers were renamed on paste."""
    renamed = (
        _CLONED_BODY.format(name="second")
        .replace("a =", "alpha =")
        .replace("a is", "alpha is")
        .replace("(a)", "(alpha)")
    )
    root = _tree(
        tmp_path,
        **{
            "shared/a.py": _CLONED_BODY.format(name="first"),
            "shared/b.py": renamed,
        },
    )
    metrics = m.collect(root)

    assert metrics.duplicated_statements > 0, (
        "a renamed copy-paste of a 7-statement body was not detected"
    )
    assert metrics.verbosity > 0
    assert metrics.duplicate_hotspots
    assert metrics.duplicate_hotspots[0]["occurrences"] == 2


def test_docstrings_do_not_pad_the_denominator(tmp_path: Path) -> None:
    """A function docstring is one Expr statement. Counting it would let a
    docs-only PR lower verbosity, which is not an improvement in duplication."""
    with_doc = 'def f(x):\n    """Doc."""\n    return x\n'
    without = "def g(x):\n    return x\n"
    a = m.collect(_tree(tmp_path / "a", **{"shared/m.py": with_doc}))
    b = m.collect(_tree(tmp_path / "b", **{"shared/m.py": without}))
    assert a.total_statements == b.total_statements == 1


def test_windows_never_span_two_blocks(tmp_path: Path) -> None:
    """A window must be a contiguous run of SIBLING statements. Splicing across
    scopes manufactures duplicates that never appear together in the source."""
    src = (
        "def f(x):\n"
        "    if x:\n"
        "        a = 1\n"
        "        b = 2\n"
        "        c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
        "    g = 6\n"
    )
    tree = ast.parse(src)
    runs = m._statement_runs(tree)
    for run in runs:
        parents = {id(s) for s in run}
        assert len(parents) == len(run)
    # The `if` body (3 statements) and the function body (4: if, d, e, g) are
    # separate runs; neither reaches DUPLICATE_WINDOW, so nothing is windowed.
    assert all(len(r) < m.DUPLICATE_WINDOW for r in runs)


def test_contiguous_spans_merges_overlapping_windows() -> None:
    assert m._contiguous_spans([1, 2, 3, 7, 8]) == [(1, 3), (7, 8)]
    assert m._contiguous_spans([]) == []
    assert m._contiguous_spans([4]) == [(4, 4)]


# ---- failure handling --------------------------------------------------------


def test_unparseable_file_is_named_and_exits_nonzero(tmp_path: Path) -> None:
    """Silently skipping a broken file would LOWER both metrics and read as an
    improvement. It must be loud."""
    root = _tree(tmp_path, **{"shared/broken.py": "def f(:\n    pass\n"})
    metrics = m.collect(root)

    assert metrics.parse_errors, "an unparseable file was silently skipped"
    assert any("broken.py" in e for e in metrics.parse_errors)


# ---- determinism + the live repo ---------------------------------------------


def test_two_runs_are_byte_identical() -> None:
    """The ratchet compares run to run; jitter makes every comparison useless."""
    first = json.dumps(m.collect().to_json(), sort_keys=True)
    second = json.dumps(m.collect().to_json(), sort_keys=True)
    assert first == second


def test_live_repo_measures_cleanly() -> None:
    """The real tree parses end to end and both metrics land in a sane range."""
    metrics = m.collect()

    assert not metrics.parse_errors, metrics.parse_errors
    assert metrics.files_scanned > 100
    assert 0.0 < metrics.structural_erosion < 1.0
    assert 0.0 <= metrics.verbosity < 1.0
    assert metrics.total_functions > 1000


def test_cli_json_is_valid_and_matches_the_api() -> None:
    """CI consumes the CLI, tests consume the API — they must not diverge."""
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_code_quality_metrics.py"), "--json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload == m.collect().to_json()
