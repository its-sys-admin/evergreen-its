---
type: operations
date: 2026-07-23
status: active
related_prs: []
workstream: null
tags: [runbook, successor-remediation, job-closure, archive-on-closure, lifecycle, tier-2, smartsheet, box, d1]
---

# Runbook — Close or archive a project (what actually happens today) (Successor-Remediation, Op Stds §43)

A §43 successor-remediation entry for the **Successor-Operator** (edits Smartsheet rows, uses the
portal admin pages, reads alert emails — does **not** read code or touch secrets). This runbook
documents **current behavior only** — what closing a job does *today*, not what a fuller closure
policy might someday do. The policy question (what *should* happen to the other per-job surfaces)
is a separate proposal pending planning-project ratification — see
`docs/reports/2026-07-23_project_closure_policy_proposal.md`.

The §42 code-reader rationale lives in `field_ops/fieldops_sync.py`
(`_archive_closed_job_trackers`) and `safety_portal/worker/fieldops_job_write.ts` (the lifecycle
route). Companions: [job_archive.md](job_archive.md) (**the archive / un-archive action and every
one of its faults** — Task B below is only the pointer),
[safety_portal_job_management.md](safety_portal_job_management.md) (add/retire jobs),
[fieldops_sync.md](fieldops_sync.md) (the mirror daemon).

## Purpose

One page answering: *"a project is finished — what do I do, and what does the system actually do?"*

The honest summary: **closing a job is mostly a passive act, and archiving one is not.** Flipping a
job off `Active` makes it drop out of dropdowns, compiles and intake — but everything the job ever
produced **stays exactly where it is**. Moving documents is a *separate*, explicitly confirmed
action (Task B → [job_archive.md](job_archive.md)), and the only thing in ITS that relocates a job's
folders.

## Where a job's lifecycle is set (this matters)

- **Portal-created jobs** (any job made in the portal — the normal case): the **portal Job Tracker
  page** is the *authoritative* lifecycle writer. An admin with the job-management capability
  selects **Active / Inactive / Archived** in the job's lifecycle selector. Do **not** flip the
  `Active` cell in `ITS_Active_Jobs` for these jobs — the mirror daemon **overwrites the sheet from
  the portal** on the job's next portal edit, and until then the flip is **worse than a no-op**:
  crews still see the job in the portal dropdown (the portal side never learned of the flip), but
  every submission they file routes to Orphaned Reports, and the weekly compiles skip the job — a
  split-brain that lasts until the sheet is overwritten or corrected.
- **Sheet-created (legacy) jobs** (rows added directly in `ITS_Active_Jobs` that never went through
  the portal): the sheet-side `Active` flip is the lever, per
  [safety_portal_job_management.md](safety_portal_job_management.md) Task B. Note for these jobs a
  sheet-side value of `Archived` behaves identically to `Inactive` — it can **never** trigger the
  tracker archive move (that automation only sees portal-origin jobs).

> **The lifecycle selector now tells the truth.** It used to display **"Inactive"** for an
> **Archived** job after any page reload, because the detail view re-derived it from a coarser
> status field that cannot tell inactive and archived apart — which is why earlier versions of this
> runbook told you to "validate by effects, not the dropdown". That is fixed: an archived job reads
> **"Archived"**, and its selector is locked (the job must be un-archived before its state can
> change again).

## Procedure

### Task A — Normal close: set the job **Inactive**

Use for a finished, paused, or on-hold job. Everything below is passive drop-out, and it is
reversible by setting the job back to Active — with one exception: submission rows already
pruned from D1 (step 5) do not return on reactivation. Box and the week sheet are unaffected;
the portal just can no longer list/serve those older forms.

What actually happens:

1. **Portal dropdowns**: the job leaves the submission dropdown (only Active jobs are served), so
   crews can no longer file against it.
2. **Late submissions are refused, not lost**: a submission that still names the job (e.g. queued
   before the flip) routes to the **Orphaned Reports** surface (or the Review Queue) for operator
   disposition. The field user still sees "received" — the refusal is operator-facing.
3. **Weekly compiles skip it**: both the safety and progress weekly generators iterate **Active**
   jobs only. No further WSR/WPR rows, packets, or week sheets are produced for the job.
4. **Mirror rows stay**: the job's rows in `ITS_Active_Jobs` and `ITS_Active_Jobs_Progress` remain
   in-sheet with `Active = Inactive` — the historical record. **Never delete the row.**
5. **D1 hygiene (automatic, delayed)**: once the job is inactive, each of its already-filed
   portal submission rows is pruned from the Worker's D1 cache when that row is 30+ days past
   its own filing date — old filings go on the next daily run, recent ones age out
   individually (Box + the week sheet remain the record). The D1 job row itself is deleted
   only if it holds none of the guarded record types (submissions, time entries, tasks,
   inspections, daily requirements, expected materials, checklist instances,
   equipment-location history). This prune is monitored by watchdog Check V.

What does **not** happen: no sheet is moved or archived, no Box folder changes, no flat-log or
review rows change. See "What closure leaves in place" below.

### Task B — Archiving a job: its own deliberate action, documented separately

Archiving is **no longer** a dropdown value. It is a separate, confirmed action with its own
button, its own runbook, and its own failure modes: **[job_archive.md](job_archive.md)** — read
that before archiving anything, and use it for every archive fault.

The short version:

- The lifecycle selector does not offer **Archived**, and the server refuses the value even if an
  old browser tab still shows it (409 `use_archive_route`). Sheet-created (legacy) jobs are
  unaffected — typing `Archived` into `ITS_Active_Jobs` for one of those triggers no automation,
  and never did.
- Archive lives on the job's **Archive** card in the portal, behind a typed confirmation that names
  exactly what moves. It relocates **seven containers** — four Smartsheet folders (Safety, Progress,
  Purchase Orders, Subcontracts) and three Box ones — into `ITS — Archive / <Job>/` and
  `ITS Archive / <Job>/`. It is reversible with **Un-archive**.
- Pressing Archive **records intent**; the office Mac performs the move on its next cycle and
  reports back. That is why the card says "Archiving…" instead of claiming it is done.
- **A stopped archive does not resume by itself.** "Partly archived" and "Nothing moved" are
  terminal — they wait for the operator's **Try again**. This is the single most important thing to
  know, and it is worked in [job_archive.md](job_archive.md).

Until 2026-08-03 archiving was a dropdown value that silently relocated four tracker sheets on the
next daemon cycle — no confirmation, no retry, and a screen that then displayed the job as
"Inactive". It never ran against real data; that path is disarmed and preserved in place.

**Task A (Inactive) is still the right move for a job that is merely finished.** Inactive is what
makes a job stop appearing in dropdowns, compiles and intake — the day-to-day effect you usually
want. Archive additionally *relocates documents*, which changes who can read them.

### Task C — What closure leaves in place (deliberate + known gaps)

Nothing below is touched by **Inactive**. Retention-in-place is the current de-facto policy for a
closed job; which parts should change is exactly what the closure-policy proposal is for.

> **Archiving is the exception, and this table predates it.** The Archive action relocates the
> job's four Smartsheet per-job folders and its three Box folders — which carries the week sheets,
> the standing trackers and the procurement documents inside them. The rows below still describe
> where those surfaces live for a job that has **not** been archived; for what an archive moves and
> what it deliberately leaves (the flat logs, the review rows, the `ITS_Active_Jobs` row, `ITS
> DATA`, `ITS Photos`, every portal record), see [job_archive.md](job_archive.md). Re-deriving this
> table for the archived case is the pending closure-policy rewrite, not a Tier-2 concern.

| Surface | Where it stays |
|---|---|
| Safety week sheets + per-job folder | On **Archived** (with the archive pass enabled), the per-job FOLDER relocates to `ITS — Archive / <Job> / Safety/` — contents intact; on Inactive, in place |
| Progress week sheets + the per-job folder | On **Archived**, the whole per-job FOLDER — all five tracker sheets AND the week sheets inside it — relocates to `ITS — Archive / <Job> / Progress/` ([job_archive.md](job_archive.md)); on Inactive, in place |
| WSR / WPR human-review rows | Their review sheets, in place (send history) |
| `ITS_Active_Jobs` / `ITS_Active_Jobs_Progress` rows | In-sheet, flagged Inactive/Archived (by design — the history; never delete) |
| Per-job "Purchase Orders" / "RFQs" / "Subcontracts" sheets | On **Archived**, their per-job FOLDERS relocate to `ITS — Archive / <Job> / <Workstream>/`; on Inactive, in place |
| PO_Log / RFQ_Log / Estimate_Log / Subcontract_Log rows + procurement review rows | Flat ledgers, in place (live commercial records, retained by design) |
| The job's Box tree (week PDFs, packets, PO/RFQ/quote/subcontract files) | On **Archived**, the two per-job Box containers relocate to `ITS Archive / <Job> /` via `box_client.move_folder` — the safety root's job folder carries the procurement files with it; `ITS DATA` / `ITS Photos` stay in place |
| D1 field-ops records (time entries, tasks, equipment history, checklists, …) | D1, retained (payroll-grade source records; only the guarded prune/purge paths above ever remove anything) |

### Task D — Removing a job entirely (destructive — NOT closure; escalate)

"Make this job disappear everywhere" is a **manual, three-system, destructive** operation —
removal, not archival — and it is **not a Tier-2 action**. Escalate to Seth. For the
Developer-Operator, the known footgun (HOUSE_REFLEXES §7):

1. **Delete the job's `ITS_Active_Jobs` row FIRST.** If the row still exists when the portal job
   is purged, the down-sync **re-creates** the job in D1 as a sheet-origin row within a minute.
2. Then purge the portal side (`purge-job`) — an atomic, audited **D1-only** cascade. It
   deliberately touches nothing outside D1.
3. Everything else — the Smartsheet per-job folders/week sheets/tracker sheets/log rows, and all
   Box files — is manual cleanup in each system's UI. No automation spans the three systems.

## Validation

- **Inactive took**: the job is gone from the portal submission dropdown; the next weekly compile
  produces no new WSR/WPR row for it; its `ITS_Active_Jobs` rows read `Inactive`.
- **Archived took**: the job's Archive card reads "Archived. All 7 folders are filed under
  `ITS — Archive / <Job>`", those folders are visible in the Archive workspace and in Box's
  `ITS Archive`, and `ITS_Errors` has no `job_archive` rows for the job. Anything short of six
  is [job_archive.md](job_archive.md) Symptom 2 — it does **not** resume on its own.

## Escalate to Seth (Tier 3) when

- An archive or un-archive fault is not resolved by the documented re-press — the workspace
  permissions, the Box archive root, the token identity and the move code are all high-class. The
  boundary is stated in observable terms in [job_archive.md](job_archive.md).
- You need a job **removed** (Task D) — destructive, three-system, Developer-Operator only.
- Anything that would *extend* archival beyond the four trackers (week sheets, Box, procurement,
  D1 export) — that is a doctrine-level policy change (§51), not an operational tweak.

## Owner

`@solutionsmith`. Update this runbook when the closure-policy proposal is ratified or the trigger
semantics change (both tracked in `docs/reports/2026-07-23_project_closure_policy_proposal.md`).
