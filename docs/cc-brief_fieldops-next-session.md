# Field-Ops Portal — Next-Session Handoff Brief

> ⚠️ **SUPERSEDED — HISTORICAL RECORD ONLY (marked 2026-08-10).** Every "do this next" below has
> since been built, and not always as specified here — notably the "P3 Materials" plan specs a
> `material_receipts` **D1 table** that was never built as such: the shipped design is migration
> `0059`'s `material_receipt_events` append-only ledger + `material_shipments` (design record in
> `docs/ROADMAP.md` Track 2 M2) and the §51 receipts mirror (#38). Do not build from this file.
> Current truth: CLAUDE.md "What's stubbed vs. real" + `docs/ROADMAP.md`.

**Repo/worktree:** `~/its-fieldops` (branch off `main`, currently `5cc4336`) · Worker+D1+React SPA expansion of the Evergreen Safety Portal
**As of:** 2026-06-28 — Write-UI phase complete (PRs #319–#322 four-part verified). P2.2 reads (#308–#310) + P2.3 writes (#312–#317, six slices; #318 = docs PR) landed. P2.4 SoR mirror is **PARKED/BLOCKED**.

> **Job-ID model note (added 2026-07-23):** superseded by P2.5 Slice 6 (2026-06-30) — "Job ID"
> is now a plain TEXT column the mirror WRITES with the portal-assigned `JOB-######` (Worker
> `job_counter`, migration 0022). The AUTO_NUMBER claims at the "reject writes" bullet and the
> "pending manual-UI setup step" item below describe the pre-Slice-6 design.

---

## TL;DR — do this next

- **Build P3 Materials now** — it is fully UNBLOCKED (D1-local, no SoR dependency). The 36-type catalog is operator-APPROVED (`field_ops/data/material_catalog_draft.json`). Ship it as an **admin-UI-EDITABLE** `material_catalog` table (CRUD routes + admin editor UI), **not** a static seed. Then `material_receipts` (per-job receive/track) and `material_incidents` (with screened photos). Mirror the **field-ops WRITE-module** slice shape just shipped — **not** the legacy safety-submission pattern (see the caution in §1 / canonical patterns).
- **Do NOT build P2.4** (the D1→Smartsheet mirror daemon). It is BLOCKED, not deferred-but-ready. Unblock condition is hard and external: **Seth must gain access to the canonical/main Evergreen Smartsheet** so the real schema + source-of-record for materials/deliverables is visible. Building against an unseen schema = encoding wrong guesses (worse than absent). Design constraints to apply once unblocked are captured in §2d so they aren't lost.
- **Land the `cap.admin.equipment` orphaned-key cleanup migration BEFORE any capability-management UI** ships — and the cap-grant UI in `AccountsPage.tsx` is itself unbuilt. Touches the capability vocabulary → confirm with Seth first.
- **Always branch per-slice off FRESH `main`** (`git fetch` first), edit Python only in a worktree **with its own venv**, and run the **four-part verify** on every PR. The live launchd daemon tree is `~/its` — never edit field-ops Python source there.
- **Before any P3 build:** confirm no open GitHub issues/PRs collide, and run a **live smoke of the new write-UI** (#319–#322) in the mirror tenant (`safety.evergreenmirror.com`) — the mocks-pass-but-live-fails class has bitten this program repeatedly.
- **Doctrine gates are Seth-only:** the Op Stds §50 "D1-as-writer" bump (v18→v19), the blueprint field-ops mission file, and the §43 daemon runbook are all FIXED high-capability-class — flag, don't self-resolve.

---

## Current state & canonical patterns to REUSE

These are the load-bearing conventions established across P2.2/P2.3 and the Write-UI phase. Replicate them; do not re-invent.

**What "mirror the slice just shipped" means — and what it does NOT (read before copying any pattern).** The safe template is the **field-ops WRITE modules** (`fieldops_*_write.ts`): capability-gate → mutation statement + audit-log statement in ONE D1 `batch()` → **bound params only** → server-generated **UNIQUE-constrained uuid** → **409 `uuid_conflict`** on replay. It does **NOT** mean the **legacy safety-submission pattern** (`/api/submit` + `/api/recent`), which uses a **client-controlled `submission_uuid` + `INSERT OR REPLACE`** (a peer can silently **overwrite a PENDING row with no audit row**) and a `/api/recent` that **leaks any job's UUID + payload** (no job scoping). A fresh engineer who copies that legacy pattern would replicate a real overwrite/scoping defect. **New P3 tables MUST use UNIQUE-uuid + audit-batch, never `INSERT OR REPLACE`, never a client-supplied primary uuid, never an unscoped list route.**

**Write-UI capability-gate is convenience only.** `useAuth()` capability checks in the SPA gate *controls* for UX, but the **Worker re-gates server-side on every route** — that is the real boundary. Pattern (PRs #319–#322): `useAuth()` cap-gate + `postJson` + `crypto.randomUUID()` for integrity-bar idempotency uuids + reload-after-write. Never trust the client gate as the security control.

**Two-process External Send Gate (Invariant 1).** Generation scripts have zero send capability; send scripts have zero AI. The Worker is **send-free by design** (serves a queue, accepts a receipt, never initiates outbound) — but note it is *outside* the Python AST gate (see Cross-cutting). Every new field-ops Worker route lands inside this ungated TS surface; keep it free of any outbound `fetch()`.

**Adversarial input handling (Invariant 2).** All external content is untrusted. Reuse `safety_reports/photo_screen.py` for any field-ops photo (incident reports, inspections) — magic→Pillow verify→bomb-cap→forced metadata-destroying re-encode→ClamAV-on-raw. MALICIOUS → CRITICAL + security-flagged Review-Queue row, refused before filing.

**Integrity-bar tables — `created_at`, NOT `recorded_at`.** `time_entries`, `equipment_logs`, `inspections` (migration 0015) use **server-authoritative timestamps** (`created_at`). **There is NO `recorded_at` column on these** — `recorded_at` exists ONLY on `equipment_location` (0014). Read routes alias: `SELECT … created_at AS recorded_at`. **DO NOT add a `recorded_at` column to any NEW integrity-bar table** (P3 `material_receipts` / `material_incidents` included). Copying older design-doc SQL that assumed `recorded_at` everywhere caused runtime **500s in P2.2** — use `created_at` and alias on read. These tables also use an **append-only `amends_uuid` chain** + **dual attribution** (account + on-behalf-of); keyset cursors page on the real `created_at`.

**Mutation + audit in ONE D1 batch.** Canonical write pattern (`worker/audit.ts`, `auditStmt` + `isUniqueViolation`): capability-gate → mutation statement + audit-log statement in a **single D1 `batch()`** → bound params only. Never write a mutation without its audit pair in the same batch.

**Singular write routes vs plural read routes.** Read modules are plural (`fieldops_personnel.ts`, `fieldops_equipment.ts`, `fieldops_jobtracker.ts`); write modules are entity-singular (`fieldops_time_write.ts`, `fieldops_job_write.ts`, `fieldops_task_write.ts`, `fieldops_equipment_write.ts`, `fieldops_equipment_roster_write.ts`). `cursor.ts` is the keyset codec — `decodeCursor` was hardened in #308 to reject non-primitive cursor values; **do not re-touch it**.

**Per-slice branch off FRESH main.** One PR per vertical slice, each contained to ~5 files. **`git fetch` and branch off current `main` every time** — the program squash-merges, so stale branches accrue residue.

**Worktree needs its own venv for Python edits.** The strict editable install resolves imports to `~/its` even with `PYTHONPATH`. A worktree editing Python *source* needs its OWN venv: `cp -R .venv .venv-wt` + `pip install -e --no-deps`. The publish daemon's `_reset_to_main` self-heal can reset a branch on the live `~/its` tree — always worktree for Python source.

**Four-part verify each PR** (`docs/operations/pr_merge_discipline.md`): (1) `state=MERGED`; (2) `mergedAt` non-null; (3) `mergeCommit.oid` present; (4) **main-branch CI on the merge commit = SUCCESS**. A PR passing 1–3 but failing 4 is functionally not landed.

**SPA test pattern.** `vi.mock('../../lib/auth')` defaulting to read-only; assertions inside `waitFor` (expect-inside-waitFor) after reload-driven state. Pure-SPA slices (#319–#322) touched no worker code.

---

## (1) IMMEDIATE — P3 Materials (buildable NOW, fully unblocked)

P3 is D1-local and independent of the parked Smartsheet layer. Scope: a datasheet-backed catalog, per-job receive/track, and incident reports. **No warehouse inventory / PO reconciliation** (explicitly out of scope).

### 1a. Admin-UI-EDITABLE material catalog
- **WHAT:** A `material_catalog` D1 table seeded from the 36 approved types, with **admin CRUD routes + a Materials-tab admin editor UI** so the operator can add/edit types in the future. **NOT a static seed-only table** — this is an explicit operator requirement ("we should be able to edit/add to it in the future within the UI").
- **WHY:** Catalog APPROVED 2026-06-27 (`field_ops/data/material_catalog_draft.json`, 36 deduped types: model/manufacturer/category/specs/source-PDF, auto-extracted from 80 project datasheets). It is correct and ready. Near-variants (480/600/800V) must stay **distinct**.
- **KEY FILES:** `~/its-fieldops/field_ops/data/material_catalog_draft.json` (source); new `safety_portal/migrations/00XX_materials.sql`; new `safety_portal/worker/fieldops_material_write.ts` + a read module; `safety_portal/src/lib/*`; `safety_portal/src/pages/` (Materials admin page); tests.
- **BLOCKERS:** None.
- **HOW TO START:** Mirror the slices just shipped — **migration + worker write module (capability-gated, mutation+audit in one batch, bound params) + read module (keyset, bound params) + lib + page + tests**. **Mirror the field-ops WRITE-module shape only** (UNIQUE server-generated uuid + audit-batch + 409 `uuid_conflict`); **do NOT copy the legacy `/api/submit` + `/api/recent` pattern** (client-controlled uuid + `INSERT OR REPLACE` + unscoped list — replicates the overwrite/scoping defect; see canonical patterns). Seed the table from the approved JSON in the migration (or a one-shot seeder). Gate admin CRUD behind an admin capability; the editor UI follows the FormEditor/AccountsPage shape. Add the new write module to `tests/test_capability_gating.py`'s AI-free list if it gains any Python actuator (it shouldn't — pure Worker/D1).

### 1b. Per-job receive/track (`material_receipts`)
- **WHAT:** `material_receipts` table (per-job: catalog type, serial/lot, qty, condition, received_by, received_at, job_id) + write route + a **submitter UI** to receive a delivery against a job.
- **WHY:** Datasheet-backed per-job receive/track is core P3; no warehouse inventory.
- **KEY FILES:** new migration; new worker write module; submitter page.
- **HOW TO START:** Integrity-bar pattern (server-authoritative `created_at`, append-only) — **do NOT add a `recorded_at` column** (alias `created_at AS recorded_at` on read; see canonical patterns). Join to `material_catalog` for type. One slice = one PR.

### 1c. Material incident reports (with screened photos)
- **WHAT:** `material_incidents` table (serial/identifier, description, severity, photos, reported_by, job_id, `material_receipt_id?`) + a **field UI** for submitters to file incident reports.
- **WHY:** Internal-only now. A future send-gated warranty path (two-process gate) is **deferred** — note it, do not build it.
- **KEY FILES:** new migration; new worker write module; field page; **reuse `~/its/safety_reports/photo_screen.py`** for incident photos.
- **BLOCKERS:** None for the D1-local build. Mirroring materials to Smartsheet is part of the parked P2.4 layer — **do not block the D1-local build on it**.
- **HOW TO START:** Integrity-bar pattern, server-authoritative `created_at` (**no `recorded_at` column**). Photos ride D1-inline base64 today (ADR-0001; ≤8 × 400 KB/submission). R2 is the recorded upgrade path if budgets grow — out of scope now.

---

## (2) BLOCKED — P2.4 SoR mirror daemon + origin-flip inversion + Box linkage

**Do not build any of this until the unblock condition is met.** Merging all duplicate entries (tech-debt `[BLOCKED 2026-06-28]`, plan, briefs, memory `decision_p2.4-parked-no-smartsheet-access` / `feedback_dont-build-against-unseen-sot` / `project_fieldops-portal-program`, code-stub `fieldops_sync.py`):

### 2a. D1→Smartsheet mirror daemon + origin-flip inversion
- **WHAT:** `field_ops/fieldops_sync.py` is an **89-line SKELETON** (`sync_once()` gate-checks `_sync_enabled()` and `return 0`; `DEFAULT_SYNC_ENABLED = False`). Build: push field-ops tables (personnel/equipment/task_assignments/time_entries/inspections) **UP to Smartsheet as operator-SoR**, AND the **origin-flip inversion** — the Worker `POST /api/internal/sync` UPSERT does **NOT** touch `jobs.origin`, so a promoted portal-created job stays **permanently sweep-exempt** unless the daemon flips `origin → 'smartsheet'` (+ `sync_state` / `canonical_job_id`), mapping provisional `PJOB-<uuid8>` → canonical `JOB-####` write-back. (`origin`/`sync_state` columns exist in migration 0017; down-sync deactivation is already scoped `WHERE origin='smartsheet'`.)
- **WHY decoupled:** The read/write layers read D1 LIVE (the local primary) and are send-free / Invariant-1-decoupled. The SoR up-sync is downstream filing they do NOT block and do NOT implement.
- **KEY FILES:** `~/its-fieldops/field_ops/fieldops_sync.py:1,9-11,48,74-85`; `field_ops/README.md:17-19,28`; `~/its/docs/tech_debt.md [BLOCKED 2026-06-28]`.
- **BLOCKERS (EXACT unblock condition):** **Seth has no access to the canonical/main Evergreen Smartsheet account** — the real schema + source-of-record for materials/deliverables is unseen. A daemon written against a guessed target schema would be wrong (worse than absent). **Unblock = Seth gains access to the main Evergreen Smartsheet (real schema + SoR visible).** Doctrine-critical → must go to **Claude Code** (not a Pit Wall agent), with **ops-stds-enforcer** + a **§30 live integration test** (`sdk-integration-test-scaffold`) + a **§43 runbook**.

### 2b. PORTAL_FIELDOPS_API_TOKEN bearer + `/api/internal/fieldops/*` endpoints
- **WHAT:** Token is declared in the Worker env type (`worker/types.ts:43`) but there is **NO bearer guard and NO `/api/internal/fieldops/pending-mirror`** (or sibling) endpoint. The privilege-separated bearer (distinct from `PORTAL_INTERNAL_API_TOKEN`) + internal mirror endpoints land **with** the P2.4 daemon.
- **BLOCKERS:** Tied to 2a (parked). Also a P0 open decision: audit posture for internal bearer admin routes (some URS `/api/internal/admin/*` user mutations lack an audit pair — add audit-batch or document the system-level omission).

### 2c. Box document linkage (`box_file_id` on field-ops records)
- **WHAT:** Add a `box_file_id` (or folder ref) column to document-bearing field-ops records (inspections; later job docs) and surface it on the read routes — mirroring how safety-report submissions carry `box_file_id`. Not yet on the field-ops tables/schema.
- **BLOCKERS:** Part of the parked SoR/filing layer; tied to the unseen-SoR concern for document filing.

### 2d. Design constraints when unblocked (capture now — apply at build time)

Recorded here so the parked-but-real constraints aren't lost when P2.4 reopens.

- **(a) Smartsheet write constraints.** `ABSTRACT_DATETIME` (Date/Time user type) accepts **only naive `YYYY-MM-DDTHH:MM:SS`** — write **naive Pacific wall-clock**; offsets/`Z` are rejected (`errorCode 5536`), plain `DATETIME` is rejected (`errorCode 4000`). **`AUTO_NUMBER` columns reject writes** (e.g. `Job ID` — never push it; resolve canonical IDs by read-back). The **live-integration test has an eventual-consistency flake** (create→read/write 1006/404) and is **operator-run-only, NOT CI** — known/tracked, don't re-diagnose. Land writers BEFORE retyping any live column. Needs **§30 SDK-vs-live body-shape coverage** for the D1→Smartsheet writes, plus token-scope / write-capability validation.
- **(b) Box document-linkage constraints.** `box_client` has **no network timeout** — a real hang risk on a polling daemon; add one before wiring Box into the loop. **Box folder delete-and-recreate breaks file-ID resolution** (find-or-create by name, never delete+recreate). The **refresh token rotates every exchange and must be persisted to Keychain or ITS dies in 60 days** (`_store_tokens` callback; `test_store_tokens_persists_refresh_token` locks it). A **dedicated ITS Box user** arrives at Phase 1.5 cutover. **`canonical_job_path()` format is UNCONFIRMED** — verify the on-disk folder convention before filing.
- **(c) Daemon config-hardening.** Hardcoded `ITS_Config` fallbacks risk **silent-fallback-after-typo** (a misspelled config key resolves to the default with no signal). Want **fail-loud startup config validation** and a **watchdog hang-killer** for the long-running poll.
- **(d) Daemon doctrine wiring.** The daemon must enroll in `tests/test_capability_gating.py`'s **AI-free list**, ship a **launchd plist**, a **Watchdog Check-C marker**, and an **`ITS_Daemon_Health` row** (the last two via the not-yet-done `shared/heartbeat.py` extraction — see Cross-cutting; the P2.4 daemon would be the 4th verbatim copy).

---

## (3) Tracked follow-ups (P2.3 write-layer governance + doctrine)

### 3a. `cap.admin.equipment` orphaned-key cleanup migration — BEFORE any cap-management UI
- **WHAT:** Migration 0016 seeds `cap.admin.equipment` + grants it to admin, but **NO route enforces it** — the roster routes gate on `cap.equipment.manage` (0013) per the design's F2 choice. Access control is correct today (fail-closed, submitter→403), but `role_capabilities` shows admin holding a key that controls nothing. Fix: a cleanup migration (e.g. **0019**) DELETEing `cap.admin.equipment` from `capabilities` + `role_capabilities`.
- **WHY now:** Must land **before the capability-management UI becomes operator-reachable**, or an operator who grants/revokes it silently affects nothing.
- **KEY FILES:** `~/its-fieldops/safety_portal/migrations/0016_equipment_management.sql`; `~/its/docs/tech_debt.md [OPEN 2026-06-27 #2]`.
- **BLOCKERS:** Touches the capability vocabulary → **confirm with Seth** (high-capability-class).

### 3b. Capability grant/revoke UI in `AccountsPage.tsx` — UNBUILT
- **WHAT:** P0 called for a cap grant/revoke surface in `AccountsPage.tsx` (grep confirms no capability code there). Default submitter/admin cap SETS are seeded by migration 0013 and work; the interactive operator surface is missing.
- **BLOCKERS:** Gate **H1 (3a) before this ships.**
- **KEY FILES:** `~/its-fieldops/safety_portal/src/pages/AccountsPage.tsx`.

### 3c. Inspection quick-log — BLOCKED on equipment pre-inspection forms catalog
- **WHAT:** The deferred P2.3 Slice 5: `POST /api/fieldops/equipment/:id/inspection` → `inspections` (version-pinned). The `inspections` table exists (0015) but has **no writer/feed**.
- **BLOCKERS:** **No equipment pre-inspection FORMS CATALOG exists to validate `form_code` against** (the form-editor's published forms are safety/progress ones, `identity-v<version>`-validated, not equipment inspections). Blocked on operator/domain input: **define the equipment pre-inspection forms + their `form_code`s** (e.g. `skid-daily`, `telehandler-preuse`). Then it's a quick add — same integrity-bar pattern as the maintenance log + a `form_code` allow-list + server-side version-pin.
- **KEY FILES:** `~/its-fieldops/safety_portal/migrations/0015_urs_integrity_bar.sql`; `~/its/docs/tech_debt.md [OPEN 2026-06-27 #1]`.

### 3d. `cap.tasks.own` 0013 label tidy — cosmetic, broad CONFIRMED
- **WHAT:** The 0013 description says "View + complete OWN assigned + daily-checklist tasks" but the enforced policy is **BROAD** (any holder advances any task — field-PM-manages-the-board). Operator **CONFIRMED broad** 2026-06-27. Update the description string to match enforced behavior — **migration-comment/description tidy, no behavior change**.
- **KEY FILES:** `~/its-fieldops/safety_portal/migrations/0013_add_roles_capabilities.sql`.

### 3e. §50 "D1-as-writer" doctrine bump (v18→v19) + §43 runbook — Seth sign-off only
- **WHAT:** Making D1 the authoritative primary that mirrors to Smartsheet (incl. payroll-grade field-ops data written send-free + audit-trailed without per-entry human approval) is a **doctrine decision**. Op Stds is currently **v18, §-cap §49 — NO §50 exists**; §50 is a CANDIDATE flag raised 2026-06-10 in the safety-portal v4 reconciliation, still pending. Adopting §50 realizes v19 ("new §"). Plus the **§43 successor-remediation runbook** for the (blocked) P2.4 daemon. Built under the operator's "proceed" go-ahead; the formal bump is the standing P0-ceremony item.
- **WHY appears 3× :** Surfaces as P2.3 follow-up #5, P2.4 SoR piece #3, and P0-ceremony #7 — **one item**.
- **BLOCKERS:** **Seth's sign-off** (FIXED high-capability-class doctrine). Separate gate from the SoR-visibility blocker. Flags-only, no `doctrine/*` edit until cleared. Companion v4/v5 flags (operator+Claude form-maintenance principle; §34 image-class screening; FM Invariant-2 Layer-6 wording) ride with it.
- **KEY FILES:** `~/its-blueprint/doctrine/operational-standards.md`; `~/its/docs/tech_debt.md [OPEN 2026-06-27 #5]`.

### 3f. Blueprint ceremony — mostly DONE; residual items
- **State:** The blueprint-side workstream-creation ceremony is realized as **`workstreams/urs-marine-portal/`** (Customer 2, URS Marine). `mission.md` is **canonical v1** (flipped draft→canonical 2026-06-17, blueprint PR #46, tag `excellence-roadmap-v5`); carries the kickoff decisions (three-tier capability-gated RBAC, 2→N DB-driven role generalization, **D1 = structured-data SoR**, Box = document SoR, Monday/PM-tool = bounded outbound via swappable adapter). B1–B5 CC briefs exist. Excellence Roadmap v4→v5 Track 3.4 (Platform Fork-Source) landed; exec manifest synced (exec PR #293).
- **RESIDUAL:** `brief.md` is **still status: draft**; B2/B3/B4 **§43 runbook entries are SPECIFIED as DoD but NOT yet written** (pending the actual builds — symptoms/low-class repairs/escalate boundaries are drafted in the briefs); FM/V&R/Op Stds/Handover Authority-block companion cites for the v5 cascade are intentionally **deferred** to each doc's next bump. There is no standalone `field-ops` blueprint dir — the program lives under `urs-marine-portal`. **Pre-flight for any blueprint frontmatter work:** confirm/register **`urs-marine-portal`** in `lint_frontmatter.py` `CANONICAL_WORKSTREAMS` (the canonical workstream slug — NOT `field_ops`/`field-ops`).
- **KEY FILES:** `~/its-blueprint/workstreams/urs-marine-portal/{mission.md,brief.md,briefs/B1-B5}`.

---

## (4) Later phases — P4 and P5 (D1-local, not SoR-blocked)

### P4 — tasks-to-people, my-tasks/daily tab, rolling SOP checklist, loop-closure
All UNBUILT (D1-local). The `task_assignments` table + add/status write routes already exist from P2.3 (gated `cap.tasks.own`, broad policy).
- **Task model extension + admin-assigns:** Extend the single task entity with `source: adhoc|deliverable|checklist`; add the admin-assigns-task-to-a-submitter-account flow. (`worker/fieldops_task_write.ts`.)
- **My Tasks / Daily tab:** Currently a **PLACEHOLDER on the home** (only Log-time got wired). Build the submitter+admin tab filtering by assignee + today's checklist instances. (`src/pages/HomePage.tsx`.)
- **Rolling SOP daily checklist (the P4 keystone):** Seed the canonical SOP template from `~/Downloads/Site_Supervisor_SOP 2.docx` — phases (Arrival / Morning Kickoff / OSHA Oversight / QC / Throughout / CM Check-ins / End of Day); items typed `manual_attest | form_linked(form_code) | count(metric≥N) | inspection`. Admin-editable template; a daily generator (Worker-on-read or small daily daemon) instantiates per-PM-per-job each day.
- **Loop-closure (hybrid auto-complete / manual-attest):** `form_linked` items (Daily JHA, Visitor Log, Incident Report, deliveries→receipt/incident, 50-photo count, Daily Report) auto-complete when the linked artifact is filed for that job+day; `manual_attest` items (PPE, site secured, conduit capped, EOD CM check-in) attest with optional note/photo. **RETENTION FENCE (risk #5):** each checklist instance MUST persist its own `satisfied_by_uuid` + `filed_at` snapshot — do NOT re-join live submissions (filed payloads stripped at 90d, deleted 30d post job-inactive in `worker/prune.ts`).
- **Daily rollup + Sign-Workers-In:** Completing the checklist generates/populates the Daily Report progress form and files it like other progress forms (P1, NOT auto-sent). Wire "Sign Workers In" against the non-login roster (P2 personnel with `account_id NULL`) + the approved-subcontractor check. (`forms/daily-report-v1.json`.)

### P5 — job inspections build-out
- **WHAT:** UI/route surface over the already-ported `inspections` table (0015). Build **job-level** inspections distinct from equipment inspections — types derived from SOP §§B–C (trenching/competent-person before-shift + after-rain, post-pile QC, racking torque, grounding, level/as-built deviations/NCR). Job-scoped records with results + photos (photo-screened via `photo_screen.py`); link to the corresponding P4 checklist items; mirror to Smartsheet (parked P2.4 layer — do not block on it).
- **KEY FILES:** `~/its-fieldops/safety_portal/migrations/0015_urs_integrity_bar.sql`.

---

## (5) Cross-cutting / hygiene

**Live smoke of the new write-UI (do before P3).** The mocks-pass-but-live-fails class has bitten this program ≥3× (e.g. the picklist-registry-missing-`SENDING` block). Run a live submission against the mirror (`safety.evergreenmirror.com`) exercising #319–#322 (equipment status/move/roster, Job-Tracker create/close/progress/add-task/task-status, time logging) before stacking P3 on top. Pattern: unload daemon if touching the live tree, or just drive the SPA in the mirror tenant.

**ITS_Active_Jobs operational go-live gaps (the Job Tracker reads this sheet).** Three live data gaps a fresh engineer will hit as confusing "empty/missing" states, not bugs: (1) the **AUTO_NUMBER `Job ID`** column has a **pending manual-UI setup step** (configure in the Smartsheet web UI; the SDK can't); (2) the **"New Job" Smartsheet form is still pending** (no operator intake surface yet); (3) **~6 rows have blank Address cells** awaiting **office-PM fill**. None block P3; flag to operator rather than "fix" in code.

**Publish-daemon health + worktree hazard.** Field-ops uses the **same publish daemon** to actuate form publishes. It has **zero watchdog / `ITS_Daemon_Health` coverage** (no `write_last_run_marker`, no health row, absent from `TRACKED_JOBS`) and **no dry-run/integration harness** — it is **operator-validated-live-only**. Its `_reset_to_main` self-heal can **strand the live `~/its` tree on a `publish/req-*` branch**. Therefore: **run it from a dedicated worktree, never edit field-ops Python source in `~/its`**, and treat its lack of observability as an open now-priority hardening item (a health row + marker would also surface a stranded-branch state).

**`shared/heartbeat.py` + `shared/runner.py` extraction (infra, 3+ consumers — trigger met).** The ~8 `ITS_Daemon_Health` heartbeat helpers + `_write_watchdog_marker` are copied VERBATIM across `weekly_send_poll.py`, `portal_poll.py`, `compile_now_poll.py` (and the retired `intake_poll.py`). AST-logic-identical, docstrings drifted. **The P2.4 mirror daemon would replicate them a 4th time** — land the extraction first (parameterize on daemon_name + state_path + slug; write-condition as a callable). ~half-day + 8–12 unit tests. `compile_now_poll`'s daemon-health row is deferred pending this. (Related: the `ITS_Daemon_Health` sheet already misleads Tier-2 successors — stale `intake_poll` row, `NEVER_RAN` rows, missing publish/compile rows — so any new self-provisioned row lands into a noisy surface; budget a cleanup pass.)

**Worker-side send-gate is outside the Python AST gate (W2).** `tests/test_capability_gating.py` AST-scans only Python under `shared/`+`safety_reports/` — it does NOT reach the TS Worker, which now holds the HMAC secret + internal bearer. Nothing structurally prevents a future Worker edit (field-ops routes included) from acquiring an outbound `fetch()`. **Every new field-ops Worker route lands in this ungated surface.** Fix when the Worker surface grows: a CI grep / ESLint rule forbidding `fetch(` in `safety_portal/worker/` except the ASSETS binding.

**Python capability gate gaps (M2).** Static-AST-import-only — blind to `importlib`/`__import__`, no transitive closure, `WALKED_ROOTS` excludes `scripts/`. Relevant when enrolling the P2.4 daemon. (`tests/test_capability_gating.py::_imports_in`.)

**Stale local branch refs.** Seven CLOSED-unmerged branches preserved conservatively (`publish/req-*` ×4–5, `feat/portal-submit-as`). Safe to delete via `git update-ref -d refs/heads/<branch>` AFTER per-branch `gh pr view` confirms not-needed — **NOT `git branch -D`** (hook-blocked in CC). PR state=MERGED is the only safe delete signal.

**Deferred Form-Request month/form filter (PR-6) — NOT built, brief ready.** Full self-contained CC brief at `~/its-fieldops/docs/cc-brief_form-request-month-filter.md` (also `~/its/docs/...`). `GET /api/filed?job_id` returns up to 500 filed forms in one flat silently-truncating table — unusable for a year-long job. Fix = Job → Month-Year → (optional) Form-type cascade, filtered by **`work_date`** (NOT `filed_at`, locked decision). New `GET /api/filed/months?job_id` → `{months:[{month,count}], form_codes:[…]}`; extend `GET /api/filed` with `&month=YYYY-MM` (regex `^\d{4}-\d{2}$`) + `&form_code` (≤64). **NO migration** (existing `idx_submissions_lookup` covers it → plain `npm run deploy`). Worker stays send-free; requester-bound `/pdf`,`/status`,`/api/internal/pdf-requests` untouched. **Run `brief-validator` first** (brief ~16 days old — verify line anchors) + `portal-worker-security-reviewer`. Out of scope: in-month pagination, free-text search, optional `idx_submissions_job_workdate` (add only if a job exceeds ~10k rows), any change to the download model or two-stage prune.

**Cheap deferred read-hook — jobs provenance.** Surface `jobs.origin`/`sync_state` in the Job-Tracker (Brief C, #310) list/detail response so the portal shows "from Smartsheet" vs "created in portal" the moment the mirror daemon lands. Small response-shape extension (`fieldops_jobtracker.ts` + lib + page + tests). **Explicitly deferred / NOT built** — do not build ahead of P2.4.

**ops-stds-enforcer is pinned at Op Stds v13** (three majors behind v18) — blind to §43/§44. Field-ops diff review against §43 DoD will under-enforce until the agent file is version-bumped (tracked in exec `docs/tech_debt.md`).

**Deploy/cutover awareness (not field-ops-specific, inherited).** All new field-ops API routes inherit the unthrottled posture (no rate limiting on `/api/login` or `/api/*` — operator-gated Cloudflare WAF at cutover). `bcryptjs` cost-10 may exceed Workers Free 10ms CPU cap (Error 1102) — decide Paid plan vs PBKDF2 before `wrangler deploy`. Session logout is client-side only (session-epoch revocation + role-aware idle timeout is a Phase-2 D1-migration fix on the shared auth middleware). After any deploy that adds/changes a `custom_domain` route, immediately repoint daemon base URLs (`workers.dev` gets disabled). Migration-before-Worker is deploy-order-critical (apply the D1 migration `--remote` BEFORE redeploying, or new routes fail-closed against a missing table). **CC is classifier-blocked on live D1 migrations — the Developer-Operator runs them.**

**Minor hygiene (one-liners, not blocking):** browser-tab title/favicon still reads "ITS Portal" (cosmetic, confirm desired field-ops wording); FormsPage rollback-UI is deferred (the publish contract accepts `op:"rollback"` but the editor never surfaces it).

**No open GitHub issues/PRs were surfaced in the gathered set** beyond the merged #302–#322 chain; confirm with `gh pr list` / `gh issue list` at session start before branching.

---

### Phase ledger (quick reference)
- **P0 Foundations** — DONE (#302). Capability system + `field_ops/` scaffold; `fieldops_sync.py` skeleton only.
- **P1 unified shell + Safety/Progress split + Daily Report** — DONE (#303–#305).
- **P2.1 schema port + jobs origin fence** — DONE (#306, migrations 0014–0017).
- **P2.2 read views** — DONE (#307–#311, migration 0018 indexes + `cursor.ts`).
- **P2.3 write routes** — DONE (#312–#317, six slices; #318 = docs PR; `audit.ts` + per-entity write modules + §43 `fieldops_job_write.md`).
- **Write-UI** — DONE (#319–#322, pure-SPA).
- **P2.4 mirror daemon** — **BLOCKED** (no SoR access; design constraints captured in §2d).
- **P3 Materials** — **NEXT, unblocked.**
- **P4 / P5** — later, D1-local.
- **Doctrine ceremony (§50 / mission / §43)** — **Seth sign-off gated.**
