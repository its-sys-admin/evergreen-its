"""Canonical Subcontract document naming — ONE source for the job-prefixed document names.

Every surface a rendered Subcontract document's name lives on consumes THESE helpers so the
names cannot drift (the multi-surface fan-out lesson): the Box file names + the Smartsheet
row attachments (`subcontract_poll`) and the review inline attachments (`subcontract_review`).
The editable package that `subcontract_docx.render_package` produces is a Subcontract body
``.docx`` plus an Annex C Schedule-of-Values ``.xlsx`` (the operator's 2026-07-11 decision:
editable Office files, reviewed in Word/Excel before wet-signature — NOT a flat PDF). The
retained ``.pdf`` helpers name a future compiled/exported PDF surface (SC-S4 send).

Mirrors the Safety convention (`safety_naming`): the job name prefixes the document so the
same subcontract number on different jobs never shares a name, and a reviewer / recipient
sees the job at a glance. Reuses `safety_naming.job_folder_name` for identical sanitisation
(already the Subcontract Box-folder sanitiser in `subcontract_poll`).

Pure naming — no I/O, no external send. A blank job name falls back to the number-only name so
a numberless/jobless edge case never crashes.

This module also OWNS the subcontract lane's Box mirror-tree ROOT config key (below) —
the deliberately-light home `subcontract_poll` AND the Track 6 archive
(`field_ops/job_archive.py`) import it from, the same leaf-module posture as
`po_naming.CFG_BOX_PORTAL_ROOT` and `safety_naming.CFG_BOX_PORTAL_ROOT`.
"""
from __future__ import annotations

from safety_reports import safety_naming

# The subcontract lane's OWN Box mirror-tree ROOT (ITS_Config Setting + the Workstream cell
# its row is read under). Tree shape: ROOT → <job folder> holds the subcontract documents
# directly — the lane's Box tree is its own path, NOT a subtree of the safety root
# (operator directive 2026-08-12, extending the 2026-08-11 PO-lane split; before that the
# lane nested under `safety_naming.CFG_BOX_PORTAL_ROOT` → <job> → "Subcontracts").
# The root folder is built + seeded by `scripts/migrations/build_box_roots.py`; the
# `.portal_root_folder_id` suffix keeps it enrolled in `production_repoint.py`'s
# repoint allowlist alongside the safety/progress/po_materials siblings.
CFG_BOX_PORTAL_ROOT = "subcontracts.box.portal_root_folder_id"
CFG_BOX_PORTAL_ROOT_WORKSTREAM = "subcontracts"


def sc_docx_filename(sc_number: str, job_name: str | None) -> str:
    """The Subcontract body ``.docx`` file name: ``<Job>_Subcontract_<sc_number>.docx``
    (job-prefixed, matching the Safety ``<job>_<...>`` file style). Falls back to
    ``Subcontract <sc_number>.docx`` when the job name is empty (the number-only name)."""
    job = safety_naming.job_folder_name(job_name or "").strip()
    return f"{job}_Subcontract_{sc_number}.docx" if job else f"Subcontract {sc_number}.docx"


def sc_xlsx_filename(sc_number: str, job_name: str | None) -> str:
    """The Schedule-of-Values ``.xlsx`` file name:
    ``<Job>_Schedule of Values_<sc_number>.xlsx`` (job-prefixed, matching the Safety
    ``<job>_<...>`` file style). Falls back to ``Schedule of Values <sc_number>.xlsx`` when
    the job name is empty (the number-only name)."""
    job = safety_naming.job_folder_name(job_name or "").strip()
    return (
        f"{job}_Schedule of Values_{sc_number}.xlsx"
        if job
        else f"Schedule of Values {sc_number}.xlsx"
    )


def sc_exhibit_filename(sc_number: str, job_name: str | None) -> str:
    """The Exhibit A ``.docx`` file name: ``<Job>_Exhibit A_<sc_number>.docx`` (job-prefixed,
    matching the Safety ``<job>_<...>`` file style). Falls back to ``Exhibit A <sc_number>.docx``
    when the job name is empty (the number-only name)."""
    job = safety_naming.job_folder_name(job_name or "").strip()
    return f"{job}_Exhibit A_{sc_number}.docx" if job else f"Exhibit A {sc_number}.docx"


def sc_package_zip_filename(sc_number: str, job_name: str | None) -> str:
    """The combined SEND package ``.zip`` name: ``<Job>_Subcontract Package_<sc_number>.zip``
    (job-prefixed, matching the Safety ``<job>_<...>`` file style). Falls back to
    ``Subcontract Package <sc_number>.zip`` when the job name is empty. This is the SC-S4 send
    artifact (the Subcontract body ``.docx`` + Exhibit A ``.docx`` + Annex C SoV ``.xlsx`` in
    one ZIP, 2026-07-15 operator decision); its ``.zip`` extension drives the send engine's
    attachment content-type (``application/zip``)."""
    job = safety_naming.job_folder_name(job_name or "").strip()
    return (
        f"{job}_Subcontract Package_{sc_number}.zip"
        if job
        else f"Subcontract Package {sc_number}.zip"
    )


def sc_pdf_filename(sc_number: str, job_name: str | None) -> str:
    """The Subcontract PDF file name: ``<Job>_Subcontract_<sc_number>.pdf`` (job-prefixed,
    matching the Safety ``<job>_<...>.pdf`` file style). Falls back to
    ``Subcontract <sc_number>.pdf`` when the job name is empty (the number-only name)."""
    job = safety_naming.job_folder_name(job_name or "").strip()
    return f"{job}_Subcontract_{sc_number}.pdf" if job else f"Subcontract {sc_number}.pdf"


def sc_pdf_title(sc_number: str, job_name: str | None) -> str:
    """The PDF's internal ``/Title`` metadata: ``Subcontract <sc_number> — <Job>``
    (job appended). Falls back to ``Subcontract <sc_number>`` when the job name is
    empty."""
    job = (job_name or "").strip()
    return f"Subcontract {sc_number} — {job}" if job else f"Subcontract {sc_number}"
