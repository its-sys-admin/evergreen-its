"""Tests for po_materials/po_generate.py — Worker-matching money math + the
deterministic PO render.

The money fixtures MIRROR safety_portal/test/po.test.ts so the two suites pin the
SAME numbers: draftBody() → EXPECTED {subtotal 125950, tax 11336, total 147286}
(auto-IL 900bp of 125950 = 11335.5 — the .5-rounds-UP boundary) and the per-watt
vector 400_000 W × 32_500_000 µ¢ → 13_000_000 ¢.

Run with: pytest -q tests/test_po_generate.py
"""
from __future__ import annotations

import io
from datetime import date
from typing import Any

import pytest

from po_materials import po_generate, vendors
from po_materials import terms as terms_lib

RATES_BP = {"IL": 900, "OR": 0}
STATE_NAMES = {"IL": "Illinois", "OR": "Oregon"}

LINES: list[dict[str, Any]] = [
    {"position": 1, "part_number": "RK-100", "description": "Rail 100", "qty": 10,
     "unit": "ea", "unit_cost_cents": 12_345, "extended_cents": 123_450,
     "watts": None, "panels": None, "pallets": None, "price_per_watt_microcents": None},
    {"position": 2, "part_number": "RK-200", "description": "Clamp kit", "qty": 2.5,
     "unit": "box", "unit_cost_cents": 1_000, "extended_cents": 2_500,
     "watts": None, "panels": None, "pallets": None, "price_per_watt_microcents": None},
]

PO: dict[str, Any] = {
    "id": 7,
    "po_number": "2026.001.2.0.0",
    "job_no": "2026.001",
    "site_phase": 2,
    "supersede_seq": 0,
    "revision": 0,
    "vendor_key": "VEN-000001",
    "job_id": "JOB-000017",
    "job_name": "Sunrise Solar",
    "ship_to_name": "Evergreen Renewables LLC",
    "ship_to_address": "100 Array Rd",
    "ship_to_city": "Rockford",
    "ship_to_state": "IL",
    "ship_to_zip": "61101",
    "delivery_contact_name": "Dana Field",
    "delivery_contact_phone": "555-0100",
    "delivery_contact_email": "dana@example.com",
    "sow_text": "Supply and deliver racking components.",
    "delivery_instructions": "Call site lead ahead of delivery.",
    "payment_terms_text": "Net 30",
    "terms_profile_id": "standard_17",
    "terms_version": "1",
    "subtotal_cents": 125_950,
    "tax_mode": "auto",
    "tax_rate_bp": 900,
    "tax_cents": 11_336,
    "shipping_cents": 10_000,
    "total_cents": 147_286,
    "line_column_variant": "default",
    "supersedes_po_id": None,
    "approver_name": "Alex Approver",
    "approver_title": "Director of Procurement",
    "created_by": "admin.alex",
}

VENDOR_ROW: dict[str, Any] = {
    "_row_id": 100,
    vendors.COL_VENDOR_NAME: "Chint Power Systems",
    vendors.COL_VENDOR_KEY: "VEN-000001",
    vendors.COL_ADDRESS: "2801 N State Hwy 78 Ste 100, Wylie TX",
    vendors.COL_CONTACT_NAME: "Jordan Lee",
    vendors.COL_CONTACT_EMAIL: "orders@chint.example",
    vendors.COL_CONTACT_PHONE: "555-0101",
    vendors.COL_ACTIVE: "Active",
}

PURCHASER: dict[str, Any] = {
    "config_version": 1,
    "entity": "Evergreen Renewables LLC",
    "address_lines": ["100 Spectrum Center Dr. STE 570", "Irvine, CA. 92618"],
    "phone": "888-303-6424",
    "invoice_routing": {
        "to": "invoices@example.com",
        "cc": ["ap-lead@example.com"],
    },
}


# ---- _js_round / line_extended_cents -------------------------------------------


def test_js_round_is_half_up_not_bankers() -> None:
    """ECMA Math.round semantics: .5 rounds UP — Python's banker's round() would
    give 2 for 2.5 and silently disagree with every signed .5-boundary value."""
    assert po_generate._js_round(2.5) == 3
    assert po_generate._js_round(0.5) == 1
    assert po_generate._js_round(11335.5) == 11336  # THE fixture boundary
    assert po_generate._js_round(2.4999) == 2
    assert po_generate._js_round(0.0) == 0
    assert round(2.5) == 2  # the disagreement this guards against


def test_line_extended_default_lines_match_worker() -> None:
    assert po_generate.line_extended_cents(LINES[0]) == 123_450  # 10 × 12345
    assert po_generate.line_extended_cents(LINES[1]) == 2_500    # 2.5 × 1000


def test_line_extended_per_watt_matches_worker_vector() -> None:
    """The vitest vector: 400kW at $0.325/W = $130,000.00."""
    line = {"qty": 1, "unit_cost_cents": None, "watts": 400_000,
            "price_per_watt_microcents": 32_500_000}
    assert po_generate.line_extended_cents(line) == 13_000_000


def test_line_extended_missing_unit_cost_is_zero() -> None:
    assert po_generate.line_extended_cents(
        {"qty": 5, "unit_cost_cents": None, "watts": None, "price_per_watt_microcents": None}
    ) == 0


# ---- recompute_totals -----------------------------------------------------------


def test_recompute_totals_matches_worker_expected() -> None:
    totals = po_generate.recompute_totals(LINES, "auto", 0, 10_000, "IL", rates_bp=RATES_BP)
    assert totals == po_generate.Totals(
        subtotal_cents=125_950, tax_rate_bp=900, tax_cents=11_336, total_cents=147_286
    )


def test_recompute_totals_exempt_matches_worker_per_watt_case() -> None:
    line = {"position": 1, "part_number": "", "description": "mods", "qty": 1,
            "unit": "W", "unit_cost_cents": None, "extended_cents": 13_000_000,
            "watts": 400_000, "panels": None, "pallets": None,
            "price_per_watt_microcents": 32_500_000}
    totals = po_generate.recompute_totals([line], "exempt", 0, 0, "IL", rates_bp=RATES_BP)
    assert totals == po_generate.Totals(13_000_000, 0, 0, 13_000_000)


def test_recompute_totals_override_and_included() -> None:
    override = po_generate.recompute_totals(LINES, "override", 850, 0, "", rates_bp=RATES_BP)
    assert override.tax_rate_bp == 850
    assert override.tax_cents == po_generate._js_round(125_950 * 850 / 10_000)
    included = po_generate.recompute_totals(LINES, "included", 900, 0, "IL", rates_bp=RATES_BP)
    assert included.tax_rate_bp == 0 and included.tax_cents == 0


def test_recompute_totals_auto_fails_closed_on_unknown_state() -> None:
    """The vitest twin: 'auto' on TX (not in the table) must refuse — a silent 0%
    would understate tax on a legal document."""
    with pytest.raises(po_generate.TotalsError):
        po_generate.recompute_totals(LINES, "auto", 0, 0, "TX", rates_bp=RATES_BP)
    with pytest.raises(po_generate.TotalsError):
        po_generate.recompute_totals(LINES, "banana", 0, 0, "IL", rates_bp=RATES_BP)


# ---- totals_mismatches (the render-time assert) ----------------------------------


def test_totals_mismatches_clean_on_signed_fixture() -> None:
    assert po_generate.totals_mismatches(PO, LINES, rates_bp=RATES_BP) == []


def test_totals_mismatches_catches_tampered_total() -> None:
    tampered = dict(PO, total_cents=147_287)
    problems = po_generate.totals_mismatches(tampered, LINES, rates_bp=RATES_BP)
    assert problems and any("total_cents" in p for p in problems)


def test_totals_mismatches_catches_per_line_skew() -> None:
    bad_lines = [dict(LINES[0], extended_cents=123_451), LINES[1]]
    problems = po_generate.totals_mismatches(PO, bad_lines, rates_bp=RATES_BP)
    assert any("extended_cents" in p for p in problems)


def test_totals_mismatches_returns_basis_failure_as_entry() -> None:
    """An unresolvable basis (unknown state) is a FENCE ENTRY, not a raise — the
    caller routes it to the Review Queue like any other mismatch."""
    unknown_state = dict(PO, ship_to_state="TX")
    problems = po_generate.totals_mismatches(unknown_state, LINES, rates_bp=RATES_BP)
    assert any("unknown_tax_state" in p for p in problems)


@pytest.mark.parametrize(
    "field, value",
    [
        ("tax_rate_bp", "bad-not-a-number"),
        ("shipping_cents", "N/A"),
        ("total_cents", "oops"),
    ],
)
def test_totals_mismatches_fences_malformed_header_field_never_raises(field, value) -> None:
    """Regression (PR #498 review BLOCKER): a signed-but-malformed numeric header
    field must be RETURNED as a `totals_basis:` fence entry, NEVER raise. HMAC proves
    the Worker signed the value, not that it is well-typed — so a Worker bug / schema
    drift / D1 tampering can present a non-numeric field. A raise here (po_poll Step 2
    is the FIRST guard, outside the per-row fence's try) would crash the whole batch
    and re-crash every cycle."""
    malformed = dict(PO)
    malformed[field] = value
    problems = po_generate.totals_mismatches(malformed, LINES, rates_bp=RATES_BP)
    assert any("totals_basis" in p for p in problems), problems


def test_totals_mismatches_fences_malformed_line_field_never_raises() -> None:
    """The line-loop conversions (extended_cents / qty / watts / unit_cost_cents) are
    inside the same guard — a malformed line field fences, never raises."""
    bad_lines = [dict(LINES[0], extended_cents="not-a-number")]
    problems = po_generate.totals_mismatches(PO, bad_lines, rates_bp=RATES_BP)
    assert any("totals_basis" in p for p in problems), problems


# ---- tax label / terms resolution ------------------------------------------------


def test_tax_line_labels_match_corpus_vocabulary() -> None:
    assert po_generate.tax_line_label("exempt", 0, "IL", STATE_NAMES) == "Tax Exempt"
    assert po_generate.tax_line_label("included", 0, "IL", STATE_NAMES) == "Sales Tax included"
    assert po_generate.tax_line_label("auto", 900, "IL", STATE_NAMES) == "9% IL Sales Tax"
    assert po_generate.tax_line_label("auto", 0, "OR", STATE_NAMES) == "Oregon has 0% State Tax"
    assert po_generate.tax_line_label("override", 875, "PA", STATE_NAMES) == "8.75% PA Sales Tax"


def test_resolve_terms_library_fills_tokens() -> None:
    render = po_generate.resolve_terms(
        "standard_17", "1",
        purchaser_entity="Evergreen Renewables LLC",
        seller_name="Chint Power Systems",
    )
    assert render.kind == "library"
    assert "Evergreen Renewables LLC" in render.text
    assert "{{" not in render.text  # STRICT fill — no blank left on a legal document


def test_resolve_terms_normalizes_v_prefixed_version() -> None:
    render = po_generate.resolve_terms(
        "standard_17", "v1",
        purchaser_entity="Evergreen Renewables LLC", seller_name="X",
    )
    assert render.kind == "library" and render.text


def test_resolve_terms_attach_kind_renders_reference_line_only() -> None:
    render = po_generate.resolve_terms(
        "negotiated_gtc", "",
        purchaser_entity="Evergreen Renewables LLC", seller_name="VSUN",
    )
    assert render.kind == "attach"
    assert "NEGOTIATED GENERAL TERMS AND CONDITIONS" in render.text


def test_resolve_terms_unknown_profile_raises() -> None:
    with pytest.raises(terms_lib.TermsError):
        po_generate.resolve_terms("field_21", "", purchaser_entity="E", seller_name="S")


# ---- render smoke -----------------------------------------------------------------


def _render(po: dict[str, Any] | None = None, lines: list[dict[str, Any]] | None = None,
            **kwargs: Any) -> bytes:
    terms = po_generate.resolve_terms(
        "standard_17", "1",
        purchaser_entity=PURCHASER["entity"],
        seller_name=str(VENDOR_ROW[vendors.COL_VENDOR_NAME]),
    )
    return po_generate.render_po_pdf(
        po if po is not None else PO,
        lines if lines is not None else LINES,
        VENDOR_ROW, PURCHASER, terms,
        po_date=date(2026, 7, 9),
        state_names=STATE_NAMES,
        **kwargs,
    )


def _page_text(pdf: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_render_produces_pdf_bytes() -> None:
    pdf = _render()
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 2_000


def test_render_is_byte_deterministic() -> None:
    """invariant=1 pins CreationDate + document ID — identical inputs must yield
    identical bytes (what makes §47 version-on-conflict a no-op on crash-retry)."""
    assert _render() == _render()


def test_render_contains_key_text() -> None:
    text = _page_text(_render())
    assert "PURCHASE ORDER" in text
    assert "2026.001.2.0.0" in text                # the PO number
    assert "Chint Power Systems" in text           # the SoR vendor snapshot (#494)
    assert "Evergreen Renewables LLC" in text      # the purchaser entity (D5)
    assert "9% IL Sales Tax" in text               # the state-labeled tax line
    assert "$1,472.86" in text                     # the total
    assert "Alex Approver" in text                 # autofilled Purchaser NAME (D9)
    assert "SUBJECT TO THE TERMS AND CONDITIONS" in text
    assert "invoices@example.com" in text  # invoice routing (D5)
    assert "Net 30" in text                        # payment terms


def test_render_supersession_clause_when_set() -> None:
    superseding = dict(PO, supersedes_po_id=5, supersede_seq=1,
                       po_number="2026.001.2.1.0")
    text = _page_text(_render(po=superseding, supersedes_po_number="2026.001.2.0.0"))
    assert "SUPERSEDE AND REPLACE" in text
    assert "2026.001.2.0.0" in text
    # And absent when not superseding.
    assert "SUPERSEDE AND REPLACE" not in _page_text(_render())


def test_render_supersession_degrades_without_predecessor_number() -> None:
    superseding = dict(PO, supersedes_po_id=5, supersede_seq=1)
    text = _page_text(_render(po=superseding, supersedes_po_number=None))
    assert "SUPERSEDE AND REPLACE" in text
    assert "SERIES 2026.001.2.0" in text  # family form, never an invented number


# ---- change-order clause ----------------------------------------------------


# The `-CO<digits>` grammar test lives beside the helper it pins:
# tests/test_po_numbering.py::test_change_order_parts_grammar (the private
# po_generate._change_order_parts was promoted to numbering.change_order_parts
# 2026-08-15 at the third consumer — render clause + po_naming CH token).


def test_render_change_order_clause_when_co_numbered() -> None:
    """A CO-numbered PO renders the change-order clause (parent named from the signed
    number) and NOT the supersession clause — a CO is not a supersession."""
    co_po = dict(PO, po_number="2026.001.2.0.0-CO1")
    text = " ".join(_page_text(_render(po=co_po)).split())  # collapse PDF line wraps
    assert "THIS DOCUMENT IS CHANGE ORDER NO. 1 TO PURCHASE ORDER 2026.001.2.0.0." in text
    assert "PURCHASE ORDER 2026.001.2.0.0 REMAINS IN FORCE AS MODIFIED HEREBY." in text
    assert "SUPERSEDE AND REPLACE" not in text


def test_render_normal_po_renders_neither_clause() -> None:
    text = " ".join(_page_text(_render()).split())
    assert "CHANGE ORDER NO." not in text
    assert "SUPERSEDE AND REPLACE" not in text


def test_render_non_digit_co_tail_renders_no_clause() -> None:
    """`-COX` is not the CO grammar — the helper returns None and NO clause renders
    (never a clause naming a parent the malformed suffix can't prove)."""
    bad = dict(PO, po_number="2026.001.2.0.0-COX")
    text = " ".join(_page_text(_render(po=bad)).split())
    assert "CHANGE ORDER NO." not in text
    assert "SUPERSEDE AND REPLACE" not in text


def test_render_per_watt_variant() -> None:
    per_watt_lines = [{
        "position": 1, "part_number": "", "description": "VSUN 545W modules",
        "qty": 1, "unit": "W", "unit_cost_cents": None, "extended_cents": 13_000_000,
        "watts": 400_000, "panels": 734, "pallets": 24,
        "price_per_watt_microcents": 32_500_000,
    }]
    per_watt_po = dict(
        PO, line_column_variant="per_watt", tax_mode="exempt", tax_rate_bp=0,
        tax_cents=0, shipping_cents=0, subtotal_cents=13_000_000, total_cents=13_000_000,
    )
    text = _page_text(_render(po=per_watt_po, lines=per_watt_lines))
    assert "Price per Watt" in text
    assert "400,000" in text
    assert "$130,000.00" in text
    assert "Tax Exempt" in text
