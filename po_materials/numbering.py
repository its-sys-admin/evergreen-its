"""PO-number parse/format + the PO_Log collision double-check (D7, PO S4).

Purpose
-------
The contractual PO identity is `{job_no}.{site_phase}.{supersede_seq}.{revision}`
where `job_no` is itself the two-segment Evergreen job number `YYYY.NNN` — five
dot-separated integer segments total (corpus S0 §4: `2025.364.1.2` omits nothing;
`2025.358.1.2.11` supersedes `2025.358.1.1.11`). The Worker ALLOCATES the number
atomically at generate (po.ts: MAX(revision)+1 within the (job_no, site_phase,
supersede_seq) family, UNIQUE-index race backstop) — this module never mints one.
It parses/formats the scheme and runs the Mac-side COLLISION DOUBLE-CHECK against
PO_Log before filing.

Why a Mac-side double-check when D1 already has a UNIQUE index: PO_Log is the
operator-visible ledger and, during the transition, the landing place of HAND-ISSUED
POs that never passed through D1 (the operator keys them in directly). A D1-allocated
number colliding with a hand-issued PO_Log row is invisible to the Worker's index —
the daemon catches it here and FENCES (Review Queue, never file, never mark-filed).
A PO_Log row that carries THIS PO's own D1 id (`po_log.find_row_by_po_number` →
`d1_id` match) is not a collision — it is a crash-retry of a partially-filed PO and
the caller resumes idempotently.

Change-order numbers (`{parent}-CO{seq}`)
-----------------------------------------
A change order is a NORMAL lane document cloned from a SENT parent; the Worker
mints its number as `{parent_po_number}-CO{seq}` at generate time (e.g.
`2026.384.1.0.0` → `2026.384.1.0.0-CO1`, second one `-CO2`). The parent stays in
force — a CO is not a supersession. That suffixed grammar is NOT part of the base
D7 scheme: `parse_po_number` handles BASE family numbers ONLY and REJECTS a
CO-suffixed string (`PoNumberError`). Nothing on the Mac parses a CO number into
D7 components — the render module derives the change-order clause's parent by a
deliberate local rsplit on `-CO` (`po_generate._change_order_parts`), because the
po_number is inside the signed HMAC string while the Worker's `change_order_of` /
`co_seq` D1 columns are store-only/unsigned (outside the po:v1 canonical).

Deterministic string/lookup helpers only — no network beyond the PO_Log read the
caller passes through `po_materials.po_log`. Smartsheet failures propagate typed
(the caller's per-row fence decides transient-vs-permanent).
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Five dot-separated integer segments: YYYY.NNN.site_phase.supersede_seq.revision.
# job_no is anchored to the Worker's JOB_NO_RE (\d{4}\.\d{3}); the last three segments
# are non-negative integers without a fixed width (site_phase ≤ 9999 Worker-bounded;
# supersede_seq/revision are small monotonics).
_PO_NUMBER_RE = re.compile(r"^(\d{4}\.\d{3})\.(\d+)\.(\d+)\.(\d+)$")


class PoNumberError(ValueError):
    """Raised on a string that is not a well-formed D7 PO number."""


class PoNumber(NamedTuple):
    """The four D7 components of a parsed PO number."""

    job_no: str          # "YYYY.NNN" — the Evergreen project job number
    site_phase: int
    supersede_seq: int
    revision: int


def format_po_number(
    job_no: str, site_phase: int, supersede_seq: int, revision: int
) -> str:
    """`${job_no}.${site_phase}.${supersede_seq}.${revision}` — byte-identical to the
    Worker's template (po.ts generate). No zero-padding on the last three segments."""
    return f"{job_no}.{site_phase}.{supersede_seq}.{revision}"


def parse_po_number(value: str) -> PoNumber:
    """Parse a D7 PO number into its components; `PoNumberError` on any malformation.

    Round-trip stable: `format_po_number(*parse_po_number(s)) == s` for every valid
    `s` (the segments carry no padding). Used by the status pass to sanity-check a
    review-row's Notes-encoded number and by the supersession display helpers.
    """
    m = _PO_NUMBER_RE.match((value or "").strip())
    if m is None:
        raise PoNumberError(
            f"not a valid PO number: {value!r} (want YYYY.NNN.site.supersede.revision)"
        )
    return PoNumber(
        job_no=m.group(1),
        site_phase=int(m.group(2)),
        supersede_seq=int(m.group(3)),
        revision=int(m.group(4)),
    )


def check_collision(po_number: str, d1_id: int) -> str | None:
    """The pre-filing PO_Log collision double-check. Returns a machine reason or None.

    * None                — no PO_Log row with this number (fresh filing), OR the
                            existing row carries THIS PO's own `d1_id` (a crash-retry
                            of a partial filing → the caller resumes idempotently).
    * 'po_number_collision' — a PO_Log row with this number exists and is NOT ours
                            (a hand-issued PO keyed in during the transition, or a
                            ledger defect). The caller FENCES: Review Queue row,
                            one-shot flag, never file, never mark-filed.

    Smartsheet failures propagate (typed) — a collision check that silently passed
    on a read error could file a duplicate legal document.
    """
    from po_materials import po_log  # late import — keep this module cheap to import

    row = po_log.find_row_by_po_number(po_number)
    if row is None:
        return None
    if po_log.row_d1_id(row) == d1_id:
        return None
    return "po_number_collision"
