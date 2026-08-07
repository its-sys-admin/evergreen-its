---
type: reference
status: active
workstream: docs
tags: [roadmap, forward-path, canonical]
---

# ITS Roadmap — the single marching order

**Purpose.** The one top-level forward path for ITS, consolidating the field-ops program, the workstream
missions, the two 2026-07-03 audits (complete-state, unbounded-growth), and tech-debt into a single ordered
list. Detail lives in the sources cited per track — **this file is the index of what's next, not a restatement.**

- Field-ops detail: `project_fieldops-portal-program.md` (auto-memory — the P/R/D/M/CS/S/G series + operator queue).
- Design source: `~/its-blueprint/workstreams/*/mission.md` (planning-layer wins).
- Current-state (what's built): `CLAUDE.md` "What's stubbed vs. real" table.
- Working standards: `docs/HOUSE_REFLEXES.md`.

> **Anti-sprawl contract:** new scope is added HERE (or to tech_debt), at the right track — not in a new
> top-level plan file. `~/.claude/plans/` is scratch; the canonical roadmap is this doc.

---

## Now → next (ordered)

### Track 0 — Finish the Progress Reporting go-live *(in-flight; started ad-hoc, complete it correctly)*
The formal 6-step sequence is `~/.claude/plans/complete-state-audit.md` A2. Status this session: gate ⑥
flipped, plists ⑤ loaded, Box folder ① made, **box-root config ② set (row 44) ✅**. Remaining:
- ② the other config rows: **dup `worker_base_url` under `Workstream=progress_reports`** (fixes the confirmed
  silent rollup-page skip), `progress_reports.progress_send.from_mailbox` (+ confirm the mailbox exists in the
  Graph access policy).
- ③ **§46 re-share** workspace `5988851429730180` to approver identities (else every send fails-closed HELD).
- ④ add `progress_reports` to the `ITS_Review_Queue` Workstream picklist.
- **Wire progress Compile-Now — LANDED.** `safety_reports/compile_now_poll.py` already iterates BOTH
  safety + progress week configs via `COMPILE_CONFIGS` (one per workstream; per-workstream
  `<ws>.compile_now_poll.polling_enabled` gate) — same daemon, no new plist.
- Validate: manual `progress_weekly_generate` → weekly progress packet under the Box root + a `WPR_human_review`
  row (no Review-Queue drain, rollup page present). Then clean up this session's smoke test data.

### Track 1 — Close out the field-ops build *(operator, near-done)*
Deploy confirmed (migrations 0028→0037 applied). Remaining operator work:
- **Confirmation flags:** D1 required-content floor · M2 `category:progress` · S5 per-manager Daily-Report
  rollup · **CS4 Part-B keep/revert** (cap.form.submit/request enforcement) · `photo-test-v1` retire-or-canary.
- **Mandatory live smokes:** photo-stays (#454) · **malicious photo RED-lights** on G1 (#452) + v6 pool (#456)
  · daemon-health self-provision (2 new rows) · capacity tripwire on the next weekly cycle · P2.6 manager smoke.
- **Cleanup:** delete orphan branch `feat/cs4b-vestigial-caps` on origin.

### Track 2 — Standing per-job trackers (P7 + M2) *(the largest net-new build)*
The design: **job = folder; weekly sheets = the per-week flow; standing per-job sheets = the running state.**
Build the cumulative, one-per-job (NOT per-week) Smartsheets, one-way-up mirrored from D1 (send/AI-free per §51;
period-split + archive-on-closure; find-or-create + capacity margin-check; never `delete_rows`):

> **Update 2026-07-09 — the P7/M2/M3 mirror suite is LANDED + live.** `progress_reports/equipment_status.py`,
> `material_list.py`, and `material_incidents.py` shipped (hours/equipment/materials/incidents FULLY LIVE per the
> field-ops program file). Slices 2/3 + M2/M3 below are BUILT and their OPEN DECISIONs were resolved in the build —
> retained here for the design record; each ships dark behind its `field_ops.fieldops_sync.*_enabled` gate.

- **P7 Slice 1 — Hours Log: LANDED + live-smoked (2026-07-04).** `progress_reports/hours_log.py` mirrors
  `time_entries` into a per-job standing `<Job> — Hours Log` sheet (PR #461); **archive-on-closure LANDED**
  (`smartsheet_client.move_sheet_to_folder` + the `fieldops_sync` archive hook — PR #465 / its#462) — the last §51
  guard. Live smoke GREEN (4 rows mirrored, idempotent, row-cap WARN — see
  `docs/audits/2026-07-04_smartsheet-wiring-audit.md` Appendix). Ships DARK: operator flips
  `field_ops.fieldops_sync.hours_enabled=true` (Workstream=field_ops) to go live.
- **P7 Slice 2 — Equipment Status & Location** (NEXT). **OPEN DECISION (confirm w/ Seth):** snapshot-vs-full-event
  depth — recommend a latest-location + readiness **snapshot** projection (one row/item, updated in place), NOT the
  accumulating-log shape (which changes the §51 guards: never-delete = retire-in-place + archive-on-closure;
  row-cap/period-split largely moot for a bounded snapshot).
- **P7 Slice 3 — Materials Status & Location.**
- **M2** — per-job **Material List** + bidirectional receive. **OPEN DECISION:** the landed table is
  `job_expected_materials` (0031); the mission specs a `material_list` (line_uuid/smartsheet_row_id/unplanned) that
  does NOT exist — recommend **EXTEND** the landed table (§14) with those 3 columns rather than adding a new table.
- **M3** — Material Incidents referencing a Material-List line + a fenced `portal_poll` photo deep-screen pass.
Design source: `progress-reporting/mission.md` §11–§13/§16.

### Track 3 — Scale-hardening for the 20×20 cutover
Most of the 14-row growth time-bomb table (`~/.claude/plans/unbounded-growth-audit.md`) is fixed (GS1 Check O /
sheet_capacity wiring, GS2 prune heartbeat + Check V, Sentry reclassification, D5 registry split). Remaining:
- Verify the **2 unverified Smartsheet quotas** (per-plan sheet cap; pooled attachment-storage quota) — one support ticket.
  **Update 2026-07-13:** `smartsheet.sheet_count_ceiling` + `_margin` ARE now present in `ITS_Config`
  (`Workstream=global`), seeded at the default `1500`/`50` (live read confirmed) — so the guard is no longer a
  SILENT hardcoded fallback (observable-config resolution now logs them from `ITS_Config`, forensic class #7
  closed for these two rows). Remaining: set the REAL plan cap once the actual Smartsheet plan sheet quota is
  known (the support-ticket item above).
- **meta-002 Tier-3 backup / escalation SLA** before the 20-job cutover (operator).
- ~~`REQUIRED_CONFIG` startup logging (#336)~~ **DONE** — implemented fleet-wide (`tests/test_required_config.py`;
  #336 CLOSED, though its GitHub title is actually "[P1] Hardening PR-6" — a citation mismatch; remaining adoption
  slice is `po_materials`/`subcontracts`). host-log prune (time-bomb #14) · watchdog hang-killer · confirm
  the installed plists' `RunAtLoad` is actually active · `brief-validator` scaffold-wiring (#341).

### Track 4 — Operator PDF documentation program (P1 / A8) — *delivery-critical subset by Aug 7*
Near-term scope = the **delivery-critical PDF set** of the Aug-7 program
(`docs/2026-07-09_aug7_delivery_program.md` WS3): the md→branded-PDF pipeline (`docs_pdf/` +
`scripts/build_docs_pdfs.py` + the §6a `docs/enablement/manifest.yaml`), **13 enablement PDFs as-built**
(the manifest is the source of truth: the 7 D2-1 guides + ITS Owner's Manual + safety-reports guide +
admin-dashboard guide + `ITS_Config` data dictionary + the **operator-dashboard** and **subcontracts** guides
added 2026-07-13), SHA-256 doc-currency check wired into CI (warn) + the cutover checklist. Full every-function A8 coverage
continues post-delivery on the same pipeline. *(Distinct from the internal CC-session context system — this is
operator-facing.)*

### Track 5 — Aug-7 Evergreen DELIVERY (production cutover + PO workstream + dashboard + docs)
**The umbrella for everything through 2026-08-07 — canonical program: `docs/2026-07-09_aug7_delivery_program.md`**
(decision register D1–D18, WS1 Purchase-Order generator slices S0–S8, WS2 operator dashboard, WS3 docs subset per
Track 4, WS4 host migration + tenant cutover + Aug-7 runbook, master calendar, risk register, Day-1 operator list).
Highlights: old-MBP production host provisioned Jul 10 / one-way flip Jul 13 / burn-in through the Jul 25–30 gap;
Phase-1.4 residue = Paid-plan-or-PBKDF2 verdict + WAF `/api/login` rate-limit + ClamAV/EICAR; tenant cutover Aug 3
(§53-gated via `scripts/verify_cutover.py`); dress rehearsals Aug 4–5; delivery + Step-8 acceptance Aug 7 (handover
v10 amendment: Tier-2 clearance moves post-delivery, D17). Attachment screening Layers 1-3 for *email* stays
Email-Triage-owned (unchanged).
WS4 operator artifacts (landed): `docs/operations/host_migration_runbook.md` · `cutover_checklist.md` v2 + `scripts/verify_cutover.py` (§53 gate) · `production_rollback.md` · `aug7_delivery_runbook.md`.
**WS2 operator dashboard — COMPLETE (2026-07-13).** All six completion blocks landed four-part clean: config-registry reconcile to the post-SC/PO surface (#567), D1-3b KeepAlive-service plist + interval-edit verb (#570), daemon-control + breaker-clear verbs + read-only send-queue panel (#574), Evergreen brand pass + audit-panel/lockout-UX/`/healthz` hardening (#576), and the activation kit + close-out. The six §44 actions are built; ships **DARK** pending the operator's one-time PIN + `tailscale_serve.sh` → plist-install activation (`docs/runbooks/operator_dashboard_config_editor.md` quick-start).

### Track 6 — Post-delivery: outage observability + estimate-lane determinism *(scoped 2026-08-07)*
Scoped the day of delivery, after a live diagnosis session found a **12.7-hour Smartsheet outage (2026-08-06,
breaker OPEN 729 min) that paged ZERO times**. Two code PRs + a docs pass; the debt-file half landed with this
entry. Working notes: `~/.claude/plans/zany-brewing-ritchie.md` (scratch — this Track is the durable record).

- **T6-1 — watchdog tiered cadence** *(fixes the blind spot; ordered first)*. The watchdog is the fleet's only
  cross-daemon observer and fires **once/day at 07:00**, so Check J WARNed at 450 s on the 11:00Z sweep and
  nothing looked again for twelve hours. Move to `StartInterval 3600` (precedent:
  `org.solutionsmith.its.picklist-sync.plist`). **Not** a plain plist edit — six checks are actively harmful
  hourly and must stay daily via a `DAILY_ONLY_CHECKS` **filter** (never a wrapper —
  `tests/test_watchdog.py:124` pins `CHECKS` by exact identity): **W** (unconditional launchd truncate lane →
  ~24 never-deleted `.gz`/log/day, inverting the growth bound it exists to enforce), **I** (a failing catch-up
  → ~72 weekly compiles + 72 *unrotatable* open CRITICALs), **D** (no cross-run dedupe → 24 Review-Queue
  rows/day), **O** (WARN-band writes a row into the very sheet it is warning about), **L** (creates+deletes a
  real Smartsheet sheet per run), **U** (security-relevant drift window 24 h → 1 h). **G** also stays daily
  until the Resend leg is fixed, or its retry loop pumps 24 WARN rows/day. Gate the daily tier on a
  `watchdog_daily.last_run` marker >20 h old, **not** `hour == 7` — a sleeping laptop must not skip a whole day.
  Tiering keeps `LOG_DIR_ROTATION_CRITICAL_THRESHOLD` and `LOG_ROTATION_TEMP_ORPHAN_AGE_SECONDS` semantically
  correct for free. Closes the "Lever 1" tech-debt entry, which predicted this exact fix.
- **T6-2 — deterministic `.xlsx` estimate tier**. A vendor's Excel quote is accepted, §34-screened and filed
  today, but `estimate_poll.py:1334-1335` (`if declared_mime != MIME_PDF: return None`) sits between the Tier-0
  branch and every extraction tier, so it can never be parsed — the office retypes every line from a file that
  already holds a typed numeric grid. Proven cheap: an adapter into the existing `ParsedPdf` contract let the
  **unmodified** `parse_generic_table` extract a vendor workbook (3/3 lines, integer cents, per-line math
  verified). Put the parse in `estimate_parse.py` (inherits its capability-gating enrollment); run the hostile
  parse in a new `estimate_sandbox.parse_xlsx_grid` child. **Read totals from CELLS via `to_cents`, never the
  `_GENERIC_TOTALS` regex** — it requires exactly two decimals, openpyxl returns `4685.0`, and an absent total
  makes `check_math` *skip* the comparison, so the doc posts `extracted` with no doc-level cross-check at all.
  `.xlsx` gets no preview, so imports ride the `no_preview_verified` acknowledgment — **do not** synthesize a
  preview from our own parse (it would make a wrong parse self-confirming). Per ADR-0004 E6 the gate flips true
  only after `scripts/eval_estimate_ladder.py --write-expectations` baselines the corpus; the fixture is empty
  today, so this finally qualifies **Tier 1** as well. Leaves Tier-2 (local Ollama) present but dark.
- **T6-3 — tech-debt file cleanup + handoff — LANDED 2026-08-07 (this pass).** `docs/tech_debt.md` was
  259,965 B, ~4 kB **over** its own 256 kB cap, and its 2026-07-14 triage index pointed at entries archived in
  July (19 of 110 bullets, over-counting open debt by ~17%; two were `[CUTOVER-BLOCKING]`-tagged). Four entries
  archived, six resolved sub-bullets swept, the index reconciled and re-counted (110 → 91), and the surviving
  `/api/recent` authorization gap re-filed under its own honest title.

---

## Backlog — parked with unblock conditions (not on the near path)
- **Canonical-Evergreen Smartsheet integration + PJOB→JOB reconciliation** — DEFERRED indefinitely; unblock =
  Seth gains read access to the canonical Evergreen schema. (ITS-owned SoR write-back is *not* blocked — §50/§51.)
- **Doctrine** §23/§24 seven-workspace topology text + any §-adds — Seth-owned, version-bump.
- **Future workstreams:** URS-Marine (Customer 2, active — briefs B1–B5); ~~Purchase Orders~~ → **promoted to
  Track 5** (Aug-7 program WS1; the RFQ stage remains future, but **Subcontracts is now BUILT** — SC-S1→S3c,
  ADR-0003, ships dark; send half SC-S4 unbuilt, operator scoped fully-in incl. send 2026-07-12); Email
  Triage (owns Invariant-2 Layer 6 — preserve the email code seed); AI Employee (Phase 3+; vector store → Phase 4).
- **Small feature / tech-debt:** publish rollback-UI picker; form-editor S1 per-item authoring; HTML email for
  weekly_send; time-entry personnel picker; finish `jobs.progress` %-removal (D1 column drop); `recipient_health`
  no-recipient severity (Seth); cosmetic tab-title/favicon still "ITS Portal"; `boxsdk`→`box_sdk_gen`;
  `build_wsr_human_review_sheet.py` ABSTRACT_DATETIME fresh-create bug; P2.5 `fieldops_sync` fast-follows (2 of 6).
- ~~**Verify (likely already built):** PR-6 Form-Request month filter (`/api/filed/months`); A5/Check-O
  row-cap rotation.~~ **CONFIRMED BUILT + CLOSED 2026-07-13:** `/api/filed/months` exists
  (`safety_portal/worker/index.ts:1127`); Check-O row-cap rotation exists (`scripts/watchdog.py`
  `_check_row_cap_rotation`) and covers BOTH `ITS_Errors` AND `ITS_Review_Queue` via the shared storm-mode
  helper (`_ROTATION_POLICIES`, #562). Nothing left to verify.
