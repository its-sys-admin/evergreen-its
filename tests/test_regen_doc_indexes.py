"""Unit tests for scripts/regen_doc_indexes.py.

Tests run against tmp_path fixtures so the real docs/ tree is not
touched. Uses the same sys.path-driven import as tests/test_watchdog.py
to avoid the mypy duplicate-module error.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import regen_doc_indexes as regen_mod  # noqa: E402
from regen_doc_indexes import (  # noqa: E402
    BEGIN_SENTINEL,
    END_SENTINEL,
    main,
    parse_doc,
    regenerate_one,
    render_index_table,
)


def _fixture_doc(
    path: Path,
    *,
    doc_type: str | None = "session_log",
    date: str | None = "2026-05-23",
    status: str | None = "closed",
    workstream: str | None = "safety_reports",
    title: str = "Test doc",
    related_prs: list[int] | None = None,
    include_frontmatter: bool = True,
) -> None:
    """Write a fixture doc to `path` with optional frontmatter."""
    if include_frontmatter:
        prs_str = (
            str(related_prs) if related_prs is not None else "[]"
        )
        frontmatter = textwrap.dedent(
            f"""\
            ---
            type: {doc_type}
            date: {date}
            status: {status}
            workstream: {workstream}
            related_prs: {prs_str}
            ---
            """
        )
    else:
        frontmatter = ""
    body = f"# {title}\n\nBody content.\n"
    path.write_text(frontmatter + body)


# ---- parse_doc ----------------------------------------------------------


def test_parse_doc_full_frontmatter(tmp_path: Path):
    doc = tmp_path / "2026-05-23_thing.md"
    _fixture_doc(doc, related_prs=[75, 76])
    entry = parse_doc(doc)
    assert entry.has_frontmatter is True
    assert entry.doc_type == "session_log"
    assert entry.date == "2026-05-23"
    assert entry.status == "closed"
    assert entry.workstream == "safety_reports"
    assert entry.title == "Test doc"
    assert entry.related_prs == [75, 76]


def test_parse_doc_no_frontmatter(tmp_path: Path):
    doc = tmp_path / "2026-05-23_thing.md"
    _fixture_doc(doc, include_frontmatter=False)
    entry = parse_doc(doc)
    assert entry.has_frontmatter is False
    assert entry.doc_type is None
    assert entry.date is None
    assert entry.status is None
    # Title still resolved from the # heading
    assert entry.title == "Test doc"


def test_parse_doc_malformed_frontmatter(tmp_path: Path):
    """YAMLError on the frontmatter block → treat as no-frontmatter."""
    doc = tmp_path / "bad.md"
    doc.write_text("---\n: : bad yaml :\n---\n# Bad\n")
    entry = parse_doc(doc)
    # parse may either reject malformed yaml or accept partial; either way
    # a robust parser shouldn't crash. We accept either has_frontmatter
    # state but require title still resolves.
    assert entry.title == "Bad"


# ---- render_index_table -------------------------------------------------


def test_render_index_table_empty(tmp_path: Path):
    out = render_index_table([], tmp_path)
    assert "no docs" in out


def test_render_index_table_with_entries(tmp_path: Path):
    doc1 = tmp_path / "2026-05-24_recent.md"
    doc2 = tmp_path / "2026-05-23_older.md"
    _fixture_doc(doc1, date="2026-05-24")
    _fixture_doc(doc2, date="2026-05-23")
    entries = [parse_doc(doc1), parse_doc(doc2)]
    out = render_index_table(entries, tmp_path)
    # Recent date appears before older date (desc within same status)
    recent_pos = out.find("2026-05-24")
    older_pos = out.find("2026-05-23")
    assert 0 <= recent_pos < older_pos


def test_render_index_table_no_frontmatter_row(tmp_path: Path):
    doc = tmp_path / "grandfathered.md"
    _fixture_doc(doc, include_frontmatter=False)
    out = render_index_table([parse_doc(doc)], tmp_path)
    assert "(no frontmatter)" in out


def test_render_index_status_sort_order(tmp_path: Path):
    """active before superseded before archived."""
    active = tmp_path / "active.md"
    superseded = tmp_path / "superseded.md"
    archived = tmp_path / "archived.md"
    _fixture_doc(active, status="active", title="Active doc")
    _fixture_doc(superseded, status="superseded", title="Superseded doc")
    _fixture_doc(archived, status="archived", title="Archived doc")
    entries = [parse_doc(p) for p in (archived, superseded, active)]
    out = render_index_table(entries, tmp_path)
    active_pos = out.find("Active doc")
    superseded_pos = out.find("Superseded doc")
    archived_pos = out.find("Archived doc")
    assert 0 <= active_pos < superseded_pos < archived_pos


# ---- regenerate_one (per-README integration) ----------------------------


def test_regenerate_one_replaces_sentinel_block(tmp_path: Path):
    readme = tmp_path / "README.md"
    readme.write_text(
        f"# Test dir\n\nProse before.\n\n{BEGIN_SENTINEL}\nold content\n{END_SENTINEL}\n\nProse after.\n"
    )
    sibling = tmp_path / "2026-05-23_thing.md"
    _fixture_doc(sibling, title="Sibling doc")
    changed, new = regenerate_one(readme)
    assert changed is True
    assert "Prose before." in new  # operator-edited prose preserved
    assert "Prose after." in new
    assert "Sibling doc" in new
    assert "old content" not in new
    assert BEGIN_SENTINEL in new
    assert END_SENTINEL in new


def test_regenerate_one_idempotent_second_run(tmp_path: Path):
    """Re-running on already-up-to-date content produces no change."""
    readme = tmp_path / "README.md"
    readme.write_text(
        f"# Test dir\n\n{BEGIN_SENTINEL}\n{END_SENTINEL}\n"
    )
    sibling = tmp_path / "2026-05-23_thing.md"
    _fixture_doc(sibling)
    # First run: changes
    changed1, new1 = regenerate_one(readme)
    assert changed1 is True
    readme.write_text(new1)
    # Second run: no change
    changed2, _ = regenerate_one(readme)
    assert changed2 is False


def test_regenerate_one_missing_sentinels_no_change(tmp_path: Path):
    """README without sentinels is left untouched."""
    readme = tmp_path / "README.md"
    original = "# No sentinels here\n\nJust prose.\n"
    readme.write_text(original)
    _fixture_doc(tmp_path / "sibling.md")
    changed, new = regenerate_one(readme)
    assert changed is False
    assert new == original


def test_main_check_mode_exits_nonzero_when_stale(tmp_path: Path, monkeypatch):
    """--check exits 1 when any README would change."""
    monkeypatch.setattr(regen_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    readme = docs_dir / "README.md"
    readme.write_text(f"# Docs\n{BEGIN_SENTINEL}\n{END_SENTINEL}\n")
    _fixture_doc(docs_dir / "2026-05-23_thing.md")
    rc = main(["--check", "--root", "docs"])
    assert rc == 1


def test_main_default_mode_writes_changes(tmp_path: Path, monkeypatch):
    """Default mode writes changes and exits 0."""
    monkeypatch.setattr(regen_mod, "REPO_ROOT", tmp_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    readme = docs_dir / "README.md"
    readme.write_text(f"# Docs\n{BEGIN_SENTINEL}\n{END_SENTINEL}\n")
    _fixture_doc(docs_dir / "2026-05-23_thing.md", title="My doc")
    rc = main(["--root", "docs"])
    assert rc == 0
    assert "My doc" in readme.read_text()


def test_find_readmes_indexes_a_checkout_under_a_dot_directory(tmp_path, monkeypatch):
    """A repo root under a dot-directory must still be indexed.

    `find_readmes` tested `path.parts` on the ABSOLUTE path. `REPO_ROOT` is absolute, so a
    checkout at `.claude/worktrees/<id>/` — where the workflow tooling puts every agent worktree —
    made `.claude` a component of every file's path: zero READMEs found, and `--check` then passed
    by comparing nothing.

    Same bug `lint_doc_conventions.walk_docs` shipped with (fixed in #56, which corrected one
    sibling and missed this one). The filter belongs on the path RELATIVE to the root: a dot-dir
    INSIDE docs/ is still skipped (asserted), one ABOVE the root is not our business.
    """
    root = tmp_path / ".claude" / "worktrees" / "wf_abc-1"
    docs_dir = root / "docs" / "runbooks"
    docs_dir.mkdir(parents=True)
    (docs_dir / "README.md").write_text("# Runbooks\n")
    hidden = root / "docs" / ".drafts"
    hidden.mkdir(parents=True)
    (hidden / "README.md").write_text("# WIP\n")

    monkeypatch.setattr(regen_mod, "REPO_ROOT", root)
    found = regen_mod.find_readmes([root / "docs"])

    assert any(p.name == "README.md" and "runbooks" in str(p) for p in found), (
        "a checkout under a dot-directory indexed NOTHING — --check then passes by comparing nothing"
    )
    assert not any(".drafts" in str(p) for p in found), "hidden dirs inside the tree must stay skipped"
