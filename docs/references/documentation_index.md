---
type: reference
date: 2026-07-15
status: active
workstream: null
tags: [documentation-corpus, tier-1, index]
---

# ITS Documentation Index

## Purpose

The master map of the ITS documentation corpus: every delivery-critical guide and reference,
what it covers, who it is for, and which tier it belongs to. This file renders as `INDEX.pdf`
(the front of the distributable manual set) and seeds the `ITS_Documentation_Index` Smartsheet
rows. When you do not know which document answers your question, start here.

<!-- src: docs/enablement/manifest.yaml (the build set + per-doc sha256); scripts/build_docs_pdfs.py | verified 2026-07-19 -->
The authoritative registration of what gets rendered is `docs/enablement/manifest.yaml` — one
entry per doc, each carrying a `sha256` of its source that is the **doc-currency baseline**.
`python -m scripts.build_docs_pdfs --check` re-hashes every source and flags drift; that check
is the CI teeth behind "the PDF matches the source."

## The three tiers

<!-- src: docs/enablement/README.md; docs/runbooks/README.md; docs/references/ | verified 2026-07-19 -->

| Tier | What it is | Audience |
|------|-----------|----------|
| **Tier 1 — System references** | Evergreen explanatory references for the whole machine (this `docs/references/` set). | The operator (Developer- and Successor-Operator). |
| **Tier 2 — Enablement guides** | Plain-language "how to use this capability" manuals (`docs/enablement/`). | The people who *use* a capability — office PM, field crews, stakeholders. |
| **Tier 3 — Successor-remediation runbooks** | §43 "what to do when it misbehaves" repair procedures (`docs/runbooks/`). | The Successor-Operator carrying out a documented, low-capability-class repair. |

Related audiences overlap: the operator reads all three; an end-user rarely leaves Tier 2.

## Tier 1 — System references

<!-- src: docs/references/*.md (this session's Tier-1 corpus) | verified 2026-07-19 -->

| Doc | Audience | Scope / one-line purpose |
|-----|----------|--------------------------|
| [system_architecture.md](system_architecture.md) | operator | The whole machine — portal plane, Mac daemon plane, the two-process Send Gate, the data doctrine, Tailscale, trust boundaries. Start here. |
| [daemon_reference.md](daemon_reference.md) | operator | Every background daemon: purpose, interval, source-of-work, config gate, heartbeat, log path, failure modes, restart. |
| [data_model_reference.md](data_model_reference.md) | operator | Every Smartsheet sheet, every D1 table, the Box folder topology, and the platform caps (cells/columns/rate). |
| [integration_reference.md](integration_reference.md) | operator | Each external system (Graph, Box, Smartsheet, Resend, Sentry, Tailscale, Healthchecks.io, Cloudflare): auth, config keys, constraints, failure signatures. |
| [security_trust_model.md](security_trust_model.md) | operator | The Send Gate, the six-layer adversarial-input defense, capability gating, the secrets model, and dashboard auth tiers — operator-facing. |
| [escalation_matrix.md](escalation_matrix.md) | operator / successor | The three-tier maintenance model, the both-rule, the four fixed escalate-to-Seth categories, and a symptom→class quick table. |
| [glossary.md](glossary.md) | all | The controlled vocabulary — HELD states, F22, §-numbers, gate/breaker/heartbeat terms, workstream tags. |
| [documentation_index.md](documentation_index.md) | all | This file — the corpus map. |

## Tier 2 — Enablement guides

<!-- src: docs/enablement/manifest.yaml (registered build set) | verified 2026-07-19 -->

The registered enablement set (see `docs/enablement/manifest.yaml` for the authoritative
titles + sha256s). Audience is drawn from each guide's intended reader.

| Guide | Audience | One-line purpose |
|-------|----------|------------------|
| [its_owners_manual.md](../enablement/its_owners_manual.md) | operator / owner | The top-level owner's manual for ITS. |
| [operator_dashboard.md](../enablement/operator_dashboard.md) | operator | Using the localhost operator dashboard (obs panels + PIN-gated actions). |
| [safety_reports_guide.md](../enablement/safety_reports_guide.md) | office | Safety reports: submit → review → weekly packet. |
| [purchase_orders.md](../enablement/purchase_orders.md) | office | Creating and sending purchase orders in the portal. |
| [subcontracts.md](../enablement/subcontracts.md) | office | Generating a subcontract package. |
| [portal_job_creation.md](../enablement/portal_job_creation.md) | office PM | Creating jobs in the ITS portal. |
| [portal_admin_dashboard.md](../enablement/portal_admin_dashboard.md) | office admin | The portal admin dashboard (account provisioning). |
| [fieldops_checklists.md](../enablement/fieldops_checklists.md) | field crew | The daily-report SOP form + assigned inspections. |
| [manager_tier.md](../enablement/manager_tier.md) | crew lead | The manager role (crew leads). |
| [subcontractor_tier.md](../enablement/subcontractor_tier.md) | subcontractor | The subcontractor tier. |
| [crew_time_corrections.md](../enablement/crew_time_corrections.md) | office | Crew corrections + time amendments. |
| [progress_rollup_numbers.md](../enablement/progress_rollup_numbers.md) | office / stakeholder | Reading the weekly progress rollup numbers. |
| [its_config_dictionary.md](its_config_dictionary.md) | operator | The generated ITS_Config data dictionary (every config key). |

## Tier 3 — Successor-remediation runbooks

<!-- src: docs/runbooks/*.md — 47 files = 46 §43 runbooks + README.md | verified 2026-07-23 (post-#679 tenant_standup.md) -->

The §43 successor-remediation runbooks live in [`docs/runbooks/`](../runbooks/README.md) — **46**
of them today, plus the README index — one per capability with a Tier-2-reachable failure mode,
each written as *symptom → low-class repair steps → explicit escalate-to-Seth boundary*. They
are the resolution targets the
[escalation_matrix.md](escalation_matrix.md) and (in a later tranche) the interactive
troubleshooting tree link into. See the runbooks README for the full, auto-indexed list.

## How the corpus is built and kept current

<!-- src: scripts/build_docs_pdfs.py (render / --check / --upload) | verified 2026-07-19 -->

- **Render:** `python -m scripts.build_docs_pdfs --all` renders every manifest doc to a branded
  PDF under `docs/_build_pdf/` (the repo never commits the rendered binaries).
- **Currency (`--check`):** re-hashes each source against its manifest `sha256`; drift fails the
  check. After editing a guide, re-record its sha256 in the manifest.
- **Distribution (`--upload`):** the dark-gated Box publish leg. It uploads the rendered corpus to
  the Box folder named by `docs_pdf.upload.box_folder_id`, but only when `docs_pdf.upload.enabled`
  is true in ITS_Config — an operator-activated, deliberate flip.
- **Index sheet:** `ITS_Documentation_Index` (Smartsheet) mirrors the manifest for at-a-glance
  lookup with a Box link per doc.

## Related docs

- [glossary.md](glossary.md) — the vocabulary used across the corpus
- [system_architecture.md](system_architecture.md) — the system the corpus documents
- `docs/enablement/README.md` — the Tier-2 index
- `docs/runbooks/README.md` — the Tier-3 index
- `docs/operations/doc_conventions.md` — the frontmatter + section conventions every doc follows
