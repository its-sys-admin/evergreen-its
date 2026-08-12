"""Canonical RFQ PDF naming — ONE source for the vendor-suffixed document name + title.

Every surface an RFQ PDF's name lives on consumes THESE helpers so they cannot drift
(the multi-surface fan-out lesson): the Box file name + the Smartsheet review-row
attachment (`rfq_poll`), the emailed attachment (PR-D `rfq_send`), and the PDF's
internal ``/Title`` metadata (`rfq_generate`). Mirrors `po_naming` — same
`safety_naming.job_folder_name` sanitiser — with the VENDOR name in the identity
instead of the job: one RFQ fans out to N vendors (ADR-0004 R2, one PDF per vendor),
so the vendor is what disambiguates sibling files inside the same job's
per-job "RFQs" folder (the PO root's per-job tree). The RFQ number already encodes the job family.

Pure naming — no I/O, no external send. A blank vendor name falls back to a
number-only name so an edge case never crashes (the po_naming posture).
"""
from __future__ import annotations

from safety_reports import safety_naming


def rfq_pdf_filename(rfq_number: str, vendor_name: str | None) -> str:
    """The RFQ PDF file name: ``<Vendor>_RFQ_<rfq_number>.pdf`` (vendor-prefixed —
    the per-vendor twin of po_naming's job-prefixed ``<Job>_PO_<n>.pdf``; the vendor
    is this document's fan-out identity and the §47 version-on-conflict idempotency
    key). Falls back to ``RFQ <rfq_number>.pdf`` when the vendor name is empty."""
    vendor = safety_naming.job_folder_name(vendor_name or "").strip()
    return f"{vendor}_RFQ_{rfq_number}.pdf" if vendor else f"RFQ {rfq_number}.pdf"


def rfq_form_filename(rfq_number: str, vendor_name: str | None = None) -> str:
    """The fillable ``.xlsx`` quote-form file name (R3/R4). With a vendor name it is the
    vendor-suffixed ``<rfq_number> - <Vendor> - Quote Form.xlsx`` (the Box file
    ``rfq_poll`` writes at filing time, where the vendor disambiguates sibling forms in
    the same job's ``RFQs`` folder under the PO root); with none it falls back to the
    number-only ``RFQ <rfq_number> - Quote Form.xlsx`` (the email attachment name
    ``rfq_send`` uses at dispatch — the vendor name is not carried on the WSR-twin review
    row, and the attachment filename only needs its ``.xlsx`` extension to drive the
    engine's content-type). ONE helper so the Box name and the emailed name cannot drift
    (the multi-surface fan-out lesson). Pure naming — no I/O, no external send."""
    vendor = (vendor_name or "").strip()
    return (
        f"{rfq_number} - {vendor} - Quote Form.xlsx"
        if vendor
        else f"RFQ {rfq_number} - Quote Form.xlsx"
    )


def rfq_pdf_title(rfq_number: str, vendor_name: str | None) -> str:
    """The PDF's internal ``/Title`` metadata: ``Request for Quote <rfq_number> —
    <Vendor>`` (vendor appended). Falls back to ``Request for Quote <rfq_number>``
    when the vendor name is empty."""
    vendor = (vendor_name or "").strip()
    return (
        f"Request for Quote {rfq_number} — {vendor}"
        if vendor
        else f"Request for Quote {rfq_number}"
    )
