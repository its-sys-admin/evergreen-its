---
type: brief
date: 2026-07-09
status: active
related_prs: []
workstream: null
tags: [aug7_delivery, purchase_orders, operator_dashboard, docs_program, cutover, phase_1.5]
---

# Aug-7 Evergreen Delivery Program — PO Workstream, Operator Dashboard, Docs Program, Production Cutover

## Objective

Deliver a working, **production-cutover** ITS in person at Evergreen's office on **Friday
2026-08-07**: the Purchase-Order generator live end-to-end, the Operator Dashboard running, the
delivery-critical branded-PDF manual set in hand, and the older MacBook Pro installed on-site as
the production host. Today is 2026-07-09; the operator is unavailable Jul 25–30, leaving **~17
working days** (Jul 9–10, 13–17, 20–24, 31, Aug 3–6). This document is the canonical program —
ROADMAP Track 5 points here; the operator-approved plan of record it was landed from is
`~/.claude/plans/we-are-going-to-fancy-harp.md` (scratch; this doc supersedes it as canonical).

## Pre-implementation: verify baseline

- Baseline: `main` @ `0cdfa5c` (2026-07-09). Mirror suite fully live; migrations through 0041
  applied; §54 backstop landed (#489).
- Every current-state claim below was verified against live HEAD on 2026-07-09 by three
  exploration passes (exec repo anatomy, blueprint doctrine, PO corpus). Re-verify
  (brief-validator discipline) before acting on any slice — claims drift.
- **PO corpus location (verified):** `~/Desktop/Evergreen project/zip project
  documents/04_Purchase_Orders/Filled` (96 files) + `06_Racking_Module_POs/Filled` (77 files) +
  `Blank/` templates. The Box-mirror `Purchase Order Draft`/`Executed` folders are **empty
  template clones** — the Desktop corpus is the only source and is reachable only from a local
  session. Analysis committed at `docs/reports/2026-07-09_po_corpus_analysis.md`; representative
  samples at `docs/references/po_samples/` (slice S0 — DONE at this doc's landing).
- Pre-cutover-conditions status (handover-plan v9, reconciled 2026-07-09):

| # | Condition | Status |
|---|-----------|--------|
| 1 | Triple-fire CRITICAL fired-and-triaged | LIKELY MET — compile the evidence record (e.g. 2026-07-05 Task-column KeyError); don't re-fire |
| 2 | Phase-1.4 hardening | Picklist + trusted-contacts delivered; photo screening live; residue = ClamAV enablement + EICAR verify |
| 3 | Teala-coordinated real-recipient wiring | OPEN — external dependency, fire request Day 1 |
| 4 | Tier-1 self-heal complete | **PARTIAL** — Check C (all tracked daemons) and Check I are complete and live. The F16 external beacon is **NOT** complete: the vendor is Healthchecks.io (`shared/heartbeat_client.py`), and the ping is skipped while `system.heartbeat_url` holds its seed placeholder, so total-host death has never been externally detectable on any host. Arming it is a config change (VC-09 requires https). |
| 5 | Tier-2 operator trained | DELIBERATELY SOFTENED → post-delivery milestone (handover v10 amendment, D17) |

- Also verified: `worker/auth.ts` is **bcryptjs cost-10, not PBKDF2** — mitigation of record is
  the Workers Paid plan; if unavailable, a ~1-day PBKDF2 swap lands BEFORE production accounts.
  `cutover_checklist.md` is prose with an item-2 numbering collision and Safety-Portal-only
  scope — v2 rewrite + mechanical gate script required (WS4).

## Context

Four workstreams compose the delivery:

1. **WS1 — Purchase-Order generator** (`po_materials/`, ~9d): portal builder → deterministic
   PDF → Box/Smartsheet filing → F22-gated human review → two-process send to vendor. The repo
   already anticipates it (reserved `po_materials` tag, commented capability-gating stubs at
   `tests/test_capability_gating.py:140-142,184-185`, Vendor DB sheet, doctrinal Phase-2 slot).
2. **WS2 — Operator Dashboard** (`operator_dashboard/`, ~4d): localhost FastAPI web app,
   Tailscale-only; §44 Tier-2 low-class action set + rich read-only visibility.
3. **WS3 — Docs program** (Track 4 / §6 / A8, ~3d): repeatable md→branded-PDF pipeline + the
   12-PDF delivery-critical set incl. the ITS Owner's Manual and the auto-generated ITS_Config
   data dictionary.
4. **WS4 — Host migration + tenant cutover + hardening + delivery day**: old MBP provisioned
   early and burned in; mirror→`evergreenrenewables.com` cutover Aug 3; two dress rehearsals;
   scripted Aug-7 on-site install/demo/training/acceptance.

Subcontracts: **design-in, build-later** — the PO data model, terms library, and review/send
pattern stay reusable for it; zero subcontract code before Aug 7.
**[SUPERSEDED 2026-07-12 — see D18 Amendment A1 below: subcontract generation was built
deterministic/no-AI (ADR-0003) and subcontracts is now IN Aug-7 scope, fully in incl. send.]**

## Decisions already resolved

Locked via operator grilling 2026-07-09 (do not reopen without operator say-so):

| # | Decision | Resolution |
|---|----------|------------|
| D1 | Aug-7 definition of done | Full production cutover complete — the demo is the real system on live tenants |
| D2 | PO transport | ITS emails the PO PDF to the vendor after human approval (two-process Invariant-1 send path) |
| D3 | RFQ vs PO | Both eventually — PO generator live Aug 7; RFQ designed into the data model, built post-delivery; blueprint mission v4 (RFQ framing) superseded |
| D4 | Vendor list sync | Editable both sides; Smartsheet = SoR, D1 = portal cache; up-sync bridge-key find-or-create, down-sync full-replace with dirty-row fence. First bidirectional §51 instance — rider ratified in this program |
| D5 | Purchaser legal entity | "Evergreen Renewables LLC" + Irvine STE 570 + 888-303-6424 + invoice cc-list, as versioned config; entity confirmation with Evergreen = pre-cutover checklist line |
| D6 | Terms & conditions | Git-versioned terms library, selectable per PO; default = standard 17-clause; vendor carries default terms profile; negotiated GTCs are attach-not-generate |
| D7 | PO numbering | ITS-generated, existing scheme `{YYYY.NNN}.{site}.{supersede}.{rev}`, supersession chaining, status machine draft→pending_review→approved→sent→superseded/canceled; number sourced from job record, never folder tags |
| D8 | Line items | Structured free-form rows with computed extended/subtotal/tax/shipping/total; tax auto by ship-to state (IL 9%, OR 0%, exempt/included, override); material-catalog pick = fast-follow |
| D9 | Signatures | Send-for-countersign; approver NAME/TITLE autofilled on Purchaser block; drawn-signature (SVG) fast-follow |
| D10 | From-mailbox | procurement@ (mirror + production) via ITS_Config `from_mailbox`; provisioning on both tenants is a checklist item |
| D11 | PO users | Any portal admin drafts (`cap.po.manage` → admin role); approval authority = ITS — Purchase Orders workspace share list (§46) |
| D12 | Dashboard platform | Localhost Python web app (FastAPI) on the ITS Mac, Tailscale-only, launchd-managed |
| D13 | Dashboard scope | §44 Tier-2 action set + read-only visibility; send-gate/secrets/doctrine/code never exposed as actions |
| D14 | Dashboard timing | Must-have for Aug 7, sequenced after PO core + cutover prep |
| D15 | Docs scope | Delivery-critical PDF set by Aug 7 via repeatable md→PDF pipeline; full A8 continues post-delivery |
| D16 | Hardware | Older MacBook Pro becomes production host, installed at Evergreen Aug 7; provision early + burn in |
| D17 | Operator gate | Handover pre-cutover condition #5 deliberately softened: Seth remains operator; Tier-2 clearance = named post-delivery milestone (recorded handover v10 amendment) |
| D18 | Subcontracts | Design-in, build later — first post-delivery workstream **→ SUPERSEDED 2026-07-12 (Amendment A1)** |

> **Amendment A1 (2026-07-12) — Subcontracts reversed INTO Aug-7 scope.** Per operator directive, D18's
> "design-in, build later" is superseded (history preserved above): subcontract-package **generation** was built
> deterministic + **NO AI** (ADR-0003; SC-S1→S3c; PRs #529–#540) and ships **dark**, ahead of Aug 7 rather than
> post-delivery. The operator scoped subcontracts **fully in incl. send** (2026-07-12), so the **SEND half (SC-S4)**
> — `subcontract_send.py` + F22 + executed-countersign + send-poller plist — is **not yet built**
> (commented stub only), tracked as **CL-38** + a separate SC-S4 engineering brief (Seth). **It is a best-effort
> Aug-7 target — try to build it, but it is NOT a cutover blocker:** if SC-S4 doesn't land, generation ships and
> subcontract SEND defers gracefully post-delivery. Do not gate Aug-7 done on it.

## Foundation invariants

- **Invariant 1 (External Send Gate, permanent):** the PO send is a NEW vendor-facing external
  audience. Generation (`po_poll.py`/`po_generate.py`) has zero send capability AND zero AI
  (fully deterministic); send (`po_send.py`/`po_send_poll.py`) has zero AI. Both enrolled in
  `tests/test_capability_gating.py` (replace the reserved commented stubs). `PO_Pending_Review`
  + F22 `verify_approval` (cell-history, fail-closed) gate every dispatch; recipients resolved
  at send time from the Vendors sheet.
- **Invariant 2:** PO drafts arrive from authenticated office admins but are still
  client-supplied data — Worker-side validation, bounds, W4 atomic mutation+audit batches,
  HMAC (`po:v1` domain) over the queued payload, daemon-side re-verify + totals recompute.
- **§51 (bidirectional rider — NEW):** vendor list is ITS-owned SoR in Smartsheet with a D1
  cache; down-sync full-replace with a dirty-row fence (a `sync_state='pending'` portal edit is
  never clobbered); up-sync bridge-key find-or-create, column-scoped, watermarked; never-delete
  (deactivate). First bidirectional instance — Seth ratifies rider vs v21 bump (S7).
- **§44 (dashboard):** ACT surface = exactly the low-class set, registry-bounded + denylist-
  tested; everything else read-only + "escalate to Seth" diagnostic bundle.
- **§53 (cutover):** mechanically verified — `scripts/verify_cutover.py` exits 0 or the cutover
  is not done.

## Implementation tasks

### WS1 — Purchase-Order generator (slices S0–S8, ~9d)

Architecture: SPA builder → Worker validates/computes cents/allocates PO number atomically in
D1/HMAC-signs/queues → `po_materials/po_poll.py` pulls, verifies, renders (reportlab, reusing
`form_pdf.py` brand primitives), files to Box (§45/§47), appends `PO_Log` + `PO_Pending_Review`
rows, receipts → F22 approval → `po_send_poll.py`/`po_send.py` dispatch via the lightly
generalized `weekly_send.send_one_row`, vendor-resolved recipient, from procurement@. Pattern:
parameterize-not-clone over the `progress_reports/` thin-binding precedent (§14).

- **S0 — PO corpus capture (0.5d, local-only) — DONE at this doc's landing.**
  `docs/reports/2026-07-09_po_corpus_analysis.md` + `docs/references/po_samples/` (3 PDFs + the
  2 blank template DOCX incl. the 17-clause T&C source). S0 gates S3 (verbatim terms
  transcription) and S4 (PDF layout); S8 adds a golden-sample gate (re-key one real PO, compare
  renders side-by-side before first live send).
- **S1 — Smartsheet workspace + sheets + registry (1.0d).** New workspace `ITS — Purchase
  Orders` (eighth §23 standalone exception; §46 membership = approval authority). Builders
  (idempotent, `--dry-run`, FLIP-precedes-SEED): `scripts/migrations/
  build_purchase_orders_workspace.py`, `build_its_vendors_sheet.py` (Vendor Name · Vendor Key
  `VEN-######` · Address · Contact Name/Email/Phone · Region PICKLIST · Supply Categories
  MULTI_PICKLIST · Default Terms Profile · GTC Reference · Active · Notes · MODIFIED cols),
  `build_po_log_sheet.py`, `build_po_pending_review_sheet.py` (**WSR schema twin** — Vendor Key
  in the `COL_JOB_ID` slot, PO Date in `COL_WEEK_OF`, PO PDF in `COL_COMPILED_PDF`; Workstream
  `po_materials`), `seed_its_vendors.py` (old Vendor DB rows + corpus vendor set). Edits:
  `shared/sheet_ids.py` constants; `shared/picklist_validation.py` REGISTRY parity same-PR.
  **Vendor DB decision:** `ITS_Vendors` = sole SoR; old Operations Vendor DB (7278304330469252)
  retired-in-place (one-time copy, `Picklist_Sync_Config` re-point, DECOMMISSIONED comment).
- **S2 — D1 migrations + Worker `po.ts` (1.5d).** `0042_po_vendors.sql` (cache + counter +
  origin/sync_state/watermarks), `0043_purchase_orders.sql` (drafts + line items, money as
  integer cents, tax_mode + tax_rate_bp, line_column_variant, supersedes_po_id, status machine,
  UNIQUE `(job_no, site_phase, supersede_seq, revision)`), `0044_po_capability.sql`
  (`cap.po.manage` → admin). `worker/po.ts` `registerPoRoutes`: browser routes (session +
  cap) for vendors CRUD / drafts CRUD / generate (re-validate, cents math, tax table, atomic
  allocation, HMAC `po:v1`, →queued) / supersede / cancel / terms / config; internal routes
  under a **new bearer tier `requirePoToken`** (`ITS_PORTAL_PO_TOKEN`): pending-drafts,
  mark-filed, status-sync, vendors sync/pending/mark-mirrored (full-replace + dirty-row fence).
  Tests `safety_portal/test/po.spec.ts`; `test_worker_send_free.py` stays green.
- **S3 — Terms library + versioned config (0.5d).** `po_materials/terms/` manifest +
  `standard_17_v1.md` (VERBATIM from `docs/references/po_samples/Purchase Order 2019.docx` —
  operator legal review before first live send) + `chint_vendor_v1.md` + `negotiated_gtc`
  (attach-kind). Immutable versioned files; drafts pin id+version. `po_materials/config/
  purchaser.json` + `tax.json` (basis points) — Worker imports at build time, Python reads at
  render time, totals assert catches skew.
- **S4 — Python generation pipeline (2.0d).** ONE multi-pass gated daemon
  `po_materials/po_poll.py` (the `fieldops_sync.py` model): drafts pass → vendor down-sync →
  vendor up-sync → status pass. New: `po_materials/{numbering,vendors,po_generate,po_review,
  po_log,po_poll}.py`; `shared/portal_client.py` additions; `shared/portal_hmac.py` `PO_DOMAIN`;
  plist + install.sh. Render layout per the corpus field inventory (see the S0 report):
  3 line-item column variants, state-labeled tax, supersession clause, dual signature blocks.
  Enrollment same-change: capability gating, watchdog `TRACKED_JOBS`, ITS_Config gates SEEDED
  false, `REQUIRED_CONFIG`.
- **S5 — Send side + engine generalization (1.5d).** `weekly_send.SendConfig`:
  `active_jobs_config` → `recipient_lookup: Callable`; add `envelope_builder`. Safety/progress
  bindings byte-equivalent; их tests pass unchanged + live smokes of both before merge —
  **highest-blast-radius edit; fallback = fork the transmitter.** New `po_send.py` +
  `po_send_poll.py` (F22 workspace = ITS — Purchase Orders), SEND_SCRIPTS enrollment,
  `scripts/smoke_test_po_send.py`.
- **S6 — SPA (1.5d, parallelizable from day 3.5).** `PoBuilderPage.tsx` (job select w/
  auto-fill + tax badge → vendor select w/ region/category chips → line-items grid → totals →
  SOW/delivery/payment → terms picker → optional supersede → preview → Generate → status
  tracker) + `PoVendorsPage.tsx` (MaterialsCatalogPage clone). Router/caps/HOME_CARDS wiring.
- **S7 — Doctrine/blueprint (0.5d, parallel).** Mission v5 rewrite; §23/§24 eighth workspace;
  §51 bidirectional rider; `docs/doctrine_manifest.yaml`; runbooks `po_poll.md` + `po_send.md`;
  enablement `purchase_orders.md`.
- **S8 — Live smokes + prove-the-controls (0.5d).** `smoke_test_po_generate.py` +
  `smoke_test_po_send.py`; golden-sample gate (S0 report); bites tests: non-member approval
  blocked · tampered HMAC refused · contamination row HARD-HELD · blank vendor email
  `held_no_recipient` · colliding po_number fail-closed · fence preserves portal edit ·
  synthetic forbidden import reds the gate.

Cut list (fast-follow if slipping): vendor up-sync (Smartsheet-only editing at launch) ·
PO_Log mirror · supersede UI · per-watt/lump-sum variants · Chint terms file · GTC
attach-at-send. **Never cut:** F22, HMAC, two-process split, numbering uniqueness, tax config,
enrollment gates.

### WS2 — Operator Dashboard (~4d)

FastAPI + uvicorn + Jinja2 + vendored htmx; `127.0.0.1:8484`; `tailscale serve` only. READ
loginless; ACT behind Keychain PIN + CSRF + Origin allowlist. Local-files-first data (logs TSV,
watchdog markers, heartbeats, breaker state, lock probes, `launchctl list`), TTL-cached
Smartsheet panels. ACT = the §44 five, registry-bounded (`TOGGLABLE_GATES`, `DAEMON_LABELS`,
`CLEARABLE_LOCKS`, add-only re-seed, re-send = Send-Status-column-only) + denylist test.
Diagnostic-bundle export through `shared/redact.py`. Enrollment: `WALKED_ROOTS`, subprocess
allowlist for `launchctl.py` only, GATED_SCRIPTS, bites test. `KeepAlive=true` plist (documented
deviation); **no `@require_active`** (it must run while PAUSED). Slices: D1-1 read-only core
(1.5d) → D1-2 actions+auth+audit (1.5d, adversarial review DoD) → D1-3 runbook + guide + 10-item
live smoke (1d).

### WS3 — Docs pipeline + delivery-critical PDF set (~3d)

reportlab + `markdown-it-py` (only new dep). `docs_pdf/` package (`brand.py` mirrors
`form_pdf.py` palette #1f4d2e/#b8860b + logo + footer; `md_render.py` tokens→Platypus;
`manifest.py`). Creates `docs/enablement/manifest.yaml` (THE §6a manifest — closes the open
tech-debt registration). `scripts/build_docs_pdfs.py` (`--all|--doc|--upload|--check`; upload →
Box "ITS Manuals", §45/§47). Doc-currency: SHA-256 staleness + warn-only CI step + cutover-
checklist line. `scripts/generate_config_dictionary.py` → `docs/references/
its_config_dictionary.md` + `operator_dashboard/config_defaults.json`. 12-PDF set: 6 existing
guides (2 touched) + safety-forms guide + admin-dashboard guide + PO guide (last) + ITS Owner's
Manual + config dictionary + dashboard guide. Slices: D2-1 pipeline (1.25d) → D2-2 content +
dictionary (1.25d) → D2-3 Box publish (0.5d).

### WS4 — Host migration, tenant cutover, hardening, delivery day

- **Host migration** (2 active days + ≥10-day burn-in; runbook `docs/operations/
  host_migration_runbook.md`): Phase A provision Jul 10 (tooling, sibling repos, venv, Keychain
  re-seed argv-form EXCEPT Box, plists installed UNLOADED, read-only smokes); Phase B one-way
  flip Jul 13 (~30-min ordered window — dev box unload + plist REMOVAL + `launchctl list`
  empty → copy `state/` + markers → Box re-seed ONLY on new host → load 11 → verification
  gates incl. the Healthchecks.io prove-it-bites); Phase C burn-in Jul 14→Aug 3 (Friday cycles Jul
  17/24; Jul 25–30 gap = unattended Tier-1 trial; Jul 31 go/no-go).
- **Tenant cutover** (Aug 3; artifacts: `cutover_checklist.md` v2 + `scripts/verify_cutover.py`
  + `docs/operations/production_rollback.md`): M365 flip (app registration, EXO
  ServicePrincipal in July, mailboxes safety@/progress@/procurement@ on production +
  progress@/procurement@ on mirror, Access Policy, DKIM/SPF) → Smartsheet re-shares/purge/config
  sweep (7 approvers as individual USER shares on Safety + Progress + PO workspaces) → Box
  dedicated `its@evergreenrenewables.com` re-auth → Worker production deploy on the new custom
  domain + Paid-plan/WAF + D1 hygiene + real accounts → workstream enables safest-first, send
  paths LAST with the two fail-closed smokes → `verify_cutover.py` green → Day-7 routing gate
  (alerts stay with Seth until the Tier-2 milestone). Rollback: sealed mirror secret backup,
  mirror Worker stays deployed, `system.state=PAUSED` global brake.
- **Hardening gate** (~1d): Paid-plan confirm (else PBKDF2 swap first), WAF rules, ClamAV +
  EICAR bites, verify-only items, cutover-blocking debt only (its#460 mailboxes, D1 hygiene,
  `scheduled_send_local`, Box user, publish_daemon watchdog slug, meta-002 SLA doc).
- **Aug-7 runbook** (`docs/operations/aug7_delivery_runbook.md`): MAINTENANCE + Healthchecks.io
  window transport; on-site install gates (network → Tailscale reverse-access over hotspot →
  15 daemons healthy → verify_cutover re-run); 40-min demo arc (field submit → **PO built
  live** → F22 approval → send lands in supplier-stand-in inbox → dashboard tour → manuals
  handoff; pre-empt the Friday 14:00 cycle with Compile Now, rehearsed); 60-min training (PM
  track + owner Step-8 drills demonstrated BY them); acceptance sign-off with v10 amendment
  language; leave-behind package.

### Master calendar

| Date | Operator-led | Agent-led |
|------|--------------|-----------|
| Wed Jul 9 | Fire ALL external requests; `gh issue close 338 340`; mirror mailboxes | PO S0–S1 |
| Thu Jul 10 | Host Phase A | PO S1–S2 |
| Mon Jul 13 | **Host Phase B one-way flip** | PO S2–S3 |
| Jul 14–16 | Burn-in; hardening; checklist v2 + verify_cutover.py drafts | PO S4 |
| Fri Jul 17 | Burn-in Friday cycle #1 | PO S4–S5 |
| Jul 20–22 | Production staging (additive): app reg, ServicePrincipal, mailboxes, DNS, Box, WAF | PO S5–S6 finish; **Dashboard starts** |
| Thu Jul 23 | External-dependency escalation deadline | Dashboard D1-2; **Docs start** |
| Fri Jul 24 | Friday cycle #2; daemon-path code freeze | Dashboard D1-3 |
| Jul 25–30 | **GAP — unattended burn-in.** Nothing merges to live | (held work only) |
| Fri Jul 31 | Go/no-go; after 14:00: D1 hygiene, production deploy, accounts | Docs D2-2 |
| Mon Aug 3 | **CUTOVER DAY** → verify_cutover green | Docs D2-3 |
| Tue Aug 4 | Soak; **dress rehearsal #1** | — |
| Wed Aug 5 | Soak; **dress rehearsal #2**; HARD CODE FREEZE | — |
| Thu Aug 6 | **Buffer day** — pack, print, charge, foreign-network Tailscale test | — |
| Fri Aug 7 | **DELIVERY** | — |

Critical path: Jul 9 external requests → Jul 20–22 staging → Aug 3 cutover → rehearsals →
Aug 7. Host flip must not slip past Jul 14. Slippage cut order (PO > cutover > dashboard >
docs): docs polish first, dashboard second (Daemon-Health sheet as fallback demo), PO trimmed
to one narrow vertical slice last. Never cut: burn-in, hardening, rehearsal #2, buffer day.

## Tests

Per-slice unit + Worker spec tests as named above; every new gate/registry/HMAC domain gets a
prove-it-bites test (inject → RED → revert); mandatory live smokes on mirror before merging
anything touching shared infrastructure (`smoke_test_po_generate`, `smoke_test_po_send`, safety
+ progress send smokes around the S5 engine edit, the dashboard 10-item smoke, EICAR for
ClamAV); the S8 golden-sample render-fidelity gate against a real corpus PO.

## Out of scope

RFQ stage (multi-supplier drafting, quote intake via Email Triage, award→PO linkage) ·
Subcontractor contract workstream (~~first post-delivery build~~ — GENERATION built pre-Aug-7 per Amendment A1; SC-S4 send half remains) · GTC attach-at-send +
drawn-signature on POs · material-catalog line-item picking · dashboard guarded-update flow
(§50) + menu-bar mini · full A8 every-function doc coverage · formal Tier-2 Successor-Operator
clearance (named date in handover v10) · boxsdk→box_sdk_gen, Check E, hang-killer (pre-existing
deferrals).

## Verification gates

1. **PO end-to-end on production:** draft → filed (Box + PO_Log + PO_Pending_Review) →
   F22-verified approval → vendor(-stand-in) inbox from procurement@ → SENT stamps everywhere;
   negative paths verified (non-member blocked, tampered HMAC refused, contamination HELD).
2. **Vendor bidirectionality:** portal edit → ITS_Vendors within one cycle; Smartsheet edit →
   portal picker; dirty-row fence preserves in-flight portal edits.
3. **Cutover:** `verify_cutover.py` exits 0 on the production host; two fail-closed send
   smokes; Day-7 routing armed; rollback doc exists.
4. **Dashboard:** panels live over localhost + Tailscale; six §44 actions demonstrated;
   denylist + bites tests green; audit rows in ITS_Errors.
5. **Docs:** 12-PDF set rendered + uploaded; `--check` green; printed set packed.
6. **Render fidelity:** golden-sample gate green (S8) before the first live PO send.

## Done when

The Aug-7 demo arc has been rehearsed twice (Aug 4–5) and executed on-site on the production
system; training drills completed BY the owner/admin; Step-8 acceptance signed with the v10
amendment language; `docs/session_logs/2026-08-07_production-cutover.md` written; Day-7
routing armed.

## Operator-side actions remaining

Day-1 list (Jul 9–10): fire the external requests (M365 admin access · DNS/zone decision ·
Teala recipient + vendor lists · 7 approver Smartsheet accounts confirmed · Box production
account · office network details · Paid-plan confirm) · Aug-7 meeting logistics + supplier-
stand-in mailbox · `gh issue close 338 340` · mirror `progress@` + `procurement@` · Host
Phase A · legal confirmation of the Purchaser entity + 17-clause T&C review. Ongoing: the
operator lane of the master calendar (host flip, production staging, cutover day, rehearsals);
sheet-id flips after builder runs; ITS_Config gate flips at go-live; approver workspace shares.

## Anti-patterns to avoid

- Daemon double-run in any flip window (Box refresh-token rotation is single-consumer) — the
  one-way ordered flip is non-negotiable.
- Editing Python source on the live `~/its` tree — per-task worktrees with own venvs, always.
- Trusting this document over live HEAD — re-verify slice claims before building
  (brief-validator); zero grep hits beat confident memory.
- Silent scope growth — new ideas slot into the post-delivery queue, not into the 17 days.
- Skipping the S5 live smokes because unit tests pass — the send engine is proven-live code;
  mocks structurally cannot catch what the smokes catch.
- Cutting burn-in, the hardening gate, rehearsal #2, or the Aug-6 buffer to recover schedule.
