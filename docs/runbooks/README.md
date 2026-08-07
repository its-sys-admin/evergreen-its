# Runbooks

Successor-Remediation runbook entries (Op Stds v16 §43). Each entry is
plain-language Markdown shipped **with** a capability, written for the
**Successor-Operator** (a trained operator who runs Claude Code and reads
Smartsheet rows + alert emails, but not code). Claude loads the relevant
entry to drive a Tier-2 repair; the operator sees the evidence and approves.

These are the operator-facing counterpart to the code-reader `§42`
docstrings/comments in the modules themselves — same capability, different
audience (see [`../operations/doc_conventions.md`](../operations/doc_conventions.md)
and Op Stds §43 vs §42). Fault-driven entries follow the §43 four-part
shape (Symptom → What the Successor-Operator checks → The Claude prompt or
UI action → Escalate-to-Seth condition); a few tree-exempt how-to entries
(role guides, config-sheet setup, job management, project closure) follow
the plain `type: operations` procedure shape instead. All use
`type: operations` frontmatter (the conforming type for runbook/procedure
docs — the convention has no separate `runbook` type).

<!-- BEGIN AUTO-INDEX -->
| Date | Type | Status | Workstream | Title | PRs |
|------|------|--------|------------|-------|-----|
| 2026-08-07 | operations | active | field_ops | [Runbook — Materials-manifest importer (`manifest_poll`) (an uploaded BOM or shipping log stuck / refused / dark) (Successor-Remediation, Op Stds §43)](material_manifest_import.md) | _–_ |
| 2026-07-23 | operations | active | _–_ | [Runbook — Close or archive a project (what actually happens today) (Successor-Remediation, Op Stds §43)](project_closure.md) | _–_ |
| 2026-07-23 | operations | active | infrastructure | [Runbook — Tenant wipe / stand-up / finish lifecycle (Successor-Remediation, Op Stds §43)](tenant_standup.md) | _–_ |
| 2026-07-22 | operations | active | infrastructure | [Runbook — ITS_Errors triage (reading the error log + the mark-resolved / clear verbs) (Successor-Remediation, Op Stds §43)](its_errors_triage.md) | _–_ |
| 2026-07-22 | operations | active | _–_ | [Runbook — ITS_Review_Queue triage (working the human-review queue + the resolve verb) (Successor-Remediation, Op Stds §43)](review_queue_triage.md) | _–_ |
| 2026-07-22 | operations | active | infrastructure | [Runbook — ITS_Time_Off + the reviewer chain (PTO entry, chain skip logic, watchdog Check D gaps) (Successor-Remediation, Op Stds §43)](time_off_reviewer_chain.md) | _–_ |
| 2026-07-21 | operations | active | infrastructure | [Runbook — On-disk log-directory rotation (watchdog Check W) (Successor-Remediation, Op Stds §43)](log_dir_rotation.md) | _–_ |
| 2026-07-19 | operations | active | po_materials | [Runbook — Vendor-estimate importer (`estimate_poll`) (an uploaded estimate stuck / refused / dark) (Successor-Remediation, Op Stds §43)](estimate_import_path.md) | _–_ |
| 2026-07-19 | operations | active | po_materials | [Runbook — RFQ generation path (`rfq_poll`) (a composed RFQ stuck / refused / dark) (Successor-Remediation, Op Stds §43)](rfq_generation_path.md) | _–_ |
| 2026-07-19 | operations | active | po_materials | [Runbook — RFQ send daemon (`rfq_send`) (an RFQ row stuck HELD / blocked / "contamination") (Successor-Remediation, Op Stds §43)](rfq_send.md) | _–_ |
| 2026-07-15 | operations | active | _–_ | [Runbook — Subcontract send daemon (`subcontract_send`) (a subcontract row stuck HELD / blocked / "contamination") (Successor-Remediation, Op Stds §43)](subcontract_send.md) | _–_ |
| 2026-07-11 | operations | active | _–_ | [Runbook — Subcontract generation path (`subcontract_poll`) (a queued subcontract stuck / fenced / dark) (Successor-Remediation, Op Stds §43)](subcontract_generation_path.md) | _–_ |
| 2026-07-10 | operations | active | po_materials | [Runbook — Config-editor actuator (`config_actuator`) (a queued config edit stuck / failed / dark) (Successor-Remediation, Op Stds §43)](config_actuator.md) | _–_ |
| 2026-07-10 | operations | active | global | [Runbook — Operator Dashboard Class-A config editor (Successor-Remediation, Op Stds §43)](operator_dashboard_config_editor.md) | _–_ |
| 2026-07-10 | operations | active | global | [Runbook — Operator Dashboard sensitive tier (Class B/C/D/E) (Successor-Remediation, Op Stds §43)](operator_dashboard_sensitive_tier.md) | _–_ |
| 2026-07-09 | operations | active | _–_ | [Runbook — PO generation daemon (`po_poll`) (a queued PO stuck / fenced / dark) (Successor-Remediation, Op Stds §43)](po_poll.md) | _–_ |
| 2026-07-09 | operations | active | _–_ | [Runbook — PO send daemon (`po_send`) (a PO row stuck HELD / blocked / "contamination") (Successor-Remediation, Op Stds §43)](po_send.md) | _–_ |
| 2026-07-04 | operations | active | field_ops | [Runbook — Hours Log up-sync (P7, the `fieldops_sync` hours pass) (Successor-Remediation, Op Stds §43)](hours_log_sync.md) | _–_ |
| 2026-07-03 | operations | active | safety_reports | [Runbook — compile_now_poll (the on-demand "Compile Now" poller) (Successor-Remediation, Op Stds §43)](compile_now_poll.md) | _–_ |
| 2026-07-01 | operations | active | field_ops | [Runbook — Field-Ops daily report (SOP form) + assigned inspections (Successor-Remediation, Op Stds §43)](fieldops_checklists.md) | _–_ |
| 2026-07-01 | operations | active | field_ops | [Runbook — Manager tier + crew→job assignment (Successor-Remediation, Op Stds §43)](manager_tier.md) | _–_ |
| 2026-07-01 | operations | active | field_ops | [Runbook — Subcontractor tier (scoped crew-create + time scoping) · Op Stds §43](subcontractor_tier.md) | _–_ |
| 2026-06-30 | operations | active | field_ops | [Runbook — Field-ops mirror daemon (`fieldops_sync`) (Successor-Remediation, Op Stds §43)](fieldops_sync.md) | _–_ |
| 2026-06-30 | operations | active | progress_reports | [Runbook — Progress weekly-send (a WPR row stuck HELD / blocked / "contamination") (Successor-Remediation, Op Stds §43)](progress_send.md) | _–_ |
| 2026-06-30 | operations | active | progress_reports | [Runbook — progress_weekly_generate (the progress weekly compile) (Successor-Remediation, Op Stds §43)](progress_weekly_generate.md) | _–_ |
| 2026-06-29 | operations | active | safety_portal | [Runbook — Form workflow selector + the `recategorize` publish op (Successor-Remediation, Op Stds §43)](form_workflow_recategorize.md) | _–_ |
| 2026-06-29 | operations | active | progress_reports | [Runbook — Progress intake routing (the `progress_reports.intake_enabled` gate) (Successor-Remediation, Op Stds §43)](progress_intake_routing.md) | _–_ |
| 2026-06-29 | operations | active | progress_reports | [Runbook — Progress Reporting config sheets (WPR_human_review + ITS_Active_Jobs_Progress) (Successor-Remediation, Op Stds §43)](progress_reporting_config_sheets.md) | _–_ |
| 2026-06-29 | operations | active | safety_reports | [Runbook — Safety weekly-send Workstream guard (a row stuck HELD / "contamination") (Successor-Remediation, Op Stds §43)](safety_weekly_send.md) | _–_ |
| 2026-06-29 | operations | active | safety_reports | [Runbook — week_sheet config binding (submissions mis-file / week sheet names change) (Successor-Remediation, Op Stds §43)](week_sheet_config.md) | _–_ |
| 2026-06-28 | operations | active | infrastructure | [Runbook — Box OAuth token stale / refresh-lock contention (Successor-Remediation, Op Stds §43)](box_token_freshness.md) | _–_ |
| 2026-06-27 | operations | active | safety_portal | [Runbook — Field-Ops portal job create (portal-origin jobs "stuck pending") (Successor-Remediation, Op Stds §43)](fieldops_job_write.md) | _–_ |
| 2026-06-12 | operations | active | safety_reports | [Runbook — Safety photo path (photo rejected / clamd down / oversized packet HELD) (Successor-Remediation, Op Stds §43)](safety_photo_path.md) | _–_ |
| 2026-06-12 | operations | active | safety_reports | [Runbook — Safety Portal filed-PDF download "stuck preparing" (Successor-Remediation, Op Stds §43)](safety_portal_pdf_download.md) | _–_ |
| 2026-06-08 | operations | active | safety_portal | [Runbook — Safety Portal admin dashboard (account management + lockout recovery) (Successor-Remediation, Op Stds §43)](safety_portal_admin_dashboard.md) | _–_ |
| 2026-06-05 | operations | active | safety_portal | [Runbook — Safety Portal forms (add / retire / update) (Successor-Remediation, Op Stds §43)](safety_portal_forms.md) | _–_ |
| 2026-06-05 | operations | active | safety_portal | [Runbook — Safety Portal job management (add / retire jobs) (Successor-Remediation, Op Stds §43)](safety_portal_job_management.md) | _–_ |
| 2026-06-03 | operations | active | safety_portal | [Runbook — Safety Portal config sheets (ITS_Active_Jobs + ITS_Forms_Catalog) (Successor-Remediation, Op Stds §43)](safety_portal_config_sheets.md) | _–_ |
| 2026-06-02 | operations | active | safety_reports | [Runbook — ITS_Daemon_Health row self-provision (Successor-Remediation, Op Stds §43)](daemon_health_self_provision.md) | _–_ |
| 2026-06-02 | operations | active | infrastructure | [Runbook — Weekly picklist audit reports drift (Successor-Remediation, Op Stds §43)](picklist_drift_reconcile.md) | _–_ |
| 2026-06-02 | operations | active | safety_reports | [Runbook — Project not routed to a Box folder (Successor-Remediation, Op Stds §43)](project_routing_onboarding.md) | _–_ |
| 2026-06-02 | operations | active | infrastructure | [Runbook — Smartsheet token cannot write (Successor-Remediation, Op Stds §43)](token_write_capability.md) | _–_ |
| 2026-06-01 | operations | active | infrastructure | [Runbook — Smartsheet circuit breaker + alerts-per-hour cap (Successor-Remediation, Op Stds §43)](circuit_breaker.md) | _–_ |
| 2026-06-01 | operations | active | safety_reports | [Runbook — weekly_generate catch-up (Successor-Remediation, Op Stds §43)](safety_weekly_generate.md) | _–_ |
| _–_ | _–_ | active | safety_portal | [Crew edit/retire + time amend/void — §43 successor-remediation](fieldops_time_amend.md) | _–_ |
| _–_ | _–_ | active | safety_portal | [Portal D1 prune health — watchdog Check V (§43 successor-remediation)](portal_prune_health.md) | _–_ |
| _–_ | runbook | skeleton | field_ops | [Runbook — per-job Materials tracking](job_materials.md) | _–_ |
| _–_ | runbook | skeleton | field_ops | [Runbook — Materials Catalog admin (`material_catalog`)](material_catalog_admin.md) | _–_ |
<!-- END AUTO-INDEX -->
