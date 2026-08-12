"""Parent-side OCR entry for the schedule lane (ADR-0006).

Purpose
-------
The one call the `schedule_poll` daemon makes to turn untrusted schedule-PDF bytes
into per-page OCR word payloads: `estimate_sandbox.run_sandboxed("ocr_page_words")`
— Quartz render + rotation ladder + Apple Vision, ALL inside the killable child
(the lane carries no ADR-0004 §Vision in-process deviation; a wedged OCR of a
hostile document is reaped like any other parse) — plus strict shape validation of
the child's JSON here in the parent. This module deliberately does NOT interpret
the words — reconstruction is `field_ops.schedule_geometry`, semantics
`field_ops.schedule_parse`. It exists so the daemon's import surface stays clean
(`estimate_ocr.ocr_pages`' text-only return shape stays untouched for the
estimate ladder — ADR-0006 names that as a hard constraint) and so the shape
validation has one owner.

Invariants
----------
    * Invariant 2 (Adversarial Input Handling): the schedule bytes are
      portal-inbound UNTRUSTED content. They are decoded ONLY inside the
      rlimited, timeout-bounded sandbox child; this parent never opens them. The
      child's JSON is re-validated field-by-field here — a bearer-gated child is
      still a process boundary, not a trusted one — and a payload violating the
      declared shape is REFUSED whole, never coerced.
    * OCR confidence is evidence for the validate screen, never a filter — the
      corpus shows digit misreads at confidence 1.0 (ADR-0006 decision 2).
    * No AI beyond local Apple Vision, no send capability, no raw network —
      enforced by tests/test_capability_gating.py.

Failure modes
-------------
Returns None on ANY failure — sandbox timeout/kill/crash, malformed JSON, a
payload that violates the declared shape. None is the daemon's degrade signal
(the document refuses as unreadable; the human re-exports). Never raises on
hostile input.

Consumers
---------
The PR-3 `field_ops.schedule_poll` daemon (its OCR tier), and
tests/test_schedule_ocr.py (the mocked validation-boundary suite).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from po_materials import estimate_sandbox

DEFAULT_MAX_PAGES = 6  # corpus schedules are 2–4 pages; 6 leaves headroom


@dataclass(frozen=True)
class OcrPages:
    """The validated child payload."""

    pages: list[list[dict[str, Any]]]  # per page: [{"text","conf","x","y","w","h"}, ...]
    page_sizes: list[tuple[int, int]]
    rotations: list[int]  # degrees the bitmap was rotated CCW before the winning pass


def ocr_schedule_pages(data: bytes, *, max_pages: int = DEFAULT_MAX_PAGES) -> OcrPages | None:
    """Bytes → validated per-page OCR words, or None (the degrade signal)."""
    raw = estimate_sandbox.run_sandboxed(
        "ocr_page_words",
        data,
        timeout_s=estimate_sandbox.OCR_TIMEOUT_S,
        args=[str(max_pages)],
    )
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return _validate(payload)


def _validate(payload: Any) -> OcrPages | None:
    """Strict shape check — a child that emits anything but the contract is a
    failure, not something to coerce."""
    if not isinstance(payload, dict):
        return None
    pages = payload.get("pages")
    sizes = payload.get("page_sizes")
    rotations = payload.get("rotations")
    if not isinstance(pages, list) or not isinstance(sizes, list) or not isinstance(rotations, list):
        return None
    if not (len(pages) == len(sizes) == len(rotations)):
        return None
    out_pages: list[list[dict[str, Any]]] = []
    for page in pages:
        if not isinstance(page, list):
            return None
        words: list[dict[str, Any]] = []
        for w in page:
            if not isinstance(w, dict) or not isinstance(w.get("text"), str):
                return None
            try:
                words.append(
                    {
                        "text": w["text"],
                        "conf": float(w.get("conf", 0.0)),
                        "x": float(w["x"]),
                        "y": float(w["y"]),
                        "w": float(w["w"]),
                        "h": float(w["h"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                return None
        out_pages.append(words)
    out_sizes: list[tuple[int, int]] = []
    for s in sizes:
        if (
            not isinstance(s, list)
            or len(s) != 2
            or not all(isinstance(v, int) and v >= 0 for v in s)
        ):
            return None
        out_sizes.append((s[0], s[1]))
    if not all(isinstance(r, int) and r in (0, 90, 270) for r in rotations):
        return None
    return OcrPages(pages=out_pages, page_sizes=out_sizes, rotations=list(rotations))


__all__ = ["DEFAULT_MAX_PAGES", "OcrPages", "ocr_schedule_pages"]
