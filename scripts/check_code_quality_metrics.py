"""Structural-erosion + verbosity metrics — the anti-slop measurement (Tier 1 M-5/M-6/M-7).

Purpose
-------
Measures the two trajectory metrics from *SlopCodeBench* (Orlanski et al.,
arXiv:2603.24755) over this repo's production packages, and emits them as JSON
for ``scripts/check_quality_ratchet.py`` to enforce:

- **structural erosion** — the share of total cyclomatic-complexity mass held by
  functions above CC 10. Rises when new capability is appended as another branch
  to an already-large handler, which the paper found is what agents do by
  default and what makes erosion climb monotonically in 80% of their runs.
- **verbosity** — the share of statements that sit inside a duplicated
  normalized-AST block. Rises with copy-paste and with speculative abstraction
  that never gets reused.

Nothing in the repo measured either before 2026-08-17. That is the gap the
2026-08-16 audit named: Operational Standards is 159 KB of prose guidance, and
the paper's central negative result is that prose guidance moves the INTERCEPT
(up to a third better initial quality) and **not the slope** (degradation rate
unchanged, at up to 47.9% more tokens). Measurement plus a ratchet is what moves
a slope. This script is the measurement half.

Why the numbers here are worth defending
----------------------------------------
Against the paper's panels (473 maintained OSS Python repos): human-maintained
verbosity averages 0.11, agent-written 0.32; human erosion 0.31, agent 0.68.
On first measurement (2026-08-17) ITS came in at verbosity 0.0664 and erosion
0.3890 — verbosity comfortably below the human panel, erosion mildly above it
and far below the agent panel. The likeliest cause of the verbosity result is
Op Stds §14 preservation-over-refactor plus the "defer abstraction until >=4
real reuse cases" rule, which together suppress both copy-paste and speculative
abstraction. The ratchet exists to HOLD that, not to chase a target.

Do not read the absolute values as comparable to the paper's to three decimals.
This instrumentation approximates the paper's rather than reproducing it — see
``_statement_runs`` for the one scoping decision that moved the number most. The
signal the ratchet consumes is the TREND under a fixed method, and the method is
fixed here.

Invariants
----------
- **Deterministic.** Pure function of the source tree: no clock, no network, no
  randomness. Two runs on one tree give byte-identical JSON, because the ratchet
  compares run-to-run and a jittery metric would make it unusable.
- **Reports, never enforces.** Thresholds live in ``.quality-ratchet.json`` and
  are enforced by ``check_quality_ratchet.py``. Keeping measurement and policy in
  separate files is what lets the floors be reviewed as data.
- **Scope is the eight production packages** (``PRODUCTION_PACKAGES``), the same
  set CI instruments for coverage. ``tests/test_code_quality_metrics.py`` pins
  that parity — a package measured for coverage but not for erosion (or the
  reverse) is a blind spot in exactly one of the two.

Failure modes
-------------
- A file that fails to parse is COUNTED AS AN ERROR and named, never skipped
  silently: silently dropping an unparseable file would lower both metrics and
  read as an improvement. ``--json`` still emits, with ``parse_errors`` non-empty;
  the ratchet checker treats a non-empty ``parse_errors`` as a failure.
- ``radon`` is a dev dependency. Its absence raises at import — this script only
  ever runs in CI or from a dev venv.

Consumers
---------
- ``scripts/check_quality_ratchet.py`` — reads ``--json`` output.
- ``.github/workflows/ci.yml`` — via the ratchet checker.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from radon.complexity import cc_visit

REPO_ROOT = Path(__file__).resolve().parent.parent

# The eight production packages. Deliberately EXCLUDES `scripts/` (109 files,
# ~33K LOC of operator-run one-shots and migrations, whose complexity profile is
# genuinely different from daemon code and whose churn would swamp the signal)
# and `tests/`. Kept identical to CI's `--cov=` set so one package list governs
# both signals; the parity is pinned by tests/test_code_quality_metrics.py.
PRODUCTION_PACKAGES: tuple[str, ...] = (
    "shared",
    "safety_reports",
    "field_ops",
    "progress_reports",
    "po_materials",
    "subcontracts",
    "docs_pdf",
    "operator_dashboard",
)

# The CC boundary above which a function's complexity counts as "eroded" mass.
# 10 is the long-standing McCabe convention and the paper's own cut.
CC_EROSION_THRESHOLD = 10

# The CC boundary at which a function must be extracted before the next feature
# adds a branch to it (the M-7 roster). See docs/operations/complexity_budget.md.
CC_EXTRACTION_THRESHOLD = 30

# Sliding-window size for the duplicate-block scan, in statements. 6 follows the
# paper. Smaller windows match incidental boilerplate (three-line try/except
# wrappers appear everywhere legitimately); larger ones miss real copy-paste.
DUPLICATE_WINDOW = 6

# Placeholder tokens for AST normalisation. Two blocks that differ only in
# identifier or literal are the SAME duplicated block for this metric — renaming
# a copy-pasted helper's variables does not make it not-copy-pasted.
_NAME = "\x00N"
_CONST = "\x00C"

# Cap the serialised duplicate roster. The metric is the number; the roster is a
# pointer at where to look, and an unbounded one would bloat every CI log.
_HOTSPOT_LIMIT = 15


@dataclass
class Metrics:
    """Everything one run measures. Serialised verbatim to JSON."""

    structural_erosion: float = 0.0
    verbosity: float = 0.0
    functions_over_cc10: int = 0
    functions_over_cc30: int = 0
    total_functions: int = 0
    total_complexity_mass: int = 0
    eroded_complexity_mass: int = 0
    total_statements: int = 0
    duplicated_statements: int = 0
    files_scanned: int = 0
    parse_errors: list[str] = field(default_factory=list)
    # The CC>30 roster, worst first — the M-7 extraction list, named so a review
    # can see WHICH functions moved rather than only that the count did.
    cc30_roster: list[dict[str, Any]] = field(default_factory=list)
    # The duplicated-block roster, most-repeated first. Same reasoning: a
    # verbosity number nobody can act on is a number nobody will act on. Each
    # entry is the FIRST occurrence of a window that appears more than once.
    duplicate_hotspots: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "structural_erosion": round(self.structural_erosion, 4),
            "verbosity": round(self.verbosity, 4),
            "functions_over_cc10": self.functions_over_cc10,
            "functions_over_cc30": self.functions_over_cc30,
            "total_functions": self.total_functions,
            "total_complexity_mass": self.total_complexity_mass,
            "eroded_complexity_mass": self.eroded_complexity_mass,
            "total_statements": self.total_statements,
            "duplicated_statements": self.duplicated_statements,
            "files_scanned": self.files_scanned,
            "parse_errors": self.parse_errors,
            "cc30_roster": self.cc30_roster,
            "duplicate_hotspots": self.duplicate_hotspots[:_HOTSPOT_LIMIT],
        }


def iter_source_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every ``.py`` file in the production packages, in a stable order."""
    files: list[Path] = []
    for pkg in PRODUCTION_PACKAGES:
        pkg_dir = root / pkg
        if not pkg_dir.is_dir():
            continue
        files.extend(
            p
            for p in pkg_dir.rglob("*.py")
            if "__pycache__" not in p.parts
        )
    return sorted(files)


# ---- structural erosion ------------------------------------------------------


def _flatten_blocks(blocks: list[Any]) -> list[Any]:
    """Every function-level block, with no double counting.

    Radon's ``cc_visit`` returns a mix that is easy to get wrong (verified
    empirically against radon 6.0.1, 2026-08-17):

    - ``F`` top-level functions, ``M`` methods, ``C`` classes are ALL returned at
      the top level. A class's ``complexity`` is the AGGREGATE of its methods,
      and those same methods are also present as ``M`` entries — so counting
      classes double-counts every method. Classes are dropped here.
    - A function's ``closures`` are NOT returned at the top level and are NOT
      included in the enclosing function's complexity. Missing them would
      undercount, so they are walked explicitly and counted as functions in
      their own right.
    """
    out: list[Any] = []
    for block in blocks:
        if block.letter == "C":
            # Methods arrive separately as `M`; the class entry is their sum.
            continue
        out.append(block)
        closures = getattr(block, "closures", None)
        if closures:
            out.extend(_flatten_blocks(closures))
    return out


def measure_complexity(files: list[Path], metrics: Metrics, root: Path = REPO_ROOT) -> None:
    """Fill in the erosion half of ``metrics``."""
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
            blocks = _flatten_blocks(cc_visit(source))
        except Exception as exc:  # noqa: BLE001 — any parse/visit fault, named not swallowed
            metrics.parse_errors.append(f"{path.relative_to(root)}: {exc!r}")
            continue

        for block in blocks:
            cc = block.complexity
            metrics.total_functions += 1
            metrics.total_complexity_mass += cc
            if cc > CC_EROSION_THRESHOLD:
                metrics.functions_over_cc10 += 1
                metrics.eroded_complexity_mass += cc
            if cc > CC_EXTRACTION_THRESHOLD:
                metrics.functions_over_cc30 += 1
                qualified = (
                    f"{block.classname}.{block.name}"
                    if getattr(block, "classname", None)
                    else block.name
                )
                metrics.cc30_roster.append(
                    {
                        "file": str(path.relative_to(root)),
                        "function": qualified,
                        "lineno": block.lineno,
                        "complexity": cc,
                    }
                )

    if metrics.total_complexity_mass:
        metrics.structural_erosion = (
            metrics.eroded_complexity_mass / metrics.total_complexity_mass
        )
    # Worst first, then a stable tiebreak so the roster is byte-identical run to run.
    metrics.cc30_roster.sort(key=lambda r: (-r["complexity"], r["file"], r["function"]))


# ---- verbosity ---------------------------------------------------------------


class _Normaliser(ast.NodeTransformer):
    """Erase identifiers and literals so a renamed copy is still a copy."""

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802 — ast API
        return ast.copy_location(ast.Name(id=_NAME, ctx=node.ctx), node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
        self.generic_visit(node)
        node.attr = _NAME
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        self.generic_visit(node)
        node.arg = _NAME
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
        # Preserve the TYPE, erase the value: `x = 1` and `x = 2` are the same
        # shape, but `x = 1` and `x = "s"` are not.
        return ast.copy_location(
            ast.Constant(value=f"{_CONST}{type(node.value).__name__}"), node
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:  # noqa: N802
        self.generic_visit(node)
        node.name = _NAME
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:  # noqa: N802
        self.generic_visit(node)
        node.name = _NAME
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:  # noqa: N802
        self.generic_visit(node)
        node.name = _NAME
        return node


def _normalised(stmt: ast.stmt) -> str:
    """A canonical string for one statement, identifiers and literals erased.

    Docstrings are excluded by the caller, not here — see ``_statement_runs``.
    """
    clone = _Normaliser().visit(ast.parse(ast.unparse(stmt)).body[0])
    return ast.dump(clone, annotate_fields=False)


_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _statement_runs(tree: ast.AST) -> list[list[ast.stmt]]:
    """Contiguous sibling-statement runs INSIDE function bodies, docstrings dropped.

    Two scoping decisions, both load-bearing, both settled by measuring:

    **Windows are taken within one block, never across a flattened file.** A
    flattened walk splices statements from unrelated scopes into runs that never
    appear together in the source, manufacturing duplicates that do not exist.

    **Only function bodies count — module and class scope are excluded from BOTH
    the numerator and the denominator.** Measured 2026-08-17: including them put
    verbosity at 0.1404, and the single most "duplicated" window in the entire
    repo occurred 321 times and was a run of six module-level constant
    assignments. Normalisation erases identifiers, so ``A = "x"`` and ``B = "y"``
    are the same shape — which means every block of six adjacent constants in the
    codebase collides with every other. That is an artifact of the normaliser, not
    redundancy: those constants are all distinct and all necessary. The same held
    for dataclass field declarations (64x) and the brand colour table (15x).

    Restricting to function bodies drops verbosity to 0.0664 and the top hits
    become real, actionable clones: the credential-resolution block repeated
    across the poll daemons, the find-or-create-sheet race block repeated across
    the standing trackers, the chunk-reassembly validator. Max repeat count falls
    from 321 to 6. A metric whose largest signal is "constants are declared next
    to each other" cannot move a slope; this one can.
    """
    runs: list[list[ast.stmt]] = []

    def walk(node: ast.AST, inside_function: bool) -> None:
        for attr in ("body", "orelse", "finalbody"):
            seq = getattr(node, attr, None)
            if not (
                isinstance(seq, list)
                and seq
                and all(isinstance(s, ast.stmt) for s in seq)
            ):
                continue
            here = inside_function or isinstance(node, _FUNC_NODES)
            if here:
                body = [s for s in seq if not _is_docstring(s)]
                if body:
                    runs.append(body)
            for stmt in seq:
                walk(stmt, here)

    walk(tree, inside_function=False)
    return runs


def measure_verbosity(files: list[Path], metrics: Metrics, root: Path = REPO_ROOT) -> None:
    """Fill in the verbosity half of ``metrics``.

    Two passes, because a window is only duplicated relative to the WHOLE corpus:
    pass 1 hashes every window and counts occurrences; pass 2 marks the
    statements covered by any window seen more than once. A statement inside two
    overlapping duplicated windows is counted ONCE — this is a fraction of
    statements, not of windows.
    """
    window_counts: Counter[tuple[str, ...]] = Counter()
    per_file: dict[Path, list[list[str]]] = {}
    per_file_runs: dict[Path, list[list[ast.stmt]]] = {}

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            err = f"{path.relative_to(root)}: {exc!r}"
            if err not in metrics.parse_errors:
                metrics.parse_errors.append(err)
            continue

        runs = _statement_runs(tree)
        per_file_runs[path] = runs
        norm_runs: list[list[str]] = []
        for run in runs:
            try:
                norm_runs.append([_normalised(s) for s in run])
            except Exception as exc:  # noqa: BLE001 — unparse/parse round-trip fault
                metrics.parse_errors.append(
                    f"{path.relative_to(root)} (normalise): {exc!r}"
                )
                norm_runs.append([])
        per_file[path] = norm_runs

        for norm in norm_runs:
            for i in range(len(norm) - DUPLICATE_WINDOW + 1):
                window_counts[tuple(norm[i : i + DUPLICATE_WINDOW])] += 1

    total = 0
    duplicated = 0
    hotspots: list[dict[str, Any]] = []
    for path, norm_runs in per_file.items():
        for run_idx, norm in enumerate(norm_runs):
            run = per_file_runs[path][run_idx]
            total += len(run)
            flagged: dict[int, int] = {}
            for i in range(len(norm) - DUPLICATE_WINDOW + 1):
                count = window_counts[tuple(norm[i : i + DUPLICATE_WINDOW])]
                if count > 1:
                    for j in range(i, i + DUPLICATE_WINDOW):
                        flagged[j] = max(flagged.get(j, 0), count)
            duplicated += len(flagged)

            # Collapse the flagged indices into CONTIGUOUS REGIONS. Sliding
            # windows overlap heavily, so one duplicated 14-statement block
            # otherwise reports as nine near-identical roster entries three
            # lines apart — a roster nobody reads. One entry per region, its
            # occurrence count the max over the windows covering it.
            for start, end in _contiguous_spans(sorted(flagged)):
                hotspots.append(
                    {
                        "occurrences": max(flagged[i] for i in range(start, end + 1)),
                        "file": str(path.relative_to(root)),
                        "lineno": run[start].lineno,
                        "end_lineno": run[end].end_lineno or run[end].lineno,
                        "statements": end - start + 1,
                    }
                )

    metrics.total_statements = total
    metrics.duplicated_statements = duplicated
    if total:
        metrics.verbosity = duplicated / total
    metrics.duplicate_hotspots = sorted(
        hotspots,
        key=lambda h: (-h["occurrences"], -h["statements"], h["file"], h["lineno"]),
    )


def _contiguous_spans(indices: list[int]) -> list[tuple[int, int]]:
    """Merge sorted indices into inclusive spans: [1,2,3,7,8] -> [(1,3),(7,8)]."""
    spans: list[tuple[int, int]] = []
    for i in indices:
        if spans and i == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], i)
        else:
            spans.append((i, i))
    return spans


# ---- entrypoint --------------------------------------------------------------


def collect(root: Path = REPO_ROOT) -> Metrics:
    """Measure everything. The one public entry point."""
    files = iter_source_files(root)
    metrics = Metrics(files_scanned=len(files))
    measure_complexity(files, metrics, root)
    measure_verbosity(files, metrics, root)
    return metrics


def _render_human(m: Metrics) -> str:
    lines = [
        "code-quality metrics — SlopCodeBench trajectory metrics "
        f"({m.files_scanned} files, {len(PRODUCTION_PACKAGES)} production packages)",
        "",
        f"  structural erosion   {m.structural_erosion:.4f}   "
        f"({m.eroded_complexity_mass} of {m.total_complexity_mass} complexity mass "
        f"in {m.functions_over_cc10} of {m.total_functions} functions above CC "
        f"{CC_EROSION_THRESHOLD})",
        f"  verbosity            {m.verbosity:.4f}   "
        f"({m.duplicated_statements} of {m.total_statements} statements inside a "
        f"duplicated {DUPLICATE_WINDOW}-statement block)",
        f"  functions over CC{CC_EXTRACTION_THRESHOLD}  {m.functions_over_cc30}",
        "",
        "  reference panels (arXiv:2603.24755, 473 maintained OSS Python repos):",
        "    erosion   human 0.31   agent 0.68",
        "    verbosity human 0.11   agent 0.32",
    ]
    if m.cc30_roster:
        lines += ["", f"  CC>{CC_EXTRACTION_THRESHOLD} roster (worst first):"]
        lines += [
            f"    {r['complexity']:>3}  {r['file']}::{r['function']}"
            for r in m.cc30_roster
        ]
    if m.duplicate_hotspots:
        lines += ["", f"  duplicated {DUPLICATE_WINDOW}-statement blocks (most repeated first):"]
        lines += [
            f"    x{h['occurrences']:<3} {h['statements']:>3} stmts  "
            f"{h['file']}:{h['lineno']}-{h['end_lineno']}"
            for h in m.duplicate_hotspots[:_HOTSPOT_LIMIT]
        ]
    if m.parse_errors:
        lines += ["", f"  PARSE ERRORS ({len(m.parse_errors)}) — metrics are UNRELIABLE:"]
        lines += [f"    {e}" for e in m.parse_errors]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure structural erosion + verbosity over the production packages."
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON on stdout"
    )
    args = parser.parse_args(argv)

    metrics = collect()
    if args.json:
        print(json.dumps(metrics.to_json(), indent=2, sort_keys=True))
    else:
        print(_render_human(metrics))

    # A parse error means the numbers understate reality; never exit 0 on one.
    return 1 if metrics.parse_errors else 0


if __name__ == "__main__":
    sys.exit(main())
