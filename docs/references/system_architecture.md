---
type: reference
date: 2026-07-14
status: active
workstream: null
tags: [documentation-corpus, tier-1]
---

# ITS System Architecture

## Purpose

<!-- src: CLAUDE.md:"## Architectural model" | verified 2026-07-14 -->
This is the operator's map of the whole machine. ITS (Integrated Technical
System) is a Claude-powered "computer employee" for a construction firm
(Evergreen Renewables, Customer 0). It runs across two physical planes: a
**Cloudflare edge plane** (the field-facing Safety Portal) and a **Mac daemon
plane** (a MacBook running scheduled Python jobs). Between them sit the
customer's systems of record — Smartsheet, Box, and Microsoft 365 (Outlook /
Graph) — plus the Anthropic API for the one reasoning step in the system.

<!-- src: CLAUDE.md:"## Architectural model" | verified 2026-07-14 -->
This document explains how those planes fit together, who is allowed to do what
from where, and the two safety invariants that shape every data path. It is a
reference — read it once to understand the shape of the system, then use the
sibling Tier-1 docs (see *Related docs*) for the detail of any one part.

## Background — two deliberately separated layers

<!-- src: CLAUDE.md:"## Architectural model" | verified 2026-07-14 -->
ITS is designed as two layers that never merge. The **Planning & Foundation
layer** (a separate Claude.ai project, not in this repo) holds mission files,
architectural decisions, schemas, and prompt designs — the canonical doctrine
(Foundation Mission v11, Operational Standards v21). The **Execution layer**
(this repository) implements what the planning layer decides: Python scripts on
a MacBook triggered by launchd, plus the Cloudflare Worker + SPA that make up
the portal. This document describes only the execution layer.

<!-- src: CLAUDE.md:"## Product context" | verified 2026-07-14 -->
The build is local-first on a MacBook through Phase 4 — there is deliberately no
cloud-server execution tier for the Python side, no multi-tenant SaaS, and
nothing exposed to the public internet except the Cloudflare edge. Customer
systems of record (Smartsheet, Box, Outlook) are unchanged by ITS; ITS reads
and writes them through APIs but does not replace them. ITS *does* own its own
operational Smartsheet sheets and the portal's D1 database (Op Stds §51).

## The whole machine at a glance

<!-- src: safety_portal/worker/index.ts:54-86 | verified 2026-07-14 -->
The portal plane is **send-free**: the Cloudflare Worker validates and signs
field submissions and queues them in D1, but it never transmits anything
outward. A Mac-side daemon (`portal_poll`) reaches out over HTTPS on a timer,
*pulls* queued submissions, verifies their signatures, and files them. All
external customer email leaves only from the Mac plane, only after a human
approves it. This is the pull model — the edge holds untrusted input at
arm's length; the trusted Mac plane decides what to do with it.

```
            FIELD DEVICES (phones / tablets, public internet)
                              │  HTTPS
                              ▼
  ┌──────────────────────────────────────────────────────────┐
  │  CLOUDFLARE EDGE PLANE  —  "its-safety-portal"            │
  │  (safety.evergreenmirror.com)          *** SEND-FREE ***  │
  │                                                            │
  │   React SPA  ──►  Worker (Hono)  ──►  D1 database         │
  │   (browser)       38 .ts modules      53 migrations       │
  │                   signs + HMAC         (queue + field-ops  │
  │                   validates, bounds     capture SoR)       │
  │                   never sends                              │
  └──────────────────────────────────────────────────────────┘
              ▲                          │
              │ HTTPS pull (bearer)      │  GET /api/internal/pending
              │ + mark-filed receipt     │  POST /api/internal/mark-filed
              │ + active-jobs sync       │  POST /api/internal/sync
              │                          ▼
  ┌──────────────────────────────────────────────────────────┐
  │  MAC DAEMON PLANE  —  the MacBook (~/its working tree)    │
  │                                                            │
  │   launchd fires a FRESH PROCESS per cycle. Each script:   │
  │     1. kill_switch.check_system_state()  (ACTIVE?)        │
  │     2. @its_error_log  (wraps main; CRITICAL → alerts)    │
  │                                                            │
  │   ┌── generation daemons ──┐   ┌── send daemons ──┐        │
  │   │ ZERO send capability   │   │ ZERO AI step     │        │
  │   │ (portal_poll, *_generate│   │ (weekly_send,    │       │
  │   │  po_poll, fieldops_sync)│   │  po_send, …)     │       │
  │   └────────────────────────┘   └──────────────────┘       │
  │            External Send Gate = two separate processes     │
  └──────────────────────────────────────────────────────────┘
       │            │            │            │            │
       ▼            ▼            ▼            ▼            ▼
  Smartsheet      Box       MS Graph     Anthropic    Resend /
  (current-     (document   (Outlook      (one LLM    Sentry
   state views,  storage)    send +        call:       (operator
   review                    intake)      intake.py)   alerts)
   surfaces)
```

<!-- src: scripts/launchd/*.plist enumeration | verified 2026-07-14 -->
Everything below expands one box of that picture. The two planes never share a
process, a credential store, or a trust level — the only thing crossing between
them is an HMAC-signed HTTPS pull initiated by the Mac.

## The portal plane (Cloudflare edge)

<!-- src: ls safety_portal/worker/*.ts → 38 files | verified 2026-07-14 -->
The portal is a Cloudflare Worker written with the Hono framework. Its code is
**38 TypeScript modules** under `safety_portal/worker/`. `index.ts` is the
router that mounts every route family; the rest are focused modules — for
example `submission.ts` (safety submissions), `po.ts` (purchase orders),
`subcontract.ts` (subcontracts), `hmac.ts` (signing/verification),
`auth.ts` / `audit.ts` (session + audit log), `photo_bounds.ts` (image size
gates), `prune.ts` (data retention), and more than twenty `fieldops_*.ts`
modules for field-ops capture (jobs, tasks, crew, time, materials, equipment,
checklists, photos). It is not a Python package.

<!-- src: ls safety_portal/migrations/*.sql → 53 files | verified 2026-07-14 -->
The Worker's data lives in a Cloudflare **D1** database, evolved through **53
ordered SQL migrations** in `safety_portal/migrations/` (0001 through 0053).
D1 is the system of record for portal submissions and all field-ops capture:
users and roles (0001, 0007, 0013, 0023), the portal queue and submission
transport (0003, 0005, 0008), publish requests and PDF-download caches (0010,
0011, 0012), field-ops core tables (0014–0041 — jobs, personnel, equipment,
materials, checklists, time entries, photo pools), purchase-order tables
(0042–0044, 0053), config-edit requests (0045–0048), and subcontract tables
(0049–0052).

<!-- src: ls safety_portal/src → App.tsx, components, forms, lib, pages, … (122 .tsx/.ts) | verified 2026-07-14 -->
The field-facing UI is a React single-page app in `safety_portal/src`
(**122 `.ts`/`.tsx` source files**), organised into `pages/`, `forms/`,
`components/`, `lib/`, and `shared/`. It talks to the Worker only over
same-origin `fetch` (login, jobs, submit, and the field-ops routes). The SPA
holds no secrets and initiates no external send — it collects field input and
posts it to the Worker.

<!-- src: safety_portal/worker/index.ts:61-86 | verified 2026-07-14 -->
The single most important property of this whole plane is **Invariant 1 at the
edge: the Worker performs zero external transmission** — no email, no
third-party outbound, no AI step. Its only outbound `fetch` is to Cloudflare's
own asset server to serve the SPA. When a field user submits, the Worker
type-checks and length-bounds the body, uses bound SQL parameters (never string
interpolation), signs the submission with an HMAC, and *queues it in D1*. It
then waits to be pulled.

### Portal-plane internals

```
  BROWSER (untrusted)
     │  same-origin fetch: /api/login, /api/jobs, /api/submit, /api/fieldops_*
     ▼
  ┌───────────────────────────────────────────────────────────┐
  │  WORKER (Hono, index.ts + 37 route modules)                │
  │                                                            │
  │   auth.ts     → HMAC-signed session cookie (HttpOnly,       │
  │                 constant-time verify; 90d field, 30m admin) │
  │   submission  → type-check + length-bound + bound SQL       │
  │   hmac.ts     → sign each queued row (domain-separated)     │
  │   photo_bounds→ reject oversized/!magic images at the edge  │
  │   audit.ts    → mutation + audit row in ONE db.batch()      │
  │                                                            │
  │            writes ▼            NEVER sends outward           │
  │        ┌───────────────────────────────────────┐            │
  │        │  D1  (53 migrations)                   │            │
  │        │  queue rows + field-ops capture SoR    │            │
  │        └───────────────────────────────────────┘            │
  │            served over ▼ (bearer-token, internal only)      │
  │   GET /api/internal/pending   → Mac pulls the queue         │
  │   POST /api/internal/mark-filed → Mac's receipt             │
  │   POST /api/internal/sync     → Mac pushes Active-Jobs list │
  └───────────────────────────────────────────────────────────┘
```

## The Mac daemon plane

<!-- src: scripts/launchd/*.plist (17 files, 1 is template.plist) | verified 2026-07-14 -->
The Mac plane is a set of scheduled Python jobs run by macOS **launchd**. There
are **16 live daemon definitions** in `scripts/launchd/` (17 `.plist` files, one
of which — `template.plist` — is only a template). launchd does not keep a
long-running Python process alive; instead it **launches a fresh process for
each cycle** from the `~/its` working tree on disk. A daemon that is "running
every 60 seconds" is really launchd starting `python -m …` anew every 60
seconds, letting it do one unit of work, and letting it exit.

<!-- src: scripts/launchd/*.plist RunAtLoad/KeepAlive comments | verified 2026-07-14 -->
There are three schedule shapes. **Polling daemons** use `StartInterval` (a
fixed number of seconds between fires) and `RunAtLoad=true` so they resume
immediately after a reboot. **Calendar daemons** use `StartCalendarInterval` (a
wall-clock time) with `RunAtLoad=false` — launchd fires a missed calendar job on
wake, so `RunAtLoad=true` would mis-fire it on the wrong day. The one exception
to the one-shot rule is the **dashboard**, a long-lived server with
`KeepAlive=true`. `KeepAlive` is otherwise never used — a one-shot daemon with
`KeepAlive=true` would be restarted the instant it exited normally.

### Live daemons (from the plists this session)

<!-- src: scripts/launchd/install.sh:60-90 (interval defaults) + plist StartCalendarInterval blocks | verified 2026-07-14 -->

| Daemon (label suffix) | Schedule | Cadence / time | Role |
|---|---|---|---|
| `portal-poll` | interval | 60s | pull portal queue → file via intake |
| `fieldops-sync` | interval | 90s | D1 job data → Smartsheet Active-Jobs |
| `po-poll` | interval | 90s | purchase-order draft drain + file |
| `compile-now-poll` | interval | 90s | on-demand safety compile trigger |
| `subcontract-poll` | interval | 120s | subcontract draft drain + file |
| `config-actuator` | interval | 120s | privileged §50 config-edit actuator |
| `publish-daemon` | interval | 120s | privileged form-publish actuator |
| `weekly-send` | interval | 900s | dispatch approved safety report emails |
| `progress-send` | interval | 900s | dispatch approved progress report emails |
| `po-send` | interval | 900s | dispatch approved purchase-order emails |
| `picklist-sync` | interval | 3600s (hourly) | sync cross-sheet picklist options |
| `weekly-generate` | calendar | Fri 14:00 | deterministic safety weekly compile |
| `progress-generate` | calendar | Fri 14:30 | deterministic progress weekly compile |
| `watchdog` | interval | 3600s hourly (+ ~24h daily tier) | health checks + missed-run detection |
| `picklist-audit` | calendar | Sun 15:00 | picklist drift audit |
| `dashboard` | server | always up (KeepAlive) | operator observability + ACT surface |

<!-- src: safety_reports/portal_poll.py:101 DEFAULT_POLL_INTERVAL=60; install.sh:80-92 | verified 2026-07-14 -->
Interval defaults live in `scripts/launchd/install.sh` and are overridable per
daemon from an `ITS_Config` row (for example
`safety_reports.portal_poll.poll_interval_seconds`, default 60). The installer
substitutes the resolved value into the plist's `StartInterval` placeholder at
install time. `picklist-sync` (3600s), `config-actuator`, and `publish-daemon`
(both 120s) carry their interval hardcoded in the plist.

### Every cycle starts with two reflexes

<!-- src: shared/kill_switch.py:1-17,39-78 | verified 2026-07-14 -->
The **kill switch** is the first thing every script calls. `check_system_state()`
reads a single `ITS_Config` row keyed `Setting=system.state`,
`Workstream=global`, whose value is `ACTIVE`, `PAUSED`, or `MAINTENANCE`. On
`ACTIVE` scripts run; on `PAUSED` scheduled scripts skip silently (the watchdog
still alerts on missed runs); on `MAINTENANCE` they skip and the watchdog stays
quiet. This lets anyone with edit access to the sheet halt ITS without touching
code — useful before audits or holidays.

<!-- src: shared/kill_switch.py:13-16,49-78 | verified 2026-07-14 -->
The kill switch is deliberately **fail-open**. On any of three failure modes —
Smartsheet unreachable, the row missing, or the value not in the enum — it
returns `ACTIVE` and emits a distinguishable `WARN` (Op Stds §1: a config-read
failure must never silently halt the system). Because it fails open, the kill
switch is an operator convenience, *not* a security control. The real security
boundary is the External Send Gate below.

<!-- src: CLAUDE.md:"## Operational conventions" (error-log decorator) | verified 2026-07-14 -->
The second reflex is the **error-log decorator**: every daemon's main function is
wrapped in `@its_error_log(script_name=...)`. It catches any unhandled
exception, writes an `ITS_Errors` sheet row, and for CRITICAL severity fires a
triple alert — Resend email, Sentry capture, and the sheet record — so a failure
is observable rather than silent.

### Daemon-plane internals

```
  launchd  ──(fires fresh process on its schedule)──►  python -m <module>
                                                          │
                                                          ▼
                                   1. kill_switch.check_system_state()
                                        PAUSED/MAINT → exit clean
                                                          │ ACTIVE
                                                          ▼
                                   2. @its_error_log wraps main()
                                        unhandled exc → ITS_Errors row
                                        CRITICAL → Resend + Sentry + sheet
                                                          │
                    ┌─────────────────────────────────────┴───────────────┐
                    ▼                                                       ▼
        GENERATION daemons                                        SEND daemons
        (no send capability)                                      (no AI step)
        portal_poll, weekly_generate,                            weekly_send,
        po_poll, po_generate, fieldops_sync,                     po_send,
        config_actuator, publish_daemon,                         progress_send,
        subcontract_poll, photo_screen …                         send_poll_core …
                    │                                                       │
                    ▼ read/write                                            ▼ send
        Smartsheet · Box · D1 (via portal_client)          MS Graph send_mail
        Anthropic (intake.py ONLY)                          (human-approved rows)
```

## The External Send Gate (Foundation Mission Invariant 1)

<!-- src: tests/test_capability_gating.py:1-19; CLAUDE.md:"### Invariant 1" | verified 2026-07-14 -->
No external transmission happens without explicit human approval, and this is
enforced structurally, not by policy. ITS uses a **two-process model**:
generation scripts (which may call the Anthropic API or compile customer-facing
content) have **zero send capability**, and send scripts (which transmit email)
have **zero AI step**. A successful prompt injection at the AI layer therefore
cannot cause an external send, because the transmitter lives in a different
process that never touches an LLM.

<!-- src: tests/test_capability_gating.py:44-220 (GATED_SCRIPTS) | verified 2026-07-14 -->
The gate is enforced at import time by `tests/test_capability_gating.py`, which
statically inspects each script's imports. `GATED_SCRIPTS` lists **17 generation
scripts** — each forbidden from importing send substrings (`send_mail`,
`resend`, `smtplib`, `email.mime`) and, for the deterministic ones, `anthropic`
too. Enrolled generation scripts include `safety_reports/intake.py`,
`portal_poll.py`, `weekly_generate.py`, `compile_core.py`, `generate_core.py`,
`publish_daemon.py`, `compile_now_poll.py`, `photo_screen.py`,
`progress_weekly_generate.py`, `field_ops/fieldops_sync.py`, `po_poll.py`,
`po_attach_screen.py`, `po_generate.py`, `config_actuator.py`,
`subcontract_generate.py`, `subcontract_docx.py`, and `subcontract_poll.py`.

<!-- src: tests/test_capability_gating.py:222-276 (SEND_SCRIPTS) | verified 2026-07-14 -->
`SEND_SCRIPTS` lists **7 send scripts** — each forbidden from importing
`anthropic` or `anthropic_client`: `weekly_send.py`, `weekly_send_poll.py`,
`send_poll_core.py` (the shared dispatch core), `progress_send.py`,
`progress_send_poll.py`, `po_send.py`, and `po_send_poll.py`. The subcontract
send half is not yet built (a commented stub). Adding a new workstream means
adding its generation and send scripts to these two lists in the same PR — that
is the whole enforcement mechanism.

<!-- src: tests/test_capability_gating.py:334-536 (F02 allowlist + WALKED_ROOTS) | verified 2026-07-14 -->
A second, orthogonal layer (audit finding **F02**) inverts the question: it
asserts that **no** module on the untrusted-content surface may import a
network-egress or process-spawn library (`requests`, `httpx`, `socket`,
`subprocess`, `boxsdk`, `anthropic`, `smartsheet`, `msal`, `sentry_sdk`,
`resend`, `pyclamd`, `importlib`, …) unless it is on an explicit allowlist with
a one-line rationale. The walked roots are `shared/`, `safety_reports/`,
`progress_reports/`, `field_ops/`, `po_materials/`, `operator_dashboard/`, and
`subcontracts/`. A future script that quietly `import requests` to exfiltrate
data fails CI before it can ship.

<!-- src: tests/test_capability_gating.py:614-687 (enrollment meta-test) | verified 2026-07-14 -->
A third meta-test closes the "forgot to enrol the new daemon" gap: any module
named `*_generate.py`, `*_send.py`, or `*_poll.py` on the workstream surface must
appear in `GATED_SCRIPTS` or `SEND_SCRIPTS`, or be explicitly exempted with a
reason — otherwise CI fails.

## The data doctrine, stated operationally

Each external system has one job. ITS never blurs them.

<!-- src: shared/smartsheet_client.py:1-33; CLAUDE.md:"## Architectural model" | verified 2026-07-14 -->
**Smartsheet — bounded current-state views + human-review surfaces.** The
`shared/smartsheet_client.py` wrapper works in column-title terms (not column
IDs), carries a typed exception hierarchy (`SmartsheetError` and subclasses for
401/403/404/429), and lazily builds a Keychain-backed client
(`ITS_SMARTSHEET_TOKEN`). Smartsheet holds operational sheets ITS owns —
`ITS_Config`, `ITS_Errors`, `ITS_Review_Queue`, `ITS_Daemon_Health`, the
`*_Pending_Review` / `*_human_review` approval sheets, and the current-state
`ITS_Active_Jobs` mirrors — not a full event log. It is the surface a human
operator reads and approves from.

<!-- src: safety_portal/worker/index.ts:61-86; safety_portal/migrations/ (53 files) | verified 2026-07-14 -->
**D1 — system of record for portal submissions + field-ops capture.** The
Cloudflare D1 database is where the field actually enters data: submissions,
jobs, tasks, crew, time, materials, equipment, checklists, and photo pools all
originate here (53 migrations). D1 is the SoR for what happened in the field;
the Mac plane pulls from it and mirrors selected current-state up into
Smartsheet for the operator.

<!-- src: shared/box_client.py:1-51 | verified 2026-07-14 -->
**Box — document storage.** The `shared/box_client.py` wrapper uses Box OAuth 2.0
User Authentication (not JWT/server-auth, which needs a paid add-on Evergreen's
tier lacks). It stores rendered PDFs and other documents in per-job folders.
A critical operational invariant: Box **rotates the refresh token on every
exchange**, so `_store_tokens` must persist the new `ITS_BOX_REFRESH_TOKEN` to
Keychain synchronously or ITS loses Box access within ~60 days; a dedicated test
locks this behavior.

<!-- src: shared/graph_client.py:1-56 | verified 2026-07-14 -->
**Microsoft Graph (Outlook) — external send + email intake.** The
`shared/graph_client.py` wrapper authenticates with MSAL client-credentials
against an Entra ID app (`ITS_MS_TENANT_ID` / `ITS_MS_CLIENT_ID` /
`ITS_MS_CLIENT_SECRET`). It exposes `send_mail` and `send_mail_large_attachment`
— the only external-send capabilities in the system — plus inbox reads for the
(now dormant) email-intake path. The module makes sending *possible*; the
capability gate ensures only send scripts can import it.

<!-- src: shared/anthropic_client.py:22-23,30-35; safety_reports/intake.py:739 | verified 2026-07-14 -->
**Anthropic — the one reasoning step.** The `shared/anthropic_client.py` wrapper
lazily loads `ITS_ANTHROPIC_KEY` from Keychain and defaults to model
`claude-sonnet-4-6`. There is exactly **one live inference call in the entire
system** — `safety_reports/intake.py:739` — which extracts structured data from
an inbound safety report. Every other pipeline (the weekly compiles, PO and
subcontract generation) is deterministic and AST-forbidden from importing
`anthropic` at all.

<!-- src: CLAUDE.md:"## Operational conventions" (credentials); shared/*_client.py | verified 2026-07-14 -->
All credentials come from the macOS Keychain via `shared.keychain.get_secret`
(secret *names* like `ITS_RESEND_API_KEY`, never values) — never env files,
never committed. Rotation is a deliberate ceremony, not a value edit.

## Remote operations over Tailscale (not the public internet)

<!-- src: operator_dashboard/config.py:20-21; operator_dashboard/__main__.py:1-7 | verified 2026-07-14 -->
The operator's control surface is the dashboard, a localhost-only FastAPI app
started with `python -m operator_dashboard`. It binds `127.0.0.1:8484` — the
loopback interface only — and is meant to be reached remotely by *exposing that
localhost port over Tailscale* (`tailscale serve 8484`), never by binding a
public interface. This is the general pattern for all Mac-plane services: bind
loopback, reach it over the private Tailscale network.

<!-- src: operator_dashboard/__main__.py:3-6; operator_dashboard/config.py | verified 2026-07-14 -->
The dashboard's read routes (launchd status, watchdog markers, heartbeats,
breaker state, log tails, error and review queues) are loginless, but every
mutating **ACT** route is **PIN-gated** with a constant-time compare and is
fail-closed until `ITS_OPERATOR_PIN` is provisioned in Keychain. The dashboard
writes only `ITS_Config` and stamps/prunes `ITS_Errors` rows; it never deploys
or sends externally.

## Trust boundaries — who can do what, from where

<!-- src: safety_portal/worker/index.ts:61-86; CLAUDE.md:"### Invariant 2" | verified 2026-07-14 -->

| Actor | From where | Can do | Cannot do |
|---|---|---|---|
| Field user | Public internet → SPA | Log in; submit safety/field-ops data to the Worker, which queues it in D1 | Cause any external send; reach the Mac plane; read another user's queue |
| Cloudflare Worker | The edge | Validate, HMAC-sign, and queue submissions in D1; serve the pull endpoints | Send email, call an LLM, or transmit anything outward (send-free by design) |
| Mac daemon (generation) | The MacBook | Pull the queue, verify HMAC, file to Box/Smartsheet, extract via one LLM call | Send customer email (no send capability, enforced at import) |
| Mac daemon (send) | The MacBook | Transmit human-approved rows via MS Graph | Call an LLM (no AI capability, enforced at import) |
| Operator | Tailscale → dashboard | Observe daemon health; PIN-gated config edits and error-log actions | Deploy or send externally from the dashboard; act without the PIN |
| External email sender | Inbound Graph | Nothing trusted — all inbound content is untrusted data (Invariant 2), wrapped and screened before use | Be trusted as-is; drive an action directly |

<!-- src: CLAUDE.md:"### Invariant 2 — Adversarial Input Handling" | verified 2026-07-14 -->
The governing rule at every boundary is **Invariant 2**: all content originating
outside the operating customer tenant is untrusted data. It is wrapped with
`shared.untrusted_content`, screened where it is a file or photo (Op Stds §34),
and passed through `shared.anomaly_logger.check()` before any extraction is
trusted. The architecture assumes prompt injection *might* succeed at the AI
layer and caps the damage at "extracted data is wrong" rather than "data
exfiltrated" or "external action taken" — because the AI process has no send
capability and no network egress.

## Edge cases & limitations

<!-- src: safety_portal/worker/index.ts:73-79 | verified 2026-07-14 -->
- **The Worker is stateless at the request layer.** Cloudflare owns the process
  lifecycle, so there is no fail-open/closed posture to maintain at the edge; a
  D1 error during login propagates and login fails closed (500). Session
  validity is cookie-derived — there is no server-side session revocation.
- **The Mac plane is a single host (SPOF).** Every daemon runs one MacBook. If
  the host dies, an external **Healthchecks.io** ping (the dead-man's switch) and
  the watchdog's staleness checks are what surface it — ITS cannot alert about its
  own total-host death from the same host. **The ping is not armed yet**
  (`system.heartbeat_url` still holds its seed placeholder, and the watchdog skips
  the ping while it does), so the SPOF is currently undetected from outside.
<!-- src: shared/box_client.py:31-36 | verified 2026-07-14 -->
- **Box refresh-token expiry window.** If ITS goes dark for more than ~60 days,
  the Box refresh token expires and the operator must re-run
  `scripts/setup_box_oauth.py`. Steady-state daily runs keep it fresh.
<!-- src: tests/test_capability_gating.py:279-297,485-513 | verified 2026-07-14 -->
- **The capability gate is static-import analysis.** It catches `import` and
  `from … import` statements; a dynamic `__import__(...)` call on the walked
  surface is a documented residual gap (a static `import importlib` is itself a
  flagged needle). Deliberately mis-naming a daemon also escapes the enrollment
  meta-test — the naming convention *is* the enforcement.

## Related docs

- **daemon_reference.md** — every daemon in detail: triggers, intervals, the
  watchdog checks, and the successor-operator repair runbooks.
- **data_model_reference.md** — the D1 schema, Smartsheet sheets/columns, and
  Box folder layout referenced above.
- **integration_reference.md** — the Smartsheet / Box / Graph / Anthropic
  clients and their auth and error models.
- **security_trust_model.md** — Invariants 1 and 2, the six-layer adversarial
  defense, and the §34 screening pipeline in depth.
- **escalation_matrix.md** — the Tier 1/2/3 maintenance model and the both-rule
  for what a Successor-Operator may repair.
- **glossary.md** — definitions of the terms used here (kill switch, send gate,
  pull model, workstream, SoR).
- **documentation_index.md** — the map of the whole Tier-1 documentation corpus.
