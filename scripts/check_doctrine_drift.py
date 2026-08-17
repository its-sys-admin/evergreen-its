"""Mechanical doctrine-drift checks for the doc-reconciliation-auditor.

Purpose
-------
Deterministic, reproducible mechanical tier of the `doc-reconciliation-auditor`
agent (`.claude/agents/doc-reconciliation-auditor.md`). Reads
`docs/doctrine_manifest.yaml` (the canonical facts) and reports drift between
those facts and this execution repo's code/docs. PROPOSE-ONLY: it prints
findings; it never edits anything.

Invariants
----------
- Read-only. No writes, no network. Exits 0 even when it finds drift (it is a
  report, not a CI gate — the operator/agent decides what to do).
- The manifest is the single source of truth. Do NOT hard-code doctrine facts
  here; read them from `docs/doctrine_manifest.yaml`.
- High precision on version drift: only current-doctrine prose surfaces are
  scanned (CLAUDE.md, README, docs/operations); historical surfaces
  (docs/session_logs, docs/audits, docs/reports) are skipped so correct history
  is never flagged. (Op Stds v13 §42 + brief guardrail: don't flag historical refs.)

Failure modes
-------------
- Missing / unparseable manifest -> prints an error and exits 2 (the only
  non-zero path; signals a broken input, not drift).
- Missing optional target files are skipped, never errors.
- Workstream-slug coverage is reported at "coverage" severity, NOT "drift":
  a planning-only workstream with no exec code is correctly-unbuilt, and the
  semantic tier classifies it — the mechanical tier must not false-alarm.

Consumers
---------
- `.claude/agents/doc-reconciliation-auditor.md` (mechanical tier).
- `tests/test_check_doctrine_drift.py`.
- Operators on demand: `python -m scripts.check_doctrine_drift [--json]`.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "docs" / "doctrine_manifest.yaml"

# Current-doctrine prose: version citations here should be current. Includes .claude/agents/
# (agent definitions ARE current-doctrine prose — an agent pinning a stale Op Stds / FM version
# is exactly the drift M1 should catch; this is where the ops-stds-enforcer "v13" staleness hid
# undetected until Brief 2.A/2.D).
CURRENT_DOCTRINE_FILES = ["CLAUDE.md", "README.md"]
CURRENT_DOCTRINE_DIRS = ["docs/operations", ".claude/agents"]
# Workstream entrypoints checked for §42 alongside shared/*.
ENTRYPOINTS = [
    "safety_reports/intake.py",
    "safety_reports/portal_poll.py",
    "safety_reports/weekly_generate.py",
    "safety_reports/weekly_send.py",
    "safety_reports/weekly_send_poll.py",
    "scripts/watchdog.py",
]

# Checks whose findings are precise enough to BLOCK a merge under --strict (the CI
# gate). Deliberately EXCLUDES M2 (tech_debt self-closure): it is a calibration-FP-prone
# heuristic — its own docstring says so, and it produces live false positives on clean
# main (legitimately-OPEN entries that mention adjacent completed PRs, e.g. the Phase-5
# deploy-prerequisites and weekly_send-smoke entries). M2 still PRINTS as a 'drift'
# finding for the doc-reconciliation-auditor; it just does not gate CI. M3/M5/M6 are
# 'coverage' (informational). M1 (version), M4 (sheet-id), and M7 (citation-resolver)
# are FP-free by construction and are the class-#4 core (forensic lessons-learned 2026-06-28).
# M8 (manifest-vs-live-blueprint version drift) is blocking too, and is the one
# blocking check CI can never fire: the runner has no ../its-blueprint, so M8
# reports COVERAGE there and gates only the operator's local --strict. That is
# the point — H-5 was that the arbiter of doctrine currency had no currency
# check anywhere, and the only place one can run is where the blueprint is.
STRICT_BLOCKING_CHECKS: frozenset[str] = frozenset({"M1", "M4", "M7", "M8"})


@dataclass
class Finding:
    check: str  # M1..M5
    severity: str  # drift | coverage | clean
    location: str
    detail: str


def _load_manifest() -> dict[str, Any]:
    if not MANIFEST.exists():
        print(f"ERROR: manifest not found at {MANIFEST}", file=sys.stderr)
        sys.exit(2)
    try:
        data = yaml.safe_load(MANIFEST.read_text())
    except yaml.YAMLError as exc:
        print(f"ERROR: manifest is not valid YAML: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print("ERROR: manifest root is not a mapping", file=sys.stderr)
        sys.exit(2)
    return data


def _current_doctrine_files() -> list[Path]:
    files: list[Path] = []
    for name in CURRENT_DOCTRINE_FILES:
        p = REPO_ROOT / name
        if p.exists():
            files.append(p)
    for d in CURRENT_DOCTRINE_DIRS:
        files.extend(sorted((REPO_ROOT / d).glob("*.md")))
    return files


# Historical/past-tense markers near a version ref mean "correct history" — not
# drift (Op Stds v13 §42 guardrail: don't flag historical refs). Checked in a
# window AROUND each match, not line-wide: a long table row can carry a current
# citation AND, far away, an unrelated "superseded".
_HIST_MARKERS = re.compile(
    r"earlier|previously|originally|former|supersed|deprecat|no longer|retired|historical|moved|lagged|reframed",
    re.I,
)


def _near_historical(line: str, start: int, end: int) -> bool:
    window = line[max(0, start - 40) : end + 80]
    return _HIST_MARKERS.search(window) is not None


def check_version_drift(m: dict[str, Any]) -> list[Finding]:
    """M1 — doctrine-version-string drift in current-doctrine prose.

    Skips a match framed as historical (a past-tense / superseded marker within
    the window around the citation) so correct history is never flagged.
    """
    dv = m["doctrine_versions"]
    ops = int(dv["operational_standards"]["current"])
    fm = int(dv["foundation_mission"]["current"])
    ops_re = re.compile(r"(?:Op Stds|Operational Standards)\s+v(\d+)")
    fm_re = re.compile(r"(?:Foundation Mission|FM)\s+v(\d+)")
    findings: list[Finding] = []
    for f in _current_doctrine_files():
        rel = f.relative_to(REPO_ROOT)
        for ln, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            for mo in ops_re.finditer(line):
                if int(mo.group(1)) != ops and not _near_historical(line, mo.start(), mo.end()):
                    findings.append(
                        Finding("M1", "drift", f"{rel}:{ln}",
                                f"Op Stds v{mo.group(1)} cited; canonical is v{ops}")
                    )
            for mo in fm_re.finditer(line):
                if int(mo.group(1)) != fm and not _near_historical(line, mo.start(), mo.end()):
                    findings.append(
                        Finding("M1", "drift", f"{rel}:{ln}",
                                f"Foundation Mission v{mo.group(1)} cited; canonical is v{fm}")
                    )
    return findings


def check_citation_resolves(m: dict[str, Any]) -> list[Finding]:
    """M7 — an `Op Stds §N` citation in current-doctrine prose that resolves to no section.

    Op Stds numbering is append-only (manifest: "no cited section renumbered"), so
    §1..§max_section all exist; a citation above max_section (or below 1) points at a
    section that does NOT exist — a typo or a forward-reference to unbuilt doctrine
    (e.g. a stray ``Op Stds §99``). This is the "citation-resolves-nowhere" leg of the
    cross-repo drift guard, and it works in a fresh CI clone (no ~/its-blueprint) because
    the section ceiling is a manifest fact, not a blueprint read.

    Anchored on the ``Op Stds``/``Operational Standards`` prefix (optionally with the
    ``vNN``) so bare ``§N`` tokens are not matched. A ``§§43-49`` range or a ``§3.1``
    subsection yields the FIRST number (43, 3) — undercounting is safe (never a false
    positive). FM is intentionally out of scope: it is structured as Invariants, not
    §-numbered sections. Skips a match framed as historical.
    """
    ops_spec = m["doctrine_versions"]["operational_standards"]
    max_section = ops_spec.get("max_section")
    if max_section is None:
        return []  # manifest hasn't declared the §-ceiling — nothing to resolve against
    ceiling = int(max_section)
    cite_re = re.compile(r"(?:Op Stds|Operational Standards)(?:\s+v\d+)?\s+§+\s*(\d+)")
    findings: list[Finding] = []
    for f in _current_doctrine_files():
        rel = f.relative_to(REPO_ROOT)
        for ln, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            for mo in cite_re.finditer(line):
                n = int(mo.group(1))
                if (n < 1 or n > ceiling) and not _near_historical(line, mo.start(), mo.end()):
                    findings.append(
                        Finding("M7", "drift", f"{rel}:{ln}",
                                f"Op Stds §{n} cited but resolves nowhere "
                                f"(valid sections are §1..§{ceiling})")
                    )
    return findings


def check_stale_tech_debt(m: dict[str, Any]) -> list[Finding]:
    """M2 — tech_debt entry whose body explicitly asserts its OWN completion while
    the header status is still OPEN.

    Precise: requires an explicit self-closure marker — a bold ``**Closed/Fixed/
    Resolved/…**`` label (the repo's own closure convention), "now fixed/closed",
    "this is/was fixed", or "fixed in PR #N". Merely mentioning another PR or the
    word "shipped" about adjacent work does NOT match (that looser heuristic
    produced only false positives in calibration — e.g. the legitimately-deferred
    Watchdog Check E entry).
    """
    td = REPO_ROOT / "docs" / "tech_debt.md"
    if not td.exists():
        return []
    header_re = re.compile(r"^##\s+(.*?)\s+\[(\w+)\s+\d{4}-\d{2}-\d{2}\]")
    closure_re = re.compile(
        r"\*\*(closed|fixed|resolved|done|delivered)\b"
        r"|\b(now|already)\s+(fixed|closed|resolved|done|merged|completed)\b"
        r"|\bthis (?:is|was|has been)\s+(?:now\s+)?(?:fixed|closed|resolved|done|merged|completed)\b"
        r"|\b(?:fixed|closed|resolved|completed)\s+in\s+PR\s*#\d+\b",
        re.I,
    )
    findings: list[Finding] = []
    cur: tuple[int, str, str, list[str]] | None = None
    entries: list[tuple[int, str, str, list[str]]] = []
    for ln, line in enumerate(td.read_text().splitlines(), 1):
        hm = header_re.match(line)
        if hm:
            if cur is not None:
                entries.append(cur)
            cur = (ln, hm.group(1), hm.group(2), [])
        elif cur is not None:
            cur[3].append(line)
    if cur is not None:
        entries.append(cur)
    for ln, title, status, body in entries:
        if status == "OPEN" and closure_re.search(" ".join(body)):
            findings.append(
                Finding("M2", "drift", f"docs/tech_debt.md:{ln}",
                        f"'{title}' is [OPEN] but body asserts completion")
            )
    return findings


def check_sheet_ids(m: dict[str, Any]) -> list[Finding]:
    """M4 — canonical sheet IDs in shared/sheet_ids.py match the manifest."""
    sheet_ids = REPO_ROOT / "shared" / "sheet_ids.py"
    if not sheet_ids.exists():
        return []
    text = sheet_ids.read_text()
    findings: list[Finding] = []
    for const, spec in m["canonical_sheets"].items():
        expected = int(spec["id"])
        mo = re.search(rf"^{re.escape(const)}\s*=\s*(\d+)", text, re.M)
        if mo is None:
            findings.append(Finding("M4", "drift", "shared/sheet_ids.py", f"{const} not found"))
        elif int(mo.group(1)) != expected:
            findings.append(
                Finding("M4", "drift", "shared/sheet_ids.py",
                        f"{const}={mo.group(1)} but manifest says {expected}")
            )
    return findings


def check_workstream_coverage(m: dict[str, Any]) -> list[Finding]:
    """M5 — manifest workstream slugs with no execution-repo acknowledgment.

    Reported as COVERAGE, not drift: a planning-only workstream with no exec
    code is correctly-unbuilt. The semantic tier classifies each.
    """
    hay = ""
    for rel in ["docs/operations/doc_conventions.md", "CLAUDE.md"]:
        p = REPO_ROOT / rel
        if p.exists():
            hay += p.read_text(errors="replace")
    findings: list[Finding] = []
    for slug in m["workstreams"]["slugs"]:
        variants = {slug, slug.replace("_", "-"), slug.replace("_", "")}
        if not any(v in hay for v in variants):
            findings.append(
                Finding("M5", "coverage", "exec repo",
                        f"workstream '{slug}' has no exec-repo mention "
                        "(semantic tier: correctly-unbuilt vs drift?)")
            )
    return findings


def check_section42(m: dict[str, Any]) -> list[Finding]:
    """M3 — §42 four-heading docstring presence (coverage, opportunistic §14)."""
    headings = m["section_42"]["required_headings"]
    head_re = re.compile(r"^(" + "|".join(re.escape(h) for h in headings) + r")$", re.M)
    targets = [p for p in sorted((REPO_ROOT / "shared").glob("*.py")) if p.name != "__init__.py"]
    targets += [REPO_ROOT / ep for ep in ENTRYPOINTS if (REPO_ROOT / ep).exists()]
    findings: list[Finding] = []
    for p in targets:
        present = len(set(head_re.findall(p.read_text(errors="replace"))))
        if present < len(headings):
            findings.append(
                Finding("M3", "coverage", str(p.relative_to(REPO_ROOT)),
                        f"§42: {present}/{len(headings)} headings (opportunistic §14 retrofit)")
            )
    return findings


def check_module_docstring_versions(m: dict[str, Any]) -> list[Finding]:
    """M6 — non-canonical Op Stds / FM version citations in shared/* + safety_reports/* MODULE
    docstrings (coverage; the semantic tier classifies).

    A module's docstring is where it declares its doctrine framing, so a non-canonical version
    there is a candidate stale citation. Reported at COVERAGE severity, NOT drift: most version
    mentions in docstrings are correct historical ATTRIBUTIONS ("the discipline added in Op Stds
    v13 §42"), which can't be distinguished from stale-current cites mechanically — so this
    surfaces candidates for the semantic (opus) tier + the operator, and never false-alarms the
    drift count. Skips a match near a historical marker (_near_historical) and a "vN §M"
    section-attribution pattern (almost always historical-safe).
    """
    dv = m["doctrine_versions"]
    ops = int(dv["operational_standards"]["current"])
    fm = int(dv["foundation_mission"]["current"])
    ops_re = re.compile(r"(?:Op Stds|Operational Standards)\s+v(\d+)")
    fm_re = re.compile(r"(?:Foundation Mission|FM)\s+v(\d+)")
    attribution_re = re.compile(r"^\s*§")  # "vN §M" — a section attribution, historical-safe
    targets = sorted((REPO_ROOT / "shared").glob("*.py")) + sorted(
        (REPO_ROOT / "safety_reports").glob("*.py")
    )
    findings: list[Finding] = []
    for p in targets:
        if p.name == "__init__.py":
            continue
        try:
            doc = ast.get_docstring(ast.parse(p.read_text(errors="replace")))
        except (SyntaxError, ValueError):
            continue
        if not doc:
            continue
        for ln, line in enumerate(doc.splitlines(), 1):
            for canon, rx, label in ((ops, ops_re, "Op Stds"), (fm, fm_re, "Foundation Mission")):
                for mo in rx.finditer(line):
                    if int(mo.group(1)) == canon or _near_historical(line, mo.start(), mo.end()):
                        continue
                    if attribution_re.match(line[mo.end():]):
                        continue
                    findings.append(
                        Finding(
                            "M6", "coverage",
                            f"{p.relative_to(REPO_ROOT)} (docstring ~L{ln})",
                            f"{label} v{mo.group(1)} in module docstring; canonical is v{canon} "
                            "(candidate stale cite — semantic tier / operator classifies)",
                        )
                    )
    return findings


# ---- M8: is the MANIFEST ITSELF current? (local mode only) --------------------
#
# Audit 2026-08-16 H-5, verbatim: "the single artifact that determines whether
# every other doctrine claim is current is the one artifact with no automated
# freshness check." Every other check in this file validates the repo AGAINST the
# manifest. Nothing validated the manifest. Both its head pins were stale at audit
# time and had been for weeks, and no surface said so.
#
# WHY CI STRUCTURALLY CANNOT DO THIS. The GitHub runner checks out ~/its alone;
# ../its-blueprint is absent, which is the entire reason the manifest lives in
# this repo (see its own HOME DECISION block). So in CI this check emits a
# COVERAGE line saying it could not run — visible, rather than a silent skip,
# because a check that quietly no-ops is indistinguishable from a passing one.
#
# WHAT IT COMPARES, AND WHAT IT DELIBERATELY DOES NOT.
#   * `version:` frontmatter of each blueprint doctrine file vs the manifest's
#     recorded `current` -> DRIFT, and BLOCKING under --strict. This is the fact
#     every downstream claim is derived from, and it is FP-free: either the live
#     file says 21 and the manifest says 21, or someone bumped doctrine without
#     syncing.
#   * `meta.blueprint_head` vs the blueprint's live HEAD -> COVERAGE only, never
#     blocking. The blueprint's HEAD advances on every session log, and most such
#     commits touch no doctrine at all. Gating on it would red-line the operator's
#     local --strict several times a week for nothing — the alarm-fatigue failure
#     (§57) that produced several of this audit's other findings. The pointer is
#     worth REPORTING stale; it is not worth blocking on.
BLUEPRINT_ROOT = REPO_ROOT.parent / "its-blueprint"


def _blueprint_frontmatter_version(path: Path) -> int | None:
    """Read `version: N` from a doctrine file's YAML frontmatter, or None."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    _, _, rest = text.partition("---")
    body, _, _ = rest.partition("\n---")
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    version = (data or {}).get("version") if isinstance(data, dict) else None
    return version if isinstance(version, int) else None


def _blueprint_head() -> str | None:
    """Short HEAD sha of the sibling blueprint checkout, or None if unreadable."""
    import subprocess  # noqa: PLC0415 — local-mode only; keeps the CI path import-free

    try:
        out = subprocess.run(
            ["git", "-C", str(BLUEPRINT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def check_manifest_freshness(m: dict[str, Any]) -> list[Finding]:
    """M8 — validate the manifest against the LIVE blueprint (local mode only)."""
    findings: list[Finding] = []
    rel = "docs/doctrine_manifest.yaml"

    if not BLUEPRINT_ROOT.is_dir():
        return [
            Finding(
                "M8", "coverage", rel,
                f"blueprint not present at {BLUEPRINT_ROOT} — manifest currency NOT "
                "checked in this run (CI mode). The manifest is validated against live "
                "doctrine only in local mode, where ../its-blueprint exists.",
            )
        ]

    for name, entry in (m.get("doctrine_versions") or {}).items():
        source = str(entry.get("source", ""))
        if not source.startswith("../its-blueprint/"):
            continue
        if entry.get("source_field") != "frontmatter.version":
            continue
        path = REPO_ROOT.parent / source[len("../") :]
        if not path.exists():
            findings.append(
                Finding("M8", "drift", rel,
                        f"{name}.source points at {source}, which does not exist in the "
                        "live blueprint — the manifest names a file that moved or was renamed")
            )
            continue
        live = _blueprint_frontmatter_version(path)
        recorded = entry.get("current")
        if live is None:
            findings.append(
                Finding("M8", "coverage", rel,
                        f"{name}: could not read `version:` frontmatter from {source}")
            )
        elif live != recorded:
            findings.append(
                Finding("M8", "drift", rel,
                        f"{name}: manifest records current={recorded}, live blueprint "
                        f"frontmatter says version={live}. Every downstream doctrine claim "
                        f"derives from this number — sync the manifest (and any stale "
                        f"v{recorded} citations) before trusting a clean run.")
            )

    recorded_head = str((m.get("meta") or {}).get("blueprint_head", "")).strip()
    live_head = _blueprint_head()
    if live_head is None:
        findings.append(
            Finding("M8", "coverage", rel,
                    f"blueprint present at {BLUEPRINT_ROOT} but its git HEAD is unreadable "
                    "— head-pin freshness not checked")
        )
    elif recorded_head and not live_head.startswith(recorded_head[:7]):
        findings.append(
            Finding("M8", "coverage", rel,
                    f"meta.blueprint_head={recorded_head} but the live blueprint HEAD is "
                    f"{live_head}. Informational, never blocking: the blueprint's HEAD moves "
                    f"on every session log and most such commits touch no doctrine. Refresh "
                    f"the pin on the next doctrine sync.")
        )

    return findings


def run_all() -> list[Finding]:
    m = _load_manifest()
    findings: list[Finding] = []
    findings += check_version_drift(m)
    findings += check_citation_resolves(m)
    findings += check_stale_tech_debt(m)
    findings += check_sheet_ids(m)
    findings += check_workstream_coverage(m)
    findings += check_section42(m)
    findings += check_module_docstring_versions(m)
    findings += check_manifest_freshness(m)
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mechanical doctrine-drift checks (propose-only).")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit 1 if any BLOCKING drift exists (M1 version / M4 sheet-id / M7 citation). "
            "The CI gate. Default stays exit-0 propose-only for the agent + unit tests."
        ),
    )
    args = ap.parse_args(argv)

    findings = run_all()
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
        return 0

    drift = [f for f in findings if f.severity == "drift"]
    coverage = [f for f in findings if f.severity == "coverage"]
    print(f"doc-reconciliation mechanical tier — {len(drift)} drift, "
          f"{len(coverage)} coverage  (PROPOSE-ONLY; writes nothing)\n")
    print("DRIFT (mechanical, high-precision):")
    if drift:
        for f in drift:
            print(f"  [{f.check}] {f.location} — {f.detail}")
    else:
        print("  none")
    print("\nCOVERAGE (informational; semantic tier classifies):")
    if coverage:
        for f in coverage:
            print(f"  [{f.check}] {f.location} — {f.detail}")
    else:
        print("  none")
    print("\nMechanical tier only. The semantic (opus) tier + the operator decide; "
          "this script never writes.")

    if args.strict:
        # Severity is part of the gate, not just the check id. A `coverage`
        # finding is informational BY DEFINITION — filtering on the id alone made
        # M8's deliberately-non-blocking head-pin note fail --strict, which is the
        # narrated-not-enforced shape (a comment reading "never blocking" sitting
        # over code that blocks) this whole change exists to remove. Caught by
        # running it. M1/M4/M7 emit only `drift`, so this is a no-op for them.
        blocking = [
            f for f in findings
            if f.check in STRICT_BLOCKING_CHECKS and f.severity == "drift"
        ]
        if blocking:
            print(
                f"\nSTRICT: {len(blocking)} BLOCKING drift finding(s) "
                f"({'/'.join(sorted(STRICT_BLOCKING_CHECKS))}) — failing CI:"
            )
            for f in blocking:
                print(f"  [{f.check}] {f.location} — {f.detail}")
            return 1
        print(
            f"\nSTRICT: no blocking drift "
            f"({'/'.join(sorted(STRICT_BLOCKING_CHECKS))} clean). M2/coverage are informational."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
