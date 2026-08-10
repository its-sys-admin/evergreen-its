"""Tests for subcontracts.subcontractors.upsert_subcontractor payload shape.

The PO-side twin (`po_materials.vendors.upsert_vendor`) is covered by
tests/test_po_vendors.py; this file exists because `upsert_subcontractor` had only
INDIRECT coverage (mocked out wholesale in tests/test_subcontract_poll.py), which is
precisely how the empty-MULTI_PICKLIST defect below reached both surfaces unnoticed.

Run with: pytest -q tests/test_subcontractors.py
"""
from __future__ import annotations

from typing import Any

import pytest

from subcontracts import subcontractors


def _portal_subcontractor(**over: Any) -> dict[str, Any]:
    """The Worker's subcontractors/pending shape (trades already a list; active 0/1)."""
    base: dict[str, Any] = {
        "sub_key": "SUB-000002",
        "sub_name": "Ridgeline Electric",
        "address": "12 Mill Rd",
        "contact_name": "Dana Ruiz",
        "contact_email": "ap@ridgeline.example",
        "contact_phone": "555-0143",
        "state": "IL",
        "trades": ["electrical"],
        "default_terms_profile": "standard_17",
        "msa_reference": "",
        "coi_reference": "",
        "license_number": "",
        "active": 1,
        "notes": "",
        "origin": "portal",
        "mirror_version": 2,
        "mirrored_version": 1,
    }
    base.update(over)
    return base


def test_upsert_drops_empty_multipicklist(mocker) -> None:
    """An EMPTY Trades list is DROPPED from the payload, exactly like a blank picklist
    scalar. The Smartsheet API rejects an empty MULTI_PICKLIST (`objectValue.values: []`)
    with errorCode 1012, so emitting the key at all is an unwritable payload — the
    subcontractor could never up-sync. Twin of the ITS_Vendors Supply Categories defect
    (tests/test_po_vendors.py::test_upsert_drops_empty_multipicklist)."""
    mocker.patch(
        "subcontracts.subcontractors.get_subcontractor_by_key", return_value=None
    )
    add = mocker.patch(
        "subcontracts.subcontractors.smartsheet_client.add_rows", return_value=[1]
    )
    subcontractors.upsert_subcontractor(_portal_subcontractor(trades=[]))
    (_, [cells]), _ = add.call_args
    assert subcontractors.COL_TRADES not in cells
    # A NON-empty list is still written through unchanged.
    add.reset_mock()
    subcontractors.upsert_subcontractor(_portal_subcontractor(trades=["electrical"]))
    (_, [cells]), _ = add.call_args
    assert cells[subcontractors.COL_TRADES] == ["electrical"]


def test_upsert_rejects_unkeyable_payload(mocker) -> None:
    add = mocker.patch("subcontracts.subcontractors.smartsheet_client.add_rows")
    with pytest.raises(ValueError):
        subcontractors.upsert_subcontractor(_portal_subcontractor(sub_key="nope"))
    add.assert_not_called()
