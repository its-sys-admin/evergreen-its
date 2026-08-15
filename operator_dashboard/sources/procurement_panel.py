"""Panel: TTL-cached READ view of the three procurement ledgers (PO_Log,
Subcontract_Log, RFQ_Log).

Lane visibility from the ITS-owned Smartsheet ledgers: per lane, the row count
grouped by Status, a change-order census (a number cell containing "-CO" is a
DISTINCT change-order document cloned from its sent/executed parent — Track D2
— never a duplicate of the parent, which stays in force), and the most recent
rows. Read-only, no new capabilities, no writes; every cell is redacted +
escaped on render (Invariant 2), and a failed lane read degrades to an
"(unavailable)" row rather than 500ing the dashboard.

KNOWN UNDERSTATEMENT (docs/tech_debt.md, 2026-08-15 "one-way divergence"): the
office's hand-marked lifecycle facts (mark-submitted, PO accepted, subcontract
executed) land in D1 — the lanes' authoritative store — and are NOT mirrored
into these ledgers, whose status pass pushes only machine transitions. The
portal's per-job Procurement screen is the authoritative lifecycle read; this
panel says so in its header rather than pretending the ledgers are current.
"""
from __future__ import annotations

import importlib
from typing import Any

from operator_dashboard.cache import cached
from operator_dashboard.config import SMARTSHEET_TTL_SECONDS
from operator_dashboard.sources.base import (
    SEV_INFO,
    SEV_UNAVAILABLE,
    SEV_WARN,
    DataSource,
    PanelResult,
    clean,
)

# lane -> (sheet_ids attribute, the ledger's document-number column). Status and
# Last Modified share names across all three builders (build_*_log_sheet.py).
_PROCUREMENT_LEDGERS: list[tuple[str, str, str]] = [
    ("po", "SHEET_PO_LOG", "PO Number"),
    ("subcontracts", "SHEET_SUBCONTRACT_LOG", "SC Number"),
    ("rfq", "SHEET_RFQ_LOG", "RFQ Number"),
]
_STATUS_COL = "Status"
_MODIFIED_COL = "Last Modified"
# Track D2 change-order numbering: `{parent_number}-CO{seq}` (the marker rides
# into the filed PDF/package names too, which embed the number).
_CO_MARKER = "-CO"

RECENT_ROWS = 5
RECENT_ROWS_DETAIL = 25  # drill-down (/view/procurement_lanes) shows more

_CO_TOOLTIP = (
    "change order — a distinct document cloned from its sent/executed parent; "
    "the parent stays in force"
)


class ProcurementLanesSource(DataSource):
    panel_id = "procurement_lanes"
    title = "Procurement lanes — PO / subcontract / RFQ ledgers"

    def _load(self) -> dict[str, Any]:
        ss: Any = importlib.import_module("shared.smartsheet_client")
        sid: Any = importlib.import_module("shared.sheet_ids")
        per: list[dict[str, Any]] = []
        for lane, attr, number_col in _PROCUREMENT_LEDGERS:
            sheet_id = getattr(sid, attr, None)
            if not sheet_id:
                # Absent attr OR a `0` unseeded-sheet placeholder — surface it
                # rather than silently dropping the lane (the send-queue rule:
                # a missing lane must never read as "all clear").
                per.append({"lane": lane, "unavailable": True})
                continue
            try:
                raw = list(ss.get_rows(sheet_id))
            except Exception:
                per.append({"lane": lane, "unavailable": True})
                continue
            counts: dict[str, int] = {}
            co_count = 0
            projected: list[dict[str, str]] = []
            for r in raw:
                status = str(r.get(_STATUS_COL) or "").strip() or "(none)"
                counts[status] = counts.get(status, 0) + 1
                number = str(r.get(number_col) or "")
                if _CO_MARKER in number:
                    co_count += 1
                projected.append(
                    {
                        "number": number,
                        "status": status,
                        "modified": str(r.get(_MODIFIED_COL) or ""),
                    }
                )
            # Newest first. Last Modified is an ISO-ish system datetime, so the
            # string sort orders correctly; blanks sort last.
            projected.sort(key=lambda p: p["modified"], reverse=True)
            per.append(
                {
                    "lane": lane,
                    "unavailable": False,
                    "total": len(raw),
                    "counts": counts,
                    "co_count": co_count,
                    "recent": projected[:RECENT_ROWS_DETAIL],
                }
            )
        return {"per": per}

    def _fetch(self, detail: bool = False) -> PanelResult:
        data = cached("procurement_lanes", SMARTSHEET_TTL_SECONDS, self._load)
        recent_cap = RECENT_ROWS_DETAIL if detail else RECENT_ROWS
        rows: list[dict[str, str]] = []
        any_avail = False
        totals: list[str] = []
        co_total = 0
        for entry in data["per"]:
            lane = entry["lane"]
            if entry["unavailable"]:
                rows.append(
                    {"lane": clean(lane), "kind": "(unavailable)", "item": "—", "detail": "—", "_sev": SEV_WARN}
                )
                totals.append(f"{lane} ?")
                continue
            any_avail = True
            totals.append(f"{lane} {entry['total']}")
            co_total += int(entry["co_count"])
            for status, n in sorted(entry["counts"].items(), key=lambda kv: -kv[1]):
                rows.append(
                    {"lane": clean(lane), "kind": "status", "item": clean(status), "detail": clean(n), "_sev": SEV_INFO}
                )
            if entry["co_count"]:
                row = {
                    "lane": clean(lane),
                    "kind": "change orders",
                    "item": f"number contains {_CO_MARKER}",
                    "detail": clean(entry["co_count"]),
                    "_sev": SEV_INFO,
                }
                row["_title_kind"] = _CO_TOOLTIP
                rows.append(row)
            for p in entry["recent"][:recent_cap]:
                row = {
                    "lane": clean(lane),
                    "kind": "recent",
                    "item": clean(p["number"]),
                    "detail": clean(f"{p['status']} · {p['modified']}"),
                    "_sev": SEV_INFO,
                }
                if _CO_MARKER in p["number"]:
                    row["_title_item"] = _CO_TOOLTIP
                rows.append(row)
        census = " · ".join(totals)
        if co_total:
            census += f" · {co_total} change orders"
        # The understatement caveat rides the header (docs/tech_debt.md one-way
        # divergence): hand-marked lifecycle lives in D1, not these ledgers.
        summary = (
            f"{census} — ledgers can understate portal-marked lifecycle; "
            "the portal's per-job Procurement screen is the authoritative read"
            if any_avail
            else "unavailable"
        )
        return PanelResult(
            panel_id=self.panel_id,
            title=self.title,
            summary=summary,
            severity=SEV_INFO if any_avail else SEV_UNAVAILABLE,
            columns=["lane", "kind", "item", "detail"],
            rows=rows,
        )
