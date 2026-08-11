"""Regenerate auto-index sections in docs/**/README.md files.

Walks the doc tree, parses YAML frontmatter from each `.md` file, and
replaces the content between

    <!-- BEGIN AUTO-INDEX -->
    <!-- END AUTO-INDEX -->

in each subdirectory's `README.md` with a sorted table of contents.

Idempotent — only modifies content between the sentinel markers.
Operator-edited prose outside the sentinels is preserved across runs.

Sort order: status (active > superseded > archived > closed > draft),
then date descending (most recent first), then filename. Files without
frontmatter list as `(no frontmatter)` so the grandfather-state surfaces
without blocking the run.

CLI
---

    python -m scripts.regen_doc_indexes                  # rewrite all README.md indexes
    python -m scripts.regen_doc_indexes --check          # CI mode: exit non-zero if any would change
    python -m scripts.regen_doc_indexes --root docs/     # restrict the walk root

The default walk includes `docs/`, `prompts/`, and `prompts/samples/`.

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

# Subtrees we scan by default. Each must have at least one README at the
# top level (the regen creates the AUTO-INDEX content for siblings).
DEFAULT_ROOTS = ("docs", "prompts")

# Sentinel markers — keep verbatim; the lint test file pattern-matches.
BEGIN_SENTINEL = "<!-- BEGIN AUTO-INDEX -->"
END_SENTINEL = "<!-- END AUTO-INDEX -->"

# Status sort priority (lower = appears earlier in the index).
STATUS_PRIORITY: dict[str, int] = {
    "active": 0,
    "draft": 1,
    "superseded": 2,
    "closed": 3,
    "archived": 4,
}
DEFAULT_STATUS_PRIORITY = 99  # unknown / missing status sorts last.


@dataclass(frozen=True)
class DocEntry:
    """One doc's metadata extracted from its frontmatter (or `None` markers)."""
    path: Path  # relative to the directory's README
    doc_type: str | None
    date: str | None
    status: str | None
    workstream: str | None
    title: str
    related_prs: list[int]
    has_frontmatter: bool


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    """Return the parsed YAML frontmatter dict, or None when absent.

    Frontmatter is the block delimited by `---` lines at the very top of
    the file. Anything past the closing `---` is body content.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    # Find the closing delimiter
    match = re.search(r"^---\s*$", text[4:], flags=re.MULTILINE)
    if match is None:
        return None
    yaml_block = text[4 : 4 + match.start()]
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_title(text: str, fallback: str) -> str:
    """Find the first `# Heading` line (post-frontmatter). Fallback to filename."""
    # Skip frontmatter if present
    if text.startswith("---"):
        match = re.search(r"^---\s*$", text[4:], flags=re.MULTILINE)
        if match is not None:
            text = text[4 + match.end():]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("##"):
            return stripped[2:].strip()
    return fallback


def parse_doc(path: Path) -> DocEntry:
    """Read `path` and project the frontmatter + title into a `DocEntry`.

    Files without frontmatter still produce a DocEntry, with `has_frontmatter=False`
    and metadata fields set to None — surfaces the grandfather state in the
    rendered index without blocking the run.
    """
    text = path.read_text()
    fm = _parse_frontmatter(text)
    title = _extract_title(text, fallback=path.stem)

    if fm is None:
        return DocEntry(
            path=path,
            doc_type=None,
            date=None,
            status=None,
            workstream=None,
            title=title,
            related_prs=[],
            has_frontmatter=False,
        )

    related_prs_raw = fm.get("related_prs", [])
    related_prs: list[int] = []
    if isinstance(related_prs_raw, list):
        for item in related_prs_raw:
            if isinstance(item, int):
                related_prs.append(item)

    return DocEntry(
        path=path,
        doc_type=fm.get("type") if isinstance(fm.get("type"), str) else None,
        date=str(fm.get("date")) if fm.get("date") is not None else None,
        status=fm.get("status") if isinstance(fm.get("status"), str) else None,
        workstream=fm.get("workstream") if isinstance(fm.get("workstream"), str) else None,
        title=title,
        related_prs=related_prs,
        has_frontmatter=True,
    )


def _sort_key(entry: DocEntry) -> tuple[int, str, str]:
    """Sort by: status priority, date desc (negated string for desc), filename."""
    status_rank = STATUS_PRIORITY.get(entry.status or "", DEFAULT_STATUS_PRIORITY)
    # We want date DESC — Python sorts strings ascending, so negate by
    # using a sentinel that flips ISO-date ordering. Easiest: sort with
    # `reverse=False` on `(status_rank, -date_as_key)` — but we can't
    # negate a string. Use a high-sentinel character to invert.
    date_key = "0000-00-00" if entry.date is None else entry.date
    # Invert by subtracting from the highest possible date character
    # Build a desc-sort key by complementing each digit
    inverted_date = "".join(
        chr(ord("9") + ord("0") - ord(c)) if c.isdigit() else c for c in date_key
    )
    return (status_rank, inverted_date, str(entry.path))


def render_index_table(entries: list[DocEntry], readme_dir: Path) -> str:
    """Render the AUTO-INDEX Markdown block from a list of DocEntry."""
    if not entries:
        return "_(no docs in this directory)_"

    lines: list[str] = []
    lines.append("| Date | Type | Status | Workstream | Title | PRs |")
    lines.append("|------|------|--------|------------|-------|-----|")

    for e in sorted(entries, key=_sort_key):
        if not e.has_frontmatter:
            # Single "(no frontmatter)" cell spanning by repetition; keep
            # the row legible by listing what we know.
            rel = e.path.relative_to(readme_dir)
            lines.append(
                f"| _(no frontmatter)_ | _–_ | _–_ | _–_ "
                f"| [{e.title}]({rel}) | _–_ |"
            )
            continue
        rel = e.path.relative_to(readme_dir)
        prs = ", ".join(f"#{n}" for n in e.related_prs) if e.related_prs else "_–_"
        date_str = e.date or "_–_"
        type_str = e.doc_type or "_–_"
        status_str = e.status or "_–_"
        ws_str = e.workstream or "_–_"
        lines.append(
            f"| {date_str} | {type_str} | {status_str} | {ws_str} "
            f"| [{e.title}]({rel}) | {prs} |"
        )
    return "\n".join(lines)


def _replace_between_sentinels(content: str, replacement: str) -> str | None:
    """Replace the content between BEGIN/END sentinels.

    Returns the new file content, or None if either sentinel is missing.
    """
    begin_idx = content.find(BEGIN_SENTINEL)
    end_idx = content.find(END_SENTINEL)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        return None
    prefix = content[: begin_idx + len(BEGIN_SENTINEL)]
    suffix = content[end_idx:]
    return f"{prefix}\n{replacement}\n{suffix}"


def find_readmes(roots: list[Path]) -> list[Path]:
    """Return every `README.md` under the given root paths, excluding hidden dirs.

    The hidden-dir test runs on the path RELATIVE to the repo root, never on the absolute path.
    `REPO_ROOT` is absolute, so a checkout living under a dot-directory — which is exactly where
    `.claude/worktrees/<id>/` puts every workflow-agent worktree — has `.claude` as a component of
    EVERY file's absolute path. The old test matched it, found zero READMEs, and `--check` then
    passed by having compared nothing.

    This is the same bug `lint_doc_conventions.walk_docs` shipped with, fixed in #56; that pass
    corrected one sibling and missed this one — the recurring incomplete-fan-out miss. A gate that
    passes because it found no work is a green light that means silence.
    """
    readmes: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("README.md"):
            try:
                rel = path.relative_to(REPO_ROOT)
            except ValueError:  # outside the repo root — not ours to index
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            readmes.append(path)
    return readmes


def regenerate_one(readme: Path) -> tuple[bool, str]:
    """Regenerate the AUTO-INDEX block in `readme`.

    Returns (changed, new_content). `changed` is True if the file content
    differs from before regeneration. `new_content` is the new full file
    content (or the unchanged original if the readme lacks sentinels).
    """
    original = readme.read_text()
    readme_dir = readme.parent
    sibling_mds = [
        p
        for p in readme_dir.iterdir()
        if p.is_file() and p.suffix == ".md" and p.name != "README.md"
    ]
    entries = [parse_doc(p) for p in sibling_mds]
    rendered = render_index_table(entries, readme_dir)

    new_content = _replace_between_sentinels(original, rendered)
    if new_content is None:
        # README missing sentinels — leave alone, return unchanged.
        return False, original
    return new_content != original, new_content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.regen_doc_indexes",
        description=(
            "Regenerate AUTO-INDEX sections in README.md files across the "
            "doc tree. Idempotent. See docs/operations/doc_conventions.md."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit non-zero if any README would change. CI lint mode; "
            "no files are modified."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help=(
            "Restrict the walk to specific roots (relative to repo root). "
            "May be passed multiple times. Default: docs, prompts."
        ),
    )
    args = parser.parse_args(argv)

    roots_arg = args.root if args.root is not None else list(DEFAULT_ROOTS)
    roots = [REPO_ROOT / r for r in roots_arg]
    readmes = find_readmes(roots)

    any_changed = False
    for readme in readmes:
        changed, new_content = regenerate_one(readme)
        if changed:
            any_changed = True
            if args.check:
                print(f"WOULD-CHANGE {readme.relative_to(REPO_ROOT)}")
            else:
                readme.write_text(new_content)
                print(f"regen {readme.relative_to(REPO_ROOT)}")

    if args.check and any_changed:
        print("\nERROR: one or more README AUTO-INDEX sections out-of-date.")
        print("Run `python -m scripts.regen_doc_indexes` to update.")
        return 1
    if not args.check and not any_changed:
        print("(no changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
