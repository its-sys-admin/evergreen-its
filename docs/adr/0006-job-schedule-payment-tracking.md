---
type: reference
date: 2026-08-11
status: active
related_prs: []
workstream: field_ops
tags: [adr, schedule, progress, payments, gantt, ocr, import, adversarial-input, section-34, sandbox, ships-dark]
---

# ADR-0006 — Job schedule / progress tracking + payment-cycle tracking

**Status:** Accepted, building. This ADR records the design decided from a survey of the 32 real
Evergreen project-schedule exports at `~/Desktop/PJCT SCHDLS` (10 jobs — Bonacci 1/2, Coker,
Colfax, Deeplake, Indian Creek, Kiwi, Minooka, Roxbury, Steger — with up to 14 dated revisions
per job), the eight operator decisions ratified 2026-08-11, and a component reuse-map of the
live materials-manifest importer (ADR-0005). The lane **ships dark** and its go-live is a
visible `ITS_Config` cell-flip.

## Context

Evergreen runs per-job construction schedules as Smartsheet-authored Gantt sheets, exported to
PDF and collected on the operator's desktop. Progress lives only in those periodic re-exports;
nothing in ITS tracks whether a job is on schedule, which Deliveries-phase items have actually
arrived, or where each client payment cycle stands against the contract's
nonpayment-notice escalation. The feature adds a per-job **Schedule page**
(`/jobs/:jobId/schedule`) in the field-ops portal: schedule intake (upload → OCR-propose →
human validate → living task list), in-portal progress mark-off, revision reconcile, distinct
Deliveries-task handling, and an office-only payment section (terms + cycles + derived
overdue/notice states). **Alerting is a designed-for later fold-in** — behind-schedule,
delivery-slip and payment-reminder engines arrive as new code over this data model, with no
schema change.

### What the corpus showed

- **31 of 32 PDFs have NO text layer.** The table text is vector glyph outlines (~2,600–3,400
  path objects, <110 real chars — title + export footer only). Deterministic pdfplumber
  extraction is impossible on them; `estimate_sandbox.parse_native`'s scanned-page heuristic
  (mean chars < 25/page) classifies them correctly for free.
- **Apple Vision OCR works** (proven 2026-08-11 on Coker p1 at 3× Quartz render: 169 items at
  confidence 1.00, 88 date tokens) — **including inside an RLIMIT'd sandbox child** (spike
  passed same day; see decision 4). But OCR misreads digits at full confidence
  (`12/01/25` → `72/01/25` observed), so confidence is NOT a filter and the human validate
  screen is the only fidelity control (the ADR-0004 red-team #2 posture, inherited verbatim).
- **Two export styles.** Grid-view exports carry Row # / Task / Duration / Start / Finish /
  % Done / Predecessors / phase tag / Contract-Milestone. The newer Gantt-view exports carry
  only Task / Start / Finish as text, with % labels riding the Gantt bars and completed tasks
  struck through — and the page content is rotated 90°. Predecessors are therefore only
  sometimes recoverable → stored verbatim when present, never parsed or enforced.
- **Revisions are the norm.** Bonacci alone has 14 dated exports. Revision handling is a
  first-class flow, not an edge case.

## Operator decisions (2026-08-11, all final)

1. **Intake = PDF upload + OCR + validate screen.** Not XLSX, not direct Smartsheet read.
2. **Progress = portal mark-off + revision reconcile.** Portal % is preserved unless the human
   explicitly takes the revision's value.
3. **Payments now = terms + cycles + derived states display.** The reminder engine is a later
   fold-in. Notices (Nonpayment / Intent-to-Suspend) are NEVER auto-sent — any future notice
   document rides a `*_Pending_Review` sheet + the External Send Gate (Invariant 1, permanent).
4. **Visibility: schedule all roles; payment section admin-only.**
5. **"Delivery" = both** payment receipts AND distinct Deliveries-phase task handling
   (`is_delivery` flag, delivered mark, later delivery-slip alerts).
6. **Payment terms attach per JOB** (the contract is per job), prefilled from the same
   client's most recent job's terms (`jobs.client_id` → `clients`, migration 0014).
7. **Fresh start** — no corpus backfill; the corpus PDFs become test fixtures only.
8. **Mark-off = quick-% chips (0/25/50/75/100 + exact) + a done-checkbox for milestones.**

## Decisions

1. **The lane is the fourth §34 Option-D pool** (po_attachments 0053 → po_estimates 0054 →
   job_manifests 0060 → job_schedules 0066): send-free Worker upload with bounds-gating +
   `schedule:v1` content-covered HMAC, chunked bytes, claim-first Mac daemon
   (`field_ops/schedule_poll.py`, 120s, gate `field_ops.schedule_poll.polling_enabled`),
   §34 screening via the shared `po_attach_screen`, sandboxed hostile-byte handling, paged
   grid post-back, result post LAST. The Worker never parses; bytes only flow Mac-ward.
2. **The parser PROPOSES, the human DISPOSES.** OCR + geometry reconstruction emit a
   reviewable grid + a proposed concept→column map; nothing reaches the living task list
   except through the validate screen's commit. A wrong-but-self-consistent OCR read passes
   every automated check — the side-by-side page preview is the single fidelity control.
3. **NO cloud AI anywhere in the lane.** Extraction is Quartz render + Apple Vision (local)
   + deterministic geometry/vocabulary parsing. The "sole live Anthropic consumer is
   `safety_reports/intake.py`" invariant holds; no Ollama tier either — the corpus showed a
   deterministic ladder + human validate suffices, and Tier-3 is manual entry on the validate
   screen.
4. **Vision runs INSIDE the sandbox child** (spike-proven 2026-08-11: ocrmac under
   RLIMIT_CPU + RLIMIT_AS returned identical results). This lane therefore has no ADR-0004
   §Vision deviation: render AND OCR of hostile bytes are both subprocess-isolated. New
   sandbox entry points get REAL dispatch branches — the `_child_main` else-branch falls
   through to `_child_test_alloc`, so an `_ALLOWED_FNS` entry without its branch is a live
   trap, and a dispatch-parity test locks every name to its function.
5. **A dedicated bearer tier** (`PORTAL_SCHEDULE_API_TOKEN` / Keychain
   `ITS_PORTAL_SCHEDULE_TOKEN`) — the ADR-0004 decision-4 posture: the daemon decodes
   hostile bytes, so its token scopes ONLY the schedule pool. Cross-lane 401s are
   RED-proofed in the worker suite.
6. **`superseded` joins the pool status machine** (the lane's one status divergence from
   0060). Revisions are expected; exactly one upload governs a job at a time, enforced by a
   partial UNIQUE (`WHERE status='committed'`), and the final commit page flips the old
   governing row committed → superseded FIRST in the same batch. A superseded row leaves the
   per-job exact-sha dedupe index, so re-uploading the displaced revision's exact bytes IS
   the rollback path for a wrong-file commit. Grid rows + previews are retained after commit
   as the next revision's reconcile evidence; chunks still die at result.
7. **Office schedule surfaces ride `cap.jobtracker.manage`** (the 0060 ride-an-existing-cap
   precedent; admin-only in practice — manager is withheld it, 0023). Task-list read rides
   `cap.jobtracker.read`. Field mark-off gets a NEW `cap.schedule.mark`
   (submitter+manager+admin, per-job scoped) in the mark-off slice; payments get a NEW
   `cap.payments.manage` granted to **admin only**, and payment data appears in no other
   route's response.
8. **The living task list** (`job_schedule_tasks`, PR-4 slice) carries per task: stable
   `task_uuid`, section (phase), name + Worker-computed `match_key` (NOT unique — duplicate
   names become blocking-ambiguous reconcile outcomes), duration/start/finish,
   **baseline_start/finish stamped at each task's OWN first commit and never rewritten**
   (slip measurement), `percent_done` + `schedule_percent` (what the last committed schedule
   asserted — the three-way % diff base), milestone/contract-milestone/delivery flags,
   delivered mark, verbatim predecessors, `last_marked_by/at` (**stamped ONLY by portal
   marks** — `last_marked_by IS NOT NULL` ⇔ a human marked it, the %-conflict predicate),
   soft-delete. No dedicated progress-events table: `audit_log` rows (from/to in detail) are
   the history.
9. **Reconcile is a three-way diff, human-resolved** (PR-6 slice): matched / ambiguous
   (BLOCKING) / new / removed (BLOCKING when the task has portal progress, a delivered mark,
   or is a contract milestone). No server-side fuzzy matching — a rename surfaces as
   new+removed and the human LINKS them (preserving task_uuid, baselines, portal %).
   Percent: `rev == schedule_percent → keep portal; last_marked_by NULL → take rev; else
   CONFLICT (default keep portal)`. Commit re-derives the plan server-side, is paged with
   the 0060 watermark protocol, and serializes concurrent commits per job.
10. **Payments are Worker+SPA only** (no daemon, no OCR): `job_payment_terms` (one per job;
    cadence + net_days + nonpayment_notice_days + intent_to_suspend_days),
    `job_payment_cycles` (manual rows — NO auto-cadence generation; a stored Worker-computed
    `due_date` snapshot), `job_payment_receipts` (append-only events → partial payments and
    retainage need no schema change). **No stored payment state** — a pure derivation
    function (`worker/payments_derive.ts`, server `today` in Pacific) computes
    draft/awaiting/due_soon/overdue/nonpayment_notice_due/notice_sent/suspension_notice_due/
    suspension_sent/paid + modifiers at read. Notice clocks key off RECORDED notice dates —
    the machine never pretends a notice went out.
11. **Box filing:** the original PDF files to `<job>/Schedules/` beside the job's other
    artifacts; the pool row records `box_file_id`. Flat evidence, same §51 posture as the
    manifest lane.
12. **Deferred fold-ins, designed-for but NOT built:** the Mac-side alert daemon (own future
    read tier `PORTAL_ALERTS_API_TOKEN`) computing behind-schedule
    (`finish < today ∧ % < 100`), not-started slip, stall (`last_marked_at` age), baseline
    slip, delivery slip (`is_delivery ∧ ¬delivered ∧ finish < today`), contract-milestone
    risk, and payment-reminder states from the derived cycle states; recipients via
    ITS_Config (config, not schema); already-sent suppression via Mac-side `state/` files
    keyed `(uuid, kind, date-bucket)`; a §51 `fieldops_sync` schedule-mirror pass to a
    per-job Smartsheet tracker. Each is new code over this schema.

## Invariants preserved

- **Invariant 1** — nothing in this lane transmits externally; the payment section is
  display-only and notice documents (if ever generated) ride the External Send Gate.
- **Invariant 2** — the upload is bounds-gated + magic-sniffed at the Worker; HMAC + separate
  sha/len recompute precede any byte handling on the Mac; §34 screening precedes render; all
  hostile-byte decoding (render + OCR) is subprocess-isolated; the browser never sees
  original bytes.
- **§4 no-invented-field-data** — OCR output is a proposal; every cell is human-confirmable,
  and consistency flags (implausible year, finish-before-start, %-range,
  duration-span-mismatch) point the human at likely misreads rather than auto-correcting.

## Slices

| Slice | Scope | Ships |
|---|---|---|
| PR-1 | ADR + migration 0066 (pool) + `fieldops_schedules.ts` + `schedule:v1` both sides + purge/prune/wire-types/errorCopy + worker suite + parity suite | dark (no SPA entry, no daemon) |
| PR-2 | sandbox hi-DPI render + `ocr_page_words` child fns + `schedule_ocr` / `schedule_geometry` / `schedule_parse` + corpus fixtures | library code |
| PR-3 | `schedule_poll.py` + plist + seeds + §43 runbook + full registry enrollment | dark (gate false) |
| PR-4 | migration `job_schedule_tasks` + validate screen + degenerate commit + `/jobs/:jobId/schedule` page | go-live flip after mirror validation |
| PR-5 | `cap.schedule.mark` + progress/milestone/delivered routes + chips UI | live |
| PR-6 | reconcile diff engine + resolutions + reconcile UI | live |
| PR-7 | payments migration + routes + derive + admin-only section | live |

## Consequences

- Go-live is `field_ops.schedule_poll.polling_enabled → true` **plus** loading the
  `org.solutionsmith.its.schedule-poll` plist (read ITS_Config for live state — this document
  records semantics, never the current value).
- A schedule lane that later wants XLSX intake widens the MIME allowlist deliberately
  (decision: PDF-only until the parser reads a Smartsheet XLSX export).
- The alert engine's arrival requires zero schema change; its arrival IS the moment
  `billing_cadence` starts doing work (transient expected-next-invoice reminders).
