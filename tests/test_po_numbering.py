"""Tests for po_materials/numbering.py — D7 parse/format + the PO_Log collision
double-check. Smartsheet is mocked (the collision check reads PO_Log through
po_materials.po_log).

Run with: pytest -q tests/test_po_numbering.py
"""
from __future__ import annotations

import pytest

from po_materials import numbering, po_log

# ---- parse / format --------------------------------------------------------


def test_format_matches_worker_template() -> None:
    assert numbering.format_po_number("2026.001", 2, 0, 0) == "2026.001.2.0.0"
    assert numbering.format_po_number("2025.358", 1, 2, 11) == "2025.358.1.2.11"


@pytest.mark.parametrize("value", [
    "2025.364.1.0.2",
    "2025.358.1.2.11",
    "2023.126.2.0.20",
    "2026.001.9999.0.0",
])
def test_parse_round_trips(value: str) -> None:
    parsed = numbering.parse_po_number(value)
    assert numbering.format_po_number(*parsed) == value


def test_parse_components() -> None:
    parsed = numbering.parse_po_number("2025.358.1.2.11")
    assert parsed.job_no == "2025.358"
    assert parsed.site_phase == 1
    assert parsed.supersede_seq == 2
    assert parsed.revision == 11


@pytest.mark.parametrize("bad", [
    "",
    "2025.358",              # job_no only
    "2025.358.1.2",          # four segments — missing revision
    "25.358.1.2.11",         # 2-digit year
    "2025.35.1.2.11",        # NNN too short
    "2025.358.1.2.11.4",     # six segments
    "2025.358.a.2.11",       # non-numeric segment
    "folder-2025.364",       # a filename tag, not a number (corpus collision warning)
])
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(numbering.PoNumberError):
        numbering.parse_po_number(bad)


def test_parse_rejects_change_order_suffix() -> None:
    """The Worker-minted CO grammar (`{parent}-CO{seq}`) is NOT a base D7 number —
    parse_po_number handles BASE family numbers only. The change-order clause's
    parent derives via a deliberate rsplit in the render module
    (po_generate._change_order_parts), never through this parser."""
    with pytest.raises(numbering.PoNumberError):
        numbering.parse_po_number("2026.384.1.0.0-CO1")


# ---- collision double-check -------------------------------------------------


def test_no_ledger_row_is_clean(mocker) -> None:
    mocker.patch.object(po_log, "find_row_by_po_number", return_value=None)
    assert numbering.check_collision("2026.001.2.0.0", 7) is None


def test_own_retry_row_is_clean(mocker) -> None:
    """A ledger row carrying OUR d1_id is a crash-retry of a partial filing — the
    caller resumes idempotently, never fences."""
    row = {"_row_id": 1, po_log.COL_NOTES: "d1_id=7"}
    mocker.patch.object(po_log, "find_row_by_po_number", return_value=row)
    assert numbering.check_collision("2026.001.2.0.0", 7) is None


def test_foreign_row_is_a_collision(mocker) -> None:
    """A row with a DIFFERENT d1_id — or none at all (a hand-issued PO keyed in
    during the transition) — is a collision: fence, never file."""
    other = {"_row_id": 1, po_log.COL_NOTES: "d1_id=9"}
    mocker.patch.object(po_log, "find_row_by_po_number", return_value=other)
    assert numbering.check_collision("2026.001.2.0.0", 7) == "po_number_collision"

    hand_issued = {"_row_id": 2, po_log.COL_NOTES: "keyed in by operator"}
    mocker.patch.object(po_log, "find_row_by_po_number", return_value=hand_issued)
    assert numbering.check_collision("2026.001.2.0.0", 7) == "po_number_collision"
