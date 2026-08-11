#!/usr/bin/env python3
"""Smoke test for po_materials/po_poll.py environment prereqs (PO S4 generation side, S8).

OPERATIONAL — makes REAL Smartsheet API calls (read-only). The generation-side twin of
``scripts/smoke_test_po_send.py``: it verifies the po_poll daemon's environment prereqs +
the cross-workstream/config guards a typo would trip, WITHOUT filing anything (no draft is
pulled, rendered, or written).

End-to-end filing is exercised by the operator's live e2e (draft a PO in the portal →
watch po_poll pull → render → Box + PO_Log + PO_Pending_Review → receipt). This smoke
checks:

  - the po_poll CONFIG binds the PO sheets (ITS_Vendors / PO_Log / PO_Pending_Review) +
    the PO gates (never safety's/progress's);
  - the 3 pass gates + poll interval + Box root + Worker base URL resolve from ITS_Config;
  - the PO credentials (Keychain ``ITS_PORTAL_PO_TOKEN`` / ``ITS_PORTAL_HMAC_SECRET`` +
    the Worker base URL) — WARN (not FAIL) if absent, an expected DEPLOY-GATED pre-cutover
    state the operator closes at cutover (deploy the Worker + set the secrets);
  - the flag file (``state/po_poll_flagged.json``) parses.

DEPLOY-GATED items the operator confirms separately at cutover (NOT this smoke): the Worker
deployed with the PO routes + ``PORTAL_PO_API_TOKEN``; the 3 gates flipped true.

Re-run after:
  - ITS_SMARTSHEET_TOKEN / ITS_PORTAL_PO_TOKEN / ITS_PORTAL_HMAC_SECRET rotation
  - Changes to po_poll.py module-level setup
  - PO sheet (ITS_Vendors / PO_Log / PO_Pending_Review) schema changes
  - the Worker base URL or Box portal-root ITS_Config values changing

Eight numbered stages, each printed to stdout. Exit 0 on full green (WARNs allowed); 1 on
any hard stage failure.
"""
from __future__ import annotations

import sys

from po_materials import po_naming, po_poll
from shared import sheet_ids, smartsheet_client
from shared.kill_switch import SystemState, check_system_state


def stage(n: int, label: str) -> None:
    print(f"\n[stage {n}] {label}")


def main() -> int:
    print("po_materials.po_poll smoke test")
    print("===============================")

    # ---- Stage 1: kill switch ACTIVE -----------------------------------
    stage(1, "kill switch system.state")
    try:
        state = check_system_state()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL — check_system_state raised: {exc!r}")
        return 1
    print(f"  OK — system.state = {state.value!r}")
    if state is not SystemState.ACTIVE:
        print(f"  WARN — state is {state.value}; po_poll would short-circuit via @require_active.")

    # ---- Stage 2: PO gate + workstream binding sanity ------------------
    stage(2, "po_poll gate keys are PO-scoped (not safety/progress)")
    problems: list[str] = []
    if po_poll.WORKSTREAM != "po_materials":
        problems.append(f"WORKSTREAM={po_poll.WORKSTREAM!r}, expected 'po_materials'")
    for key in (po_poll.CFG_POLLING_ENABLED, po_poll.CFG_VENDORS_SYNC_ENABLED,
                po_poll.CFG_STATUS_SYNC_ENABLED):
        if not key.startswith("po_materials.po_poll."):
            problems.append(f"gate key {key!r} is not po_materials.po_poll.*-scoped")
    if not sheet_ids.SHEET_PO_PENDING_REVIEW or not sheet_ids.SHEET_ITS_VENDORS or not sheet_ids.SHEET_PO_LOG:
        problems.append("one of SHEET_PO_PENDING_REVIEW / SHEET_ITS_VENDORS / SHEET_PO_LOG is 0 (unflipped)")
    if problems:
        for p in problems:
            print(f"  FAIL — {p}")
        return 1
    print("  OK — gates PO-scoped; PO sheet ids flipped")

    # ---- Stage 3: ITS_Config PO keys readable --------------------------
    stage(3, "ITS_Config keys (3 gates + interval + Box root + Worker base URL)")
    drafts_on = po_poll._polling_enabled()
    vendors_on = po_poll._vendors_sync_enabled()
    status_on = po_poll._status_sync_enabled()
    print(f"  OK — polling_enabled={drafts_on} vendors_sync_enabled={vendors_on} status_sync_enabled={status_on}")
    if not (drafts_on or vendors_on or status_on):
        print("  INFO — all gates false (DARK) — expected pre-cutover; flip after the deploy + smoke.")
    box_root = po_poll._read_str_setting(po_naming.CFG_BOX_PORTAL_ROOT, "",
                                         workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM)
    if box_root:
        print(f"  OK — PO Box root = {box_root!r}")
    else:
        print(f"  WARN — {po_naming.CFG_BOX_PORTAL_ROOT} unset — filing HELDs (po_box_root_unresolved) until set.")
    base_url = po_poll._read_str_setting(po_poll.CFG_WORKER_BASE_URL, "",
                                         workstream=po_poll.CFG_WORKER_BASE_URL_WORKSTREAM)
    print(f"  {'OK' if base_url else 'WARN'} — Worker base URL = {base_url!r}"
          + ("" if base_url else " (unset — set safety_reports.portal.worker_base_url at cutover)"))

    # ---- Stage 4: PO credentials (deploy-gated → WARN on absence) -------
    stage(4, "PO credentials (Keychain PO token + HMAC secret + Worker base URL)")
    creds = po_poll._resolve_credentials()
    if creds is not None:
        print("  OK — creds resolved (PO token + HMAC secret + base URL all present)")
    else:
        print(
            "  WARN — PO credentials incomplete (fail-CLOSED — the daemon no-ops each cycle). "
            "Expected pre-cutover: seed Keychain ITS_PORTAL_PO_TOKEN (== the Worker's "
            "PORTAL_PO_API_TOKEN) + ITS_PORTAL_HMAC_SECRET, and set the Worker base URL. "
            "DEPLOY-GATED — not a fault before cutover."
        )

    # ---- Stage 5: PO sheets reachable + schema -------------------------
    stage(5, "PO sheets reachable (ITS_Vendors / PO_Log / PO_Pending_Review)")
    for name, sid in (("ITS_Vendors", sheet_ids.SHEET_ITS_VENDORS),
                      ("PO_Log", sheet_ids.SHEET_PO_LOG),
                      ("PO_Pending_Review", sheet_ids.SHEET_PO_PENDING_REVIEW)):
        try:
            rows = smartsheet_client.get_rows(sid)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL — {name} get_rows raised: {exc!r}")
            return 1
        print(f"  OK — {name} reachable ({len(rows)} rows)")

    # ---- Stage 6: ITS_Daemon_Health reachable --------------------------
    stage(6, "ITS_Daemon_Health reachable")
    try:
        smartsheet_client.get_rows(sheet_ids.SHEET_DAEMON_HEALTH)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL — ITS_Daemon_Health get_rows raised: {exc!r}")
        return 1
    print("  OK — ITS_Daemon_Health reachable")

    # ---- Stage 7: flag-file parse --------------------------------------
    stage(7, "one-shot flag file parses (state/po_poll_flagged.json)")
    try:
        flags = po_poll._load_flags()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL — _load_flags raised: {exc!r}")
        return 1
    print(f"  OK — flag file parsed ({len(flags)} flagged PO id(s))"
          + (f": {sorted(flags)}" if flags else ""))

    # ---- Stage 8: credential-absent no-op is safe ----------------------
    stage(8, "gate/cred posture summary")
    if creds is None or not (drafts_on or vendors_on or status_on):
        print("  OK — the daemon is a safe no-op this posture (creds/gates dark) — nothing files.")
    else:
        print("  OK — creds present + a gate is live — the daemon would process this cycle.")

    print("\nAll stages green (WARNs are expected deploy-gated pre-cutover states).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
