---
type: operations
date: 2026-07-10
status: active
related_prs: []
workstream: null
tags: [cutover, aug7_delivery, repoint, its_config, external_send_gate, staged]
---

# Production repoint change-set (CL-12 ITS_Config sweep) — STAGED, NOT APPLIED

## Purpose

The exact, reviewed mapping of every mirror (`evergreenmirror.com` / mirror folder-id) value → its
production value, for the Aug-3 tenant cutover. **This artifact is not applied — applying it IS the
Aug-3 cutover (operator + Seth).** It is the CL-12 sweep's worksheet, gated afterward by
`python -m scripts.verify_cutover --only config` with **NO** `--allow-sandbox` (VC-03 must PASS).

> **Mechanized (2026-07-23):** §A–D are applied by
> `scripts/migrations/production_repoint.py` from its reviewed data file
> `production_repoint_map.json` (PLAN default; typed-phrase `--commit`; DRIFTED
> rows refuse the sweep; §E structurally excluded). The map carries two §B rows
> built after this doc was originally authored — `subcontracts.subcontract_send.from_mailbox`
> and `po_materials.rfq_send.from_mailbox` (both VC-03 sandbox-scan-enrolled, now
> reflected in the §B table below) — and EXCLUDES the LEGACY
> `safety_reports.intake.mailbox` row. The §Approver-model
> share swap is applied by `seed_production_shares.py` (+ its manifest) and
> verified by `verify_cutover --only approver-shares` (VC-10), replacing the
> hand `python -c` diff below.

Every row/read-site below was ground-truthed against live HEAD `a638fc5`. The code holds only
mirror-domain **fallback defaults**; the live ITS_Config rows override at runtime — so the sweep is a
Smartsheet + Box edit, **not a code change**.

## Procedure — the ITS_Config production sweep (CL-12)

Production domain throughout: **evergreenrenewables.com** (re-**NEW**-ables). **Phase-1
single-mailbox model (operator decision 2026-07-23): ALL send lanes send from
`its@evergreenrenewables.com`; per-lane shared mailboxes are the later `safety@` / `progress@` /
`procurement@` step.** Box + operator identity: `its@evergreenrenewables.com`.

### A. worker_base_url — ONE Setting name, THREE physical rows (one per Workstream cell)

`safety_reports.portal.worker_base_url` is read under three Workstream cells. **All three** must move
to the production custom domain.

| # | Setting | Workstream cell | from (mirror) | to (production) | Read site | In VC-03? |
|---|---|---|---|---|---|---|
| 1 | `safety_reports.portal.worker_base_url` | `safety_reports` | `https://safety.evergreenmirror.com` | `https://safety.evergreenrenewables.com` | portal_poll / publish_daemon / portal_admin / fieldops_sync / po_poll | ✅ (always) |
| 2 | `safety_reports.portal.worker_base_url` | `progress_reports` | `https://safety.evergreenmirror.com` | `https://safety.evergreenrenewables.com` | progress_weekly_generate.py:105 | ✅ **(newly enrolled this PR)** |
| 3 | `safety_reports.portal.worker_base_url` | `po_materials` | `https://safety.evergreenmirror.com` | `https://safety.evergreenrenewables.com` | config_actuator.py:177 | ✅ **(newly enrolled this PR)** |

> The production portal domain (`safety.evergreenrenewables.com` here) is the CL-09 DNS decision —
> confirm the exact hostname with the operator; use it verbatim in all three rows.

### B. Send FROM addresses (mailbox/identity rows)

> **Phase-1 single-mailbox model (operator decision 2026-07-23, AMENDED 2026-07-24):** four of the
> five send lanes send from `its@evergreenrenewables.com`; the fifth (`weekly_send`, safety) sends
> from `safety@evergreenrenewables.com`, an **SMTP alias on the `its@` mailbox** — one mailbox, two
> addresses, so the single-mailbox model holds. Per-lane shared **mailboxes** (`progress@` /
> `procurement@`) are the later step. The two rows built after this doc was originally authored
> (`subcontract_send` / `rfq_send`) are included below to match `production_repoint_map.json`.

| Setting | Workstream | from (mirror) | to (production) | In VC-03? |
|---|---|---|---|---|
| `safety_reports.weekly_send.from_mailbox` | `safety_reports` | `safety@evergreenmirror.com` | `safety@evergreenrenewables.com` (alias on the `its@` mailbox — 2026-07-24 amendment) | ✅ (scanned) |
| `progress_reports.progress_send.from_mailbox` | `progress_reports` | `progress@evergreenmirror.com` | `its@evergreenrenewables.com` | ✅ (scanned) |
| `po_materials.po_send.from_mailbox` | `po_materials` | `procurement@evergreenmirror.com` | `its@evergreenrenewables.com` | ✅ **(newly enrolled this PR)** |
| `subcontracts.subcontract_send.from_mailbox` | `subcontracts` | `procurement@evergreenmirror.com` | `its@evergreenrenewables.com` | ✅ (scanned — added post-authorship, SC-S4 2026-07-15) |
| `po_materials.rfq_send.from_mailbox` | `po_materials` | `procurement@evergreenmirror.com` | `its@evergreenrenewables.com` | ✅ (scanned — added post-authorship, ADR-0004 R3) |
| `safety_reports.intake.mailbox` | `safety_reports` | `safety@evergreenmirror.com` | `safety@evergreenrenewables.com` (only if email intake resurrected) | ⬜ (email-intake path LEGACY/dormant — portal PULL superseded it) |

### C. System-global rows

| Setting | Workstream | from | to | In VC-03? |
|---|---|---|---|---|
| `system.operator_email` | `global` | `seths@evergreenmirror.com` | `its@evergreenrenewables.com` (operator alert recipient) | ⬜ (candidate — see below) |
| `system.heartbeat_url` | `global` | seed placeholder (`PLACEHOLDER_uptimerobot_heartbeat_url`) | real Healthchecks.io ping URL | ✅ (VC-09, shape only) |

### D. Box root folder IDs (numeric — repoint to production folder IDs, CL-17)

| Setting | Workstream | Action |
|---|---|---|
| `safety_reports.box.portal_root_folder_id` | `safety_reports` | → production Box folder ID |
| `progress_reports.box.portal_root_folder_id` | `progress_reports` | → production Box folder ID |
| `po_materials.box.portal_root_folder_id` | `po_materials` | → production Box folder ID *(added post-authorship — the 2026-08-11 PO-lane root split)* |
| `subcontracts.box.portal_root_folder_id` | `subcontracts` | → production Box folder ID *(added post-authorship — the 2026-08-12 subcontract-lane root split)* |
| `field_ops.box.archive_root_folder_id` | `field_ops` | → production Box folder ID *(added post-authorship — the Track 6 archive root)* |

### E. Send-scope gates DEFERRED (do NOT flip blindly — CL-13 + External Send Gate)

- `progress_reports.progress_send.{scheduled_send_local, polling_enabled}` — **currently MISSING** in
  ITS_Config (VC-03 fails on them). Seeding `polling_enabled=true` is a **send-enable** (high-class).
- `po_materials.po_send.{polling_enabled, scheduled_send_local}` — seeded `polling_enabled=false` (dark).
  Enabling PO send at cutover is a send-scope decision (Seth). This is why they were **not** enrolled in
  VC-03 as `"true"` (that would force the gate to demand a send-enable). Enroll + seed only once each is
  confirmed in the Aug-7 send scope, and read each row's Description for an in-cell precondition first.

### NOT config repoints (do NOT edit these as config rows)
- `safety_reports.authorized_approvers` — **LEGACY / seed removed 2026-06-06.** Live F22 authority is
  workspace-share membership (see Approver model below), not this config list. Class-E display-only.
- `safety_reports.recipients.*` — no live read; recipients resolve from **ITS_Active_Jobs at send time**
  (that's the CL-29 ITS_Active_Jobs edit, not a config sweep).
- `safety_reports.reviewer_chain` — carries mirror residue ONLY if its JSON names `evergreenmirror`
  emails; check the value, edit only if so.

## Approver model — authoritative source on the production send path (reconcile CL-11)

**The `authorized_approvers` ITS_Config row is the LEGACY path and is NOT authoritative.** Live F22
approval authority = each send-bearing **workspace's individual USER share list**, resolved by
`smartsheet_client.list_workspace_share_emails` (fail-CLOSED, no try/except). The seed for
`authorized_approvers` was removed 2026-06-06; it is Class-E display-only. **Do not silently keep both
models** — the workspace-share membership is the one that gates sends.

CL-11 action (operator, Smartsheet share on all THREE send-bearing workspaces — Safety Portal,
Progress, Purchase Orders):
- **UNSHARE** the mirror validation accounts: `daniels@` / `seths@` / `benf@` **@evergreenmirror.com**.
- **SHARE** the 7 production approvers as **individual USER shares** (GROUP shares do NOT count — a
  group-only share yields an EMPTY authorized set that **silently fail-closes every send**):

| Email (@evergreenrenewables.com) | Name | Role |
|---|---|---|
| jacobs@ | Jacob Stephens | CEO |
| ezraj@ | Ezra Jones | CFO |
| jechiahs@ | Jechiah Stephens | Head of Engineering |
| benf@ | Ben Finkhousen | Senior PM |
| tiffanym@ | Tiffany Montastirsky | Head of Permitting |
| tealap@ | Teala Paradise | Procurement & Subcontracting |
| samr@ | Sam Rigney | Head of Field Operations |

> ⚠ **EZRA-TYPO CAUTION.** The domain is `evergreenrenewables.com` (re-**NEW**-ables); the original
> contact sheet carried a `renwables` typo for Ezra. A non-matching account email **fail-closes that
> approver SILENTLY**. Each approver must be a real Smartsheet USER whose account email matches
> exactly (cell-history `modifiedBy` exposes the email — verify per approver).

## Validation / how to verify the sweep was applied

After the sweep, on the production host:

```
python -m scripts.verify_cutover --only config       # NO --allow-sandbox — must PASS (VC-03)
```

Per-workspace approver-share check (CL-11), for each of the 3 workspace ids:

```
python -c "from shared import smartsheet_client as s; print(sorted(s.list_workspace_share_emails(<workspace_id>)))"
# → exactly the 7 production approver emails; ZERO evergreenmirror.com residue
```

Residue backstop (CL-14):

```
grep -rn 'evergreenmirror' --include='*.py' shared/ safety_reports/ progress_reports/ po_materials/
# → only fallback-default constants / seed / smoke scripts (as-designed); NO live runtime value
```

## Owner

Developer-Operator (Seth). This is the CL-12 cutover sweep + the CL-11 workspace-share swap —
**all four FIXED high-capability classes touch it** (External Send Gate, secrets/auth via the mailbox
identities, doctrine via §53, and the Smartsheet-share operation). Applying it IS the Aug-3 cutover.
