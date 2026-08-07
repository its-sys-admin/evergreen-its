# ITS — Tech Debt

Items deliberately deferred. Each carries the rationale for deferral and the trigger for revisiting. The repo-side companion to Master Checklist §6 (planning project) — this file holds execution-layer tech debt; the Master Checklist holds owner-decision tech debt.

When to add an entry: a session deliberately chooses preservation-over-refactor (per Op Stds v11 §14), discovers an external-API constraint that forced a workaround, or defers a non-trivial cleanup that's larger than the current session can absorb.

**Split 2026-07-12:** resolved/closed/delivered/superseded entries now live in [`docs/tech_debt_closed.md`](tech_debt_closed.md) (archive) so this file stays under the 256 KB cap — this file holds only OPEN items. When an entry closes, **move** it to the archive with resolution detail (don't delete — history is cheap, context is expensive).

**Cutover triage:** every open entry below is **post-delivery** unless its header is prefixed **`[CUTOVER-BLOCKING]`** (must resolve before the Aug-7 production cutover). The authoritative cutover gate is `docs/operations/cutover_checklist.md` (CL-01…CL-39) + `scripts/verify_cutover.py`, not these tags — the tags are prioritization only.

## 2026-07-14 Debt-Zero Triage — session disposition

Every open entry below was triaged against live HEAD on 2026-07-14 (8-agent verification workflow + operator session). **Counts:** 123 unique entries — 12 verified **resolved/stale → moved** to [`tech_debt_closed.md`](tech_debt_closed.md); 91 **re-parked** (still valid, deferred, owner-tagged below; originally 110 — reconciled 2026-08-07, see note); 1 folded into the send-path re-park (scheduled_send_local). This index is the dated disposition record; each entry keeps its full rationale in place.

> **Index reconciled 2026-08-07 (delivery-day tech-debt cleanup).** This 2026-07-14 index was never
> updated when entries were archived, so it had drifted into pointing at entries that no longer exist —
> 19 of its 110 "re-parked (still valid)" bullets named entries already moved to
> [`tech_debt_closed.md`](tech_debt_closed.md), over-counting open debt by ~17%. Two of the stale bullets
> were `[CUTOVER-BLOCKING]`-tagged, which overstated cutover risk. Those bullets are removed and the five
> group counts re-derived from the surviving bullets. The three `### This session's code outcomes` bullets
> below are a historical session record, not entry pointers, and are left untouched. Verification method:
> each bullet title was matched against the surviving `## ` headers and cross-checked against the archive;
> one apparent stale bullet (`Publish daemon: privileged subprocess chain…`) was a FALSE POSITIVE — its
> entry is still live, the header just reads "chain **is** operator-validated-live" — and was kept.

### This session's code outcomes (2026-07-14)

- **DONE + merged:** Block A security scrub (#584 — production-identity scrub + working-tree gitleaks guard); C1 po_send fail-safe default (#585); C5 anomaly_logger FP narrowing (#586); C3 docs_pdf dark-gated Box publish leg (#588).
- **In flight:** item-7 portal tab-title + inline-SVG favicon (#589); SC-CFG-2 MAX_ADDRESS hoist (#590, portal-worker-security-reviewer CLEAN).
- **Re-parked with findings (need Seth / a focused session):** **C2** scheduled_send fail-closed — External Send Gate is FIXED high-class; C1 got explicit "nothing else on the send path" co-resolution, C2 did not (bundle the observability WARN-log + seeder here too). **C4/item-9** fail-closed guard-hook — exec hooks are real files (not the vulnerable relative-symlink shape); DR-D1's real target is the blueprint's symlink hooks (doctrine-fenced), a fail-closed SessionStart assertion has a chicken-and-egg + brick-CC blast radius needing operator-present validation; Check M detects post-hoc today. **C6/§404** hours_log indexed lookup — premature optimization on a low-volume path (don't-harden-dormant); revisit at the 20-job scale point. **C7/§466** smartsheet-python-sdk pin — the "dropped smartsheet.exceptions" claim is STALE: 4.2.0 restores it (all 3 used names present) and the FULL MOCKED suite passes on 4.2.0; but it is a MAJOR 3→4 bump and the suite mocks the SDK, so the operator must run `pytest -m integration` against 4.2.0 (§30 / mandatory-live-smoke) before loosening the pin to `<5.0.0`. One-line change, de-risked.

### Re-parked, by owner (still valid — trigger in each entry)

**Seth** (7):

- DKIM in-process re-validation — Security/threat-model enhancement (in-MTA-trust assumption); half-day. Trigger: security review or threat-model session flags it.
- Field-ops P2.3 write-layer follow-ups (deferred sub-features + governance) — Four remain: inspection quick-log (operator domain-input blocked), cap.admin.equipment orphan cleanup + 0013 label tidy (touch cap vocabulary → confirm Seth), §50 doctrine bump. Trigger: cap-management UI scheduled / equipment-inspection forms defined.
- Field-ops Smartsheet/Box source-of-truth integration (P2.4+ downstream) — BLOCKED on Seth gaining canonical-Evergreen Smartsheet SoR visibility + the §50 doctrine bump; building against unseen schema is worse than absent. Trigger: SoR access AND/OR §50 doctrine reaches Seth.
- Optional fail_closed_until kill-switch hardening (deferred) — Kill-switch fail-open contract + defense-in-depth is doctrine/security territory; revisit when a time-bounded hard halt is actually needed.
- Progress (and safety) no-recipient HELD surfaces a record, not an operator page — Adding a CRITICAL push leg on a no-recipient HELD is a Send-Gate severity-posture decision (§3.1, Seth-owned). Trigger: operator decides a blocked customer-facing send warrants an active page.
- Safety Portal M2 — capability gate static-AST-import-only — Dynamic-import/transitive-closure/scripts-scope gaps remain (now documented as residual in-code); security capability-gate is high-class. Trigger: next capability-gating hardening / before Customer-1.
- Single-token blast radius for Smartsheet — Secrets/blast-radius split is high-class (auth); revisit when a rotation incident causes a system-wide outage or multi-customer secrets land.

**operator** (21):

- /pending-jobs transport flakiness — deeper cause untraced, only blast-radius mitigated — Root-cause diagnosis needs a captured live failure + Cloudflare WAF/bot-fight dashboard cross-check; symptom already mitigated by #469. Trigger: next observed transient /pending-jobs failure.
- Confirm canonical_job_path() format with owner — Still no real consumer (only test_box_client.py:524 references it); owner never validated the write-path format. Trigger: first workstream that consumes canonical_job_path.
- Cross-leg dedupe activation — Sentry/Smartsheet stay record-only, so no push-leg dedupe needed; correlation_id already wired. Trigger: operator configures Sentry/Smartsheet alert rules.
- Doc-conventions lint strict-mode flip after retrofit window closes — Scheduled: bulk frontmatter retrofit sweep + one-line --strict CI flip; trigger 2026-07-24 (not yet reached) or operator opens a retrofit session / accepts permanent grandfather.
- ITS_Active_Jobs Address cells blank — office PM fill required — Live Smartsheet data-fill by office PM (no code); revisit at portal production go-live before enabling Work-Location autofill.
- ITS_Active_Jobs column order cosmetically scrambled — Cosmetic UI drag-reorder; not load-bearing (active_jobs looks up by title), convenience-only.
- ITS_Daemon_Health sheet observability drift — Partially eased (publish daemon now self-provisions its row) but stale live rows (retired intake_poll etc.) need operator UI deletion + Last-Error-clear. Trigger: next daemon-health pass; before cutover.
- Operator-UI Shortcuts for trusted-contacts workflows — Half-day tooling nicety, no functional gap. Trigger: tooling-track session bandwidth.
- Orphaned Reports sheet — column styling not applied — Cosmetic column widths on a live Smartsheet sheet (forbidden live-Smartsheet-write for a DO). Trigger: operator finds default widths inconvenient / a portal styling pass.
- Phase 5 manual week-sheet additions — By-design operator workflow for occasional manual corrections; no automation intended — retained as documented behavior.
- PowerShell Get-ApplicationAccessPolicy -Identity <friendly-name> directory lookup fails — Documented bare-cmdlet workaround for an EXO PowerShell gotcha; reference. Trigger: next EXO ApplicationAccessPolicy work.
- PowerShell macOS Gatekeeper deprecation 2026-09-01 — Runbook-only Azure-Cloud-Shell fallback; no code change. Trigger: 2026-08-15 calendar status check.
- Safety Portal — job-specific JHA variant content deferred — Parent/variant mechanism already built + data-driven; revisit when PM identifies a job with site-specific JHA requirements.
- Safety Portal — toolbox talk header context missing from form definitions — Definitions faithful to source PDFs; trivial field add gated on PM confirming a Presenter/Date header is wanted.
- Seed system.box_smoke_folder_id in ITS_Config — Manual create-Box-folder + seed-ITS_Config step for the opt-in --write-test smoke; read-only smoke works without it. Trigger: diagnosing Box scope/permission issues.
- Smartsheet UI-only constraints (Forms, CF, Filter Views, Restrict-to-dropdown) — Permanent platform limitation, manual deployment steps documented. Trigger: annual re-check / Smartsheet exposes these via API.
- Smartsheet-wiring audit findings — daemon-health + capacity hygiene — M-1/M-2/M-3/S-1 all RESOLVED/DONE; residual is seeding the two cross-workstream footgun ITS_Config rows + the sheet_capacity global carve-out. Trigger: cutover config-seed pass.
- Stale Anthropic Service Account svac_…SR7vDMJ for archival — Manual Anthropic-Console archive of an already-key-deleted service account; auth/console-side, not a code task. Trigger: next Anthropic Console visit.
- voice@ mailbox AppAccessPolicy scope addition pending — EXO scope-add + ITS_Config row; watchdog already iterates mail_intake.*. Trigger: a workstream activates voice@ as an intake source.
- Watchdog sweep cadence vs dedupe window length — Intentional 24h-max summary delay for daily-rhythm ops. Trigger: operator reports ≥24h-delayed summaries causing triage problems.
- WPR_Pending_Review final removal (decommission-by-doc → delete) — Blocked on operator deleting the live WPR sheet; SHEET_WPR_PENDING_REVIEW + smoke_test_watchdog_catchup WPR seeding still present (grep confirms). Trigger: after operator deletes the WPR Smartsheet.

**deploy-dependent** (7):

- .dash-section CSS class duplicates .card — .dash-section still in global.css alongside .card; cosmetic dedup, entry says not worth a standalone PR, needs deploy. Trigger: next design-system consolidation pass.
- Form editor: S1 per-item scale/comment authoring from scratch — editorModel.ts still lacks scale/comment item-creation fields; frontend change needs Worker deploy. Trigger: a new HSSE-type form authored via the editor.
- Publish daemon: rollback UI picker missing — Backend rollback op live; SPA picker still deferred and needs a Worker deploy to land. Trigger: a rollback is operationally needed / Phase-3 form-editor polish.
- Remove the progress-% estimate system-wide — Remaining cleanup touches the worker CREATE-route INSERT (portal-worker-security-reviewer DoD) + a destructive ALTER TABLE DROP COLUMN migration (0014, deploy-coupled). Trigger: a supervised worker-reviewed PR.
- Safety Portal — bcryptjs cost-10 may exceed Workers Free 10ms CPU cap — auth.ts still imports bcryptjs at cost 10 (unresolved); Paid-plan-vs-PBKDF2 choice is a production-cutover deploy decision.
- Subcontracts — PO/SC Configuration + builder follow-ups — SC-CFG-2 (index.ts still hardcodes literal 512 vs shared MAX_ADDRESS, verified) is in HELD PR #548 needing Worker deploy; PR-B2 is a large operator-directed exhibit-A+payment-terms build needing deploy + a Layer-A legal-attestation seed; SC-CFG-1 is informational-only.
- Worker publish-reject paths return bare error codes — no reason field — Low-severity optional server-side parity; client fallback is explicit+non-silent; Worker change needs deploy. Trigger: an unmapped code surfaces in prod / publish-flow UI polish.

**phase-1.5** (12):

- Attachment screening pipeline Layers 1-3 — Email-attachment §34 arbitrary-file path still unbuilt (photo/PO screeners are separate); required before Phase 1.5 cutover. Trigger: Phase 1.4 hardening session.
- Config editor (§50) — deferred follow-ups — CE-1/CE-2/CE-3/CE-6 verified RESOLVED (sweep to archive); open CE-5 (MEDIUM — who may attest legal clearance) is a pre-activation §44 decision gating config_actuator.polling_enabled, CE-4/CE-7 low residuals folding into PR-B2 / next po.test.ts touch.
- Fallback path removal after ITS_Config cutover — ~30min cleanup but gated on operator confirming one clean Friday cycle post-cutover; _check_legacy_allowlist still live at intake.py:626. Trigger: cutover confirmation.
- hours_log.find_entry_row does a full client-side scan of the sheet on every upsert/supersede call — O(sheet-size) scan still present but benign at low volume; revisit at the 20-job cutover or when a live Hours Log cycle is observed slowing.
- Native multi-PICKLIST graduation for Trusted Contacts scope columns — ~1hr; trusted_contacts allowlist currently dormant. Trigger: Picklist Hardening #1 deliverable lands + allowlist goes live.
- Phase 1.5 — provision dedicated ITS Box user account, re-auth — Cutover task: re-auth as dedicated ITS Box user + ownership/collab transfer; no code change. Trigger: Phase 1.5 production-tenant cutover.
- Pre-mirror-tree portal Box filings are sandbox orphans — 3 sandbox pre-activation orphans, no repair required in sandbox; revisit at a live customer Box-root cutover to decide leave-or-hand-move.
- R2 Watchdog Check E (Anthropic spend trend) deferred to Phase 1.5 — No _check_spend/anthropic_billing in watchdog; deferral is cost-signal-at-sandbox-scale rationale, confirmed in CLAUDE.md. Trigger: production cutover.
- Safety Portal Phase 5 — deploy prerequisites (Cloudflare secrets + D1 + wrangler.jsonc IDs) — Sandbox secrets/D1 done (HMAC live-validated 2026-06-08); production-tenant secrets/D1 re-run is a cutover gate.
- Safety Portal — no rate limiting on /api/login or /api/* — No ratelimit binding/WAF rule in worker; CUTOVER-BLOCKING, Cloudflare-dashboard/operator gated. Trigger: Evergreen production cutover.
- Watchdog/launchd hang-killer: hard-kill a daemon exceeding N× expected cycle duration — Recovery complement to Check-C detection; larger design decision (kill location, per-daemon N sizing) — Phase 1.4/1.5 reliability target.
- weekly_send upload-session — live-Graph integration smoke — Only mocked coverage of the 4-step Graph upload-session; §30 mocks-pass-live-fails surface. Trigger: pre-Customer-1 live-tenant validation / first real photo-bearing packet.

**post-delivery** (44):

- Alert-dedupe state is per-machine — Single-host through Phase 3; per-host dedupe is correct today. Trigger: ITS gains multi-host execution (Phase 4+).
- Alert-dedupe state-file growth in pathological flap-with-new-error-code scenarios — Bound holds today (fixed error_code); monitor-only. Trigger: state file exceeds ~100 persistent entries between sweeps.
- Alert-routing dedupe key granularity — Speculative not-yet-needed enhancement (all CRITICALs use error_code=uncaught_exception); ITS_Errors+Sentry still record each bug. Trigger: production shows different-bugs-collapsed-in-a-window.
- Allowlist drift detection — typo'd trusted-contacts entry silently quarantines — Email allowlist is dormant (portal PULL is the live intake path); half-day feature, revisit at next _load_contacts touch or Phase 1.6.
- Box folder delete-and-recreate breaks folder ID resolution — A2 routing sheet has landed, but the folder-ID validation layer + SDK trashed-folder smoke are Phase 2 hardening, not launch-critical.
- Checklist template identity is title-keyed (0026 design) — Low-blast-radius edge case; §14 says don't build the code/slug column speculatively. Trigger: template library grows past seeded set / admin reports items merging wrongly.
- Config-change audit trail — Phase 2 audit-as-deliverable; Smartsheet native cell-history covers the common case — trigger = first customer compliance/audit requirement.
- Configuration validation at daemon startup — Depends on A2/A3/A5 config-migration completing and overlaps #336; Phase 1.6 validation layer after the config-migration cluster.
- Conftest mock surface coverage — Latent forward-protection for future credentialed clients; still valid. Trigger: a new shared/*_client.py or a Linux-CI macOS-only failure.
- D1-primary tables have no ITS-side backup — Time Travel is the restore path — Accepted decision (R3-F7): §14 explicitly rejected a backup job, restore path documented. Trigger: a third D1-primary table lands / Cloudflare changes Time Travel retention.
- Draft cache stores one draft per account — Accepted single-slot design (deliberate); only revisit if concurrent multi-form editing is requested. Trigger: operator requests multi-form edit / a WIP draft-loss incident.
- Eventually migrate from legacy boxsdk to box_sdk_gen (Gen API) — ~half-day dependency migration; pin <4.0.0 holds. Trigger: Box announces 3.x deprecation, capability gap, or annual hygiene sweep.
- Hardcoded BOX_SUBPATH_BY_CATEGORY in safety_reports/intake.py — A2 (shared/project_routing.py) has landed so the trigger is technically met, but this lives in the dormant email-intake branch; it is a ~2hr config-migration, not a quick fix.
- HTML email rendering for weekly_send — weekly_send still sends WSR Email Body as text; HTML render awaits Teala's feedback on inline-text format after first 30 days of real cycles.
- Inline doctrine-pin normalization across shared/* + safety_reports/* — The one real fix (weekly_send.py:72 §23.3→§19) lives in a SEND-path file (excluded); fold in opportunistically next time those files are touched.
- ITS_Active_Jobs CC recipients are operator-entered, not allowlist-validated — Accepted-risk under trusted-operator-input model (Send Gate still requires explicit approval); revisit only if allowlist validation is later wanted.
- Nightly auto-index regen wiring — CI --check gate is the load-bearing enforcement; wire the launchd/watchdog job only when local drift is observed or a 3rd daemon touches watchdog wiring.
- P2.5 job-tracker up-sync — fast-follows — Items 1 (needs picklist_sync.py capability-gating enrolled FIRST to avoid meta-test break) and 4 (find-after-create race, idempotent nuisance) remain; 4 others closed. Trigger: item1 when picklist_sync enrollment addressed; item4 on an observed duplicate.
- P2.5 Slice 6 — portal-owned canonical number: residual redundancy — Two harmless §14-preservation leftovers (Portal Job Key == Job ID; always-set canonical mirror machinery) kept to avoid churn on a working reviewed path. Trigger: a later slice consolidates the identity columns.
- Picklist_Sync_Config mixes config and runtime state — §14 preservation, single consumer. Trigger: first operator-edit accident wiping hash/timestamp, or multi-customer schema evolution.
- PO attachments (Feature B) — conscious deferrals — ATT-1..6 are all accepted limitations / Phase-2 §34 hardening (VirusTotal, ObjStm/OpenXML deep-parse) or by-design; limited-blast-radius posture; revisit at the Phase-2 §34 hardening pass or an upload-scope widening.
- Portal permission-model stale plumbing — vestigial/orphaned caps, coarse gate — Items 3-4 RESOLVED; residual = 3 cheap-open ungated caps + cleaning 3 orphaned-cap migration comments (Worker/migration, needs deploy). Trigger: cap-management UI or inspection surface ships.
- PR-4 Part A — PDF download cache deferred optimizations + PR-5 supersession — Deliberate deferred perf optimizations, within-UI-budget today; PR-5 supersession is a forward note. Trigger: latency/scale review of the download path.
- Publish daemon: privileged subprocess chain operator-validated-live only — No --dry-run harness for the git/gh/wrangler chain yet; high-class deploy-adjacent daemon work. Trigger: publish-daemon code modified / Phase-3 hardening.
- R-series spec Deferred #5–#10 — named follow-ups — Explicit locked-decision scope cuts (not regressions); listed to avoid rediscovery as bugs. Trigger: next field-ops UX pass — check before re-scoping.
- R2 upgrade path for portal photo transport (deferred) — ADR-0001 deferred design (second storage plane); revisit when field crews need >4 full-res photos/field or the Worker body bound blocks a budget raise.
- Safety email-intake retire — operator-manual + future-PR follow-ups — Tombstone deletion done (intake_poll.py + weekly_summary.py DELETED 2026-07-03); residual = WPR sheet deletion (still referenced, own entry) + optional Job Slug column delete.
- Safety Portal M7 — publish daemon runs destructive git on live ~/its tree — Still runs git clean/checkout on the live tree without worktree isolation; daemon-hardening code change. Trigger: next publish-daemon hardening; before cutover.
- Safety Portal — Worker-side capability-gate for TS not covered by Python AST gate — Still no TS capability-gate (grep: no eslint no-restricted / no .ts coverage in test_capability_gating.py); duplicate of the later ts-sendgate entry — revisit when Worker gains outbound code path.
- safety_reports week-folder create-find race condition — Rare at single-machine cadence; WARN+operator-visibility preferred over auto-clean. Trigger: race observed in practice (multi-machine ops).
- SDK-vs-live body-shape mismatches need integration coverage — Mitigation landed (test_smartsheet_client_integration.py); kept open as standing pattern-to-extend reminder for new SDK wrappers.
- Severity-tiered + multi-recipient alert routing — Phase 2 post-Customer-1; single-recipient ITS_Config routing is adequate for the solo/Customer-0 stage — trigger = team expansion or Customer 2 onboarding.
- Smartsheet API constraint: AUTO_NUMBER columns rejected at sheet creation — Permanent platform constraint with row-ID workaround ("likely never"); reference note. Trigger: a workstream needs user-visible auto-IDs.
- Smartsheet API constraint: column FORMAT must be set via model attribute, not dict constructor — Documented SDK-vs-live reference gotcha; apply_column_styles already uses the attribute path — revisit only when new column-format code is written.
- Smartsheet API constraint: DATETIME columns require system column type — Permanent Smartsheet platform constraint (ABSTRACT_DATETIME workaround confirmed 2026-06-09); reference note. Trigger: Smartsheet surfaces user-editable DATETIME.
- smartsheet-python-sdk upper-bound pin (CI-break stopgap) — Pin <3.10.0 still live and load-bearing (import at smartsheet_client.py:46); proper ~1hr fix requires verifying the newer SDK exception surface — dependency-maintenance pass.
- Smoke harness pattern divergence between dedupe smoke and Resend/Sentry smokes — Forward-protection note, no active failure; documents that new full-triple-fire smokes must use @its_error_log. Trigger: next triple-fire smoke authored.
- Structural fix: lazy keychain loading + DI-injected kill_switch — Non-trivial cross-call-site refactor deferred by design; fold in when a session next touches smartsheet_client or kill_switch for another reason.
- Subcontracts — SC-S3c adversarial-review follow-ups (non-blocking) — SC3c-4 now RESOLVED (DAEMON_ROOTS generalized to all 5 daemon pkgs); SC3c-1 (shared PO+SC supersede race needs joint po.ts re-review), SC3c-2 (stale comment on applied migration 0050), SC3c-3 (forward-looking to SC-S4 send) remain valid, revisit when SC-S4 send is built.
- Summary email content depth (filter-criteria vs inline correlation IDs) — Pull-from-SoR design accepted; schema upgrade deferred. Trigger: open-summary→open-ITS_Errors friction becomes frequent.
- weekly_send upload-session chunk-retry hardening (deferred) — Cross-cycle session-resume/cancel deferred, restart-from-zero cheap today; revisit on recurring mid-upload failures or packets nearing the 150 MB ceiling.
- weekly_send upload-session threshold = 2.5 MB (heuristic, not measured) — Send-path threshold tuning against the live tenant; revisit when the first live photo packet crosses ~2.5 MB or a graph_error cluster appears near 3 MB.
- Worker-side send-gate enforcement (the TS Worker is outside the Python AST capability-gate) — Confirmed no TS send-gate CI rule exists; Worker stays send-free by design — add fetch-allowlist grep/ESLint when it gains an outbound path or at deploy hardening.
- §6a enablement-doc DoD owed per Progress-Reporting slice — PARTIALLY_MITIGATED — manifest pipeline now exists; residual = retire in-doc TODOs, add material_catalog guide, D2-2/D2-3 content. Trigger: each Progress-Reporting slice brief.

---

## Documentation-consolidation audit — ~41 findings remain unapplied [OPEN 2026-07-29, medium]

A 12-agent audit (6 auditors + 6 adversarial verifiers, against HEAD `885d4a4`) produced **172
confirmed findings** across 63 files. The 2026-07-26→29 session landed **six themed PRs**
(#2–#7) and took exactly one file to completion (`host_migration_runbook.md`, all 19). Honest
accounting:

```
172  total findings
113  live in files those PRs touched  <- only the themed subset was applied
 59  in files never opened
```

True applied count ≈ **60–70**. Of the 59 untouched, **~13 are out of scope by rule** (code:
`verify_cutover.py` 4, `generate_config_dictionary.py` 3, `watchdog.py` 1, `system_map.py` 1;
historical: `docs/reports/` 4), **5 are blueprint-repo**, leaving **~41 legitimate docs items**.
Largest clusters: `docs/doctrine_manifest.yaml` (4), `docs/runbooks/its_errors_triage.md` (3),
`context-pack/repo-overview.md` (3), `docs/ROADMAP.md` (3), `docs/troubleshooting/tree.yaml` (3),
`docs/operations/production_rollback.md` (2), `docs/runbooks/estimate_import_path.md` (2),
`docs/runbooks/subcontract_generation_path.md` (2), `docs/references/picklist_sync.md` (2).

The findings file (with verbatim stale text, file:line evidence and pre-reviewed `draft_fix`
for each) is NOT in the repo — it was a session artifact at `~/doc_findings.md` on the
production host. **Re-running the audit is cheaper than reconstructing it** if that file is gone.

**Trigger:** next docs-focused session, or before the Aug-7 delivery if any of the remaining
files are operator-facing on the day.

**Two traps for whoever picks this up:**
1. **The sha-pin set is 22 sources, not 12** — see the entry below.
2. `check_doctrine_drift --strict` is BLOCKING in CI (M1/M4/M7); `doctrine_manifest.yaml` edits
   are exactly the kind that trip it. Run it locally before pushing.

---

## Enablement sha-pinning covers `docs/references/` too, not just `docs/enablement/` [OPEN 2026-07-29, low]

`docs/enablement/manifest.yaml` records a sha256 for **22 source files**, and CI
(`test_docs_pdf` → `build_docs_pdfs --check`) goes RED when any drifts. The commonly-documented
warning names only `docs/enablement/`. In fact **10 pinned files live under `docs/references/`
and `docs/troubleshooting/`**: `daemon_reference.md`, `integration_reference.md`,
`escalation_matrix.md`, `system_architecture.md`, `documentation_index.md`,
`its_config_dictionary.md`, `security_trust_model.md`, `glossary.md`, `data_model_reference.md`,
`troubleshooting_guide.md`.

There is **no `--record`/`--update` flag** — `build_docs_pdfs` only offers `--check`. Re-recording
is manual: `shasum -a 256 <file>` → paste into the matching `sha256:` line. The 2026-07-26→29
consolidation re-recorded 8 values this way.

**Fix candidates:** (a) add a `--record` mode to `scripts/build_docs_pdfs.py`; (b) at minimum,
state the true pinned scope wherever the enablement-sha trap is documented. **Trigger:** next
session that edits a `docs/references/` file, or any `docs_pdf` work.

---

## `its_config_dictionary.md` asserts fail-OPEN for every config read — wrong for send gates [OPEN 2026-07-29, medium]

`docs/references/its_config_dictionary.md` states: *"**Default** is what ITS uses when the row is
**missing, blank, or unreadable** — every read is fail-open to this value."*

That is **false for the send gates**, which are the reads where it matters most.
`po_send_poll.py`, `rfq_send_poll.py` and `subcontract_send_poll.py` all set
`DEFAULT_POLLING_ENABLED = False` (CO-1, PR #585 `45fe4df`: *"a send gate never fails open"*), so
a missing row fails **safe**. Conversely `progress_send_poll.py:76` and
`safety_reports/weekly_send_poll.py:69` DO default `True` — those two genuinely fail open, which
the same sentence obscures by making fail-open sound universal and benign.

It is a **GENERATED file** (`<!-- GENERATED FILE — do not hand-edit -->`); the string lives in
`scripts/generate_config_dictionary.py`, so this needs a **code PR**, and the regen must be
followed by re-recording the doc's sha256 in the enablement manifest.

**Trigger:** any `generate_config_dictionary.py` work, or the next send-gate documentation pass.

---

## WS2 dashboard clear-error-log verb (#587/#591/#594) + live error-chase findings [OPEN 2026-07-14]

Session that shipped #587 (back-nav banner-extension), #591 (11-row `ITS_Config` migration seed), and #594
(the Class-B dashboard clear-error-log verb, `shared/errors_rotation.py`), plus a manual forensic wipe of
`ITS_Errors` (6,249 → 217: 215 open CRITICALs + 2 `errors_log_cleared` audit rows preserved) and a live
error-chase over the remainder. Findings from the chase, not yet actioned:

- **DASH-5 (RETRACTED 2026-07-17 — see the 2026-07-15 error-flood diagnosis).** Originally flagged HIGH/P1:
  both out-of-band alert legs down (`ITS_RESEND_API_KEY` 401, `ITS_SENTRY_DSN` `BadDsn`). **FALSIFIED.** A
  2026-07-15 forensic read of `ITS_Errors` + `~/its/logs` found the underlying error volume was **phantom
  pytest pollution**: ~13–15 pytest runs during the 07-14 dev session made REAL network calls against
  `tests/conftest.py`'s stub creds (`test-{service}`) and their `shared.error_log` writes landed in the LIVE
  dated log (2,285 Smartsheet 401s + 27 Resend 401 + 27 Sentry BadDsn, all test-generated) alongside 457 clean
  live daemon cycles. Real CRITICALs alerted successfully both before and after the window (07-14 19:59Z,
  07-15 08:35Z) — the alert legs were never actually broken. **Do NOT rotate `ITS_RESEND_API_KEY` /
  `ITS_SENTRY_DSN` on this finding.** The pollution mechanism itself is a genuine, still-open, DIFFERENT
  tech-debt item — see "pytest live-log/state pollution" below. Detail: auto-memory
  `project_error-flood-diagnosis-2026-07-15.md`.
- **DASH-6 (LOW, operator/dev) — `config_actuator`'s broad `except Exception` sites make root-causing an
  incident slower than it should be.** `po_materials/config_actuator.py` carries a dozen-plus
  `except Exception as exc:  # noqa: BLE001` sites (deliberately broad, per their own comments — "any
  actuation failure is terminal+alerted", "never wedge the cycle"). This session's chase of a
  `config_actuator`-attributed `ITS_Errors` row needed a source read to conclude it was benign (a gate
  flipped before its matching Worker secret/route was deployed — see the activation lesson below), rather
  than being legible from the error message/error_code alone. Not urgent (the incident was genuinely benign
  and the broad catches are individually justified), but a pass to give each site a more specific
  `error_code`/message would make the next incident self-diagnosing from the `ITS_Errors` row alone. Trigger:
  next `config_actuator` touch, or a recurrence of an unlabeled `config_actuator` error.
- **DASH-7 (RESOLVED 2026-07-17 — see the 2026-07-15 error-flood diagnosis + the 07-16/17 live activation).**
  Originally flagged: `po_send_poll` showing "no marker" (never ran) while `po_materials.po_send.polling_enabled`
  appeared to read `True` live, diverging from the seeded/documented `false`. **Reconciled, no drift was ever
  real** — the 07-15 diagnosis confirmed the gate genuinely read `false` as seeded/documented and
  `po_send_poll` had never been activated (no plist installed, matching the intentional `VC-02
  DARK_UNLOADED_LABELS` posture); the original chase's "`True`" reading was an artifact of the same
  pytest-pollution window as DASH-5, not a live config state. **Superseded 2026-07-16/17** — the operator has
  since deliberately activated BOTH the PO-send and subcontract-send lanes live (plist loaded, gate flipped,
  Application Access Policy scope confirmed via Exchange Online PowerShell `Test-ApplicationAccessPolicy`,
  end-to-end Graph self-send from `procurement@` verified) — see memory-archive §G68.3. `po_materials.po_send.
  polling_enabled` reading `true` today is the intended live state, not drift.
- **DASH-8 (RESOLVED 2026-07-14, PR #597).** The dashboard had no "mark this CRITICAL resolved" verb — built
  same day: `mark_errors_resolved` (Class-B, `operator_dashboard/act/errors_ops.py`) stamps `Resolved At` on
  open-CRITICAL rows matching a **required** Script/Error-code filter (unfiltered mass-resolve refused),
  making them terminal so `clear_error_log` (#594) can then sweep them. Wired in one PR: the route
  (`/act/errors/resolve`), the `config.html` form, the mutation-route registry test, and CLAUDE.md.
- **DASH-9 (near-RESOLVED 2026-07-17) — open-CRITICAL backlog in `ITS_Errors`, down from 215 to 9.** The
  2026-07-14 forensic wipe correctly preserved every open CRITICAL (never auto-deleted), leaving 215
  open-CRITICAL rows post-wipe — most believed storm-era noise from the 2026-07-13 row-cap incident (the same
  `config_row_missing` firehose that filled the sheet to its 20k cap), none individually confirmed-benign at
  the time. **2026-07-16/17 partial action:** using the new `mark_errors_resolved` verb (#597), 83 of the 215
  were confirmed benign and marked resolved (50 `intake_poll`/retired-daemon + 33 smoke/test rows) → 132
  remained. **2026-07-17 full diagnosis pass:** the backlog had grown back to 134 by the time this session's
  own error-flood diagnosis ran (ongoing transient-Smartsheet-outage-window CRITICALs, ~2026-07-12→07-16,
  accruing between passes — root-caused LOW severity, the retry→breaker→fail-open stack behaved correctly,
  no code change needed beyond #608's alert-hygiene fix below; full root-cause narrative in blueprint
  memory-archive §G69.1); using the same verb,
  dry-run→live per `(script, error-code)`, audit-stamped `its-diagnosis-2026-07-17`, resolved **92 transient +
  33 historical** rows → backlog **134→9**. **The 9 residual, individually accounted for:** 7×
  `safety_reports.intake` / `uncaught_exception` — a REAL, still-open bug (`'tuple' object has no attribute
  'value'`) on the legacy/dormant email-intake code path (portal-marker is the live transport;
  `intake.py`'s email-ingestion stages are dormant-but-present and this is firing from within them) — NOT
  fixed, needs a real code fix or a decision to excise the dormant path entirely; 2× `scripts.watchdog` /
  `critical` — residue from the already-fixed 2026-07-13 row-cap incident (PR #562's storm-mode fallback has
  been live since), kept unresolved deliberately as historical record, safe to resolve whenever. Trigger for
  the remaining 7: next `safety_reports/intake.py` touch, or the dormant-email-path excision decision.
  **2026-07-19 correction (forensics pass): the "7× `safety_reports.intake` `uncaught_exception`, believed a
  REAL still-open tuple bug" residual is FALSIFIED.** All 7 rows are pytest pollution from a single 9-minute
  window on 2026-05-21 — the tracebacks contain mock frames and pytest tmpdir paths, and the production
  `kill_switch` code cannot produce the `'tuple' object has no attribute 'value'` shape. No dormant-path code
  fix or excision decision is needed for these rows. The 2× `scripts.watchdog` row-cap CRITICALs are likewise
  confirmed stale (fix live since 2026-07-13, zero recurrence). Both classes cleared as benign.
- **DASH-10 (on-the-horizon, WS2 follow-up) — dashboard native-app repackaging decision captured, not built.**
  Operator directed **Option A** for a future session: repackage the dashboard as a native macOS `.app` via
  `pywebview` + `py2app`, keeping the existing Tailscale-only exposure model unchanged (no new network
  surface, just a nicer launch/window experience than the current browser-tab + web-app-manifest Dock
  shortcut from #581). Not scoped or built this session — recorded here so the decision isn't re-litigated
  next time WS2 polish comes up. Trigger: next WS2 session, operator bandwidth for a UI-shell change.
- **DASH-11 (LOW, WS2 follow-up) — `picklist-sync` is unreachable via the dashboard's interval-edit verb.**
  An operator question ("can the dashboard change daemon run intervals?") surfaced that
  `operator_dashboard/act/daemon_ops.edit_interval` (#570) covers only an 8-daemon label allowlist;
  `picklist-sync`'s 3600s cadence is a hardcoded `StartInterval` literal in its plist, outside that
  allowlist, so its interval can't be edited from the dashboard today (confirmed: the daemon itself was
  healthy, this is a coverage gap, not a bug). Either add it to the allowlist or document the exclusion
  explicitly so the question doesn't need re-investigating next time. Trigger: next WS2 daemon-control
  polish pass.
- **DASH-12 (BUILT 2026-07-19) — dashboard Restart-dashboard verb.** Shipped as
  `operator_dashboard/act/dashboard_ops.py` + `POST /act/dashboard/restart` (Class B, elevated-confirm
  `restart-dashboard`): audit row written BEFORE the spawn, then a detached
  `/bin/sh -c 'sleep 1; exec launchctl kickstart -k gui/<uid>/org.solutionsmith.its.dashboard'` with
  `start_new_session=True` + closed stdio (survives the dashboard's own SIGTERM). Restart-ONLY — never a
  pull/deploy, never another label; `daemon_ops.controllable_labels()` still excludes the dashboard
  (`tests/test_dashboard_restart.py` locks all of this, incl. the restart-only command allowlist). §43
  entry in `docs/runbooks/operator_dashboard_config_editor.md`. The operator-pre-authorized self-exclusion
  exception is documented in the verb's module docstring.
- **DASH-13 (verb BUILT 2026-07-19; disposition pending operator) — `ITS_Review_Queue` backlog.** The
  2026-07-17 characterization ("285× no-safety-contact re-raised every Friday") was STALE — the 2026-07-19
  live read found **296 PENDING: 277× "weekly compile failed" (189 of them a one-day 06-13 storm for the
  since-deleted JOB-000013), only 6× no-contact (all 2026-06-07, jobs since deleted, cannot recur), 13
  misc**. Root causes fixed in PR #613: compile_now_poll scan-phase transients no longer mislabeled +
  review-row dedupe caps a stuck-Compile-Now retry at ONE PENDING row per (job, week). The bulk-resolve
  verb shipped as `operator_dashboard/act/review_ops.py` + `POST /act/review/resolve` (Class B, elevated
  `resolve-review`, filter-required, preview mode, nothing deleted). REMAINING (operator decisions):
  (a) the 3 surviving jobs (JOB-000017/-018/-027) are sandbox fixtures — deactivate portal-side (D1
  lifecycle → inactive; a sheet-side flip gets overwritten by fieldops_sync on the next portal edit) or
  keep as test fixtures and populate JOB-000027's blank Safety Reports Contact Email; (b) sweep the 232
  stale rows for deleted jobs via the new verb; (c) the 4 "sheet-count near cap … margin 60" rows expose a
  margin==cap misconfig (`sheet_capacity` margin should be < the 60 cap) — an ITS_Config value fix.
  **2026-07-19 handoff-pass update:** (b) DONE — the remaining 62 stale rows swept via the resolve verb
  (count-guarded dry-run first; queue now 2 PENDING = the two 'Acme Concrete' picklist mismatched-reference
  rows, a real pending data decision). (c) RESOLVED-AS-FOSSIL — the live `smartsheet.sheet_count_ceiling/
  margin` rows read 1500/50 (sane, explicit-seeded 2026-07-06); the four "14/60 (margin 60)" review rows
  were fired 2026-07-15 under values since corrected/reverted — no config change needed. JOB-000027's blank
  Safety Reports Contact Email populated (seth@solutionsmith.org, matching the sibling test jobs; audited
  `active_jobs_contact_populated`). REMAINING operator decisions: (a) deactivate-or-keep the 3 sandbox jobs
  (portal-side if deactivating), and the Acme Concrete picklist removal. The dashboard upgrade slate is
  filed at `docs/2026-07-19_dashboard_upgrade_slate.md`.
- **DASH-14 (found 2026-07-19, FIXED 2026-07-19) — PR #613's config-read fence fix is not ported to 3 replicas.**
  **FIXED 2026-07-19: all 3 replicas ported** (`safety_reports/compile_now_poll.py`,
  `field_ops/fieldops_sync.py`, `safety_reports/generate_core.py` — each file's single
  `_read_str_setting`-style reader now catches base `SmartsheetError` → WARN `config_read_error` +
  fallback, exactly the #613 shape, with per-reader fence tests). The F22 `_load_authorized_approvers`
  gate in `send_poll_core.py` remains deliberately fail-CLOSED, untouched. Same PR also closed the
  watchdog Check S stale-green blind spot: a green latest ci.yml run is now compared against
  origin/main's actual HEAD sha (the 2026-07-19 push-event delivery gap left merge commits with ZERO
  runs reading as "green" indefinitely); mismatch → WARN naming both shas + `gh workflow run ci --ref
  main` (the #619 workflow_dispatch); HEAD-resolution failure stays fail-safe INFO. Original entry:
  PR #613 fixed a class of bug where a daemon-local `_read_str_setting` config-row reader caught only
  `smartsheet_client.SmartsheetNotFoundError` + `SmartsheetCircuitOpenError` before falling back to a
  default — letting a generic single-cycle transient (`SmartsheetError` base: read-timeout, HTTP 500/502)
  escape uncaught to `@its_error_log` as a full spurious CRITICAL instead of a WARN+fallback. The fix landed
  in `po_materials/po_poll.py`, `subcontracts/subcontract_poll.py`, and `safety_reports/send_poll_core.py`
  only. **Confirmed live via `grep` this session, the identical shape remains unfixed in 3 more readers:**
  `safety_reports/compile_now_poll.py`'s own `_read_str_setting`, `field_ops/fieldops_sync.py`, and
  `safety_reports/generate_core.py` — all three still catch only the two named exception types. This is a
  direct 3-file port of PR #613's fix pattern, not a design question. **Deliberately NOT the same as** the
  F22 `_load_authorized_approvers` gate in `send_poll_core.py`, which PR #613 correctly left untouched
  (fail-closed on a config-read failure feeding an approval-authority list is the intended behavior, not a
  bug). Trigger: next error-hygiene pass, or immediately if any of the three fires a spurious CRITICAL
  again. See blueprint memory-archive §G70.3.

**Activation lesson from this session's chase (not a tech-debt item, a process note):** a daemon's
`ITS_Config` polling gate should be flipped `True` only **after** its matching Cloudflare Worker
secret/route is actually deployed — flipping the gate first produces a benign-but-noisy bearer-rejected/401
CRITICAL storm on every cycle until the deploy catches up. Root-caused this session on a subcontract-lane
`bearer_rejected` error. Apply this ordering on every future config-actuator/Worker-secret pairing.

- **[RESOLVED 2026-07-21] A 4th, previously-uncounted replica of the DASH-14/#613 config-read fence gap:
  `safety_reports/publish_daemon.py` — plus the F22 approver gate, both closed by the transient-fence PR.** The "FIXED 2026-07-19: all 3 replicas ported" claim above is
  itself now stale — a fresh `grep` this session (2026-07-21, coverage-gap hunt) found
  `publish_daemon.py`'s own config-row reader (`get_setting(key, workstream=WORKSTREAM)`, ~line 195)
  still catches only `SmartsheetNotFoundError`/`SmartsheetCircuitOpenError`, not the generic
  `SmartsheetError` base — the exact #613 shape, a direct one-file port away, not a design question
  (unlike the F22 `_load_authorized_approvers` contrast noted above, which is correctly untouched).
  **Separately, still open (found same session, NOT fixed):** the F22 gate itself
  (`send_poll_core._load_authorized_approvers`, shared by all five send-poll daemons —
  `weekly_send_poll`/`progress_send_poll`/`po_send_poll`/`subcontract_send_poll`/`rfq_send_poll`) is
  *deliberately* unfenced/fail-closed by its own §42 docstring — correct for the SEND decision (never
  dispatch on an unreadable approver set) but means a one-cycle transient `list_workspace_share_emails`
  blip still pages a full CRITICAL and aborts the cycle, the same noisy-but-safe class #613/#628 quieted
  elsewhere via a WARN-and-skip reclassification that didn't touch the fail-closed *behavior*, only the
  *severity*. No reclassification has been attempted here — flagged, not built, since it touches the F22
  security gate and warrants care before touching. Trigger: next error-hygiene pass, or the next time
  either surface actually pages on a Smartsheet blip. See blueprint memory-archive §G72.
  **RESOLUTION (2026-07-21, the transient-fence PR).** Both surfaces are now fenced.
  `publish_daemon` adopts `shared/creds_resolution.read_base_url` and PROPAGATES
  `SmartsheetCircuitOpenError` instead of swallowing it into a fail-open "polling disabled"
  (reporting a breaker outage as a disabled gate was a lie, and it kept the fleet's fastest
  observer out of the circuit-open escalation). The F22 gate was reclassified with explicit
  operator ratification — severity only, never behaviour: a transient approver-read failure
  records ERROR and counts toward a sustained counter (threshold 3 at the 15-min send cadence)
  instead of paging immediately, while auth/permission errors still page at once. Fail-closed is
  unchanged and now PROVEN by test (zero `send_fn` calls on ANY approver-load failure, transient
  or not); the prove-it-bites injection for that assertion dispatched a real send and the test
  caught it. `_load_authorized_approvers`' §42 contract docstring was rewritten in the same diff
  so the contract cannot contradict the code.

- **[OPEN 2026-07-21, Seth-owned] `po_materials.rfq_send.polling_enabled` shipped `first_activation_gated`
  (dashboard tier "A") rather than `elevated_confirm`, per PR #627's own in-code rationale — and, found
  live the same session, the gate already reads `true` on the mirror (as do `po_send`/`subcontract_send`/
  `estimate_poll`/`rfq_poll`), contradicting `CLAUDE.md`'s `po_materials/rfq_*` row (still says "ships
  **dark**... Go-live = FIXED high-class External-Send-Gate flip ... → Seth") and this repo's own prior
  session-close records. #627 itself only fixed the *dashboard's* notes to stop asserting a live-state
  claim (§42/§55.4 truthful-reporting fix) — it deliberately did **not** touch the gate value or resolve
  whether tier "A" is the right activation posture for an External-Send-Gate crossing. Two things need
  Seth's call, not autonomous action: (1) whether `rfq_send` (and the sibling procurement gates) should
  in fact be live on the mirror, or the flip was premature; (2) whether the console's activation tier for
  this class of gate should be `elevated_confirm` (PIN + typed confirm + attestation) rather than the
  faster-brake "A" tier, given `apply_elevated_edit` can already complete a false→true send-gate flip
  ~~Also stale from the same finding: `CLAUDE.md` lines ~148/249 still say "16 tracked jobs"~~ —
  **RESOLVED 2026-07-26 (documentation-consolidation pass):** both CLAUDE.md surfaces now read
  **18 tracked jobs**, matching `watchdog.TRACKED_JOBS` (verified: 18 entries). The rest of this
  entry — the `rfq_send` activation-posture questions — remains OPEN and is unaffected. Trigger: next
  operator RFQ-send go-live/activation-posture session. See blueprint memory-archive §G72.2.

- **[OPEN 2026-07-21, low, Seth-owned] `config_actuator` reads `safety_reports.portal.worker_base_url`
  under `workstream="po_materials"`; `po_poll` reads the same key under `workstream="safety_reports"`.**
  Both rows exist so both resolve today — this is a **preserved-byte-for-byte** divergence, not a live
  bug (`config_actuator.py`'s own docstring at `_resolve_creds` names it explicitly and declines to
  "fix" it mid-unrelated-change, per §14). Worth a doctrine/config-model look at some point: either the
  two workstream scopes should converge on one, or the divergence should be named as intentional
  (e.g., "config_actuator reads its own daemon's workstream scope for every config key, no exceptions")
  rather than left implicit. Trigger: next config-model/`ITS_Config` schema session.

- **[OPEN 2026-07-21, low] `scripts/verify_cutover.py`'s VC-01 docstring undercounts required secrets:
  says 18, `REQUIRED_SECRETS` is actually 20.** The tuple itself (`NON_BOX_SECRETS` 11 +
  `BOX_SECRETS` 3 + `PO_SECRETS` 1 + `DARK_BEARER_SECRETS` 4 + `OPERATOR_SECRETS` 1 = 20) already
  correctly enrolls `ITS_PORTAL_ESTIMATE_TOKEN` + `ITS_PORTAL_RFQ_TOKEN` (added with the RFQ/estimate
  lane) — the cutover CHECK is not under-enforcing, only the module-docstring summary line (~line 33) is
  stale. Trivial fix, not urgent (no functional gap). Trigger: next `verify_cutover.py` touch.

- **[OPEN 2026-07-21, low] `operator_dashboard/act/registry.py` enrolls `subcontracts.subcontract_send.
  polling_enabled` (added by PR #627) but not its `from_mailbox`/`scheduled_send_local` siblings** —
  `rfq_send` got all three (`polling_enabled`, `from_mailbox`, `scheduled_send_local`) in the same PR,
  `subcontract_send` only got the one that was already-missing-and-flagged. A parity gap between two
  structurally-identical send lanes' console coverage, same shape as the coverage-gap hunt's other
  findings this session. Trigger: next dashboard registry touch, or the next `subcontract_send` config
  session.

## 2026-07-15 error-flood diagnosis — open gaps surfaced, not fixed [OPEN 2026-07-17]

Diagnosis-only session (no code changes) that decomposed "today's massive error log" into two unrelated
storms — see auto-memory `project_error-flood-diagnosis-2026-07-15.md` for full detail; DASH-5/DASH-7 above
were retracted/resolved from this diagnosis. Four open design gaps surfaced, none fixed:

- **(Seth, observability) — `ITS_Errors` record-writes are lost, not queued, during a Smartsheet outage.**
  `shared/error_log.py:133` wraps the `ITS_Errors` write in `circuit_breaker.bypass()` with no retry/queue —
  during the 2026-07-15 08:35–09:36Z real Smartsheet US outage (vendor-side, breaker behaved textbook: tripped
  at 5 failures, self-closed after 9 failed probes), **1,264 of ~1,368 ITS_Errors record-writes were
  permanently lost**; `~/its/logs/2026-07-15.log` is the only full record of that window. No business-data
  loss and every fail-open default collapsed fail-safe, but the forensic leg itself has no durability under
  exactly the outage it exists to record. Trigger: next alerting/observability hardening pass.
- **(Seth, alerting) — a total Smartsheet outage by itself pages nobody.** Breaker-open is logged WARN only;
  watchdog Check J (prolonged-open) is daily-cadence, not real-time. The operator got exactly one page during
  the 07-15 storm, and only coincidentally (`progress_send_poll` CRITICAL `ReadTimeout` at onset) — a cleaner
  full-outage window could page zero times. Trigger: same pass as above; needs a severity-posture decision
  (Seth-owned, Op Stds §3.1 territory).

  **2026-08-06 — BOTH gaps above RECURRED, and the "pages zero times" hypothetical actually happened.**
  A **12.7-hour** Smartsheet outage (10:23Z→23:04Z; HTTP 500 code 4000 + read timeouts; breaker OPEN
  **729 min**, 361 short-circuit observations across the fleet) froze every Smartsheet-backed daemon —
  the escalation text's own words: *"approved sends are FROZEN and nothing is being filed."* Outcome
  versus the two predictions:
  - **Durability gap (2nd occurrence):** the outage left **zero `ITS_Errors` CRITICAL rows** — the
    record leg could not write because Smartsheet *was* the outage. `~/its/logs/2026-08-06.log` is again
    the only forensic record. 07-15 lost 1,264 of 1,368 writes; 08-06 lost the incident entirely.
  - **Paging gap (worse than predicted):** 07-15 got one coincidental page. 08-06 got **zero**. The
    third leg was independently dead — Resend 403s on an unverified domain (see the
    `resend_client.DEFAULT_FROM` entry at the end of this file, severity raised there). Sentry was the
    lone surviving leg. **A 12.7-hour full-fleet freeze was invisible until a human went looking.**

  This is the two-outage pattern the 07-15 entry anticipated: the three legs are not independent under
  a Smartsheet outage (one leg *is* Smartsheet) and the remaining two can both be down without anyone
  noticing. Note the watchdog behaved correctly throughout — Check J WARNed at 450s (< its 600s
  threshold) on the 11:00Z sweep, and the daily cadence is exactly the "not real-time" limitation named
  above. **Trigger raised: this is now a twice-realized outage-blindness class, not a hypothetical.**
  Recovery required no intervention (breaker self-closed, all daemons resumed clean) — the defect is
  purely in *observability*, not recovery.
- **(operator/dev, recurring) — pytest runs during a live dev session pollute the LIVE dated error log,
  Keychain-adjacent state, and `~/its/state/*.lock` files.** `tests/conftest.py:138-141`'s stub creds
  (`test-{service}`) still make REAL network calls that 401/error, and `shared.error_log` appends those to
  the SAME dated log the live daemons write to — the exact mechanism that produced the DASH-5 false alarm.
  Confirmed **active/recurring** (re-fired again 2026-07-15 13:41Z, a full diagnosis session after the first
  occurrence). The existing conftest live-state write guard does not cover this class (tests also touch live
  `~/its/state/*.lock` via bare `open()`). Trigger: next test-infra hardening session — needs either fully
  mocked network boundaries in the integration-adjacent tests, or a distinct non-production log path for test
  runs.
- **(informational) — host timezone is EDT (UTC-4), not Pacific**, confirmed during the diagnosis; any
  PDT-based mtime/window math on this host is off by 3h. Not itself a bug, just a fact worth not re-deriving.

> **Audit 2026-07-24 (tech-debt janitorial pass):** Sub-item 3 (pytest pollution) RESOLVED by PR #637 (four conftest fixtures incl. the open()-hole). STILL OPEN: (1) ITS_Errors record-writes have no retry/queue — error_log.py loses the row on SmartsheetError (1,264 lost in the 07-15 outage); (2) watchdog Check J is daily-cadence, not real-time paging.

## PO-workspace accumulating ledgers have no row-cap/period-split watchdog [OPEN 2026-07-19, low]

The three PO-workspace **append-only ledger sheets** — `PO_Log`, `Subcontract_Log`, and
`Estimate_Log` (the ADR-0004 estimate importer's ledger) — grow monotonically with no
row-cap monitor anywhere: watchdog **Check O**'s `_ROTATION_POLICIES` covers only
`ITS_Errors` + `ITS_Review_Queue` (and rotation-by-delete would be WRONG for a SoR
ledger anyway), while the progress-reporting standing trackers already carry the
SoR-safe shape (`material_list.check_row_cap` WARN + operator period-split enqueue;
`hours_log`/`equipment_status` run `sheet_capacity.check_create_headroom`) — these
three writers have none (`grep row_cap|sheet_capacity po_log.py estimate_log.py
subcontract_log.py` = zero hits). At current volumes the Smartsheet ~20k/sheet cap is
years out (severity **low**), but past it `add_rows` fails and the ledger mirror goes
silently blind — the same failure class the 2026-07-13 `ITS_Errors` row-cap incident
proved out. Right fix is the `material_list` pattern replicated (WARN threshold +
Review-Queue period-split enqueue, NEVER delete), not a Check-O rotation policy.
**Trigger:** Phase 1.5 hardening pass (bucket with the other phase-1.5 items), or the
first ledger to cross ~15k rows. **Tag:** `po_materials`, `subcontracts`, `phase-1.5`.

## M365 client-secret expiry is undetected until it takes the whole send path down [OPEN 2026-07-24, high]

The Entra-ID app client secret behind `ITS_MS_CLIENT_SECRET` **expires** on a tenant-set
lifetime, and `shared/graph_client.py` is the sole transport for *every* external send —
safety WSR, progress WPR, PO, RFQ, subcontract. Nothing in ITS knows the expiry date:
`_get_token()` (`graph_client.py:191-193`) reads the three credentials from Keychain and
only discovers the expiry when the MSAL client-credentials grant starts failing — i.e. at
the moment of the first blocked send, across every send lane at once. PR #705 made all
three M365 credentials dashboard-rotatable, which gives the **Developer-Operator** a
console **repair** path (§44 v21.x rider — current-credential-gated self-rotation by the
holder; Class C is Developer-Operator-only and a Successor-Operator still escalates on
secrets/auth). What is missing is the **detection** path. The operator learns about the
expiry from a failed send, never before it.

Two gaps, both open:

- **No advance warning.** No watchdog check, no `ITS_Config` row recording the expiry
  date, no lead-time alert. The registry note added in #705 tells the operator to "record
  the expiry at seed time and calendar the rotation" — that is **narrated, not enforced**
  (Op Stds v21 §52), and it depends on a human remembering across a 6–24 month gap.
- **No distinguishable failure signal.** A Graph auth failure at expiry surfaces as
  whatever CRITICAL the calling send daemon happens to raise, so nothing names "the M365
  client secret expired" or "expires in N days". A Successor-Operator escalates on
  secrets/auth regardless, but today they cannot even tell Seth *what* broke — the
  escalation carries a generic send failure, not a diagnosis.

Candidate shapes, **not decided** — the documentation-and-alerting design is the open
question, not just the code: a `system.ms_client_secret_expires_at` ITS_Config row seeded
when the production Entra app is registered plus a watchdog check warning at T-30/T-14/T-7;
and/or a distinct `graph_auth_expired` error_code on the MSAL failure so the symptom is
greppable and runbook-linkable; plus the §43 runbook entry that pairs the symptom with the
now-existing console repair. The detection half is the load-bearing one — the repair half
already shipped.

**Trigger:** the cutover config-seed pass, or the Phase 1.5 hardening pass, whichever comes
first — the expiry date can only be captured when the production Entra app is registered, so
recording it is naturally a cutover step and is cheap to do then and expensive to reconstruct
later. **Tag:** `operator_dashboard`, `security`, `phase-1.5`.

## PO attachments (Feature B) — conscious deferrals [OPEN 2026-07-13]

From the Feature-B build (PO document attachments — the §34 doc-attachment pool → Mac screen →
Box pipeline). None blocks the ship; each is a deliberate scope line:

- **ATT-1 (doctrine-aligned) — VirusTotal (§34 Layer 4) not wired.** Op Stds §34 defers it to
  Phase 2+; `po_attach_screen` runs L1–L3 only (ClamAV config-gated OFF). Trigger: the Phase-2
  §34 hardening pass wires VT hash-lookup for BOTH photo_screen and po_attach_screen together.
- **ATT-2 (LOW) — encrypted OpenXML containers are not specifically classified.** A
  password-protected .docx/.xlsx either fails the zip walk (→ suspicious, refused-to-review) or
  walks by entry NAMES only (macro/executable name detection still holds) with content
  inspection impossible. Acceptable: the operator's own spec docs are not expected encrypted.
  Trigger: a real encrypted-attachment workflow.
- **ATT-3 (LOW) — attachments upload as ONE JSON request (≤10 MB decoded).** The Worker chunks
  into D1 rows server-side; there is no SPA-side chunked/resumable upload. Fine at the locked
  10 MB cap; a future cap raise needs an upload-session pattern (mirror filed_pdfs in reverse).
- **ATT-4 (BY DESIGN) — attachments on a PO canceled BEFORE filing are never screened/filed.**
  The internal pending route serves only FILED parents (pending_review+); a queued→canceled
  PO's attachment bytes sit in D1 until the prune's canceled-PO chunk hygiene (90d past
  updated_at) drops them. The byte-free rows remain as the forensic manifest. Revisit only if
  cancel volume makes 90d of latent bytes a real size concern (the prune's size tripwire now
  samples po_attachment_chunks).
- **ATT-5 (ACCEPTED LIMITATION, operator posture 2026-07-13) — the PDF active-content scan is
  blind to /ObjStm compressed object streams + compressed xref.** `po_attach_screen._scan_pdf`
  is a raw-byte marker scan (plus #xx name-escape normalization) — NOT a PDF parse. Markers
  inside flate-compressed object streams (the DEFAULT of modern PDF producers) are invisible
  to it, and we deliberately do NOT flag ObjStm-bearing PDFs (that is most legitimate modern
  PDFs — flooding the review queue would break the workflow) or build a deep parser. The
  operator's accepted posture: PO attachments are a limited-blast-radius, limited-access
  workflow — the real controls are that boundary + the optional ClamAV layer. The in-code
  honesty note lives on `PDF_ACTIVE_MARKERS`. Trigger: the Phase-2 §34 hardening pass (with
  ATT-1's VirusTotal), or a widening of who can upload.
- **ATT-6 (ACCEPTED LIMITATION, operator posture 2026-07-13) — OpenXML content-level vectors
  beyond macros/rels/OLE-parts are not inspected.** The zip walk now catches vbaProject.bin
  (malicious), nested executables (malicious), `TargetMode="External"` rels naming an
  attachedTemplate/oleObject (suspicious), and `embeddings/oleObject*.bin` parts (suspicious)
  — but DDE field codes inside document.xml (and other in-content constructs) are NOT parsed.
  Same limited-blast-radius rationale as ATT-5; the in-code note lives in the module docstring
  + `_scan_openxml`. Trigger: same as ATT-5.

## Subcontracts — SC-S3c adversarial-review follow-ups (non-blocking) [OPEN 2026-07-11]

From the SC-S3c verify phase (portal-worker-security-reviewer + ops-stds-enforcer + completeness critic;
all three verdicts CLEAN/WARN, no BLOCK). Deferred deliberately — none blocks the dark ship:

- **SC3c-1 (LOW, shared with PO) — supersede double-submit dup-guard is check-then-act, not atomic-in-WHERE.**
  `worker/subcontract.ts` `POST /:id/supersede` pre-`SELECT`s for an in-flight successor (`WHERE
  supersedes_sc_id=?1 AND status!='canceled'`) then acts — a tight double-click / replay by a
  `cap.subcontracts.manage` holder could mint two live successors for the same slot (each still passes
  its own SOV/HMAC/F22 gates, so it's a business-logic idempotency race, not an auth/money bypass; damage
  ceiling = a human cancels one draft). This is **verbatim inherited from `worker/po.ts:1113-1119`** — SC-S3c
  faithfully mirrors the reviewed PO pattern rather than diverging. **Fix belongs to BOTH** (fold the dup
  check into the clone `INSERT…SELECT`'s WHERE via `AND NOT EXISTS (SELECT 1 FROM <t> WHERE
  supersedes_*_id=?1 AND status!='canceled')`, then a post-insert SELECT only to disambiguate the 409
  message) — a shared po.ts+subcontract.ts change with its own PO re-review, out of SC-S3c's scope.
- **SC3c-2 (COSMETIC) — `migrations/0050_subcontracts.sql` header comment overstates a Worker gate.** It
  credits the Worker with asserting the §2.1 spelled-out price WORDS match the figure; that check is
  actually the Python render step (`subcontract_generate` via num2words), not a pre-queue Worker gate.
  Not a hole (the check exists in the pipeline), but a stale comment on a money/legal boundary. 0050 is a
  merged+applied migration — fix the comment only alongside a genuine 0050 touch (editing an applied
  migration file in isolation risks the migration-tracking / doc-currency sha).
- **SC3c-3 (LOW, forward-looking for SC-S4) — the SOV `.xlsx` Box file id is discarded.** `subcontract_poll`
  files both `.docx`+`.xlsx` but only tracks the `.docx` id as `box_file_id`; the `.xlsx` lives in Box under
  its deterministic `sc_xlsx_filename` with no ledger/D1 handle. Correct for S3c (the reviewer gets both via
  the inline attach); SC-S4's send — which will attach BOTH — must re-derive `sc_xlsx_filename` to locate it.
> **Cleanup 2026-08-07:** the **SC3c-4** bullet was deleted from this entry — its premise is dead at HEAD.
> It asserted `tests/test_daemon_scaffold.py` scoped the subprocess-AST guard to `safety_reports` only via a
> singular `DAEMON_ROOT`; `grep -rn "DAEMON_ROOT[^S]" --include="*.py"` now returns ZERO hits, and
> `tests/test_daemon_scaffold.py:39` defines `DAEMON_ROOTS` over all five daemon packages
> (`safety_reports`, `po_materials`, `subcontracts`, `progress_reports`, `field_ops`) with a comment naming
> the bullet's own ID. Left in place it read as a live capability-gate coverage hole on the PO/subcontract
> daemon surface — the worst class of stale entry, because it invites someone to "fix" a guard that already
> bites. SC3c-1/-2/-3 below remain valid.

## Subcontracts — PO/SC Configuration + builder follow-ups [OPEN 2026-07-12]

From the Office Operations nav / PO-SC Configuration session (PRs #541/#542/#546, plus the HELD PRs
#544/#548). None blocks the dark ship; PR-B2 below is the remaining operator-directed build item, not a
bug.

- **SC-CFG-1 (INFORMATIONAL, non-blocking) — `attach_reference.md` won't auto-flag as diverged if a
  future `standard_subcontract_v2` ever changes the preamble/§2.1 wording.** PR #544 (HELD for operator
  merge — touches ADR-0003 + the manifest description) fixed a real fence: an `attach`-kind terms profile
  (`negotiated_msa`) had no library text to load, so `render_body_text` raised and a valid negotiated-MSA
  subcontract could never file. Fix renders a one-page reference body from a new sha-pinned
  `subcontracts/terms/attach_reference.md` — PURE VERBATIM fragments lifted from the `standard_subcontract`
  body's preamble + §2.1 + signature block (an earlier draft with paraphrased/invented clauses was BLOCKED
  by ops-stds review and rewritten to pure-verbatim before re-review cleared it), so it correctly carries no
  independent legal-review gate of its own. **The residual:** `attach_reference.md` is pinned by its own
  `terms._ATTACH_REFERENCE_SHA256` module constant, frozen at v1-era wording. If `standard_subcontract` is
  ever bumped to a v2 with different preamble/§2.1 text, nothing re-checks `attach_reference.md` against the
  new wording — it just keeps rendering the frozen v1 fragments, consistent with the existing
  immutable-pin-per-version pattern elsewhere in the manifest, but silently so for this one file. **Trigger:**
  only relevant the day a `standard_subcontract_v2` is minted — worth an ADR-0003 note or a cross-check at
  that point, not before. **Tag:** `subcontracts`, `terms`, `legal-gate`, `informational`.
> **Cleanup 2026-08-07:** the **SC-CFG-2** bullet was deleted from this entry — its prescribed fix has been
> executed verbatim. It asked to "hoist `MAX_ADDRESS` into a shared Worker constants module and import it at
> all four call sites"; at HEAD `safety_portal/worker/constants.ts:10` reads
> `export const MAX_ADDRESS = 512;` and it is imported by `index.ts`, `po.ts`, `subcontract.ts`,
> `fieldops_job_write.ts` **and** `rfq.ts` (five sites, one more than the bullet anticipated). The only
> surviving `512` literals in the Worker are the constant's own definition and the comment recording the
> old duplicated state. This entry's 2026-07-24 audit note already recorded it resolved by PR #590, so the
> same item was reading as open in two places within one file.
- **PR-B2 (the remaining Exhibit-A + payment-terms build, operator-directed, NOT started) — Exhibit-A
  versioned+gated editing + subcontract payment-terms editing + a `config.ts` comment fix.** Mapped this
  session (Explore agent) as one LARGE, atomic Python+worker+SPA change, deliberately left for the operator's
  presence because it needs a worker deploy AND a Layer-A legal-attestation seed:
  1. Restructure `subcontracts/exhibit/manifest.json` `trade_templates` from flat `{file,sha256}` to
     versioned `{current_version, versions:{vN:{file,sha256,legal_review}}}` — requires seeding the 7
     existing trade templates `legal_review=cleared` (an operator Layer-A attestation, the same pattern used
     for `standard_subcontract` v1, `95a01cb`).
  2. `exhibit.py`'s loader + the `subcontract_docx` renderer's pin-resolution add a legal gate to the LIVE
     render path (currently exhibit.py has no such gate — a known WARN from PR #538's review, intentional at
     the time since Exhibit A is operator-authored per-trade Article II, not independently-drafted legal
     text like the standard body).
  3. `config_apply.py` gains `_apply_exhibit_*` handlers reusing the existing `add_version`/`set_current`/
     `create_profile` op shapes — no new D1 migration needed.
  4. `config_actuator.py`'s `_MANAGED_PATHS` + `_MANAGED_TERMS_DIRS` add `subcontracts/exhibit`.
  5. Worker `config.ts` gains an `exhibit` artifact kind + registry entry + a kind→op branch rework (new
     `EXHIBIT_OPS`); `worker/subcontract.ts` gains new serve routes (list template keys + get text by
     key/version) — **atomic with the manifest schema change**, because the worker build-imports
     `exhibitManifest` directly (the same "Worker bundles config at build time" constraint noted throughout
     this doc).
  6. `subcontracts.ts` SPA fetchers + a NEW exhibit-editor block in `PoConfigPage` — NOT the shared
     `TermsProfilesEditor` (exhibit is keyed per-trade, not per-profile).
  7. Payment-terms editing (CE-7 above) folds in here too, once the served `/api/subcontracts/config` route
     exposes the day-fields.
  **Trigger:** next dedicated subcontracts-config session, operator present for the deploy + the
  legal-attestation seed. **Tag:** `subcontracts`, `config-editor`, `exhibit-a`, `deploy-gated`,
  `legal-gate`, `not-started`.

> **Audit 2026-07-24 (tech-debt janitorial pass):** RESOLVED: SC-CFG-2 (MAX_ADDRESS=512 constant, PR #590) and PR-B2 items 1-6 (versioned/legal-gated Exhibit A, PR #552). STILL OPEN: SC-CFG-1 (awaits a standard_subcontract v2 mint) and PR-B2 item 7 / CE-7 (subcontract payment-terms day-field editing).

## Config editor (§50) — deferred follow-ups [OPEN 2026-07-10]

From the slice-2 (`config_actuator`) build + adversarial review (PR #509):

> **Cleanup 2026-08-07:** the **CE-1 / CE-2 / CE-3 / CE-6** bullets were deleted from this entry — all
> four were already recorded RESOLVED in their own text, and CE-1's closing line literally asked for this
> sweep ("Sweep to `tech_debt_closed.md` in the follow-up doc-hygiene pass"), which never happened. Re-verified
> at HEAD before removal: CE-1 `safety_reports/publish_daemon.py:583` `reason = redact(reason[:1800])`;
> CE-2 `po_materials/terms.py:104` refuses a version whose `legal_review != "cleared"`; CE-3
> `tests/test_config_apply.py:30` `SEED_CONFIG_VERSION = 5` with version asserts made RELATIVE to the seed;
> CE-6 a grep of `docs/enablement/purchase_orders.md` for the old "read-only"/"not a portal edit" phrasing
> returns empty. ~75 lines of closed narrative were burying the three genuinely-open bullets below — of which
> **CE-5 matters**: it is a MEDIUM pre-activation §44 decision (who may attest legal clearance) gating
> `po_materials.config_actuator.polling_enabled`, and it deserves to be readable without scrolling past four
> finished items.
- **CE-4 (LOW, out of scope of CE-3's fix) — `po.test.ts`'s `draftBody` hard-codes `ship_to_state:"IL"`.**
  Flagged by PR #514 as a known residual: CE-3's fix makes the test track a tax-RATE edit to IL (or an
  additional state) correctly, but a tax edit that **removes or renames the IL entry entirely** would still
  break `po.test.ts`, because `draftBody` assumes an IL ship-to unconditionally. Pre-existing, not introduced
  by PR #514. Low real-world risk while IL is the only active job state. **Trigger:** revisit if/when a
  second ship-to state goes live, or the next time `po.test.ts` is touched for an unrelated reason. **Tag:**
  `po_materials`, `config-editor`, `ci`, `low-severity`.
- **CE-5 (MEDIUM, pre-activation decision) — terms "Make a version current" attests legal clearance; the
  attesting population isn't yet decided.** Terms editing shipped in two slices this session (T1 #518 —
  edit-text pre-fill; T2 #520 — make-current + the Layer-A `legal_review != "cleared"` render-side refusal,
  **CE-2 RESOLVED**). The portal's confirmable "Make a version current" control (`cap.po.manage`) both clears
  `legal_review` and advances `current_version` in one action — i.e. checking that box IS the legal
  attestation ("I've reviewed this version's legal text"). `docs/runbooks/config_actuator.md` and this
  session's memory keep that judgment a FIXED §44 high-class call (Seth/legal, training-enforced, never a
  Tier-2 flip) — but the control itself only checks `cap.po.manage`, not a narrower "is this person actually
  Seth or legal" capability. **Decide before activation:** whether any `cap.po.manage` holder may attest, or
  whether the control needs a narrower capability / a second confirmation step. **Trigger:** before flipping
  `po_materials.config_actuator.polling_enabled` live for terms editing (the editor as a whole is already
  gated on this flag; this is a use-of-capability question, not a code gap). **Tag:** `po_materials`,
  `config-editor`, `terms`, `authorization`, `pre-activation`.
- **CE-7 (LOW, blocks a SPA feature not a live edit) — subcontract payment-terms editing deferred to
  PR-B2: the actuator needs day-fields the served config doesn't expose yet.** PR #546 ("PO/SC
  Configuration — subcontract Contractor + terms editors (v1)") built the Contractor identity editor + the
  extracted shared `TermsProfilesEditor` for subcontracts, but deliberately left payment-terms editing
  unbuilt: `po_materials/config_apply._apply_payment_terms_edit` (the actuator handler — it is workstream-
  generic, not subcontracts-specific, despite living under `po_materials/`) validates+writes
  `application_for_payment_day` / `progress_payment_day` (`_bp(..., 1, 31)`), but the served
  `/api/subcontracts/config` route does not yet expose those fields to the SPA. Building the editor now
  would let the operator POST a payload the actuator can validate but the SPA can't pre-fill/round-trip
  correctly (no source of truth for the current values). **Fix:** extend the subcontracts-config Worker
  route to serve the two day-fields (small, deploy-gated — the same "Worker bundles config at build time"
  pattern as purchaser/tax/terms), then build the SPA editor. Folds into **PR-B2** alongside Exhibit-A
  versioned+gated editing (see the Subcontracts — PO/SC Configuration section below for the full PR-B2
  scope). **Tag:** `subcontracts`, `config-editor`, `deferred`, `low-severity`.

## Smartsheet-wiring audit findings — daemon-health + capacity hygiene [OPEN 2026-07-04]

From `docs/audits/2026-07-04_smartsheet-wiring-audit.md` (Task B — the SoR is wired correctly; these are hygiene/observability items, **no correctness breaks**):
- **M-1 (MEDIUM) — RESOLVED 2026-07-06:** `smartsheet.sheet_count_ceiling`=1500 / `_margin`=50 seeded as **explicit** `ITS_Config` rows (Workstream `global`), closing the silent-hardcoded-default gap (forensic class #7). Operator confirmed **Business plan — not limit-constrained** (upgrade if approached), so the values stay at the conservative advisory default (the guard WARNs but never blocks a create; won't fire until ~240 jobs); the true per-workspace cap isn't Smartsheet-API-exposed. Each row carries a tuning note in its Description. Raise if it ever false-WARNs.
- **M-2 (MEDIUM) — RESOLVED 2026-07-06 (stale claim):** inspected the LIVE `ITS_Daemon_Health` sheet before any delete — it holds **exactly the 6 healthy self-provisioning daemon rows** (fieldops_sync, portal_poll, publish_daemon, compile_now_poll, weekly_send_poll, progress_send_poll), all reporting `OK`. The 5 stale placeholder rows this entry described are **already gone** — nothing to delete. (Good instance of "trust the live state, never the claim": name-guarded inspection found the cleanup was already done, avoiding a delete against a live daemon's row.) The `watchdog`/`shared.picklist_sync` self-report-vs-external-monitor question (S-2) is moot — neither has a stale row.
- **M-3 (LOW) — CLOSED 2026-07-05 (PR #473, `86bfab0a`, four-part verify CLEAN: state=MERGED, mergedAt non-null, mergeCommit present, main-branch CI on the merge commit = SUCCESS):** `fieldops_sync` heartbeat interval mismatch — `SYNC_INTERVAL_SECONDS` set 300→90 to match launchd `StartInterval=90` (`install.sh:79`); feeds the daemon-health cadence.
- **S-1 (systemic) — MECHANISM DONE 2026-07-06 (#481 `c04f4cd`, four-part verify CLEAN):** the tracked `REQUIRED_CONFIG` startup-logging pass (#336) is BUILT — `shared/required_config.py` + `resolve_and_log` wired into ALL daemons; each declares a module-level `REQUIRED_CONFIG`; a missing declared row now WARNs `config_row_missing` **distinctly** (no longer silent) and each resolved setting logs its source; the §52 `narrated_controls` ledger entry `required_config_observable_resolution` flipped `dated_exception`→`enforced`. Residual (OPEN): the two named cross-workstream footgun rows still must be SEEDED correctly (unchanged); the shared `sheet_capacity` global keys are a documented carve-out (a bounded follow-up — see the `required_config.py` docstring).

**Tag:** `smartsheet`, `daemon-health`, `config`, `capacity`, `audit`, `field_ops`.

> **Audit 2026-07-24 (tech-debt janitorial pass):** RESOLVED: M-1 (seeded rows), M-2 (stale claim), M-3 (PR #473), S-1 mechanism (PR #481 / issue #336). STILL OPEN: shared/sheet_capacity.py _read_int_setting still does a bare silent-fallback (no observable-config WARN + source logging) for smartsheet.sheet_count_ceiling / _margin.

## `/pending-jobs` transport flakiness — deeper cause untraced, only blast-radius mitigated [OPEN 2026-07-05]

**PR #469 (`466e1e8`) fixed the SYMPTOM, not the root cause.** The live bug ("logged time not
showing" in the Hours Log) traced to `fieldops_sync._sync_inside_lock` returning early whenever
`GET /api/internal/fieldops/pending-jobs` raised a `PortalTransportError` — starving the independent
hours/equipment/material-list mirror passes on any cycle where the job-queue fetch happened to fail.
#469 **decouples** the passes (a transient job-fetch failure no longer blocks the others) and adds a
Check-Q-style sustained-outage escalation, but it never diagnosed **why `/pending-jobs` fails
intermittently in the first place**. No live failure has been captured with its actual HTTP status
code or response body — the daemon logs only that a `PortalTransportError` was raised, not what the
Worker actually returned.

**Suspected causes (unconfirmed):** Cloudflare bot-fight-mode / WAF challenging the daemon's
server-to-server bearer-token request (no browser fingerprint, no cookie jar — a classic false-positive
shape for bot mitigation), or a transient Worker-side D1 query error/timeout unrelated to Cloudflare's
edge. Both are plausible; neither has evidence yet.

**Fix:** on the next observed transient failure, log the actual status code + a truncated response
body at WARN (currently swallowed into a generic `PortalTransportError`); cross-check the Cloudflare
dashboard's bot-fight/WAF event log for the `/api/internal/fieldops/pending-jobs` route during the
failure window. If confirmed Cloudflare-side, the fix is a WAF allowlist rule scoped to the daemon's
bearer-token header pattern (never widen the allowlist to all traffic). If Worker-side, escalate as a
D1 query-shape issue.

**Tag:** `field_ops`, `fieldops_sync`, `transport`, `cloudflare`, `diagnose`.

## Remove the progress-% estimate system-wide [OPEN 2026-07-06 — SPA+route done; code-cleanup + column-drop DEFERRED as operator-reviewed]

**Ready spec (verified against live main 2026-07-06; all refs = the `jobs.progress` %-estimate, NOT the sync-mirror `progress`/`progress_report`/`progress_contact`):** the SPA slider/bar + the `POST /:job_id/progress` route/handler are already gone (#403, 2026-07-03); the client create call no longer sends `progress`. **Remaining:** (1) `worker/fieldops_job_write.ts` — stop honoring `body.progress`: delete `const progress` (~L171) + the `clampPct` helper (~L46), bind `0` in the INSERT (~L238, keeps the column/shape → no positional renumber); (2) dead read surfaces (zero consumers): `worker/wire-types.ts` `JobRow.progress` (~L50) + `JobDetail.progress` (~L133), `worker/fieldops_jobtracker.ts` the two `SELECT j.progress` (~L48/L162) + row types (~L64/L173) + response maps (~L136/L338); (3) `src/lib/fieldops_jobtracker.ts` `progress?: number` in the createJob body type (~L107); (4) `src/lib/errorCopy.ts` dead `invalid_progress` (~L95); (5) `test/fieldops-job-write.test.ts` drop the `progress: 40` create + change the assert to `toBe(0)`. **DEFERRED (2026-07-06):** touches the worker CREATE-route INSERT (a trust boundary — `portal-worker-security-reviewer` DoD) + the destructive `ALTER TABLE jobs DROP COLUMN progress` (`0014`) migration is deploy-coupled; dead-code removal on a `NOT NULL DEFAULT 0` dormant column is low-value / moderate-risk, so it's parked for a supervised worker-reviewed PR rather than an autonomous one (the column is harmless left in place). Original note below.

**Operator-locked 2026-07-01: the `jobs.progress` %-complete estimate is a misleading single-value guess and should be removed EVERYWHERE, not just omitted from the P6 rollup** (P6 already excludes it). A **multi-surface** removal — enumerate ALL consumers first (the multi-surface fan-out discipline):
- ~~SPA: the progress bar / slider control in the Job Tracker~~ — DONE (#403 removed the UI; the `setJobProgress` client fn deleted R4-F5).
- ~~Worker: `POST /api/fieldops/job/:job_id/progress` route (`fieldops_job_write.ts`)~~ — DONE 2026-07-03 (deleted with the B3 dead-route approval; tombstone in `fieldops_job_write.ts`). Still remaining: `progress` in the create body (accepted, default 0).
- D1: the `jobs.progress` column (`0014`) — leave the column vs. drop via migration (decide; a drop needs care).
- Any read route/response surfacing `progress`.
Grep `progress` across worker + SPA and distinguish `jobs.progress` (the %-estimate to remove) from the unrelated `sync_state` mirror progress. **Tag:** `field_ops`, `job-tracker`, `cleanup`, `multi-surface`.

## P2.5 Slice 6 — portal-owned canonical number: residual redundancy [OPEN 2026-06-30]

**Slice 6 (P2.5 revision).** The portal now ASSIGNS the canonical `JOB-######` (worker `job_counter`, migration 0022) and writes it as BOTH `job_id` and `canonical_job_id` from birth; `active_jobs_writer` writes it into the Smartsheet "Job ID" column (retyped AUTO_NUMBER → TEXT at cutover). Two deliberate §14-preservation leftovers — both harmless, both candidates for a later cleanup:

1. **`Portal Job Key` column == `Job ID`.** Both Active-Jobs columns now carry the identical `JOB-######`. The daemon's find-or-create still keys on Portal Job Key (unchanged, tested), so the column is redundant-but-load-bearing. A future simplification could drop Portal Job Key and key find-or-create on Job ID directly (and drop the `active_jobs.get_job` second-loop fallback) — deferred to avoid churn on a working, reviewed path.
2. **`canonical_job_id` mirror machinery is now always-set.** The down-sync canonical-aware pre-pass (`index.ts`) and the `jobs-mark-mirrored` `COALESCE(?4, canonical_job_id)` were built for the old NULL-until-read-back model; with canonical set at birth they are idempotent no-ops, not removed (they still correctly fence portal jobs off the smartsheet down-sweep).

**Revisit when:** a later slice consolidates the identity columns, or the canonical machinery is otherwise touched. **Tag:** `field_ops`, `job-tracker`, `smartsheet-upsync`, `preservation`.

## P2.5 job-tracker up-sync — fast-follows [OPEN 2026-06-30, updated 2026-07-01]

**P2.5 (PRs #383–#387).** The job-tracker → Smartsheet up-sync (`field_ops/fieldops_sync.py` + `shared/active_jobs_writer.py`) landed with six tracked, non-blocking follow-ups. **P2.5 cut over LIVE 2026-07-01** (`sync_enabled=true`; JOB-000017 confirmed mirrored to both Active-Jobs sheets); three of the six items closed same-day (#397, #400):

1. **`_ENROLLMENT_SUFFIXES += "_sync.py"` — still OPEN, re-evaluated and deliberately deferred again by FF5 (PR #400, 2026-07-01).** Adding the `_sync.py` suffix to the capability-gating enrollment list cascades and flags the pre-existing `shared/picklist_sync.py` as unenrolled (breaking the meta-test). Correct fix order: enroll `picklist_sync.py` in the appropriate gating list FIRST (separate PR), then add the `_sync.py` suffix. `tests/test_capability_gating.py` carries the revert note.
2. **Watchdog Check-C `fieldops_sync` slug not wired — RESOLVED by PR #397 (2026-07-01).** `fieldops_sync` now writes its freshness marker into `scripts/watchdog.py` `TRACKED_JOBS` (8-min staleness window, mirroring the `safety_compile_now_poll` 90s→8-min pattern). Verified FRESH against the live daemon post-cutover.
3. **`_route_to_review` partial-commit context — RESOLVED by PR #400 FF-B (2026-07-01).** A per-job fence now records `mirrored_safety` + `failed_sheet` in the Review-Queue payload, so the operator can tell from the row alone whether the failure was pre- or post-safety-write.
4. **Re-find-after-create race-dup hardening — still OPEN, re-evaluated and deliberately deferred again by FF5 (PR #400, 2026-07-01).** `active_jobs_writer.upsert_job`'s find-or-create has the same find-after-create race as `week_folder` (two near-simultaneous cycles could create two rows for one Portal Job Key). FF5 judged it hard-to-hit (single-host, serialized daemon) and idempotent (a duplicate row is a nuisance, not a correctness break) — skipped again in favor of the higher-value 401-severity + partial-commit fixes in the same PR. Tracked for symmetry with the `week_folder` entry.
5. **401-on-mark-mirrored severity — RESOLVED by PR #400 FF-A (2026-07-01).** A 401 on `mark_fieldops_jobs_mirrored` now raises `PortalAuthError` (a `PortalTransportError` subclass) through an earlier explicit `except` clause → CRITICAL `fieldops_mark_mirrored_unauthorized`, instead of falling into the generic transient-retry clause. Matches the pending-jobs 401 posture already used elsewhere.
6. **JOB-1042 placeholder UX nit — RESOLVED by Slice 6.** The Job-ID input was removed entirely (the portal now assigns the number on create), so the placeholder no longer exists.

**Revisit when:** items 1 and 4 (both OPEN) — item 1 when `picklist_sync.py`'s capability-gating enrollment is separately addressed; item 4 opportunistically, or if a live near-simultaneous-cycle duplicate is ever observed. **Tag:** `field_ops`, `job-tracker`, `smartsheet-upsync`, `watchdog`, `capability-gating`.

> **Audit 2026-07-24 (tech-debt janitorial pass):** RESOLVED: fast-follows 2, 3, 5, 6. STILL OPEN: item 1 (shared/picklist_sync not enrolled in test_capability_gating _ENROLLMENT_SUFFIXES) and item 4 (active_jobs find-after-create race).

## Progress (and safety) no-recipient HELD surfaces a record, not an operator page [OPEN 2026-06-30]

**P5 (PR #380).** `shared/recipient_health.report_unhealthy_recipient` files an `ITS_Review_Queue` RECORD on a no-recipient HELD (visible in the operator review queue; watchdog Check A WARNs if it sits past 2× SLA; watchdog Check T WARNs on a HELD older than 24h). It deliberately does **not** fire an operator PAGE — per Op Stds §3.1 the only §3.1-compliant push leg `alert_dedupe` may gate is a `Severity.CRITICAL`, and a missing-contact config issue was judged not CRITICAL-class (consistent with `_mark_held`'s existing WARN treatment of HELDs).

**Revisit when:** the operator decides a blocked customer-facing weekly send warrants an active page rather than a queue item — at which point add a dedicated CRITICAL push leg (a Send-Gate severity-posture decision, Seth-owned). **Tag:** `progress_reports`, `safety_reports`, `external-send-gate`.

## `hours_log.find_entry_row` does a full client-side scan of the sheet on every upsert/supersede call [OPEN 2026-07-04]

**P7 Slice 1 (exec PR #461).** `progress_reports/hours_log.find_entry_row(sheet_id, entry_uuid)` calls `smartsheet_client.get_rows(sheet_id)` (fetches every row in the sheet) and then scans client-side for the matching `Entry UUID`. It is the dedupe/amend-resolution authority for both `upsert_entry_row` (idempotent re-mirror safety) and `supersede_entry_row` (amend chains), so it runs at least once per pending time entry, every `fieldops_sync` cycle. Per Op Stds §51 design, this is a **standing, append-only, never-deleted** sheet — the exact accumulating shape the A5 row-cap watchdog exists to bound at ~20k rows. A full-sheet fetch-and-scan per entry is O(sheet size) per call, meaning per-cycle cost grows linearly with the sheet's lifetime total, not with the cycle's actual workload — the daemon accumulates a heavier cycle every day the job stays open, well before the row-cap watchdog itself would fire.

Not urgent today (a new job's Hours Log starts empty and low-volume by design — a handful of entries/day), but it is the first §51 accumulating-log write path built this way; the same shape will recur in the P7 Equipment/Materials mirror passes. Two independent fixes available when it bites: (a) cache the sheet's UUID→row-id map in daemon-local state between cycles (invalidate on a miss, re-fetch full); (b) if Smartsheet's `get_rows` gains column-value filtering in a future SDK, filter server-side instead of client-side. Neither is built.

**Tag:** `progress_reports`, `field_ops`, `smartsheet-upsync`, `p7`, `scaling`, `§51`. **Revisit when:** a live Hours Log sheet is observed taking a materially longer `fieldops_sync` cycle, or before onboarding a job with a crew large enough to make per-cycle entry volume nontrivial (a 20-job cutover is the named scale point in the 2026-06-28 20×20 eval).

## weekly_send upload-session threshold = 2.5 MB (heuristic, not measured) [OPEN 2026-06-12]

**PR-3 (photo workstream tail).** `weekly_send` now switches transport by compiled-packet size: `≤ UPLOAD_SESSION_THRESHOLD_BYTES` (2.5 MB) sends **inline** via `graph_client.send_mail` (one request, base64-inline); `>` it sends via the Graph **upload-session** (`graph_client.send_mail_large_attachment` — draft → chunked PUT honoring `nextExpectedRanges` → send). The threshold is a **heuristic**: Graph's inline `/sendMail` ceiling is ~3 MB, and base64 inflates the payload ~33% plus message-envelope overhead, so 2.5 MB raw leaves headroom below the wire limit. It was **not** empirically measured against the live Graph tenant — the exact inline-reject boundary (and whether it counts raw or base64 bytes) is unverified. Low risk because the upload-session path is correct for ANY size 3–150 MB, so a too-low threshold just sends some sendable-inline packets the (slightly slower) chunked way; a too-high threshold is the only real failure (an inline send that Graph rejects ~3 MB → FAILED + retry, never a silent drop).

**Tag:** `safety-reports`, `graph`, `send-gate`, `threshold-heuristic`. **Revisit when:** the first live photo-bearing packet crosses ~2.5 MB (confirm the inline/upload boundary against the real tenant and tune the constant), or a `weekly_send.graph_error` retry cluster appears on packets near 3 MB.

## R2 upgrade path for portal photo transport (deferred) [OPEN 2026-06-12]

**PR-3 / cross-ref [ADR-0001](adr/0001-portal-photo-transport-d1-vs-r2.md).** Site photos ride **D1-inline base64** today (owner decision 2026-06-12) — simplest transport within the current ≤8 × 400 KB per-submission budget, and it keeps the Worker a send-free queue holding no documents. The recorded **upgrade path is Cloudflare R2** (object storage; D1 carries only the object key, the Mac fetches bytes at screen time), to be adopted when **field crews need > 4 full-res photos per field** (or the per-submission photo budget is raised past what D1-inline base64 carries within the Worker body bound). Deferred because R2 means provisioning a second storage plane, an object-key scheme, lifecycle/expiry, and a Mac access path — non-trivial and unneeded at the current budget.

**Tag:** `safety-portal`, `photo`, `r2`, `transport`, `adr`. **Revisit when:** the > 4-full-res-photos-per-field trigger fires, or the Worker body bound blocks a needed photo-budget increase. See ADR-0001 for the full decision + consequences.

## weekly_send upload-session chunk-retry hardening (deferred) [OPEN 2026-06-12]

**PR-3.** `graph_client._put_upload_chunk` mirrors `_request`'s retry shape (429/503 back off + retry; a hang fails fast as `GraphTimeoutError` without consuming the budget) and the chunk loop **honors `nextExpectedRanges`** so an interrupted transfer *can* resume to a server-reported offset within a single call. What is **deferred**: (a) no **session-resume across `send_one_row` calls** — a chunk failure that escapes the retry budget aborts the whole upload (the draft is left UNSENT in Drafts, fail-toward-not-sending), and the next poll cycle re-creates a fresh draft from byte 0 rather than resuming the prior `uploadUrl`; (b) no **explicit upload-session cancel** (`DELETE uploadUrl`) on abort — the abandoned draft + session simply expire (Graph TTL); (c) the anti-stall guard forces linear progress if a 200 body reports a non-advancing range rather than retrying the same range. Acceptable because a 3–150 MB packet uploads in a handful of chunks, restart-from-zero is cheap at that size, and the External Send Gate is unaffected (a failed upload never sends a partial packet).

**Tag:** `safety-reports`, `graph`, `upload-session`, `retry`. **Revisit when:** live telemetry shows recurring mid-upload failures on large packets (then add cross-cycle session resume + an explicit cancel), or packet sizes grow toward the 150 MB ceiling where restart-from-zero becomes expensive.

## Smartsheet API constraint: column FORMAT must be set via model attribute, not dict constructor [OPEN 2026-06-07]

**Verified live (PR #187, 2026-06-07).** When using the Smartsheet Python SDK to create or update a column, the column **format string** (font, size, bold, color, etc.) must be assigned via the model **attribute** (`column.format = "..."`) — passing `format` as a key in the dict constructor (`smartsheet.models.Column({"format": "..."})`) silently drops the value. Column **width** works via either path (dict or attribute). The same per-cell format DOES work via the `Cell` dict constructor (`_resolve_cells` attaches it via the `_formats` meta-key extension).

**Palette index source:** `GET /2.0/serverinfo` → `.formats.color` (array, index → hex). Verified live: 38 = `#237F2E` (dark green), 7 = `#E7F5E9` (light green), 18 = `#E5E5E5` (gray). `dateFormat` enum at `.formats.dateFormat`. Format-descriptor positions: 2=bold, 8=textColor, 9=backgroundColor, 16=dateFormat.

**Impact:** code that sets a column format via the dict constructor silently succeeds (200) but the column stays unformatted. Always use the attribute path for column format.

**Tag:** `smartsheet`, `sdk-vs-live`, `styling`. **Revisit when:** any new column-format code; `smartsheet_client.apply_column_styles` already uses the attribute path.

## Safety Portal — `scheduled_send_local` not seeded + silent fail-open on malformed value [OPEN 2026-06-08]

`safety_reports.weekly_send.scheduled_send_local` (ITS_Config; e.g. `"MON 07:00"` — the Pacific weekday/time window in which `Approve for Scheduled Send` rows dispatch) is read live each cycle by `weekly_send_poll._read_str_setting` → `_parse_scheduled_spec` → `_is_scheduled_window`. Two minor gaps: (1) it is **not** in `scripts/seed_its_config.py` (added manually to the mirror) — a fresh tenant build would lack the row and fall back to the `DEFAULT_SCHEDULED_SEND_LOCAL = "MON 07:00"` constant (functionally safe, but undocumented in the seeder). (2) `_parse_scheduled_spec` **silently** coerces any malformed value (bad weekday, bad time, empty) to `(MON, 07:00)` with **no log** — an operator typo'd window would quietly send Monday 07:00 instead of erroring. The fallback is intentional + tested (`test_parse_scheduled_spec_defaults_on_malformed`), but it's a quiet-failure footgun for an operator-tuned schedule.

**Proposed fix:** (a) add the row to `seed_its_config.py`; (b) WARN-log to ITS_Errors when `_parse_scheduled_spec` hits the `except` branch (still fall back, but surface the bad value). ~30 min. **Revisit when:** next seeder pass or weekly_send hardening. Surfaced 2026-06-08 (operator asked to confirm the config-driven schedule during mirror activation).

## `smartsheet-python-sdk` upper-bound pin (CI-break stopgap) [OPEN 2026-06-08]

`pyproject.toml` now pins `smartsheet-python-sdk>=3.0.0,<3.10.0`. A release >3.9.0 (2026-06-08) dropped/moved `smartsheet.exceptions`, which `shared/smartsheet_client.py:46` imports (`import smartsheet.exceptions as sdk_exc`) — the previously-unpinned `>=3.0.0` let CI fresh-install the broken version and **all 48 test modules failed at collection** (`ModuleNotFoundError: No module named 'smartsheet.exceptions'`). main was last green at `d393ee6` (2026-06-07 19:35); the breaking SDK release landed after. Local + every prior green CI run used 3.9.0 (which has `smartsheet.exceptions`).

**Stopgap (PR #192):** upper-bound `<3.10.0` keeps CI on a working SDK. Caps below 3.10 (the lowest possible breaker) rather than `<4.0.0`, since a minor *or* major could be the one that dropped the module.

**Proper fix (deferred):** verify the newer SDK's exception surface, then either (a) update `shared/smartsheet_client.py`'s import to the new location and loosen the bound, or (b) make the import resilient (try/except across the old/new path). ~1 hr. **Revisit when:** next dependency-maintenance pass, or when a smartsheet SDK feature/security update is wanted.

## Pre-mirror-tree portal Box filings are sandbox orphans [OPEN 2026-06-07]

**Mirror root activated 2026-06-08** — `safety_reports.box.portal_root_folder_id = 388017263015` (`ITS_Safety_Portal`) seeded in ITS_Config; new submissions now file to `ROOT → per-job → per-week`. The 3 submissions filed BEFORE activation (to the legacy tree) are confirmed orphans; left as-is (sandbox), per below.

PR-K mirrors the Smartsheet schema in Box (`ROOT → per-job → per-week → PDFs`),
replacing the legacy `project_routing` → category-subfolder layout for the portal
path. Submissions filed BEFORE the operator activates the mirror tree (sets
`safety_naming.CFG_BOX_PORTAL_ROOT`) live under the old category subfolders (e.g.
`Bradley 1 ▸ … ▸ 05. Tool Box Talks`). These are **pre-launch sandbox orphans** — no
migration is provided (validation-tenant data, pre-customer-1). Box keeps both; the
mirror tree simply files NEW submissions into the new tree once activated.

**Repair:** none required (sandbox). At a real cutover, decide per-customer whether
to leave or hand-move the handful of pre-activation PDFs. **Revisit when:** the Box
root is activated for a live customer tenant.

## Watchdog/launchd hang-killer: hard-kill a daemon exceeding N× expected cycle duration [OPEN 2026-06-02]

Fix part (c) carved out of the now-closed graph_client-timeout entry. The graph + (future) box timeouts convert *known* network surfaces' hangs into finite errors, but a hang from any *other* cause (a future un-timed call, a CPU spin, a deadlock) still defeats the launchd one-shot-per-interval model: the hung process holds the fcntl lock and every later interval no-ops on `poll_lock_held`. Check C's marker-staleness floor only **detects** this (after the staleness window); it does not **recover** it (the 2026-06-02 incident needed a manual `launchctl kickstart -k`).

**Proposed fix:** a watchdog (or a launchd `ExitTimeOut` / wrapper) that hard-kills a daemon process whose elapsed wall time exceeds N× its expected cycle duration, so the next interval can re-acquire the lock and self-heal. Larger design decision (where the kill lives, how to size N per daemon, interaction with legitimately-long cycles) — its own item.

**Phase target:** 1.4/1.5 reliability — the recovery complement to Check C's detection.

Surfaced: 2026-06-02 A2 graph_client timeout work (the indefinite-hang incident motivated detection→recovery, not just per-call timeouts).

## Conftest mock surface coverage [OPEN 2026-05-23]

`tests/conftest.py` (PR #74) autouse-mocks `shared.keychain.get_secret` and `shared.kill_switch.check_system_state`. The keychain mock at the source attribute covers all 7 credentialed surfaces transitively (smartsheet_client / graph_client / box_client / resend_client / sentry_client / anthropic_client / alert_dedupe). Two opt-out lists guard test files that exercise these surfaces directly (`test_keychain.py` + `test_helpers.py` for keychain; `test_kill_switch.py` for kill_switch).

Latent risk: future credentialed surfaces (a new client wrapper for a new external service) might need parallel opt-outs if a corresponding `tests/test_<service>_client.py` lands. Action trigger: any new Linux-CI failure with a `*Error: macOS-only` signature, OR a CI-fix follow-on PR that adds a fixture beyond the keychain + kill_switch pair, OR a new credentialed client module added to `shared/`.

**Revisit when:** next CI-hygiene pass, or any of the above triggers.

## Structural fix: lazy keychain loading + DI-injected kill_switch [OPEN 2026-05-23]

The conftest fix (PR #74) closes the immediate CI hole. A durable structural fix would:

- `shared/smartsheet_client.py::_get_client` — defer the `keychain.get_secret("ITS_SMARTSHEET_TOKEN")` call from build time to first-API-call time, so a test that never makes a real network call never hits the keychain.
- `shared/kill_switch.py` — accept a `get_setting` callable via dependency injection (with the module-level `smartsheet_client.get_setting` as default), so tests can inject without monkeypatching the source module.

Both are non-trivial refactors with cross-call-site impact. Deferred from PR #74 to keep scope focused on the CI fix. Trigger: next session that touches either module for an unrelated reason, fold the refactor in.

**Revisit when:** smartsheet_client or kill_switch refactor session lands.

## Smartsheet API constraint: DATETIME columns require system column type [OPEN]

Discovered 2026-05-17 evening while provisioning `ITS_Errors`, `ITS_Quarantine`, and other sheets. The Smartsheet "Create Sheet" endpoint accepts `DATETIME` columns only when paired with `systemColumnType: MODIFIED_DATE | CREATED_DATE`. User-defined DATETIME columns (e.g., "Timestamp", "Surfaced At", "Resolved At", "Received At", "Reviewed At") are rejected with a generic HTTP 500 / error code 4000 and no descriptive message.

**Workaround:** Use `DATE` for all user-defined date columns. Time-of-day precision is lost from the in-sheet representation.

**Mitigation:** Smartsheet's intrinsic row-level `created_at` (and `modified_at`) attributes are full datetimes and are queryable via the API. Code-side ordering and time-of-day inspection use those fields rather than the in-sheet DATE columns. The in-sheet DATE columns serve human readability; the intrinsic timestamps serve programmatic precision.

**Revisit when:** Smartsheet API surfaces user-editable DATETIME columns, or a workstream finds DATE-only resolution genuinely insufficient and the `created_at` fallback isn't viable for the use case.

_Update 2026-06-09 (PR #245 WSR Approved At / Sent At sweep):_ `ABSTRACT_DATETIME` (the "Date/Time" user type in the Smartsheet UI) **CAN** be created/retyped to via `update_column` and accepts a **naive** `YYYY-MM-DDTHH:MM:SS` value (stored/displayed literally). A plain `DATETIME` column is still rejected with errorCode 4000 — that restriction stands. `ABSTRACT_DATETIME` rejects any offset or 'Z' suffix (errorCode 5536). Existing DATE-only cells coerce to midnight on retype to ABSTRACT_DATETIME. The `WSR_human_review` sheet (id `5035670127988612`) columns "Approved At" (col `7944658226548612`) and "Sent At" (col `5129908459442052`) were live-retyped DATE → ABSTRACT_DATETIME, confirming the above. Write naive Pacific wall-clock (operator preference).

## Smartsheet API constraint: AUTO_NUMBER columns rejected at sheet creation [OPEN]

Discovered same session. `systemColumnType: AUTO_NUMBER` is rejected at the "Create Sheet" endpoint, whether or not the column is primary, with or without an `autoNumberFormat` config. Other system column types (`MODIFIED_DATE`, `MODIFIED_BY`) are accepted in the same payload — so the rejection is specific to AUTO_NUMBER, not a generic system-column-at-create issue.

**Workaround:** Each system sheet's primary column is a plain `TEXT_NUMBER` that code populates with a descriptive label ("Error", "Quarantined Message", "Entry"). Smartsheet's intrinsic row IDs serve as the unique identity for any code-side references.

**Mitigation:** Code-side row references use the Smartsheet row ID (returned in every API response). The human-readable primary column gives operators a meaningful label in the UI without needing auto-numbering.

**Revisit when:** A workstream requires user-visible auto-IDs (e.g., a customer-facing ticket number) and the code-populated label pattern is insufficient. Likely never — the intrinsic row IDs cover the technical need and labels cover the human need.

## PowerShell macOS Gatekeeper deprecation 2026-09-01 [OPEN]

The powershell@preview cask path used for EXO ServicePrincipal management (Connect-ExchangeOnline; New-ServicePrincipal) is scheduled for macOS Gatekeeper deprecation on 2026-09-01. Without intervention, post-deprecation runs will fail Gatekeeper signature verification on the cutover MacBook.

Plan B: Azure Cloud Shell. Same Connect-ExchangeOnline + New-ServicePrincipal commands run in a browser shell instead of local PowerShell. No code change required; runbook change only.

Cutover impact: Handover Plan v6 Step 4 verification currently assumes local PowerShell. If Phase 1.5 cutover lands after 2026-09-01, runbook needs the Azure Cloud Shell variant.

Resolves when: 2026-08-15 calendar check confirms status (still scheduled / postponed / cask alternative emerged). Runbook updated based on findings.

## R2 Watchdog Check E (Anthropic spend trend) deferred to Phase 1.5 [OPEN 2026-05-20]

Check E of R2 Watchdog (Anthropic API spend trend analysis) deferred to a follow-on PR (the Check E shipping PR) at Phase 1.5 production cutover. **Architectural choice, not capability gap.** Individual Anthropic orgs DO expose Admin keys once a formal Organization is created (Settings → Organization with business address; verified 2026-05-20). Deferral rationale: sandbox spend signal-to-noise is too low at $5-credit scale for trend analysis to produce meaningful alerts. Re-evaluate at production cutover when spend is real and recurring. Implementation will add `shared/anthropic_billing.py` + `_check_spend_trend` in `scripts/watchdog.py`, seed the 4 `spend.*` `ITS_Config` rows + the `system.anthropic_admin_api_keychain_key` row, and convert the existing smoke runner's Phase E from a SKIPPED placeholder into a real exerciser.

Originally surfaced 2026-05-20 in R2 Session 2 pre-flight (the Keychain `ITS_ANTHROPIC_ADMIN_API_KEY` held a workspace key, `sk-ant-api03-…` prefix, not an Admin key). Session 2 shipped Checks A/B/C/D/F via PR #36; Check E is the only outstanding piece of the R2 Watchdog spec.

## PowerShell `Get-ApplicationAccessPolicy -Identity <friendly-name>` directory lookup fails [OPEN 2026-05-20]

`Get-ApplicationAccessPolicy -Identity <friendly-name>` fails with a directory-object-not-found error in Exchange Online PowerShell, even when the policy exists and is valid.

**Workaround:** call the bare cmdlet (no `-Identity`) and filter the result set client-side. Pattern: `Get-ApplicationAccessPolicy | Where-Object { $_.Description -match '<keyword>' }` or pipe to `Select` and pattern-match the returned rows.

Captured 2026-05-20 during M365 sandbox re-verification while validating the `ITS Scoped Mailboxes` policy for R2 Watchdog Check F. The bare-cmdlet form returned a valid record with `IsValid: True` despite the friendly-name lookup failing seconds earlier on the same policy.

## voice@ mailbox AppAccessPolicy scope addition pending [OPEN 2026-05-20]

`voice@evergreenmirror.com` is one of 5 ITS-intake mailboxes (per the mailbox roster) but is NOT currently in the `ITS Scoped Mailboxes` ApplicationAccessPolicy scope. Confirmed by `Get-ApplicationAccessPolicy` on 2026-05-20 — current scope covers `safety / procurement / subcontracts / its`, no `voice@`.

**Resolves when:** an ITS workstream activates the `voice@` mailbox as an intake source. At that point: add `voice@evergreenmirror.com` to the AppAccessPolicy scope via Exchange Online PowerShell, and register the corresponding `mail_intake.voice.max_idle_hours` row in `ITS_Config` so R2 Watchdog Check F starts monitoring it. No code change required for the policy update; the watchdog already iterates `mail_intake.*` rows via `smartsheet_client.get_settings_with_prefix` (PR #36).

## Stale Anthropic Service Account `svac_…SR7vDMJ` for archival [OPEN 2026-05-20]

Stale Anthropic Service Account `svac_…SR7vDMJ` (created during R2 Watchdog Check E investigation 2026-05-20) flagged for archival. The associated workspace API key has already been deleted from macOS Keychain. No urgency; clean up when next in the Anthropic Console (Settings → Service Accounts → Archive). Captured here so the cleanup isn't forgotten at the next Anthropic-Console visit.

## Eventually migrate from legacy boxsdk to `box_sdk_gen` (Gen API) [OPEN 2026-05-20]

The `boxsdk` PyPI package jumped to a renamed Gen API at 10.x (imports as `box_sdk_gen`, with a substantially different surface). PR #39 pins to `<4.0.0` to use the legacy 3.x API. The Gen API is the future direction per Box; legacy 3.x will eventually be deprecated.

**Action:** re-evaluate when (a) Box announces a deprecation timeline for 3.x, (b) the legacy API lacks something the Gen API offers, or (c) annual dependency-hygiene sweep.

**Migration scope:** `shared/box_client.py`, `tests/test_box_client.py`, `scripts/setup_box_oauth.py`, `scripts/smoke_test_box.py`. Probably non-trivial (~half day of work).

**Urgency:** low. Pin holds until Box deprecation pressure or capability gap.

Surfaced: PR #39 review, 2026-05-20.

## Phase 1.5 — provision dedicated ITS Box user account, re-auth [OPEN 2026-05-20]

ITS currently authenticates to Box as `seths@evergreenmirror.com` (operator account). All API actions attribute to that user in Box audit trails, and all ITS-created files are owned by that user.

At Phase 1.5 cutover, provision a dedicated ITS Box user account (e.g., `its@evergreenrenewables.com` once the production tenant is live) and re-authenticate ITS as that user. No code changes needed — just re-run `scripts/setup_box_oauth.py` while logged into Box as the new user.

**Concerns to handle at migration time:**
- File ownership of anything ITS created under the operator account may need to be transferred to the new user.
- Collaborator permissions on existing folders must be granted to the new user before re-auth.
- Old refresh token under the operator account should be revoked in the Box account settings.

**Urgency:** Phase 1.5 cutover task. Not before.

Surfaced: PR #39 brief, 2026-05-20.

## Confirm `canonical_job_path()` format with owner [OPEN 2026-05-20]

`shared/box_client.py` exposes `canonical_job_path(customer, job_number, job_name, year)` which returns `"/Customer/JobNum — JobName/YYYY/"`. This is the WRITE-path format for new ITS-created content.

Owner confirmation has not happened yet — the format is the legacy-stub placeholder, never validated against owner preference. `box_migration/parse_job_v3.py` handles read-side recognition of the 4 active Box schemas, so this only affects what ITS creates going forward, not what it can recognize.

**Action:** surface to owner at next opportunity, confirm or adjust format, update `shared/box_client.py` + tests if needed.

**Urgency:** low until the first workstream consumes `canonical_job_path`. At that point the decision becomes blocking and locks the format for all future ITS-created content.

Surfaced: PR #39 brief, Open Question Q2, 2026-05-20.

## Seed `system.box_smoke_folder_id` in ITS_Config [OPEN 2026-05-20]

`scripts/smoke_test_box.py` supports a `--write-test` opt-in flag that does a write-read-delete loop against a known sandbox folder. The folder ID comes from an `ITS_Config` row at `system.box_smoke_folder_id`.

The row is not yet seeded. The read-only smoke (default invocation) works without it; only the opt-in write-test path requires it.

**Action:** create a dedicated "ITS Smoke" folder in Box, copy its folder ID, seed the `ITS_Config` row. After seeding, run `python3 scripts/smoke_test_box.py --write-test` once to confirm.

**Urgency:** low. Read-only smoke is sufficient for most operator checks. Write-test is useful only when diagnosing suspected scope or permission issues.

Surfaced: PR #39 brief, Open Question Q4, 2026-05-20.

## Alert-routing dedupe key granularity [OPEN 2026-05-20]

(Naming gloss for this entry and several below: "PR α" = PR #42 — alert-dedupe core; "PR β" = PR #44 — watchdog Check G summary sweep. Greek-letter aliases predate the actual PR numbers landing.)

`shared/alert_dedupe.py` keys dedupe windows on `(script, error_code)` (built at the `_fire_resend_leg` call site). Today's only call path uses `error_code="uncaught_exception"`, so all decorator-driven CRITICALs from a given script collapse into one window. If production shows distinct underlying exception classes inside one script collapsing within a window — and the operator misses the second bug because the first one suppressed its alert — upgrade the key to `(script, error_code, exc_class)`.

**Action:** one-line change at the `dedupe_key = f"{script}::{error_code}"` site in `shared/error_log._fire_resend_leg`. Thread `exc_class` from the decorator's `except Exception as e:` path via `type(e).__name__`.

**Urgency:** low until production surfaces the collapse-different-bugs failure mode. Bounded blast radius — Smartsheet ITS_Errors + Sentry still record each bug separately, so the operator sees the second bug eventually; only the wake-up email is delayed.

Surfaced: PR α (alert-dedupe-core) brief, 2026-05-20.

## Cross-leg dedupe activation [OPEN 2026-05-20]

PR α suppresses only the Resend leg. Sentry events and Smartsheet ITS_Errors rows always write (per Op Stds v11 §3.1 — dedupe applies only to push, never to records). Today this is the right choice: Sentry's own alert rules and Smartsheet's sheet-level notifications are NOT configured.

**Resolves when:** the operator configures Sentry alert rules (or Smartsheet notifications) that themselves wake the operator on every event. At that point, those legs become "push" surfaces too and need their own dedupe layer. The shared `correlation_id` is already wired through all three legs, so a future cross-leg dedupe (or alert-aggregator) has the join key it needs.

**Urgency:** activates only when external alert rules are configured. No risk while Sentry/Smartsheet stay record-only.

Surfaced: PR α (alert-dedupe-core) brief, 2026-05-20.

## Alert-dedupe state is per-machine [OPEN 2026-05-20]

`~/its/state/alert_dedupe.json` lives on the local MacBook. The dedupe window is per-host. If ITS ever runs on multiple hosts (Phase 4+ blueprint generalization, or a hot-spare during MacBook RMA), each host would dedupe independently — and an operator-facing flapping CRITICAL on two hosts would produce one email per host instead of one total.

**Resolves when:** ITS gains multi-host execution. The state needs to move into a centralized store. Smartsheet itself can't host it (Smartsheet IS a triple-fire leg; circular dependency). Likely candidates: a dedicated S3 prefix, a Redis sidecar, or a per-customer SQLite that lives on whichever host happens to be authoritative.

**Urgency:** low. Phase 1 through Phase 3 is single-host on a designated MacBook. Multi-host is a Phase 4+ blueprint-generalization decision.

Surfaced: PR α (alert-dedupe-core) brief, 2026-05-20.

## Smoke harness pattern divergence between dedupe smoke and Resend/Sentry smokes [OPEN 2026-05-20]

`scripts/smoke_test_alert_dedupe.py` uses the full `@its_error_log` decorator path so all three triple-fire legs fire (Smartsheet `log()` write + Resend + Sentry). `scripts/smoke_test_sentry.py` and `scripts/smoke_test_resend.py` call `shared.error_log._alert_critical` directly, which deliberately bypasses `log()` and therefore does NOT write to ITS_Errors.

The divergence is acceptable because the older two scripts validate narrower scopes (the Sentry leg, the Resend leg), and the alert-dedupe smoke validates the cross-leg integration. The trap is that the `_alert_critical`-direct pattern silently skips the Smartsheet leg — if a future smoke claims to exercise full triple-fire but uses that pattern, the ITS_Errors assertion will pass vacuously (zero rows match, zero rows expected by the harness).

**Action:** any new smoke that intends to verify all three legs MUST go through the `@its_error_log` decorator. Smoke that targets a single leg can keep the `_alert_critical`-direct pattern.

**Urgency:** low. No active failure; this entry is forward-protection for the next time someone writes a triple-fire smoke. Discovered post-PR-#42 merge when the operator's live run produced 0 ITS_Errors rows.

Surfaced: PR α (alert-dedupe-core) live verification, 2026-05-20.

## Alert-dedupe state-file growth in pathological flap-with-new-error-code scenarios [OPEN 2026-05-20]

PR β's two-phase deletion bounds state-file growth at ≤1 day per `(script, error_code)` key pair across the sweep cadence: an entry is fired-and-marked on sweep N, deleted on sweep N+1. Worst-case file growth across the ITS lifetime is one entry per distinct dedupe key.

The pathological scenario the bound assumes against: a script that flaps repeatedly with a NEW `error_code` each window, producing unbounded distinct keys per day. `_alert_critical` today always uses `error_code="uncaught_exception"`, so the bound holds. If `_fire_resend_leg` is ever upgraded to a richer key (e.g., `(script, error_code, exc_class)` per the existing tech-debt entry on key granularity), AND the underlying script raises a wide variety of exception classes within short windows, growth could accelerate.

**Action:** monitor state-file row count. If it grows past ~100 persistent entries between sweeps, investigate before tuning sweep cadence or compacting the state schema.

**Urgency:** none today. Bounded blast radius; sweep cadence is the lever if the file ever balloons.

Surfaced: PR β (watchdog summary sweep) brief, 2026-05-20.

## Watchdog sweep cadence vs dedupe window length [OPEN 2026-05-20]

Default `alerting.dedupe_window_minutes = 60`. Watchdog runs once daily at 7:00 AM ET. Worst-case operator-visible summary delay = ~24 hours from window close (a window that closes at 7:01 AM waits until the next morning's sweep).

This is intentional: operators on the daily-rhythm cadence don't need real-time summary push, and the 24h delay only applies to the close-the-loop notification — the original CRITICAL email + the suppressed-marker log lines fire in real time.

**Resolves if:** operator wants tighter feedback. Lever 1 — increase watchdog cadence to hourly via launchd. Lever 2 — separate the summary sweep into its own scheduled script with its own cadence. No code change to dedupe core in either case.

**Urgency:** none. Re-evaluate if operator triage workflow shows ≥24h-delayed summaries causing problems.

Surfaced: PR β (watchdog summary sweep) brief, 2026-05-20.

## Summary email content depth (filter-criteria vs inline correlation IDs) [OPEN 2026-05-20]

PR β summary email body lists aggregate counts + window timestamps + filter criteria pointing at ITS_Errors (Script + Surfaced At range). It does NOT enumerate per-suppressed-event correlation IDs inline, because the state file stores only aggregates per dedupe key — individual UUIDs live in ITS_Errors rows.

If operator triage workflow shows excessive Smartsheet lookups when triaging a summary, the upgrade path is: grow the state schema to retain a list of correlation IDs per window (capped at N most recent to bound file size), and inline those in the summary body. State migration would be needed; existing entries lack the field.

**Action:** track operator triage patterns. If "open the summary → open ITS_Errors → copy filter → run filter" becomes a frequent friction point, upgrade the schema.

**Urgency:** none today. Pull-from-source-of-truth pattern is cleaner if operator only triages a handful of summaries per week.

Surfaced: PR β (watchdog summary sweep) brief, 2026-05-20.

## Picklist_Sync_Config mixes config and runtime state [OPEN 2026-05-20]

`Picklist_Sync_Config` holds both configuration (mapping_id, source/target sheet+column, enabled, notes) and runtime state (last_run_at, last_run_hash) on the same sheet. Architecturally a small smell — runtime state evolving on a "config" sheet means operators editing the sheet can accidentally clear hash/timestamp, forcing a full re-sync.

**Why kept as-is:** §14 preservation-over-refactor. Phase 1.5 doesn't need the split. The convenience of "one sheet per concern" outweighs the purity cost while there's only one consumer.

**Resolves if:** picklist_sync grows complex enough to need migration/versioning (multi-customer fork edge cases, schema evolution of per-mapping state, etc.). At that point: move `last_run_at` + `last_run_hash` to a separate `Picklist_Sync_State` sheet keyed on `mapping_id`, leave `Picklist_Sync_Config` purely declarative.

**Urgency:** none. Watch for operator-edit accidents that wipe hash/timestamp — first such incident is the resolution trigger.

Surfaced: Picklist sync hardening review, 2026-05-20.

## SDK-vs-live body-shape mismatches need integration coverage [OPEN 2026-05-20]

PRs #47/#48/#49 each surfaced one body-shape mismatch the Smartsheet SDK accepted silently but the live API rejected, in successive iterations:

- **PR #47**: `id` in body — errorCode 1032 ("attribute(s) column.id are not allowed for this operation").
- **PR #48**: `type` missing from body — errorCode 1090 ("Column.type is required when changing options").
- **PR #49**: `type` present but wrapped as `EnumeratedValue`, SDK silently strips it — wire body becomes `{"options": [...]}` with no `type`, API rejects same as #48.

Class of bug: `SimpleNamespace`-based mocks at the SDK boundary don't enforce the live API's contract on body shape, required fields, or value wrapping. Mock tests passed; live calls failed.

**Mitigation landed in this PR (2026-05-21):** `tests/test_smartsheet_client_integration.py` runs create → list → update → delete round-trips against live sandbox sheets. Registered as `@pytest.mark.integration`; default `pytest` skips them (pyproject.toml `addopts = -m 'not integration'`). Operator runs `pytest -m integration` pre-deployment after any `shared/smartsheet_client.py` or `shared/picklist_sync.py` change.

**Pattern to extend:** any future `shared/*` SDK wrapper that exercises a non-trivial verb (update/create/delete) on typed columns or rows should gain a parallel integration test. The pattern: create the minimum live state required, exercise the verb, assert post-state, tear down in `finally`.

**Urgency:** addressed. Note kept open for visibility — any new wrapper that lands without parallel integration coverage re-introduces the class of bug.

Surfaced: PR #46 → #47 → #48 → #49 iteration, 2026-05-20/21.

> **Audit 2026-07-24 (tech-debt janitorial pass):** Concrete debt RESOLVED — live-API integration coverage exists (tests/test_smartsheet_client_integration.py + parallel coverage for every in-scope shared/* wrapper, @pytest.mark.integration, skipped by default). What remains is only a standing forward-guard, now canonical in HOUSE_REFLEXES §2 — a candidate to close as a duplicate reflex rather than a live gap.

## Smartsheet UI-only constraints (Forms, CF, Filter Views, Restrict-to-dropdown) [OPEN]

Several Smartsheet features are exposed only through the Smartsheet web UI and have NO REST/SDK surface — meaning Claude Code can NOT provision, audit, or sync these per-customer settings during deployment. Operator must configure each manually at deployment time and document the choices.

The known UI-only surfaces (as of 2026-05):

- **Form creation + configuration** — `Smartsheet → Forms` panel. Forms are the primary intake surface for several workstreams; no API equivalent. Form rules (required fields, conditional logic, custom thank-you page, branding) are all UI-only.
- **Conditional Formatting** (cell-color rules based on cell values or row state) — UI-only.
- **Filter Views** (saved per-user filter definitions over a sheet) — UI-only.
- **Restrict to dropdown values only** (PICKLIST column validation toggle) — UI-only. Critical for `shared/picklist_sync.py` activation: the sync writes the option list, but the "reject free-text entries" enforcement toggle must be set manually per column. Without it, picklist sync still works but users can type values that aren't in the master DB (canonical-name drift).

**Impact on `shared/picklist_sync.py`:** the `Restrict to dropdown values only` toggle must be manually set on each downstream PICKLIST column at deployment time. Without it, the sync still works (options stay in sync) but the strict-mode validation that prevents users from typing vendor-name drift is absent. Documented in `docs/references/picklist_sync.md` activation checklist step 5.

**Impact on form-and-clone cascade:** every form requires manual UI setup. The cascade flow assumes operator builds forms in the UI as the final cutover step.

**Resolves if:** Smartsheet exposes any of these surfaces via API. Worth re-checking annually — Smartsheet's API surface expands slowly. No action item today; this entry exists so future operators / new customer forks know the manual-deployment-step list without rediscovering it.

**Urgency:** none. Operationally accepted; manual deployment steps documented per-customer.

Surfaced: Phase-0 architecture review 2026-05; referenced from `docs/references/picklist_sync.md` activation checklist.

## safety_reports week-folder create-find race condition [OPEN 2026-05-21]

`safety_reports/week_folder.ensure_current_week_folder` performs a find-or-create on the per-week folder under each project's Field Reports subtree. Two concurrent callers (e.g., a same-week intake.py and a Friday weekly_generate.py firing within the same minute) could both pass the initial `find_folder_by_name_in_folder` step and both create the folder; Smartsheet does not enforce folder-name uniqueness, so both creates succeed.

The helper detects the duplicate on a post-create find: if the post-create lookup returns a different folder ID than the just-created one, it logs a WARN to ITS_Errors with `error_code="week_folder_race_duplicate"` and proceeds with the first match (the survivor). The orphan folder ID appears in the WARN message for operator triage.

**Workaround:** operator manually deletes orphan folders via short-lived sandbox token + curl per Op Stds v11 §25 MCP-gap REST fallback (`curl -X DELETE https://api.smartsheet.com/2.0/folders/<orphan_id> -H "Authorization: Bearer <token>"`). No automatic cleanup — race is rare at single-machine cadence, and the safer move is operator visibility (WARN → review) over an automated delete that could race against legitimate concurrent writes.

**Why not auto-clean:** the orphan folder is initially empty (the losing-race caller hasn't created its sheets yet at the moment of duplicate detection). But a subsequent run on the orphan side WOULD create sheets, and an auto-delete couldn't safely distinguish "empty orphan" from "filled-by-another-thread orphan." Operator visibility wins.

**Resolves if:** observed in practice (no incident expected at single-machine cadence; multi-machine ops would trigger this).

Surfaced: R3 foundation PR brief, 2026-05-21.

## Fallback path removal after ITS_Config cutover [OPEN 2026-05-23]

Per the ITS_Trusted_Contacts delivery above, the legacy ITS_Config allowed_senders fallback stays in `safety_reports/intake.py` (`_check_legacy_allowlist` + the `sheet_contacts` branch in `_run_pipeline`) until the operator confirms one full Friday cycle clean post-cutover. Then:

- Remove `_check_legacy_allowlist`.
- Remove the `sheet_contacts = trusted_contacts._load_contacts()` / `if sheet_contacts:` branch in `_run_pipeline`; replace with direct `check_trusted_sender(...)` call.
- Delete `_fallback_logged` + the once-per-process INFO log.
- Drop the `CFG_ALLOWED_SENDERS` constant + `_read_allowed_senders` helper.
- Update `test_intake_stage2_refactor.py::test_empty_sheet_falls_back_to_its_config_allowlist` + `test_sheet_with_rows_is_authoritative_skips_legacy_allowlist` accordingly.

**Effort:** ~30-min session.

**Revisit when:** operator confirms one Friday cycle clean post-cutover.

## Native multi-PICKLIST graduation for Trusted Contacts scope columns [OPEN 2026-05-23]

`Project Scope` and `Workstream Scope` columns on `ITS_Trusted_Contacts` are TEXT_NUMBER JSON-lists, not native multi-PICKLIST. Rationale (per the Phase 1.4 brief): the Smartsheet SDK returns inconsistent shapes for multi-PICKLIST (sometimes comma-string, sometimes list) and the cross-sheet picklist sync from PR #45-51 doesn't cover multi-select reliably. Once the Phase 1.4 picklist-hardening deliverable lands:

- Convert column types to MULTI_PICKLIST.
- Update `shared/trusted_contacts.py::_parse_scope` to accept either form during the transition.
- Add reference-checked sync to the picklist_sync.py registry.

**Effort:** ~1 hour session.

**Revisit when:** Picklist Hardening #1 deliverable lands.

## DKIM in-process re-validation [OPEN 2026-05-23]

`shared/header_forgery.py` trusts the inbound MTA's `Authentication-Results` DKIM verdict — no local DNS TXT lookup + RSA verify. Acceptable for Phase 1: the only path delivering messages is via the verified inbound MTA chain. If a future threat-model session demands cryptographic re-validation:

- Add `dkimpy` (or `python-dkim`) to requirements.
- Replace the `dkim=tokens.get(...)` path with a re-validation step (parse `DKIM-Signature` → DNS TXT lookup → RSA verify).
- Cache DNS TXT records per (selector, domain) for the poll cycle.

**Effort:** ~half-day session.

**Revisit when:** security review or threat-model session flags the in-MTA-trust assumption.

## Operator-UI Shortcuts for trusted-contacts workflows [OPEN 2026-05-23]

`ITS_Trusted_Contacts` operator edits today require direct Smartsheet UI. A Shortcuts-track addition could wrap common flows:

- "Approve pending sender" — picks PENDING_VERIFICATION rows, prompts operator, flips to ACTIVE + sets Last Verified=today.
- "Disable sender" — by Email or row pick, flips Status to DISABLED + notes the reason.
- "Verify identity" — re-stamps Last Verified=today for ACTIVE rows.

**Effort:** ~half-day session.

**Revisit when:** Tooling-track session has bandwidth.

## Attachment screening pipeline Layers 1-3 [OPEN 2026-05-22]

Implement 4-layer attachment screening per Op Stds v11 §34 + FM v8 Invariant 2 Layer 6 (Layers 1-3 for Phase 1.5; Layer 4 VirusTotal deferred Phase 2+):
- Layer 1 (static): magic-number verification, size sanity, filename pattern matching.
- Layer 2 (structural): PyMuPDF or pypdf for PDF JS/embedded-file detection; python-docx/openpyxl for Office macro/OLE detection; EXIF anomalies; embedded URL extraction.
- Layer 3 (ClamAV): pyclamd + clamd daemon + freshclam auto-update. Homebrew install on operator Mac.
- Layer 4 (VirusTotal): defer.

EICAR test signature fixtures verify pipeline health without real malware. Integration test against corpus of legitimate DFR samples.

Disposition: malicious → ITS_Quarantine + CRITICAL triple-fire + sender DISABLED in ITS_Trusted_Contacts; suspicious → ITS_Review_Queue; clean → proceed.

**Effort:** ~half-day to one-day session (operator-side ClamAV install + code + tests).

**Revisit when:** Phase 1.4 security hardening session lands; required before Phase 1.5 cutover.

## HTML email rendering for weekly_send [OPEN 2026-05-23]

`weekly_send.py` v0.1.0 sends `Draft Body` as inline text via `content_type="Text"`. Sponsors may prefer HTML formatting (paragraph breaks, bullet lists, the WPR layout's table structure rendered properly). Calibrate with Teala after the first 30 days of real Friday cycles — same 30-day window as the `safety_weekly_generate` prompt v0.1.0 calibration entry.

Implementation: render `Draft Body` (currently plain text with `[REVIEWER TO FILL]` placeholders) into minimal HTML via a small template, pass `content_type="HTML"` to `graph_client.send_mail`. Same recipient flow.

**Effort:** ~half-day session including +2-4 unit tests for the rendering function + a smoke run.

**Revisit when:** Teala provides feedback on the v0.1.0 inline-text format (after first 30 days of real cycles).

## Doc-conventions lint strict-mode flip after retrofit window closes [OPEN 2026-05-24]

`scripts/lint_doc_conventions.py` ships warn-only. Two follow-on items track the retrofit window's close:

1. **Bulk-retrofit sweep** of grandfathered docs (~36 session logs + a handful of pre-existing audits / references) — add YAML frontmatter to each. Target window: ~60 days (2026-07-24). Lazy retrofit per `docs/operations/doc_conventions.md` is the interim policy; this sweep is the optional bulk-migration option.
2. **Flip lint to `--strict`** in CI after the sweep completes. `.github/workflows/ci.yml` currently invokes the lint without `--strict`; one-line change to add the flag once the sweep lands and all violations clear.

Trigger conditions:
- Auto-trigger #1: 2026-07-24 reached (default sweep target).
- Manual-trigger #1: operator decides to skip the bulk sweep and accept indefinite grandfather state. In that case strict-mode flip is also skipped; the conventions doc's "Retrofit policy" section should be updated to mark the policy as permanent.

**Effort:** ~2 hours for bulk sweep (mostly automatable — frontmatter generation from filename/git-log); ~5 min for the strict-mode flip.

**Revisit when:** 2026-07-24, or sooner if operator opens a doc-retrofit session.

## Nightly auto-index regen wiring [DEFERRED 2026-05-24]

`docs/operations/doc_conventions.md` mentions a "nightly regeneration" path for `scripts/regen_doc_indexes.py` via `scripts/watchdog.py::TRACKED_JOBS`. Not wired in the initial ship: regen runs in CI (`--check` mode) on every PR, which is the load-bearing enforcement. A nightly launchd job would add freshness for un-merged branches sitting on the operator's MacBook, but the CI gate is sufficient for `main`.

**Action when triggered:**
1. Add launchd plist `org.solutionsmith.its.doc-index-regen.plist` (StartCalendarInterval, daily 03:00 local).
2. Have the script write a watchdog marker on successful regen.
3. Append `doc_index_regen` to `scripts/watchdog.py::TRACKED_JOBS` with 36-hour freshness window.

**Effort:** ~30 min.

**Revisit when:** operator notes drift between local doc state and CI's view, OR a third polling daemon ships and the watchdog wiring patterns are being touched anyway.

## Hardcoded BOX_SUBPATH_BY_CATEGORY in safety_reports/intake.py [OPEN 2026-05-24]

`safety_reports/intake.py:172` defines `BOX_SUBPATH_BY_CATEGORY: dict[str, tuple[str, ...] | None]` — hardcoded mapping from inbound email category to Box subfolder path. `VALID_CATEGORIES` (line 195) is derived from this dict's keys. Adding a new safety-reports category requires code change.

**Failure mode:** same shape as `BOX_PROJECT_FOLDERS` (config-migration sibling): operator can't add a category without a PR. Lower change cadence than projects (categories churn slowly — the safety-reports taxonomy is more stable than the project set), but same redeploy-for-ops-task problem.

**Proposed fix:** migrate to either (a) `ITS_Config` rows with key prefix `BOX_SUBPATH_<category>` and tuple values JSON-encoded, or (b) a dedicated `ITS_Category_Routing` sheet alongside the project-routing sheet from the A2 entry. Same caching pattern. Same Box-resolution validation. Coupled enough with A2 that landing both in one PR pair makes sense (a `shared/routing.py` module covering both lookups).

**Effort:** ~2 hours, lower than A2 because category set is smaller and the schema is simpler (no `Active` bool needed if categories are append-only).

**Phase target:** 1.6 — lower priority than A2 because category set is stable. Bundle with A2 only if the routing-module shape benefits from co-design.

**Tag:** `config-migration`.

**Revisit when:** A2 lands (do A3 right after, sharing the routing-module pattern), OR a new safety category needs adding before A2 lands (force the move at that point).

Surfaced: 2026-05-24 hardcoded-values audit brief, §A3.

## Severity-tiered + multi-recipient alert routing [OPEN 2026-05-24]

Current state: `shared/resend_client.send_alert()` sends to a single recipient resolved from `system.operator_email` in ITS_Config at runtime (per `shared/resend_client.py:164`). No multi-recipient distribution. No severity gating — every CRITICAL via `_alert_critical` fires the same Resend leg to the same single recipient regardless of severity.

Adequate for the solo-operator stage. Becomes a gap when:

- Team composition expands (on-call rotation, multiple operators in different timezones).
- Severity stratification matters (CRITICAL to phone-via-Resend, WARN to a digest sheet only).
- Customer 2+ onboarding lands and per-customer recipient lists need separation.

**Proposed fix:** new `ITS_Alert_Routing` sheet with columns `Email` (TEXT_NUMBER, primary), `Severity Threshold` (PICKLIST: CRITICAL/WARN/INFO), `Workstream Filter` (TEXT_NUMBER, JSON list — `["*"]` for all), `Active` (bool), `Notes`. `send_alert()` reads the sheet, filters rows by severity ≥ threshold AND workstream match, fans out to each matching recipient. Email validation at sheet load (basic `^[^@]+@[^@]+\.[^@]+$`). Keep `system.operator_email` as the single-recipient fallback when the sheet is empty or unreachable.

**Effort:** ~half-day session including schema migration script (mirror the trusted-contacts pattern) + `shared/alert_routing.py` reader + `send_alert()` rewiring + tests.

**Phase target:** 2 (post-Customer-1 cutover). Single-recipient is sufficient for the solo + Customer-0 stage and shouldn't preempt Phase 1.4/1.5 critical-path work.

**Tag:** `config-migration`.

**Revisit when:** team expansion is concrete, OR Customer 2 onboarding begins.

Surfaced: 2026-05-24 hardcoded-values audit brief, §A4. Note: the brief's premise (hardcoded recipients in `shared/alert.py`) was inaccurate — that file doesn't exist; recipient is already ITS_Config-sourced. This entry reframes the spirit of the concern: future multi-recipient + severity-tiered routing, not present-day hardcoding.

## Allowlist drift detection — typo'd trusted-contacts entry silently quarantines [OPEN 2026-05-24]

`ITS_Trusted_Contacts` entries with a typo in the Email field silently route legitimate senders to quarantine. Operator has no signal that the list itself is wrong vs. the sender being legitimately untrusted. Same shape applies to the legacy `safety_reports.intake.allowed_senders` JSON list still alive as the dead-fallback path (per the existing "Fallback path removal after ITS_Config cutover [OPEN 2026-05-23]" entry — that fallback should be removed soon, narrowing this surface).

**Failure mode:** field PM emails a JHA from `joe.smith@evergreenrenewables.com`. Trusted-contacts row was seeded with `joe.smtih@evergreenrenewables.com` (transposed). Message routes to ITS_Quarantine instead of intake. Operator assumes everything is fine until a missed safety report surfaces downstream.

**Proposed fix (two-layer):**

1. **Validation at sheet read:** `shared/trusted_contacts._load_contacts()` adds basic email regex validation when materializing rows from `ITS_Trusted_Contacts`. Rows with malformed emails get logged to `ITS_Errors` with `error_code='trusted_contacts_row_malformed'` and skipped. Cheap; surfaces typos in the email format itself.
2. **Reconciliation sweep:** weekly job that lists distinct senders in `ITS_Quarantine` over the last 7 days. For each, compute Levenshtein distance against every active `ITS_Trusted_Contacts` Email. Distance ≤ 2 surfaces as a `near_miss_quarantine` row in `ITS_Review_Queue` with the two emails side-by-side. Low-urgency review-queue item, not an alert. Catches typos that pass basic regex (`joe.smtih@...` is a valid email format).

**Effort:** ~3 hours for layer 1 (regex validation + 5-6 unit tests). ~half-day for layer 2 (sheet read + Levenshtein + review-queue integration + tests + watchdog cadence wiring).

**Phase target:** 1.6 (lands cleanly post-Customer-0-cutover; layer 1 can ship immediately once `_load_contacts` is being touched anyway).

**Revisit when:** layer 1 — next touch of `shared/trusted_contacts.py`. Layer 2 — Phase 1.6 hardening, or operator first encounters a near-miss-typo incident.

Surfaced: 2026-05-24 hardcoded-values audit brief, §B1.

## Box folder delete-and-recreate breaks folder ID resolution [OPEN 2026-05-24]

Box folder IDs are stable across renames but NOT across delete-and-recreate. If someone deletes a project folder in Box and recreates it with the same name, uploads to the stale ID will land in the wrong place (or fail, depending on SDK behavior against trashed folders — needs verification: the boxsdk 3.x trashed-folder upload path returns success or error?).

**Failure mode (silent variant — needs SDK verification):** if Box returns 2xx on upload-to-trashed-folder, ITS-generated files land in trash invisibly. Operator sees no upload error; thinks files are filed correctly. Real-world impact: documents lost until someone notices missing files in the active folder.

**Failure mode (loud variant):** Box returns error; intake daemon surfaces via triple-fire CRITICAL alert. Operator gets the alert but the failure cause ("404 folder not found" against a folder that "exists" in Box UI under a new ID) is opaque without tribal knowledge of the delete-recreate gotcha.

**Proposed fix (depends on A2 landing first):**

1. **Startup validation** in the new `shared/project_routing.py` (or whatever lands from A2): every active row's `Box Folder ID` must resolve via Box API to a non-trashed folder. Validation runs at daemon startup AND in a weekly reconciliation watchdog check. Log WARN + skip routing to invalid folders rather than crash.
2. **Operator runbook entry**: "If a Box folder is recreated, update the routing sheet with the new ID. The old ID will WARN in watchdog within 24 hours regardless."
3. **SDK trashed-folder behavior verification:** one-off smoke test against a deliberately-trashed sandbox folder to confirm whether boxsdk 3.x upload returns error or silently lands in trash. Document the answer in `docs/references/box_sdk_gotchas.md` (or similar).

**Effort:** ~2 hours for validation logic + watchdog wiring (mostly straightforward once A2's routing sheet exists). ~30 min for the SDK behavior smoke test.

**Phase target:** Phase 2 — depends on A2 landing first, since this is the validation layer for that routing config.

**Revisit when:** A2 lands; bundle this immediately after as the second PR in the config-migration cluster.

Surfaced: 2026-05-24 hardcoded-values audit brief, §B2.

## Configuration validation at daemon startup [OPEN 2026-05-24]

Once items A2 / A3 / A5 (and the existing trusted-contacts work) migrate config into Smartsheet, daemons fetch config at startup with no formal validation step. A malformed row, missing key, or unresolvable folder ID can let the daemon enter its main loop with broken config — it'll fail per-cycle at unpredictable points instead of failing loud at startup.

**Failure mode:** operator typos an ITS_Config row. Daemon starts. First poll cycle runs. Per-cell-write fails in some downstream call. ITS_Errors fills with cryptic errors. Operator's mental model: "ITS broke, why is the watchdog quiet?" — because the watchdog can't distinguish "broken config" from "broken external API."

**Proposed fix:** new `shared/config_validation.py` with a single `validate_all()` entry point called from every daemon's `main()` before the loop starts. Per-daemon manifest of required keys + validators:

- All required ITS_Config keys present (per a per-daemon registry — `intake_poll.REQUIRED_CONFIG`, etc.).
- All email addresses pass `^[^@]+@[^@]+\.[^@]+$`.
- All Box folder IDs resolve via Box API to non-trashed folders (depends on A2 landing).
- All referenced Smartsheet sheet IDs exist (cheap `get_sheet_summary`-style probe).

On failure: log full report to Sentry + ITS_Errors, exit non-zero. **Do not enter the loop with broken config — fail loud.**

**Effort:** ~half-day session including the validation module + per-daemon registries + tests + integration smoke + runbook update ("if a daemon fails to start, check the Sentry / ITS_Errors entry for the validation report").

**Phase target:** 1.6 — lands after A2/A3/A5 migrate config into Smartsheet. Sequence: config-migration cluster → validation layer.

**Tag:** `config-migration` (the consumer side).

**Revisit when:** A2 lands, AND a third polling daemon is queued, OR operator hits the silent-fallback-into-bad-config failure mode in real ops.

Surfaced: 2026-05-24 hardcoded-values audit brief, §C1.

## Config-change audit trail [OPEN 2026-05-24]

Once configuration lives in Smartsheet (ITS_Config rows + future `ITS_Trusted_Contacts` / `ITS_Project_Routing` / `ITS_Alert_Routing` sheets), changes happen without a git commit. For security-relevant config — `ITS_Trusted_Contacts` especially — this is an audit gap. Smartsheet has cell-history natively, but that history is bounded to the Smartsheet tenant; if a customer ever needs an external audit copy independent of Smartsheet (compliance requirement, post-incident forensics, vendor risk), there's no out-of-band record.

**Failure mode (low-frequency):** post-incident, operator wants to know "who added `acme@external-domain.com` to trusted contacts on 2026-XX-XX." Smartsheet cell history covers it. But if the question is "show me the entire trusted-contacts state on 2026-XX-XX" — Smartsheet's history surface is per-cell, not point-in-time-snapshot; reconstructing requires manual scrubbing.

**Proposed fix (layered):**

1. **Runbook entry:** document Smartsheet's built-in cell-history view as the canonical audit trail. Train operator on the per-cell-history surface. Low-cost, covers the common case.
2. **Weekly diff-export job** for high-stakes sheets (`ITS_Trusted_Contacts`, future `ITS_Alert_Routing`): snapshot to a versioned file in Box on a weekly cadence. Filename `<sheet_name>_<YYYY-MM-DD>.json`. Gives a point-in-time snapshot independent of Smartsheet. Watchdog Check writes a marker; missing snapshots WARN.
3. **Higher-stakes-yet option (deferred):** route trusted-contacts edits through a PR-style approval flow in a separate sheet (`ITS_Trusted_Contacts_Proposed` → operator-approval column → applied to canonical sheet). Likely overkill for solo-operator stage.

**Effort:** ~1 hour for layer 1 (runbook). ~half-day for layer 2 (snapshot script + Box upload + watchdog wiring + tests). Layer 3 is a separate workstream if it ever lands.

**Phase target:** 2 (post-Customer-1 cutover, when audit-as-deliverable becomes a customer-facing concern). Not a launch blocker for Customer 0.

**Revisit when:** first customer raises compliance / audit requirements explicitly, OR a security review session formally surfaces the gap.

Surfaced: 2026-05-24 hardcoded-values audit brief, §C2.

## Single-token blast radius for Smartsheet [OPEN 2026-05-29]

One PAT (`ITS_SMARTSHEET_TOKEN`) does ALL Smartsheet read + write across the whole system. A scope mistake on rotation (e.g. accidentally minting a read-only or viewer-scoped token) breaks every daemon at once, and — per the entry above — does so silently at first write. There is no blast-radius reduction (no separate read vs write tokens, no per-workstream tokens).

**Proposed consideration (not necessarily implement):** evaluate splitting tokens by capability or workstream at a future hardening pass, weighed against the added secret-management complexity for a solo-operator stage. Likely overkill before Customer 2+ multi-customer secret management (already deferred to 1Password CLI per the observability-stack roadmap).

**Phase target:** 2+ (revisit alongside multi-customer secrets).

**Revisit when:** a rotation incident actually causes a system-wide outage, OR multi-customer secret management lands.

Surfaced: 2026-05-29 integration-keychain-stub fix session.

## Optional `fail_closed_until` kill-switch hardening (deferred) [DEFERRED 2026-05-29]

The kill switch is **fail-OPEN by design** (Op Stds v14 §1, audit F07): if ITS_Config is unreachable, the `system.state` row is missing, or its value is invalid, `check_system_state()` resolves to ACTIVE-with-WARN so scheduled work proceeds — it is an operator-convenience pause, NOT a security control. (See the `shared/kill_switch.py` Phase 3 no-op / preserved-fail-open paragraph in the "Picklist-hardening pre-Customer-1" `[CODE DELIVERED 2026-05-23]` entry above, and the `shared/kill_switch.py` capability-table row + the `@require_active` bullet in CLAUDE.md.)

The F07 reframe (blueprint PR #23, Q8) deferred an **optional** `fail_closed_until` mechanism: a timestamp in ITS_Config (e.g. `system.fail_closed_until`) that would let the operator make the kill switch fail **CLOSED** (block / exit cleanly) until a specified time — a time-bounded hard halt for a known-bad window (e.g. "halt all scheduled work until 2026-XX-XX 09:00 while I investigate") — as defense-in-depth over the current always-fail-open behavior.

**Why deferred (not built):** the External Send Gate (Foundation Mission Invariant 1) is the real security boundary — no external transmission happens without explicit human approval regardless of kill-switch state — so a fail-CLOSED kill switch is belt-and-suspenders, not a gap. Adding it now would also complicate the deliberately-simple fail-open contract that the preserved Phase 3 decision settled on.

**Proposed shape (if built):** read an optional `system.fail_closed_until` ISO-8601 timestamp in `check_system_state()`; if present AND `now < fail_closed_until` AND the state row is unreachable/missing/invalid, return PAUSED (block) instead of the fail-open ACTIVE. Absent or past → current fail-open behavior unchanged. Keep it strictly opt-in so the default stays fail-open.

**Effort:** ~half-day (config read + one branch in `check_system_state` + tests covering present-future / present-past / absent).

**Phase target:** 2+ defense-in-depth hardening; not a launch blocker (Invariant 1 already covers the security case).

**Revisit when:** an operator ever needs a time-bounded hard halt of scheduled work (a known-bad maintenance/incident window) that the simple operator-set PAUSED state + fail-open default doesn't cover.

Surfaced: 2026-05-29 exec-ledger-cleanup session (F07 reframe Q8 ledger item). Related: the kill-switch fail-open note in the Picklist-hardening DELIVERED entry above; Op Stds v14 §1; FM Invariant 1 (External Send Gate).

## Inline doctrine-pin normalization across shared/* + safety_reports/* [DEFERRED 2026-06-01]

Tranche 0 (PR #132 — FM v11 / Op Stds v16 citation reconciliation) reconciled the *current-doctrine prose* surfaces (CLAUDE.md, README.md, the manifest) but deliberately did NOT touch the **inline doctrine-version pins in `shared/*` + `safety_reports/*` module docstrings/comments** — a sweep of **~50 sites across 17 files** (the Tranche-0 brief §7 set a "stop and report if >15 sites" guardrail; this is far past it). The pins cite a mix of **FM v8 / Op Stds v11 / v13 / v14**, each recording the doctrine version current *when that module was written* — i.e. historical provenance. Per Op Stds §14 (preservation-over-refactor) + §42 (self-documentation), and because `check_doctrine_drift.py` deliberately scopes `.py` files OUT of the M1 version-drift tier, these are correctly left as-is for now: they are not current-doctrine prose.

Two things a future normalization pass should resolve:
1. **Decide the convention (operator call).** Either (a) leave each pin as build-time provenance (cheapest; the version dates the decision), or (b) normalize to an "as-of v16 / FM v11" convention with the build-time version noted. Stylistic/provenance choice, not a correctness fix.
2. **One real correctness fix to fold in:** `safety_reports/weekly_send.py:72` cites `Op Stds v11 §23.3` for the "sheet-level columns added via UI, not API" constraint. **§23.3 resolves nowhere** in any blueprint version (§23 is the Workspace-Topology stub). Tranche 0 corrected the *matching* CLAUDE.md citation to **§19 (Smartsheet UI-only constraint)** — the canonical home, confirmed by the doc-reconciliation-auditor across 5 commits. Retarget `weekly_send.py:72` §23.3→§19 here so code + doc agree. (`shared/picklist_sync.py:23` similarly cites `Op Stds v11 §25` for "MCP-gap REST fallback" while §25 in live v16 is "per-workstream sheets" — verify and retarget during the sweep.)

**Effort:** ~1–2 hours (mechanical, but each of ~50 pins wants a per-site judgment: bump-version vs leave-as-provenance vs retarget-section). **Phase target:** not a launch blocker — provenance pins don't affect behavior.

**Revisit when:** an operator wants a uniform doctrine-pin convention across the code, or the next session that touches `weekly_send.py` / `picklist_sync.py` for another reason (fix the §23.3→§19 / §25 mis-cites opportunistically per §14 retrofit-when-touched).

Surfaced: 2026-06-01 Tranche 0 doctrine-citation reconciliation (PR #132). Related: PR #132 body "Flags & operator decisions" §2; CLAUDE.md §23.3→§19 correction.

## ITS_Active_Jobs Address cells blank — office PM fill required [OPEN 2026-06-03]

The 6 rows seeded into ITS_Active_Jobs (PR #155) have blank Address values. Real addresses were not invented (§4 — adversarial input / data fidelity; no structured live source exists). The Safety Portal's Work Location auto-fill path will return empty strings until these cells are populated.

**Required action:** office PM opens ITS_Active_Jobs in Smartsheet (Operations workspace → Safety Portal folder) and fills the Address column for all 6 rows (bradley-1, bradley-2, evergreen-hq, poa, rockford-s1, rockford-s2) with the correct street addresses before the Safety Portal goes live.

**No code change required.** The column exists and is schema-correct; the data gap is operational.

**Tag:** `safety-portal`, `data-gap`.

**Revisit when:** Safety Portal goes live (before activating Work Location auto-fill).

Surfaced: 2026-06-03 Safety Portal config sheets session (PR #155). Related: `docs/runbooks/safety_portal_config_sheets.md`.

## Safety Portal — Worker-side capability-gate for TS not covered by Python AST gate [OPEN 2026-06-04]

`tests/test_capability_gating.py` enforces Invariant 1 at the Python AST level. It does not reach the TypeScript Worker at `safety_portal/worker/`. Phase 2 Worker is send-free by inspection (no email, no Graph, no Anthropic). When the Phase 5 HMAC email shim lands (the Worker emits a verified email to `safety@` → `intake.py`), this gap becomes load-bearing.

**Proposed fix (at Phase 5):** add a TS-equivalent capability-gate step — either a `tsc --noEmit` + `grep`-based AST scan of Worker entrypoints for forbidden imports, or extend `test_capability_gating.py` to scan `.ts` entrypoints with the same pattern. Phase 2 does not require this yet.

Note: the Phase 2 brief referenced "Decision 4" for this item, but no named blueprint decision with that ID exists. The decision is tracked here instead.

**Tag:** `safety-portal`, `capability-gate`, `invariant-1`.

**Revisit when:** Phase 5 email-shim work begins.

Surfaced: 2026-06-04 Safety Portal Phase 2 session (PR #158). Related: `tests/test_capability_gating.py`.

## Safety Portal — bcryptjs cost-10 may exceed Workers Free 10ms CPU cap [OPEN 2026-06-04]

`safety_portal/worker/src/worker/auth.ts` uses bcryptjs with cost factor 10. On the Cloudflare Workers **Free plan**, CPU time is capped at 10ms per request (Error 1102). A bcrypt compare at cost 10 can take 50–100ms in V8, reliably triggering the cap on login.

**Options at deploy:**
1. Deploy on Cloudflare Workers **Paid plan** (5ms CPU wall removed; 30s+ allowed) — simplest.
2. Swap `auth.ts` to `Web Crypto PBKDF2-SHA-256` at 100k iterations — CPU-comparable security, runs within Free limits, requires `nodejs_compat` flag and minor code change.

**Tag:** `safety-portal`, `cloudflare`, `performance`.

**Revisit when:** Safety Portal deploy. Decision is Paid-plan vs PBKDF2 swap. Decide before `wrangler deploy`.

Surfaced: 2026-06-04 Safety Portal Phase 2 session (PR #158).

## Phase 5 manual week-sheet additions [OPEN 2026-06-05]

Operator-decided edge case (2026-06-05): if a PM submits a safety doc directly (outside the portal) for a specific job-week, the operator adds a row + the safety doc directly to the per-job week sheet, fills the relevant cells; `intake.py` ignores the manually-added row and `weekly_generate.py` rolls it into the compiled packet like any other doc. This is by design — no automation needed for an occasional manual correction.

**Tag:** `safety-portal`, `operator-workflow`.

**Revisit when:** Phase 5 build. Low-urgency; operator-decided.

Surfaced: 2026-06-05 Safety Portal Phase 3 session (PR #160).

## ITS_Active_Jobs column order cosmetically scrambled [OPEN 2026-06-05, low]

The 4 contact columns (Stakeholder Name, Stakeholder Email, Stakeholder Phone, Safety Reports Contact Email) were added one-at-a-time to ITS_Active_Jobs after the initial schema, causing them to interleave with Active/Notes and the system columns in the Smartsheet UI. Column order is not load-bearing — `shared/active_jobs.py` looks up columns by title, not position. Reorder in the Smartsheet UI if desired for operator readability.

**Tag:** `safety-portal`, `cosmetic`, `smartsheet-ui`.

**Effort:** ~5 minutes (UI drag-to-reorder).

**Revisit when:** convenience; not a blocker.

Surfaced: 2026-06-05 Safety Portal Phase 3 session (PR #160).

## ITS_Active_Jobs CC recipients are operator-entered, not allowlist-validated [OPEN 2026-06-05, accepted-risk]

`shared/active_jobs.py` `cc_emails` (and the TO `safety_reports_contact_email`) come from operator-typed TEXT cells on ITS_Active_Jobs. They are email-shape-validated + de-duped, but NOT checked against `ITS_Trusted_Contacts` or any allowlist. When Phase 5 `weekly_send` wires up `cc_emails`, a PM socially-engineered into entering an attacker address would CC the compiled packet to an unintended party. **Accepted risk** (trusted-operator-input model; the External Send Gate still requires explicit `Approved for Send` before any send). Phase 5 `weekly_send` must document that CC/TO recipients are unverified operator-entered addresses, and log the full resolved TO+CC list at send (already in the Phase 5 brief).

**Tag:** `safety-portal`, `safety-reports`, `phase-5`, `accepted-risk`.

**Revisit when:** building Phase 5 `weekly_send` recipient resolution.

Surfaced: 2026-06-05 Safety Portal Phase 3 contacts amendment (ops-stds-enforcer W1).

> **Audit 2026-07-24 (tech-debt janitorial pass):** Deliverable (b) RESOLVED — weekly_send.py logs the full resolved TO+CC at send (INFO, before the SENDING marker). STILL OPEN: deliverable (a) — the §43 runbook (safety_weekly_send.md) still doesn't document that CC/TO are unverified operator-entered addresses. Otherwise a permanent accepted-risk note.

## Safety Portal — toolbox talk header context missing from form definitions [OPEN 2026-06-05, low]

The source Toolbox Talk PDFs have no operator header fields (the digital record gets job and work-date from the submission envelope; the sign-in section's first row serves as the instructor record). The 5 `toolbox-talk-*.json` definitions are faithful to the source PDFs and therefore contain no Presenter or Date-on-page field. If a Presenter/Date-on-page header field is wanted beyond what the envelope provides, it must be added explicitly to those definitions.

**Tag:** `safety-portal`, `form-definitions`, `low`.

**Effort:** trivial (add a field to the definition + update the catalog row).

**Revisit when:** PM confirms whether a header field is wanted on the rendered PDF.

Surfaced: 2026-06-05 Safety Portal Phase 4 PR 1 session (PR #164). Related: `safety_portal/forms/toolbox-talk-*.json`.

## Safety Portal — job-specific JHA variant content deferred [OPEN 2026-06-05]

The parent/variant mechanism is built (ITS_Forms_Catalog `Parent Form` + `Variant Tag` columns; meta-schema `variantOf` field in form definitions). Specific job-site JHA variants (e.g., `jha-bradley`) are added later as: (1) a new row in ITS_Forms_Catalog with `Parent Form = jha` + a `Variant Tag`; (2) a new `safety_portal/forms/jha-<variant>.json` definition inheriting/overriding the parent. No code change to the renderer — variant resolution is data-driven.

**Tag:** `safety-portal`, `form-definitions`, `phase-4+`.

**Revisit when:** PM identifies a job with site-specific JHA requirements.

Surfaced: 2026-06-05 Safety Portal Phase 4 PR 1 session (PR #164). Related: `safety_portal/forms/meta-schema.json` `variantOf`, ITS_Forms_Catalog `Parent Form`/`Variant Tag` columns.

## [OPEN] Worker-side send-gate enforcement (the TS Worker is outside the Python AST capability-gate)

**What:** `tests/test_capability_gating.py` enforces Invariant 1 (no send capability on
generation scripts; no AI on send scripts) by AST-scanning Python under `shared/` +
`safety_reports/`. It does NOT reach the TypeScript Cloudflare Worker
(`safety_portal/worker/`). As of Phase 5 PR 2 the Worker holds the HMAC signing secret +
the internal bearer token, so it is no longer trivially "send-free by binding-absence" —
its send-free posture rests on code review + the module docstring only. The **pull model**
keeps the Worker send-free by design (it serves a queue + accepts a receipt; it never
initiates outbound), but nothing structurally PREVENTS a future Worker edit from acquiring
an outbound `fetch()` to an external host.

**Fix (when the Worker surface grows):** add a CI grep / ESLint rule forbidding `fetch(` in
`safety_portal/worker/` except to an allowlist (the ASSETS binding), as the TS-side
equivalent of `test_capability_gating.py`. Surfaced by `ops-stds-enforcer` (W2).

**Tag:** `safety-portal`, `security`, `invariant-1`, `phase-5`, `medium`.

**Revisit when:** the Worker gains any new outbound-capable code path, or at the deploy hardening pass.

Surfaced: 2026-06-05 Safety Portal Phase 5 PR 2 (transport queue).

## [OPEN — production-tenant pending; sandbox RESOLVED 2026-07-22] Safety Portal Phase 5 — deploy prerequisites (was CUTOVER-BLOCKING, OPEN 2026-06-05)

Additional prerequisites surfaced by Phase 5 PR 2 (transport queue, PR #169) beyond the base deploy entry above:

1. `CLOUDFLARE_API_TOKEN` — operator obtains (Workers + D1 + R2 scopes); `wrangler login` or env var.
2. `wrangler d1 create its-safety-portal-db` → copy `database_id` into `wrangler.jsonc` (placeholder present).
3. `wrangler d1 migrations apply` (remote, migrations 0001–0005).
4. Worker secrets (two new Phase 5 secrets, in addition to `SESSION_SIGNING_SECRET`):
   - `wrangler secret put HMAC_PAYLOAD_SECRET` (≥32-byte random; used by `shared/portal_hmac.py` verify contract; cross-language HMAC validated in PR #169 tests).
   - `wrangler secret put PORTAL_INTERNAL_API_TOKEN` (bearer token for `/api/internal/*`; mirrored to Keychain as `ITS_PORTAL_INTERNAL_TOKEN` on the Mac side).
5. Keychain entries on the Mac: `ITS_PORTAL_HMAC_SECRET` (same value as `HMAC_PAYLOAD_SECRET`) + `ITS_PORTAL_INTERNAL_TOKEN`.
6. `wrangler deploy` → custom domain binding.

**Tag:** `safety-portal`, `phase-5`, `deploy`, `cloudflare`.

**Revisit when:** Safety Portal deploy session. This entry extends the earlier "deploy + provisioning deferred" entry; that entry covers the base steps; this one covers Phase 5-specific secrets and the D1 migration count update.

Surfaced: 2026-06-05 Safety Portal Phase 5 PR 2 session (PR #169).

**Resolution (2026-07-22, mechanical verify):** every prerequisite was completed by the
2026-06-08 mirror go-live and its successors — the Worker serves `safety.evergreenmirror.com`
(custom domain bound), remote D1 exists with ALL migrations applied ("No migrations to
apply" @ `f2bb9a0`, far past the 0001–0005 this entry names), and the HMAC/internal-token
secret pairs are live (portal_poll has pulled with verified HMACs since 2026-06-08;
VC-01 keychain check enrolls the Mac-side pair).

> **Audit 2026-07-24 (tech-debt janitorial pass):** Sandbox/mirror deploy prerequisites RESOLVED (2026-06-08+; custom domain bound, all D1 migrations applied, HMAC + internal-token secrets live). STILL OPEN (the actual cutover-blocking half): the PRODUCTION-tenant secrets / D1 / wrangler.jsonc re-run — wrangler.jsonc still targets safety.evergreenmirror.com. Header retagged from a bare [RESOLVED] (which read as fully done) to reflect the production-tenant re-run still pending.

## [OPEN] Safety email-intake retire — operator-manual + future-PR follow-ups [2026-06-05]

The 2026-06-05 retire of the safety email-intake path (PR: chore/retire-safety-email-intake)
left these:

1. **Operator-manual: unload the launchd job** `org.solutionsmith.its.safety-intake` on the
   production Mac — `scripts/uninstall_safety_intake_daemon.sh`. `intake_poll.py` is a retired
   tombstone (quiet WARNING no-op on `poll_once`); until unloaded it runs every 60s doing
   nothing. Never done from code.
2. **Operator-manual: delete the `Job Slug` Smartsheet COLUMN** (if/when wanted) — by hand in
   the UI after confirming nothing reads it. Never from a migration. (Runbook: safety_portal_job_management.md Task B.)
3. **Future PR: delete WPR_Pending_Review** (sheet 3096105695793028 + `SHEET_WPR_PENDING_REVIEW`)
   — GATED on the `weekly_generate`/`weekly_send` rewire to `WSR_human_review`. WPR is
   DECOMMISSIONED-by-doc but still read/written by the live weekly daemons; deleting the
   constant/sheet now breaks them. Pairs with the existing Phase-5 weekly-rewire tech-debt entry.
4. **Future: cleanup the tombstone + its assets** — delete `safety_reports/intake_poll.py`,
   `scripts/launchd/org.solutionsmith.its.safety-intake.plist`, and `install/uninstall_safety_intake_daemon.sh`
   once no orphan plist remains and `portal_poll.py` has landed.
5. **Preserved (do NOT touch):** `shared/graph_client.py` (incl. `fetch_latest_inbound_timestamp`,
   whose docstring still says "Used by watchdog Check F" — stale, fix in a future shared/-touching
   PR) and all other `shared/` primitives — Email Triage reuses them.

**Tag:** `safety-portal`, `email-triage`, `cleanup`, `phase-5`, `medium`.

Surfaced: 2026-06-05 safety email-intake retire.

## WPR_Pending_Review final removal (decommission-by-doc → delete)

After the Phase-5 WSR rewire (PRs portal-rewire-pr1..pr4, 2026-06-05), **no live
runtime code references `WPR_Pending_Review`**: `weekly_generate` (compile→WSR),
`weekly_send` + `weekly_send_poll` (send←WSR), and `watchdog` Check I (row-exist←WSR)
are all repointed. The constant `shared.sheet_ids.SHEET_WPR_PENDING_REVIEW` + the
`shared.picklist_validation` WPR registry entry are kept (decommission-by-doc) only
because a few non-runtime refs remain:

  - `scripts/smoke_test_watchdog_catchup.py` — still seeds/clears WPR rows to simulate
    a populated week; needs a WSR rewrite (the catch-up now checks WSR via the Saturday
    `Week Of`).
  - `tests/test_picklist_validation.py` — asserts the WPR Send Status registry entry.
  - the constant + picklist entry themselves.

**Follow-up (trivial, after the operator deletes the WPR sheet):** rewrite the catch-up
smoke to WSR, drop the picklist WPR entry + its test assertion, then delete
`SHEET_WPR_PENDING_REVIEW`. The WPR Smartsheet sheet itself is operator-deleted.

**Tag:** `safety-portal`, `cleanup`, `phase-5`, `low`.

Surfaced: 2026-06-05 WSR rewire (PR4).

## [OPEN 2026-06-09] Publish daemon: rollback UI picker missing

The backend rollback path is fully built: `apply_publish` supports a `rollback` op, the daemon handles it, and `PublishOp` carries the rollback target. The **editor's retired-version-history PICKER UI** is the only missing piece — there is no way to select a rollback target in the admin form without direct API calls. The rollback op is functional today via API.

**Fix:** add a dropdown in `FormEditor.tsx` that populates from the retired form definitions (versions with `status: "retired"` in the catalog) and issues a `rollback` publish-request.

**Tag:** `safety-portal`, `phase-2`, `form-editor`, `low`.

**Revisit when:** a rollback is operationally needed, or at the start of Phase-3 form-editor polish.

Surfaced: 2026-06-09 Phase-2 Form Manager build (PRs #203–#218).

## [OPEN 2026-06-09] Publish daemon: privileged subprocess chain is operator-validated-live only

`safety_reports/publish_daemon.py` orchestrates a chain of git/gh/wrangler subprocess calls (commit, create PR, wait for CI, merge, deploy). Unit tests mock at the subprocess boundary per Op Stds §30. PR #218's `_wait_for_ci` + `_reset_to_main` ran live for the first time during the operator's recovery session. No dedicated integration test harness for the full commit→merge→deploy chain exists.

**Fix:** build a dry-run harness (flag `--dry-run`) that exercises the subprocess chain against a throwaway branch without merging or deploying, so CI can catch subprocess-interface regressions. Until then, every daemon code change to the privileged subprocess chain requires operator live-smoke before merge.

**Tag:** `safety-portal`, `phase-2`, `publish-daemon`, `medium`.

**Revisit when:** the publish daemon code is modified, or at the Phase-3 hardening pass.

Surfaced: 2026-06-09 Phase-2 Form Manager build (PR #218).

## [OPEN 2026-06-09] Form editor: S1 per-item scale/comment authoring from scratch

The `hsse` form uses `scale` and `comment` item-level attributes. These survive an **edit** operation today (existing values are preserved in the round-trip through `apply_publish`). However, there is **no UI in the form editor** to set `scale` or `comment` values when creating a new item from scratch. A new `hsse`-type form authored through the editor would produce items without these attributes.

**Fix:** add `scale` / `comment` optional fields to the item-creation widget in `editorModel.ts` / `FormEditor.tsx`. Scope: narrow UI change, no backend changes needed.

**Tag:** `safety-portal`, `phase-2`, `form-editor`, `low`.

**Revisit when:** a new HSSE-type form is authored via the editor.

Surfaced: 2026-06-09 Phase-2 Form Manager build (PRs #203–#218).

## [CUTOVER-BLOCKING] [OPEN 2026-06-09] Safety Portal — no rate limiting on `/api/login` or `/api/*` (Part-A A2)

Nothing throttles the portal Worker: `/api/login` runs `bcrypt.compare` at cost 10 per attempt (brute-force + a CPU-cost amplification vector), and `/api/submit` + all routes are unbounded.

**Fix (operator, cutover):** add Cloudflare **rate-limiting rules** (dashboard → Security → WAF → Rate limiting rules) — tight on `/api/login` (~5 req / 10 s / IP → ~10 min block), looser blanket on `/api/*`. Documented as a cutover step in `safety_portal/README.md` ("Production hardening — operator cutover steps"). In-code alternative: the Workers **`ratelimit` binding** (in-repo + testable) — adopt if GA for the account at deploy time. **Operator-gated** (Cloudflare account/dashboard), so NOT implemented in code this session per the operator's call.

**Tag:** `safety-portal`, `security`, `operator-action`, `cutover`.

**Revisit when:** Evergreen production cutover, or when the `ratelimit` binding is confirmed GA.

Surfaced: 2026-06-09 Part-A production-hardening session (A2).

## [OPEN 2026-06-09, low] Orphaned Reports sheet — column styling not applied (Part-C C1 cosmetic)

`scripts/migrations/build_orphaned_reports_sheet.py` creates the Orphaned Reports sheet (built live 2026-06-09, `SHEET_ORPHANED_REPORTS=2577084374273924`) with the correct columns + types, but does NOT apply the cosmetic column WIDTHS/formats the brief C1 "styled" item mentioned (it mirrors `build_its_active_jobs_sheet.py`, which also doesn't style in-script). The sheet is fully functional with default widths.

**Fix:** add a `_apply_styles_best_effort`-style pass (per-column width/format) to the migration AND a one-shot `update_column` styling run against the existing live sheet (find-or-create skips a re-create, so the existing sheet needs the columns updated directly), OR fold it into `scripts/style_safety_portal_sheets.py`.

**Tag:** `safety-portal`, `orphaned-reports`, `cosmetic`.

**Revisit when:** the operator finds the default widths inconvenient, or a styling pass is run across the Safety Portal sheets.

Surfaced: 2026-06-09 Part-C session (functional done; cosmetic styling deferred).

## [OPEN 2026-06-09, low] Draft cache stores one draft per account — starting a new form replaces it

`src/lib/draftCache.ts` (PR #250) stores exactly ONE draft per admin account (localStorage key `its-portal-draft:v1:<username>`). Opening the editor for a second form (or creating a brand-new form while a WIP edit exists) silently overwrites the cached draft for that account.

This is accepted behavior — the operator builds one form at a time, and the confirm-discard dialog before starting a fresh form guards against accidental loss. However, the limitation is worth tracking: if concurrent multi-form editing is ever needed, the key scheme would need to include the form identity (e.g., `its-portal-draft:v1:<username>:<formId>`).

**Fix (if multi-form editing is ever desired):** change the localStorage key to include the form identity; expose a "clear draft" call per form; update the editor mount logic to auto-restore the per-form draft.

**Tag:** `safety-portal`, `form-editor`, `draft-cache`, `low`.

**Revisit when:** operator requests concurrent multi-form edit capability, or a WIP draft-loss incident is reported.

Surfaced: 2026-06-09 Form Editor UX + draft-caching session (PR #250; deliberate single-slot design).

## [OPEN 2026-06-09, low] Worker publish-reject paths return bare error codes — no `reason` field for server-side parity with `explainPublish`

The Worker's `POST /api/admin/publish` endpoint returns HTTP 400/401 with a bare JSON `{ error: "..." }` body for validation failures. `FormsPage.explainPublish` (PR #249) maps these codes on the client side, but the server never writes a human-readable `reason` alongside the code. If a new reject path is added on the Worker (or a Hono middleware fires before the handler), `explainPublish` may encounter an unmapped code and fall back to the "code + HTTP status" catch-all.

The current fallback is explicit and non-silent (shows "code + HTTP status"), so this is low-severity. It is deferred because the client-side fix (PR #249) is self-contained and the Worker paths are stable.

**Fix (optional):** add a `reason` field to the Worker's reject bodies so the client can display the server-authored message directly, removing the client-side mapping table entirely.

**Tag:** `safety-portal`, `form-editor`, `error-messaging`, `low`.

**Revisit when:** a new Worker reject path surfaces an unmapped code in production, or a UI polish pass is done on the publish flow.

Surfaced: 2026-06-09 Form Editor UX + draft-caching session (PR #249; client fix is self-contained).

---

## 2026-06-09 Evening Forensic Audit — Deferred Findings

The following entries were surfaced by a read-only 12-dimension forensic audit of the Safety Portal this session. H2, M3, M8, and the SENDING-picklist regression were fixed in PRs #247/#252/#253 respectively. The findings below are explicitly deferred.

## [OPEN 2026-06-09] Safety Portal M2 — capability gate is static-AST-import-only; transitive and dynamic paths are unchecked

`tests/test_capability_gating.py::_imports_in` is static AST-import-only — blind to `importlib.__import__` dynamic imports, has no transitive closure over `shared/` + `safety_reports/`, and `WALKED_ROOTS` excludes `scripts/`. The docstring ("fails at CI before it can ship") overstates the gate's reach.

**Fix:** add `importlib` / `__import__` needles to the banned-pattern scanner; build a transitive-closure walk over `shared/` + `safety_reports/` (not just the top-level file); add a `scripts/`-scoped check for the no-AI-and-send combination.

**Tag:** `security`, `capability-gate`, `testing`.

**Revisit when:** next `tests/test_capability_gating.py` hardening pass, or before Customer-1 launch.

Surfaced: 2026-06-09 12-dimension forensic audit (M2).

## [OPEN 2026-06-09] Safety Portal M7 — publish daemon runs destructive git on the live `~/its` tree without a lock or worktree

`publish_daemon.py` runs `git clean -fd` / `git checkout` on the live `~/its` working tree with no exclusive lock and no guard against the `.claude` `PreToolUse` hook (which has zero reach into `subprocess.run`). `_reset_to_main` scopes the clean to `safety_portal/forms` only, but the tree was stranded in production earlier this session before `_unstrand_if_needed` was added. This violates the repo's own documented worktree discipline and could discard an operator's uncommitted work.

**Fix:** run the daemon from a dedicated worktree + venv (the repo's canonical discipline for processes that write Python source); add a refuse-with-WARN on dirty managed paths instead of silently discarding.

**Tag:** `safety-portal`, `publish-daemon`, `git-discipline`, `medium`.

**Revisit when:** next publish-daemon hardening pass. Before Evergreen production cutover.

Surfaced: 2026-06-09 12-dimension forensic audit (M7).

## [OPEN 2026-06-09] ITS_Daemon_Health sheet observability drift

The operator-visibility surface has drifted significantly from the live daemon topology:
- The RETIRED `safety_reports.intake_poll` row is still present (frozen 2026-06-05, status "OK") — PENDING DELETE (row `7461022174478212`, operator-gated).
- `weekly_generate`, `weekly_send`, `picklist_sync`, and `watchdog` rows read `NEVER_RAN` with pre-pivot WPR descriptions.
- `publish_daemon`, `compile_now_poll`, and `picklist_audit` have NO rows.
- `portal_poll`'s "Last Error Summary" column is not cleared on a successful cycle (stale-error display persists).

A Tier-2 successor-operator reading this sheet would be misled about which daemons are live and healthy.

**Fix (in priority order):** (1) operator deletes the `intake_poll` row via UI; (2) publish daemon gains `ITS_Daemon_Health` self-provision (M6 above); (3) compile_now_poll gains a health row (tracked in the Part-B entry at line ~1858 above); (4) portal_poll clears Last Error Summary on a clean cycle; (5) remaining unloaded daemons' descriptions updated when they are loaded.

**Tag:** `observability`, `daemon-health`, `tier-2-successor`, `medium`.

**Revisit when:** next daemon-health hardening pass. Before Evergreen production cutover.

Surfaced: 2026-06-09 12-dimension forensic audit (live ITS_Daemon_Health inspection).

> **Audit 2026-07-24 (tech-debt janitorial pass):** Original premise (stale rows on the now-deleted sheet) is obsolete post-2026-07-23 rebuild and the self-provision asks are resolved. STILL OPEN (broader than stated): shared/heartbeat.py writes last_error_summary only when not None, so a clean cycle never CLEARS a prior error string — it stays stale on the operator surface for every HeartbeatReporter consumer. Rewrite to a narrow clear-on-clean entry.

## [OPEN 2026-06-12] PR-4 Part A — PDF download cache: deferred optimizations + PR-5 supersession

PR-4 Part A shipped the request-driven canonical PDF download (D1-chunked `filed_pdfs` cache, `pdf_requested`/`box_file_id`/`pdf_ready_at` columns, the `portal_poll._service_pdf_requests` pass, the submitted-page receipt). Four deliberate deferrals:

- **Timing-A post-back deferred.** The brief's "if `pdf_requested` is set when intake files, upload the just-rendered PDF" optimization was NOT built — it would force `intake.py` to acquire portal creds + call `portal_client` (breaking the intake/portal_poll separation, since intake holds the rendered bytes but not the creds, and portal_poll holds the creds but not the bytes). Instead the `portal_poll` `_service_pdf_requests` pass re-downloads the filed PDF from Box via `box_file_id` (one extra Box GET + up to one ~60s cycle of latency) for ALL requests, before or after filing. Within the "under 2 min" UI. **Revisit if** the request-before-filing case becomes latency-sensitive at scale.
- **D1 size telemetry uses the `SUM(LENGTH(...))` fallback.** `PRAGMA page_count`/`page_size` throws `D1_ERROR: not authorized: SQLITE_AUTH` under Miniflare (verified in `prune.test.ts`); the Worker keeps a PRAGMA-first `try/catch` for real Cloudflare D1 (where it may be authorized) and falls back to summing `chunk_b64` + `payload_json` byte lengths. **Revisit if** Cloudflare authorizes `PRAGMA` through the D1 binding (then the byte sum, which under-counts indexes/overhead, can be dropped).
- **Recent-submissions list affordance deferred to PR-5.** The brief's "recent-submissions list gains the same per-row affordance" has no surface today (the SPA has only the single-row amend-prefill notice). PR-5 builds the `FormRequestPage` browse list; Part A delivers the **submitted-page** receipt/download only. **Revisit:** PR-5.
- **PR-5 supersession (forward note).** PR-5 refactors the single `submissions.pdf_requested`/`pdf_ready_at` columns into a `pdf_requests(submission_uuid, account, requested_at, ready_at)` table (downloads become **requester-bound, 24h**, not owner-set). Part A's submitter-request flow becomes the first row in that table — Part A behavior is preserved exactly. Do NOT change Part A's contract mid-flight; PR-5 supersedes it as its own reviewed change.

**Tag:** `safety-portal`, `pdf-download`, `deferred-optimization`, `pr-5-supersession`.

**Revisit when:** PR-5 (form-request browse) lands; or a latency/scale review of the download path.

Surfaced: 2026-06-12 PR-4 Part A implementation.

> **Audit 2026-07-24 (tech-debt janitorial pass):** RESOLVED: bullet 3 (recent-submissions affordance, PR #280) and bullet 4 (PR-5 supersession — now historical fact). STILL OPEN: bullet 1 (Timing-A post-back — portal_poll still re-downloads from Box) and bullet 2 (D1 size telemetry PRAGMA fallback, prune.ts). Trigger (Customer-1 scale) not yet fired.

## weekly_send upload-session — live-Graph integration smoke (deferred to pre-Customer-1) [OPEN 2026-06-12]

**PR-3 review (§30 SDK-vs-Live).** `graph_client.send_mail_large_attachment` (draft → createUploadSession → chunked PUT honoring `nextExpectedRanges` → send) is covered ONLY by mocked unit tests (`tests/test_graph_client_upload_session.py`); there is no live-Graph integration smoke. The four-step Graph REST sequence + the pre-authed `uploadUrl` on a different domain (outlook.office.com, which rejects an `Authorization` header) + the 320 KiB-aligned chunk ranges are exactly the mocks-pass-but-live-fails surface §30 guards. Pre-Customer-1 (and as part of confirming the 2.5 MB threshold), run a live sandbox smoke with a throwaway 3–4 MB PDF fixture: create draft → createUploadSession → single-chunk PUT → send → assert the message lands in **Sent**, then clean it up. Add as `tests/test_graph_client_upload_session_integration.py` (skipif no live token, mirroring the integration-marker gating used elsewhere).

**Tag:** `safety-reports`, `graph`, `integration-smoke`, `pre-customer-1`.

**Revisit when:** the pre-Customer-1 live-tenant validation pass, or the first real photo-bearing weekly packet.

Surfaced: 2026-06-12 PR-3 adversarial review.

## [BLOCKED 2026-06-28] Field-ops Smartsheet/Box source-of-truth integration (P2.4+ downstream)

> **⛔ BLOCKED — PARKED 2026-06-28 (operator decision).** The P2.4 mirror daemon is blocked on **no access to the canonical/main Evergreen Smartsheet account**: Seth cannot currently see the real **schema** or the **source-of-record** for materials / deliverables / etc. A daemon whose whole job is to write D1 → the canonical Smartsheet, built against an *unseen* target schema, would encode **guesses** that will be wrong — worse than absent. **Do not build P2.4 until the SoR is visible.** This blocks ONLY the up-sync/filing layer; every D1-local phase (P3 materials admin-editable catalog, etc.) is unaffected. **Unblock condition:** access to the main Evergreen Smartsheet (real schema + SoR). See `decision_p2.4-parked-no-smartsheet-access` + `feedback_dont-build-against-unseen-sot` memories. The §50 doctrine bump (below) is a *separate* gate that also still needs Seth's sign-off.

The P2.2 field-ops READ views (Personnel #308 / Equipment #309 / Job Tracker #310) read **D1 live** (the local primary) and are send-free — deliberately decoupled from the source-of-truth sync/filing layer (Invariant 1). Wiring Smartsheet (operator-SoR, structured) + Box (document-SoR, filing) in as canonical stores is downstream work the read/write layer does NOT block but does NOT yet implement. Three concrete pieces:

1. **P2.4 mirror daemon** (`field_ops/fieldops_sync.py`) — **PARTIALLY SUPERSEDED 2026-06-30.** The **JOB up-sync half is BUILT** (P2.5 Slice 5: `field_ops/fieldops_sync.py` + `shared/active_jobs_writer.py` dual-sheet mirror into the ITS-owned `ITS_Active_Jobs` + `ITS_Active_Jobs_Progress` sheets; §50/§51-blessed; ships `sync_enabled` OFF). The **origin-flip inversion described here was a BUG and is RETIRED** — the corrected identity model keeps `origin='portal'` FOREVER (the typed `job_id` is the permanent key; a `Portal Job Key` bridge + `canonical_job_id` write-back replace the flip; the Worker down-sync gained a canonical-aware pre-pass instead). What REMAINS parked: the **field-ops-tables up-sync** (personnel / equipment / task_assignments / time_entries / inspections → P7) and the **canonical/main Evergreen Smartsheet integration** (still ⛔ BLOCKED on SoR visibility — that integration writes the *unseen* canonical account, not the ITS-owned sheets P2.5 mirrors). So P2.5 unblocked the JOB mirror against ITS-owned sheets; P7/M2 + canonical-Evergreen stay parked.
2. **Box document linkage** — add a `box_file_id` (or folder ref) column to the document-bearing field-ops records (inspections; later job docs) and surface it on the read routes. Mirrors how safety-report submissions carry `box_file_id`. Not yet on the field-ops tables/schema.
3. **Op Stds §50 "D1-as-writer" doctrine blessing** — making D1 the primary that mirrors to Smartsheet is a doctrine decision; v18→v19 bump to FLAG to Seth. Plus the §43 successor-remediation runbook for the P2.4 daemon. (The read routes themselves are read-only Worker code → a break is high-capability-class category-4 code-fix-only → no Tier-2-reachable failure mode → **no §43 entry required for the read views**; planning layer to confirm.)

**Optional cheap read-layer hook (deferred, NOT built):** surface jobs `origin`/`sync_state` in the Job-Tracker list/detail response so the portal shows provenance ("from Smartsheet" vs "created in portal") the moment the mirror daemon lands. Small response-shape extension to `fieldops_jobtracker.ts` + lib + page + tests.

**Tag:** `field-ops`, `smartsheet`, `box`, `source-of-truth`, `doctrine`, `planning-layer`, `blocked`. **Revisit when:** Seth gains access to the main Evergreen Smartsheet (real schema + SoR visible) — the hard prerequisite — AND/OR the §50 doctrine bump reaches Seth.

Surfaced: 2026-06-27 (operator forward-compatibility concern, P2.2 read-views session); **moved to BLOCKED 2026-06-28** (operator parked P2.4 — no canonical Smartsheet access). See `project_fieldops-portal-program` + `decision_p2.4-parked-no-smartsheet-access` memories + `docs/session_logs/2026-06-27_field-ops-p2.2-read-views.md`.

## [OPEN 2026-06-27] Field-ops P2.3 write-layer follow-ups (deferred sub-features + governance)

The P2.3 write routes landed complete (PRs #312–#317; `docs/session_logs/2026-06-27_field-ops-p2.3-write-routes.md`). Five tracked follow-ups deferred out of the write slices (item #4 write-UI **RESOLVED 2026-06-28**; four remain):

1. **Inspection quick-log** (the design's Slice 5 also). A lightweight equipment pre-use inspection write (`POST /api/fieldops/equipment/:id/inspection` → `inspections`, version-pinned) was NOT built: there is **no equipment-pre-inspection forms catalog** in the system to validate `form_code` against (the form-editor's published forms are the safety/progress ones, `identity-v<version>`-validated, not equipment inspections). **Blocked on an operator/domain input:** define the equipment pre-inspection forms + their `form_code`s (e.g. `skid-daily`, `telehandler-preuse`). Then it's a quick add — same integrity-bar pattern as the maintenance log + a `form_code` allow-list + server-side version-pin.

2. **H1 — orphaned `cap.admin.equipment` capability key** (security-governance, from the Slice-6 review). Migration 0016 seeds `cap.admin.equipment` + grants it to admin, but **no worker route enforces it** — the roster routes gate on `cap.equipment.manage` (0013), per the design's F2 choice. Current access control is correct (fail-closed, submitter→403), so it was NOT a merge blocker. BUT the live `role_capabilities` table shows admin holding a key that doesn't control any access: an operator on the capability-management surface who grants/revokes `cap.admin.equipment` will silently affect nothing. **Fix before the cap-management UI becomes operator-reachable:** a cleanup migration (e.g. `0019`) `DELETE`ing `cap.admin.equipment` from `capabilities` + `role_capabilities` (touches the capability vocabulary → confirm with Seth). **Tag:** `field-ops`, `capabilities`, `governance`, `migration`.

3. **`cap.tasks.own` 0013 label tidy.** The description says "View + complete OWN assigned + daily-checklist tasks" but the task-status route enforces a **broad** policy (any holder advances any task — field-PM-manages-the-board). Operator CONFIRMED broad (2026-06-27). Update the 0013 description string to match the enforced behavior (cosmetic; a migration-comment / description tidy, not a behavior change).

4. ~~**Write-UI phase.**~~ **RESOLVED 2026-06-28** (PRs #319–#322, all four-part-verified). The forms that drive the P2.3 routes shipped as 4 pure-SPA slices: equipment status+machine-log #319, equipment move+roster admin #320, Job-Tracker create/close/progress/add-task/task-status #321, time-logging #322. Canonical write-UI pattern: `useAuth()` capability-gate (convenience — Worker re-gates) + `postJson` + `crypto.randomUUID` for integrity-bar uuids + reload-after + `vi.mock("../../lib/auth")` (default read-only) test pattern. See `project_fieldops-portal-program` memory.

5. **§50 D1-as-writer doctrine bump** (planning layer / Seth). P2.3 makes D1 an authoritative writer for payroll-grade field-ops data without per-entry human approval (send-free, audit-trailed). Built under the operator's "proceed" go-ahead; the formal Op Stds v18→v19 §50 blessing is the standing P0-ceremony item (see the SoR-integration entry above).

**Tag:** `field-ops`, `p2.3`, `write-routes`. **Revisit when:** the cap-management UI is scheduled (H1), or the equipment-inspection forms are defined (#1). _(Item #4 write-UI RESOLVED 2026-06-28.)_

Surfaced: 2026-06-27 (P2.3 write-routes session); item #4 resolved 2026-06-28 (write-UI phase session).

## [OPEN 2026-06-28] `.dash-section` CSS class duplicates `.card`

The `safety_portal/worker/src/styles/` tree contains a `.dash-section` utility class that is substantially identical to `.card` — same border, padding, border-radius, and box-shadow rules. The duplication is minor (2 classes, ~8 lines) and has no functional impact, but it is a maintenance surface: a future design-system change to `.card` must also update `.dash-section` or the two surfaces drift.

**Fix:** alias `.dash-section` as `@apply .card` or consolidate at the next design-system pass. Not worth a standalone PR.

**Tag:** `field-ops`, `frontend`, `css`, `minor`. **Revisit when:** next design-system consolidation pass.

Surfaced: 2026-06-28 Progress-Reporting program session.

## [PARTIALLY_MITIGATED 2026-07-09] §6a enablement-doc DoD owed per Progress-Reporting slice

**Update 2026-07-09 (WS3 / D2-1, `feat/docs-pdf-pipeline`):** the §6a manifest artifact NOW EXISTS — `docs/enablement/manifest.yaml`, loaded by `docs_pdf/manifest.py`, rendered to branded PDF manuals by `scripts/build_docs_pdfs.py` (the md→PDF pipeline in the new `docs_pdf/` package). It is seeded with all seven enablement guides that exist on main today (`fieldops_checklists`, `manager_tier`, `subcontractor_tier`, `portal_job_creation`, `progress_rollup_numbers`, `crew_time_corrections`, `purchase_orders`). "Registration" is now a concrete action: add an entry (key/title/version/source/sha256) to that YAML. Doc-currency is enforced by `build_docs_pdfs.py --check` (SHA-256 drift; warn-only-friendly, mirrors `regen_doc_indexes --check`). Residual work keeping this open: (a) the in-doc `TODO(operator): register this doc in the §6a manifest` comments in each enablement guide are now actionable and can be retired when those docs are next touched (deferred — editing them triggers a frontmatter retrofit; `crew_time_corrections.md` also lacks conforming `type`/`date` frontmatter); (b) `material_catalog` (M1) still has no capability-guide entry (no guide authored yet); (c) the D2-2 content (ITS Owner's Manual, generated ITS_Config data dictionary) + the D2-3 Box publish leg are not built. See `docs/2026-07-09_aug7_delivery_program.md` WS3.

Per the approved plan (`~/.claude/plans/let-s-go-with-option-greedy-fiddle.md`), every progress-workstream slice that creates a sheet, compiles, or adds a daemon ships a **§43 successor-remediation runbook skeleton + §6a manifest registration in the same PR** (definition-of-done, not a follow-up). The polished distributable PDF (A8 documentation program) is a pre-20-job-cutover requirement.

Currently: M1 (material_catalog, migration 0019 + Worker CRUD + admin SPA) was the first Track M slice and **did not ship a §6a manifest registration** — M1 is D1-local (no Smartsheet sheet, no daemon, no external send), so the §43/§6a DoD obligation is reduced, but the §6a capability manifest should still record the `material_catalog` capability. Track M slices that add daemon paths (M2 bidirectional sync, M3 incidents + photos) have a full §43/§6a obligation.

**Rule going forward:** every slice brief for the Progress-Reporting program must explicitly call out the §6a registration step and the §43 runbook scope (often "None for this slice — read-only/D1-local" is the correct answer, but it must be stated, not omitted).

**Tag:** `progress-reports`, `doctrine`, `§43`, `pre-cutover`. **Revisit when:** each Progress-Reporting slice brief is written.

Surfaced: 2026-06-28 Progress-Reporting program session (approved plan §6/A8 clause).

> **Audit 2026-07-24 (tech-debt janitorial pass):** Residual (c) RESOLVED (D2-2 content PR #515 + D2-3 Box publish leg PR #588). STILL OPEN: (a) six TODO(operator) register comments in docs/enablement/*.md not retired; (b) material_catalog (M1) not registered in docs/enablement/manifest.yaml.

## [OPEN 2026-06-29] Portal permission-model stale plumbing — vestigial + orphaned capabilities, coarse gate, missing crew→job link

**Surfaced 2026-06-29** during a forensic investigation of the portal permission model (operator asked "what happened to my 3-tier permission model that broke my login and got reverted?"). Resolution: the capability system (migration `0013`, PR #302, `8bd9995`) is **live and was never reverted**; the 2026-06-28 login breakage was the deploy-order lockout, fixed operationally. The 5-agent read-only sweep + direct verification surfaced stale/half-wired permission plumbing to address later — **documented, not fixed** (preservation-over-refactor, §14). Relevant to the queued **P2.6 — Manager tier** slice and any future capability-management UI.

1. **Granted-but-never-enforced capabilities** (defined in `0013`, granted to a role, but no route gates on them — routes use `requireSession` or `requireRole('admin')` instead, so the cap is not a security boundary). Originally 4 named: `cap.form.submit`, `cap.form.request`, `cap.inspection.job`, `cap.checklist.manage` (plus `cap.tasks.assign`, tracked as a 5th in the same sweep). Two are now RESOLVED: **`cap.tasks.assign`** by the S1 Assigned-Tasks build (migration `0025`) — task create/reassign routes gate on `cap.jobtracker.manage` OR `cap.tasks.assign` (with a subcontractor-target guard); **`cap.checklist.manage`** by the S2 checklist-engine build (PR #407), carried through R1/R4/R5 (PRs #416/#417/#420) — every checklist CRUD/assign/cancel route in `fieldops_checklist.ts` (`gates.requireCapability(CAP_CHECKLIST)`, ~19 call sites) now gates on it. **Still ungated (1 remains, deliberately):** `cap.inspection.job` — NO surface exists to gate (nothing writes the `inspections` table; job-level inspection forms ride `/api/submit` under `cap.form.submit`). `cap.form.submit` + `cap.form.request` are now ENFORCED (PR #440, 2026-07-03 — intended as a held PR, merged via a disclosed staging error; the deep security review's lockout analysis proved all three roles hold both caps, so no ability was lost): `/api/submit` + the six form-request/download surfaces. Decide enforce-or-remove on `cap.inspection.job` when a job-level inspection surface ships.
2. **3 orphaned capability references** appearing ONLY in `migration 0016_equipment_management.sql` comments (lines 54-55), never defined in `0013`: `cap.inspection.fill`, `cap.dashboard.equipment`, `cap.machine.log` — URS-Marine port leftovers; granting any would fail the `role_capabilities` FK. Clean the comments. (Companion to the already-tracked `cap.admin.equipment` orphan-key cleanup in the "Field-ops P2.3 write-layer follow-ups" entry above.)
3. **Coarse `cap.jobtracker.manage` — RESOLVED by P2.6 (PR #398, 2026-07-01).** `cap.crew.assign` (the 19th capability) + `POST /api/fieldops/personnel/:id/assign` shipped, letting a Manager assign/move crew without granting `cap.jobtracker.manage` (job/task creation stays admin-only). Time entries confirmed orthogonal as designed — a person placed on Job A can log time against Job B without reassignment.
4. **No `personnel.current_job` column / standalone crew→job assignment route — RESOLVED by P2.6 (PR #398, 2026-07-01).** `personnel.current_job TEXT` (migration `0023`) + the assign route above are live. **New finding surfaced scoping the next slice (unified job-create flow):** the job-list and job-detail crew queries in `fieldops_jobtracker.ts` still compute crew from `task_assignments`, NOT from the new `current_job` column — a person placed via the P2.6 route would not show up as crew until that's converged. Tracked as its own slice: see the "Unified job-creation flow" entry above (spec at `~/.claude/plans/spec_unified-job-create-flow.md`, Slice 1) and `memory-archive.md` §G49.6.

**Tag:** `safety-portal`, `capabilities`, `auth`, `field-ops`, `P2.6`. **Revisit when:** item 1 is 2-of-5 RESOLVED 2026-07-01/07-02 (`cap.tasks.assign` by S1, `cap.checklist.manage` by S2/R1/R4/R5) — 3 caps still cheap-open (no trigger yet); item 2 still-open cheap cleanup (no trigger yet); items 3-4 RESOLVED 2026-07-01 (crew-query convergence spun out as its own tracked follow-up, see item 4 note).

Surfaced: 2026-06-29 permission-model forensic investigation; full spec at `~/.claude/plans/what-happened-to-my-floating-porcupine.md`; reusable inventory in the `reference_portal-capability-enforcement-gaps` memory.

---

## R-series spec Deferred #5–#10 — named follow-ups, not in this program [OPEN 2026-07-02]

**Surfaced 2026-07-02**, `~/.claude/plans/refinement-spec-r-series.md` §3 "Deferred / won't-do." Six items were explicitly scoped OUT of the R-series refinement program (R1–R5, R7) as named follow-ups, not silent gaps:

5. **Mid-day template re-sync into open instances.** Admin edits to a checklist template take effect "tomorrow," not on today's already-generated instances (R4 ships copy-only — "changes take effect tomorrow" — snapshot semantics kept as-is).
6. **Mid-day job-reassignment orphan-instance surfacing/auto-cancel.** If a person is reassigned off a job mid-day, their already-generated daily-checklist instance for that job is not auto-cancelled or flagged orphaned; R2's day-rollover refetch narrows the confusion window but doesn't close the gap.
7. **Scoped crew edit/retire for subcontractor-created crew + time amend/void UI** via the `amends_uuid` chain — a data-correction follow-up epic. R2 ships the crew list + duplicate warning; R1/R7 stop new junk rows, but no amend/void UI exists yet.
8. **Server-side completed-history cutoff/deletion.** R2 ships client-side collapse only; history stays queryable/unbounded server-side.
9. **Full URL router.** R3 ships minimal hash/history integration only (push-per-view-change, popstate restore, `beforeunload` dirty-form guard) — not a real router.
10. **`task_assignments.due_date` column.** Considered and deferred per audit; `created_at`/`assigned_by` rendering (R2) covers the urgency-signal gap for now, but there is no due-date field on task assignments.

None of these are regressions — they were locked-decision scope cuts made explicitly, with the reasoning captured in the spec. Listed here so they don't get silently rediscovered as "bugs" in a future session.

**Tag:** `field_ops`, `checklist`, `tasks`, `r-series`, `deferred`. **Revisit when:** planning the next field-ops UX pass — check this list before re-scoping any of the six from scratch.

---

> **Audit 2026-07-24 (tech-debt janitorial pass):** OBSOLETE: #5/#6 (superseded by the D-series SOP daily-form redesign). RESOLVED: #7/#9/#10 (PRs #451/#453/#450). STILL OPEN: only #8 (server-side completed-history cutoff/deletion — 0 implementation hits). The list itself is an intentional locked-decision scope-cut register kept so these aren't re-discovered as 'bugs'.

## Checklist template identity is title-keyed (0026 design) — a same-title admin template collides on re-seed [OPEN 2026-07-02]

**Flagged during the #414 review** (migration `0028_sop_checklist_content.sql`, R-seed). Checklist template find-or-create is keyed on `(kind, title)` — every seed `INSERT` is guarded `WHERE NOT EXISTS (SELECT 1 FROM checklist_templates WHERE kind = 'generic_inspection' AND title = '<exact title>')`, and the `daily_default` re-seed logic is sentinel-guarded on an exact item **label** match. This is a deliberate 0026 design choice (no template "code"/slug column), and it works cleanly for migration idempotency (a re-apply is a no-op).

The edge case: if an **admin authors a template through the UI** with a title that happens to exactly match a future seed migration's title (e.g., re-creates "Excavation / Trench Daily Inspection" by hand), a later migration re-apply — or a future seed migration reusing that exact title — will treat the admin's template as "already exists" and silently **merge items into it** (via the per-item `NOT EXISTS (template, label)` guard) rather than creating a separate template. Blast radius is low today: templates are seeded once (0026 placeholder → 0028 real content) and there's no evidence of an admin having hand-authored a colliding title.

**Fix (if it becomes live):** add a stable template `code`/slug column distinct from the human-editable `title`, and key find-or-create on `code`. Only worth doing if the inspection/checklist template library grows past the current seeded set and admin-authored templates become common — preservation-over-refactor (§14) says don't build this speculatively.

**Tag:** `field_ops`, `checklist`, `templates`, `data-model`, `r-series`. **Revisit when:** the checklist/inspection template library grows beyond the seeded set, or an admin reports items merging into the wrong template.

---

## D1-primary tables have no ITS-side backup — Cloudflare D1 Time Travel is the restore path (accepted) [OPEN 2026-07-03]

**R3-F7 (resiliency audit), decision: don't build a backup job — document the restore path.** Two tables are **D1-primary** (no Smartsheet/Box mirror; ITS holds no other copy): `job_daily_requirements` (per-job daily-form requirement overlay, migration `0030`/`0032`) and `job_expected_materials` (per-job expected-receipts list, migration `0031`). Everything else in D1 is either a queue drained to the Mac (submissions → filed PDFs), a mirror of Smartsheet (`ITS_Active_Jobs` sync), or re-derivable. Receipt EVIDENCE already survives outside D1 — a confirmed receipt appends a `deliveries_received` row into the filed daily PDF, and an incident files its own material-incident submission — so a D1 loss cannot silently erase what was received.

**Restore path (operator, Tier-3/Seth):** Cloudflare **D1 Time Travel** — every D1 database keeps 30 days of point-in-time restore (`npx wrangler d1 time-travel info its-safety-portal-db`, then `… time-travel restore its-safety-portal-db --timestamp=<unix|ISO>`). Restore rolls back the WHOLE database, not one table — expect to replay any submissions queued after the restore point (the Worker re-serves unfiled rows; already-filed PDFs are safe on Box/Smartsheet).

**Blast radius if lost outright (>30 days / Time Travel unavailable):** re-enterable admin data — the office re-keys each job's requirement items and expected-materials rows from the client's punch list. Bounded, annoying, not evidence-destroying. That bound is WHY no ITS-side backup job is built (§14; the audit explicitly rejected one).

**Tag:** `field_ops`, `d1`, `resilience`, `runbook`, `accepted`. **Revisit when:** a third D1-primary table lands (re-evaluate the no-backup call), or Cloudflare changes the Time Travel retention window.

- **[OPEN 2026-07-03] `_write_heartbeat()` liveness-touch called bare across all 6 daemon consumers** — a
  local-disk `OSError` from `HeartbeatReporter.write_liveness()` (`state_io.atomic_write_text` raises
  natively) would propagate out of the poll/publish loop and skip that cycle's health-row +
  watchdog-marker writes. Pre-existing live pattern (PR #344) replicated verbatim by the CS3 consumers
  per review; the right fix is ONE shared-level catch inside `shared/heartbeat.py::write_liveness`
  (never-blocks-primary-work applied to the liveness half too), not six call-site wraps. (CS3 ops-stds
  review WARN, 2026-07-03.)

- **[OPEN 2026-07-03] G1 item-photo queue: no explicit queue-AGE signal + refusal-spam window** — the
  stuck-pending >7d prune WARN + the portal_poll heartbeat notes are the only backlog signals (the
  brief's req-5 wanted an age signal; deferred as minimal-viable). A hostile account spamming refused
  photos pages once per dedupe window (Sentry+Resend deduped post-#449; ITS_Errors records per
  occurrence, bounded by Check O rotation) — accepted posture, revisit if it fires in practice.
  (G1 regression review WARNs, 2026-07-03.)

- **[OPEN 2026-07-03] Daily-form date-flip discards attached photos (second in-session loss path)** —
  `onDateChange` applies drafts without the photo overlay: flip-away wipes live photos and flip-back
  can't restore them (drafts are photo-stripped by quota design). Defensible (photos belong to their
  date) but the in-code honest-regression comment frames unmount as the only loss path — this is a
  second. Fix = the same functional-overlay pattern if it bites in practice. (Photo-disappear fix
  review NIT, 2026-07-03.)

## Converge `fieldops_sync`/`portal_poll` onto the shared `shared/sustained_failure.py` counter [OPEN 2026-07-20]

PR #635 extracted `SustainedFailureCounter` FROM `fieldops_sync`/`portal_poll`'s existing private
per-daemon sustained-failure counters and wired the shared version into the four newer
`po_materials`/`subcontracts` poll daemons (`estimate_poll`/`rfq_poll`/`po_poll`/`subcontract_poll`) —
answering "why did nothing fire during the #632 21h estimate-pending-fetch outage" (every fire surface
keys on CRITICAL; a per-cycle ERROR storm was invisible until 5 consecutive cycles escalate). The two
original daemons (`field_ops/fieldops_sync.py`, `safety_reports/portal_poll.py`) were deliberately left on
their own pre-existing copies this session (§14 preservation-over-refactor — no live bug in either, pure
duplication, not worth touching mid-feature-session). Migrating both onto the shared module removes the
last duplicated copies of this pattern. Trigger: next touch to either daemon, or a dedicated
observability-consolidation pass. See `docs/session_logs/2026-07-20_po-hub-tab-fold.md`;
`shared/sustained_failure.py` CLAUDE.md row already documents the gap.

## Legacy jobs missing structured `job_no`/address after migration 0057 — per-job manual backfill only [OPEN 2026-07-20]

Migration 0057 (PR #634) added `jobs.job_no` (the Evergreen `YYYY.NNN` number) and structured
`address_city`/`address_state`/`address_zip` columns to the jobs SoR, but existing rows are backfilled only
when an operator edits that specific job via the tracker's "Edit job details" page (#636) — there is no
bulk-backfill script. Only Coker (JOB-000028) is filled so far (operator request, this session). Any report
or builder feature that assumes `job_no`/address is populated fleet-wide will see blanks for every
unedited job. Trigger: before any feature that reads `job_no`/address across ALL jobs (not just the
per-job dropdown autofill, which already degrades gracefully to a name-prefix fallback); or a dedicated
data-entry pass by the office.

## Watchdog Check W (`shared/log_rotation.py`) ships archive-only — the delete stage is deferred pending an off-host-copy decision [OPEN 2026-07-21]

PR #651 shipped Check W's `run_log_rotation` as **v1: gzip-in-place, never delete** — daily `logs/<date>.log`
files older than 14 local days become verified `.gz` siblings (original removed only after a streamed
sha256+length round-trip confirms the archive), and `logs/launchd/<daemon>.out.log` gets a copy→verified-`.gz`
→`os.truncate(path, 0)` in place (inode preserved for the daemon's held fd). Nothing is ever unlinked once
archived; `.gz` files accumulate under `~/its/logs/` bounded only by disk (1.1 TiB free as of this session, so
not urgent). The PR explicitly scoped the delete stage out: **"The only irreversible op ships separately,
after an off-host copy exists."** Per Op Stds §44, "off-host copy of the forensic record" is a **FIXED
high-capability-class decision** (secrets/infra scope, not a Tier-2 repair) — and it isn't a free choice:
`shared/redact.py` / the §54 backstop doctrine rules out Box as the destination (it's an Evergreen customer
system of record, not an ITS operational archive target), so the real decision is which off-host mechanism
(a dedicated cloud bucket, an encrypted external volume synced on a schedule, something else) Seth wants
before any deletion is safe to build. **Trigger:** when Seth picks an off-host-copy mechanism, or when `.gz`
accumulation is large enough to matter (watch via `du -sh ~/its/logs` — no automated size-of-archive alarm
exists yet, only the existing per-run 1 GiB single-file size-cap skip). **Tag:** `watchdog`, `log_rotation`,
`observability`, `high-severity` (decision-gated, not urgent).

## Watchdog Check W dropped the brief's per-file mtime incident-skip guard — Option B (size-ceiling override) is a possible future restore, not built [OPEN 2026-07-21]

The brief that spec'd Check W (#651) pinned a per-file *"skip any launchd file whose `st_mtime` is within N
minutes"* guard, meant to avoid truncating a file mid-incident. The implementation deliberately dropped it —
flagged explicitly in the PR body rather than silently kept — because it fatally exempted exactly the files
that most need truncating: `portal_poll`'s `.out.log` (the largest target, ~36 MB) writes every 60s, so its
mtime is *always* "recent," meaning the guard would have made it **never eligible for truncation**, defeating
the check's purpose on its biggest offender. Seth ratified the deviation as-is this session (walked through
danger/purpose/doctrine; see `project_cutover-builders-and-logs-growth-2026-07-21.md` auto-memory and
blueprint memory-archive §G73) — the real incident guard that remains is the open-CRITICAL whole-lane hold
(Check W records but does not page during an open incident), and copy-gz-truncate archives content to a
verified `.gz` *before* truncating, so `tail -f` and the archived record both survive. **Possible future
refinement, NOT currently built:** "Option B" — restore a per-file mtime skip but gate it with a size
ceiling (e.g. ~5 MB) so small, recently-written files keep the mid-tail courtesy while large busy files
(like `portal_poll`'s) still truncate regardless of mtime. Only worth building if the operator later wants
the courtesy back for some smaller daemon's `.out.log`; no known daemon exhibits the "truncated mid-tail"
symptom this would guard against as of this session. **Trigger:** Seth requests the mtime courtesy back, or
a smaller daemon's log is observed truncated at an inconvenient moment during a live incident. **Tag:**
`watchdog`, `log_rotation`, `low-severity`, `deferred-refinement`.

## Archive-on-closure — defined/implemented for one narrow slice, never proven live; ~11 per-job surfaces have no closure path at all [OPEN 2026-07-23]

A 3-lens adversarial audit run during the 2026-07-23 tenant-wipe/stand-up rehearsal — code-level
(`~/its/logs/reviews/2026-07-23_arch_code.json`), doctrine/runbook-level (`2026-07-23_arch_docs.json`),
per-job-surface inventory (`2026-07-23_arch_inventory.json`) — converged on one verdict for "what happens
when a project is archived": Op Stds v21 §51 + its folded riders define archive-on-closure for **exactly
one slice**, the four progress standing-tracker sheets (Hours Log, Equipment, Material List, Material
Incidents), implemented by `field_ops/fieldops_sync.py:811-869` (`_archive_closed_job_trackers` →
`shared/smartsheet_client.move_sheet_to_folder`, its#462, PR #465) and never anything else.

**Three compounding gaps, none of them fixed by this audit (diagnosis only):**

1. **Trigger-semantics mismatch.** The move fires only when a portal-origin job's `lifecycle` is explicitly
   set to `'archived'`. The portal UI's own documented normal close path is `'inactive'`
   (`fieldops_job_write.ts:273-274`) — which does **not** archive anything. Nothing in the UI or any doc
   tells an operator that closing and archiving are different actions with different consequences.
   Smartsheet-origin jobs are structurally exempt entirely (`fieldops_sync` mirrors only portal-origin dirty
   jobs), so setting `Archived` directly in `ITS_Active_Jobs` can never trigger the move.
2. **Closure policy undefined for ~11 other per-job surfaces.** Safety + progress week sheets and their
   per-job Smartsheet folders, WSR/WPR human-review rows, PO/RFQ/estimate/subcontract per-job mirror sheets
   (`shared/job_sheet.py`-created) and their flat Log rows, and **all** per-job Box content
   (`shared/box_client.py` has zero move/archive primitive) have no archive path defined OR implemented
   anywhere. The only whole-project procedure on record is the destructive manual 3-system `purge-job` nuke
   (D1 cascade + manual Smartsheet + manual Box) — deletion, not archival, with its own documented footgun
   (delete the `ITS_Active_Jobs` row first or the down-sync re-creates it; HOUSE_REFLEXES §7).
3. **§30 live smoke never run.** The committed operator-run live integration test
   (`tests/test_smartsheet_client_integration.py::test_move_sheet_to_folder_relocates_live`) has been a
   listed pre-reliance TODO since the 2026-07-04 session log
   (`docs/session_logs/2026-07-04_smartsheet-verify-hours-smoke-archive-on-closure.md`) and no later session
   log records it running. Decisive corroboration: the `ITS — Archive` workspace held **0 sheets** in the
   2026-07-23 pre-wipe dump (`~/its/logs/migrations/prewipe_20260723T030026Z/_manifest.json`) — no job has
   ever actually reached `lifecycle='archived'` against live data in this system's history.

**A concurrent Brief-A session opened PR #678** (`docs/project-closure-archive` — a project-closure runbook
+ closure-policy proposal) addressing the doc side of gaps 1/2 above; check `gh pr list`/`gh pr view 678`
before starting doc work here, it may already be answered. Gap 3 (the live smoke) and any code-level fix for
gap 1/2 remain open regardless of what #678 lands, since #678 is docs/proposal-only. **Trigger:** Seth
reviews the three dossiers + PR #678 and decides (a) whether `'inactive'` should also archive or archiving
stays deliberate, (b) a closure policy for the ~11 uncovered surfaces (retain-in-place is the current
de-facto policy, never chosen explicitly), (c) schedules the overdue live move smoke. **Tag:** `field_ops`,
`archive-on-closure`, `its#462`, `§51`, `audit`, `seth-owned`.

## Un-adopted optimization findings from the 2026-07-23 stand-up rehearsal — 2 of 5 named opportunities remain unclaimed, 3 already in flight [PARTIALLY RESOLVED 2026-07-23 — 3 residuals still open, see audit note]

**RESOLVED 2026-07-23 (Brief A close-out, PRs #673/#675/#676/#679/#680/#683/#685/#686/#687, all
four-part-verified, exec HEAD `fc27f75`):** all 5 named opportunities below landed. The 3 "already in
flight" items merged as claimed — finish/epilogue → **#679**, CL-12 repoint actuator → **#680**, CL-11
shares + VC-10 → **#685** (reconciled with #674's ACT-fence, which #679 adopted wholesale, deleting its own
`_run_marker.py`; its#677 closed as superseded). Both "genuinely unclaimed" items also landed: item 1
(checklist/punchlist/README collapse around the one-step stand-up + the CL-12 "all gates true" doc-bug fix)
→ **#686**; item 2 (run-branch mode) → **#687** (per-run `standup/run-<UTC>` branch, per-stage checkpoint
commits with a `:(exclude)logs` pathspec, `--resume` merge-main-then-STOP-on-conflict flow, landing-PR
push). Of the "lower-priority" findings, one more also landed: scoping `sheet_ids_regen --retry-missing` to
the unresolved `--expect` constants' workspaces → **#683**. Remaining lower-priority findings (more CL items
as mechanical `verify_cutover` checks, the generic dump-restore utility, the config-seed-engine dedup) are
genuinely still open — see the new `docs/session_logs/2026-07-23_standup-process-optimization.md` (PR #688)
and the fresh tech-debt entries below for what's left. Original findings preserved below for the record.

Three review passes over the 2026-07-23 tenant wipe+stand-up rehearsal produced optimization dossiers —
operator-experience (`~/its/logs/reviews/2026-07-23_opt_operator.json`), runtime/safety
(`2026-07-23_opt_runtime.json`), code-simplification (`2026-07-23_opt_simplify.json`) — proposing follow-on
work beyond what shipped this session (exec PRs #664-672/#674). **A concurrent Brief-A session has already
claimed 3 of the higher-value findings** — confirmed via `gh pr list`/worktree inspection at the time of
this entry, re-check before acting on any of these:

- **Standup finish/epilogue mechanization** (codify the by-hand post-merge tail: state cleanup, posture-
  flagged fleet reload, cycle-wait, heartbeat/error verify, gate-flip report) — **IN FLIGHT, PR #679**
  (`feat/standup-finish`, open as of this writing). Do not re-build.
- **CL-12 repoint actuator** (`production_repoint.py` — a declarative plan/commit tool for the production
  repoint changeset) — **IN FLIGHT, PR #680** (`feat/production-repoint`, open as of this writing). Do not
  re-build.
- **CL-11 approver-share manifest + a mechanical VC-10 approver-shares `verify_cutover` check** — **IN
  PROGRESS**, local WIP branch `feat/production-shares` (worktree `~/its-shares`, one commit ahead of
  `origin/main` as of this writing, not yet pushed/PR'd: "mechanize CL-11 — approver-share manifest +
  guarded seeder + VC-10 approver-shares gate"). Do not re-build; if picking this up, coordinate with
  whoever owns that worktree first.

**Genuinely still unclaimed, no worktree/PR found for either as of this writing:**

1. **Collapse the cutover checklist's Smartsheet/Box stand-up into ONE `standup.py` invocation.** The Aug-7
   punch-list's cutover-day binder (`docs/operations/cutover_checklist.md`,
   `docs/operations/cutover_operator_punchlist.md`, `docs/runbooks/host_migration_runbook.md`) has zero
   mentions of `standup.py`/`sheet_ids_regen.py` (grep-confirmed) and `scripts/migrations/README.md` still
   documents the superseded manual builder-by-builder walk with hand-pasted FLIP steps as "the Phase-1
   cutover builder sequence" — on the real cutover day an operator following the binder as written would
   redo the exact ordeal this rehearsal just paid to retire. **Same PR should fix the CL-12 doc bug:**
   `cutover_checklist.md`'s CL-12 line asserts "all `*_enabled` gates true," which contradicts CL-03 (send
   daemons dark), CL-13 (read gate Descriptions first), the standup epilogue's own stated posture ("gates
   seeded DARK — the production posture"), and HOUSE_REFLEXES §5's "static text must never assert a LIVE
   gate state" rule — rewrite as "gates flipped per the activation plan, send gates last, Seth."
2. **Auto-created run-branch mode for `standup.py`** — commit regen output per stage and merge
   `origin/main` into a dedicated run branch, instead of the stash/pull/pop dance the operator improvised 6
   times this session to land mid-run fix-PRs (#665-#672) without losing in-progress regen state.

Lower-priority findings recorded in the dossiers but not named here (see the JSON files directly for full
detail): enrolling more manual CL items as mechanical `verify_cutover` checks (CL-15/CL-17/CL-19); rejecting
the scratch-prefix dress-rehearsal mode in favor of scheduling an early real production stand-up; documenting
why a production wipe variant is deliberately out of scope; scoping `sheet_ids_regen --retry-missing`
re-resolution to only the unresolved `--expect` constants' workspaces; a generic dump-restore utility for
any sheet on demand; extracting one shared config-seed engine module (parameterize-not-clone) behind the
existing per-lane `seed_*.py`/`build_*.py` files. **Trigger:** before starting any of the "genuinely
unclaimed" items, re-run `gh pr list` and `git worktree list` — the concurrent session was still active at
the time this entry was written and may have claimed more ground since. **Tag:** `migrations`, `standup`,
`cutover`, `optimization`, `parallel-session-coordination`.

> **Audit 2026-07-24 (tech-debt janitorial pass):** The 5 NAMED opportunities landed (PRs #679/#680/#685/#686/#687, all four-part-verified). STILL OPEN — three lower-priority rehearsal-dossier residuals that have NO separate tech-debt entry, retained here so they aren't lost: (1) a generic on-demand dump-restore utility for any sheet; (2) one shared config-seed engine module (parameterize-not-clone behind the per-lane seed_*/build_* files); (3) enrolling CL-15/CL-17/CL-19 as mechanical verify_cutover checks.

## `_loaded_its_daemons`/`_loaded_its_labels`/`_launchctl_list` — a launchd-query helper now has 3-4 near-identical copies [OPEN 2026-07-23]

The Brief-A stand-up hardening pass (PRs #673–#687, see `docs/session_logs/2026-07-23_standup-process-optimization.md`)
brought the count of near-identical "shell out to `launchctl list`, parse for `org.solutionsmith.its.*`
labels" helpers to 3-4 across `scripts/migrations/wipe_tenant.py`, `scripts/migrations/standup.py`,
`scripts/verify_cutover.py`, and `scripts/migrations/production_repoint.py` — each one independently
re-derives the same daemon-label/loaded-state check rather than sharing a `shared/launchd.py` primitive.
Op Stds §14 (preservation-over-refactor) sets a ≥4 real reuse cases threshold before a convergence PR is
warranted, not "still open" or "collision-safe" alone (HOUSE_REFLEXES §6 "don't harden dormant subsystems").
This has now reached that threshold on count, but **do not build the extraction speculatively** —
`improve-codebase-architecture` is a constrained skill requiring explicit operator approval, and the four
call sites currently differ slightly in what they need back (label list only vs. loaded/unloaded state vs.
a specific daemon's status), so the shared shape needs Seth's confirmation, not just a mechanical dedup.
**Trigger:** next time a 5th consumer needs the same launchd-query logic, or Seth explicitly greenlights the
extraction. **Tag:** `migrations`, `standup`, `§14`, `launchd`, `refactor-candidate`.

## `seed_production_shares.list_workspace_shares` not enrolled in the family's `_rest_retry` transient-retry seam [OPEN 2026-07-23]

`shared.smartsheet_client.list_workspace_shares` (new, PR #685, backing CL-11/VC-10) is a read-only helper
and was deliberately left off the `scripts/migrations/_rest_retry.py` bounded-retry allowlist that #673
introduced for the wipe/standup/regen family's write-and-restore paths — it doesn't currently back a hot
path where a transient 429/5xx would be costly to hand-retry, and the AST-locked approved-callers list in
`_rest_retry.py` would need to grow to admit it. **Trigger:** if `list_workspace_shares` starts backing a
polling daemon or another frequently-invoked path (rather than the current one-shot CL-11 seeding/verify
use), enroll it in `_TRANSIENT_RETRY` in the same PR that adds the new consumer. **Tag:** `migrations`,
`cutover`, `CL-11`, `shares`, `low-severity`.

## `seed_production_shares.py` `already_present` check is presence-only, not access-level-aware [OPEN 2026-07-23]

The ADD-only CL-11 shares seeder (`scripts/migrations/seed_production_shares.py`, PR #685) treats an
approver already present on a workspace's share list as "done" — it does not check WHAT access level that
existing share carries. A workspace where the manifest expects an approver at EDITOR but the live share is
actually VIEWER-only will show `already_present` and be silently skipped, even though the approver cannot
actually exercise F22 approval authority (checkbox-flip requires write access) until manually corrected.
This was called out as a manual spot-check note in the #685 PR body rather than a code fix, because
narrowing the ADD-only seeder into an access-level-comparing one changes its risk profile (an automated
"fix the access level" step starts editing existing production shares, not just adding new ones — a
bigger, more dangerous surface than the reviewed PR's scope). **Trigger:** before relying on VC-10
`approver-shares` as a complete go/no-go signal at cutover, manually cross-check each flagged
"already_present" approver's actual access level against the manifest's expected level — do not assume
presence implies correct access. **Tag:** `migrations`, `cutover`, `CL-11`, `shares`, `F22`, `seth-owned`.

## `render_submission_pdf` is not byte-deterministic — pre-existing, deliberate, now explicitly documented (2026-07-23, PR #693)

The document-polish session's byte-determinism adversarial-review lens verified PO/RFQ/subcontract-package/
zip/quote-form renders are byte-identical across repeated in-process AND cross-process runs (different
`PYTHONHASHSEED`s) — `render_submission_pdf` (the safety/progress form-PDF renderer in `form_pdf.py`) was
NOT included in that determinism set and is not expected to be: it embeds a wall-clock "Filed at" style
timestamp in the rendered output by design, so byte-identity across two renders of the same submission is
not a meaningful property for it. This predates PR #693 and is not a regression the session introduced —
it is recorded here because the adversarial-review pass surfaced it as worth naming explicitly rather than
leaving it as an implicit assumption. **Trigger:** none — informational; revisit only if a future feature
(e.g. a render-diff/dedup tool) needs safety/progress PDFs to be byte-deterministic, at which point the
timestamp field would need to move out of the rendered bytes (e.g. into filename/metadata only).
**Tag:** `form_pdf`, `determinism`, `informational`, `low-severity`.

## `form_pdf._esc` does not escape quote characters — safe today only because no untrusted string reaches a Paragraph attribute-value slot (2026-07-23, PR #693 escaping red-team finding)

The escaping red-team lens of PR #693's adversarial verify pass confirmed `_esc` (the HTML-escape helper
feeding ReportLab `Paragraph` markup) correctly neutralizes `<`, `>`, and `&` across all five changed
surfaces (positive+negative controls), but does not escape `"`/`'`. This is not exploitable today because
every current call site interpolates escaped data only into Paragraph TEXT CONTENT, never into an
attribute-value position (e.g. `color="..."` or `name="..."`) where an unescaped quote could break out of
the attribute and inject markup. The residual risk is a future change that interpolates external/operator
data into a `Paragraph` markup ATTRIBUTE rather than element content. **Discipline going forward:** never
interpolate untrusted or operator-editable data into a ReportLab Paragraph markup attribute value (only
into element content, which `_esc` already covers); if an attribute-position use case ever arises, extend
`_esc` (or a dedicated attribute-escaper) to cover quotes before shipping it, and add a red-team test proving
the injection is neutralized before merge (per the exec `CLAUDE.md` "adversarial review is
definition-of-done on any trust-boundary surface" rule). **Trigger:** any future `form_pdf.py`/PO/RFQ/
subcontract renderer change that interpolates data into a Paragraph markup attribute rather than content.
**Tag:** `form_pdf`, `escaping`, `security`, `informational`, `low-severity`.

## F22 token-identity self-exclusion filter — DEFERRED until the dedicated its@ Smartsheet token [DEFERRED 2026-07-23]

Phase 1 runs on an operator-designated personal Smartsheet PAT (D1,
`docs/operations/phase1_cutover_decisions.md`); that account owns all ITS workspaces, so — per the
Op Stds §46 owner-inclusion open question — the token identity is inherently within every F22
approver set. A self-exclusion filter (subtract the token identity's own email from the approver
set) is therefore DELIBERATELY not shipped now: it would also remove that account's legitimate
human approval authority. Accepted residual = the §46 owner-inclusion residual (identity is matched
email-only, `shared/approval_verification.py:35-38`) — the same posture the mirror ran under.

**Scoped design (build when triggered):** ONE seam —
`safety_reports/send_poll_core.py::_load_authorized_approvers` (~:220, currently returns
`smartsheet_client.list_workspace_share_emails(config.f22_workspace_id)` verbatim) subtracts a new
`shared/smartsheet_client.get_current_user_email()` (`GET /users/me`, mirroring the
`list_workspace_share_emails` raw-REST pattern at ~:2021–2070, decorated `@_breaker_guard` +
`@_transient_retry` — which REQUIRES adding the name to `APPROVED_RETRY_ENROLLMENT` in
`tests/test_smartsheet_retry.py`, a set-equality assertion that RED-lights otherwise).
EMPTY_ALLOWLIST interaction: on an automation-only workspace (token identity is the sole share)
the subtraction empties the set → `verify_approval` blocks ALL sends fail-closed — intended, not a
bug. Tests to touch: `tests/test_weekly_send_poll.py:132-160` (the `_load_authorized_approvers`
trio), `tests/test_send_poll_core.py`, `tests/test_approval_verification.py`.

**Revisit when:** the its@ Smartsheet identity migration (a dedicated its@ seat replaces the
operator-designated personal PAT) — ship the filter in the same change that swaps the token.
**Tag:** `f22`, `security`, `send-gate`, `cutover`, `seth-owned`.

## resend_client.DEFAULT_FROM swap — blocked on CL-10 solutionsmith sender-domain verification [OPEN 2026-07-23]

`shared/resend_client.py` still ships `DEFAULT_FROM = "onboarding@resend.dev"` (:56) — the Resend
sandbox sender. Swapping to a real sender is a follow-up to the alerting-constants PR and is
BLOCKED on CL-10 (`docs/operations/cutover_checklist.md`): the solutionsmith sender domain must
show `Verified` in the Resend dashboard first — an unverified-domain `from` makes Resend reject
every CRITICAL alert email, which is the out-of-band alert leg (worse than the sandbox sender).
One-line constant change once CL-10 is green; operator alerts only, NOT a customer send path
(Invariant 1 untouched).

**Revisit when:** CL-10 shows the solutionsmith domain `Verified` in Resend — swap `DEFAULT_FROM`
in the same session and live-fire one test alert to confirm delivery. **Tag:** `alerting`,
`resend`, `cutover`, `CL-10`, ~~`low-severity`~~ **`CONFIRMED-LIVE`**.

**2026-08-06 — CONFIRMED LIVE, severity raised from `low` to `HIGH`. This entry's own prediction
came true; the CRITICAL email leg is dead in production right now.** Two watchdog sweeps this
session (23:08Z, 23:10Z) both surfaced, from `_check_alert_dedupe_summaries`:

```
ResendAuthError('HTTP 403: You can only send testing emails to your own email address
(seths@evergreenmirror.com). To send emails to other recipients, please verify a domain at
resend.com/domains, and change the `from` address to an email using this domain.')
```

This is **broader than the `DEFAULT_FROM` swap this entry scopes**. With no verified domain the
Resend account is in test mode, which constrains the **recipient** as well as the sender: the only
deliverable address is the account owner's own (`seths@evergreenmirror.com`), while
`system.operator_email` is `its@evergreenrenewables.com`. So **every** operator alert email is
rejected, not merely mis-branded — `resend_client.send_alert` defaults `to` to that config row
(`shared/resend_client.py:180`).

**Why this matters more than the severity suggested:** it converged with the two 2026-07-15
error-flood gaps (see that entry, ~line 466) during a **12.7-hour Smartsheet outage on 2026-08-06
(10:23Z→23:04Z, HTTP 500 code 4000 + read timeouts, breaker OPEN 729 min)**. All three legs of the
triple-fire degraded at once:

| Leg | State during the outage |
|-----|-------------------------|
| `ITS_Errors` record | **lost** — Smartsheet was the thing that was down (the 2026-07-15 durability gap, 2nd occurrence) |
| Resend email push | **403 rejected** — this entry |
| Sentry push | fired — the *only* surviving leg |

The outage produced **no `ITS_Errors` CRITICAL row at all**; the sole forensic record is
`~/its/logs/2026-08-06.log`. A 12.7-hour full-fleet freeze of every Smartsheet-backed daemon —
"approved sends FROZEN and nothing is being filed" — went unnoticed until an operator-initiated
diagnosis. Operator decision this session: **leave it, log it** (domain verification is Seth's to
do); recorded here rather than repointing `system.operator_email`, which was offered and declined.

**Two fixes, either one restores the leg:** (a) verify a domain at `resend.com/domains` and swap
`DEFAULT_FROM` — the original scope, and the correct fix; or (b) interim, repoint
`system.operator_email` to `seths@evergreenmirror.com`, the one address Resend will currently
deliver to. **Trigger: raised to next session — do not wait for the CL-10 cutover slot.**

## `GET /api/recent` is session-scoped but not ownership-scoped — any authenticated portal user can read any job's latest submission payload [OPEN 2026-08-07]

**Re-filed from the archived "Safety Portal M1" entry on 2026-08-07.** M1's headline defect (a submitter
silently overwriting a peer's PENDING submission with no audit trail) no longer exists at HEAD — cross-actor
overwrite is refused with a 409 `uuid_conflict` (`safety_portal/worker/index.ts:857`) and every replace writes
an atomic audit row (`:924`). That entry was archived because a security-sounding headline which is false at
HEAD distorts every read of the portal's security posture. **This narrower residual survives and is filed here
under its own honest title so it is not lost with the archive.**

`app.get("/api/recent", requireSession, …)` (`safety_portal/worker/index.ts:623`) gates on a valid session but
applies **no per-job ownership predicate** — the SELECT is scoped only by job / form / date. Any authenticated
user can therefore pull any active job's latest `submission_uuid` + payload (the Amend-prefill path). This is
an authorization gap, not an authentication one: the caller must be logged in, so it is not remotely
exploitable, but the portal's user population now includes **subcontractors**, which widens who "any
authenticated user" means relative to when M1 was first written (2026-06-09).

**Fix:** scope the `/api/recent` query by the caller's job access (the same predicate the rest of the
field-ops read routes use), or reject a request for a job the session has no claim on. **Adversarial review is
definition-of-done here** — this is a D1 read-route fed by client-supplied identifiers, exactly the surface
`portal-worker-security-reviewer` exists for; unit tests structurally cannot find an authorization gap.
Coordinate with in-flight Worker edits before touching `index.ts`.

**Revisit when:** the next Worker security-hardening pass, or before the subcontractor user population grows
beyond the current pilot set. **Tag:** `safety-portal`, `security`, `authorization`, `worker`, `medium`.

