# ITS — Integrated Technical System (Execution Layer)

[![ci](https://github.com/SolutionSmith-debug/its/actions/workflows/ci.yml/badge.svg)](https://github.com/SolutionSmith-debug/its/actions/workflows/ci.yml)

ITS is a Claude-powered "computer employee" for construction and renewables firms — it turns
field submissions, project data, and documents into filed records and human-approved reports
across Smartsheet, Box, and Outlook. It is a **white-glove custom-development practice**, not a
SaaS product: each customer gets a fully-customized build forked from the ITS blueprint and
maintained in its own private repo. **Evergreen Renewables is Customer 0** — first deployment
and design partner; this repo is Evergreen-specific.

This is the **execution layer**. The **planning layer** lives in a separate Claude.ai project
("ITS Foundation & Planning") and in the [`its-blueprint`](https://github.com/SolutionSmith-debug/its-blueprint)
repo (doctrine + mission files); this repo implements what is decided there. If a claim here
contradicts the blueprint doctrine (Operational Standards v21, Foundation Mission v11), **the
blueprint wins**.

> **New to the codebase?** [`CLAUDE.md`](CLAUDE.md) is the authoritative context (conventions +
> a live "what's stubbed vs. real" table) and [`docs/ROADMAP.md`](docs/ROADMAP.md) is the single
> marching order for what's next. This README is the human-facing surface intro.

## Architecture — two layers, deliberately separated

- **Planning & Foundation** (Claude.ai + `its-blueprint`) — mission files, architectural
  decisions, doctrine, prompt/schema designs. Not in this repo.
- **Execution** (this repo) — Claude Code and Python on a MacBook, triggered by **launchd
  polling daemons** (the canonical intake pattern, Op Stds v21 §31). It reads/writes the systems
  of record over their APIs and calls the Anthropic API for reasoning steps.

Systems of record, unchanged by ITS: **Smartsheet** (structured data), **Box** (documents),
**Outlook/Graph** (communication). The customer-facing **Safety Portal** is a **Cloudflare
Worker + D1 + React SPA** (`safety_portal/`) — a send-free capture surface; nothing is
transmitted from it. It is **local-first**: no cloud-server execution, Tailscale-only, no public
service exposed.

## Non-negotiable invariants (Foundation Mission v11)

- **External Send Gate (permanent).** No external transmission without explicit human approval.
  Two-process model: generation scripts have **zero** send capability; send scripts have **zero**
  AI step. Enforced at import by `tests/test_capability_gating.py`.
- **Adversarial Input Handling.** All content originating outside the operating tenant is
  untrusted data — sender allowlist + header-forgery detection, untrusted-content tagging on every
  AI call, capability gating, structured-output enforcement, a post-hoc anomaly tripwire, and
  attachment/photo screening (§34).
- **Never silent.** Failures are observable, recoverable, and surfaced — a triple-fire CRITICAL
  path (Smartsheet `ITS_Errors` + Resend email + Sentry), confidence scoring that routes
  ambiguity to a human review queue, and a fail-open kill switch (operator convenience, *not* the
  security boundary — the Send Gate is).
- **Credentials from the macOS Keychain**, never env files, never committed (`shared/keychain.py`).

Production-quality and defensively built for a 10–50-person firm; high availability is not
required, but nothing fails silently and a human is permanently in the loop on every external
send.

## Repository layout

| Path | What it is |
|------|------------|
| `shared/` | Cross-cutting helpers every workstream reuses — kill switch, error-log + triple-fire alerting, API clients (Anthropic, Box, Graph, Smartsheet, Sentry, Resend, portal), Keychain, untrusted-content tagging, anomaly logger, quarantine, review queue, alert dedupe, atomic state I/O, picklist sync/validation, scheduling, sheet IDs, heartbeat, capacity guards. Start here. |
| `safety_reports/` | The Safety Portal pull pipeline (Python): `portal_poll` (intake daemon) → `intake` (12-stage filing) → `weekly_generate`/`compile_now_poll` (deterministic weekly compile, generation half of the Send Gate) → `weekly_send`/`weekly_send_poll` (send half). Also `photo_screen` (§34) and the shared `generate_core` engine. |
| `progress_reports/` | The Progress Reporting workstream — the progress twin of the safety pipeline (`progress_weekly_generate`, `progress_send`/`_poll`, `wpr_review`) + the P7 per-job `hours_log` standing tracker, instantiating the parameterized shared machinery (not cloned). |
| `field_ops/` | The Field-Ops expansion — `fieldops_sync`, the D1→Smartsheet mirror daemon (job identity + the per-job standing trackers) that makes ITS-owned Smartsheet the downstream SoR (Op Stds v21 §51). |
| `po_materials/` | The Purchase-Order workstream — a deterministic (no-AI) direct-PO-to-vendor pipeline: `po_generate` (integer-cents render, Worker-parity assert), `po_poll` (pull daemon), `po_send`/`_poll` (F22 send half), plus the §50 **config actuator** (`config_actuator`/`config_apply`) that commits + deploys approved workstream-config edits. Ships dark. |
| `subcontracts/` | Deterministic subcontract-package generation (ADR-0003, no AI) — `subcontract_generate` (SOV guard → §50 legal gate → token fill), `subcontract_docx` (editable `.docx`/`.xlsx`, not PDF), `subcontract_poll` (pull daemon), a WSR review twin. Generation built + dark; the send half (SC-S4) is a tracked build. |
| `operator_dashboard/` | The WS2 operator dashboard — a localhost-only FastAPI app (`python -m operator_dashboard` @127.0.0.1:8484, Tailscale-exposed): loginless read-only observability panels + a PIN-gated ACT surface (ITS_Config editor, secret rotation). Writes only ITS_Config; never deploys or sends. Ships dark. |
| `docs_pdf/` | The branded enablement-PDF generator (markdown → reportlab): `manifest`/`md_render`/`brand`, rendered by `scripts/build_docs_pdfs.py`; `--check` is the CI docs-currency gate. Not a daemon. |
| `safety_portal/` | The Cloudflare **Worker** (`worker/`, send-free D1 API + capability layer), the React **SPA** (`src/`), D1 **migrations/**, and form definitions. `README.md` there carries the migration punch-list + per-slice activation notes. |
| `scripts/` | Scheduled entry points + launchd plists — `watchdog.py` (daily; the dead-man's-switch checks), `run_picklist_sync.py` (hourly), `install.sh`, and `migrations/` (operator-run Smartsheet/D1 build scripts). |
| `schemas/` · `prompts/` | Version-controlled JSON schemas (Anthropic tool-use) and prompt files. |
| `tests/` | pytest suite (unit + capability-gating + doctrine-drift gates). Integration tests are operator-run only (`-m integration`, live sandbox creds). |
| `docs/` | Everything that isn't code — see [Documentation](#documentation). |

## The daemons (launchd, local-first)

Interval pollers (`org.solutionsmith.its.*`), each one-shot-per-`StartInterval`, kill-switch-gated,
error-log-wrapped, and heartbeating to the `ITS_Daemon_Health` sheet:

`portal-poll` (Safety Portal intake) · `weekly-generate` / `weekly-send` · `compile-now-poll`
(on-demand compile, both safety + progress) · `progress-generate` / `progress-send` ·
`fieldops-sync` (D1→Smartsheet mirror) · `po-poll` / `po-send` (Purchase Orders) ·
`subcontract-poll` (subcontracts) · `config-actuator` (§50 config code-actuator) ·
`publish-daemon` (form-editor code actuator, §50) · `picklist-sync` / `picklist-audit` ·
`watchdog` (staleness floor + catch-up + the external Healthchecks.io dead-man's
switch — which only fires once `system.heartbeat_url` is configured; see
`docs/references/integration_reference.md`).

**15** daemon plists ship; **14 load at cutover** — `po-send` (a send daemon) stays
launchd-**unloaded** (send-gate defense-in-depth, VC-02-enforced), while the dark generation
daemons load but sit runtime-gated off.

## Current state

The Safety Portal safety-report pipeline is built and live-validated end-to-end on the mirror
tenant; the Progress Reporting workstream is going live; and the Field-Ops portal expansion
(in-portal jobs, personnel, equipment, materials, time, tasks, and a rolling SOP daily form) is
largely built. The **Purchase-Order** and **subcontract-generation** workstreams (both
deterministic, no-AI) are built and ship dark, and the **operator dashboard** (WS2) is built
(dark pending its PIN). The authoritative, always-current picture is **[`CLAUDE.md`](CLAUDE.md)'s
"What's stubbed vs. real" table**; what's next is **[`docs/ROADMAP.md`](docs/ROADMAP.md)**.

Built in a **sandbox tenant** (`evergreenmirror.com` + matching Smartsheet/Box) before cutover to
the live Evergreen tenant at the Phase 1.4 → 1.5 gate.

## Setup

**Python** (3.12+):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # sanity check
```

Re-run `pip install -e ".[dev]"` after pulling a PR that changes `pyproject.toml` (a
`ModuleNotFoundError` after `git pull` usually means a new dependency).

> **Editing Python source?** The launchd daemons run this working tree from disk every ~60s, so
> uncommitted edits go live immediately. Do Python-source work in a per-task `git worktree` off
> `origin/main` **with its own fresh venv** — see [`docs/operations/worktree_discipline.md`](docs/operations/worktree_discipline.md).
> Docs-only edits on the live tree are fine.

**Safety Portal** (Cloudflare Worker + SPA):

```bash
cd safety_portal
npm ci
npm test           # worker vitest
git -C ~/its pull origin main   # ALWAYS first before a live deploy (stale-migrations lockout class)
npx wrangler d1 migrations apply its-safety-portal-db --remote
npm run deploy
```

## Operating conventions

Normative summary; the canonical sources are `CLAUDE.md` (execution conventions +
`docs/HOUSE_REFLEXES.md`, the working standards) and the blueprint doctrine.

- **Kill switch first** — every script entry calls `shared.kill_switch.check_system_state()` (or
  `@require_active`). PAUSED/MAINTENANCE → exit cleanly.
- **Error-log decorator** — every `main` is wrapped in `@its_error_log`.
- **Two separate files** per workstream — a generation script and a send script (Send Gate).
- **Schemas in `schemas/`, prompts in `prompts/`**, both version-controlled with a `version` field.
- **Adversarial review is definition-of-done** on any trust-boundary surface (untrusted parse, a
  D1/Smartsheet write-route, an external-send path) — via `/security-review` or the repo's
  `ops-stds-enforcer` / `portal-worker-security-reviewer` agents.
- **Four-part PR-landing verify** — `state=MERGED` · `mergedAt` · `mergeCommit` · main-branch CI
  on the merge commit = SUCCESS (`docs/operations/pr_merge_discipline.md`).

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — the authoritative execution context (loaded by Claude Code each session).
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — the single marching order · [`docs/HOUSE_REFLEXES.md`](docs/HOUSE_REFLEXES.md) — working standards.
- [`docs/adr/`](docs/adr/) + [`CONTEXT.md`](CONTEXT.md) — domain model & architecture decisions.
- [`docs/runbooks/`](docs/runbooks/) — §43 successor-remediation runbooks · [`docs/operations/`](docs/operations/) — PR/merge/worktree/doc procedures.
- [`docs/session_logs/`](docs/session_logs/) · [`docs/audits/`](docs/audits/) · [`docs/reports/`](docs/reports/) · [`docs/tech_debt.md`](docs/tech_debt.md).
- Doc conventions (frontmatter/sections/filenames): [`docs/operations/doc_conventions.md`](docs/operations/doc_conventions.md). CI lints new docs + checks the auto-generated indexes.
- **Doctrine** (canonical, planning-layer): `../its-blueprint/doctrine/` — Operational Standards v21, Foundation Mission v11.
