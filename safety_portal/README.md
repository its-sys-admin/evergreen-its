# ITS Safety Portal

Web portal for Evergreen field PMs to submit daily safety paperwork directly —
replacing the inbox-and-PDF path. Field PMs log in, fill structured forms, capture
signatures on-screen, and (later phases) submit into the existing `safety_reports`
intake pipeline.

- **Planning docs (canonical):** `../../its-blueprint/workstreams/safety-portal/mission.md`
  (v1) + `brief.md`. Decisions Q1–Q10 are locked there.
- **This is the TypeScript / Cloudflare workstream** — it does **not** follow the
  Python `safety_reports/` shape.

> **Phase 2 scaffold (this directory).** Deployable Cloudflare skeleton + a minimal,
> themed, end-to-end slice: **login → home → one hard-coded JHA stub**. No submission,
> no PDF, no email, no Smartsheet — those land in later phases (see
> [What's stubbed](#whats-stubbed--out-of-scope)).

---

## Pending live activation (operator punch-list)

One table, one command block — the consolidated view of which shipped D1 migrations are
applied on the **live** D1 vs still pending. This exists because a Worker deployed ahead of
its migration fail-closes `resolveCapabilities` → the universal-lockout class (2026-06-28).

| Migration | Slice | PR | Applied live? |
|---|---|---|---|
| `0023_manager_role` | P2.6 Manager tier — [section](#manager-tier--third-portal-role-p26--0023) | #398 | ✅ |
| `0024_index_personnel_current_job` | Unified job-create — [section](#unified-job-create-flow--crew-converges-on-placement-0024) | #402 | ✅ |
| `0025_manager_task_assign` | S1 Assigned-Tasks — [section](#assigned-tasks--manager-task-authority-0025--checklist-engine-0026) | #406 | ✅ 2026-07-02 |
| `0026_checklist_engine` | S2 checklist engine — [section](#assigned-tasks--manager-task-authority-0025--checklist-engine-0026) | #407 | ✅ 2026-07-02 |
| `0027_subcontractor_crew_create` | Slice T subcontractor tier — [section](#subcontractor-tier--scoped-crew-create--time-scoping-0027) | #412 | ✅ 2026-07-02 |
| `0028_sop_checklist_content` | SOP content seed — [section](#sop-checklist-content-seed-0028) | #414 | ✅ (R-series deploy) |
| `0029_checklist_instance_template_title` | R1 worker contracts — [section](#assigned-tasks-r1--instance-template-title-0029--worker-contract-fixes) | #416 | ✅ (R-series deploy) |
| `0030_job_daily_requirements` | D4 per-job daily-form requirements — [section](#per-job-daily-form-requirements-d4--0030) | #427 | ☐ pending |
| `0031_job_expected_materials` | M1 expected materials — [section](#expected-materials--per-job-receipt-list-material-receipts-m1--0031) | #426 | ☐ pending |
| `0032_job_daily_requirements_kinds` | D5 requirement kinds (number/date/select) — [section](#requirement-kinds-widened-d5--0032) | #435 | ☐ pending |
| `0033_prune_meta` | GS2 prune observability — [section](#prune-observability-gs2--0033) | #447 | ☐ pending |
| `0034_time_amend_index` | G2.3 crew edit/retire + time amend/void — [section](#crew-editretire--time-amendvoid-g23--0034) | #451 | ☐ pending |
| `0035_task_due_date` | G2.6 task due dates — [section](#task-due-dates--overdue-pills-g26--0035) | #450 | ☐ pending |
| `0036_item_photos` | G1 Slice 1 item-photo capture queue — [section](#checklist-item-photos--capture--pending-queue-g1-slice-1--0036) | #452 | ☐ pending |
| `0037_daily_photo_pool` | daily-report v6: additional site photos (pool) + D.13 incident link — [section](#daily-report-photo-pool--additional-site-photos-v6--0037) | #456 | ☐ pending |
| `0038_time_entries_mirror` | P7 Slice 1 Hours Log up-sync watermark — [section](#hours-log-up-sync-watermark-p7-slice-1--0038) | #461 | ☐ pending |
| `0039_material_list_mirror` | P7 M2 Material List up-sync keys (line_uuid + unplanned) — [section](#material-list-up-sync-keys-p7-m2--0039) | (M2) | ☐ pending |
| `0040_checklist_recurrences` | Recurring checklists per job (#16) — [section](#recurring-checklists-per-job-16--0040) | (#16) | ☐ pending |
| `0041_checklist_completion_emit` | Checklist completion → weekly progress report (#17, Seam A) — [section](#checklist-completion--weekly-progress-report-17-seam-a--0041) | (#17) | ☐ pending |
| `0042_po_vendors` | PO S2 vendor cache + counter — [section](#purchase-orders--d1--worker-po-slice-s2--00420044) | (PO S2) | ☐ pending |
| `0043_purchase_orders` | PO S2 drafts + line items + numbering backstop — [section](#purchase-orders--d1--worker-po-slice-s2--00420044) | (PO S2) | ☐ pending |
| `0044_po_capability` | PO S2 `cap.po.manage` → admin — [section](#purchase-orders--d1--worker-po-slice-s2--00420044) | (PO S2) | ☐ pending |
| `0045_create_config_requests` | Config-editor send-free queue — [section](#config-editor-queue--d1--worker-configts-slice-1--0045) | (config S1) | ☐ pending |
| `0046_config_requests_set_current_op` | Terms make-current: widen `config_requests.op` CHECK for `set_current` — [section](#terms-make-current--layer-a-legal-gate--0046) | (config T2) | ☐ pending |
| `0047_config_requests_cleared_at` | Config status-monitor forensic-safe Clear: `config_requests.cleared_at` | (config F1, #524) | ☐ pending |
| `0048_config_requests_create_profile_op` | New-terms-profile op: widen `config_requests.op` CHECK for `create_profile` | (config F2, #526) | ☐ pending |
| `0049_subcontractors` | Subcontracts S1: `subcontractors` D1 cache (§51 mirror of ITS_Subcontractors) + counter | (SC-S1, #529) | ☐ pending |
| `0050_subcontracts` | Subcontracts S1: `subcontracts` + `sov_lines` (D1-authoritative drafts) | (SC-S1, #529) | ☐ pending |
| `0051_subcontracts_capability` | Subcontracts S1: `cap.subcontracts.manage` → admin | (SC-S1, #529) | ☐ pending |
| `0052_subcontractors_state` | Subcontractors grouped by STATE not region: `subcontractors` region→state rebuild | (SC group-by-state, #533) | ☐ pending |
| `0059_material_tracking` | Materials tracking (PR2): line `part_number`/`category`/`expected_ship_date` + `material_receipt_events` (append-only delivery ledger) + `material_shipments` (scheduled loads) | (PR2) | ☐ pending |
| `0061_gayk_transport_checklists` | GAYK/DOYLE pile-driver transport inspection-library seed — two `generic_inspection` templates (Start-Up, Loading & Securing). CONTENT-ONLY: no schema change, no new routes, so the already-deployed Worker renders these rows exactly like the `0028` ones | (#44) | ✅ 2026-08-10 |
| `0062_gayk_weekly_maintenance_checklist` | GAYK/DOYLE pile-driver **weekly maintenance** inspection-library seed — one `generic_inspection` template, 9 duties. CONTENT-ONLY, same shape as `0061`. Seeds NO recurrence row: cadence is a per-job/per-person operator assignment | (#45) | ☐ pending |

> ⚠️ **This table is behind the migrations directory: `0053`–`0058` and `0060` shipped but were
> never listed here.** Pre-existing drift, not introduced by `0059` or `0061`. It is a
> DOCUMENTATION gap, not an apply hazard — the command below is `migrations apply`, which applies
> everything pending in order regardless of what this table says. Worth a reconciling pass.

Canonical apply-and-deploy sequence (applies **all** pending migrations, in order — never a
subset):

```bash
cd ~/its && git pull origin main   # ALWAYS first — the stale-migrations-list lockout class
cd safety_portal
npx wrangler d1 migrations apply its-safety-portal-db --remote
npm run deploy
```

Each linked per-slice **Activation** section carries that slice's post-deploy live smoke.

> **Convention:** every future slice that ships a migration adds one row here (unchecked) in
> the same PR; the operator flips it to ✅ (with the date) at cutover. Rows older than `0023`
> predate this table and are all long since applied — see the per-slice sections below.

---

## Architecture

A **single Cloudflare Worker** serves the built React SPA (static assets) **and**
handles same-origin `/api/*` routes — zero CORS, one deployment unit.

| Layer | Tech |
|---|---|
| Frontend | Vite + React 19 (`src/`, `index.html`) |
| Backend | Cloudflare Worker via **Hono** (`worker/`) |
| Bundler | `@cloudflare/vite-plugin` (runs the Worker in dev, builds both for deploy) |
| Auth | D1 `users` table + `bcryptjs` (cost 10); HMAC-signed session cookie |
| Database | Cloudflare D1 (`migrations/`) |
| PDF storage | **Box** (system of record). No R2 — under Option-B render the Worker never holds a PDF; `intake.py` renders + stores it in Box. |

> **Deploy target (historical note):** live as a **Workers + Static Assets** deploy at
> `https://safety.evergreenmirror.com` since 2026-06-08 (the pre-first-deploy Workers-vs-Pages
> reconciliation resolved to Workers — Pages is in maintenance mode). Note `custom_domain: true`
> in `wrangler.jsonc` disables the `*.workers.dev` URL on deploy (error 1042).

---

## Layout

```
safety_portal/
  index.html              # SPA entry (Vite root)
  src/                    # React SPA (client)
    pages/                # LoginPage, HomePage, JhaStubPage
    components/           # AppHeader, SignaturePad (SVG-vector capture)
    lib/                  # api.ts (fetch wrappers), auth.tsx (AuthContext)
    styles/               # tokens.css (design system), global.css
  worker/                 # Cloudflare Worker (Hono): /api/login, /api/session, /api/logout
  migrations/             # D1 schema + seed (0001 users, 0002 validation user)
  public/                 # static assets (evergreen-logo.svg)
  reference_forms/        # the 10 source PDFs — Phase-4 source-of-truth (see its README)
  wrangler.jsonc          # Worker + assets + D1 bindings (NO secrets; no R2 — PDFs live in Box)
  vite.config.ts · package.json · tsconfig*.json
  .dev.vars.example       # local secret template (copy to .dev.vars, gitignored)
```

---

## Local development (no Cloudflare token required)

`vite dev` / `wrangler dev` run fully on Miniflare with D1 simulated locally —
**no Cloudflare account or token needed.**

```bash
cd safety_portal
npm install

# 1. Local signing secret (gitignored)
cp .dev.vars.example .dev.vars
#    then put a real value in it:  openssl rand -base64 48

# 2. Apply migrations to the LOCAL D1 (creates users + seeds the validation user)
npm run db:migrate:local

# 3. Run it
npm run dev            # Vite dev server (HMR) — http://localhost:5173
#   …or a production-like local serve of the built Worker + assets:
npm run build && npx wrangler dev --local   # http://localhost:8787
```

### Seeded validation credential (local / validation only)

```
username:  test.pm
password:  portal-dev-2026
```

This is a **throwaway, documented dev credential** seeded by `migrations/0002`. It
unlocks only a local/validation D1 that does not exist in production. **Do not apply
`0002` to production** — real field PMs are provisioned via the Phase 7 admin route.

### Useful scripts

```bash
npm run typecheck       # tsc for client + worker (strict)
npm run build           # vite build -> dist/client (SPA) + dist/<name> (Worker)
npm run db:query:local "SELECT * FROM users;"
```

---

## Deploy (operator — requires CLOUDFLARE_API_TOKEN)

Deferred this session (built + validated locally first). When ready, with a token
scoped to **Workers + D1 edit** exported as `CLOUDFLARE_API_TOKEN`
(+ `CLOUDFLARE_ACCOUNT_ID`):

```bash
cd safety_portal
npx wrangler d1 create its-safety-portal-db          # -> paste database_id into wrangler.jsonc
npx wrangler d1 migrations apply its-safety-portal-db --remote   # users + seed (validation env)
npx wrangler secret put SESSION_SIGNING_SECRET       # paste `openssl rand -base64 48`
npm run deploy                                       # vite build && wrangler deploy
#   -> https://its-safety-portal.<account>.workers.dev
```

Then attach the custom domain `safety.evergreenmirror.com` (dashboard / `routes`) —
see [the reconciliation note](#deploy-target-workers-static-assets-vs-pages-reconciliation).

> **Plan caveat (bcryptjs):** a cost-10 `bcrypt.compare` can exceed the Workers **Free**
> plan's 10 ms CPU limit (Error 1102). The deployed Worker must be on the **Paid** plan,
> or swap `worker/auth.ts` to Web-Crypto **PBKDF2-SHA-256 @100k iters** (the documented
> Workers-constrained substitute for bcrypt). Honoring the mission's literal "bcrypt cost
> 10" is why bcryptjs is used here.

### Production hardening — operator cutover steps (Part-A findings)

The in-code hardening (**A1** idempotent submit id, **A3** daily D1 prune cron) ships in the
Worker and needs no operator action. Two items require the Cloudflare **dashboard/account** at
cutover — do them on the production account (a fresh account defaults to the **Free** plan):

- **A5 — Workers plan go/no-go (BLOCKER).** `/api/login` runs `bcrypt.compare` at cost 10,
  which can exceed the Workers **Free** 10 ms CPU cap (Error 1102) → a total login outage.
  **Confirm the production Worker is on the Workers Paid plan before go-live.** If Paid is not
  available, the documented Workers-constrained substitute is **PBKDF2-SHA-256 @100k iters** in
  `worker/auth.ts` — but that changes the mission-locked "bcrypt cost 10" parameter and needs a
  password-rehash migration, so it is **developer + doctrine work, not a cutover toggle** (surface
  to Seth; do not swap silently).

- **A2 — rate limiting (add at cutover).** Nothing throttles `/api/login` (brute-force + bcrypt
  CPU-cost amplification) or `/api/*` (unbounded). Add Cloudflare **rate-limiting rules** in the
  dashboard (Security → WAF → Rate limiting rules): a tight rule on `/api/login` (e.g. ~5 req /
  10 s per IP → block ~10 min) and a looser blanket rule on `/api/*`. The in-code alternative is
  the Workers **`ratelimit` binding** (`wrangler.jsonc` + per-route `.limit()`), reproducible +
  testable in-repo — adopt it if it is GA for the account at deploy time; until then the
  dashboard rule is the cutover step (re-create it on any new account).

### Secrets

All secrets are Workers Secrets / `.dev.vars` — **never committed**. Phase 2 needs only
`SESSION_SIGNING_SECRET`. Later phases add `HMAC_PAYLOAD_SECRET`,
`PORTAL_INTERNAL_API_TOKEN` (the poller's bearer), and `PORTAL_ADMIN_API_TOKEN`
(the operator-only admin bearer — **separate** so the poller's token can't provision
users) with macOS Keychain mirrors per ITS convention.

---

## Phase 7 — operator user provisioning + session revocation

Users are **operator-provisioned** (NOT self-service; no user-role model — brief §4).
The operator passes plaintext over a bearer-gated admin channel; the **backend
bcrypt-hashes** (cost 10) before write — plaintext is never stored, returned, or logged.

**Routes** (`/api/internal/admin/*`, gated by `requireAdminToken` = `PORTAL_ADMIN_API_TOKEN`,
which is **separate** from the poller's `PORTAL_INTERNAL_API_TOKEN`):
`POST users` (provision, 409 if exists) · `POST users/reset` · `POST users/disable` ·
`POST users/enable` · `GET users` (no hashes).

**Revocation:** `requireSession` reads `users.disabled` per request (migration 0006) and
401s a disabled/deleted user immediately — fail-closed (a D1 error also → 401). The
cookie stays valid cryptographically, but the lookup gates it.

**Operator CLI** (Mac, not a daemon): `python -m safety_reports.portal_admin <cmd>`
— `add-user <lastname.firstname>` / `reset-password <u>` / `disable-user <u>` /
`enable-user <u>` / `list-users`. Reads the Worker URL from ITS_Config + the admin
bearer from Keychain `ITS_PORTAL_ADMIN_TOKEN`; passwords via `getpass` (confirmed twice).

### Activation punch-list (operator — needs Cloudflare/Keychain auth)

The Worker/admin/migration code sits **inert** in the repo until activated. The
Box-409 fix + sheet-styling (PRs G/I, Python-only) activate on a plain `~/its` pull;
the admin route needs:

1. Set `PORTAL_ADMIN_API_TOKEN` (Worker secret) + `ITS_PORTAL_ADMIN_TOKEN` (Keychain),
   **byte-equal** (`openssl rand -hex 32`; `wrangler secret put` + `security add-generic-password -U -a "$USER" -s ITS_PORTAL_ADMIN_TOKEN -w`).
2. Apply migration **0006** to live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the
   `requireSession` disabled-read errors and 401s every session.
3. **Redeploy** the Worker (`npm run deploy`) — activates the admin routes + revocation.
4. Provision real users: `python -m safety_reports.portal_admin add-user lastname.firstname`.
5. (Optional) custom domain — see PR-J's `wrangler.jsonc` `routes` (dashboard add or `wrangler deploy`).

> **This is the secrets/auth boundary** — review the admin diff before activating.

---

## Admin dashboard (Phase 1 — role model + in-app account management)

Adds an in-browser admin surface for the two admins (CEO + head PM) on top of the
operator CLI above. **Migration 0007** adds `users.role` (`submitter` default | `admin`)
+ an `audit_log` table.

**Role is read fresh from D1 per request** (`requireSession` now `SELECT`s `disabled, role`),
**not** baked into the cookie — a demotion takes effect on the next request (same reasoning
as the per-request `disabled` check). `/api/login` + `/api/session` return the role so the
SPA can show/hide the admin tabs; that is display-only — every admin route is re-gated
server-side by `requireRole("admin")`.

**In-app surface** (`/api/admin/*`, gated by `requireSession` + `requireRole("admin")` —
SESSION+role, distinct from the bearer `/api/internal/admin/*`): `GET users` ·
`POST users` (create, role selectable) · `POST users/credentials` (edit username/password —
self-edit clears the cookie → re-login) · `POST users/role` (change role) ·
`POST users/delete`. Each mutation + its `audit_log` row run in one atomic D1 batch.

**Last-admin guard** (operator's call, ON): the session routes refuse to demote / delete the
**only enabled admin** (`409 last_admin`). The bearer operator routes are deliberately **NOT**
guarded — they are the break-glass path *out* of a zero-admin lockout (see below).

**Tab 1 "filled out as" (submit-as)** is a separate later slice — not in this PR.

**CLI:** `portal_admin add-user <u> --role admin` bootstraps an admin; `portal_admin set-role
<u> submitter|admin` is break-glass for the role model.

### Activation (operator — needs Cloudflare/Keychain auth; on the LIVE portal)

Mirrors the Phase-7 punch-list. The `worker_base_url` already points at the custom domain —
do **not** re-point.

1. Apply migration **0007** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the
   `requireSession` `role`-read errors and (fail-closed) 401s every session. **ORDER-CRITICAL**,
   same rule as 0006.
2. **Redeploy** (`npm run deploy`).
3. **Regression-check the LIVE portal:** existing users still log in + submit (role defaults
   `submitter`; existing accounts keep access; the admin routes are additive). Do not regress.
4. Provision the two admins:
   `portal_admin add-user stephens.jacob --role admin` and `… finkhousen.ben --role admin`
   (password = username at provision; no forced change).

> **This is the secrets/auth + impersonation boundary** — review the diff before activating.

### Session revocation (slice 8a — `users.session_epoch`, deferred audit #7)

Real logout / password-change revocation. **Migration 0009** adds `users.session_epoch`
(monotonic counter, `DEFAULT 0`); the epoch is snapshotted into the session cookie at login
and re-read per request in `requireSession` (folded into the same `disabled + role` SELECT).
A cookie whose epoch is **behind** the DB epoch is rejected (`401 revoked`); **logout** and
**password-change** (both the bearer reset and the in-app credentials route) increment the
column, so an outstanding/captured cookie dies on its next request. A pre-#7 cookie (no epoch
claim) is treated as `0`, so existing sessions survive the migration.

#### Activation (operator — secrets/auth boundary; escalates to the Developer-Operator)

1. Apply migration **0009** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the
   `requireSession` `session_epoch`-read errors and (fail-closed) 401s every session.
   **ORDER-CRITICAL**, same rule as 0006/0007.
2. **Redeploy** (`npm run deploy`) — activates the epoch check + the logout / password bumps.
3. **Regression-check the LIVE portal:** existing users still log in + submit (pre-#7 cookies
   survive; epoch defaults `0`); after a logout, re-using the old cookie is rejected (`401`).

> Out of scope here: the admin 30-minute idle timeout (slice 8b).

### Form editor publish queue (slice 3a — `publish_requests`)

**Migration 0010** adds the send-free `publish_requests` queue. The admin Forms editor's
Publish calls `POST /api/admin/publish`, which VALIDATES the composed definition
server-side (closed vocabulary + reserved-key denylist + cross-section-unique keys +
hard bounds) and, only if valid, ENQUEUES a row — it never commits or deploys. The Mac
publish daemon (slice 3b) is the sole privileged actuator (mirrors the External Send
Gate). `GET /api/admin/publish-status` is the monitor read view.

#### Activation (operator — secrets/auth + deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0010** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else
   `/api/admin/publish` errors on the missing table. **ORDER-CRITICAL**, same rule as 0006/0007/0009.
2. **Redeploy** (`npm run deploy`) — activates the enqueue + status routes (still inert for
   PMs without the Mac publish daemon, which is the privileged actuator that lands forms).

> Out of scope here: the Mac publish daemon (slice 3b) + the editor UI (slices 4–6).

### Request-driven canonical PDF download (PR-4 Part A — `0011`)

**Migration 0011** adds the PDF-cache columns on `submissions` (`pdf_requested`,
`box_file_id`, `pdf_ready_at`) + the `filed_pdfs` chunk table. A field PM (or the
attributee / an admin) clicks "Make available for download"; `POST
/api/submissions/:uuid/request-pdf` sets `pdf_requested=1`. The Worker holds NO Box
creds and is SEND-FREE: the Mac-side portal_poll daemon GETs the serviceable set
(`GET /api/internal/pdf-requests`), downloads the filed PDF from Box, base64-chunks it,
and POSTs the chunks (`POST /api/internal/filed-pdf`); `GET /api/submissions/:uuid/pdf`
reassembles the D1 chunks and serves the byte-identical Box copy as an attachment.

> **Superseded by PR-5 (Form Request, `0012`) below.** As of PR-5 the access model is
> **requester-bound, not ownership-gated**: a non-admin must hold a live `pdf_requests`
> row for *this* account to download (a different account — even the actor/attributee —
> gets **404**), and the prune is **two-stage** (strip payload at 90d, delete the row 30d
> after the job goes inactive; chunks evicted when no live request references them) rather
> than a flat 24h-from-`pdf_ready_at` sweep. The PR-5 section is the single source of truth.

#### Activation (operator — secrets/auth + deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0011** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the new
   PDF routes error on the missing columns/table. **ORDER-CRITICAL**, same rule as
   0006/0007/0009/0010.
2. **Redeploy** (`npm run deploy`) — activates the request/status/pdf + internal
   pdf-requests/filed-pdf routes (still inert until the Mac portal_poll PDF-cache pass
   runs, which is what populates the chunks).

> Out of scope here: the Mac portal_poll PDF-cache pass + the SPA download button (sibling surfaces).

### Form Request browse + requester-bound PDF (PR-5 — `0012`)

**Migration 0012** adds the `pdf_requests` table (one row per `(submission_uuid, account)` —
downloads are **requester-bound**, 24h). PR-5 adds the in-portal "Form Request" flow: any
authenticated account browses an **ACTIVE** job's filed forms (`GET /api/filed?job_id=…`) and
batch-requests their PDFs (`POST /api/request-pdfs`, ≤20/batch); each request upserts a
`pdf_requests` row, the Mac PDF-cache pass services only forms with a **live** request
(`GET /api/internal/pdf-requests` now requires one), and `GET /api/submissions/:uuid/pdf` is
re-gated so only the **requesting** account (or an admin) may download within 24h — a different
account, even the original submitter, gets **404** (no enumeration). Prune is now two-stage:
strip `payload_json` at 90d (keep the metadata row so a filed form stays browseable while its job
is active), delete the row 30d after the job goes **inactive**; `pdf_requests` expire at 24h and
their chunks are evicted. The Worker remains SEND-FREE (no Box creds, no egress).

#### Activation (operator — secrets/auth + deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0012** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else `/api/filed`,
   `/api/request-pdfs`, the requester-bound `/status` + `/pdf`, and the updated
   `/api/internal/pdf-requests` error on the missing `pdf_requests` table. **ORDER-CRITICAL**,
   same rule as 0006/0007/0009/0010/0011.
2. **Redeploy** (`npm run deploy`) — activates the Form Request routes + the requester-bound
   re-gate. The Mac portal_poll PDF-cache pass already services any live request.

> Out of scope here: the Mac portal_poll PDF-cache pass (sibling surface, unchanged — it reads
> `GET /api/internal/pdf-requests`, which now returns only live-requested forms).

### Field-Ops schema + split-brain fence (P2.1 — `0014`–`0017`)

**Migrations 0014–0016** port the URS-Marine field-ops tables into D1 (clients, personnel,
equipment, task_assignments, equipment_location, time_entries, inspections, equipment_logs +
additive ALTERs to `jobs`/`equipment`). **Migration 0017** adds `jobs.origin` / `sync_state` /
`canonical_job_id` — the split-brain fence: a portal-CREATED job (`origin='portal'`) must NOT be
deactivated by the Smartsheet down-sync (`/api/internal/sync`), which only deactivates
`origin='smartsheet'` jobs absent from the payload. The field-ops integrity-bar tables
(`time_entries`/`task_assignments`/`inspections`) are D1-PRIMARY operational SoR, so `prune` now
protects any job holding them from deletion.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migrations **0014–0017** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the scoped
   `/api/internal/sync` deactivation 500s on the missing `origin` column and the field-ops-aware
   `prune` 500s on the missing tables. **ORDER-CRITICAL**, same rule as 0006/0007/0009/0010/0011/0012.
2. **Redeploy** (`npm run deploy`) — activates the origin-scoped down-sync + the field-ops-aware prune.

> The field-ops READ/WRITE routes + the Mac mirror daemon (`field_ops/fieldops_sync`) that promotes
> portal jobs into `ITS_Active_Jobs` land in later P2 slices — these migrations are inert until then.

### P3 Materials catalog (M1 — `0019`)

**Migration 0019** adds the `material_catalog` table — the datasheet-backed material TYPE vocabulary
(36 operator-approved types seeded inline) the per-job Material List draws from (manifest model, M2).
A plain reference table: admin CRUD gated `cap.materials.manage`; retire is a soft-delete (`active=0`)
so a receipt/incident referencing a `catalog_id` keeps its target. Read gated `cap.materials.receive`.
Both capabilities are already seeded in 0013 — 0019 seeds no capability vocabulary.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0019** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else `GET /api/fieldops/materials`,
   `POST /api/fieldops/material`, `…/:id/update`, `…/:id/delete` 500 on the missing `material_catalog`
   table. **ORDER-CRITICAL**, same rule as 0013/0015/0016.
2. **Redeploy** (`npm run deploy`) — activates the catalog CRUD routes + the Materials admin page.

### Form workflow selector (Phase-2 — `0020`)

**Migration 0020** rebuilds `publish_requests` to add a `category` column and extend the `op`
CHECK with the new `recategorize` op (the form-builder **workflow selector** — a form's workflow,
today `safety` / `progress`, is chosen at create and changeable afterwards). The registry of valid
workflows is `safety_portal/workflows.json`, read by both the Worker and Python. No FK
dependencies; existing rows carry `category` as NULL.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0020** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else `POST /api/admin/publish`,
   `GET /api/admin/publish-request`, and `POST /api/internal/publish/claim` 500 on the missing
   `category` column (the INSERT + both SELECTs name it). **ORDER-CRITICAL**, same rule as 0010.
   (Always `git pull` `~/its` to latest `main` BEFORE `wrangler d1 migrations apply` — the
   stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the `recategorize` op + the Workflow selector in the
   Forms editor.

### Manager tier — third portal role (P2.6 — `0023`)

**Migration 0023** seeds a third role `manager` (crew lead), a new capability `cap.crew.assign`
(granted to `manager` + `admin`), the manager's 11 grants (submitter's 8 + `cap.personnel.read` +
`cap.personnel.manage` + `cap.crew.assign`), and adds `personnel.current_job` (the crew→job
placement). The role is a pure INSERT — migration 0013 already replaced 0007's role CHECK with an FK
to `roles(key)`, so seeding the role satisfies it (no `users` rebuild). New Worker route
`POST /api/fieldops/personnel/:id/assign` (cap.crew.assign). See `docs/runbooks/manager_tier.md`
(§43) + `docs/enablement/manager_tier.md` (§6/A8).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0023** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else a user set to
   `manager` resolves to the EMPTY capability set (fail-closed) → blank tabs / 401, and
   `POST /api/fieldops/personnel/:id/assign` 500s on the missing `personnel.current_job` column.
   **ORDER-CRITICAL**, same rule as 0013/0020. (Always `git pull` `~/its` to latest `main` BEFORE
   `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the `manager` role vocabulary, the Accounts 3-way
   role control, the crew-assign route, and the Personnel "Assign" control.
3. **Smoke** (`wrangler dev` or live): set a user to `manager` (Accounts page or
   `portal_admin set-role <u> manager`); confirm they see Personnel + can assign crew (201), but
   get 403 on job-create / task-create / login-mint, and cannot open the admin dashboard.

### Unified job-create flow — crew converges on placement (`0024`)

**Migration 0024** adds `idx_personnel_current_job` on `personnel(current_job)`. This backs the
**crew-convergence** change: a job's "crew" (both the Job Tracker LIST card and the DETAIL view)
now MEANS the people currently **placed** on it (`personnel.current_job`, from 0023), NOT the
distinct assignees of its `task_assignments`. The Job Tracker detail view gains reusable
**Assign crew** (`cap.crew.assign`) and **Assign equipment** (`cap.equipment.field`) controls, and
creating a job routes into its detail with a "finish setting up" nudge — all reusing the already
security-reviewed `assign` / equipment-`location` routes (no new routes).

**SEMANTICS SHIFT (call out to the operator):** after this deploys, an existing job that had
task-assignment "crew" but nobody *placed* on it shows an EMPTY crew list until someone is placed
(via the new Assign-crew control or the Personnel page). No data is lost — those task assignments
still appear in the job's TASKS list with their assignee. This is the intended convergence.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0024** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`). The index is additive and
   `IF NOT EXISTS`, so a stale deploy won't hard-fail (the crew query is correct without the index,
   just slower); still apply-before-deploy per the standing rule. (Always `git pull` `~/its` to
   latest `main` BEFORE `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the crew-convergence query + the detail-view
   Assign-crew / Assign-equipment controls + the create nudge.
3. **Smoke** (live): create a job → assign a person + a piece of equipment + a task → all three show
   on the job; the person's "Placed on" (Personnel page) shows the job.

### Assigned-Tasks — manager task authority (`0025`) + checklist engine (`0026`)

**Migration 0025** grants the `manager` role `cap.tasks.assign` — managers can now create / assign /
complete tasks (only to subcontractor-role accounts, guarded), and the task create/reassign routes gate
on `cap.jobtracker.manage` OR `cap.tasks.assign`. **Migration 0026** adds the checklist-engine tables
(`checklist_templates` / `checklist_items` / `checklist_instances` / `checklist_item_states`) + seeds the
`daily_default` template from `daily-report-v1.json`; the admin per-job checklist editor + template routes
read them (`cap.checklist.manage`, admin-only).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migrations **0025 then 0026** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`). 0026's tables are read by every
   checklist route (`GET /api/fieldops/checklist/*`), so a premature deploy 500s them; 0025 grants the
   manager cap that the re-gated task routes accept. Both are additive + guarded (`IF NOT EXISTS` +
   NOT-EXISTS-guarded seed), so a stale re-apply is safe. (Always `git pull` `~/its` to latest `main`
   BEFORE `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the "My Tasks" tab, the manager task controls, and the
   admin Daily-checklist editor on the Job Tracker job detail.
3. **Smoke** (live): a manager creates/assigns a task to a subcontractor (not to an admin/manager);
   the "My Tasks" tab shows a user's assigned tasks; an admin edits the default checklist + adds/removes
   a per-job item on a job's detail.

_Historical note (D2, 2026-07): the daily-checklist SPA surfaces described above were retired by the
SOP daily form (see "SOP daily form — the Daily tab IS the form" below); the engine + tables stay for
assigned inspections._

### Subcontractor tier — scoped crew-create + time scoping (`0027`)

**Migration 0027** adds one capability `cap.crew.create` (granted to `submitter` + `admin`) and the
`personnel.created_by` provenance column. The `submitter` tier is re-presented to users as
**"Subcontractor"** — a **DISPLAY-LABEL-ONLY** rename: the role **KEY stays `'submitter'`** (the
security-load-bearing fail-safe default in `worker/auth.ts` — "unknown → submitter, never upward"), so
NO role/vocabulary row changes and the grant matrix is preserved. A subcontractor keeps all 8 of its
0013 caps + gains `cap.crew.create`. New Worker route `POST /api/fieldops/crew` (`cap.crew.create`)
creates a **NON-LOGIN** roster person auto-placed on the ACTOR's own current job (`created_by` stamped;
422 `not_placed` if the actor isn't placed; any account/login payload → 400 `login_not_allowed`).
`GET /api/fieldops/crew/mine` backs the subcontractor time-log picker. The time-entry route now SCOPES a
subcontractor (`cap.time.log` WITHOUT `cap.personnel.manage`) to logging only for their OWN linked
personnel OR a person they created (`created_by = them`) → else 403 `forbidden_personnel`;
managers/admins stay unrestricted. See `docs/runbooks/subcontractor_tier.md` (§43) +
`docs/enablement/subcontractor_tier.md` (§6/A8).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0027** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else `POST /api/fieldops/crew`
   403s every caller (fail-closed empty cap) and the crew-create INSERT + the time-scoping SELECT 500
   on the missing `created_by` column. **ORDER-CRITICAL**, same rule as 0013/0023/0025. (Always
   `git pull` `~/its` to latest `main` BEFORE `wrangler d1 migrations apply` — the stale-migrations-list
   lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the "Subcontractor" display label, the My-Tasks
   Add-crew control, the scoped crew-create route, and the subcontractor time-log picker/scoping.
3. **Smoke** (live): set a user to `submitter`, place them on a job (Personnel → Assign, or a manager
   places them); they see **Add crew** on My Tasks → add a field-only helper (lands on their job); the
   helper appears in their time-log "For" picker; logging time for a stranger they didn't create is
   refused (403). An unplaced subcontractor gets a "must be placed on a job" message. A manager/admin is
   unaffected (full job-crew picker, no scoping).

### SOP checklist content seed (`0028`)

**Migration 0028** is CONTENT-ONLY (no schema change, no new routes): it replaces the migration-0026
placeholder `daily_default` items with the **13-item Site-Supervisor-SOP daily checklist**
(`Site_Supervisor_SOP 2.docx` — incl. the count-50 site-photos item, the count-2 CM check-ins item,
and the two form_linked items: `jha` + the `Daily Field Report filed` capstone) and seeds the S6
inspection library with **6 `generic_inspection` templates** from the ER Safety Manual
(Box 2265234453251): Excavation/Trench, Scaffold, Crane & Rigging, Aerial Lift/MEWP,
Ladder & Fall-Gear, Hot-Work/Welding. Guarded + idempotent: the delete+reseed runs only while the
`daily_default` lacks the `'Daily Field Report filed'` sentinel item, and every INSERT is
NOT-EXISTS-guarded (an admin-created same-title library template is never duplicated).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0028** to the live D1
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) **BEFORE** the next redeploy
   per the standing rule — though this one is **LOW-RISK either order** (content-only: the deployed
   Worker renders the new rows exactly like the old ones). (Always `git pull` `~/its` to latest
   `main` BEFORE `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **No redeploy required** — the change is data. Notes:
   - **Already-generated daily instances keep their snapshot** (S3 snapshots items at generation);
     the new 13-item default takes effect on the **next day's roll** of each manager's checklist.
   - **Per-job overrides authored against the OLD placeholder items are cleared** by the migration's
     orphan-marker cleanup (suppression markers pointing at deleted default item ids); per-job ADDED
     items survive. Re-author any wanted suppressions against the new items via the Job Tracker
     checklist editor.
3. **Smoke** (live): admin → Job Tracker → a job's Daily-checklist editor shows the 13 SOP items in
   order (photos item target 50, check-ins target 2); the inspection library lists the 6 seeded
   templates; a placed manager's next-day "My Tasks" daily checklist rolls the new items.

_Historical note (D2, 2026-07): the 0028 daily_default rows are now DORMANT — the SOP content lives in
the `daily-report-v2` form definition and the Daily tab renders it as a form (see "SOP daily form —
the Daily tab IS the form" below). The 6 inspection-library templates stay live._

### Assigned-Tasks R1 — instance template title (`0029`) + worker contract fixes

**Migration 0029** adds `checklist_instances.template_title` — the assigned inspection template's
title, SNAPSHOTTED at assign time (same lineage rule as the item snapshot: renaming/deleting the
library template never mutates an in-flight instance) — and best-effort BACKFILLS existing
`kind='inspection'` instances through the item-snapshot lineage (`source_item_id` →
`checklist_items.template_id` → `checklist_templates.title`); instances whose lineage no longer
resolves stay NULL (the UI falls back to "Inspection #id"). Ships with the R1 worker contract pass:
task-status ownership guard (403 `forbidden_task`), open-first list ordering, assign-time
validation (`empty_template` / `job_and_date_required` / catalog-checked `unknown_form_code`),
below-target acknowledge (`note_required`), `/checklist/mine` reason codes + `/tasks/mine` `linked`,
Q3 on-or-before due-date reconcile for inspections, `filed_by`/`rolled_up_by` attribution, and
required-bounded time-entry hours (422 `invalid_hours`).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0029** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else `POST
   /api/fieldops/checklist/assign` and `GET /api/fieldops/checklist/assigned` 500 on the missing
   `template_title` column. **ORDER-CRITICAL**, same rule as 0026. (Always `git pull` `~/its` to
   latest `main` BEFORE `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the R1 contract (ownership guard, ordering, assign
   validation, reasons/attribution fields, hours bounds).
3. **Smoke** (live): assign an inspection → the assignee's My Tasks card shows the template's title;
   a subcontractor flipping another person's task gets a permission message; a time entry without
   hours is refused.

### SOP daily form — the Daily tab IS the form (D1 `daily-report-v2` + D2 — no migration)

**No migration.** D1 shipped the `daily-report-v2` definition (the full Site-Supervisor SOP as
`guidance`/`form_link` sections with the DFR data fields interleaved) + catalog bump +
`launch:"daily-tab"`. **D2** makes the My-Tasks **Daily tab the form itself**: date selector
(Pacific today default, past dates show the filed state first) + the v2 form rendered inline
(job from the manager's placement via the Job Tracker viewer data; crew/equipment/prepared_by
prefilled best-effort from the job detail) + `form_link` deep-links riding the existing openForm
machinery with live "Filed ✓ \<time> by \<name>" indicators from the NEW read-only endpoint
`GET /api/fieldops/daily-form/status?job_id&date` (`cap.tasks.own`; latest submission per parent
family via the S4 family match; display-name-only attribution). Filing goes through the unchanged
send-free `/api/submit` → Mac intake → weekly packet.

**RETIRED SPA surfaces (D2):** the R2 checkbox daily checklist (DailyChecklistSection), the admin
"Default daily checklist" editor (Checklists page — now inspections-only), the Job-Tracker per-job
Daily-checklist editor + its cross-link, and the Daily Report's entry in the Submit-a-Form CREATE
picker (`launch:"daily-tab"` parents are hidden there; Form Request / download / history surfaces
untouched). **The checklist ENGINE stays** (assigned inspections use it; §14/§49). *Update
2026-07-03 (operator-approved, B3):* the two dead daily-generation Worker routes — `GET
/checklist/mine` (which still WROTE daily instances + snapshots when called) and `GET
/checklist/mine/rollup-draft` — were **deleted**, along with the dead job-write back-compat routes
`POST /job/:id/close` (the `/lifecycle` route is the live close path) and `POST /job/:id/progress`
(nothing displayed the value since #403). Tombstones at each site in
`worker/fieldops_checklist.ts` / `worker/fieldops_job_write.ts`; handlers recoverable from git
history. The template-editor routes, inspection engine (assign/assigned/instances/cancel/
item-state), and their tables are untouched. Daily content edits now happen in the
**form definition** via the form builder / publish pipeline.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. **No D1 migration** — the `checklist_templates` `daily_default` rows (0026/0028) stay in place,
   dormant for the daily flow (inspections keep their tables live).
2. **Redeploy** (`npm run deploy`) — activates the status endpoint + the rebuilt Daily tab + the
   retirements in one step (SPA assets + Worker deploy together).
3. **Smoke** (live): a placed manager's My Tasks → Daily report shows the date selector + the SOP
   form with crew/equipment prefilled; "Create Job Hazard Analysis" opens the JHA prefilled and,
   after filing it, the button shows "Filed ✓" on return; submitting the daily report flips the
   "Daily report filed ✓" banner; the Submit-a-Form picker no longer lists Daily Field Report; the
   Checklists page shows no "Default daily checklist" area; a Job Tracker job detail shows no
   Daily-checklist editor; an assigned inspection still renders and auto-checks.

### Per-job daily-form requirements (D4 — `0030`)

**Migration 0030** adds `job_daily_requirements` — the admin-authored **additive overlay** of
per-job requirement items (kinds: `note` / `confirm` / `text` / `form_link`) that the portal
fetches at render time and injects into the daily form's `job_requirements` section. Answers
file WITH the submission (`values.job_requirements`, self-describing), so filed PDFs stay
stable regardless of later requirement edits. Authoring is `cap.checklist.manage` (admin);
the tab read (`GET /api/fieldops/daily-form/requirements`) is `cap.tasks.own` with the same
per-job ownership scope as `/api/fieldops/daily-form/status`. Full detail in the migration
header + `docs/runbooks/fieldops_checklists.md` (§ per-job daily-form requirements).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0030** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the
   daily-requirements routes 500 on the missing `job_daily_requirements` table.
   **ORDER-CRITICAL**, same rule as 0026. (Always `git pull` `~/its` to latest `main` BEFORE
   `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the requirements routes + the admin editor +
   the `job_requirements` section in the daily form (SPA + Worker deploy together).
3. **Smoke** (live): an admin adds a requirement to a job (Job Tracker job detail); a manager
   placed on that job sees it rendered in the Daily tab's form and their answer files with the
   submission; a manager on another job does NOT see it.

### Requirement kinds widened (D5 — `0032`)

**Migration 0032** rebuilds `job_daily_requirements` (SQLite can't widen a CHECK in place — the
`0020` rebuild precedent) to extend the requirement-kind vocabulary from four to seven:
`note` / `confirm` / `text` / `form_link` plus **`number`** (numeric answer), **`date`**
(calendar-date answer), and **`select`** (pick-one from an admin-authored option list — the new
`options` column, a JSON array bounded route-side to 1–20 non-empty choices of ≤120 chars;
NULL for every other kind). Existing rows are preserved (options copied NULL). Answers still
file as the self-describing `values.job_requirements = [{label, kind, response}]` strings, so
the filed-PDF rendering (generic label→response rows) needed **no change**. Photo is
deliberately excluded — an untrusted image upload needs the §34 image-class screening design
first (see `docs/tech_debt.md`, "Checklist item-state photo CAPTURE"). Full detail in the
migration header + `docs/runbooks/fieldops_checklists.md` (Symptom F).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migrations to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — the one command applies
   **`0030` then `0031` then `0032` in sequence** (none are live yet; 0030 creates the 4-kind
   table and 0032 immediately rebuilds it to the 7-kind + `options` shape — zero rows to copy).
   **ORDER-CRITICAL**, same rule as 0026: a Worker serving the new kinds against a pre-0032
   table 500s on the missing `options` column. (Always `git pull` `~/its` to latest `main`
   BEFORE `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the 7-kind validation + the options editor +
   the number/date/select rendering (SPA + Worker deploy together).
3. **Smoke** (live): an admin adds a **Choice** requirement with two options to a job (Job
   Tracker job detail) plus a **Number** and a **Date** one; a manager placed on that job sees
   a dropdown / numeric input / date picker in the Daily tab's "Job-specific requirements",
   answers all three, files — the filed PDF shows the three label → answer rows.

### Expected materials — per-job receipt list (Material receipts M1 — `0031`)

**Migration 0031** adds `job_expected_materials` — the per-job list of what materials a job is
expecting (recorded by the office at job creation or as the job develops), which managers later
confirm receipt against. One row per expected arrival: catalog-picked (`material_id` → the 0019
`material_catalog`, validated ACTIVE at write) or free-text (`description` required);
qty/unit/expected-date; `status ∈ expected|received|incident` with `received_at`/`received_by`
stamped by the receive/flag routes (`received_by` stores the account username; reads resolve the
personnel **display name only** — W9). Expectation CRUD is `cap.materials.manage` (the Job
Tracker job-detail "Expected materials" section); the read + `POST …/:id/receive` /
`…/:id/flag-incident` are `cap.materials.receive` with the **per-job ownership scope** (a
non-admin only touches the job they're placed on). Both capabilities were already seeded
(0013/0023) — 0031 seeds no capability vocabulary. The receive/flag routes are wired into the
daily form (D.13 deliveries) + the material-incident form in **M2**; in M1 the admin section +
read surface carry the state. *(Numbering: `0030` belongs to the D4 slice, built in parallel —
both are additive, so apply order is safe.)*

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0031** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else
   `GET /api/fieldops/expected-materials`, `POST /api/fieldops/expected-material` (+ `…/update`,
   `…/seq`, `…/delete`, `…/receive`, `…/flag-incident`) 500 on the missing
   `job_expected_materials` table. **ORDER-CRITICAL**, same rule as 0019. (Always `git pull`
   `~/its` to latest `main` BEFORE `wrangler d1 migrations apply` — the stale-migrations-list
   lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the expected-materials routes + the Job Tracker
   "Expected materials" section (SPA + Worker deploy together).
3. **Smoke** (live): an admin opens a job's detail → "Expected materials" → adds one from the
   catalog and one free-text; a manager placed on that job sees the read-only list (and a manager
   on another job does NOT — 403 `forbidden_job` in the network tab); the Materials Catalog page
   shows the cross-note pointing at the Job Tracker.

### Prune observability (GS2 — `0033`)

**Migration 0033** adds `prune_meta` — the one-row heartbeat the daily scheduled D1 prune
UPSERTs after **every** run (`last_run_at`, sampled `db_size_bytes`, the 6 GB `size_warn`
condition, per-stage delete counters, and `failed_stages` — the stage names whose fenced
try/catch caught a throw). This closes the unbounded-growth audit's #4 time bomb: the prune
cron was a single point of *silent* failure (one throw mid-sequence skipped every later
retention stage forever; success was a `console.log` nobody tails), and a dead prune at
20×20 scale is a 10 GB D1 wall → every INSERT fails → total field-capture outage. Alongside
the heartbeat: each prune stage now runs isolated (a throw is recorded, later stages still
run), terminal `publish_requests` rows (`archived`/`failed`) are pruned 90 d after their
terminal stamp (blob hygiene), and `checklist_instances` + `equipment_location` join the
jobs-delete guard (an inactive job holding either is never deleted; their nullable `job_id`
is `IS NOT NULL`-filtered so a NULL row can't poison the `NOT IN`). The Mac watchdog's
**Check V** reads `GET /api/internal/prune-status` (bearer: the internal token tier, same as
`/api/internal/pending`) and pages: **CRITICAL** on `failed_stages` non-empty or
`db_size_bytes` > 6 GB; **WARN** on `last_run_at` > 48 h stale or an absent meta row.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0033** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the scheduled
   prune's meta write fails (fenced — the prune itself still runs) and
   `GET /api/internal/prune-status` 500s, which Check V reports as "unreachable" instead of
   real health. (Always `git pull` `~/its` to latest `main` BEFORE
   `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`) — activates the stage-isolated prune + meta write + the
   prune-status route.
3. **Smoke** (live): `curl -H "Authorization: Bearer $ITS_PORTAL_INTERNAL_TOKEN"
   https://safety.evergreenmirror.com/api/internal/prune-status` → `{"prune":null}` (or the
   last run's record); after the next 09:00 UTC cron (or a `wrangler` triggered scheduled
   test), the same call returns `last_run_at` + counters with `failed_stages: []`; the next
   morning's watchdog run logs Check V INFO "D1 prune healthy".

### Task due dates + overdue pills (G2.6 — `0035`)

**Migration 0035** adds `task_assignments.due_date` (nullable `TEXT 'YYYY-MM-DD'`) — the
assigned-tasks flow gains the deadline semantics inspections already have. The task CREATE
route accepts an optional `due_date` (regex-validated, the checklist `DUE_DATE_RE` shape); a
**reassign never touches it** (the deadline belongs to the work, not the holder). `/tasks/mine`
and both Job Tracker task legs expose it; within each status band `/tasks/mine` now orders
dated tasks first by `due_date ASC` (overdue → soonest-due), undated last, `created_at DESC`
tiebreak unchanged. The SPA renders `due <date>` + the same Overdue warn pill inspections use
(not-done AND `due_date` < Pacific-today), and the Job Tracker add-task form gains an optional
date input. Historical tasks are not backfilled — no date, no pill, sorted after dated work.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0035** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the task
   create/read routes 500 on the missing column. (Always `git pull` `~/its` to latest `main`
   first — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`).
3. **Smoke** (live): add a task with a past due date from the Job Tracker → the task row shows
   `due <date>` + an Overdue pill; the assignee's My Tasks lists it FIRST in the open band;
   reassign it → the due date survives.

### Crew edit/retire + time amend/void (G2.3 — `0034`)

The operator-confirmed G2.3 epic: *"a subcontractor who typos a crew name can't fix it, and a
wrong time entry can't be corrected by anyone."* Full semantics in `docs/g23_crew_time_spec.md`
(G2.3). What ships:

- **Scoped crew EDIT/RETIRE** (`POST /api/fieldops/crew/:id/update` + `/retire`,
  cap.crew.create): a subcontractor fixes name/trade on — or soft-retires — crew **they
  created** (`personnel.created_by = actor`, ownership folded into the UPDATE). Retire is
  refused 409 when anyone **else** has logged time on the person (`crew_has_foreign_time`) or
  the person is placed on a **different** job (`crew_on_other_job`) — real workers escalate to
  the office. The manager/admin cap.personnel.manage routes are unchanged.
- **Non-destructive time AMEND/VOID** (`POST /api/fieldops/time-entry/:uuid/amend`,
  cap.time.log): a NEW chain row (`amends_uuid` = the target — the 0015 append-only chain),
  original never mutated; recorder-or-cap.personnel.manage only; **head-only** (409
  `not_head`, folded atomically into the INSERT); **void** = amend to `hours 0` + a required
  reason in `notes`. The create route now REJECTS a body `amends_uuid` (400
  `use_amend_route`). All time reads (job detail, personnel list/detail; the rollup already
  did) resolve to chain **heads only** via `NOT EXISTS` — never `NOT IN` (NULL-poison class).
- **Migration 0034** is a partial index on `time_entries(amends_uuid)` — performance-only
  (the head probes); **no lockout risk** if the apply is missed (slower scans, not 500s).
- SPA: Job-Tracker time rows gain Edit (prefilled amend form) / Void (inline required reason),
  shown only when the worker-computed `can_amend` is true; "corrected" pill + struck-through
  "voided" rows. The My-Tasks Add-crew list gains Edit/Retire on `created_by_me` rows.

See `docs/runbooks/fieldops_time_amend.md` (§43) + `docs/enablement/crew_time_corrections.md` (office guide).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0034** to the live D1 BEFORE the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`). (Always `git pull`
   `~/its` to latest `main` first — the stale-migrations-list lockout class.) A missed apply
   here degrades to slower time-leg reads, not an outage — but apply-before-deploy stays the
   rule.
2. **Redeploy** (`npm run deploy`) — activates the amend/void + crew edit/retire routes and
   the heads-only reads (SPA + Worker deploy together).
3. **Smoke** (live): as a subcontractor — add a crew member with a typo'd name on your job →
   Edit fixes it; add a duplicate → Retire removes it; log a wrong time entry on your job →
   Edit corrects it (the old row disappears from the table, the new one wears "corrected");
   Void with a reason strikes it through. As a manager: correct someone else's entry. In the
   network tab: retiring a person someone else logged time on 409s `crew_has_foreign_time`;
   amending a superseded entry 409s `not_head`.
### Checklist item photos — capture + pending queue (G1 Slice 1 — `0036`)

**Migration 0036** adds `item_photos` — the record-only photo-capture queue for checklist
items (**Option D, RATIFIED 2026-07-03**: photo evidence goes ON THE RECORD — screened on the
Mac, filed to Box — and the app shows only its status; **no serving route exists, ever**, and
**delete-on-screen** supersedes retention: D1 holds photo bytes only while `pending`). The
assignee attaches ONE photo per checklist item (`POST
/api/fieldops/checklist/item-state/:id/photo` — session + `cap.tasks.own` +
assignee-ownership; the verbatim `validatePhotoValues` bounds; HMAC-signed like submissions;
`photo_ref='pending:<id>'` stamped atomically with the queue INSERT + audit). The SPA renders
the lifecycle only: *"photo attached — screening…"* → *"photo on file ✓"* / refusal copy +
retry. A prune stage (`item_photos`) deletes stuck-pending rows (>7 d — the Mac screening
loop is down; the growth cap, not the alerting path) + orphans, and pending bytes join the D1
size tripwire. **Slice 2 (ships in this same PR)** is the Mac screening pass
(`portal_poll` + `photo_screen`, the byte-identical §34 pipeline) + the Box filing + the
clean/refused post-back; until it ships, uploads sit visibly `pending` (never lost, never
shown).

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0036** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the photo route
   and the extended `/checklist/assigned` read 500. (Always `git pull` `~/its` to latest
   `main` FIRST — the stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`).
3. **Smoke** (live): as an assignee with an assigned inspection, attach a photo to a
   manual/count item → the row flips to *"photo attached — screening…"*; a second attach on
   the same item is refused ("one photo per item"); the audit log gains a
   `checklist_item_photo_add` row. (The pending state clears within ~1-3 minutes once the portal_poll daemon — Slice 2, this PR — screens the photo; a persistently-pending photo means the daemon is not running.)

### Daily-report photo pool — additional site photos (v6 — `0037`)

**Migration 0037** adds `daily_photo_pool` — the pre-submit, §34-screened photo pool behind
the daily report's **"Add more photos"** button (operator directive 2026-07-03: *"upload more
than just four photos … add as many of those as you need"*). The inline 4-photo `site_photos`
field is **untouched**; it is payload-budgeted (CS2: 280KB×4 ≈ 1.49MB < the 1.8MB submit cap),
so additional photos structurally cannot ride the submission — each uploads **individually**
(`POST /api/fieldops/daily-photo` — session + the manager/admin daily-report role gate +
`requireJobScope`; the G1 item-photo bounds verbatim; a per-(job,date,uploader) 40-photo cap
and a 200-pending global backstop, both **folded into the INSERT** so a concurrent burst can't
blow the D1 ceiling; HMAC domain-separated `daily_photo:v1`) into the pool. The **Option D**
posture is inherited from `item_photos`: screened on the Mac, filed to Box, **no serving route,
delete-on-screen**. The submission carries only tiny **references**
(`values.additional_photos = [{pool_id, caption?}]`); at submit the Worker validates each ref
(exists / same job+date / uploaded-by-actor / not refused / unclaimed-or-mine) and **claims**
it atomically (claim-first, compensated on a lost race; an amendment **transfers** the filed
report's claims — the amends target is server-verified before honoring the transfer). The v6
cut also adds, under **D.13 Material & Equipment Deliveries**, a **"Report a material
incident →"** form link (the M2 per-row buttons stay). The Mac pass (`portal_poll`
`_service_daily_photos`) screens the pool **before** the submission fetch so the common case
files same-cycle; a still-pending reference defers the submission a bounded number of cycles,
then files with a *"N photos pending screening"* note (never blocks filing forever). A prune
stage deletes unclaimed pool rows (>7 d) + orphans; claimed rows follow delete-on-screen.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)

1. Apply migration **0037** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the pool routes
   and the v6 daily-report render 500. (Always `git pull` `~/its` to latest `main` FIRST — the
   stale-migrations-list lockout class.)
2. **Redeploy** (`npm run deploy`).
3. **Smoke** (live): as a placed manager, open the daily report → below the 4-photo field, use
   **"Add more photos"** and attach one → it chips *"screening…"* then *"photo on file ✓"*
   within ~1-3 min; submit → the filed PDF shows the additional photo(s) after the inline grid;
   a **malicious** upload must red-light (CRITICAL + a security-flagged Review-Queue row,
   refused, never filed — the prove-the-control-bites check). Under D.13, **"Report a material
   incident →"** deep-links the material-incident form.

### Lockout recovery (break-glass) — escalate to the Developer-Operator

If both admins are ever locked out (e.g. passwords lost, or both disabled), recovery runs
through the bearer CLI — **which reads the Keychain admin bearer (`ITS_PORTAL_ADMIN_TOKEN`),
so it is a high-capability (secrets/auth) operation that escalates to Seth**, not a Tier-2
repair: `portal_admin set-role <u> admin` / `enable-user <u>` / `reset-password <u>`. These
bearer routes have **no** last-admin guard precisely so they can restore an admin when the UI
can't. See `docs/runbooks/safety_portal_admin_dashboard.md`.

### Testing

Worker logic (the role gate, account CRUD, last-admin guard, self-edit re-auth, bearer
break-glass, audit rows) is tested with **`@cloudflare/vitest-pool-workers`** — the tests run
in **workerd (the real runtime) against a Miniflare D1** with the real migrations applied, not
mocks (`test/admin.test.ts`). `npm test` runs them; CI runs them in the `portal` job
(`npm ci` → `npm run typecheck` → `npm test`). The Python `test` job does not cover the
Worker TS, so this job is what makes the four-part "main-CI green" verify meaningful for the
auth code.

---

## Security posture (Phase 2)

- **Invariant 1 — External Send Gate:** the Worker performs **zero external
  transmission** (no email, no third-party outbound, no AI step). It only validates a
  login, signs/verifies a session cookie, and serves the SPA. The Phase 5 email shim is a
  separate, capability-gated component. *(Known gap, blueprint Decision-4-equivalent: the
  Python AST capability-gate does not reach the TS Worker; a Worker-side equivalent is
  Phase 5 work — out of scope here because the Worker is send-free.)*
- **Invariant 2 — Adversarial Input Handling:** all browser input is untrusted — request
  bodies are type-checked and length-bounded; D1 access uses bound parameters (no string
  interpolation); the session cookie is HttpOnly + signed (HMAC-SHA256 via `crypto.subtle`,
  constant-time verify — a tampered cookie is rejected).
- **Session model (accepted Phase-2 gap):** sessions are cookie-derived with **no
  server-side revocation** — `/api/logout` clears the client cookie only, and
  `requireSession` does not re-check that the user still exists, so a stolen or
  deprovisioned-user cookie stays valid until `iat + 90 days`. Acceptable because no real
  PMs exist until they're provisioned via the **Phase 7 admin route**, which adds the D1
  session table for explicit invalidation/deprovisioning.

> **Types:** `worker/types.ts` is the hand-authored source of truth for the `Env` bindings.
> `npm run cf-typegen` (`wrangler types`) is optional — no tsconfig depends on its generated
> `worker-configuration.d.ts` in Phase 2, and a fresh clone typechecks without it.

---

## What's stubbed / out of scope

Phase 2 is the skeleton + one form stub. **Not built here** (later phases per `brief.md` §14):

- Generic form runtime (`_runtime/` renderer + pdf_renderer) and per-form `form.ts` — **Phase 4**.
- The other nine forms (see `reference_forms/`) — **Phase 4**.
- Sync Worker (cron + Smartsheet webhook), D1 mirror tables — **Phase 3**.
- Submission pipeline: Python PDF render (Box-stored), the pull-model `portal_poll` daemon, `intake.py` portal branch — **Phase 5**.
- `/admin` route, user CRUD, per-user password scheme (Q2b) — **Phase 7**.
- JHA Weekly Compliance Rollup — **Phase 5/6**. (No R2 — PDFs live in Box.)

The JHA view is a **hard-coded stub** that mirrors the real layout to validate the stack;
it does not submit.

---

### Hours Log up-sync watermark (P7 Slice 1) — 0038

**What ships:** the first Track-2 standing per-job tracker — a per-job **Hours Log** Smartsheet in
the `ITS — Progress Reporting` workspace (in the job's folder, beside its week sheets), one-way-up
mirrored from D1 `time_entries` (send-free + AI-free, Op Stds v19 §51). Migration `0038` adds the
per-row `time_entries.mirrored_at` watermark + a partial pending index; the Worker gains
`GET /api/internal/fieldops/hours-pending` + `POST /api/internal/fieldops/hours-mark-mirrored`
(field-ops-token-gated, same privilege separation as the job-mirror queue); the existing
`field_ops.fieldops_sync` daemon gains a **hours pass** that runs in the SAME cycle/lock/heartbeat.

**Shipped DARK** — the hours pass is gated OFF by `field_ops.fieldops_sync.hours_enabled`
(default false). Applying `0038` + deploying the Worker changes nothing until the operator flips it.

**Activation (post apply-all + deploy):**
1. Confirm `ITS_PORTAL_FIELDOPS_TOKEN` (Keychain) matches the Worker `PORTAL_FIELDOPS_API_TOKEN`
   (already true — the job-mirror pass uses it).
2. Set `field_ops.fieldops_sync.hours_enabled = true` in `ITS_Config` (Workstream `field_ops`).
3. **Live smoke:** log a crew time entry in the portal → within one `fieldops_sync` cycle a row
   appears in the job's `<Job> — Hours Log` sheet (correct display name, hours, work date);
   amend it → the prior row flips to `Superseded`; the daemon's `ITS_Daemon_Health` row stays OK.
4. Kill the daemon mid-write → next cycle re-mirrors idempotently (find-or-create by `Entry UUID`),
   no duplicate row.

**§51 guards (2026-07-04 v19.x rider — Path B):** never-`delete_rows` + the SoR-safe **row-cap WARN
watchdog** (`hours_log.check_row_cap` — WARNs + Review-Queues an operator period-split as the sheet
nears the ~20k cap; the rider ratifies this as satisfying §51's period-split for LOW-VOLUME logs,
threshold `progress_reports.hours_log.row_cap_warn_threshold`, default 15000) ship in this PR; the
A1 sheet-count margin-check fires on each create. **archive-on-closure** is the one §51 guard still a
committed follow-up (**its#462** — needs a `smartsheet_client` move-sheet method). The `archived`
lifecycle write is **live today** (portal admin), so once `hours_enabled` is on, an archived job's
Hours Log **strands** in the progress workspace until #462 lands — never-deleted → recoverable, no
data loss, but the archive guarantee is skipped. **Land #462 before enabling `hours_enabled` in a
tenant where jobs may archive** (or accept the bounded stranded-sheet exposure — a manual move
recovers it). **Deferred to later slices:** the Equipment + Materials standing trackers (P7 Slices 2–3).

### Material List up-sync keys (P7 M2) — 0039

**What ships:** the per-job **Material List** standing tracker — a per-job `<Job> — Material List`
Smartsheet in the `ITS — Progress Reporting` workspace (in the job's folder, beside its Hours Log +
Equipment + week sheets), one-way-up mirrored from the operator-authored D1 `job_expected_materials`
list (send-free + AI-free, Op Stds v19 §51). Migration `0039` adds the two mirror keys the up-sync
needs — `line_uuid` (the stable per-line find-or-create key, backfilled + minted at the ADD-line
INSERT) and `unplanned` (0/1 off-manifest flag, default 0); it adds NO `smartsheet_row_id` (that is
bidirectional-only — a deferred FUTURE model, explicitly out of scope). The Worker gains
`GET /api/internal/fieldops/material-list-snapshot` (field-ops-token-gated, send-free, uncapped,
fully-bound SQL — same privilege separation + shape as the equipment-snapshot route); the existing
`field_ops.fieldops_sync` daemon gains a **material pass** that runs in the SAME cycle/lock/heartbeat.

**Model — PORTAL-AUTHORED, ONE-WAY-UP:** the operator authors + edits the list in the portal (the
#426 `cap.materials.manage` CRUD); M2 mirrors the WHOLE list UP (expected content + delivery state +
`Unplanned`). **NO down-sync, NOT bidirectional.** A removed (deactivated) line is marked
`On List = Removed` on the sheet, **never deleted** (§51).

**Shipped DARK** — the material pass is gated OFF by `field_ops.fieldops_sync.materials_enabled`
(default false). Applying `0039` + deploying the Worker changes nothing until the operator flips it.

**Activation (post apply-all + deploy):**
1. Confirm `ITS_PORTAL_FIELDOPS_TOKEN` (Keychain) matches the Worker `PORTAL_FIELDOPS_API_TOKEN`
   (already true — the job/hours/equipment passes use it).
2. Set `field_ops.fieldops_sync.materials_enabled = true` in `ITS_Config` (Workstream `field_ops`).
3. **Live smoke:** add an expected-materials line in the portal Job Tracker → within one
   `fieldops_sync` cycle a row appears in the job's `<Job> — Material List` sheet (correct Material /
   Description / Qty / Status); confirm receipt → the row flips to `received` with `Received By`
   showing the DISPLAY name; deactivate a line → next cycle its row flips to `On List = Removed` (not
   deleted); the daemon's `ITS_Daemon_Health` row stays OK.
4. Deactivate a job's LAST active line → the roster still visits it and marks the stale rows Removed
   (the count-drops-to-zero reconcile); a job with no sheet is silently skipped (no empty sheet).

**§51 guards:** never-`delete_rows` + the SoR-safe row-cap WARN watchdog (`material_list.check_row_cap`,
threshold `progress_reports.material_list.row_cap_warn_threshold`, default 15000); the A1 sheet-count
margin-check fires on each create; **archive-on-closure** now moves the Material List sheet too (same
its#462 move machinery — landed for the standing trackers). **Deferred:** a bidirectional
Smartsheet→D1 down-sync (would need `smartsheet_row_id`) — a FUTURE model, not M2.

### Recurring checklists per job (#16) — 0040

**What ships:** an admin can make a checklist **assignment recurring** — the same Assign-an-inspection
form gains a "Recurring checklist" checkbox → a cadence (**daily / weekly / biweekly / monthly**) + a
"generates off of" **anchor date**. A per-job generator (D1 `checklist_recurrences`, migration `0040`)
then spawns the assignee's `kind='inspection'` checklist instance on each cadence date — the SAME
instance shape a one-shot assign creates, so it surfaces in the assignee's Assigned-Tasks tab + the
admin Outstanding-assignments list with zero new read code. Generation runs in the **existing daily
Worker cron** (`scheduled()`, `wrangler.jsonc triggers.crons` 09:00 UTC) — no new daemon, fully
contained to the portal. New admin surfaces: `GET /checklist/recurrences` + `POST
/checklist/recurrence/:id/deactivate` (a "Recurring assignments" band that lists active generators +
a Stop button). Generation also **auto-stops** when the job closes.

**Idempotent by construction:** each spawn is `INSERT OR IGNORE` keyed on the EXISTING
`UNIQUE(kind, job_id, assignee_personnel_id, instance_date)` (0026), so a double-run never
double-spawns; a per-recurrence `last_generated_date` watermark bounds each pass. Catch-up after a
cron gap is capped at 45 days (older dates dropped + logged, never a flood).

**Shipped DARK** — gated by the Worker var `RECURRING_CHECKLISTS_ENABLED` (default `"false"` in
`wrangler.jsonc`, NOT a secret). While dark: the cron no-ops, the assign route refuses a recurrence
block with `400 recurring_disabled` (never-silent), and the SPA hides the recurring controls (the flag
rides `/api/login` + `/api/session`). So applying `0040` + deploying changes nothing until the flip.
The one-shot assign path is **byte-identical** when dark.

**Activation (post apply-all + deploy):**
1. Edit `wrangler.jsonc` → `"vars": { "RECURRING_CHECKLISTS_ENABLED": "true" }` → `npm run deploy`.
   (This is the operator-visible gate — an in-repo var, no ITS_Config/Smartsheet row to hunt for.)
2. **Live smoke:** in the admin Checklists page, Assign an inspection → check "Recurring checklist" →
   pick a job + cadence + a start date of **today** → "Set recurring". Confirm: (a) a "Recurring
   assignments" row appears; (b) the assignee's Assigned-Tasks tab shows today's instance immediately
   (the assign route materializes the first one on the spot); (c) tomorrow's cron adds the next
   instance (or force it by re-deploying / waiting for 09:00 UTC); (d) Stop the recurrence → it leaves
   the Recurring-assignments list; already-created instances remain under Outstanding assignments.
3. Close the job (lifecycle → closed) → the next cron auto-stops the recurrence (audit
   `checklist_recurrence_autostop`) and spawns nothing further.

**Adding a cadence later** is a Worker-code change only (extend `RECURRENCE_CADENCES` +
`enumerateCadenceDates` in `worker/fieldops_recurrence.ts` + the SPA `CADENCE_OPTIONS`) — the D1 table
deliberately carries no cadence CHECK, so no table rebuild.

### Checklist completion → weekly progress report (#17, Seam A) — 0041

**What ships:** when an admin-assigned **inspection** instance is COMPLETE (every item done), the
assignee **signs off** and the Worker synthesizes a `category:'progress'` **`checklist-completion-v1`**
submission — the item roster + the signature — that rides the **EXISTING** intake →
progress-week-sheet → weekly-compile pipeline. This is a **standard submission the built pipeline
files**, NOT a new §51 SoR write-route: the Worker mints it through the SAME
`buildSubmissionInsert` (`worker/submission.ts`, §14-extracted from `/api/submit`) so the row is
byte-identical — the same 5-field canonical HMAC, `box_verified=0`, and attribution columns — and
`portal_poll` verifies + files it like any other submission. The assignee's Assigned-inspections view
gains a **"Sign & log to progress report"** action on a complete inspection (a signature capture →
POST `/api/fieldops/checklist/instance/:id/submit`); once logged it shows a **"Logged to progress
report ✓"** pill. Emit is **exactly once per instance** — the `emitted_submission_uuid` one-shot
marker (migration `0041`, guarded `WHERE emitted_submission_uuid IS NULL`); a second submit → `409
already_submitted` with no duplicate. The required signature satisfies the definition's
`required_signature_inputs_min:1` legal floor (the new `checklist-completion` parent falls to
required-content.json's `defaults_for_new_identities`), so **no required-content.json edit**.

**Shipped DARK** — gated by the Worker var `CHECKLIST_PROGRESS_LOGGING_ENABLED` (default `"false"` in
`wrangler.jsonc`, NOT a secret). While dark: the emit route refuses with `400
progress_logging_disabled` (never-silent) and the SPA hides the "Sign & log" action (the flag rides
`/api/login` + `/api/session` as `checklist_progress_logging_enabled`). `/api/submit` is
**byte-identical** across the extraction (regression-locked by `test/submit-as.test.ts`), so applying
`0041` + deploying changes nothing until the flip.

**Activation (operator — deploy boundary; escalates to the Developer-Operator):**
1. Apply migration **0041** to the live D1 **BEFORE** the redeploy
   (`npx wrangler d1 migrations apply its-safety-portal-db --remote`) — else the emit route 500s on
   the missing `emitted_submission_uuid` / `completion_signature` / `completion_signed_at` columns.
   **ORDER-CRITICAL** (the migration header states it). (Always `git pull` `~/its` to latest `main`
   BEFORE `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
2. **Flip the Worker var:** `wrangler.jsonc` → `"vars": { …, "CHECKLIST_PROGRESS_LOGGING_ENABLED":
   "true" }` → `npm run deploy` (SPA + Worker deploy together; the operator-visible gate, no
   ITS_Config/Smartsheet row to hunt for). This arms the emit + reveals the "Sign & log" action.
3. **Route the synthesized submission on to the progress destination:** flip the SEPARATE ITS_Config
   `progress_reports.intake_enabled` flag so the Mac-side intake files the `checklist-completion-v1`
   submission into the **progress week-sheet** and the eventual weekly compile sends via the
   **progress@** mailbox (vs. the safety path). *(Until that flag is on, the submission is minted +
   verified but the progress-side filing/send is inert — the Worker leg here is independent of it.)*
4. **Live smoke:** as an admin, assign an inspection to a placed person with a job + due date; as that
   person, complete every item, open the inspection, "Sign & log to progress report", sign, submit.
   Confirm: (a) a `checklist-completion-v1` submission appears in `/api/internal/pending` with a
   verifying HMAC; (b) the inspection shows "Logged to progress report ✓" and a re-tap is refused
   (409); (c) with `progress_reports.intake_enabled` on, the submission lands in the job's progress
   week-sheet and rides the weekly compile.

**Adding fields to the emitted document later** is a Worker-code change (extend the `values` shape in
`worker/fieldops_checklist.ts`) + a matching `forms/checklist-completion-v1.json` add-version — the
definition + the emit payload must stay in lockstep (the header signature field is the legal floor).

### Purchase Orders — D1 + Worker po.ts (slice S2) — 0042/0044

**What ships:** the Worker half of the PO pipeline (Aug-7 program WS1 S2,
`docs/2026-07-09_aug7_delivery_program.md`). Migration `0042` creates `po_vendors` (the D1 cache
of the ITS_Vendors SoR, D4 bidirectional §51 rider: dirty-row fence + watermarks) + the
`po_vendor_counter` allocator; `0043` creates `purchase_orders` + `po_line_items` (money as
integer cents, D7 status machine, the UNIQUE `(job_no, site_phase, supersede_seq, revision)`
numbering backstop); `0044` grants `cap.po.manage` to `admin` (0023 pattern). `worker/po.ts`
registers the browser surface (vendors CRUD / drafts CRUD / generate with server-side cents
recompute + totals assert + atomic D7 allocation + `po:v1` HMAC / supersede / cancel) and the
`/api/po/internal/*` queue under the NEW `requirePoToken` bearer tier (`PORTAL_PO_API_TOKEN`,
privilege-separated from the portal_poll / admin / fieldops tokens).

**Shipped DARK** — nothing consumes these routes until the S4 Mac daemon
(`po_materials/po_poll.py`) and the S6 SPA pages land; applying 0042–0044 + deploying changes
nothing user-visible (admins gain the capability; the SPA has no PO surface yet).

**Activation (post apply-all + deploy):**
1. `wrangler secret put PORTAL_PO_API_TOKEN` (generate: `openssl rand -base64 48`), then mirror
   the SAME value into the macOS Keychain as `ITS_PORTAL_PO_TOKEN` (S4 daemon side).
2. The S3 terms/config slice replaces the temporary in-Worker `TAX_RATE_BP` const
   (`worker/po.ts`) with `po_materials/config/tax.json`; `GET /api/po/terms` + `/api/po/config`
   land there too (deliberately NOT in S2).

### Config editor queue — D1 + Worker config.ts (slice 1) — 0045

**What ships:** the send-free cloud queue for the generic §50 config editor (config slice 1 of 3).
Migration `0045` creates `config_requests` (the audit queue — `(workstream, artifact_key)` is the
per-artifact serialization key; the same `queued→validated→tested→merged→live→archived|failed`
state machine + lease/claim/stamp model as `publish_requests`). `worker/config.ts` registers
`POST /api/config/requests` + `GET /api/config/requests/status` (browser, session + per-workstream
capability) and the four `/api/internal/config/{pending,claim,stamp,stuck}` daemon routes under the
NEW `requireConfigToken` bearer tier (`PORTAL_CONFIG_API_TOKEN`, privilege-separated from the
portal_poll / admin / fieldops / PO tokens). A generic `CONFIG_REGISTRY` declares each workstream's
editable artifacts + cap; `po_materials` is real (purchaser / tax / terms), `subcontracts` is a
documented **placeholder** (a future subcontract workflow adds its artifacts + `cap.subcontracts.manage`
here with zero route changes).

**Shipped DARK** — nothing consumes these routes until the config slice-2 Mac daemon
(`po_materials/config_actuator.py`) and slice-3 SPA editor land; applying 0045 + deploying changes
nothing user-visible.

#### Activation (operator — secrets/auth + deploy boundary; escalates to the Developer-Operator)
Apply-before-deploy (0045 is a "reads-a-new-table" migration — the Worker 500s if it deploys ahead
of the table):
1. From a fresh `~/its`: `cd safety_portal && npx wrangler d1 migrations apply its-safety-portal-db --remote`
   (applies **all** pending, in order), then `npm run deploy`.
2. `wrangler secret put PORTAL_CONFIG_API_TOKEN` (generate: `openssl rand -base64 48`), then mirror
   the SAME value into the macOS Keychain as `ITS_PORTAL_CONFIG_TOKEN` (the slice-2 daemon side —
   `config_actuator` reads it fail-closed). The internal `/api/internal/config/*` routes are
   fail-closed on the missing secret, so they 401 until this is provisioned.

### Terms make-current + Layer-A legal gate — 0046

**What ships:** the terms LEGAL-ACTIVATION flow (config editor slice T2). `terms._version_entry` now
REFUSES a library version whose `legal_review != "cleared"` (Layer A — an un-cleared version can't
render onto a PO); the two shipped versions (`standard_17_v1`, `chint_vendor_v1`) are backfilled to
`cleared` in the same change so no live PO fences. A new `set_current` config op
(`config_apply` + `worker/config.ts`) plus a confirmable **"Make a version current"** portal control
clears a version's legal review + repoints `current_version` through the actuator. Migration `0046`
widens the `config_requests.op` CHECK to allow `set_current` (SQLite table-recreate; in LOCKSTEP with
`config.ts` `CONFIG_OPS` + `config_apply`). A new read-only `GET /api/po/terms/:id/versions` feeds the
make-current picker.

**Shipped DARK** — like the rest of the config editor, gated behind `config_actuator.polling_enabled`.

#### Activation (operator — deploy boundary; escalates to the Developer-Operator)
Apply-before-deploy (0046 widens a CHECK the Worker's new `set_current` INSERT depends on — a
make-current submit 500s if the Worker deploys ahead of the migration):
1. From a fresh `~/its` (`git pull origin main` FIRST — the stale-migrations-list lockout class):
   `cd safety_portal && npx wrangler d1 migrations apply its-safety-portal-db --remote` (applies
   **all** pending, in order), then `npm run deploy`.
2. No new secret. Smoke (live): admin → PO Configuration → **"Make a version current"** lists the
   terms versions with their `legal_review`; the confirm checkbox gates "Make it live"; submitting
   queues an `op:set_current` config request (track it in the status monitor).
