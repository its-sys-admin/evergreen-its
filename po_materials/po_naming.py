"""Canonical PO PDF naming — ONE source for the job-prefixed document name + title.

Every surface a PO PDF's name lives on consumes THESE helpers so the four cannot drift
(the multi-surface fan-out lesson): the Box file name + the Smartsheet row attachment
(`po_poll`), the emailed attachment (`po_send`), and the PDF's internal ``/Title``
metadata (`po_generate`). Mirrors the Safety convention (`safety_naming`): the job name
prefixes the document so the same PO number on different jobs never shares a name, and a
reviewer / recipient sees the job at a glance. Reuses `safety_naming.job_folder_name` for
identical sanitisation (already the PO Box-folder sanitiser in `po_poll`).

Pure naming — no I/O, no external send. A blank job name falls back to the pre-existing
number-only name so a numberless/jobless edge case never crashes.

This module also OWNS the PO lane's Box mirror-tree ROOT config key (below) — the
deliberately-light home the three lane daemons (`po_poll` / `rfq_poll` /
`estimate_poll`) AND the Track 6 archive (`field_ops/job_archive.py`) import it from,
the same leaf-module posture as `safety_naming.CFG_BOX_PORTAL_ROOT`.
"""
from __future__ import annotations

from safety_reports import safety_naming

# The PO lane's OWN Box mirror-tree ROOT (ITS_Config Setting + the Workstream cell its
# row is read under). Tree shape: ROOT → <job folder> holds the PO PDFs directly, with
# "RFQs" and "Vendor Quotes" as child folders — the PO lane's Box tree is its own path,
# NOT a subtree of the safety root (operator directive 2026-08-11; before that the lane
# nested under `safety_naming.CFG_BOX_PORTAL_ROOT` → <job> → "Purchase Orders").
# The root folder is built + seeded by `scripts/migrations/build_box_roots.py`; the
# `.portal_root_folder_id` suffix keeps it enrolled in `production_repoint.py`'s
# repoint allowlist alongside the safety/progress twins.
CFG_BOX_PORTAL_ROOT = "po_materials.box.portal_root_folder_id"
CFG_BOX_PORTAL_ROOT_WORKSTREAM = "po_materials"


def po_pdf_filename(po_number: str, job_name: str | None) -> str:
    """The PO PDF file name: ``<Job>_PO_<po_number>.pdf`` (job-prefixed, matching the
    Safety ``<job>_<...>.pdf`` file style). Falls back to ``PO <po_number>.pdf`` when
    the job name is empty (the pre-2026-07 name)."""
    job = safety_naming.job_folder_name(job_name or "").strip()
    return f"{job}_PO_{po_number}.pdf" if job else f"PO {po_number}.pdf"


def po_attachment_filename(po_number: str, attachment_id: int, original_filename: str) -> str:
    """The filed name of a PO DOCUMENT ATTACHMENT (Feature B):
    ``PO_<po_number>_ATT<id>_<original>``. One source for BOTH delivery surfaces (the
    Box file in the job's "Purchase Orders" folder AND the PO_Log row attachment — the
    multi-surface fan-out lesson).

    The D1 attachment id is PART OF THE NAME — load-bearing (review BLOCKER fix): a PO
    carries up to 5 attachments and two uploads can share an original filename (two
    ``IMG_0001.jpg`` phone photos; two vendors' ``spec.pdf``). Without the id, the
    second upload would land as a NEW VERSION of the first in Box
    (`upload_bytes_or_new_version` keys on the name) AND `attach_pdf_to_row
    (replace=True)` would DELETE the first's PO_Log inline copy — silent loss of the
    first document on both surfaces. §47 version-on-conflict is for the SAME logical
    artifact re-filed (a crash-retry of THIS attachment keeps its id → same name →
    idempotent new version); two independent uploads are DISTINCT documents and get
    distinct names. The PO number still prefixes the name so attachments group beside
    their PO PDF; the original name (charset-bounded by the Worker gate) stays visible."""
    return f"PO_{po_number}_ATT{attachment_id}_{original_filename}"


def po_pdf_title(po_number: str, job_name: str | None) -> str:
    """The PDF's internal ``/Title`` metadata: ``Purchase Order <po_number> — <Job>``
    (job appended). Falls back to ``Purchase Order <po_number>`` when the job name is
    empty."""
    job = (job_name or "").strip()
    return f"Purchase Order {po_number} — {job}" if job else f"Purchase Order {po_number}"
