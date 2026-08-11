#!/usr/bin/env python3
"""Backfill the inline PDF attachments on existing PO/RFQ Smartsheet rows (2026-08-11).

Why
---
Until 2026-08-11 the PO lane attached its rendered PDF only to the PO_Pending_Review
row; the flat PO_Log row and BOTH per-job tracking-sheet mirrors ("<Jobs>/<job>/Purchase
Orders" and "<Jobs>/<job>/RFQs") carried only a link cell — the operator opened a row in
the PO workspace and found nothing attached. The filing code now attaches on every
surface (self-healing on re-serve), but rows filed BEFORE the change never re-serve, so
their attachments must be backfilled once. This script is that one-shot.

What it attaches, per surface (skip when the filename is already attached)
--------------------------------------------------------------------------
  - flat PO_Log rows:              the PO PDF (Box file id parsed from the "PO PDF" link cell)
  - per-job "Purchase Orders" rows: the PO PDF (same parse — the sheet is structure-cloned)
  - flat RFQ_Log rows:             the RFQ PDF ("Box PDF File ID" cell) + the vendor quote
  - per-job "RFQs" rows:           form (.xlsx), found by its deterministic name in the
                                   job's `<PO root>/<job>/RFQs/` Box folder

The Box file's OWN name is used as the attachment filename (it was uploaded under the
canonical `po_naming`/`rfq_naming` name), so the backfill can never invent a name that
the self-heal path would then duplicate.

Safety posture: PLAN-BY-DEFAULT (`--apply` + y/N to actuate); reads Box, writes ONLY
Smartsheet row attachments (no cells, no Box writes, no sends, no AI). Re-run safe —
already-attached rows print `[skip]`. Run AFTER relocate_po_box_folders.py (the quote-form
lookup resolves `<PO root>/<job>/RFQs/`).

Exit 0 on success/no-op/decline; 2 on missing config.
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from po_materials import po_log, po_naming, rfq_log, rfq_naming  # noqa: E402
from shared import box_client, sheet_ids, smartsheet_client  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_BOX_LINK = re.compile(r"app\.box\.com/file/(\d+)")

PERJOB_PO_SHEET = "Purchase Orders"
PERJOB_RFQ_SHEET = "RFQs"


def _row_attachment_names(sheet_id: int, row_id: int) -> set[str]:
    client = smartsheet_client.get_client()
    atts = client.Attachments.list_row_attachments(sheet_id, row_id)
    return {a.name for a in (atts.data or [])}


def _file_id_from_link(cell_value: Any) -> str | None:
    m = _BOX_LINK.search(str(cell_value or ""))
    return m.group(1) if m else None


def _perjob_sheets() -> list[tuple[str, int, str]]:
    """[(job folder name, sheet_id, kind)] for every per-job PO/RFQ tracking sheet."""
    client = smartsheet_client.get_client()
    out: list[tuple[str, int, str]] = []
    jobs_folder = client.Folders.get_folder(sheet_ids.FOLDER_PO_JOBS)
    for folder in jobs_folder.folders or []:
        sub = client.Folders.get_folder(folder.id)
        for sheet in sub.sheets or []:
            if sheet.name == PERJOB_PO_SHEET:
                out.append((folder.name, sheet.id, "po"))
            elif sheet.name == PERJOB_RFQ_SHEET:
                out.append((folder.name, sheet.id, "rfq"))
    return out


def _plan_po_rows(sheet_id: int, label: str) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for row in smartsheet_client.get_rows(sheet_id):
        file_id = _file_id_from_link(row.get(po_log.COL_PO_PDF))
        if file_id is None:
            print(f"[warn] {label}: row {row['_row_id']} ({row.get(po_log.COL_PO_NUMBER)!r}) "
                  f"has no parseable Box link — skipped.")
            continue
        plan.append({"sheet_id": sheet_id, "row_id": row["_row_id"], "label": label,
                     "file_id": file_id, "content_type": "application/pdf"})
    return plan


def _plan_rfq_rows(sheet_id: int, label: str, job_folder_name: str | None,
                   po_root: str | None) -> list[dict[str, Any]]:
    """RFQ rows: the PDF by its recorded Box id + the quote form by name in <job>/RFQs/."""
    plan: list[dict[str, Any]] = []
    rfqs_folder: str | None = None
    if job_folder_name is not None and po_root is not None:
        job_folder = box_client.find_child_folder(po_root, job_folder_name)
        if job_folder is not None:
            rfqs_folder = box_client.find_child_folder(job_folder, "RFQs")
    form_files: dict[str, str] = {}
    if rfqs_folder is not None:
        form_files = {i["name"]: i["id"] for i in box_client.list_folder(rfqs_folder, limit=1000)
                      if i["type"] == "file"}
    for row in smartsheet_client.get_rows(sheet_id):
        pdf_id = str(row.get(rfq_log.COL_BOX_PDF_FILE_ID) or "").strip()
        if pdf_id:
            plan.append({"sheet_id": sheet_id, "row_id": row["_row_id"], "label": label,
                         "file_id": pdf_id, "content_type": "application/pdf"})
        form_name = rfq_naming.rfq_form_filename(
            str(row.get(rfq_log.COL_RFQ_NUMBER) or ""), str(row.get(rfq_log.COL_VENDOR_NAME) or "")
        )
        form_id = form_files.get(form_name)
        if form_id:
            plan.append({"sheet_id": sheet_id, "row_id": row["_row_id"], "label": label,
                         "file_id": form_id, "content_type": XLSX_MIME})
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="actuate the plan (default: print it)")
    args = parser.parse_args()

    po_root = (smartsheet_client.get_setting(
        po_naming.CFG_BOX_PORTAL_ROOT, workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM
    ) or "").strip() or None
    if po_root is None:
        print("[warn] PO Box root unset — per-job RFQ quote-form lookups will be skipped.")

    plan = _plan_po_rows(sheet_ids.SHEET_PO_LOG, "PO_Log")
    plan += _plan_rfq_rows(sheet_ids.SHEET_RFQ_LOG, "RFQ_Log", None, None)
    for job_name, sid, kind in _perjob_sheets():
        if kind == "po":
            plan += _plan_po_rows(sid, f"{job_name}/Purchase Orders")
        else:
            plan += _plan_rfq_rows(sid, f"{job_name}/RFQs", job_name, po_root)
    # Flat RFQ_Log form attachments ride the per-job lookup only when the job is known;
    # the flat sheet's rows already carry both files from the 2026-07-20 ledger-attach
    # change, so the skip-if-present check below makes the omission harmless.

    todo: list[dict[str, Any]] = []
    for item in plan:
        meta = box_client.get_file_metadata(item["file_id"])
        name = str(meta.get("name") or "")
        if not name:
            print(f"[warn] {item['label']}: Box file {item['file_id']} has no name — skipped.")
            continue
        existing = _row_attachment_names(item["sheet_id"], item["row_id"])
        if name in existing:
            print(f"[skip] {item['label']} row {item['row_id']}: {name!r} already attached.")
            continue
        todo.append({**item, "name": name})
        print(f"[plan] {item['label']} row {item['row_id']}: attach {name!r}")

    if not args.apply:
        print(f"\n{len(todo)} attachment(s) would be made — pass --apply to actuate.")
        return 0
    if not todo:
        print("[skip] nothing to backfill.")
        return 0
    try:
        answer = input(f"Attach {len(todo)} file(s) to Smartsheet rows? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("Declined — nothing attached.")
        return 0
    for item in todo:
        data = box_client.download_file(item["file_id"])
        smartsheet_client.attach_pdf_to_row(
            item["sheet_id"], item["row_id"], item["name"], data,
            content_type=item["content_type"],
        )
        print(f"[ok] {item['label']} row {item['row_id']}: {item['name']!r}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
