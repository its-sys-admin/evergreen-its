"""One-shot migration: seed the `field_ops.manifest_poll.*` ITS_Config rows
(PR3b / ADR-0005 — the materials-manifest importer daemon).

Companion to `field_ops/manifest_poll.py`. Run once at PR landing; safe to re-run
(per-row idempotency-guarded on Setting+Workstream — the seed_estimates_config.py
pattern).

Why the gate row exists even though the value ships FALSE — the dark-ship gate
reflex (HOUSE_REFLEXES §5): a boolean gate read via `_read_bool_setting(default=
False)` treats a MISSING row identically to `false`, so a capability that "ships
dark" without a seeded row has NO visible switch at all and the operator hunts for
one that does not exist. Seeding the row `false` in the same change that adds the
gated code makes activation a visible cell-flip, and the #336 `resolve_and_log`
startup pass stops WARNing `config_row_missing`.

What it seeds (2 rows, workstream `field_ops`):

    field_ops.manifest_poll.polling_enabled       = false  (the ONE daemon gate — dark)
    field_ops.manifest_poll.poll_interval_seconds = 120    (install-time cadence)

Three keys the daemon reads deliberately get NO new row here, because it REUSES
gates that other lanes own: the Worker base URL and the Box mirror-tree root
(`safety_reports`), and the §34 screener's ClamAV layer
(`po_materials.po_attach_screen.clamav_enabled`). One scanner posture spans every
document pool; a second row would let them silently disagree.

Auth: ITS_SMARTSHEET_TOKEN from macOS Keychain (same path the runtime SDK uses).

Run from `~/its` with the venv activated:

    python3 scripts/migrations/seed_manifest_config.py

Exit code 0 on success or no-op; nonzero on any error.
"""
from __future__ import annotations

import sys
from typing import Any

import requests  # type: ignore[import-untyped]

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from shared import keychain, sheet_ids  # noqa: E402

BASE = "https://api.smartsheet.com/2.0"

WORKSTREAM = "field_ops"

CONFIG_ROWS: list[dict[str, Any]] = [
    {
        "Setting": "field_ops.manifest_poll.polling_enabled",
        "Workstream": WORKSTREAM,
        "Value": "false",
        "Description": (
            "Gate for the materials-manifest importer daemon (manifest_poll, PR3b / "
            "ADR-0005): pull office-uploaded BOMs and shipping logs from the Worker "
            "pool, manifest:v1 HMAC- and digest-verify, §34 doc-screen, extract the "
            "cell grid inside the killable sandbox child, parse it, file the ORIGINAL "
            "document to Box <job>/Materials/Manifests/, and post a reviewable grid "
            "for the validate screen. It NEVER commits a material line by itself and "
            "never picks a quantity column — the office disposes. Turning this ON is "
            "a capability activation (§44): confirm with Seth. Preconditions live in "
            "docs/runbooks/material_manifest_import.md (Go-live), not in this cell — "
            "read that runbook rather than trusting any doc's claim about this "
            "value; ITS_Config is the single source of truth for what it is set to. "
            "This daemon NEVER sends anything customer-facing (generation half of "
            "the External Send Gate)."
        ),
    },
    {
        "Setting": "field_ops.manifest_poll.poll_interval_seconds",
        "Workstream": WORKSTREAM,
        "Value": "120",
        "Description": (
            "Integer seconds between manifest_poll cycles. Read at INSTALL time by "
            "scripts/launchd/install.sh to substitute into the plist's StartInterval "
            "(the value is BAKED into the installed plist — changes take effect at "
            "the next `install.sh load org.solutionsmith.its.manifest-poll`, not "
            "hot). Default 120s, matching estimate-poll: manifests are an office "
            "upload trickle, and the work per document (screen, sandboxed parse, "
            "Box file) is the same shape. If you raise this materially, widen the "
            "manifest_poll marker window in scripts/watchdog.py too."
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
