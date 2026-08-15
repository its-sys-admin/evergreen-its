"""Deterministic PO render + the Worker-matching integer-cents money math (PO S4).

Two halves, both DETERMINISTIC (no AI, no network, no clock reads — the caller
supplies the PO date), enforced by the capability gate (tests/test_capability_gating.py
GATED_SCRIPTS: graph_client / send_mail / resend / smtplib / email.mime / anthropic all
AST-forbidden — this is the generation side of Invariant 1):

1. **Money math** — `line_extended_cents` / `recompute_totals` mirror the Worker's
   `lineExtendedCents` / `computeTotals` (safety_portal/worker/po.ts) EXACTLY,
   including JS float semantics: products are computed in float64 and rounded with
   ECMA `Math.round` (half-up for the non-negative domain — Python's banker's
   `round()` would disagree on every .5 boundary, so `_js_round` exists). The 'auto'
   tax mode resolves through `po_materials/config/tax.json` (the SAME file the Worker
   imports at build time) and FAILS CLOSED on an unknown state. `totals_mismatches`
   is the render-time assert: any disagreement between the recompute and the SIGNED
   values means Worker↔Mac version skew (or a signed defect) — the caller REFUSES to
   file and fences to the Review Queue; a wrong number on a legal document is the
   one outcome this pipeline may never produce.

2. **PDF render** — `render_po_pdf` lays out the Family-A Evergreen PO (corpus S0
   report §3–§6: brand header, DATE / PO NUMBER, SHIP TO + DELIVERY CONTACT, Seller
   block, the three line-item column variants, state-labeled tax line, SOW /
   delivery-instructions / payment-terms sections, invoice-routing line, in-body
   supersession clause, T&C statement + terms block, dual signature blocks with the
   Purchaser side's NAME/TITLE autofilled). It REUSES `safety_reports.form_pdf`'s
   brand primitives (§14 parameterize-not-clone: the palette #1f4d2e/#b8860b, logo
   masthead, gold-rule section headers, footer canvas) so every ITS document shares
   ONE visual system. The render is BYTE-DETERMINISTIC for fixed inputs
   (`invariant=1` pins reportlab's CreationDate + document ID), which is what makes
   §47 version-on-conflict Box filing idempotent across crash-retries.

Vendor snapshot (the #494 security-review decision)
---------------------------------------------------
The vendor whose name/address/contact are EMBEDDED in the rendered PDF is resolved
from **ITS_Vendors (the Smartsheet SoR) at render time** — `po_poll` calls
`po_materials.vendors.get_vendor_by_key` and passes the row here — NOT from the D1
`po_vendors` cache and NOT from any client-supplied field. The queued draft carries
only the HMAC-covered `vendor_key`; a compromised/stale portal cache therefore cannot
put a wrong Seller identity on a legal document. The PDF is a SNAPSHOT: a later
vendor edit does not retro-change an issued PO (Box version history preserves each
render).
"""
from __future__ import annotations

import io
import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, NamedTuple

from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    Flowable,
    KeepTogether,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from po_materials import po_naming
from po_materials import terms as terms_lib
from po_materials import vendors as vendors_mod
from po_materials.po_log import format_total_cents as _money
from safety_reports.form_pdf import (
    _CONTENT_W,
    _FOOT,
    _GOLD,
    _LINE,
    _MARGIN,
    _TINT,
    _brand_header,
    _callout,
    _canvas_maker,
    _p,
    _rich_body,
    _section_header,
    _styles,
)

# ---- Money math (mirror worker/po.ts — see module docstring) -----------------------


class TotalsError(Exception):
    """Raised when a totals basis cannot be resolved (unknown tax state under 'auto',
    unknown tax mode). FAIL-CLOSED: the caller fences — never a silent 0% on a legal
    document (the Worker's computeTotals returns the same refusals as error codes)."""


class Totals(NamedTuple):
    """The recomputed money quad — the shape the assert compares to the SIGNED row."""

    subtotal_cents: int
    tax_rate_bp: int  # the RESOLVED rate (auto → table value; exempt/included → 0)
    tax_cents: int
    total_cents: int


def _js_round(x: float) -> int:
    """ECMA-262 `Math.round` for the non-negative domain: half rounds UP.

    Python's built-in `round()` is banker's (half-to-even) and disagrees on every
    exact .5 product — e.g. qty 2.5 × 1¢: JS Math.round(2.5)=3, Python round(2.5)=2.
    Implemented as floor + fractional-part compare (NOT `floor(x + 0.5)`, whose
    float addition mis-rounds the largest-double-below-.5 edge JS handles per spec).
    """
    f = math.floor(x)
    return int(f) + (1 if x - f >= 0.5 else 0)


def line_extended_cents(line: Mapping[str, Any]) -> int:
    """One line's extended integer cents — mirrors the Worker's `lineExtendedCents`.

    Per-watt lines (watts AND price_per_watt_microcents present) use
    round(watts × ppw / 1e6); every other line uses round(qty × unit_cost_cents).
    Products are computed in FLOAT (float64) exactly like JS — byte-agreement with
    the signed values matters more than exactness at astronomically-bounded inputs.
    """
    watts = line.get("watts")
    ppw = line.get("price_per_watt_microcents")
    if watts is not None and ppw is not None:
        return _js_round((float(watts) * float(ppw)) / 1_000_000.0)
    qty = float(line.get("qty") or 0)
    unit_cost = line.get("unit_cost_cents")
    return _js_round(qty * float(unit_cost if unit_cost is not None else 0))


def recompute_totals(
    lines: Sequence[Mapping[str, Any]],
    tax_mode: str,
    tax_rate_bp: int,
    shipping_cents: int,
    ship_to_state: str,
    *,
    rates_bp: Mapping[str, int] | None = None,
) -> Totals:
    """Recompute subtotal/tax/total from the lines' STORED extended cents — mirrors
    the Worker's `computeTotals` (which also sums stored extended values; per-line
    recompute-vs-stored is `totals_mismatches`' separate check).

    `tax_rate_bp` is the row's stored rate — consumed ONLY for mode 'override' (where
    the stored value IS the resolved override); 'auto' re-resolves from `rates_bp`
    (default: `po_materials/config/tax.json`, the same table the Worker builds in) and
    raises `TotalsError` on an unknown state (fail-closed); exempt/included → 0.
    """
    subtotal = 0
    for line in lines:
        subtotal += int(line.get("extended_cents") or 0)
    if tax_mode == "auto":
        table = rates_bp if rates_bp is not None else terms_lib.load_tax_config()["rates_bp"]
        if ship_to_state not in table:
            raise TotalsError(f"unknown_tax_state:{ship_to_state}")
        rate = int(table[ship_to_state])
    elif tax_mode == "override":
        rate = int(tax_rate_bp)
    elif tax_mode in ("exempt", "included"):
        rate = 0
    else:
        raise TotalsError(f"unknown_tax_mode:{tax_mode}")
    tax = _js_round((float(subtotal) * float(rate)) / 10_000.0)
    total = subtotal + tax + int(shipping_cents)
    return Totals(subtotal_cents=subtotal, tax_rate_bp=rate, tax_cents=tax, total_cents=total)


def totals_mismatches(
    po: Mapping[str, Any],
    lines: Sequence[Mapping[str, Any]],
    *,
    rates_bp: Mapping[str, int] | None = None,
) -> list[str]:
    """THE render-time totals assert: recompute everything and diff against the
    SIGNED row. Returns [] when clean; otherwise machine-readable mismatch strings
    (each names the field + both values — integers only, no PII). ANY entry means
    REFUSE + Review-Queue fence, never file (module docstring).

    NEVER RAISES. A totals-basis failure is returned as a `totals_basis:` mismatch,
    not raised — for BOTH an unresolvable tax mode/state (`TotalsError`) AND a
    non-numeric/mis-typed signed money field (`tax_rate_bp` / `shipping_cents` / a
    line's `extended_cents` / `qty` / `watts` / `unit_cost_cents` arriving as a
    non-number). HMAC proves the Worker signed THIS value — it says nothing about the
    value's TYPE (a Worker bug, schema drift, or direct D1 tampering can sign a
    malformed field). This call site (po_poll Step 2) is the per-row fence's FIRST
    guard, so a raise here would abort the whole batch and re-crash every cycle — the
    exact "one bad row never kills the cycle" invariant this returns-not-raises
    contract protects. Every conversion below is inside the guard.
    """
    problems: list[str] = []
    try:
        for line in lines:
            expected = line_extended_cents(line)
            stored = int(line.get("extended_cents") or 0)
            if expected != stored:
                problems.append(
                    f"line{line.get('position')}.extended_cents: recomputed {expected} != signed {stored}"
                )
        totals = recompute_totals(
            lines,
            str(po.get("tax_mode") or ""),
            int(po.get("tax_rate_bp") or 0),
            int(po.get("shipping_cents") or 0),
            str(po.get("ship_to_state") or ""),
            rates_bp=rates_bp,
        )
        for field, recomputed in (
            ("subtotal_cents", totals.subtotal_cents),
            ("tax_rate_bp", totals.tax_rate_bp),
            ("tax_cents", totals.tax_cents),
            ("total_cents", totals.total_cents),
        ):
            signed = int(po.get(field) or 0)
            if recomputed != signed:
                problems.append(f"{field}: recomputed {recomputed} != signed {signed}")
    except TotalsError as exc:
        problems.append(f"totals_basis: {exc}")
    except (ValueError, TypeError) as exc:
        problems.append(
            f"totals_basis: malformed numeric field ({type(exc).__name__}: {exc})"
        )
    return problems


# ---- Tax-line label + terms resolution ----------------------------------------------


def tax_line_label(
    tax_mode: str,
    tax_rate_bp: int,
    ship_to_state: str,
    state_names: Mapping[str, str] | None = None,
) -> str:
    """The Sales-Tax line's LABEL text, per the corpus toggle vocabulary (S0 §3):
    'Tax Exempt' | 'Sales Tax included' | 'Oregon has 0% State Tax' |
    '9% IL Sales Tax' (rate trimmed: 875bp → '8.75%'). `state_names` defaults to the
    tax.json table's display names; an unknown state degrades to its code."""
    if tax_mode == "exempt":
        return "Tax Exempt"
    if tax_mode == "included":
        return "Sales Tax included"
    if tax_rate_bp == 0:
        names = state_names if state_names is not None else terms_lib.load_tax_config()["state_names"]
        display = str(names.get(ship_to_state, ship_to_state) or ship_to_state)
        return f"{display} has 0% State Tax"
    pct = f"{tax_rate_bp / 100:g}"
    return f"{pct}% {ship_to_state} Sales Tax"


class TermsRender(NamedTuple):
    """What the T&C block renders: kind 'library' carries the token-filled verbatim
    terms text; kind 'attach' carries only the manifest's one-line reference (the
    negotiated-GTC document itself is attach-not-generate, D6)."""

    kind: str  # 'library' | 'attach'
    text: str


def _normalize_terms_version(version: str) -> str | None:
    """Draft-pinned version string → the manifest's version key. The manifest keys
    are bare ('1'); a client may pin 'v1' — strip the prefix. Blank → None (the
    loader resolves current_version)."""
    cleaned = (version or "").strip()
    if cleaned and cleaned[0] in ("v", "V"):
        cleaned = cleaned[1:]
    return cleaned or None


def resolve_terms(
    terms_profile_id: str,
    terms_version: str,
    *,
    purchaser_entity: str,
    seller_name: str,
) -> TermsRender:
    """Resolve the draft-pinned (profile, version) into render-ready terms.

    Library profiles load the sha256-verified verbatim text and STRICT-fill its
    tokens ({{purchaser_entity}} from purchaser.json, {{seller_name}} = the SoR
    vendor name); attach profiles yield only the manifest `render_line`. Any
    integrity/usage failure raises `terms_lib.TermsError` — a PERMANENT per-PO
    fence at the caller (a PO must never render with wrong or blank contract
    language).
    """
    profile = terms_lib.get_profile(terms_profile_id)
    if profile.get("kind") == "attach":
        return TermsRender(kind="attach", text=str(profile.get("render_line") or ""))
    text = terms_lib.load_terms_text(
        terms_profile_id, _normalize_terms_version(terms_version)
    )
    filled = terms_lib.substitute_tokens(
        text, {"purchaser_entity": purchaser_entity, "seller_name": seller_name}
    )
    return TermsRender(kind="library", text=filled)


# ---- PDF render ---------------------------------------------------------------------

_T_C_STATEMENT = "THIS PURCHASE ORDER IS SUBJECT TO THE TERMS AND CONDITIONS AS FOLLOWS:"


def _qty_display(value: Any) -> str:
    """qty for the table cell: integral → '10', else the ≤3dp decimal ('2.5')."""
    if value is None:
        return ""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _count_display(value: Any) -> str:
    """Optional integer counts (watts/panels/pallets) → thousands-grouped display."""
    if value is None:
        return ""
    return f"{int(value):,}"


def _ppw_display(microcents: Any) -> str:
    """price_per_watt_microcents → '$0.325000' style dollars-per-watt display."""
    if microcents is None:
        return ""
    dollars = int(microcents) / 100_000_000  # microcents → dollars (1e6 µ¢/¢ × 100 ¢/$)
    return f"${dollars:.6f}".rstrip("0").rstrip(".") if dollars else "$0"


def _address_lines(address: str) -> list[str]:
    """Split the ITS_Vendors comma-separated address block into display lines."""
    return [part.strip() for part in str(address or "").split(",") if part.strip()]


def _kv_block(title: str, rows: list[str], st: dict) -> Table:
    """A labeled block (SHIP TO / DELIVERY CONTACT / Seller) — title + value lines."""
    body: list[list[Any]] = [[_p(title, st["colhead"])]]
    for line in rows:
        body.append([_p(line, st["cell"])])
    t = Table(body, colWidths=[_CONTENT_W / 2 - 8])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (0, 0), 0.8, _GOLD),
        ("BACKGROUND", (0, 0), (0, 0), _TINT),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    # A half-width block placed directly in the frame (the lone SELLER block) must
    # hug the left grid line, not float platypus-centred (2026-07-23 polish fix).
    t.hAlign = "LEFT"
    return t


def _line_items_table(
    lines: Sequence[Mapping[str, Any]], variant: str, st: dict
) -> Table:
    """The line-items grid in the corpus's three column variants (S0 §5)."""
    if variant == "per_watt":
        head = ["Order Size (W)", "Panels", "Pallets", "Price per Watt", "Description", "Subtotal Amounts"]
        widths = [0.14, 0.10, 0.10, 0.14, 0.36, 0.16]
        rows = [
            [
                _count_display(line.get("watts")),
                _count_display(line.get("panels")),
                _count_display(line.get("pallets")),
                _ppw_display(line.get("price_per_watt_microcents")),
                str(line.get("description") or ""),
                _money(int(line.get("extended_cents") or 0)),
            ]
            for line in lines
        ]
    elif variant == "lump_sum":
        head = ["Price Breakdown", "Unit", "Description", "Subtotal Amounts"]
        widths = [0.18, 0.12, 0.52, 0.18]
        rows = [
            [
                _money(int(line["unit_cost_cents"])) if line.get("unit_cost_cents") is not None else "",
                str(line.get("unit") or ""),
                str(line.get("description") or ""),
                _money(int(line.get("extended_cents") or 0)),
            ]
            for line in lines
        ]
    else:  # default
        head = ["Part # / SKU", "Pieces", "Per Unit Cost", "Description", "Subtotal Amounts"]
        widths = [0.16, 0.10, 0.14, 0.42, 0.18]
        rows = [
            [
                str(line.get("part_number") or ""),
                _qty_display(line.get("qty")),
                _money(int(line["unit_cost_cents"])) if line.get("unit_cost_cents") is not None else "",
                str(line.get("description") or ""),
                _money(int(line.get("extended_cents") or 0)),
            ]
            for line in lines
        ]

    data: list[list[Any]] = [[_p(h, st["colhead"]) for h in head]]
    for row in rows:
        data.append([_p(cell, st["cell"]) for cell in row])
    t = Table(data, colWidths=[w * _CONTENT_W for w in widths], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _TINT),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, _GOLD),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, _LINE),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    return t


def _totals_table(po: Mapping[str, Any], state_names: Mapping[str, str], st: dict) -> Table:
    """Right-aligned Subtotal / tax-label / Shipping / TOTAL block."""
    rows: list[list[Any]] = [
        [_p("Subtotal", st["cellb"]), _p(_money(int(po.get("subtotal_cents") or 0)), st["cell"])],
    ]
    label = tax_line_label(
        str(po.get("tax_mode") or ""), int(po.get("tax_rate_bp") or 0),
        str(po.get("ship_to_state") or ""), state_names,
    )
    tax_cents = int(po.get("tax_cents") or 0)
    rows.append([_p(label, st["cellb"]),
                 _p(_money(tax_cents) if tax_cents else "—", st["cell"])])
    shipping = int(po.get("shipping_cents") or 0)
    if shipping:
        rows.append([_p("Shipping", st["cellb"]), _p(_money(shipping), st["cell"])])
    rows.append([_p("TOTAL", st["colhead"]),
                 _p(_money(int(po.get("total_cents") or 0)), st["cellb"])])
    t = Table(rows, colWidths=[2.4 * 72, 1.4 * 72], hAlign="RIGHT")
    t.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, _LINE),
        ("LINEABOVE", (0, -1), (-1, -1), 1.2, _GOLD),
        ("BACKGROUND", (0, -1), (-1, -1), _TINT),  # the TOTAL row reads as the answer
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _signature_block(title: str, name: str, title_line: str, st: dict) -> Table:
    """One signature block: tinted party-title band, then labelled fill lines
    (Date / NAME / TITLE / SIGNATURE) drawn as hairline RULES rather than typed
    underscore runs (2026-07-23 polish). Autofilled values (the Purchaser
    NAME/TITLE, D9) print ON their line; blank lines stay signable in print. The
    corpus's dual blocks are two of these side-by-side."""
    half = _CONTENT_W / 2 - 8
    label_w = 0.95 * 72
    body: list[list[Any]] = [
        [_p(title, st["colhead"]), ""],
        [_p("Date", st["colhead"]), _p("", st["cell"])],
        [_p("NAME", st["colhead"]), _p(name or "", st["cellb"])],
        [_p("TITLE", st["colhead"]), _p(title_line or "", st["cell"])],
        [Spacer(1, 18), ""],
        [_p("SIGNATURE", st["colhead"]), _p("", st["cell"])],
    ]
    t = Table(body, colWidths=[label_w, half - label_w])
    t.setStyle(TableStyle([
        ("SPAN", (0, 0), (1, 0)),
        ("LINEBELOW", (0, 0), (1, 0), 0.8, _GOLD),
        ("BACKGROUND", (0, 0), (1, 0), _TINT),
        # The fill lines: a visible grey rule under each value cell.
        ("LINEBELOW", (1, 1), (1, 3), 0.6, _FOOT),
        ("LINEBELOW", (1, 5), (1, 5), 0.6, _FOOT),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]))
    return t


def _change_order_parts(po_number: str) -> tuple[str, int] | None:
    """Split a change-order PO number `{parent}-CO{seq}` → (parent_number, seq), else None.

    A change order is a NORMAL lane document cloned from a SENT parent; the Worker
    mints its number as `{parent_po_number}-CO{seq}` at generate time (e.g.
    `2026.384.1.0.0` → `2026.384.1.0.0-CO1`). The parent stays in force — this is
    NOT a supersession (`supersedes_po_id` is NULL on a CO), so the two in-body
    clauses are mutually exclusive in practice, though the render tolerates both.

    §42 — why derive from the number STRING and not a D1 column: the po_number is
    inside the signed HMAC canonical the daemon has already verified, so the parent
    identity printed in a legal clause re-derives from SIGNED data. The Worker's
    `change_order_of`/`co_seq` columns are STORE-ONLY (the `estimate_id` precedent,
    worker/po.ts) and outside the po:v1 canonical — an unsigned column could drift
    or be tampered without failing verification, and a clause must never name a
    parent the signature does not cover. The `-CO<digits>` suffix is deliberately
    NOT part of the base D7 grammar (`numbering.parse_po_number` rejects it; see
    po_materials/numbering.py), so this is a local rsplit, not a parse_* call.
    A malformed tail (non-digits, empty head) returns None — NO clause renders
    rather than a wrong one. Mirrors subcontracts/subcontract_docx.py's twin
    (§14: two 10-line consumers in unlinked lanes — duplicated, cross-referenced).
    """
    value = (po_number or "").strip()
    if "-CO" not in value:
        return None
    head, tail = value.rsplit("-CO", 1)
    if not head or not tail.isascii() or not tail.isdigit():
        return None
    return head, int(tail)


def render_po_pdf(
    po: Mapping[str, Any],
    lines: Sequence[Mapping[str, Any]],
    vendor: Mapping[str, Any],
    purchaser: Mapping[str, Any],
    terms: TermsRender,
    *,
    po_date: date,
    supersedes_po_number: str | None = None,
    state_names: Mapping[str, str] | None = None,
) -> bytes:
    """Render one Family-A purchase order → PDF bytes. DETERMINISTIC for fixed inputs.

    Args:
        po: the HMAC-VERIFIED /pending row (canonical header fields; the money
            values here have already passed `totals_mismatches` — this function
            renders, it does not re-police).
        lines: the row's `line_items`, position-ordered.
        vendor: the ITS_VendORS SoR row from `vendors.get_vendor_by_key` — the
            render-time snapshot embedded in the document (see module docstring;
            #494 decision).
        purchaser: `terms_lib.load_purchaser_config()` — entity, address_lines,
            phone, invoice_routing (D5 versioned config; NEVER hard-coded here).
        terms: `resolve_terms(...)` output (library text token-filled, or the
            attach-kind reference line).
        po_date: the DATE printed on the PO (the caller's filing date — passed in
            so the render itself stays clock-free/deterministic).
        supersedes_po_number: the predecessor's contractual number when this PO
            supersedes one (None + a set `supersedes_po_id` degrades to family-form
            wording — never an invented number).
        state_names: tax.json display names (default: loaded from config).

    `invariant=1` pins reportlab's CreationDate + document ID so identical inputs
    yield identical bytes — the §47 version-on-conflict Box filing then makes a
    crash-retry a byte-identical new version, not a divergent document.
    """
    st = _styles()
    names = state_names if state_names is not None else terms_lib.load_tax_config()["state_names"]
    entity = str(purchaser.get("entity") or "")
    # Second-gen letterhead (2026-07-23): the document identity — PURCHASE ORDER,
    # PO number, date — rides the band's right column; the old separate DATE/PO
    # NUMBER band below the masthead is retired. Same text layer, one band higher.
    flow: list[Flowable] = _brand_header(
        "", st, doc_label="PURCHASE ORDER",
        meta_lines=[("PO NUMBER", str(po.get("po_number") or "")),
                    ("DATE", po_date.strftime("%-m/%-d/%Y"))],
    )

    # Purchaser identity under the masthead (D5 versioned config).
    for line in [entity, *purchaser.get("address_lines", []),
                 f"PH {purchaser.get('phone')}" if purchaser.get("phone") else ""]:
        if line:
            flow.append(_p(line, st["meta"]))
    flow.append(Spacer(1, 10))

    # SHIP TO + DELIVERY CONTACT side-by-side.
    ship_city_line = " ".join(
        part for part in (
            f"{po.get('ship_to_city')}," if po.get("ship_to_city") else "",
            str(po.get("ship_to_state") or ""),
            str(po.get("ship_to_zip") or ""),
        ) if part
    )
    ship_rows = [str(v) for v in (
        po.get("ship_to_name"), po.get("job_name"), po.get("ship_to_address"), ship_city_line,
    ) if v]
    contact_rows = [str(v) for v in (
        po.get("delivery_contact_name"), po.get("delivery_contact_phone"),
        po.get("delivery_contact_email"),
    ) if v]
    pair = Table(
        [[_kv_block("SHIP TO", ship_rows, st),
          _kv_block("DELIVERY CONTACT", contact_rows, st)]],
        colWidths=[_CONTENT_W / 2, _CONTENT_W / 2],
    )
    pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    flow.append(pair)
    flow.append(Spacer(1, 8))

    # Seller / Supplier block (the render-time ITS_Vendors snapshot — #494).
    seller_rows = [
        str(vendor.get(vendors_mod.COL_VENDOR_NAME) or ""),
        *_address_lines(str(vendor.get(vendors_mod.COL_ADDRESS) or "")),
    ]
    contact_bits = [
        str(vendor.get(col) or "").strip()
        for col in (vendors_mod.COL_CONTACT_NAME, vendors_mod.COL_CONTACT_PHONE,
                    vendors_mod.COL_CONTACT_EMAIL)
    ]
    contact_line = " · ".join(bit for bit in contact_bits if bit)
    if contact_line:
        seller_rows.append(contact_line)
    flow.append(_kv_block("SELLER / SUPPLIER", [r for r in seller_rows if r], st))
    flow.append(Spacer(1, 8))

    # Line items + totals.
    flow.append(_line_items_table(lines, str(po.get("line_column_variant") or "default"), st))
    flow.append(Spacer(1, 6))
    flow.append(_totals_table(po, names, st))

    # SOW / delivery instructions / payment terms.
    if str(po.get("sow_text") or "").strip():
        flow.append(_section_header("SCOPE OF WORK", st))
        flow.extend(_rich_body(str(po.get("sow_text")), st))
    if str(po.get("delivery_instructions") or "").strip():
        flow.append(_section_header("DELIVERY INSTRUCTIONS", st))
        flow.extend(_rich_body(str(po.get("delivery_instructions")), st))
    if str(po.get("payment_terms_text") or "").strip():
        flow.append(_section_header("PAYMENT TERMS", st))
        flow.extend(_rich_body(str(po.get("payment_terms_text")), st))

    # Invoice routing (D5 config, corpus §3 — lookup/config, never free text).
    routing = purchaser.get("invoice_routing") or {}
    cc_list = ", ".join(str(c) for c in routing.get("cc", []))
    routing_line = f"Please send all invoices to {routing.get('to', '')}"
    if cc_list:
        routing_line += f", cc: {cc_list}"
    routing_line += f". Reference PO {po.get('po_number')} on all invoices."
    flow.append(Spacer(1, 8))
    flow.append(_callout(routing_line, st))

    # In-body supersession clause (D7, corpus §4) — rendered ONLY when this PO
    # actually supersedes one.
    if po.get("supersedes_po_id") is not None:
        if supersedes_po_number:
            clause = (
                f"THIS PURCHASE ORDER IS ISSUED TO SUPERSEDE AND REPLACE THE PREVIOUSLY "
                f"ISSUED PURCHASE ORDER #{supersedes_po_number}, RENDERING THE PRIOR "
                f"PURCHASE ORDER NULL AND VOID."
            )
        else:
            # Predecessor number unresolvable (pre-pipeline / hand-issued) — degrade
            # to family-form wording, never an invented number.
            family = (
                f"{po.get('job_no')}.{po.get('site_phase')}."
                f"{max(int(po.get('supersede_seq') or 0) - 1, 0)}"
            )
            clause = (
                f"THIS PURCHASE ORDER IS ISSUED TO SUPERSEDE AND REPLACE THE PREVIOUSLY "
                f"ISSUED PURCHASE ORDER OF SERIES {family} FOR THIS PROJECT, RENDERING "
                f"THE PRIOR PURCHASE ORDER NULL AND VOID."
            )
        flow.append(Spacer(1, 8))
        flow.append(_callout(clause, st))

    # In-body change-order clause — rendered when this PO's OWN number carries the
    # Worker-minted `-CO<seq>` suffix (parent derived from the SIGNED number string,
    # never the store-only D1 columns — see _change_order_parts). The parent REMAINS
    # in force: a CO is not a supersession, so this clause and the one above are
    # mutually exclusive in practice (supersedes_po_id is NULL on a CO) but render
    # independently and coexist harmlessly if a record ever carried both.
    co_parts = _change_order_parts(str(po.get("po_number") or ""))
    if co_parts is not None:
        parent_number, co_seq = co_parts
        co_clause = (
            f"THIS DOCUMENT IS CHANGE ORDER NO. {co_seq} TO PURCHASE ORDER "
            f"{parent_number}. PURCHASE ORDER {parent_number} REMAINS IN FORCE AS "
            f"MODIFIED HEREBY."
        )
        flow.append(Spacer(1, 8))
        flow.append(_callout(co_clause, st))

    # T&C statement + block (D6).
    flow.append(_section_header("TERMS AND CONDITIONS", st))
    flow.append(_p(_T_C_STATEMENT, st["legal"]))
    flow.append(Spacer(1, 4))
    if terms.kind == "attach":
        flow.append(_p(terms.text, st["body"]))
    else:
        flow.extend(_rich_body(terms.text, st))

    # Dual signature blocks (always present — corpus §3).
    sig_pair = Table(
        [[
            _signature_block(
                f"PURCHASER — {entity}",
                str(po.get("approver_name") or ""),
                str(po.get("approver_title") or ""),
                st,
            ),
            _signature_block(
                f"SELLER / SUPPLIER — {vendor.get(vendors_mod.COL_VENDOR_NAME) or ''}",
                "", "", st,
            ),
        ]],
        colWidths=[_CONTENT_W / 2, _CONTENT_W / 2],
    )
    sig_pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    flow.append(Spacer(1, 14))
    flow.append(KeepTogether([sig_pair]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        title=po_naming.po_pdf_title(str(po.get("po_number") or ""), po.get("job_name")),
        leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=0.7 * 72,
        # Byte-determinism: pins CreationDate + the document /ID (see docstring).
        invariant=1,
    )
    doc.build(flow, canvasmaker=_canvas_maker(f"Purchase Order {po.get('po_number')}"))
    return buf.getvalue()
