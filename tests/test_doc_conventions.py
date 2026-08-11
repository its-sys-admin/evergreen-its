"""Unit tests for scripts/lint_doc_conventions.py.

Tests run against tmp_path fixtures so the real docs/ tree is not touched.
Uses sys.path-driven import to avoid the mypy duplicate-module error
(see tests/test_watchdog.py for the original pattern).
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import lint_doc_conventions as lint_mod  # noqa: E402
from lint_doc_conventions import (  # noqa: E402
    CANONICAL_STATUS,
    CANONICAL_TYPES,
    CANONICAL_WORKSTREAMS,
    lint_file,
    main,
)


def _write_doc(
    path: Path,
    *,
    doc_type: str = "session_log",
    date: str = "2026-05-24",
    status: str = "closed",
    workstream: str = "docs",
    include_frontmatter: bool = True,
    title: str = "Test doc",
) -> None:
    """Write a fixture doc with optional frontmatter."""
    if include_frontmatter:
        frontmatter = textwrap.dedent(
            f"""\
            ---
            type: {doc_type}
            date: {date}
            status: {status}
            workstream: {workstream}
            ---
            """
        )
    else:
        frontmatter = ""
    body = f"# {title}\n\nBody.\n"
    if doc_type == "session_log":
        # Session logs must carry the four-part verify block (WP5 convention).
        body += (
            "\n## Landing verify\n"
            "- pytest: 1 passed\n"
            "- mypy: 0 errors\n"
            "- ruff: clean\n"
            "- main-branch CI on merge commit: SUCCESS\n"
        )
    path.write_text(frontmatter + body)


# ---- Module-level invariants -------------------------------------------


def test_canonical_types_match_spec():
    """Spec in docs/operations/doc_conventions.md must match the code constants."""
    expected = {
        "session_log",
        "brief",
        "audit",
        "report",
        "operations",
        "reference",
        "sample",
        "readme",
    }
    assert set(CANONICAL_TYPES) == expected


def test_canonical_status_match_spec():
    expected = {"draft", "active", "superseded", "archived", "closed"}
    assert set(CANONICAL_STATUS) == expected


def test_canonical_workstreams_match_spec():
    expected = {
        "safety_reports",
        "safety_portal",
        "progress_reports",
        "field_ops",
        "po_materials",
        "subcontracts",
        "operator_dashboard",
        "box",
        "ci",
        "security",
        "docs",
        "infrastructure",
    }
    assert set(CANONICAL_WORKSTREAMS) == expected


# ---- lint_file ----------------------------------------------------------


def test_lint_clean_doc_no_violations(tmp_path: Path, monkeypatch):
    """A well-formed doc lints clean."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-05-24_thing.md"
    _write_doc(doc)
    violations = lint_file(doc.relative_to(tmp_path))
    assert violations == []


def test_lint_missing_frontmatter_new_doc(tmp_path: Path, monkeypatch):
    """A NEW doc (date > grandfather) without frontmatter is a violation."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    # Filename date is post-grandfather (2026-05-24 is the cutoff;
    # 2026-06-01 > 2026-05-24)
    doc = docs_dir / "2026-06-01_new_thing.md"
    _write_doc(doc, include_frontmatter=False)
    violations = lint_file(doc.relative_to(tmp_path))
    rules = [v.rule for v in violations]
    assert "frontmatter-required" in rules


def test_lint_missing_frontmatter_grandfathered_doc(tmp_path: Path, monkeypatch):
    """A pre-grandfather-date doc without frontmatter is permitted (lazy retrofit)."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    # 2026-05-17 < 2026-05-24 (grandfather date)
    doc = docs_dir / "2026-05-17_old_thing.md"
    _write_doc(doc, include_frontmatter=False)
    violations = lint_file(doc.relative_to(tmp_path))
    assert violations == []


def test_lint_unknown_type(tmp_path: Path, monkeypatch):
    """type=bogus is a violation."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-06-01_bogus.md"
    _write_doc(doc, doc_type="bogus")
    violations = lint_file(doc.relative_to(tmp_path))
    rules = [v.rule for v in violations]
    assert "type-canonical" in rules


def test_lint_unknown_status(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-06-01_thing.md"
    _write_doc(doc, status="pending-review")
    violations = lint_file(doc.relative_to(tmp_path))
    rules = [v.rule for v in violations]
    assert "status-canonical" in rules


def test_lint_unknown_workstream(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-06-01_thing.md"
    _write_doc(doc, workstream="not-a-real-workstream")
    violations = lint_file(doc.relative_to(tmp_path))
    rules = [v.rule for v in violations]
    assert "workstream-canonical" in rules


def test_lint_missing_required_field_workstream(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-06-01_thing.md"
    doc.write_text(
        "---\ntype: session_log\ndate: 2026-06-01\nstatus: closed\n---\n# X\n"
    )
    violations = lint_file(doc.relative_to(tmp_path))
    rules = [v.rule for v in violations]
    assert "workstream-required" in rules


def test_lint_session_log_missing_date(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-06-01_thing.md"
    doc.write_text(
        "---\ntype: session_log\nstatus: closed\nworkstream: docs\n---\n# X\n"
    )
    violations = lint_file(doc.relative_to(tmp_path))
    rules = [v.rule for v in violations]
    assert "date-required" in rules


def test_lint_session_log_missing_verify_block(tmp_path: Path, monkeypatch):
    """A NEW session log without the four-part verify block flags (WP5)."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-07-12_thing.md"
    doc.write_text(
        "---\ntype: session_log\ndate: 2026-07-12\nstatus: closed\nworkstream: docs\n---\n"
        "# X\n\nDid work. No verify block.\n"
    )
    rules = [v.rule for v in lint_file(doc.relative_to(tmp_path))]
    assert "session-log-verify-block" in rules


def test_lint_plans_citation_flagged(tmp_path: Path, monkeypatch):
    """A doc citing ~/.claude/plans/ as authoritative flags (WP5)."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "operations"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "cites-plans.md"
    doc.write_text(
        "---\ntype: operations\nstatus: active\nworkstream: docs\n---\n"
        "# X\n\nThe authoritative spec is `~/.claude/plans/foo.md`.\n"
    )
    rules = [v.rule for v in lint_file(doc.relative_to(tmp_path))]
    assert "plans-citation" in rules


# ---- Exempt list --------------------------------------------------------


def test_lint_exempts_top_level_readme(tmp_path: Path, monkeypatch):
    """CLAUDE.md and README.md are exempt from frontmatter requirements."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# CLAUDE.md\n\nNo frontmatter here.\n")
    readme = tmp_path / "README.md"
    readme.write_text("# README.md\n\nNo frontmatter here.\n")
    assert lint_file(Path("CLAUDE.md")) == []
    assert lint_file(Path("README.md")) == []


def test_lint_exempts_tech_debt_accumulator(tmp_path: Path, monkeypatch):
    """docs/tech_debt.md is the accumulator, exempt from frontmatter."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    td = docs_dir / "tech_debt.md"
    td.write_text("# Tech Debt\n\nNo frontmatter here.\n")
    assert lint_file(Path("docs/tech_debt.md")) == []


def test_lint_exempts_index_readmes(tmp_path: Path, monkeypatch):
    """Every README.md in any subdirectory is exempt (auto-generated)."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    sub = tmp_path / "docs" / "audits"
    sub.mkdir(parents=True)
    readme = sub / "README.md"
    readme.write_text("# Audits\n\nIndex.\n")
    assert lint_file(Path("docs/audits/README.md")) == []


def test_lint_exempts_docs_agents(tmp_path: Path, monkeypatch):
    """docs/agents/*.md follow the mattpocock/skills agent-OS convention, not
    the ITS doc schema — exempt (same rationale as prompts/ direct children)."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    for name in ("issue-tracker.md", "triage-labels.md", "domain.md"):
        (agents / name).write_text(f"# {name}\n\nNo frontmatter (upstream convention).\n")
        assert lint_file(Path(f"docs/agents/{name}")) == []
    # Scoping guard: a non-agents doc without frontmatter MUST still flag.
    other = tmp_path / "docs" / "operations"
    other.mkdir(parents=True)
    (other / "thing.md").write_text("# thing\n\nNo frontmatter.\n")
    assert any(v.rule == "frontmatter-required" for v in lint_file(Path("docs/operations/thing.md")))


# ---- main() / CLI --------------------------------------------------------


def test_main_warn_only_default_exits_zero(tmp_path: Path, monkeypatch):
    """Warn-only mode exits 0 even with violations."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-06-01_thing.md"
    _write_doc(doc, include_frontmatter=False)  # new doc, missing fm
    rc = main(["--paths", "docs"])
    assert rc == 0


def test_main_strict_mode_exits_nonzero_on_violations(tmp_path: Path, monkeypatch):
    """Strict mode exits 1 on any violation."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-06-01_thing.md"
    _write_doc(doc, include_frontmatter=False)
    rc = main(["--strict", "--paths", "docs"])
    assert rc == 1


def test_main_clean_tree_strict_exits_zero(tmp_path: Path, monkeypatch):
    """Strict mode on a clean tree exits 0."""
    monkeypatch.setattr(lint_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    doc = docs_dir / "2026-06-01_thing.md"
    _write_doc(doc)  # full frontmatter
    rc = main(["--strict", "--paths", "docs"])
    assert rc == 0


def test_walk_docs_lints_a_checkout_living_under_a_dot_directory(tmp_path: Path, monkeypatch):
    """A repo root under a dot-directory must still be linted.

    `walk_docs` used to test `path.parts` on the ABSOLUTE path. `REPO_ROOT` is absolute, so a
    checkout at `.claude/worktrees/<id>/` made `.claude` a component of every file's path — the
    hidden-file filter matched all of them, the linter walked zero documents, and it printed
    "no violations". That is exactly where the workflow tooling puts agent worktrees, so the
    gate reported green from a worktree while the identical commit reported 89 warnings from
    the normal checkout.

    The filter belongs on the path RELATIVE to the root: a dot-directory INSIDE docs/ is still
    correctly skipped (asserted below), a dot-directory ABOVE the root is not our business.
    """
    root = tmp_path / ".claude" / "worktrees" / "wf_abc-1"
    docs_dir = root / "docs" / "session_logs"
    docs_dir.mkdir(parents=True)
    _write_doc(docs_dir / "2026-06-01_thing.md")
    # A genuinely hidden path INSIDE the tree must still be skipped.
    hidden = root / "docs" / ".drafts"
    hidden.mkdir(parents=True)
    _write_doc(hidden / "2026-06-01_wip.md")

    monkeypatch.setattr(lint_mod, "REPO_ROOT", root)
    found = lint_mod.walk_docs([root / "docs"])

    assert Path("docs/session_logs/2026-06-01_thing.md") in found, (
        "a checkout under a dot-directory linted NOTHING — the gate passes by finding no work"
    )
    assert not any(".drafts" in str(p) for p in found), "hidden dirs inside the tree must stay skipped"
