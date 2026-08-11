"""Lint markdown docs against the conventions defined in
`docs/operations/doc_conventions.md`.

Checks each doc for:

  1. Frontmatter present (required for dated / status-bearing types)
  2. Required frontmatter fields populated
  3. `type` in the canonical set
  4. `workstream` in the canonical taxonomy
  5. `status` in the canonical set
  6. Filename matches naming convention for the type
  7. (Soft) required section headers per type — warn only

Warn-only by default during the retrofit window. Strict mode (`--strict`)
exits non-zero on any violation; flip is the post-retrofit follow-on.

The exempt list covers entry-point docs and the tech-debt accumulator:

    CLAUDE.md
    README.md
    docs/tech_debt.md
    docs/**/README.md   (every subdirectory README — auto-generated index)
    prompts/<name>.md   (direct children — prompt-specific frontmatter convention)
    docs/agents/*.md    (mattpocock/skills agent-OS config — upstream convention)

The grandfather list — docs that pre-date this PR and are explicitly
permitted to lack frontmatter until they're touched for unrelated
reasons — is computed at runtime from `git log` (docs whose newest
commit predates this PR's merge SHA aren't required to conform). For
the initial ship the grandfather check is approximated: any doc
without frontmatter that lives in `docs/session_logs/`, `docs/audits/`,
`docs/reports/`, or `docs/references/` and is older than 2026-05-24
is grandfathered. New docs MUST conform.

CLI
---

    python -m scripts.lint_doc_conventions
    python -m scripts.lint_doc_conventions --strict
    python -m scripts.lint_doc_conventions --paths docs/operations/

See `docs/operations/doc_conventions.md` for the source-of-truth spec.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parent.parent

# Canonical taxonomies — must match docs/operations/doc_conventions.md.
CANONICAL_TYPES = frozenset({
    "session_log",
    "brief",
    "audit",
    "report",
    "operations",
    "reference",
    "sample",
    "readme",
})

CANONICAL_STATUS = frozenset({
    "draft",
    "active",
    "superseded",
    "archived",
    "closed",
})

CANONICAL_WORKSTREAMS = frozenset({
    "safety_reports",
    "safety_portal",
    "progress_reports",   # Progress Reporting workstream (P2+)
    "field_ops",          # field-ops portal capture + job-tracker→Active-Jobs SoR mirror
    "po_materials",       # Purchase-Order workstream (WS1; deterministic po_generate/po_poll/po_send)
    "subcontracts",       # Subcontract-package generation (ADR-0003, deterministic)
    "operator_dashboard", # WS2 operator dashboard (localhost obs + PIN-gated ACT surface)
    "box",
    "ci",
    "security",
    "docs",
    "infrastructure",
})  # `null` is also valid (as Python None in YAML). Keep in sync with the table in
# docs/operations/doc_conventions.md (#workstream-taxonomy) AND doctrine_manifest.yaml
# workstream_tags — this frozenset is the THIRD copy of that taxonomy (multi-surface).

# Types that require a `date` field.
DATE_REQUIRED_TYPES = frozenset({"session_log", "brief", "report"})

# Files explicitly exempt from frontmatter. Path strings relative to REPO_ROOT.
EXEMPT_FILES = frozenset({
    "CLAUDE.md",
    "README.md",
    "docs/tech_debt.md",
})

# Subdirectory README.md files are exempt regardless of where they live.
def _is_exempt_readme(rel_path: Path) -> bool:
    return rel_path.name == "README.md"


def _is_exempt_prompt(rel_path: Path) -> bool:
    """Direct children of `prompts/` (NOT prompts/samples/) follow the
    prompt-specific frontmatter convention (`name / version / model / notes`)
    documented in `prompts/README.md`. They're exempt from the canonical
    doc-conventions frontmatter."""
    parts = rel_path.parts
    return len(parts) == 2 and parts[0] == "prompts" and parts[1].endswith(".md")


def _is_exempt_agents(rel_path: Path) -> bool:
    """Files under `docs/agents/` follow the mattpocock/skills agent-OS
    convention (issue-tracker / triage-labels / domain config consumed by the
    installed skills), not the ITS doc-conventions schema — same rationale as
    the `prompts/` direct-children carve-out. See CLAUDE.md "## Agent skills"."""
    parts = rel_path.parts
    return len(parts) >= 2 and parts[0] == "docs" and parts[1] == "agents" and rel_path.name.endswith(".md")


def _is_exempt(rel_path: Path) -> bool:
    if str(rel_path) in EXEMPT_FILES:
        return True
    if _is_exempt_readme(rel_path):
        return True
    if _is_exempt_prompt(rel_path):
        return True
    return _is_exempt_agents(rel_path)


# Grandfather date — docs created/modified before this date don't require
# frontmatter (lazy retrofit per doc_conventions.md). New docs after this
# date MUST conform.
GRANDFATHER_DATE = "2026-05-24"


@dataclass(frozen=True)
class LintViolation:
    """One lint finding. `severity` tags whether strict mode exits on it."""
    path: Path
    rule: str
    severity: str  # "error" | "warn"
    message: str


def _parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return (parsed_dict_or_None, parse_error_or_None)."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None, "missing frontmatter delimiter at file start"
    match = re.search(r"^---\s*$", text[4:], flags=re.MULTILINE)
    if match is None:
        return None, "missing closing frontmatter delimiter"
    yaml_block = text[4 : 4 + match.start()]
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        return None, f"YAML parse error: {exc!r}"
    if not isinstance(parsed, dict):
        return None, "frontmatter is not a YAML mapping"
    return parsed, None


def _doc_likely_grandfathered(rel_path: Path) -> bool:
    """Approximation: a doc with date-prefix filename before GRANDFATHER_DATE
    is treated as grandfathered. Evergreen docs (no date prefix) created
    before GRANDFATHER_DATE are also grandfathered via filesystem mtime check.
    """
    # Filename date prefix (`YYYY-MM-DD_…`)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})_", rel_path.name)
    if match is not None:
        return match.group(1) < GRANDFATHER_DATE

    # Evergreen docs — check git mtime if available, else filesystem mtime
    # Conservative: treat as grandfathered if it exists pre-this-session
    # via mtime check. Slightly imperfect (a touched-but-unmodified file
    # would look new) but acceptable for the lazy-retrofit policy.
    try:
        import time
        mtime_str = time.strftime(
            "%Y-%m-%d", time.gmtime((REPO_ROOT / rel_path).stat().st_mtime)
        )
        return mtime_str < GRANDFATHER_DATE
    except OSError:
        return False


def lint_file(rel_path: Path) -> list[LintViolation]:
    """Lint one markdown file. Returns a list of violations.

    Exempt files return []. Grandfathered files return [] for the "missing
    frontmatter" violation but still get section-header warns if applicable.
    """
    if _is_exempt(rel_path):
        return []

    abs_path = REPO_ROOT / rel_path
    text = abs_path.read_text()
    fm, parse_error = _parse_frontmatter(text)

    violations: list[LintViolation] = []

    if fm is None:
        if _doc_likely_grandfathered(rel_path):
            return []  # grandfathered; no violation
        violations.append(
            LintViolation(
                path=rel_path,
                rule="frontmatter-required",
                severity="error",
                message=parse_error or "frontmatter missing or unparseable",
            )
        )
        return violations  # no point checking field semantics with no frontmatter

    # Validate `type`
    doc_type = fm.get("type")
    if not isinstance(doc_type, str) or doc_type not in CANONICAL_TYPES:
        violations.append(
            LintViolation(
                path=rel_path,
                rule="type-canonical",
                severity="error",
                message=(
                    f"type={doc_type!r} not in canonical set "
                    f"{sorted(CANONICAL_TYPES)}"
                ),
            )
        )

    # Validate `status`
    status = fm.get("status")
    if not isinstance(status, str) or status not in CANONICAL_STATUS:
        violations.append(
            LintViolation(
                path=rel_path,
                rule="status-canonical",
                severity="error",
                message=(
                    f"status={status!r} not in canonical set "
                    f"{sorted(CANONICAL_STATUS)}"
                ),
            )
        )

    # Validate `workstream` (None / null is allowed)
    workstream = fm.get("workstream", "MISSING")
    if workstream == "MISSING":
        violations.append(
            LintViolation(
                path=rel_path,
                rule="workstream-required",
                severity="error",
                message="missing required field 'workstream' (use null for cross-cutting)",
            )
        )
    elif workstream is not None and (
        not isinstance(workstream, str) or workstream not in CANONICAL_WORKSTREAMS
    ):
        violations.append(
            LintViolation(
                path=rel_path,
                rule="workstream-canonical",
                severity="error",
                message=(
                    f"workstream={workstream!r} not in canonical set "
                    f"{sorted(CANONICAL_WORKSTREAMS)} or null"
                ),
            )
        )

    # Validate `date` (required for time-bound types)
    if doc_type in DATE_REQUIRED_TYPES:
        date_val = fm.get("date")
        if date_val is None:
            violations.append(
                LintViolation(
                    path=rel_path,
                    rule="date-required",
                    severity="error",
                    message=(
                        f"type={doc_type!r} requires a 'date' field "
                        f"(YYYY-MM-DD)"
                    ),
                )
            )

    # Validate filename convention
    filename_violation = _check_filename(rel_path, doc_type)
    if filename_violation is not None:
        violations.append(filename_violation)

    # Session-log four-part verify block (landing evidence)
    verify_block_violation = _check_session_log_verify_block(rel_path, doc_type, text)
    if verify_block_violation is not None:
        violations.append(verify_block_violation)

    # Ephemeral ~/.claude/plans/ citation
    plans_violation = _check_plans_citation(rel_path, text)
    if plans_violation is not None:
        violations.append(plans_violation)

    return violations


def _check_filename(rel_path: Path, doc_type: str | None) -> LintViolation | None:
    """Filename convention check."""
    name = rel_path.name
    date_prefix = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)

    # Time-bound types should have date prefix
    if doc_type in DATE_REQUIRED_TYPES and date_prefix is None:
        return LintViolation(
            path=rel_path,
            rule="filename-date-prefix",
            severity="warn",
            message=(
                f"type={doc_type!r} expects filename `YYYY-MM-DD_topic-slug.md`"
            ),
        )

    # Slug check: lowercase, underscores, no caps, no double-underscore
    stem = name.rsplit(".", 1)[0]
    slug = stem[11:] if date_prefix is not None else stem
    if "__" in slug:
        return LintViolation(
            path=rel_path,
            rule="filename-slug-double-underscore",
            severity="warn",
            message="slug contains `__`; use single underscore between words",
        )
    if slug != slug.lower():
        return LintViolation(
            path=rel_path,
            rule="filename-slug-case",
            severity="warn",
            message=f"slug should be lowercase; found {slug!r}",
        )
    return None


# Markers of the canonical four-part verify block (docs/session_logs/README.md):
# pytest / mypy / ruff / main-branch CI on the merge commit.
VERIFY_BLOCK_MARKERS = ("pytest", "mypy", "ruff", "main-branch CI")

# Ephemeral planning-file path that must NOT be cited as authoritative in a
# committed doc — a fresh CC session cannot reach `~/.claude/plans/`.
_PLANS_CITATION = re.compile(r"\.claude/plans/")


def _strip_frontmatter(text: str) -> str:
    """Return the body — everything after the closing frontmatter delimiter."""
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return text
    match = re.search(r"^---\s*$", text[4:], flags=re.MULTILINE)
    return text[4 + match.end():] if match is not None else text


def _check_session_log_verify_block(
    rel_path: Path, doc_type: str | None, text: str
) -> LintViolation | None:
    """A session log must carry the four-part verify block (pytest / mypy / ruff /
    main-branch CI on the merge commit) — the landing evidence a fresh session relies
    on. Warn-only; grandfathered logs are skipped."""
    if doc_type != "session_log" or _doc_likely_grandfathered(rel_path):
        return None
    body = _strip_frontmatter(text).lower()
    missing = [m for m in VERIFY_BLOCK_MARKERS if m.lower() not in body]
    if missing:
        return LintViolation(
            path=rel_path,
            rule="session-log-verify-block",
            severity="warn",
            message=(
                f"session log missing four-part verify marker(s) {missing} "
                "(pytest / mypy / ruff / main-branch CI on merge commit; "
                "see docs/session_logs/README.md)"
            ),
        )
    return None


def _check_plans_citation(rel_path: Path, text: str) -> LintViolation | None:
    """Flag a committed doc that cites `~/.claude/plans/` (an ephemeral, uncommitted
    planning file) as a source of truth — reference the committed doc that superseded
    it instead. Warn-only."""
    if _PLANS_CITATION.search(_strip_frontmatter(text)):
        return LintViolation(
            path=rel_path,
            rule="plans-citation",
            severity="warn",
            message=(
                "cites `~/.claude/plans/` (ephemeral, uncommitted) — reference the "
                "committed doc that superseded it instead (a fresh CC session can't reach it)"
            ),
        )
    return None


def walk_docs(roots: list[Path]) -> list[Path]:
    """Return every `.md` file under the roots, excluding hidden + README.md
    files. README.md files are exempt by the rule above; including them in
    the walk just to skip them later is wasteful.

    The hidden-file test runs on the path RELATIVE to the repo root, never on the
    absolute path. That distinction is the whole bug this shape fixes: `REPO_ROOT`
    is absolute, so in a checkout living under a dot-directory — which is exactly
    where `.claude/worktrees/<id>/` puts every workflow-agent worktree — `.claude`
    is a component of EVERY file's absolute path. The old test matched it, skipped
    every document, and reported "no violations" because it had linted nothing.

    A gate that passes by finding no work is worse than no gate: it is a green
    light that means silence. It reported clean from a workflow worktree while the
    identical commit reported 89 warnings from `~/its`.
    """
    md_files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            rel = path.relative_to(REPO_ROOT)
            if any(part.startswith(".") for part in rel.parts):
                continue
            md_files.append(rel)
    return md_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.lint_doc_conventions",
        description=(
            "Lint markdown docs against ITS doc conventions. Warn-only by "
            "default during the retrofit window; --strict exits non-zero on "
            "any violation. See docs/operations/doc_conventions.md."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on any violation (post-retrofit).",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="Restrict lint to specific paths (defaults to docs/ + prompts/).",
    )
    args = parser.parse_args(argv)

    if args.paths is None:
        roots = [REPO_ROOT / "docs", REPO_ROOT / "prompts"]
    else:
        roots = [REPO_ROOT / p for p in args.paths]

    md_files = walk_docs(roots)
    all_violations: list[LintViolation] = []
    for rel_path in sorted(md_files):
        all_violations.extend(lint_file(rel_path))

    if not all_violations:
        print("Doc-conventions lint: no violations.")
        return 0

    print(f"Doc-conventions lint: {len(all_violations)} violation(s):")
    for v in all_violations:
        prefix = "[strict-error]" if args.strict else "[warn]"
        print(f"  {prefix} {v.path}: {v.rule}: {v.message}")

    if args.strict:
        print(
            "\nStrict mode active — exiting non-zero. See "
            "docs/operations/doc_conventions.md."
        )
        return 1
    print(
        "\nWarn-only mode (retrofit window). Run with --strict to exit "
        "non-zero on violations."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
