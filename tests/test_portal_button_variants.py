"""Mechanically enforce the portal design language's `.btn--ghost` rule (was review-only).

`.btn--ghost` (`safety_portal/src/styles/global.css`) is the BRG-HEADER button variant:

    /* Ghost button on the BRG header - white outline for contrast on green. */
    .btn--ghost { background: transparent; color: var(--c-on-brg); border-color: rgba(255,255,255,0.6); }

`--c-on-brg` is `#ffffff`. On the light page ground (`--c-surface` `#f7f6f2`) or on a white
card it renders at roughly 1.04:1 contrast - invisible. global.css already states the
intended rule in prose:

    ".btn--ghost is intentionally NOT here - it is the white-on-green HEADER ghost; the
     trackers' page-context ghosts are remapped to --secondary in the markup so they are
     visible on the white page."

That rule lived only at review, and six controls shipped in violation of it: the deep-link
"Back to My Tasks" control on FormFillPage (invisible in the field - the operator-reported
bug this gate closes), four controls in ChecklistItemRow, and the remove-vendor chip in
RfqBuilderPage. Notably FormFillPage rendered the SAME label correctly green on its
post-submit screen, so the two halves of one flow disagreed.

This promotes the rule to a CI gate, mirroring the source-scanning allowlist idiom in
tests/test_capability_gating.py and tests/test_state_write_discipline.py: no `.tsx`/`.ts`
source on the SPA surface may use `.btn--ghost` except on the AppHeader sign-out control,
which sits on the green header where the variant is correct.

The gate is a Python source scan rather than a vitest suite on purpose: the SPA tsconfig
project carries no `@types/node`, so a TS test reading its own source fails `npm run
typecheck`, and the worker-pool suite runs inside workerd with no filesystem. The scan is
plain text, so it needs neither.

Pick the right variant instead (`safety_portal/src/styles/global.css`):
  * `.btn--primary`   - the canonical action (green fill, white text, gold border)
  * `.btn--secondary` - in-page action or back control (white fill, BRG text, BRG border)
  * `.btn--danger`    - inline icon/remove control (white fill, red text, red border)

Run with: pytest -q tests/test_portal_button_variants.py
"""

from __future__ import annotations

from pathlib import Path

SPA_SRC = Path(__file__).resolve().parent.parent / "safety_portal" / "src"

GHOST_CLASS = "btn--ghost"

# The one sanctioned role for `.btn--ghost`: the sign-out button rendered into
# `<AppHeader action={...}>`, which paints on the BRG green header. Every such call site
# invokes `logout()` on the same line, so the marker ties the class to that ROLE rather
# than to a file allowlist - a file allowlist would silently permit a SECOND, page-context
# ghost inside an already-listed file (FormFillPage held exactly that pair: a correct
# header ghost at the top and the invisible back button below it).
HEADER_SIGN_OUT_MARKER = "logout()"

# Guards the guard. If the sign-out markup is refactored (or the pages are renamed), the
# offender assertion below would pass VACUOUSLY on zero hits and quietly stop protecting
# anything - the "green proves nothing" failure mode. Update this deliberately.
EXPECTED_HEADER_GHOSTS: dict[str, int] = {
    "components/PageShell.tsx": 1,
    "pages/AccountsPage.tsx": 1,
    "pages/FormFillPage.tsx": 1,
    "pages/HomePage.tsx": 1,
}


def _ghost_hits() -> list[tuple[str, int, str]]:
    """Every `.btn--ghost` occurrence on the SPA surface as (relpath, 1-based line, text).

    Test sources are walked too: a test that pins `btn--ghost` on a page-context control
    is asserting the defect, so it must be updated alongside the markup it covers.
    """
    hits: list[tuple[str, int, str]] = []
    for path in sorted(SPA_SRC.rglob("*")):
        if path.suffix not in (".ts", ".tsx") or not path.is_file():
            continue
        for lineno, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if GHOST_CLASS in text:
                hits.append((path.relative_to(SPA_SRC).as_posix(), lineno, text.strip()))
    return hits


def test_ghost_button_is_the_header_sign_out_only() -> None:
    """No `.btn--ghost` outside the AppHeader sign-out control."""
    offenders = [h for h in _ghost_hits() if HEADER_SIGN_OUT_MARKER not in h[2]]
    assert offenders == [], (
        "`.btn--ghost` on a non-header control - it renders white-on-near-white and will be "
        "invisible on the light page ground. Use --secondary (in-page action / back), "
        "--primary (canonical action) or --danger (inline remove):\n"
        + "\n".join(f"  {f}:{n} - {t}" for f, n, t in offenders)
    )


def test_header_sign_out_ghosts_are_still_present() -> None:
    """The sanctioned call sites still exist, so the gate above is not vacuous."""
    counts: dict[str, int] = {}
    for relpath, _lineno, text in _ghost_hits():
        if HEADER_SIGN_OUT_MARKER in text:
            counts[relpath] = counts.get(relpath, 0) + 1
    assert counts == EXPECTED_HEADER_GHOSTS, (
        "The AppHeader sign-out ghost call sites moved. If that is intentional, update "
        "EXPECTED_HEADER_GHOSTS deliberately - it is what keeps the offender gate honest."
    )
