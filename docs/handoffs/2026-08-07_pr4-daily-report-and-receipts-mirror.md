---
type: reference
date: 2026-08-07
status: active
related_prs: [17]
workstream: field_ops
tags: [handoff, materials, daily_report, section51, mirror, form_version, photo_pool, next-session]
---

# Handoff — completing PR4 (daily-report receipt integration + the §51 receipts mirror)

**Every claim below was verified against live HEAD `2acda74` on 2026-08-07** by a four-way
parallel check, and **two claims from the previous brief were found WRONG** — they are corrected
here and flagged 🔴. Treat anything *not* marked verified as a hypothesis and `grep` it, per
HOUSE_REFLEXES §1. The last brief's central claim ("#727 is landed") was also false; that is the
pattern this header exists to interrupt.

Work happens on **`its-sys-admin/evergreen-its`** only. `origin` must resolve there —
`tests/test_watchdog.test_check_s_repo_matches_origin_remote` pins Check S's slug to it.

## What already landed (do NOT rebuild)

`evergreen-its#17` (`2acda74`, four-part verified) shipped the mirror's first half:

- `progress_reports/material_receipts.py` — the §51 mirror, append-only posture (no
  `retire_removed`, no `On List`, no reconcile-zeroed branch → the #468 zero-drop class is
  structurally impossible).
- `GET /api/internal/fieldops/material-receipts` in `worker/index.ts` — reads the TABLE, LEFT-JOINs
  `material_shipments` for the BOL, bounds to ACTIVE jobs, display-name-only actor.
- `safety_portal/test/fieldops-material-receipts.test.ts` — 9 tests.
- **Archive enrollment is DONE** — `fieldops_sync.py:868-874` already carries
  `material_receipts.material_receipts_sheet_name(...)` as the fifth tracker, and
  `test_preserved_helper_moves_every_tracker` pins the count at 5. **Do not redo it.**

### 🔴 Known gap in what shipped

**`tests/test_material_receipts.py` does not exist.** The 412-line module has **no Python tests** —
only the Worker route is covered. Clone `tests/test_material_incidents.py` 1:1 (swap
`ensure_material_receipts_sheet` / `upsert_receipt_row` / `find_receipt_row`, key `Event UUID`) and
add cases for the two DERIVED columns. This is the first thing to fix.

---

## Part A — finish the mirror (`fieldops_sync` pass)

Template: `_mirror_material_incidents_pass` in `field_ops/fieldops_sync.py`. Clone its shape.

**The gate does not exist yet.** Add it as **five coordinated surfaces**, each with a live
`incidents_enabled` precedent:

1. `CFG_RECEIPTS_ENABLED = "field_ops.fieldops_sync.receipts_enabled"` + `DEFAULT_RECEIPTS_ENABLED
   = False` + `_receipts_enabled()` in `fieldops_sync`.
2. **TWO** `REQUIRED_CONFIG` entries — the gate **and**
   `ConfigKey(material_receipts.CFG_ROW_CAP_WARN, "progress_reports",
   material_receipts.DEFAULT_ROW_CAP_WARN, "int")`.
3. A row in `scripts/migrations/seed_daemon_gate_config.py`, `Value: "false"` (a dark gate with no
   ROW is a switch the operator cannot find).
4. `scripts/verify_cutover.py` VC-03 (`non_empty`, never forced true).
5. The config dictionary regen + its enablement-manifest sha re-record.

**`shared/portal_client.py` needs two additions** (the receipts lane is absent; `grep -i receipt`
returns only unrelated mark-filed prose):

- `FIELDOPS_MATERIAL_RECEIPTS_PATH = "/api/internal/fieldops/material-receipts"` after line 82.
- `get_fieldops_material_receipts(base_url, token)` — byte-for-byte the incidents shape, envelope
  key **`receipts`** (the Worker returns `c.json({ receipts: … })`).

**Three traps in this part:**

- **`check_row_cap` currently has NO caller** (`progress_reports/material_receipts.py:362`). The
  incidents template calls it once per job at the end of its reconcile. Wire it or it is dead code.
- **The grouper must be NEW, not reused.** `_group_incidents_by_job` keys on `submission_uuid`;
  receipts key on `event_uuid`. `_group_materials_by_job` returns a bare list (material-list has a
  reconcile roster) — receipts, like incidents, need `project_name`.
- **The 401 branch is deliberately non-fatal and order-dependent** — it logs CRITICAL and
  `return out` rather than raising, because earlier passes on the same bearer may have succeeded.
- **`upsert_receipt_row` is keyword-only and fixed** (`material_receipts.py:271-287`) — 15 kwargs.

**No new watchdog entry, plist, or secret is needed** — the pass rides inside the existing
`fieldops-sync` daemon (one host / lock / heartbeat, Check C) on the existing
`ITS_PORTAL_FIELDOPS_TOKEN`.

**§43 runbook entry is missing** and is definition-of-done (CLAUDE.md step 9). Neither
`job_materials.md` nor `fieldops_sync.md` documents a receipts-mirror symptom.

**Material List's deferred PR2 columns** (`part_number` / `category` / `expected_ship_date`):
`shared/smartsheet_client._resolve_cells` raises `KeyError` for a title absent from an
already-created sheet — the safe procedure was verified and is in the mirror-pass findings; add the
column to live sheets *before* writing to it.

---

## Part B — the daily-report half (untouched)

### 🔴 CORRECTION 1 — `expected_materials_receipt` is NOT a runtime `values` key

The previous brief said the mount "already reserves" the key, implying it is free to seed. **It is
not.** The key is a **publish-time namespace reservation only**
(`worker/publishValidation.ts:383-395`), so a future value-bearing section cannot collide with it.
Nothing seeds it: `FormRenderer.initialValues` (`FormRenderer.tsx:27-43`) has no
`expected_materials` branch, and `DailyReportTab.tsx:183-187` states outright that the section
"files no values of its own".

**Two tests actively pin its ABSENCE**, and a third pins the PDF behaviour:

- `src/forms/__tests__/expected-materials-section.test.tsx:76` —
  `expect("expected_materials_receipt" in initialValues(DEF)).toBe(false)`
- `src/components/__tests__/DailyReportTab.test.tsx:711` —
  `expect("expected_materials_receipt" in payload.values).toBe(false)`
- `safety_reports/form_pdf.py:786-799` renders the section as a **note line only**, with the comment
  *"Reprinting the live D1 list here would snapshot mutable state the submission never carried."*

**Option A is the ratified decision to invert exactly this contract** — so those three assertions
must be **deliberately rewritten**, not worked around, and the rewrite is the reviewable heart of
the PR. Do not seed the key while leaving the tests asserting its absence.

### 🔴 CORRECTION 2 — the cited test pins something else

`tests/test_form_pdf.py:774-800` is **`test_expected_materials_renders_note_line_only_in_submission_mode`**,
which pins that a stray value under `expected_materials_receipt` must NOT reach the PDF. The
**empty-array fallback** the brief meant is pinned by
**`test_job_requirements_absent_or_empty_is_skipped` at `tests/test_form_pdf.py:722-732`.**

### The seeding mechanism (verified)

Seed through raw **`setValues`**, never `editValues`. `editValues`
(`DailyReportTab.tsx:209-213`) sets `dirtyRef.current = true`, which gates exactly two things:
draft persistence to `sessionStorage` (`:286`, key `its-daily-draft:<job>:<date>`) and prefill
suppression / draft-wins (`:393`). Seeding through it would persist a draft the manager never typed
and resurrect it later.

**The reference pattern is the D4 requirements seed** — `DailyReportTab.tsx:420` and `:438`:

```ts
setValues((v) => (v[reqKey] === undefined ? { ...v, [reqKey]: seed } : v));
```

Merge-if-absent, raw `setValues`, `lastPrefill.current` updated alongside.

### `confirmReceipt` → `markDelivery` (verified)

`DailyReportTab.tsx:655-693` currently appends one `deliveries_received` row via `editValues` and is
pinned by `DailyReportTab.test.tsx:708-710`. Generalise to the three-way mark
(`delivered` / `partial` / `not_delivered`) with qty + note.

### The v7 cut

- **No v7 exists** in any form — no `forms/daily-report-v7.json`, no catalog entry, no code
  reference. The ratified shape holds: sections **byte-identical to v6**, only `form_code` /
  `version` / `comment` change, plus `catalog.json` and the `expected_materials` branch description
  in `meta-schema.json`. **`required-content.json` is NOT touched** — verified correct.
- 🔴 **BLOCKER:** `tests/test_form_definitions.py:539` hard-pins v6 as catalog current —
  `assert "daily-report-v6" in CURRENT_FORM_CODES`. It RED-lights the instant catalog.json advances.
- **No manual registry churn needed**: the SPA registry is data-driven from `catalog.json` via
  `vite-plugin-eager-forms.ts`, and both render-smoke nets (`tests/test_render_smoke.py`, the SPA
  `render-smoke` test) drive off the catalog ACTIVE set, so v7 is picked up automatically.
- At most **one** `expected_materials` mount per definition (`publishValidation.ts:464-467`).

### Photo binding (migration `0061`)

- `worker/fieldops_daily_photos.ts:113` enforces an exact `{pool_id, caption?}` shape — **verified**.
  Do not add `line_uuid` to the submission ref.
- 🔴 **CONSTRAINT the previous brief missed:** the pool's HMAC canonical is
  `["daily_photo:v1", jobId, workDate, photoJson].join("\n")` (`:87-89`), recomputed Mac-side. **Any
  new upload-time field is UNSIGNED unless it goes inside `photo_json`.** Design `0061` around that.
- The pool INSERT (`:343-362`) is a single W4 `db.batch` with both caps folded into the
  `INSERT…SELECT` WHERE (`POOL_CAP_PER_DAY=40`, `POOL_PENDING_GLOBAL_MAX=200`) — a new column must be
  threaded into that SQL, not appended after.
- ⚠ **Naming trap:** two different identifiers are live. `material_receipt_events.line_id` /
  `material_shipments.line_id` are **INTEGER** soft-refs to `job_expected_materials.id`;
  `line_uuid` is **TEXT**. Pick deliberately and validate against the right one.
- Next free migration number: verify with `ls safety_portal/migrations/ | tail -3` (0060 is taken).

### Drive-by (two lines)

`additional_photos` is missing a `case` in `src/forms/editorValidation.ts` and
`src/components/FormEditor.tsx`. `tsc` structurally cannot catch a recurrence (`validateSection`
returns void, no `noImplicitReturns`), so pair the fix with teeth: add an `additional_photos` section
to the `FormEditor.readonly.test.tsx` fixture.

---

## Suggested slicing

1. `tests/test_material_receipts.py` (closes the gap in what shipped).
2. The `fieldops_sync` receipts pass + its five gate surfaces + §43 runbook.
3. The daily-report snapshot + the three deliberate test rewrites.
4. `daily-report-v7` + the `test_form_definitions.py:539` update.
5. Migration `0061` + the photo binding.
6. The two-line editor-mirror drive-by, with its fixture.

## Also still open, from PR3b

`ManifestValidatePage.tsx` — the three-pane validate screen. Without it the manifest lane is
complete server-side but unusable by the office: an upload is pulled, screened, parsed, filed to Box
and turned into a grid that has no screen to review it on. Arguably higher value than PR4.
