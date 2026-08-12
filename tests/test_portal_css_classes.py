"""Every className the Safety-Portal SPA renders must resolve to a real CSS rule.

WHY THIS EXISTS. An audit found SIXTY-TWO class names referenced in
``safety_portal/src/**/*.tsx`` with no definition in any stylesheet. That is not cosmetic
debt: ``WeeklyReportPage`` referenced fifteen of them and rendered as raw unstyled HTML,
and the three OCR-check screens each declared a split class that was never defined, so
their side-by-side source preview — each screen's stated ONLY control against a misread —
collapsed to roughly 100px on a phone. Nothing failed. The pages just quietly looked wrong.

Nothing else catches this. TypeScript does not know about CSS, the bundler ships a class
nobody styles, and every vitest suite passes either way. (It was attempted in vitest first
and could not read the stylesheets: Vite's CSS pipeline hands back an empty string even
with ``?raw``. Scanning the TSX from Python is the established pattern here — see
``test_portal_button_variants.py``.)

ALLOWLIST, NOT A ZERO. Fourteen names legitimately carry no rule of their own, and each is
listed with its reason. Adding to that list is a deliberate act; the point of the guard is
that it cannot happen by accident.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "safety_portal" / "src"
STYLES = SRC / "styles"

#: Names with no rule of their own ON PURPOSE. An unexplained entry is how the original
#: sixty-two accumulated, so every one carries its reason.
ALLOWED: dict[str, str] = {
    # Bases of a template literal: the element is styled by the modifier that gets
    # appended (``className={`fr__cell${wide ? "--wide" : ""}`}``), so the bare prefix
    # never reaches the DOM on its own.
    "fr": "template base; the .fr__* modifiers carry the rules",
    "fr__cell": "template base; .fr__cell--* carry the rules",
    "fr__content": "template base; .fr__content--* carry the rules",
    "fr__guidance": "template base; .fr__guidance--rail carries the rules",
    "fr__callout--": "template base; .fr__callout--critical etc. carry the rules",
    "fr__static--": "template base; .fr__static--heading etc. carry the rules",
    "photo-field": "template base; .photo-field__* carry the rules",
    "form-editor__step--": "template base; the --state modifiers carry the rules",
    "form-editor__req-status--": "template base; the --state modifiers carry the rules",
    # Test hooks that are owed no styling. A <details>/<summary> disclosure is fully
    # functional from UA styles; the name exists so a test can address the element.
    "dash-completed": "queried by FieldOpsMyTasks + AssignedInspectionsSection tests",
    "receipt": "queried by a vitest suite AND a Python renderer; removing it breaks both",
    # Inert wrappers inside sections that are already styled. Kept because they name the
    # region for a reader; styling them would change pages nobody asked to change.
    "fr__job-reqs": "wrapper inside an already-styled .fr__section",
    "fr__expected-materials": "wrapper inside an already-styled .fr__section",
    "fr__additional-photos": "wrapper inside an already-styled .fr__section",
}

_CLASSNAME = re.compile(r"""className=(?:\{)?[`"']([^`"'{}]*)""")
_SELECTOR = re.compile(r"\.([A-Za-z_][-\w]*)")


def _defined_classes() -> set[str]:
    css = "\n".join(p.read_text(encoding="utf-8") for p in sorted(STYLES.glob("*.css")))
    return set(_SELECTOR.findall(css))


def _rendered_classes() -> dict[str, set[str]]:
    """token -> the files that render it. Test suites are excluded: they assert on
    markup rather than author it."""
    out: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.tsx")):
        if "__tests__" in path.parts:
            continue
        for match in _CLASSNAME.finditer(path.read_text(encoding="utf-8")):
            for raw in match.group(1).split():
                # A trailing '$' means the capture stopped at a template literal's '${',
                # so this is a PREFIX rather than a whole class name.
                token = raw.rstrip("$")
                if not token or token.startswith("$"):
                    continue
                out.setdefault(token, set()).add(path.name)
    return out


def test_every_rendered_class_has_a_rule() -> None:
    defined = _defined_classes()
    missing = {
        token: files
        for token, files in _rendered_classes().items()
        if token not in defined and token not in ALLOWED
    }
    detail = "\n".join(f"  .{t} — {', '.join(sorted(f))}" for t, f in sorted(missing.items()))
    assert not missing, (
        "These class names render but have no CSS rule, and are not on the explained "
        f"allowlist:\n{detail}\n\nDefine them, delete the dead reference, or add them to "
        "ALLOWED with the reason."
    )


def test_allowlist_stays_honest() -> None:
    """An allowlist entry that outlives its reference is the next silent drift."""
    rendered = set(_rendered_classes())
    stale = sorted(a for a in ALLOWED if a not in rendered)
    assert not stale, f"Allowlisted but no longer rendered anywhere — delete these: {stale}"
