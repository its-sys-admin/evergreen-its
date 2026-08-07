"""Structural guards for the Track 6 archive route that its own runtime suite cannot cover.

Two things the Worker's vitest suite structurally CANNOT prove, so they live here where a plain
Python process can read the TypeScript source:

1. The mutating UPDATE re-asserts the permitted archive_state in its own WHERE. That predicate
   defends against ANOTHER WRITER — the Mac-side pass, which writes the same columns from its own
   process — moving the row between the handler's SELECT and its UPDATE. workerd serializes the
   test requests and D1 serializes writes, so the race cannot be staged in-harness: deleting the
   predicate leaves the whole vitest file green (verified by inject-confirm-revert). A source-level
   assertion is the only mechanical backstop available until the daemon exists to race against.

2. `jobFolderKey` in the Worker and `safety_naming.job_folder_name` in Python must agree, because
   the Worker SNAPSHOTS the key and the daemon RESOLVES folders with it. A divergence sends the
   daemon hunting for a folder that was never created — a permanent archive failure whose only
   symptom is an unexplained archive_state='failed'.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from safety_reports.safety_naming import job_folder_name

REPO_ROOT = Path(__file__).resolve().parents[1]
JOB_WRITE_TS = REPO_ROOT / "safety_portal" / "worker" / "fieldops_job_write.ts"


def _src() -> str:
    return JOB_WRITE_TS.read_text(encoding="utf-8")


def test_archive_update_carries_an_in_where_state_guard() -> None:
    src = _src()
    # The guard is built as `stateGuard` and interpolated into the UPDATE's WHERE.
    assert "ARCHIVE_RESTARTABLE_SQL" in src, "the restartable-state SQL list vanished"
    assert "archive_state = 'complete'" in src, "the un-archive direction lost its state guard"
    assert re.search(
        r"WHERE job_id=\?1 AND origin='portal' \$\{stateGuard\}", src
    ), (
        "the archive UPDATE no longer interpolates the state guard into its WHERE — the handler is "
        "back to check-then-act, and a concurrent daemon write can be stomped"
    )


def test_archive_update_keeps_the_origin_fence() -> None:
    # Belt-and-braces on the fence the vitest suite DOES cover, because losing it here is
    # unrecoverable: a stray write to an origin='smartsheet' row corrupts it with no self-heal.
    assert _src().count("AND origin='portal'") >= 2, (
        "the archive SELECT and UPDATE must BOTH be scoped to origin='portal'"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "Bradley 1",
        "  Coker  ",
        "A/B",
        "Bradley\u00a0Solar",   # NBSP — routinely arrives via copy-paste from Word/Excel/PDF
        "Bra\u200bdley",        # zero-width space
        "Bra\u200ddley",        # zero-width joiner
        "Bradley\u200e",        # LTR mark
        "Bradley\u2002Solar",   # en space
        "Bradley\u3000Solar",   # ideographic space
        "Cok\u0007er",          # C0 control
        "Bradley \U0001f600 Solar",  # astral printable — must SURVIVE
        "Bradley Solar",        # ASCII space — isprintable() excepts it specifically
        "    ",                 # sanitizes to empty → falls back to the raw stripped input
    ],
)
def test_job_folder_key_ts_mirror_matches_python(raw: str) -> None:
    """Re-implement the TS rule here and assert it agrees with Python on every case.

    Python's `str.isprintable()` is "NOT (Unicode Other or Separator), except ASCII space" — i.e.
    Cc, Cf, Cs, Co, Cn, Zl, Zp, Zs. An earlier Worker version filtered only C0/C1 controls, which
    looks equivalent and is not; every non-ASCII case above is one the narrower filter got wrong.
    """
    import unicodedata

    def ts_rule(project_name: str) -> str:
        printable = "".join(
            ch
            for ch in project_name
            if ch == " " or unicodedata.category(ch)[0] not in ("C", "Z")
        )
        cleaned = printable.replace("/", "-").strip()
        return cleaned if cleaned else project_name.strip()

    assert ts_rule(raw) == job_folder_name(raw), f"TS/Python divergence on {raw!r}"


def test_ts_source_uses_the_unicode_property_filter_not_a_c0_range() -> None:
    """The mirror above only proves the RULE agrees — this proves the Worker implements that rule.

    Without it, someone could 'simplify' jobFolderKey back to a codepoint-range check and every
    test here would still pass, because the mirror is independent of the TS source.
    """
    src = _src()
    assert re.search(r"\\p\{Cc\}.*\\p\{Zs\}", src), (
        "jobFolderKey no longer filters on Unicode General_Category properties — a C0/C1 range "
        "check is NOT equivalent to Python's str.isprintable()"
    )
    assert 'ch === " " ||' in src, "the ASCII-space exception was dropped"
