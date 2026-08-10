---
type: operations
date: 2026-07-09
status: active
related_prs: []
workstream: null
tags: [cutover, delivery, fail_closed, security, aug7_delivery]
---

# ITS Cutover Checklist (v2)

Mechanical checklist for the **validation → production** cutover: the build
flips off the `evergreenmirror.com` sandbox tenants and onto the real
`evergreenrenewables.com` identities (target: **Mon Aug 3**, per
`docs/2026-07-09_aug7_delivery_program.md` WS4). The cutover is **not done
until every item below is verified AND `python -m scripts.verify_cutover`
exits 0** (Op Stds v21 §53 — a cutover claim is narrative until mechanically
verified).

> **v2 rewrite (2026-07-09).** v1 (2026-05-29) had an item-2 numbering
> collision (two "Cutover item 2" sections) and Safety-Portal-only scope.
> v2 renumbers every item as `CL-NN`, widens scope to ALL live workstreams
> (safety, progress, field-ops trackers, PO) + host identity + DNS/mailboxes +
> Box + D1 hygiene + WAF/Paid plan + rollback + the Day-7 routing gate, and
> makes every item **mechanically verifiable** — a command or a sheet-cell
> check, never "confirm X works" prose. Items a machine can check
> cross-reference a `scripts/verify_cutover.py` check id (`VC-NN`).

## Purpose

One walkable list that turns the tenant cutover from memory into mechanism.
Companions: `docs/operations/host_migration_runbook.md` (the host must be
through Phase C go/no-go BEFORE this list starts),
`docs/operations/production_rollback.md` (every rollback path),
`docs/operations/aug7_delivery_runbook.md` (the on-site day).

## Checklist-id ↔ verify_cutover mapping

| verify_cutover | covers checklist items |
|----------------|------------------------|
| VC-01 `keychain` | CL-02 |
| VC-02 `launchd` | CL-03 |
| VC-03 `config` | CL-12, CL-36 (+ the config half of CL-14) |
| VC-04 `daemon-health` | CL-24 |
| VC-05 `review-queue` | CL-25 |
| VC-06 `alerting` | CL-26 |
| VC-07 `git` | CL-04 |
| VC-08 `d1-migrations` | CL-18 |
| VC-09 `heartbeat-url` | CL-27 |
| VC-10 `approver-shares` | CL-11 (+ CL-37's subcontracts leg) |
| full run (all ten) | CL-30 |
| (manual-only items) | CL-01, CL-05–CL-10, CL-13, CL-15–CL-17, CL-19–CL-23, CL-28, CL-29, CL-31–CL-35, CL-37–CL-42 |

## Procedure

### Phase 0 — preconditions & host identity

- [ ] **CL-01 — burn-in go/no-go recorded.** The Jul-31 host go/no-go verdict
  exists in a session log (`ls docs/session_logs/2026-07-31*` or later naming
  it). No-go = the cutover date moves; do not proceed.
- [ ] **CL-02 — Keychain complete on the production host** (20 secrets per
  `scripts/verify_cutover.py` `REQUIRED_SECRETS`: 11 non-Box + Box triplet +
  `ITS_PORTAL_PO_TOKEN` + the four dark-daemon bearers — config-actuator
  (`ITS_PORTAL_CONFIG_TOKEN`), subcontract-poll (`ITS_PORTAL_SUB_TOKEN`),
  estimate (`ITS_PORTAL_ESTIMATE_TOKEN`), RFQ (`ITS_PORTAL_RFQ_TOKEN`) — + the
  operator-dashboard PIN (`ITS_OPERATOR_PIN`)). Don't trust this prose count:
  `REQUIRED_SECRETS` is the composition of record.
  Verify: `python -m scripts.verify_cutover --only keychain` → PASS. (VC-01)
- [ ] **CL-03 — every must-load daemon runs on the production host only** (the shipped
  plist set minus `DARK_UNLOADED_LABELS` — VC-02 derives the exact set; 18 of the 20
  shipped plists at last count, don't trust a hardcoded number). `po-send` **and
  `rfq-send`** (both SEND daemons) stay **UNLOADED** — send-gate defense-in-depth; VC-02
  excludes them from the must-load set (both in `DARK_UNLOADED_LABELS`) and FAILS if either
  IS loaded. `subcontract-poll`, `rfq-poll`, and the other generation daemons load but are
  runtime-dark (`polling_enabled=false`).
  Verify: `python -m scripts.verify_cutover --only launchd` → PASS on the
  production host, AND on the dev box:
  `launchctl list | grep solutionsmith` prints nothing. (VC-02)
- [ ] **CL-04 — production host repo at clean origin/main.**
  Verify: `python -m scripts.verify_cutover --only git` → PASS. Never deploy
  or migrate from a stale checkout (forensic class #2). (VC-07)

### M365 / DNS / mailboxes

- [ ] **CL-05 — production app registration live.** Graph client-credentials
  flow succeeds against the `evergreenrenewables.com` tenant with the
  re-seeded `ITS_MS_*` triplet.
  Verify: `python scripts/smoke_test_graph.py` (read-only) exits 0.
- [ ] **CL-06 — EXO ServicePrincipal + Application Access Policy applied**
  (PowerShell done in July, before the Sep-1 deprecation; AAP scoped to the
  single production mailbox per CL-07 / CL-40).
  Verify: `Test-ApplicationAccessPolicy -Identity its@evergreenrenewables.com
  -AppId <ITS_MS_CLIENT_ID>` → `AccessCheckResult: Granted`, and the same for
  a non-ITS mailbox → `Denied`.
- [ ] **CL-07 — single production mailbox exists:** `its@evergreenrenewables.com`
  — ALL send lanes (safety, progress, procurement/PO, RFQ, subcontracts) send
  from this ONE mailbox for Phase 1; every `*.from_mailbox` ITS_Config row
  repoints to it (the CL-12 sweep). Later identity splits (a `safety@`,
  `procurement@`, …) use **SHARED mailboxes** (free, no license) — NEVER a
  personal mailbox: the Application Access Policy grants the app full mail
  read/write over the entire mailbox it covers (decision record:
  `docs/operations/phase1_cutover_decisions.md` D3). Mirror rehearsal
  mailboxes (its#460) are unaffected.
  Verify: Graph read of `its@evergreenrenewables.com` succeeds (smoke script),
  or EXO `Get-Mailbox its@evergreenrenewables.com` returns it.
- [ ] **CL-08 — DKIM/SPF verified for the production sending domain.**
  Verify: `dig TXT evergreenrenewables.com +short` contains the SPF record
  including M365 (`include:spf.protection.outlook.com`), and
  `dig CNAME selector1._domainkey.evergreenrenewables.com +short` resolves to
  the M365 DKIM host (repeat for `selector2`).
- [ ] **CL-09 — portal production DNS live.** The production custom domain
  resolves and serves the Worker.
  Verify: `curl -sI https://<production-portal-domain>/ | head -1` → `HTTP/2 200`
  and the response is the portal SPA (content-type text/html — remember the
  SPA fallback returns 200 for anything; also fetch a known asset and check
  its content-type).
- [ ] **CL-10 — Resend sender domain verified** (replaces the
  `onboarding@resend.dev` sandbox sender).
  Verify: Resend dashboard → Domains → solutionsmith domain `Verified`, and
  `grep -rn "onboarding@resend.dev" shared/ ITS_Config-sweep` shows the
  runtime sender comes from config, not the sandbox default.

### Smartsheet — tenant stand-up, shares, config sweep

> **The production Smartsheet/Box stand-up is ONE step** (rehearsal-proven,
> 2026-07-23 full sandbox rebuild): from a per-task worktree with its own venv,
> daemons down —
> `python3 scripts/migrations/standup.py --no-restore`
> — builders + auto-FLIP (`sheet_ids_regen`) + seeds run end-to-end; land the
> regenerated ID surfaces via PR; after merge + pull,
> `python3 scripts/migrations/standup.py finish` reloads the fleet DARK and
> prints the gate-flip worksheet (`docs/runbooks/tenant_standup.md`). The
> hand-FLIP builder walk is retired to a per-builder appendix in
> `scripts/migrations/README.md`.
>
> **Bridge step (required before VC-02/CL-30):** finish's `dark` posture leaves ALL FIVE
> send-dispatch plists unloaded, but the must-load set (CL-03/VC-02) expects every
> production-approved send lane loaded — after `finish`, load each per-plist
> (`./install.sh load org.solutionsmith.its.<label>`; a §44 operator action). As of
> 2026-08-10 that is ALL FIVE — `weekly-send`, `progress-send`, `subcontract-send`,
> `po-send` and `rfq-send` — because `DARK_UNLOADED_LABELS` is now EMPTY; the last two
> were activated by the operator and are no longer dark. Derive the set from
> `_expected_labels()` rather than from this list. Skipping this step fails CL-03, CL-24,
> CL-28 and CL-30 verbatim-by-design. (Historically `--posture full` was NOT the bridge
> because it also loaded po/rfq-send; with the set empty that distinction no longer bites,
> but keep using the per-plist loads so the action stays explicit and auditable.)

- [ ] **CL-11 — F22 approver authority = workspace membership (all three
  send-bearing workspaces).** The authorized-approver set is each workspace's
  **individual USER share list** (`smartsheet_client.list_workspace_share_emails`;
  GROUP shares do NOT count — a group-only share yields an empty authorized
  set and silently fail-closes every send). On the production **ITS — Safety
  Portal**, **ITS — Progress** and **ITS — Purchase Orders** workspaces:
  UNSHARE the mirror validation accounts (`daniels@` / `seths@` / `benf@`
  `evergreenmirror.com`) and SHARE the seven production approvers as
  individual USER shares:

  | Email | Person | Role |
  |-------|--------|------|
  | `jacobs@evergreenrenewables.com`   | Jacob Stephens     | CEO |
  | `ezraj@evergreenrenewables.com`    | Ezra Jones         | CFO |
  | `jechiahs@evergreenrenewables.com` | Jechiah Stephens   | Head of Engineering |
  | `benf@evergreenrenewables.com`     | Ben Finkhousen     | Senior PM |
  | `tiffanym@evergreenrenewables.com` | Tiffany Montastirsky | Head of Permitting |
  | `tealap@evergreenrenewables.com`   | Teala Paradise     | Procurement & Subcontracting |
  | `samr@evergreenrenewables.com`     | Sam Rigney         | Head of Field Operations |

  > ⚠️ The domain is `evergreenrenewables.com` (re-**new**-ables) — the
  > original contact sheet carried a `renwables` typo for Ezra. A non-matching
  > email fail-closes that approver **silently**. Each approver must be a real
  > Smartsheet user whose account email matches exactly (cell-history
  > `modifiedBy` exposes email only).

  Apply (mechanized): review/edit the approver manifest
  (`scripts/migrations/production_shares_manifest.json` — data, per-workspace
  narrowing + access levels are Seth's call), then
  `python3 scripts/migrations/seed_production_shares.py` (PLAN) →
  `--commit` (Seth-attended; ADD-only — mirror-account unshares stay a manual
  UI step, named loudly by the plan output).
  Verify (mechanical): `python -m scripts.verify_cutover --only approver-shares`
  → PASS (manifest ⊆ live USER shares, zero `evergreenmirror.com` residue,
  GROUP shares flagged as non-counting). (VC-10)
- [ ] **CL-12 — ITS_Config production sweep.** All load-bearing rows point at
  production: `safety_reports.portal.worker_base_url` (the production custom
  domain), the `from_mailbox` rows, `scheduled_send_local` rows seeded, and
  **zero `evergreenmirror` residue in any Value cell**. Gates are NOT part of
  this sweep: every `*_enabled` flip follows the activation plan — read each
  row's Description first (CL-13), send gates LAST + Seth (the earlier "all
  gates true" wording contradicted CL-03's send-daemons-dark and is retired).
  Apply (mechanized): `python3 scripts/migrations/production_repoint.py`
  (PLAN; classifies already-production / will-repoint / DRIFTED-refuse) →
  `--commit` (Seth-attended, typed phrase; §E gates structurally excluded —
  the tool cannot carry a gate flip).
  Verify: `python -m scripts.verify_cutover --only config` (NO
  `--allow-sandbox`) → PASS. (VC-03)
  *Phase-gated cutovers* (a leg deliberately staying mirror, e.g. Phase-1's
  portal-stays-on-mirror week) run `--profile <name>` instead — the profile
  exempts exactly its named rows from the sandbox scan and everything else
  stays production-scanned; `--allow-sandbox` is never a phase verdict
  (`scripts/verify_cutover.py` `PROFILES`).
- [ ] **CL-13 — gate-flip discipline.** Before flipping ANY `*_enabled` row,
  read its full Description cell — an in-cell precondition ("do NOT set true
  until …") is doctrine (§44 high-class), not a suggestion.
  Verify: each flipped row's Description carries no unmet precondition (fetch
  the row cells, not just the rowId).
- [ ] **CL-14 — no sandbox residue in code/config.**
  Verify: `grep -rn "evergreenmirror" --include="*.py" ~/its` → only
  intentional hits (docs, historical session logs, this checklist); zero in
  runtime paths. Plus the VC-03 config-value scan above. (VC-03 partial)
- [ ] **CL-15 — `ITS_Review_Queue` Workstream picklist includes every live
  workstream** (incl. `progress_reports`, `po_materials`).
  Verify: read the column's picklist options via
  `python -c "…get_columns…"` or the Smartsheet UI column editor — options ⊇
  the live workstream set; `shared/picklist_validation.REGISTRY` parity CI is
  green on main.

### Box

- [ ] **CL-16 — dedicated production Box identity.** Box OAuth re-run on the
  production host as `its@evergreenrenewables.com` (single-host rule — see
  `host_migration_runbook.md` Hazard 2; sealed mirror-secret backup FIRST per
  `production_rollback.md`).
  Verify: `python -c "from shared import box_client; print(box_client.get_client().user().get().login)"`
  → `its@evergreenrenewables.com`.
- [ ] **CL-17 — production folder roots + routing reseeded.** The two
  `*.box.portal_root_folder_id` rows are AUTO-PASTED by the stand-up's
  box-roots stage (`standup._stage_box_roots`) — this item shrinks to the
  CL-16 Box-OAuth prerequisite plus the resolve verify (plus
  `BOX_PROJECT_FOLDERS` / `ITS_Project_Routing` when the routing lane wakes).
  Verify: a dry read of each configured root resolves
  (`box_client` `get_folder` per configured id — no 404).

### Cloudflare Worker / D1 / WAF

- [ ] **CL-18 — remote D1 schema current.**
  Verify: `python -m scripts.verify_cutover --only d1-migrations` → PASS
  (runs `wrangler d1 migrations list its-safety-portal-db --remote`; one
  retry on transient 7403). (VC-08)
- [ ] **CL-19 — D1 production hygiene: no seed/test rows.** Schema-only on
  production; seed migrations skipped or their rows deleted.
  Verify (read-only):
  `npx wrangler d1 execute its-safety-portal-db --remote --command "SELECT username, disabled FROM users"`
  lists ONLY real accounts (no `test.pm`);
  `… --command "SELECT COUNT(*) c FROM submissions"` → 0 or only real rows.
- [ ] **CL-20 — real PM accounts provisioned** via
  `safety_reports.portal_admin add-user` (Mac CLI), one per field PM.
  Verify: the CL-19 users query lists exactly the expected PM set, all
  `disabled=0`, and one PM login succeeds on the production domain.
- [ ] **CL-21 — Workers Paid plan confirmed** (bcryptjs cost-10 login exceeds
  the Free plan's 10 ms CPU cap → Error 1102). If Paid is unavailable, the
  PBKDF2 swap must have landed BEFORE account provisioning.
  Verify: a real login on the production domain returns 200 (no 1102), and
  the Cloudflare dashboard shows the Worker on Paid. Mechanical probe:
  `curl -s -o /dev/null -w "%{http_code}" -X POST https://<domain>/api/login -d '<real-creds-json>' -H 'content-type: application/json'` → `200`.
- [ ] **CL-22 — WAF rate-limit on `/api/login`** (~5 req/10s/IP) + blanket
  `/api/*` rule staged.
  Verify: 12 rapid bogus-credential POSTs to `/api/login` from one IP → the
  tail requests return `429` (observe, then stop; do not lock a real
  account — bogus usernames only).
- [ ] **CL-23 — branch protection intact on the deploy path:** `main`
  requires `test` + `portal` + `secrets`.
  Verify: `gh api repos/SolutionSmith-debug/its/branches/main/protection --jq
  '.required_status_checks.contexts'` lists all three.

### Workstream enables — safest-first, send paths LAST

Order: intake → mirrors/trackers → compile → **send paths last**.

- [ ] **CL-24 — daemons healthy end-to-end on production config.**
  Verify: `python -m scripts.verify_cutover --only daemon-health` → PASS
  (every Enabled ITS_Daemon_Health row fresh < 2× interval). (VC-04)
- [ ] **CL-25 — review queue reachable** (triage surface live before any
  send enables).
  Verify: `python -m scripts.verify_cutover --only review-queue` → PASS. (VC-05)
- [ ] **CL-26 — alerting legs shape-valid** (Sentry DSN + Resend key).
  Verify: `python -m scripts.verify_cutover --only alerting` → PASS. (VC-06)
- [ ] **CL-27 — Healthchecks.io heartbeat configured** (`system.heartbeat_url`).
  This has **never been armed on any host** — the row still holds its seed
  placeholder, and `scripts/watchdog.py` skips the ping while it does, so there
  is currently no external dead-man's switch at all. Create the check with
  **period = 1 day, grace = 1 hour** (the watchdog pings once daily at 07:00),
  then write its https ping URL into the row.
  Verify: `python -m scripts.verify_cutover --only heartbeat-url` → PASS;
  check green in the Healthchecks.io dashboard. (VC-09)
- [ ] **CL-28 — fail-closed send smoke, per send-bearing workstream (safety,
  progress; PO when live).** On the production review sheet: one row approved
  by a workspace member → `*_send_poll` DISPATCHES it; one row approved by a
  NON-member account → send **blocked** and a forensic `approval_unverified`
  event lands in ITS_Errors.
  Verify: the two rows' `Send Status` cells (SENT vs still PENDING/HELD) +
  the ITS_Errors row exist. This proves F22 against production identities —
  run it BEFORE real recipients are wired live.
- [ ] **CL-29 — real-recipient wiring (Teala-coordinated) recorded.**
  `ITS_Active_Jobs` safety-reports contact + CC columns (and the progress /
  PO equivalents) carry the production recipients.
  Verify: read the contact cells for every Active job — zero
  `evergreenmirror.com`, zero blanks on active rows (a blank TO = HELD, never
  silent, but should be zero at cutover).

### Subcontracts (operator-scoped fully in-scope incl. send, 2026-07-12)

- [ ] **CL-34 — subcontract Worker deployed + D1 migrations 0049–0052 applied.**
  The `worker/subcontract.ts` routes are live and D1 migrations 0049 (subcontractors),
  0050 (subcontracts + sov_lines), 0051 (`cap.subcontracts.manage`), 0052 (region→state)
  are applied on production D1 **before** the Worker deploys (deploy-order-critical,
  forensic class #2).
  Verify: `wrangler d1 migrations list <db> --remote` → none pending (VC-08 covers this
  fleet-wide); a subcontract internal route returns 401 without its bearer.
- [ ] **CL-35 — subcontract Worker secret + Keychain twin.** `PORTAL_SUB_API_TOKEN`
  (wrangler secret) set on the production Worker, and its Keychain twin `ITS_PORTAL_SUB_TOKEN`
  seeded (named by CL-02 / VC-01). The shared `ITS_PORTAL_HMAC_SECRET` must equal the Worker
  payload secret (domain-separated `sub:v1`, not key-separated).
- [ ] **CL-36 — subcontract poll gate rows seeded** (`seed_subcontracts_config.py` ran):
  the three `subcontracts.subcontract_poll.{polling_enabled,subcontractors_sync_enabled,status_sync_enabled}`
  rows exist. Verify: VC-03 asserts presence (never a forced value — ITS_Config is the
  single source of live gate state). The rows are the daemon's pause/activate switches;
  flipping them is a later operator cell-flip decision.
- [ ] **CL-37 — subcontract review + registry shares (F22 = §46 membership).** The
  `Subcontract_Pending_Review` review-twin sheet and the `ITS — Subcontracts` workspace
  share list carry the production approver identities (send/execute approval authority is
  workspace membership, not a portal capability). `ITS_Subcontractors` seeded
  (`seed_its_subcontractors.py`).
- [ ] **CL-38 — subcontract SEND half (SC-S4) — BUILT (landed 2026-07-15), a must-load
  lane, NOT a blocker.** `subcontract_send.py`/`subcontract_send_poll.py` are enrolled in
  the capability-gating SEND list, the `org.solutionsmith.its.subcontract-send` plist
  ships in the must-load set (NOT dark-unloaded — CL-03/VC-02 count it), and its config
  rows (`subcontracts.subcontract_send.from_mailbox`/`polling_enabled`/
  `scheduled_send_local`) are VC-03-enrolled (`seed_subcontracts_send_config.py`).
  ITS_Config is the single source of the gate's live value; changing a send gate is a
  FIXED External-Send-Gate action (Seth). Runbook: `docs/runbooks/subcontract_send.md`.
  **Do NOT gate Aug-7 done on this item.**
- [ ] **CL-38b — RFQ SEND half (ADR-0004 R3-R4) — BUILT and since ACTIVATED; a must-load
  lane, NOT a blocker.** The
  outbound-RFQ send lane (`rfq_send.py`/`rfq_send_poll.py`, plist `org.solutionsmith.its.rfq-send`)
  is built and its config rows are seeded present (`seed_rfq_send_config.py`: `from_mailbox`
  sandbox-scanned, `polling_enabled`/`scheduled_send_local`/`poll_interval_seconds` seeded —
  VC-03 asserts presence, NEVER forced `true`). `rfq-send` WAS a **dark-unloaded** SEND daemon;
  the operator activated it (gate flipped + plist loaded), and it was removed from
  `DARK_UNLOADED_LABELS` on 2026-08-10 alongside `po-send` so VC-02 stopped reporting a
  months-old deliberate decision as a send-gate violation. It is now in the must-load set.
  ITS_Config is the single source of the gate's live value — read it there, never here.
  **Re-darkening this lane, or first-enabling any FUTURE send lane, is a FIXED
  high-capability-class External-Send-Gate operator action (Seth): add the label back to
  `DARK_UNLOADED_LABELS`, flip `po_materials.rfq_send.polling_enabled`, and unload the plist
  together.** Uses the existing `ITS_PORTAL_RFQ_TOKEN` bearer — no new secret. **Do NOT gate
  Aug-7 done on this item.**

### Production Worker topology

- [ ] **CL-39 — production Worker is a SECOND Worker + D1** (not a route-swap on the mirror
  Worker), so the mirror Worker (`safety.evergreenmirror.com`) and rollback **R1** stay intact
  (operator decision 2026-07-12; see `docs/operations/production_worker_route_decision.md`).
  Verify: the production `wrangler` env/route points at the production custom domain; the mirror
  Worker is untouched (still resolves + serves).

### Phase-1 identity & account decisions (2026-07-23 — see `phase1_cutover_decisions.md`)

- [ ] **CL-40 — single-mailbox AAP scope verified (manual).** The Exchange
  Application Access Policy is scoped to `its@evergreenrenewables.com` ONLY —
  the app cannot touch any other mailbox (D3,
  `docs/operations/phase1_cutover_decisions.md`).
  Verify: `Test-ApplicationAccessPolicy -Identity its@evergreenrenewables.com
  -AppId <ITS_MS_CLIENT_ID>` → `Granted`, AND the same cmdlet against at least
  one PERSONAL mailbox (e.g. `jacobs@evergreenrenewables.com`) → `Denied`.
  Paste both results into the cutover session log.
- [ ] **CL-41 — Cloudflare account transferred + Workers Paid on the
  transferred account (manual).** The existing dedicated Cloudflare account is
  owned/billed by `its@evergreenrenewables.com` (Super-Admin invite accepted,
  payment method on file, Workers Paid active on THIS account — the CL-21
  Paid-plan check must hold post-transfer). No Worker / D1 / zone migration;
  the portal stays on `safety.evergreenmirror.com` through Phase 1 (D2,
  `phase1-hybrid` profile).
  Verify: Cloudflare dashboard → Members shows `its@evergreenrenewables.com`
  as Super-Admin + billing owner; plan page shows Workers Paid; re-run the
  CL-21 login probe → `200` (no 1102) after the transfer.
- [ ] **CL-42 — Phase-1 Smartsheet identity decision acknowledged (manual).**
  The Phase-1 Smartsheet token identity is an operator-designated personal PAT
  (a workspace owner). The F22 self-exclusion filter is DELIBERATELY not
  shipped for Phase 1 — per the Op Stds §46 owner-inclusion residual, the token
  identity is inherently within the approver set, so filtering it would remove
  that account's legitimate approval authority. §44 high-class,
  operator-ratified 2026-07-23.
  Verify: `docs/operations/phase1_cutover_decisions.md` (D1) read by whoever
  walks this checklist, and the tech-debt entry "F22 token-identity
  self-exclusion filter" still names the migration trigger (the dedicated
  `its@` Smartsheet seat). No mechanical check — this row exists so the
  posture is a recorded decision, not an accident.

### Final gate + Day-7

- [ ] **CL-30 — THE GATE:** `python -m scripts.verify_cutover` (full run, no
  flags) exits **0** on the production host. Paste the output into the
  cutover session log verbatim. (VC-01…VC-09)
- [ ] **CL-31 — rollback assets in place BEFORE declaring done:** sealed
  mirror-secret backup exists (see `production_rollback.md` — made BEFORE any
  secret was overwritten); mirror Worker still deployed; rollback doc printed
  for the on-site binder.
  Verify: `curl -sI https://safety.evergreenmirror.com/ | head -1` → 200 (the
  mirror rollback target is alive).
- [ ] **CL-32 — Day-7 routing gate armed.** Alerts (Resend/Sentry/Healthchecks.io)
  stay routed to Seth beyond Day 7 until the Tier-2 clearance milestone
  (handover v10 amendment, D17).
  Verify: alert-destination fields in Resend templates / Sentry alert rules /
  Healthchecks.io contacts list Seth's addresses; a dated tech-debt or session-log
  entry names the Day-7 review date.
- [ ] **CL-33 — Day-7 review executed (T+7):** zero unexplained CRITICALs,
  Check-C markers continuous, dedupe summaries reviewed; only THEN disable
  mirror portal users and (optionally) tear the mirror Worker down per
  `production_rollback.md` §"Mirror decommission".
  Verify: session-log entry `docs/session_logs/` dated T+7 with the three
  observations.

## Daemon (re)install — interval substitution (§43 note, carried from v1)

A cutover that (re)installs the launchd daemons uses
`scripts/launchd/install.sh load <plist> [interval]`. For the **interval**
daemons (`weekly-send`, `portal-poll`, `compile-now-poll`, `progress-send`,
`fieldops-sync`) the installer substitutes `__POLL_INTERVAL_SECONDS__` from
(priority): the optional `[interval]` arg → the daemon's ITS_Config
poll-interval row → a per-daemon default (900 / 60 / 90 / 900 / 90). If the
token / Smartsheet isn't ready at cutover time the read falls back to the
default (a `note:` line on stderr — harmless). After each load, confirm with
`install.sh status` and `plutil -lint` on the installed copy.

**Successor-Operator boundary (Op Stds §43/§44):** running the installer +
reading a config row is low-capability-class. If `install.sh load` fails
`plutil -lint` or a `__…__` placeholder survives, **escalate to Seth** — a
plist or installer change is a code change (high-class).

## Validation

The cutover is done when CL-01 … CL-32, CL-34–CL-37, and CL-39–CL-42 are all checked,
CL-30's full `verify_cutover` output (exit 0) is pasted in the cutover session log,
and CL-33 is scheduled with a named date. (CL-38 is a deferred SC-S4 build
dependency — explicitly NOT required for Aug-7 done.) Anything less is §52
narrated-not-enforced — not done.

## Owner

`@solutionsmith`. Future workstreams append items here (with a `CL-NN` id and
a mechanical verify) in the PR that owns the new cutover-sensitive config;
machine-checkable items also enroll in `scripts/verify_cutover.py` in the
same PR.
