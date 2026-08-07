"""One-shot migration: seed the `po_materials.estimate_poll.*` ITS_Config rows
(ADR-0004 E2 — the vendor-estimate importer daemon).

Companion to `po_materials/estimate_poll.py`. Run once at PR landing; safe to
re-run (per-row idempotency-guarded on Setting+Workstream — the
seed_po_materials_config.py pattern).

Why the gate row exists even though the value ships FALSE — the dark-ship gate
reflex (HOUSE_REFLEXES §5): a boolean gate read via `_read_bool_setting(default=
False)` treats a MISSING row identically to `false`, so a capability that "ships
dark" without a seeded row has NO visible switch at all. Seeding the row `false`
in the same change that adds the gated code makes activation a visible cell-flip,
and the #336 `resolve_and_log` startup pass stops WARNing `config_row_missing`.

What it seeds (10 rows, workstream `po_materials`):

    po_materials.estimate_poll.polling_enabled       = false  (the ONE daemon gate — dark)
    po_materials.estimate_poll.poll_interval_seconds = 120    (install-time cadence)
    po_materials.estimate_poll.max_pages_preview     = 12     (runtime preview cap)

  Extraction-ladder rows (PR-B, ADR-0004 E4-E6 — the three tier gates dark):

    po_materials.estimate_extract.tier1_enabled        = false
    po_materials.estimate_extract.tier1_xlsx_enabled   = false
    po_materials.estimate_extract.tier2_enabled        = false
    po_materials.estimate_extract.ocr_enabled          = false
    po_materials.estimate_extract.model                = qwen3.5:9b
    po_materials.estimate_extract.ollama_base_url      = http://127.0.0.1:11434
    po_materials.estimate_extract.confidence_threshold = 0.75
    po_materials.estimate_extract.timeout_seconds      = 600

The §34 screener's ClamAV layer deliberately gets NO new row — estimate_poll
REUSES the existing `po_materials.po_attach_screen.clamav_enabled` gate
(seeded false by seed_po_materials_config.py).

Auth: ITS_SMARTSHEET_TOKEN from macOS Keychain (same path the runtime SDK uses).

Run from `~/its` with the venv activated:

    python3 scripts/migrations/seed_estimates_config.py

Exit code 0 on success or no-op; nonzero on any error.
"""
from __future__ import annotations

import sys
from typing import Any

import requests  # type: ignore[import-untyped]

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from shared import keychain, sheet_ids  # noqa: E402

BASE = "https://api.smartsheet.com/2.0"

WORKSTREAM = "po_materials"

CONFIG_ROWS: list[dict[str, Any]] = [
    {
        "Setting": "po_materials.estimate_poll.polling_enabled",
        "Workstream": WORKSTREAM,
        "Value": "false",
        "Description": (
            "Gate for the vendor-estimate importer daemon (estimate_poll, ADR-0004 "
            "E2): pull uploaded estimates from the Worker pool, est:v1 HMAC-verify, "
            "§34 doc-screen, doc-type-classify (invoices/AP reports refused), file "
            "clean docs to Box + Estimate_Log, post disposition-screen previews, "
            "report needs_review. Ships FALSE (dark). Flip to 'true' ONLY after "
            "(a) the Worker is deployed with the estimate routes + "
            "PORTAL_ESTIMATE_API_TOKEN secret, (b) Keychain holds "
            "ITS_PORTAL_ESTIMATE_TOKEN, (c) Estimate_Log is built and "
            "SHEET_ESTIMATE_LOG flipped (build_estimate_log_sheet.py), and (d) the "
            "E2 partial live smoke has passed on the mirror. This daemon NEVER "
            "sends anything customer-facing (generation half of the External Send "
            "Gate)."
        ),
    },
    {
        "Setting": "po_materials.estimate_poll.poll_interval_seconds",
        "Workstream": WORKSTREAM,
        "Value": "120",
        "Description": (
            "Integer seconds between estimate_poll cycles. Read at INSTALL time by "
            "scripts/launchd/install.sh to substitute into the plist's StartInterval "
            "(the value is BAKED into the installed plist — changes take effect at "
            "the next `install.sh load org.solutionsmith.its.estimate-poll`, not "
            "hot). Default 120s: estimates are an office-upload trickle, staggered "
            "off portal-poll (60s) and po-poll (90s)."
        ),
    },
    {
        "Setting": "po_materials.estimate_poll.max_pages_preview",
        "Workstream": WORKSTREAM,
        "Value": "12",
        "Description": (
            "Max pages rendered as disposition-screen preview PNGs per estimate "
            "(Quartz inside the killable estimate_sandbox child; Pillow re-encoded; "
            "each preview capped at the Worker's 1 MB decoded limit). Read at "
            "RUNTIME by estimate_poll each cycle; clamped 1-50. Pages beyond the "
            "cap simply have no preview — the disposition screen's no-preview "
            "acknowledgment path covers them."
        ),
    },
    {
        "Setting": "po_materials.estimate_extract.tier1_enabled",
        "Workstream": WORKSTREAM,
        "Value": "false",
        "Description": (
            "Gate for the Tier-1 DETERMINISTIC extraction (estimate_parse "
            "template→generic ladder over native-text PDF pages; no AI). Ships "
            "FALSE (dark). Flip to 'true' ONLY after the extraction-core modules "
            "have landed AND the offline corpus eval "
            "(scripts/eval_estimate_ladder.py) qualifies Tier-1 quality against "
            "tests/fixtures/estimate_corpus_expectations.json. Gate off → every "
            "native-text doc lands needs_review (manual Tier-3), exactly the "
            "PR-A behavior."
        ),
    },
    {
        "Setting": "po_materials.estimate_extract.tier1_xlsx_enabled",
        "Workstream": WORKSTREAM,
        "Value": "false",
        "Description": (
            "Gate for the Tier-1 DETERMINISTIC VENDOR-SPREADSHEET extraction "
            "(estimate_parse.parse_xlsx_estimate: sandboxed openpyxl grid -> the same "
            "generic-table line logic as the PDF tier, with doc-level totals read from "
            "the CELL grid rather than regexed from flattened text). NO AI. Separate "
            "from tier1_enabled because it is a distinct parse path with its own "
            "failure modes. ON: a vendor .xlsx quote is extracted onto the disposition "
            "screen instead of being hand-keyed. OFF: those documents land needs_review "
            "(manual Tier-3) — no other lane is affected either way. Independent of "
            "Tier 0, which claims ITS's own HMAC-verified quote form first and is "
            "always on. Note .xlsx documents get no rendered preview, so an accepting "
            "import rides the 'no preview available - I verified against the original' "
            "acknowledgment, exactly as a verified Tier-0 form does."
        ),
    },
    {
        "Setting": "po_materials.estimate_extract.tier2_enabled",
        "Workstream": WORKSTREAM,
        "Value": "false",
        "Description": (
            "Gate for the Tier-2 LOCAL-Ollama schema-constrained extraction "
            "(estimate_extract; localhost only — vendor pricing never leaves the "
            "machine, ADR-0004 decision 1; at most ONE Tier-2 document per "
            "cycle). Ships FALSE (dark). Flip to 'true' ONLY after (a) the "
            "pinned model is pulled on this host (`ollama pull <model row>`), "
            "(b) the offline corpus eval qualifies Tier-2 quality "
            "(scripts/eval_estimate_ladder.py --tier2), and (c) Tier-1 is "
            "already live. NO cloud AI — this gate never enables any "
            "network-egress inference."
        ),
    },
    {
        "Setting": "po_materials.estimate_extract.ocr_enabled",
        "Workstream": WORKSTREAM,
        "Value": "false",
        "Description": (
            "Gate for the macOS-Vision OCR pass (estimate_ocr) that feeds "
            "SCANNED documents into Tier-2. Ships FALSE (dark); meaningful only "
            "with tier2_enabled=true. Off → scanned docs (no native text) land "
            "needs_review for manual Tier-3 entry."
        ),
    },
    {
        "Setting": "po_materials.estimate_extract.model",
        "Workstream": WORKSTREAM,
        "Value": "qwen3.5:9b",
        "Description": (
            "Pinned local Ollama model for Tier-2 extraction. SWAPPING IT "
            "RE-RUNS THE OFFLINE CORPUS EVAL to re-qualify (ADR-0004 decision "
            "1) — change the row only alongside an eval run. The model must be "
            "pulled on this host before tier2_enabled flips true."
        ),
    },
    {
        "Setting": "po_materials.estimate_extract.ollama_base_url",
        "Workstream": WORKSTREAM,
        "Value": "http://127.0.0.1:11434",
        "Description": (
            "Local Ollama base URL for Tier-2 extraction. LOCALHOST ONLY by "
            "doctrine — vendor pricing never leaves the machine (ADR-0004 "
            "decision 1); never point this at a remote host."
        ),
    },
    {
        "Setting": "po_materials.estimate_extract.confidence_threshold",
        "Workstream": WORKSTREAM,
        "Value": "0.75",
        "Description": (
            "Minimum Tier-2 extraction confidence (0-1) to post 'extracted'; "
            "below it the document degrades to needs_review (the disposition "
            "screen's manual Tier-3). Read at runtime each cycle."
        ),
    },
    {
        "Setting": "po_materials.estimate_extract.timeout_seconds",
        "Workstream": WORKSTREAM,
        "Value": "600",
        "Description": (
            "Wall-clock budget in seconds for one Tier-2 extraction call "
            "(keep_alive=0 load-on-demand makes the first call slow — the model "
            "loads from disk). On timeout the document degrades to needs_review."
        ),
    },
]


def _headers() -> dict[str, str]:
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_json(path: str, body: Any) -> dict[str, Any]:
    r = requests.post(BASE + path, headers=_headers(), json=body)
    r.raise_for_status()
    json_body: dict[str, Any] = r.json()
    return json_body


def _get_json(path: str) -> dict[str, Any]:
    r = requests.get(BASE + path, headers=_headers())
    r.raise_for_status()
    json_body: dict[str, Any] = r.json()
    return json_body


def _find_config_row(
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    setting: str,
    workstream: str,
) -> dict[str, Any] | None:
    col_id_by_title = {c["title"]: c["id"] for c in columns}
    setting_col = col_id_by_title["Setting"]
    workstream_col = col_id_by_title["Workstream"]
    for row in rows:
        s = w = None
        for cell in row.get("cells", []):
            if cell.get("columnId") == setting_col:
                s = cell.get("value")
            elif cell.get("columnId") == workstream_col:
                w = cell.get("value")
        if s == setting and w == workstream:
            return row
    return None


def seed_config_rows() -> list[tuple[str, str]]:
    """Seed the estimate_poll rows. Idempotent per row.

    Returns: list of (setting, status) tuples — status is "created" or "exists".
    """
    sheet = _get_json(f"/sheets/{sheet_ids.SHEET_CONFIG}?include=columns")
    columns = sheet["columns"]
    rows = sheet["rows"]
    col_id_by_title = {c["title"]: c["id"] for c in columns}

    results: list[tuple[str, str]] = []
    for row_spec in CONFIG_ROWS:
        existing = _find_config_row(
            rows, columns, row_spec["Setting"], row_spec["Workstream"]
        )
        if existing is not None:
            print(
                f"[skip] ITS_Config row Setting={row_spec['Setting']!r} "
                f"Workstream={row_spec['Workstream']!r} already present."
            )
            results.append((row_spec["Setting"], "exists"))
            continue

        cells = []
        for title, value in row_spec.items():
            if title in col_id_by_title:
                cells.append({"columnId": col_id_by_title[title], "value": value})
        payload = [{"toBottom": True, "cells": cells}]
        result = _post_json(f"/sheets/{sheet_ids.SHEET_CONFIG}/rows", payload)
        new_id = result["result"][0]["id"]
        print(
            f"[ok] Seeded ITS_Config row id={new_id}: "
            f"Setting={row_spec['Setting']!r} Value={row_spec['Value']!r}"
        )
        results.append((row_spec["Setting"], "created"))
    return results


def main() -> int:
    print(f"[info] ITS_Config sheet = {sheet_ids.SHEET_CONFIG}")
    print(f"[info] Workstream = {WORKSTREAM!r}")
    print(f"[info] Seeding {len(CONFIG_ROWS)} rows (estimate_poll: gate false + "
          f"interval + preview cap; estimate_extract: three tier gates false + "
          f"model/base-url/threshold/timeout pins; ClamAV reuses the existing "
          f"po_attach_screen row)")
    print()

    row_results = seed_config_rows()

    print()
    print("Summary:")
    for setting, status in row_results:
        print(f"  {setting}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
