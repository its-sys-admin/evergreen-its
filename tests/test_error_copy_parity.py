"""ERROR_COPY parity for the field-ops Worker routes (SPA plain-language contract).

`safety_portal/src/lib/errorCopy.ts` maps every worker error code the field-ops feature
can return to plain-language copy. An unmapped code does NOT fail loudly — it falls
through `humanizeCode()` to:

    "Something went wrong (signature too large). Please try again."

which is worse than a raw code: for a size failure or a dark gate the "please try again"
advice is *actively false*, and the user is told to repeat the one action that cannot
work. Killing exactly that fallback is why the R3-F2 `too_large` copy was written.

Nothing enforced the mapping, so it silently rotted: at the time this test was added the
"Sign & log to progress report" flow alone had FIVE dead-end codes
(`signature_required`, `signature_too_large`, `already_submitted`,
`progress_logging_disabled`, plus `photo_required` on the completion path) — three of
which `src/lib/fieldops_checklist.ts`'s own docstring names as the route's branch codes.
21 codes across the `fieldops_*` family were unmapped in total.

Scope is deliberately the `fieldops_*.ts` routes: that is errorCopy.ts's own stated
scope ("every worker error code the field-ops feature can return"). The PO / subcontract
/ config vocabularies are mapped opportunistically and are not held to parity here.

Static text analysis, mirroring tests/test_worker_send_free.py (the in-repo precedent for
grepping the Worker from pytest).

Run with: pytest -q tests/test_error_copy_parity.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_DIR = REPO_ROOT / "safety_portal" / "worker"
ERROR_COPY_TS = REPO_ROOT / "safety_portal" / "src" / "lib" / "errorCopy.ts"

# `error: "some_code"` — the shape every worker route returns.
_CODE_RE = re.compile(r'error:\s*"([a-z0-9_]+)"')
# A top-level key of the ERROR_COPY object literal (two-space indent, `key:`).
_KEY_RE = re.compile(r"^ {2}([a-z0-9_]+):", re.M)
_MAP_RE = re.compile(
    r"export const ERROR_COPY: Record<string, string> = \{(.*?)\n\};", re.S
)

# Codes that intentionally have NO user-facing copy. Keep this EMPTY unless there is a
# real reason — an entry here is a promise that the code can never reach a user's screen.
INTENTIONALLY_UNMAPPED: frozenset[str] = frozenset()


def _mapped_codes() -> set[str]:
    body = _MAP_RE.search(ERROR_COPY_TS.read_text())
    assert body, "could not locate the ERROR_COPY object literal in errorCopy.ts"
    return set(_KEY_RE.findall(body.group(1)))


def _returned_codes() -> dict[str, list[str]]:
    """code -> the fieldops_*.ts files that return it."""
    out: dict[str, list[str]] = {}
    for path in sorted(WORKER_DIR.glob("fieldops_*.ts")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for code in set(_CODE_RE.findall(path.read_text())):
            out.setdefault(code, []).append(rel)
    return out


def test_every_fieldops_worker_error_code_has_plain_language_copy() -> None:
    assert WORKER_DIR.is_dir(), f"missing worker dir: {WORKER_DIR}"
    mapped = _mapped_codes()
    returned = _returned_codes()
    assert returned, "found no fieldops_*.ts error codes — the regex or layout changed"

    missing = {
        code: files
        for code, files in sorted(returned.items())
        if code not in mapped and code not in INTENTIONALLY_UNMAPPED
    }
    assert not missing, (
        "field-ops Worker error codes with no ERROR_COPY entry — each one renders as "
        '"Something went wrong (…). Please try again." to a field user:\n'
        + "\n".join(f"  - {code}  ({', '.join(files)})" for code, files in missing.items())
        + "\nAdd copy to safety_portal/src/lib/errorCopy.ts (second person, name the "
        "remedy, end with a period, and never say 'try again' when retrying cannot help)."
    )


def test_error_copy_entries_follow_the_house_conventions() -> None:
    """Cheap style guard so new copy stays consistent with the 213 that predate it."""
    body = _MAP_RE.search(ERROR_COPY_TS.read_text())
    assert body
    # Join wrapped values, then pull every "..." string literal that is a map value.
    entries = re.findall(
        r"^ {2}[a-z0-9_]+:\s*\n?\s*\"((?:[^\"\\]|\\.)*)\"", body.group(1), re.M
    )
    assert len(entries) > 200, f"expected the full map, parsed only {len(entries)}"
    bad_end = [e for e in entries if not e.endswith((".", "!", "?"))]
    assert bad_end == [], f"ERROR_COPY entries must end with a period: {bad_end[:5]}"
    # NOTE: a "no raw error code leaked into the copy" check was tried and deliberately
    # dropped. The worker vocabulary includes bare generic codes (`exists`, `not_found`),
    # so matching them against English prose flags ordinary sentences ("That item no
    # longer exists.") and a snake_case regex flags deliberate examples ("e.g.
    # vendor_acme"). A guard that cries wolf gets deleted or muted; this one is not worth
    # the noise, and the parity test above is the assertion that actually matters.
