"""Unit tests for scripts/bom_reconcile.py.

The load-bearing test here is the customer-data guard: every artefact this script derives from a
vendor BOM is customer data, and the easy operator mistake is a relative `--out` run from the repo
root. Uses the same sys.path-driven import as tests/test_regen_doc_indexes.py to avoid the mypy
duplicate-module error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bom_reconcile as bom  # noqa: E402

# ---- the customer-data guard -----------------------------------------------------------------

def test_refuses_an_out_path_inside_the_repo(tmp_path):
    """A derived BOM artefact must never be writable into the checkout."""
    with pytest.raises(SystemExit) as excinfo:
        bom._refuse_if_inside_repo(bom.REPO_ROOT / "lines.json")
    assert "inside the repo" in str(excinfo.value)


def test_refuses_a_nested_out_path_inside_the_repo():
    """Not just the root — anywhere under it, including a plausible-looking scripts/ drop."""
    with pytest.raises(SystemExit):
        bom._refuse_if_inside_repo(bom.REPO_ROOT / "scripts" / "corpus" / "lines.json")


def test_allows_an_out_path_outside_the_repo(tmp_path):
    resolved = bom._refuse_if_inside_repo(tmp_path / "bom" / "lines.json")
    assert resolved == (tmp_path / "bom" / "lines.json").resolve()


# ---- header / column resolution --------------------------------------------------------------

def test_find_header_locates_the_row_carrying_every_required_token():
    rows = [
        ("1", ["Evergreen Energy", "", ""]),
        ("1", ["PART NUMBER", "DESCRIPTION", "GROUPING"]),
        ("1", ["7000727", "Linear actuator", "ACTUATOR"]),
    ]
    idx, header = bom.find_header(rows, ["PART NUMBER", "DESCRIPTION", "GROUPING"])
    assert idx == 1
    assert header["PART NUMBER"] == 0
    assert header["GROUPING"] == 2


def test_find_header_returns_none_when_a_required_token_is_absent():
    rows = [("1", ["PART NUMBER", "DESCRIPTION"])]
    idx, header = bom.find_header(rows, ["PART NUMBER", "DESCRIPTION", "GROUPING"])
    assert idx is None
    assert header == {}


def test_column_resolves_in_the_order_the_names_are_given():
    """`QTY` must not win over `TOTAL QTY` — the preference order is the disambiguation."""
    header = {"QTY": 3, "TOTAL QTY": 4}
    assert bom.column(header, "TOTAL QTY", "REQUIRED QTY", "QTY") == 4


def test_column_returns_none_when_nothing_matches():
    assert bom.column({"PART NUMBER": 0}, "UOM", "UNIT OF MEASURE") is None


# ---- part-number shape -----------------------------------------------------------------------

@pytest.mark.parametrize("part", ["7000727", "20436-000", "21086-100", "119940", "805001-US"])
def test_part_number_pattern_accepts_real_vendor_shapes(part):
    assert bom.PART_NUMBER_RE.fullmatch(part)


@pytest.mark.parametrize("junk", ["QTY", "1234", "Total:", "", "part 7000727", "7000727 EA"])
def test_part_number_pattern_rejects_non_part_cells(junk):
    assert not bom.PART_NUMBER_RE.fullmatch(junk)


def test_variant_suffix_collapses_shipping_variants_to_one_base_part():
    assert bom.VARIANT_SUFFIX_RE.sub("", "805001-SHIP") == "805001"
    assert bom.VARIANT_SUFFIX_RE.sub("", "805001-US") == "805001"
    # a meaningful trailing group is NOT a packaging variant and must survive
    assert bom.VARIANT_SUFFIX_RE.sub("", "20436-000") == "20436-000"


# ---- category proposal -----------------------------------------------------------------------

def test_vendor_grouping_beats_the_description_keyword_fallback():
    from collections import Counter

    # description says BOLT (-> hardware) but the vendor grouped it under TORQUE TUBE
    groupings: Counter[str] = Counter({"TORQUE TUBE": 3})
    assert bom._propose_category(groupings, "BOLT, TORQUE TUBE SPLICE") == "torque_tube"


def test_keyword_fallback_applies_only_when_the_grouping_is_unrecognised():
    from collections import Counter

    assert bom._propose_category(Counter(), "HEX NUT M6") == "hardware"
    assert bom._propose_category(Counter({"NOT A KNOWN GROUP": 1}), "GPS receiver") == "scada"
    assert bom._propose_category(Counter(), "something unclassifiable") == "other"


# ---- the migration stage refuses to emit a partial seed --------------------------------------

def test_migration_refuses_when_a_curated_part_is_missing_from_the_corpus(tmp_path):
    """A silently-short seed is the dangerous failure — 27 of 28 rows must not be emitted."""
    recon = tmp_path / "reconciliation.json"
    recon.write_text(json.dumps([
        {"part_number": bom.EQUIPMENT[0][0], "sources": ["Bradley 1 Customer BOM.pdf"]},
    ]))
    args = type("Args", (), {"reconciliation": str(recon)})()
    with pytest.raises(SystemExit) as excinfo:
        bom.cmd_migration(args)
    assert "not found in the reconciliation" in str(excinfo.value)
    assert "do not emit a partial seed" in str(excinfo.value)


def test_migration_emits_every_curated_row_when_all_parts_resolve(tmp_path, capsys):
    recon = tmp_path / "reconciliation.json"
    recon.write_text(json.dumps([
        {"part_number": pn, "sources": ["Bradley 1 Customer BOM.pdf"]}
        for pn, *_ in bom.EQUIPMENT
    ]))
    args = type("Args", (), {"reconciliation": str(recon)})()
    assert bom.cmd_migration(args) == 0
    emitted = [ln for ln in capsys.readouterr().out.strip().split("\n") if ln.strip()]
    assert len(emitted) == len(bom.EQUIPMENT)
    # SQL string literals must be escaped, not concatenated raw
    assert all(ln.strip().startswith("('") for ln in emitted)
