"""SC-number parse/format + the Subcontract_Log collision double-check (D7, SC S1).

Purpose
-------
The contractual subcontract identity is `{job_no}.{site_phase}.{supersede_seq}.{revision}`
where `job_no` is itself the two-segment Evergreen job number `YYYY.NNN` — five
dot-separated integer segments total (corpus S0 §4: `2025.364.1.2` omits nothing;
`2025.358.1.2.11` supersedes `2025.358.1.1.11`). The Worker ALLOCATES the number
atomically at generate (sub.ts: MAX(revision)+1 within the (job_no, site_phase,
supersede_seq) family, UNIQUE-index race backstop) — this module never mints one.
It parses/formats the scheme and runs the Mac-side COLLISION DOUBLE-CHECK against
Subcontract_Log before filing.

Why a Mac-side double-check when D1 already has a UNIQUE index: Subcontract_Log is
the operator-visible ledger and, during the transition, the landing place of
HAND-ISSUED subcontracts that never passed through D1 (the operator keys them in
directly). A D1-allocated number colliding with a hand-issued Subcontract_Log row
is invisible to the Worker's index — the daemon catches it here and FENCES (Review
Queue, never file, never mark-filed). A Subcontract_Log row that carries THIS
subcontract's own D1 id (`subcontract_log.find_row_by_sc_number` → `d1_id` match) is
not a collision — it is a crash-retry of a partially-filed subcontract and the
caller resumes idempotently.

Change-order numbers (`{parent}-CO{seq}`)
-----------------------------------------
A change order is a NORMAL lane document cloned from a SENT parent; the Worker
mints its number as `{parent_sc_number}-CO{seq}` at generate time (e.g.
`2026.384.1.0.0` → `2026.384.1.0.0-CO1`, second one `-CO2`). The parent stays in
force — a CO is not a supersession. That suffixed grammar is NOT part of the base
D7 scheme: `parse_sc_number` handles BASE family numbers ONLY and REJECTS a
CO-suffixed string (`ScNumberError`). Nothing on the Mac parses a CO number into
D7 components — `change_order_parts` (below) splits the suffix off by a deliberate
rsplit on `-CO`, because the sc_number is inside the signed HMAC string while the
Worker's change-order D1 columns are store-only/unsigned (outside the sub:v1
canonical). Consumers: the render clause (`subcontract_docx`) and the CH
file-name token (`subcontract_naming`).

Deterministic string/lookup helpers only — no network beyond the Subcontract_Log
read the caller passes through `subcontracts.subcontract_log`. Smartsheet failures
propagate typed (the caller's per-row fence decides transient-vs-permanent).
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Five dot-separated integer segments: YYYY.NNN.site_phase.supersede_seq.revision.
# job_no is anchored to the Worker's JOB_NO_RE (\d{4}\.\d{3}); the last three segments
# are non-negative integers without a fixed width (site_phase ≤ 9999 Worker-bounded;
# supersede_seq/revision are small monotonics).
_SC_NUMBER_RE = re.compile(r"^(\d{4}\.\d{3})\.(\d+)\.(\d+)\.(\d+)$")


class ScNumberError(ValueError):
    """Raised on a string that is not a well-formed D7 SC number."""


class ScNumber(NamedTuple):
    """The four D7 components of a parsed SC number."""

    job_no: str          # "YYYY.NNN" — the Evergreen project job number
    site_phase: int
    supersede_seq: int
    revision: int


def format_sc_number(
    job_no: str, site_phase: int, supersede_seq: int, revision: int
) -> str:
    """`${job_no}.${site_phase}.${supersede_seq}.${revision}` — byte-identical to the
    Worker's template (sub.ts generate). No zero-padding on the last three segments."""
    return f"{job_no}.{site_phase}.{supersede_seq}.{revision}"


def parse_sc_number(value: str) -> ScNumber:
    """Parse a D7 SC number into its components; `ScNumberError` on any malformation.

    Round-trip stable: `format_sc_number(*parse_sc_number(s)) == s` for every valid
    `s` (the segments carry no padding). NO production caller today (tests only) —
    kept as the grammar's executable definition; live code treats numbers as opaque
    strings, and change-order derivation is a deliberate rsplit
    (`change_order_parts`), not a parse_* call.
    """
    m = _SC_NUMBER_RE.match((value or "").strip())
    if m is None:
        raise ScNumberError(
            f"not a valid SC number: {value!r} (want YYYY.NNN.site.supersede.revision)"
        )
    return ScNumber(
        job_no=m.group(1),
        site_phase=int(m.group(2)),
        supersede_seq=int(m.group(3)),
        revision=int(m.group(4)),
    )


def change_order_parts(number: str) -> tuple[str, int] | None:
    """Split a change-order SC number `{parent}-CO{seq}` → (parent_number, seq), else None.

    A change order is a NORMAL lane document cloned from a SENT parent; the Worker
    mints its number as `{parent_sc_number}-CO{seq}` at generate time. The parent
    stays in force — a CO is NOT a supersession (`supersedes_sc_id` is NULL on one).

    §42 — why derive from the number STRING and not a D1 column: the sc_number is
    inside the signed HMAC string the daemon has already verified (`verify_sub`),
    so anything a consumer derives here (the contract clause's parent, the CH
    file-name token) re-derives from SIGNED data. The Worker's change-order columns
    are STORE-ONLY, outside the sub:v1 canonical — an unsigned column could drift
    or be tampered without failing verification. The `-CO<digits>` suffix is
    deliberately NOT part of the base D7 grammar (`parse_sc_number` rejects it), so
    this is a rsplit, not a parse_* call. A malformed tail (non-digits, empty head)
    returns None — consumers render NO clause / add NO marker rather than a wrong
    one. Promoted from `subcontract_docx._change_order_parts` 2026-08-15 at the
    third consumer (§14: render clause + `subcontract_naming` CH token);
    `po_materials.numbering` keeps the lane twin — the lanes share no module.
    """
    value = (number or "").strip()
    if "-CO" not in value:
        return None
    head, tail = value.rsplit("-CO", 1)
    if not head or not tail.isascii() or not tail.isdigit():
        return None
    return head, int(tail)


def check_collision(sc_number: str, d1_id: int) -> str | None:
    """The pre-filing Subcontract_Log collision double-check. Returns a machine reason
    or None.

    * None                — no Subcontract_Log row with this number (fresh filing), OR
                            the existing row carries THIS subcontract's own `d1_id` (a
                            crash-retry of a partial filing → the caller resumes
                            idempotently).
    * 'sc_number_collision' — a Subcontract_Log row with this number exists and is NOT
                            ours (a hand-issued subcontract keyed in during the
                            transition, or a ledger defect). The caller FENCES: Review
                            Queue row, one-shot flag, never file, never mark-filed.

    Smartsheet failures propagate (typed) — a collision check that silently passed
    on a read error could file a duplicate legal document.
    """
    from subcontracts import subcontract_log  # late import — keep this module cheap to import

    row = subcontract_log.find_row_by_sc_number(sc_number)
    if row is None:
        return None
    if subcontract_log.row_d1_id(row) == d1_id:
        return None
    return "sc_number_collision"
