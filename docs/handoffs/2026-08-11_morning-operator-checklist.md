---
type: reference
date: 2026-08-11
status: active
workstream: field_ops
tags: [handoff, materials, operator-smoke, checklist, morning]
---

# Morning checklist — the five smokes only a human can run (2026-08-11)

The overnight reconcile session stood up the PR4 materials/delivery workflow on this host and
verified everything headless-verifiable: 0063 applied → Worker deployed (bundle serves v7) → the
Material List back-fill fixed (#59) and confirmed live on the Kiwi sheet → both missing config rows
seeded → `receipts_enabled` flipped → the receipts mirror's first live fire watched. What remains
is exactly the set that needs eyes, a phone, or portal credentials. Each item says what GOOD looks
like and what to do on a miss.

## 1 — Two-tap delivery marks (phone if possible)

On a job's **Materials tracking** page: tap *Delivered* once → the button should re-label to a
confirm state, and **revert on its own after ~6s** if you do nothing. Tap *Delivered* then
*Not delivered* → nothing should be recorded, and *Not delivered* is now the armed one. Then arm →
confirm a real mark and watch the pill + running total move.

- **A mis-tap that records is permanent** (append-only ledger, no delete). Correct it by recording
  a further event — never ask for a row deletion.
- Miss → note exactly which surface (Materials page vs daily report) and hand the session the
  wording; the daily report's *Confirm receipt* staying ONE-tap is **deliberate** (tracked debt,
  #58) — not a bug to report.

## 2 — v7 daily report files the snapshot table

File a daily report on a job that HAS expected materials, then open the filed PDF from Box: the
expected-materials section should be a **table snapshotting what was on screen**, not the old
note line.

- Miss (no table on a fresh v7 submission) → check the submission's `values` carries
  `expected_materials_receipt`; escalate with the submission UUID.

## 3 — an OLD v5/v6 daily report still shows its note line

Open any pre-v7 daily report PDF from Box (or re-render one). The section must still read as the
original **note line** — absence-of-the-key is deliberately distinguished from empty-snapshot so
historical PDFs never silently lose a paragraph.

- Miss → STOP and escalate: that is a `form_pdf` backward-compatibility regression.

## 4 — bound photo upload + the cross-job refusal (0063)

On the daily report, attach a delivery-confirmation photo **bound to a material line** and file.
Then confirm in the portal that the photo shows its line context. If you can, try binding a photo
to a line from a **different job** — the upload must be refused with `unknown_material_line`
(HTTP 422), never silently stored.

- An ordinary unbound day photo must still upload and screen normally.
- Miss → escalate with the job + line; the validation is at upload time by design (the HMAC signs
  the binding inside `photo_json`).

## 5 — receipts mirror: mark → sheet round trip

After any real delivery mark today, wait one ~90s cycle and open the job's
**`<Job> — Material Receipts`** sheet (Progress Reporting workspace, the job's folder): the event
should appear as a row, and marking the SAME line again should move the existing row's
**Line Qty Received / Line Status** (those two columns are derived rollups — moving is correct).

- Nothing is ever deleted from this sheet. Duplicate-looking rows after a create race: keep the
  one that keeps updating.
- Miss → `/troubleshoot` → standing trackers → *receipts_ledger_stale* (new tree node), runbook
  `fieldops_sync.md` Symptom G.

## NOT this morning

- **Do not import a real vendor manifest yet.** The lane is live but carries audit-confirmed
  defects (the Quantity mapping on the validate screen is inert; "Merge onto the matching line"
  duplicates lines; a mid-import failure can strand the manifest). See `docs/tech_debt.md`
  "Materials-manifest + expected-materials correctness cluster" — fixes are queued. If one MUST be
  imported, use a throwaway job, choose "Add as new lines", and hand-check every quantity.
- The manifest parser eval stays WAIVED — the sample corpus lives on the dev Mac, not here.
