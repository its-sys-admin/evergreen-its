# Session logs

Durable narrative record of Claude Code sessions that change the repo. Each log
captures the decisions made *during execution* — the context that doesn't
survive in commit messages, doesn't belong in the Claude.ai planning project,
and would otherwise have to be reconstructed by re-reading transcripts months
later.

## Why these exist

Three records describe the project, and each loses something the others keep:

- **Claude.ai planning project** — decisions *about* the system. Foundation
  Mission, Operational Standards, mission files, the Master Checklist. Stable,
  version-numbered, owner-facing.
- **Commit history** — what changed in the code. Atomic, machine-readable, but
  the *why* gets compressed into one or two paragraphs per commit.
- **Session logs** — decisions made *during* execution: which of two valid
  approaches got picked and the reasoning, what was deliberately left
  untouched, what was flagged for the planning project, what failed before
  succeeding. Bridges the planning project (system-level) and the commit
  history (code-level).

A session log is the answer to "why did 2026-05-17 land this particular set of
commits and not the obvious alternative." Re-reading a transcript six months
later is expensive; re-reading a 50-line log is cheap.

## When to write one

Write a session log when **both** are true:

1. The session lands ≥1 commit.
2. The session involved at least one non-obvious decision — a choice between
   valid alternatives, a deliberate carveout from a project rule, an item
   handed off to the planning project, or a diagnosis that didn't match the
   initial brief.

Don't write one for pure mechanical commits (typo fix, dependency bump,
formatting-only). The commit message is sufficient there.

If unsure: write it. The cost of an unnecessary log is one extra commit; the
cost of a missing log is reconstructing context from transcripts.

## Filename convention

`YYYY-MM-DD_short-slug.md` — date first so chronological sort works in any
file browser; slug short enough to read at a glance.

Examples:
- `2026-05-17_ruff_and_doc_refresh.md`
- `2026-05-20_safety_reports_intake_wiring.md`

If a single date has multiple distinct sessions, append `_2`, `_3`, etc.

## Section ordering

Every log uses this section order. Skip a section only if it would be empty.

1. **Date + session focus** — one sentence at the top, after the H1.
2. **Commits landed** — SHA, title, one-line purpose per commit.
3. **CI runs** — URL, duration, result per run.
4. **Decisions made during session** — one line each. Include the alternative
   that was rejected and the reasoning, not just the choice.
5. **Open items handed off** — anything flagged for the planning project's
   Master Checklist, future sessions, or external systems. Include the
   suggested wording when possible so the recipient can copy-paste.
6. **What was NOT touched** — explicit list. The negative space matters: it
   documents that the omissions were deliberate, not oversights.
7. **Lessons captured to memory** — which memory files were updated and what
   the takeaway was. Cross-references the persistent-memory system so future
   sessions can find the rule even without re-reading this log.

## Planning project vs. session log — what goes where

**Planning project (Claude.ai)** — decisions about the system itself:
canonical doc versions, invariants, architectural choices, workstream missions,
Master Checklist items, schemas, the things that outlive any one session.

**Session log (this directory)** — decisions made during a session, including
the ones that *didn't* reach the planning project: which lint rule to suppress,
which precedent to mirror, why option (b) beat option (a) on a doc link, what
the regex bug was that caused a tally miss. If the decision is about a
specific commit or set of commits, it lives here.

If a session-log decision turns out to be load-bearing for the system
(e.g., "we now have a session-log convention"), the next session that has a
natural reason to touch the planning surface (CLAUDE.md, an Op Std, a mission
file) carries the decision up. Session logs are the staging area; the
planning project is canonical.

## Auto-generated index

Populated by `scripts/regen_doc_indexes.py`. Operator-edited prose lives
outside the sentinel block.

<!-- BEGIN AUTO-INDEX -->
| Date | Type | Status | Workstream | Title | PRs |
|------|------|--------|------------|-------|-----|
| 2026-08-10 | session_log | active | field_ops | [Session log — 2026-08-10/11 overnight · Materials stand-up on the production host, the 1135 live failure, and the unification pass](2026-08-10_overnight-materials-standup-and-reconcile.md) | _–_ |
| 2026-08-03 | session_log | active | field_ops | [Session log — 2026-08-02 → 08-03 · Repo reconcile, a real local dev environment, and Track 6 (job-archive workflow) PR-0 through PR-5](2026-08-03_track6-job-archive-workflow-pr0-through-pr5.md) | #712, #713, #715, #716, #717, #718, #719, #720, #721, #722 |
| 2026-07-14 | session_log | active | _–_ | [2026-07-14 — Debt-Zero + Security-Scrub](2026-07-14_debt-zero-and-security-scrub.md) | _–_ |
| 2026-07-12 | session_log | active | subcontracts | [Subcontract generator COMPLETE — SC-S3c (backend) + SC-S5 (UI) + SC-S3b Exhibit A](2026-07-12_subcontract-generator-complete-s3c-s5-exhibit-a.md) | #536, #537, #538 |
| 2026-07-11 | session_log | active | subcontracts | [Subcontract generation workstream — SC-S1 foundation (deterministic, PO-mirror)](2026-07-11_subcontracts-s1-foundation.md) | #529 |
| 2026-07-11 | session_log | active | subcontracts | [Subcontract generation — SC-S3a render core + SC-S3b editable .docx/.xlsx + group-by-state](2026-07-11_subcontracts-s3a-s3b-render-and-state.md) | #532, #533, #534 |
| 2026-07-10 | session_log | active | _–_ | [Cutover readiness gap — CL-01…CL-33 disposition + verify_cutover baseline (2026-07-10)](2026-07-10_cutover-gap.md) | _–_ |
| 2026-07-10 | session_log | active | _–_ | [Overnight (autonomous): §50 config-editor Features 1 & 2 + Aug-7 cutover readiness](2026-07-10_overnight-config-editor-features-and-cutover-readiness.md) | #524, #525, #526 |
| 2026-06-10 | session_log | active | safety_portal | [Session — Agent Optimization (Brief 2) + Safety Portal Hardening (Brief 1)](2026-06-10_agent-optimization-and-portal-hardening.md) | #260, #261, #263, #264, #265, #266 |
| 2026-08-25 | session_log | closed | field_ops | [Session log — 2026-08-24 → 2026-08-25 · Henry's phantom receipt, a continuation row that duplicated a line, and the audit that caught its own migration header](2026-08-25_materials-receipt-forensics-and-continuation-rows.md) | #188, #189, #190 |
| 2026-08-20 | session_log | closed | safety_portal | [Session log — 2026-08-20 · Two operator-requested portal features: WPR material-list curation and resizable review grids (both deployed live)](2026-08-20_wpr-materials-curation-and-grid-viewport.md) | #185, #186 |
| 2026-08-19 | session_log | closed | safety_portal | [Session log — 2026-08-19 · Two failed form publishes: a test that counted across sections, and a CI gate that read its own cancelled run as failure](2026-08-19_erosion-form-publish-unblock.md) | #180, #181 |
| 2026-08-17 | session_log | closed | ci | [Session log — 2026-08-17 · Executing the 2026-08-16 forensic audit's exec-side brief: fail-closed guards, an armed dead-man's switch, and the quality ratchet](2026-08-17_audit-remediation-and-quality-ratchet.md) | #154, #155, #156, #157, #158, #160, #161 |
| 2026-08-15 | session_log | closed | _–_ | [Session log — 2026-08-15 · Change orders as full generated documents (Track D2)](2026-08-15_change-orders-as-documents.md) | #148, #149 |
| 2026-08-15 | session_log | closed | _–_ | [Session log — 2026-08-15 · Design-completion pass, operator decisions Q1–Q5, dashboard wiring](2026-08-15_design-completion-and-operator-decisions.md) | #151 |
| 2026-08-13 | session_log | closed | _–_ | [Session log — 2026-08-13 · A tech-debt sweep, dashboard wiring for the new lanes, and finishing the repo-identity migration](2026-08-13_debt-sweep-and-dashboard-wiring.md) | #122 |
| 2026-08-13 | session_log | closed | _–_ | [Session log — 2026-08-13 → 2026-08-14 · Job Tracker unification + WPR program](2026-08-13_job-tracker-unification-and-wpr-program.md) | #131, #132, #133, #134, #135, #136, #137, #138, #139, #140, #141, #142, #143 |
| 2026-08-12 | session_log | closed | field_ops | [Session log — 2026-08-11 → 2026-08-12 · ADR-0006 Job Schedule / Progress + Payment tracking — full lane, planned through live E2E](2026-08-12_adr0006-schedule-payment-tracking-full-lane.md) | #80, #84, #85, #90, #91, #92, #93 |
| 2026-08-12 | session_log | closed | field_ops | [Session log — 2026-08-11 → 2026-08-12 · Box-root splits (PO, subcontracts), dashboard validity wiring, and the all-lanes audit that closed 24 gaps](2026-08-12_box-root-splits-dashboard-validity-and-wiring-audit.md) | #79, #98, #104, #112 |
| 2026-08-12 | session_log | closed | safety_portal | [Session log — 2026-08-12 evening · Job-detail design pass, part three — the two-column shell, three real bugs, and a verifier that withheld its verdict](2026-08-12_job-detail-design-pass-part-three.md) | #119 |
| 2026-08-12 | session_log | closed | safety_portal | [Session log — 2026-08-12 · A frontend design program on the Safety Portal SPA — the phantom-CSS bug family, and four PRs deployed live](2026-08-12_safety-portal-frontend-design-program.md) | #99, #103, #109, #114 |
| 2026-08-12 | session_log | closed | _–_ | [2026-08-12 — Tech-debt triage against live HEAD, then building the actionable half](2026-08-12_tech-debt-triage-and-build.md) | #105, #106, #107, #108, #110, #111 |
| 2026-08-12 | session_log | closed | progress_reports | [Session log — 2026-08-12 · The report reads the schedule, the lane's debt clears, and one regression I shipped](2026-08-12_weekly-production-report-schedule-and-debt.md) | #97, #100, #101 |
| 2026-08-11 | session_log | closed | field_ops | [Session log — 2026-08-10 → 2026-08-11 · Archive follow-ups: the restore drill, six PRs, a deploy, and a tech-debt trim under cap](2026-08-11_archive-followups-deploy-and-tech-debt-trim.md) | #43, #49, #52, #56, #57, #65 |
| 2026-08-11 | session_log | closed | field_ops | [Session log — 2026-08-11 → 2026-08-12 · The Evergreen job-identifier ask that uncovered twelve validators, a silent-corruption bug, a client-dedupe defect, an equipment-catalog reconciliation, and procurement site propagation (five PRs)](2026-08-11_job-identifier-site-phase-and-procurement-propagation.md) | #69, #73, #75, #78, #86 |
| 2026-08-11 | session_log | closed | field_ops | [Session log — 2026-08-10 → 2026-08-11 · Manifest finish, the first real documents, and four same-day operator decisions](2026-08-11_materials-live-fire-manifest-finish-and-operator-decisions.md) | #59, #60, #61, #62, #63, #66, #71, #72, #74 |
| 2026-08-11 | session_log | closed | progress_reports | [Session log — 2026-08-11 · The Weekly Production Report: the progress lane gets a client-facing artifact](2026-08-11_weekly-production-report.md) | #81, #82, #83, #87, #88, #89 |
| 2026-08-10 | session_log | closed | field_ops | [Session log — 2026-08-10 · The archive button diagnosed, the missing gate row found, and the first attended Box drill](2026-08-10_archive-button-diagnosis-and-live-drill.md) | #33 |
| 2026-08-10 | session_log | closed | infrastructure | [Session log — 2026-08-06 → 2026-08-10 · Outage diagnosis, the second alerting-gap occurrence, External Send Gate activation across procurement, and five merged PRs](2026-08-10_outage-diagnosis-alerting-gap-and-gate-activation.md) | #11, #12, #13, #14, #16 |
| 2026-08-10 | session_log | closed | field_ops | [Session log — 2026-08-10 · PR4 materials/delivery workflow completion (five PRs, four-part clean)](2026-08-10_pr4-materials-delivery-workflow-completion.md) | #38, #40, #45, #48, #50 |
| 2026-08-10 | session_log | closed | safety_portal | [Session log — 2026-08-10 · Pre-op inspection forms, GAYK transport/maintenance checklists, and a training waiver](2026-08-10_preop-inspection-forms-and-checklists.md) | #44, #47, #51 |
| 2026-08-07 | session_log | closed | field_ops | [Session log — 2026-08-07 · Manifest import activation on the production Mac (and the precondition that was waived)](2026-08-07_manifest-import-activation.md) | _–_ |
| 2026-08-07 | session_log | closed | field_ops | [Session log — 2026-08-07 · Manifest import transport (PR3b): the pool, the daemon, and the paged commit](2026-08-07_manifest-import-transport-pr3b.md) | #727, #729, #732, #733, #734 |
| 2026-08-07 | session_log | closed | field_ops | [Session log — 2026-08-06 → 08-07 · Materials-tracking program: back-button contrast fix, per-job Materials page (migration 0059), and the BOM/shipping-log manifest parser](2026-08-07_materials-tracking-back-button-page-manifest-parser.md) | #724, #725, #727 |
| 2026-08-07 | session_log | closed | field_ops | [Session log — 2026-08-07 · Track 6 completion: the Box leg, the un-archive leg, the archive button, the drain, and the deploy](2026-08-07_track6-job-archive-completion-and-deploy.md) | #726, #728, #18, #20, #21 |
| 2026-08-02 | session_log | closed | infrastructure | [Session log — 2026-08-02 · Repo reconcile, a real local dev environment, and the archive-workflow plan](2026-08-02_repo-reconcile-and-local-dev-environment.md) | #712, #713 |
| 2026-07-29 | session_log | closed | _–_ | [2026-07-26 → 2026-07-29 — Documentation consolidation: six themed PRs against a 172-finding audit](2026-07-29_documentation-consolidation.md) | #2, #3, #4, #5, #6, #7 |
| 2026-07-26 | session_log | closed | infrastructure | [Session log — 2026-07-25 → 2026-07-26 · Production-host migration Phase 1: dev-box teardown + new production-host stand-up](2026-07-26_production-host-migration-phase1.md) | _–_ |
| 2026-07-26 | session_log | closed | infrastructure | [2026-07-25 → 2026-07-26 — ITS production host stand-up (Florida MacBook), fleet live at 15 daemons](2026-07-26_production-host-standup.md) | _–_ |
| 2026-07-24 | session_log | closed | operator_dashboard | [2026-07-24 — The three M365 / Graph secrets become dashboard-rotatable (Developer-Operator self-service), detection gap logged](2026-07-24_m365-secrets-dashboard-rotatable.md) | #705, #707 |
| 2026-07-23 | session_log | closed | _–_ | [2026-07-23 — Brief B: project-archive path (§51 closure) — verification + doc deliverables](2026-07-23_archive-path-closure-docs.md) | #678 |
| 2026-07-23 | session_log | closed | _–_ | [2026-07-23 — Second-generation document polish across all 10 rendered deliverable types](2026-07-23_pdf-polish-second-gen.md) | #693 |
| 2026-07-23 | session_log | closed | infrastructure | [2026-07-22 → 2026-07-23 — Full tenant wipe + orchestrated stand-up rehearsal (production-cutover dry run)](2026-07-23_tenant-wipe-standup-rehearsal.md) | #664, #665, #666, #667, #668, #669, #670, #671, #672, #674 |
| 2026-07-22 | session_log | closed | infrastructure | [2026-07-21 → 2026-07-22 — Phase-1 pre-builder-family gap migrations + the two-stage `~/its/logs` growth fix (Check W)](2026-07-22_cutover-migration-builders-and-log-growth-fixes.md) | #649, #650, #651 |
| 2026-07-22 | session_log | closed | operator_dashboard | [2026-07-22 — Cutover-day-1 CC items + operator-dashboard refinement pass (autonomous afternoon)](2026-07-22_dashboard-refinement-and-cutover-day1.md) | _–_ |
| 2026-07-21 | session_log | closed | infrastructure | [2026-07-19 → 2026-07-21 — Dashboard RFQ representation, the config-read-vs-credential fix, hermetic test isolation, and an 8-area coverage-gap hunt](2026-07-21_dashboard-rep-config-read-fix-hermetic-tests-coverage-gap-hunt.md) | #627, #628, #637, #638, #639, #640, #641, #642 |
| 2026-07-20 | session_log | closed | po_materials | [2026-07-20 — Fold RFQs + Vendor Estimates into a Purchase-Orders tab hub (#629)](2026-07-20_po-hub-tab-fold.md) | #629 |
| 2026-07-19 | session_log | closed | operator_dashboard | [2026-07-19 — Error-hygiene forensic survey + dashboard system-map build (DASH-12/DASH-13)](2026-07-19_error-hygiene-and-dashboard-system-map.md) | #613, #614 |
| 2026-07-19 | session_log | closed | po_materials | [2026-07-19 — RFQ / vendor-estimate lane: design → build (E1–E6 / R1–R4) → mirror go-live](2026-07-19_rfq-estimate-lane-build-and-mirror-golive.md) | #618, #620, #621, #623, #624, #625 |
| 2026-07-17 | session_log | closed | infrastructure | [2026-07-17 — Error-flood triage + dashboard hardening](2026-07-17_error-triage-and-dashboard-hardening.md) | #608, #609 |
| 2026-07-17 | session_log | closed | subcontracts | [Session — config_actuator fail-soft, dashboard mark-errors-resolved verb, SC-S4 subcontract send lane, and PO + subcontract external-send LIVE (2026-07-14 → 2026-07-17)](2026-07-17_mark-resolved-subcontract-send-lane-and-external-send-go-live.md) | #596, #597, #599 |
| 2026-07-15 | session_log | closed | docs | [2026-07-15 — Documentation corpus, Tranches B–E (tree · dashboard · currency · distribution)](2026-07-15_docs-corpus-program-b-through-e.md) | #601, #602, #603, #604 |
| 2026-07-15 | session_log | closed | docs | [2026-07-15 — Documentation corpus, Tranche A (Tier-1 system references)](2026-07-15_docs-corpus-tranche-a-tier1-references.md) | #598 |
| 2026-07-14 | session_log | closed | _–_ | [Session — Dashboard back-nav, ITS_Config seed, clear-error-log verb, and the out-of-band alerting gap](2026-07-14_dashboard-backnav-configseed-errorclear-alerting-gap.md) | #587, #591, #594 |
| 2026-07-13 | session_log | closed | _–_ | [Session — Autonomous delivery + readiness + tech-debt burn (2026-07-13, unattended)](2026-07-13_autonomous-delivery-readiness-and-techdebt-burn.md) | #568, #569, #571, #572, #573 |
| 2026-07-13 | session_log | closed | _–_ | [2026-07-13 — exec-side of the Op Stds v21 doctrine elevation (#551 / #553 / #555)](2026-07-13_doctrine-elevation-v21-exec.md) | #551, #553, #555 |
| 2026-07-13 | session_log | closed | _–_ | [Session — ITS_Errors row-cap incident (Check O storm-mode fix) + per-job tracking / PO document attachments / delivery-contact autofill (PRs #562, #563, #564, #566)](2026-07-13_row-cap-incident-perjob-tracking-attachments-delivery-contact.md) | #562, #563, #564, #566 |
| 2026-07-13 | session_log | closed | _–_ | [Session — WS2 Operator Dashboard COMPLETION (Blocks 1-6, PRs #567/#570/#574/#576)](2026-07-13_ws2-operator-dashboard-completion.md) | #567, #570, #574, #576 |
| 2026-07-12 | session_log | closed | docs | [Documentation reconciliation — reconcile every doc to the as-built system (6 WPs)](2026-07-12_documentation-reconciliation.md) | #543, #545, #547, #549 |
| 2026-07-12 | session_log | closed | subcontracts | [Session — Office Operations nav + PO/SC Configuration subcontract editors (PRs #541/#542/#546 landed; #544/#548 held for operator; PR-B2 mapped, not started)](2026-07-12_office-ops-nav-and-po-sc-config.md) | #541, #542, #546, #544, #548 |
| 2026-07-10 | session_log | closed | _–_ | [Session — Config-editor follow-ups (fix the self-defeating CI test, CE-3) + terms editing (Step 2, slices T1/T2)](2026-07-10_config-editor-followups-terms-editing.md) | #514, #511, #518, #520 |
| 2026-07-10 | session_log | closed | _–_ | [Session — PO follow-ups (ship-to / catalog / config-view / naming) + the generic §50 config-editor 3-slice vertical (PRs #504–#512, #511 stuck)](2026-07-10_po-followups-and-config-editor-vertical.md) | #504, #505, #506, #507, #508, #509, #510, #511, #512 |
| 2026-07-10 | session_log | closed | _–_ | [Session — WS2 Operator Dashboard: D1-1 read-only core + D1-2 Class-A config editor + D1-3 sensitive tier (PRs #516, #519, #523)](2026-07-10_ws2-operator-dashboard-d1-1-d1-2-d1-3.md) | #516, #519, #523 |
| 2026-07-09 | session_log | closed | field_ops | [Session — field-ops mirror-suite full activation + items 1–4 close-out batch + §54 secret-leak backstop](2026-07-09_fieldops-activation-plus-items-1-4-closeout-and-section54.md) | #487, #488, #489 |
| 2026-07-06 | session_log | closed | field_ops | [Session — M3 Slice 2 Material Incidents ledger: build → deploy → activate (PR #483), plus close-out prep](2026-07-06_m3-slice2-material-incidents-build-deploy-activate.md) | #483 |
| 2026-07-04 | session_log | closed | progress_reporting | [Session — Progress-Reporting go-live (Track 0) + P7 Slice 1 Hours Log up-sync held on §51, cleared via v19.x rider (exec #459/#461/#463, blueprint #58)](2026-07-04_progress-golive-p7-hours-log-and-s51-rider.md) | #459, #461, #463 |
| 2026-07-03 | session_log | closed | field_ops | [Session — Complete-state hardening + unbounded-growth audit + design-table execution + live-report fixes (PRs #434–#456, blueprint #55)](2026-07-03_complete-state-growth-audit-design-table.md) | #405, #434, #435, #436, #437, #438, #439, #440, #442, #443, #415, #444, #445, #446, #447, #448, #449, #450, #451, #452, #453, #454, #455, #456 |
| 2026-07-03 | session_log | closed | field_ops | [Session — SOP-Daily-Form redesign + Material Receipts + design-refinement + optimization sweep (PRs #423–#432)](2026-07-03_sop-daily-form-material-receipts-and-optimization-sweep.md) | #423, #424, #425, #426, #427, #428, #429, #430, #431, #432 |
| 2026-07-02 | session_log | closed | field_ops | [Session — Assigned-Tasks feature (S1–S6+T) + R-series UX refinement program (PRs #406–#421, #415 held)](2026-07-02_assigned-tasks-and-r-series-refinement.md) | #406, #407, #408, #409, #410, #411, #412, #413, #414, #415, #416, #417, #418, #419, #420, #421 |
| 2026-07-01 | session_log | closed | field_ops | [Session — P2.5 cutover LIVE + P2.6 Manager tier + FF4/FF5 daemon hardening (PRs #395–#400)](2026-07-01_manager-tier-ff4-ff5-cutover.md) | #395, #396, #397, #398, #399, #400 |
| 2026-07-01 | session_log | closed | field_ops | [Session — P2.5 job up-sync CUT OVER LIVE + P6 rollup + concurrency audit (PRs #391–#393)](2026-07-01_p2.5-cutover-live-p6-rollup.md) | #391, #392, #393 |
| 2026-06-30 | session_log | closed | field_ops | [Session — P2.5 job-tracker → Smartsheet up-sync (PRs #383, #384, #385, #386, #387)](2026-06-30_p2.5-job-tracker-upsync.md) | #383, #384, #385, #386, #387 |
| 2026-06-30 | session_log | closed | field_ops | [Session — P2.5 Slice 6: portal-owned canonical job number (PR #389)](2026-06-30_p2.5-slice6-portal-number.md) | #389 |
| 2026-06-30 | session_log | closed | progress_reporting | [Session — P4 progress compile engine + form-builder workflow selector (PRs #372, #373, #375, #376)](2026-06-30_p4-progress-compile-and-workflow-selector.md) | #372, #373, #375, #376 |
| 2026-06-30 | session_log | closed | progress_reporting | [Session — P5 progress SEND half + operability guards (PRs #379, #380, #381)](2026-06-30_p5-progress-send-and-operability-guards.md) | #379, #380, #381 |
| 2026-06-30 | session_log | closed | progress_reporting | [Session — Stage 1 parameterize complete (PRs #353–#359)](2026-06-30_stage1-parameterize-complete.md) | #353, #354, #355, #356, #359 |
| 2026-06-30 | session_log | closed | infrastructure | [Session — Tech-debt cleanup alongside in-flight Phase-2 (PRs #363–#368)](2026-06-30_tech-debt-cleanup-alongside-phase2.md) | #363, #364, #365, #366, #367, #368 |
| 2026-06-29 | session_log | closed | field_ops | [Session — Tier-A Stage-0 foundation + Personnel CRUD landed (PRs #329, #344, #345, #346, #349)](2026-06-29_field-ops-progress-stage0-foundation-landed.md) | #329, #344, #345, #346, #349 |
| 2026-06-29 | session_log | closed | infrastructure | [Session — Forensic lessons-learned retrospective → 6 standards-hardening PRs](2026-06-29_forensic-lessons-learned-hardening.md) | #330, #342, #343, #347, #348, #350, #351, #352 |
| 2026-06-29 | session_log | closed | infrastructure | [Session — Op Stds v18→v19 (§50 code-actuation / §51 SoR write-back) + job-tracker pivot planning (P2.5) + Manager-tier folding (P2.6)](2026-06-29_opstds-v19-doctrine-and-jobtracker-pivot.md) | #358 |
| 2026-06-28 | session_log | closed | safety_portal | [Session — Field-Ops Progress-Reporting program design + Stage-0 slices (M1/P-A1/A2) + live lockout fix](2026-06-28_field-ops-progress-reporting-stage0_2.md) | #325, #326, #327, #328 |
| 2026-06-28 | session_log | closed | safety_portal | [Session — Field-Ops write-UI phase complete (Slices 3–4, PRs #321–#322)](2026-06-28_field-ops-write-ui-phase.md) | #321, #322 |
| 2026-06-28 | session_log | closed | _–_ | [Session — Forensic scaling evaluation to 20 jobs / 20+ daily users (read-only; no commits)](2026-06-28_forensic-scaling-eval-20x20.md) | _–_ |
| 2026-06-27 | session_log | closed | safety_portal | [Session — Field-Ops P2.2 read views (Personnel / Equipment / Job Tracker), A/B/C landed](2026-06-27_field-ops-p2.2-read-views.md) | #308, #309, #310 |
| 2026-06-27 | session_log | closed | safety_portal | [Session — Field-Ops P2.3 write routes (job/task/time/equipment CRUD), 6 slices landed](2026-06-27_field-ops-p2.3-write-routes.md) | #312, #313, #314, #315, #316, #317 |
| 2026-06-20 | session_log | closed | safety_portal | [Session — feat/fix(safety-portal): banner wordmark rebrand — drop PNG lockup, render live gold-script "Integrated Technical System"](2026-06-20_safety-portal-banner-wordmark.md) | #297, #298, #299, #300 |
| 2026-06-18 | session_log | closed | safety_portal | [Session — D1 job cleanup + clean-slate purge + tech-debt easy-wins (later arc, 2026-06-17 evening → 2026-06-18)](2026-06-18_d1-job-cleanup-and-tech-debt-easy-wins.md) | #292, #294, #295 |
| 2026-06-17 | session_log | closed | safety_portal | [Session — Safety Portal test-artifact cleanup (live API) + daily/weekly PDF naming scheme (PRs #289 / #290)](2026-06-17_safety-portal-test-artifact-cleanup-and-pdf-naming.md) | #289, #290 |
| 2026-06-15 | session_log | closed | safety_reports | [Session — feat(safety-reports): Safety Portal PDF beautification — Evergreen logo, gold section rules, branded weekly packet](2026-06-15_pdf-beautification-evergreen-logo-gold-rules.md) | #287 |
| 2026-06-14 | session_log | closed | safety_portal | [Session — feat(safety-portal): rebrand "Safety Portal" → "ITS Portal" (maximalist header, Phase A / B2)](2026-06-14_its-portal-rebrand.md) | #285 |
| 2026-06-13 | session_log | closed | safety_portal | [Session — fix(safety-portal): bound week-sheet name to Smartsheet's 50-char cap + drain permanent 400s](2026-06-13_week-sheet-50-char-cap-drain-permanent-400s.md) | #283 |
| 2026-06-12 | session_log | closed | safety_portal | [Session — PR-5 Form Request + PR-3 Graph upload-session merged; local tree cleanup](2026-06-12_pr5-form-request-pr3-graph-upload-tree-cleanup.md) | #275, #276 |
| 2026-06-10 | session_log | closed | safety_portal | [Session log — Admin idle timeout widened to 30 min + bounded dirty-editor keep-alive (PR #258)](2026-06-10_admin-idle-timeout-bounded-keep-alive.md) | #258 |
| 2026-06-09 | session_log | closed | safety_portal | [Session log — Form Editor UX fixes + per-account draft cache + live SPA deploy (PRs #249–#250)](2026-06-09_form-editor-ux-draft-cache.md) | #249, #250 |
| 2026-06-09 | session_log | closed | safety_portal | [Session log — Part B: Compile Now poller + Part C: Orphaned Reports reroute (PRs #232–#235)](2026-06-09_part-b-compile-now-part-c-orphaned-reports.md) | #232, #233, #234, #235 |
| 2026-06-09 | session_log | closed | safety_portal | [Session log — Publish CI gate hardening + idle self-heal + Part A production hardening (PRs #222, #224, #227, #228, #230)](2026-06-09_publish-ci-gate-hardening-and-part-a.md) | #222, #224, #227, #228, #230 |
| 2026-06-09 | session_log | closed | safety_portal | [Session log — Publish pipeline bugfix chain + WSR Approved At / Sent At datetime (PRs #236, #241–#242, #244–#245)](2026-06-09_publish-pipeline-bugfix-chain-and-wsr-datetime.md) | #236, #241, #242, #244, #245 |
| 2026-06-09 | session_log | closed | safety_portal | [Session log — Safety Portal Phase 2: Form Manager + automated publish pipeline (PRs #203–#218)](2026-06-09_safety-portal-phase2-form-manager-publish-pipeline.md) | #203, #204, #205, #206, #207, #208, #209, #210, #211, #212, #213, #214, #215, #216, #217, #218 |
| 2026-06-09 | session_log | closed | safety_reports | [Session log — weekly_send hardening (audit H2/M3/M8) + append-only compile + picklist regression + incident-report E2E (PRs #247–#248, #252–#255)](2026-06-09_weekly-send-hardening-audit-findings-incident-report-e2e.md) | #247, #248, #252, #253, #254, #255 |
| 2026-06-08 | session_log | closed | safety_portal | [Session log — Safety Portal Admin Dashboard (Phase 1) + security audit + post-audit hardening](2026-06-08_admin-dashboard-audit-and-security-hardening.md) | #193, #194, #195, #197 |
| 2026-06-08 | session_log | closed | safety_portal | [Session log — Safety Portal mirror activation (end-to-end live proof on evergreenmirror.com)](2026-06-08_safety-portal-mirror-activation.md) | #185 |
| 2026-06-07 | session_log | closed | safety_portal | [Session log — Safety Portal Phase 7: styling, Box schema, custom domain (PRs #186–#189 + #185 open)](2026-06-07_safety-portal-phase7-styling-box-schema.md) | #185, #186, #187, #188, #189 |
| 2026-06-05 | session_log | closed | safety_portal | [Session log — Safety Portal Phase 3: contacts amendment (contact routing columns)](2026-06-05_safety-portal-phase3-contacts-amendment.md) | #162 |
| 2026-06-05 | session_log | closed | safety_portal | [Session log — Safety Portal Phase 3: job model + live Job-ID resolution](2026-06-05_safety-portal-phase3-job-model.md) | #160 |
| 2026-06-05 | session_log | closed | safety_portal | [Session log — Safety Portal Phase 4 PR 1: forms foundation](2026-06-05_safety-portal-phase4-pr1-forms-foundation.md) | #164 |
| 2026-06-05 | session_log | closed | safety_portal | [Session log — Safety Portal Phase 4 PR 2 → Phase 5 PR 2: display runtime, PDF renderer, WSR foundation, transport queue](2026-06-05_safety-portal-phase4-runtime-renderer-phase5-foundation-transport.md) | #166, #167, #168, #169 |
| 2026-06-05 | session_log | closed | safety_portal | [Session log — Safety Portal WSR rewire (Phase-5 Python pull model, PRs #173–#176)](2026-06-05_safety-portal-wsr-rewire-pull-model.md) | #173, #174, #175, #176 |
| 2026-06-04 | session_log | closed | safety_portal | [Session log — Safety Portal Phase 2: Cloudflare scaffolding + minimal portal](2026-06-04_safety-portal-phase2-cloudflare-scaffold.md) | #158 |
| 2026-06-03 | session_log | closed | infrastructure | [Session log — Phase 3a/3b decisions + E1 cutover (continuation of 2026-06-02)](2026-06-03_phase3a-3b-e1-cutover.md) | #151, #152, #153 |
| 2026-06-03 | session_log | closed | safety_portal | [Session log — Safety Portal config sheets + unifying alignment audit](2026-06-03_safety-portal-config-sheets-and-alignment-audit.md) | #155, #156 |
| 2026-06-02 | session_log | closed | infrastructure | [Session log — E1 project-routing migration + first picklist-drift reconcile](2026-06-02_e1-and-picklist-drift-reconcile.md) | #149, #150 |
| 2026-06-01 | session_log | closed | infrastructure | [Session log — Tier-1 self-heal: weekly_generate catch-up (Check I)](2026-06-01_tier1-self-heal-checkI-catchup.md) | #133 |
| 2026-05-29 | session_log | closed | docs | [2026-05-29 — Exec-side ledger cleanup: FM v8→v9 + Op Stds v13→v14 doctrine bump](2026-05-29_exec-ledger-cleanup.md) | #125 |
| 2026-05-29 | session_log | closed | security | [2026-05-29 — F02 (network-capability allowlist) + F22 (approval-attestation verification)](2026-05-29_f02-f22-capability-approval.md) | #118 |
| 2026-05-29 | session_log | closed | safety_reports | [2026-05-29 — F20: schema-version enforcement in `weekly_generate._load_tool_schema`](2026-05-29_f20-schema-version.md) | #129 |
| 2026-05-29 | session_log | closed | ci | [2026-05-29 — Integration tests silently broken by autouse keychain stub; token-leak redaction](2026-05-29_integration-keychain-stub-fix.md) | #123 |
| 2026-05-29 | session_log | closed | docs | [2026-05-29 — OBS-1: CLAUDE.md + README.md citation sweep (Op Stds v13→v14 / FM v8→v9)](2026-05-29_obs1-citation-sweep.md) | #127 |
| 2026-05-29 | session_log | closed | infrastructure | [2026-05-29 — Worktree-isolation fix + agent/workflow optimization audit](2026-05-29_worktree-discipline-and-audit.md) | #121 |
| 2026-05-28 | session_log | closed | infrastructure | [2026-05-28 — Agent-infrastructure follow-ons: session-close-maintainer staleness guard + agent-skills config landed](2026-05-28_agent-infra-followons.md) | #110, #111 |
| 2026-05-28 | session_log | closed | infrastructure | [2026-05-28 — `shared/alert_dedupe.py` → `state_io` migration (PR 2 of Phase 1.4 hardening cluster)](2026-05-28_alert-dedupe-state-io-migration.md) | #104, #88 |
| 2026-05-28 | session_log | closed | docs | [2026-05-28 — Doc-reconciliation: doctrine-version drift + canonical manifest + reconciliation agent](2026-05-28_doc-reconciliation.md) | #101, #103, #106 |
| 2026-05-28 | session_log | closed | infrastructure | [2026-05-28 — F16: wire the external heartbeat ping (Option A)](2026-05-28_f16-heartbeat-ping.md) | #114 |
| 2026-05-28 | session_log | closed | security | [2026-05-28 — Phase 1.4 sweep: F17 (intake_poll watchdog tracking) + F04 (keychain stdin write) + watchdog docstring drift](2026-05-28_f17-f04-docstring-sweep.md) | #113 |
| 2026-05-28 | session_log | closed | security | [2026-05-28 — Forensic-audit remediation (HIGH-1 injection fix + LOW hygiene + HIGH-2 surfacing)](2026-05-28_forensic-audit-remediation.md) | #95, #96 |
| 2026-05-28 | session_log | closed | security | [2026-05-28 — Portal-pivot reconciliation + HIGH-2 supersession](2026-05-28_portal-pivot-reconciliation.md) | #98, #99, #100 |
| 2026-05-28 | session_log | closed | _–_ | [2026-05-28 — Subagent hardening (codeql propose-only + delegation/path/token fixes)](2026-05-28_subagent-hardening.md) | #92, #93 |
| 2026-05-24 | session_log | closed | docs | [2026-05-24 — CC file-based memory consolidation](2026-05-24_cc-memory-consolidation.md) | _–_ |
| 2026-05-24 | session_log | closed | docs | [2026-05-24 — Markdown doc conventions + index generation + lint](2026-05-24_doc_conventions.md) | _–_ |
| 2026-05-24 | session_log | closed | docs | [2026-05-24 — Execution-repo doctrine version drift cleanup](2026-05-24_doctrine-version-drift-cleanup.md) | _–_ |
| 2026-07-23 | session_log | complete | infrastructure | [Session log — 2026-07-23 · Stand-up process optimization (Brief A)](2026-07-23_standup-process-optimization.md) | #673, #675, #676, #679, #680, #683, #685, #686, #687 |
| 2026-07-23 | session_log | complete | infrastructure | [Session log — 2026-07-23 · Independent verify pass over Briefs A/B + tail + drill prep](2026-07-23_verify-pass-tail-and-drill.md) | #690, #691 |
| 2026-07-13 | _–_ | _–_ | _–_ | [2026-07-13 — PO/SC config + subcontract/PO builder hardening](2026-07-13_po-sc-config-and-builder-hardening.md) | _–_ |
| 2026-07-10 | session_log | complete | docs | [Session — WS3 D2-2: enablement content authoring + ITS_Config data dictionary generator (PR #515)](2026-07-10_ws3-docs-d2-2-guides-and-config-dictionary.md) | #515 |
| 2026-07-06 | session_log | complete | field_ops | [Session 2026-07-06 (extended, operator-driven continuation) — Op Stds v20 consolidation + 2B progress-logging + #336 REQUIRED_CONFIG + M3 Slice 1 (PRs #478–#482, blueprint #62–#63)](2026-07-06_doctrine-v20-2b-progress-log-required-config-m3-slice1.md) | #478, #479, #480, #481, #482 |
| 2026-07-06 | session_log | complete | field_ops | [Session 2026-07-06 (autonomous) — Recurring checklists per job (#16, PR #476)](2026-07-06_recurring-checklists.md) | #476 |
| 2026-07-05 | session_log | complete | field_ops | [Session 2026-07-05 (part 2) — Activate shipped-dark trackers + Hours Log Task column + hygiene (PRs #472–#473)](2026-07-05_activate-trackers-hours-task-column-hygiene.md) | #472, #473 |
| 2026-07-05 | session_log | complete | field_ops | [Session 2026-07-05 (part 3) — Checklist form-builder redesign + My Tasks drill-in + photo-required + Materials grouping (PR #475)](2026-07-05_checklist-form-builder-my-tasks-photo-required.md) | #475 |
| 2026-07-05 | session_log | complete | field_ops | [Session 2026-07-05 (morning) — Hours Log go-live + P7 Slice 2 Equipment + live decouple fix + M2 Material List](2026-07-05_hours-log-fix-slice2-equipment-m2-materials.md) | #468, #469, #470 |
| 2026-07-04 | session_log | complete | field_ops | [Session 2026-07-04 (overnight) — Smartsheet wiring verification + Hours Log go-live smoke + archive-on-closure](2026-07-04_smartsheet-verify-hours-smoke-archive-on-closure.md) | #465 |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-17 — Ruff exemption for box_migration + v5/v6/v7/v4 doc pointer refresh](2026-05-17_ruff_and_doc_refresh.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-17 — Smartsheet workspace restructure (operator vs customer separation)](2026-05-17_smartsheet_workspace_restructure.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — `_alert_critical` Resend wiring + mypy tech-debt closure](2026-05-18_alert_critical_and_mypy_closure.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — box_migration reconcile + parse_subsubject](2026-05-18_box_migration_reconcile.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — restore parse_job v1 + v2 (cascade dependency for v3)](2026-05-18_box_migration_v1_v2_restore.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — doc cleanup: CLAUDE.md doc-version refs and stub/real table](2026-05-18_doc_cleanup.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — error_log Smartsheet write + SDK 404 filter](2026-05-18_error_log_smartsheet_write.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — kill_switch reads ITS_Config; initial seven-row seed](2026-05-18_kill_switch_and_config_seed.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — post-PR-#15 followup (memory + mypy + chore close)](2026-05-18_post_pr15_followup.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — sanity-check sweep](2026-05-18_sanity_check_sweep.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — Sentry triple-fire complete + Phase 1 critical-path unblock](2026-05-18_sentry_and_phase1_unblock.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-18 — smartsheet_client wired against sandbox](2026-05-18_smartsheet_client_wired.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [Session log — 2026-05-19 Cascade Absorb](2026-05-19_cascade_absorb.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-19 — chore sweep + mypy lockdown](2026-05-19_chore_sweep_and_mypy_lockdown.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [Session log — 2026-05-19 Watchdog Session 1](2026-05-19_watchdog_session_1.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [Session log — 2026-05-20 person_tag regex refinement (redo)](2026-05-20_person_tag_regex_refinement_redo.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [Post-Box-Pivot Repo Doc Cascade — 2026-05-20](2026-05-20_post_box_pivot_doc_cascade.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [Session log — 2026-05-20 PTO fetcher wiring](2026-05-20_pto_fetcher_wiring.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [Session log — 2026-05-20 Watchdog Session 2](2026-05-20_watchdog_session_2.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [Alert-routing dedupe ship + picklist sync foundation + V1 fix — 2026-05-21](2026-05-21_alert_dedupe_and_picklist_sync.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-21 — Box 1111A clone cascade](2026-05-21_box_1111a_clone_cascade.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-21 — Intake-test either-path refactor](2026-05-21_intake_test_either_path_refactor.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-21 — R3 Foundation PR](2026-05-21_r3_foundation_pr.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-21 — R3 session 1: intake.py wiring](2026-05-21_r3_session_1_intake_wiring.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-21 — Safety intake heartbeat row writes to ITS_Daemon_Health](2026-05-21_safety_intake_heartbeat.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-21 — Safety intake polling-daemon trigger](2026-05-21_safety_intake_polling_daemon.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-21 — Smartsheet error-translation refactor](2026-05-21_smartsheet_error_translation_refactor.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-22 — Box 1111B blueprint design (1111A forensic + canonical redesign)](2026-05-22_box_blueprint_1111b_design.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-22 — 2026-05-22 cascade absorb (repo-side doc reconciliation)](2026-05-22_cascade_absorb.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-22 — Follow-on fix: transient-404 retry + GENERATION_FAILED placeholder](2026-05-22_followon_404_retry.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-22 — R3 Session 2: safety_reports/weekly_generate.py + WPR pipeline](2026-05-22_r3_session_2_weekly_generate.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-23 — 1111B Box Blueprint Materialization (live build in mirror tenant)](2026-05-23_1111b_materialization.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-23 — CI main-branch keychain fix + four-part verification discipline](2026-05-23_ci_keychain_fix.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-23 — Picklist-hardening pre-Customer-1 (Phase 1.4 #2)](2026-05-23_picklist_hardening.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-23 — Post-1111B canonical cutover (re-clone strategy)](2026-05-23_post_1111b_canonical_cutover.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-23 — R3 Session 3: safety_reports/weekly_send.py + weekly_send_poll.py (closes R3 cycle)](2026-05-23_r3_session_3_weekly_send.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-23 — ITS_Trusted_Contacts + Intake Stage 2 refactor + Header forgery detection](2026-05-23_trusted_contacts_stage2_refactor.md) | _–_ |
| _(no frontmatter)_ | _–_ | _–_ | _–_ | [2026-05-25 — `shared/state_io.py` + atomic-write migration (closes F19 + F23)](2026-05-25_state-io-atomic-write.md) | _–_ |
<!-- END AUTO-INDEX -->

## First entry

[`2026-05-17_ruff_and_doc_refresh.md`](./2026-05-17_ruff_and_doc_refresh.md) —
ruff exemption for `box_migration/*` and the v5/v6/v7/v4 doc pointer refresh
PR #8 left half-done. Also the session that established this convention.
