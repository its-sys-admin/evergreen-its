import type { Context } from "hono";
import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
import type { Env, Vars } from "./types";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { MAX_ADDRESS } from "./constants";
// Feature (2026-07-24): the job-create Safety CC <datalist> is fed from the SAME §50-edited
// delivery-contacts bundle the PO builder uses (worker/po.ts imports this too). Bundled at BUILD
// time; served read-only by GET /api/fieldops/delivery-contacts below (jobtracker-cap-gated).
import deliveryContactsConfig from "../../po_materials/config/delivery_contacts.json";

// P2.3 Slice 2 + P2.5 Slice 1 — JOB WRITE (create / lifecycle / contacts).
// cap.jobtracker.manage (admin-only). Send-free (D1 only).
//
// jobs is a PLAIN (in-place mutable) table — NOT integrity-bar — so updates UPDATE in place, but
// every mutation still writes an audit_log row in the SAME D1 batch (W4).
//
// P2.5 — PORTAL IS THE AUTHORITATIVE WRITER. The create form owns the full job source-of-truth
// (address, stakeholder, Safety + Progress routing contacts + CC arrays, lifecycle). The Mac-side
// mirror daemon (field_ops/fieldops_sync.py, Slice 5) reads dirty portal rows over
// GET /api/internal/fieldops/pending-jobs and find-or-creates a row in BOTH ITS-owned Active-Jobs
// sheets keyed by job_id (each sheet's "Portal Job Key" column).
//
// P2.5 Slice 6 — THE PORTAL ASSIGNS THE CANONICAL NUMBER. The office employee no longer types a
// Job ID; the create route allocates the next JOB-###### from the `job_counter` table (migration
// 0022) and uses it as BOTH job_id (D1 PK) and canonical_job_id from birth. That single identity
// shows everywhere — portal, both Active-Jobs sheets (Job ID column, retyped off AUTO_NUMBER),
// every report, Box. There is no Smartsheet-generates-then-read-back handshake: see
// allocateJobNumber() below and shared/active_jobs_writer.py (writes Job ID = job_id on upsert).
//
// 0017 ORIGIN FENCE: a portal-CREATED job is stamped origin='portal' FOREVER, so the 60s
// `/api/internal/sync` full-replace (scoped to origin='smartsheet') can never deactivate it.
//
// VERSION VECTOR (migration 0021): every SoR/lifecycle mutation bumps mirror_version + sets
// sync_state='pending' (the dirty flag). The daemon advances each sheet's watermark independently
// and flips sync_state→'synced' only when BOTH catch up (see /api/internal/fieldops/jobs-mark-mirrored
// in index.ts). progress% is NOT a mirrored SoR field (the Active-Jobs sheets have no progress
// column) — it survives only as an optional create-body field (default 0); the standalone
// POST /:job_id/progress route was deleted (see the tombstone below).

const MAX_JOB_ID = 64;
const MAX_NAME = 256;
const MAX_PHONE = 40;
const MAX_EMAIL = 320;
const MAX_CC = 5; // mirrors each Active-Jobs sheet's CC 1..5 columns
// Loose email shape: no whitespace, one @, a dotted domain (matches shared/active_jobs _EMAIL_RE).
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

// The values /lifecycle will SET. Deliberately EXCLUDES 'archived' — see the route below.
// 'archived' remains a legal stored value (JOB_LIFECYCLES in constants.ts, migration 0021); it is
// simply not settable through this route.
const LIFECYCLE_SETTABLE = new Set(["active", "inactive"]);

function clampPct(n: number): number {
  return Math.max(0, Math.min(100, Math.round(n)));
}

/** lifecycle → the legacy `active` int (the dropdown/down-sync flag): only 'active' is live. */
function lifecycleToActive(lifecycle: string): number {
  return lifecycle === "active" ? 1 : 0;
}

/** Allocate the next portal-assigned canonical job number, atomically.
 *
 *  `UPDATE … SET last_value = last_value + 1 … RETURNING last_value` is a SINGLE atomic statement;
 *  D1 serializes writes, so two concurrent creates receive distinct numbers (no read-then-write
 *  race). Returns the formatted `JOB-######` (6-digit zero-pad, matching the legacy AUTO_NUMBER),
 *  or null when the counter is UNAVAILABLE — either the seed row is missing OR the `job_counter`
 *  table itself is absent (migration 0022 not applied before the Worker deployed; D1 THROWS
 *  "no such table" rather than returning null, so we catch it). Both collapse to null so the caller
 *  returns ONE clean fail-closed 500 `counter_unavailable` for the real deploy-order case — never a
 *  malformed id, and never an opaque `internal_error` the runbook can't grep for. */
async function allocateJobNumber(db: D1Database): Promise<string | null> {
  let row: { last_value: number } | null;
  try {
    row = await db
      .prepare("UPDATE job_counter SET last_value = last_value + 1 WHERE id = 1 RETURNING last_value")
      .first<{ last_value: number }>();
  } catch {
    return null; // table absent (0022 not applied) → fail closed, identical to a missing row
  }
  if (!row) return null;
  return `JOB-${String(row.last_value).padStart(6, "0")}`;
}

// ---- routing-block validation (shared by create + contacts edit) ----------

interface Routing {
  job_no: string;
  address: string;
  address_city: string;
  address_state: string;
  address_zip: string;
  stakeholder_name: string;
  stakeholder_email: string;
  stakeholder_phone: string;
  safety_contact_name: string;
  safety_contact_email: string;
  safety_cc: string[];
  progress_contact_name: string;
  progress_contact_email: string;
  progress_cc: string[];
}

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

/** Validate + normalize a CC array field: ≤MAX_CC entries, each a non-empty length-bounded
 *  email-shaped string. Returns the cleaned array, or null on any malformed entry / over-cap. */
function parseCc(v: unknown): string[] | null {
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) return null;
  if (v.length > MAX_CC) return null;
  const out: string[] = [];
  for (const e of v) {
    const s = typeof e === "string" ? e.trim() : "";
    if (!s) continue; // skip blanks (an empty CC slot)
    if (s.length > MAX_EMAIL || !EMAIL_RE.test(s)) return null;
    out.push(s);
  }
  return out;
}

type RoutingResult = { ok: true; routing: Routing } | { ok: false; error: string };

/** Parse the SoR/routing block from a request body. All fields optional (default ''); a present
 *  contact email must be email-shaped; CC arrays bounded. Used by create (full) + contacts (edit). */
function parseRouting(body: Record<string, unknown>): RoutingResult {
  // 0057 — the Evergreen YYYY.NNN tracking number + the structured address block.
  // job_no: empty (not yet assigned) or the exact Evergreen shape; the builders'
  // dropdown autofill reads it back, so a malformed value is refused loudly here.
  const job_no = str(body.job_no);
  if (job_no && !/^\d{4}\.\d{3}$/.test(job_no)) return { ok: false, error: "invalid_job_no" };
  const address = str(body.address);
  const address_city = str(body.address_city);
  if (address_city.length > MAX_NAME) return { ok: false, error: "invalid_address_city" };
  const address_state = str(body.address_state).toUpperCase();
  if (address_state && !/^[A-Z]{2}$/.test(address_state)) return { ok: false, error: "invalid_address_state" };
  const address_zip = str(body.address_zip);
  if (address_zip.length > 16) return { ok: false, error: "invalid_address_zip" };
  const stakeholder_name = str(body.stakeholder_name);
  const stakeholder_email = str(body.stakeholder_email);
  const stakeholder_phone = str(body.stakeholder_phone);
  const safety_contact_name = str(body.safety_contact_name);
  const safety_contact_email = str(body.safety_contact_email);
  const progress_contact_name = str(body.progress_contact_name);
  const progress_contact_email = str(body.progress_contact_email);

  if (address.length > MAX_ADDRESS) return { ok: false, error: "invalid_address" };
  for (const n of [stakeholder_name, safety_contact_name, progress_contact_name]) {
    if (n.length > MAX_NAME) return { ok: false, error: "invalid_contact_name" };
  }
  if (stakeholder_phone.length > MAX_PHONE) return { ok: false, error: "invalid_phone" };
  for (const e of [stakeholder_email, safety_contact_email, progress_contact_email]) {
    if (e.length > MAX_EMAIL) return { ok: false, error: "invalid_email" };
    if (e && !EMAIL_RE.test(e)) return { ok: false, error: "invalid_email" };
  }
  const safety_cc = parseCc(body.safety_cc);
  const progress_cc = parseCc(body.progress_cc);
  if (safety_cc === null || progress_cc === null) return { ok: false, error: "invalid_cc" };

  return {
    ok: true,
    routing: {
      job_no, address, address_city, address_state, address_zip,
      stakeholder_name, stakeholder_email, stakeholder_phone,
      safety_contact_name, safety_contact_email, safety_cc,
      progress_contact_name, progress_contact_email, progress_cc,
    },
  };
}

export function registerJobWriteRoutes(app: FieldopsApp, gates: FieldopsGates): void {
  // GET /api/fieldops/delivery-contacts — the configured delivery-contact suggestion list feeding
  // the job-create Safety CC <datalist>. Gated cap.jobtracker.manage (the job creator's OWN cap) so
  // a plain job creator reads it WITHOUT holding cap.po.manage (GET /api/po/config is PO-admin-only,
  // a different capability). Read-only + bound: returns ONLY the { name, phone, email } already in
  // the §50-edited bundle — no leak, no mutation, no W4 audit row. Mirrors the delivery_contacts
  // shape worker/po.ts serves at GET /api/po/config.
  app.get(
    "/api/fieldops/delivery-contacts",
    gates.requireSession,
    gates.requireCapability("cap.jobtracker.manage"),
    (c) => c.json({ delivery_contacts: deliveryContactsConfig.contacts }),
  );

  // POST /api/fieldops/job — create a portal-origin job with full routing SoR (+ optional client).
  app.post(
    "/api/fieldops/job",
    gates.requireSession,
    gates.requireCapability("cap.jobtracker.manage"),
    async (c) => {
      let body: Record<string, unknown>;
      try {
        body = (await c.req.json()) as Record<string, unknown>;
      } catch {
        return c.json({ error: "bad_request" }, 400);
      }
      if (typeof body !== "object" || body === null || Array.isArray(body)) {
        return c.json({ error: "bad_request" }, 400);
      }

      // Slice 6: the portal ASSIGNS the Job ID (allocated below); the office employee types only
      // the Project Name. body.job_id is ignored.
      const projectName = typeof body.project_name === "string" ? body.project_name.trim() : "";
      if (projectName.length < 1 || projectName.length > MAX_NAME) return c.json({ error: "invalid_project_name" }, 400);
      const progress = typeof body.progress === "number" && Number.isFinite(body.progress) ? clampPct(body.progress) : 0;

      const routed = parseRouting(body);
      if (!routed.ok) return c.json({ error: routed.error }, 400);
      const r = routed.routing;

      // CREATE-ONLY: at least one Safety CC recipient is REQUIRED on a new job (up to the MAX_CC cap
      // that parseCc already enforces). The weekly safety email CCs these addresses; a job created
      // with none would silently ship to the primary recipient only. This guard lives HERE, NOT in
      // the shared parseRouting/parseCc — the /contacts EDIT route below MUST still allow blanking
      // the CC list (operator decision: required is create-only). Evaluated before the client insert
      // + number allocation so a rejected create burns nothing.
      if (r.safety_cc.length === 0) return c.json({ error: "safety_cc_required" }, 400);

      // Client linkage: an existing client_id (verified) OR an inline new_client. Validate shapes.
      const clientIdRaw = typeof body.client_id === "number" && Number.isInteger(body.client_id) ? body.client_id : null;
      const newClient =
        body.new_client !== null && typeof body.new_client === "object" && !Array.isArray(body.new_client)
          ? (body.new_client as Record<string, unknown>)
          : null;
      if (body.client_id !== undefined && clientIdRaw === null) return c.json({ error: "invalid_client_id" }, 400);
      if (newClient && clientIdRaw !== null) return c.json({ error: "client_id_and_new_client" }, 400);

      const actor = c.get("session").username;

      let clientId: number | null = clientIdRaw;
      if (newClient) {
        const name = typeof newClient.name === "string" ? newClient.name.trim() : "";
        if (name.length < 1 || name.length > MAX_NAME) return c.json({ error: "invalid_client_name" }, 400);
        const contact = typeof newClient.contact === "string" ? newClient.contact : null;
        const phone = typeof newClient.phone === "string" ? newClient.phone : null;
        const email = typeof newClient.email === "string" ? newClient.email : null;
        if ((contact !== null && contact.length > MAX_NAME) || (phone !== null && phone.length > MAX_PHONE) || (email !== null && email.length > MAX_EMAIL)) {
          return c.json({ error: "invalid_client_field" }, 400);
        }
        const inserted = await c.env.DB
          .prepare("INSERT INTO clients (name, contact, phone, email) VALUES (?1,?2,?3,?4) RETURNING id")
          .bind(name, contact, phone, email)
          .first<{ id: number }>();
        clientId = inserted!.id;
      } else if (clientId !== null) {
        const client = await c.env.DB.prepare("SELECT id FROM clients WHERE id = ?1").bind(clientId).first();
        if (!client) return c.json({ error: "unknown_client" }, 422);
      }

      // Allocate the canonical number LAST — after all validation + the optional client insert — so
      // a 400/422 never burns a number. null ⇒ migration 0022 not applied → fail closed (500), never
      // a malformed id (deploy-order discipline: apply 0022 --remote BEFORE the Worker deploys).
      const jobId = await allocateJobNumber(c.env.DB);
      if (jobId === null) return c.json({ error: "counter_unavailable" }, 500);

      // Mutation + audit in ONE batch. 0017 fence (origin='portal', sync_state='pending') + 0021
      // SoR fields + version vector (lifecycle='active', mirror_version=1; watermarks default 0 so the
      // brand-new row is immediately dirty for the mirror daemon). canonical_job_id = job_id from
      // birth (?1) — the portal owns the number, so there is no Smartsheet read-back to wait for.
      // Server-authoritative created_at.
      try {
        await c.env.DB.batch([
          c.env.DB
            .prepare(
              `INSERT INTO jobs (
                 job_id, project_name, active, status, progress, client_id, created_at,
                 origin, sync_state, canonical_job_id,
                 address, stakeholder_name, stakeholder_email, stakeholder_phone,
                 safety_contact_name, safety_contact_email, safety_cc,
                 progress_contact_name, progress_contact_email, progress_cc,
                 lifecycle, mirror_version,
                 job_no, address_city, address_state, address_zip)
               VALUES (?1, ?2, 1, 'active', ?3, ?4, unixepoch(),
                       'portal', 'pending', ?1,
                       ?5, ?6, ?7, ?8,
                       ?9, ?10, ?11,
                       ?12, ?13, ?14,
                       'active', 1,
                       ?15, ?16, ?17, ?18)`,
            )
            .bind(
              jobId, projectName, progress, clientId,
              r.address, r.stakeholder_name, r.stakeholder_email, r.stakeholder_phone,
              r.safety_contact_name, r.safety_contact_email, JSON.stringify(r.safety_cc),
              r.progress_contact_name, r.progress_contact_email, JSON.stringify(r.progress_cc),
              r.job_no, r.address_city, r.address_state, r.address_zip,
            ),
          auditStmt(c, actor, "job_create", jobId, { job_id: jobId, client_id: clientId, origin: "portal" }),
        ]);
      } catch (e) {
        if (isUniqueViolation(e)) return c.json({ error: "job_exists" }, 409);
        throw e;
      }
      return c.json({ ok: true, job_id: jobId }, 201);
    },
  );

  // POST /api/fieldops/job/:job_id/lifecycle — set the job lifecycle (active|inactive ONLY).
  // Derives the legacy `active` flag, bumps the mirror version, re-dirties the row. THE close path:
  // the UI closes a job via { lifecycle: 'inactive' } (the old bare /close alias was deleted —
  // tombstone below).
  //
  // 'archived' IS REFUSED HERE (409 use_archive_route). Until this change it was accepted, and
  // accepting it was a live hazard, not a cosmetic one: a `lifecycle='archived'` write re-dirties
  // the row, and on the very next mirror cycle the Mac-side daemon relocated FOUR standing tracker
  // sheets out of the job's progress folder — with no confirmation prompt, no retry on failure, and
  // a UI that then re-displayed the job as "Inactive" because it re-derived state from the legacy
  // `status` column. One dropdown selection, a silent multi-sheet move, and no way to tell it had
  // happened. It had never fired against live data; it was one click from doing so.
  //
  // Archiving is a heavyweight, reversible, cross-system relocation and gets its own confirmed
  // route (ROADMAP Track 6). 409 rather than 400 because the intent is valid and the route is
  // wrong — same shape as `use_amend_route`; `invalid_lifecycle` ("That isn't a valid job state")
  // would be a lie, since 'archived' IS a valid stored state.
  app.post(
    "/api/fieldops/job/:job_id/lifecycle",
    gates.requireSession,
    gates.requireCapability("cap.jobtracker.manage"),
    async (c) => {
      const jobId = c.req.param("job_id");
      if (jobId.length > MAX_JOB_ID) return c.json({ error: "invalid_job_id" }, 400);
      let body: Record<string, unknown>;
      try {
        body = (await c.req.json()) as Record<string, unknown>;
      } catch {
        return c.json({ error: "bad_request" }, 400);
      }
      // JSON `null`/arrays parse fine but aren't objects; dereferencing body.lifecycle on them
      // would throw → bare 500 (the "audit #1" class). Require a plain object first.
      if (typeof body !== "object" || body === null || Array.isArray(body)) {
        return c.json({ error: "bad_request" }, 400);
      }
      const lifecycle = typeof body.lifecycle === "string" ? body.lifecycle.trim().toLowerCase() : "";
      // Checked BEFORE the settable-set test so a stale SPA bundle (or curl) gets the specific,
      // actionable code instead of a generic "invalid state".
      if (lifecycle === "archived") return c.json({ error: "use_archive_route" }, 409);
      if (!LIFECYCLE_SETTABLE.has(lifecycle)) return c.json({ error: "invalid_lifecycle" }, 400);
      const res = await setLifecycle(c, jobId, lifecycle);
      return res;
    },
  );

  // TOMBSTONE (operator-approved deletion, 2026-07-03): POST /api/fieldops/job/:job_id/close — the
  // thin back-compat alias → lifecycle='inactive' — was DELETED (zero SPA/Python callers since
  // setLifecycle superseded it in the UI, P2.5). The /lifecycle route above is the live close path.
  // Git history has the handler (this file, pre-deletion).

  // POST /api/fieldops/job/:job_id/contacts — partial edit of the routing SoR block; bumps the
  // mirror version + re-dirties. Only routing fields are touched (job_id/lifecycle/status untouched).
  app.post(
    "/api/fieldops/job/:job_id/contacts",
    gates.requireSession,
    gates.requireCapability("cap.jobtracker.manage"),
    async (c) => {
      const jobId = c.req.param("job_id");
      if (jobId.length > MAX_JOB_ID) return c.json({ error: "invalid_job_id" }, 400);
      let body: Record<string, unknown>;
      try {
        body = (await c.req.json()) as Record<string, unknown>;
      } catch {
        return c.json({ error: "bad_request" }, 400);
      }
      if (typeof body !== "object" || body === null || Array.isArray(body)) {
        return c.json({ error: "bad_request" }, 400);
      }
      const routed = parseRouting(body);
      if (!routed.ok) return c.json({ error: routed.error }, 400);
      const r = routed.routing;
      // Optional project_name edit (the "edit ALL job information" surface, 2026-07-20):
      // ABSENT = unchanged (a name can never be blanked through omission — unlike the
      // routing block's documented full-overwrite); present → 1..MAX_NAME or 400. NOTE
      // the operational consequence carried to the SPA copy: per-job Smartsheet/Box
      // folders are find-or-create BY NAME at filing time, so a rename means FUTURE
      // filings open a new folder — visible in the UI hint, never silent.
      let projectName: string | null = null;
      if (body.project_name !== undefined) {
        projectName = typeof body.project_name === "string" ? body.project_name.trim() : "";
        if (projectName.length < 1 || projectName.length > MAX_NAME) {
          return c.json({ error: "invalid_project_name" }, 400);
        }
      }
      const actor = c.get("session").username;
      // Bump the version + re-dirty in the same batch; conditional audit logs only on a real row.
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            // SCOPED TO origin='portal' (security, W5): never edit a smartsheet-origin job's SoR —
            // the down-sync can't reconcile these fields, so a stray write would corrupt it forever.
            `UPDATE jobs SET
               address=?2, stakeholder_name=?3, stakeholder_email=?4, stakeholder_phone=?5,
               safety_contact_name=?6, safety_contact_email=?7, safety_cc=?8,
               progress_contact_name=?9, progress_contact_email=?10, progress_cc=?11,
               job_no=?12, address_city=?13, address_state=?14, address_zip=?15,
               project_name=COALESCE(?16, project_name),
               mirror_version=mirror_version+1, sync_state='pending'
             WHERE job_id=?1 AND origin='portal'`,
          )
          .bind(
            jobId,
            r.address, r.stakeholder_name, r.stakeholder_email, r.stakeholder_phone,
            r.safety_contact_name, r.safety_contact_email, JSON.stringify(r.safety_cc),
            r.progress_contact_name, r.progress_contact_email, JSON.stringify(r.progress_cc),
            r.job_no, r.address_city, r.address_state, r.address_zip,
            projectName,
          ),
        auditStmtIfChanged(c, actor, "job_contacts", jobId, { job_id: jobId }),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
      return c.json({ ok: true, job_id: jobId }, 200);
    },
  );

  // ── ARCHIVE / UN-ARCHIVE (ROADMAP Track 6) ─────────────────────────────────────────────────
  //
  // Their OWN routes rather than lifecycle values, for three concrete reasons: there is nowhere
  // on /lifecycle to hang a confirmation token; the audit action would be indistinguishable from
  // an ordinary close in audit_log; and the error vocabulary here (already_archived /
  // confirm_mismatch / archive_in_flight) is meaningless for active|inactive.
  //
  // Gated on cap.job.archive, NOT cap.jobtracker.manage — every admin holds the latter for routine
  // create / rename / close, and archiving is a heavyweight, reversible, cross-system relocation
  // that should be separately narrowable later. Not requireRole("admin") either: FieldopsGates
  // deliberately exposes only the capability helpers, and widening it would break the
  // no-import-cycle contract with index.ts.

  app.post(
    "/api/fieldops/job/:job_id/archive",
    gates.requireSession,
    gates.requireCapability("cap.job.archive"),
    async (c) => archiveTransition(c, "archive"),
  );

  app.post(
    "/api/fieldops/job/:job_id/unarchive",
    gates.requireSession,
    gates.requireCapability("cap.job.archive"),
    async (c) => archiveTransition(c, "unarchive"),
  );

  // TOMBSTONE (operator-approved deletion, 2026-07-03): POST /api/fieldops/job/:job_id/progress —
  // the manual progress-% write — was DELETED. Nothing displayed the value (the UI slider/bar was
  // removed in #403, the P6 rollup deliberately excludes progress %), and no Python read it. The
  // `jobs.progress` COLUMN and the optional create-body `progress` field remain (see the
  // "Remove the progress-% estimate system-wide" tech-debt entry for the full multi-surface
  // removal). Git history has the handler (this file, pre-deletion).
}

// ---- lifecycle setter (used by /lifecycle; formerly also the deleted /close alias) -------------

/** Set lifecycle + the derived `active` flag (+ legacy `status` for back-compat: inactive/archived
 *  → 'closed', active → 'active'), bump mirror_version, re-dirty (sync_state='pending'), audit.
 *  Idempotent-friendly: a no-op same-value set still bumps the version (cheap; the daemon no-ops).
 *
 *  SCOPED TO origin='portal' (security): the portal is the SOLE writer of portal-created jobs;
 *  these edit routes must NEVER touch an origin='smartsheet' row (the down-sync only reconciles
 *  project_name/active, so a stray lifecycle/SoR write to a smartsheet job would corrupt it
 *  permanently, with no self-heal). A non-portal (or unknown) job_id → 0 changes → 404. */
async function setLifecycle(
  c: Context<{ Bindings: Env; Variables: Vars }>,
  jobId: string,
  lifecycle: string,
): Promise<Response> {
  const active = lifecycleToActive(lifecycle);
  const status = lifecycle === "active" ? "active" : "closed";
  const actor = c.get("session").username;
  const res = await c.env.DB.batch([
    c.env.DB
      .prepare(
        "UPDATE jobs SET lifecycle=?2, active=?3, status=?4, mirror_version=mirror_version+1, sync_state='pending' " +
          "WHERE job_id=?1 AND origin='portal'",
      )
      .bind(jobId, lifecycle, active, status),
    auditStmtIfChanged(c, actor, "job_lifecycle", jobId, { job_id: jobId, lifecycle }),
  ]);
  if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
  return c.json({ ok: true, job_id: jobId, lifecycle }, 200);
}


// ---- archive / un-archive transition (Track 6) -------------------------------------------------

/** The states a NEW archive/un-archive request may be raised from. `partial`/`failed` are
 *  retryable by design — the operator's "Try again" re-raises and resets the attempt counter. */
const ARCHIVE_RESTARTABLE = new Set(["none", "partial", "failed"]);
/** In-flight: the daemon owns the row until it reports a terminal result. */
const ARCHIVE_IN_FLIGHT = new Set(["requested", "in_progress"]);

/** The same restartable set as a SQL literal list, for the atomic in-WHERE guard. Kept beside the
 *  Set so the two cannot drift — `archiveTransition` asserts they agree at call time. */
const ARCHIVE_RESTARTABLE_SQL = "('none','partial','failed')";

/** The states a STALLED relocation may be RESUMED from, as distinct from newly raised. `none` is
 *  deliberately absent: it means nothing is half-done, so there is nothing to resume. Un-archive
 *  needs this because its normal entry point is `complete` — without a resume arm a half-restored
 *  job has no forward path at all, and the UI's only retry control would have to run the OPPOSITE
 *  direction to move at all. (issue #54) */
const ARCHIVE_RESUMABLE = new Set(["partial", "failed"]);
const ARCHIVE_RESUMABLE_SQL = "('partial','failed')";

/** Unicode "Other or Separator" — the complement of Python's `str.isprintable()` (which excepts
 *  the ASCII space, handled at the call site). Cc/Cf/Cs/Co/Cn + Zl/Zp/Zs. */
const NONPRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;

/** Sanitize a project name into the per-job folder key.
 *
 *  LOCKSTEP with `safety_reports/safety_naming.job_folder_name` — drop non-printables, turn '/'
 *  into '-', strip surrounding whitespace, and fall back to the raw stripped name if sanitizing
 *  empties it. Mirrored here (rather than imported) because the Worker cannot import Python; the
 *  cross-language parity is covered by test/job-archive.test.ts.
 *
 *  Snapshotted at REQUEST time so a later /contacts rename cannot strand the daemon against a
 *  name that no longer resolves. */
export function jobFolderKey(projectName: string): string {
  // Python's str.isprintable() is "NOT (Unicode category Other or Separator), except ASCII space"
  // — i.e. Cc, Cf, Cs, Co, Cn, Zl, Zp, Zs. A narrower C0/C1-only filter is NOT equivalent, and the
  // difference is reachable in practice: a project name pasted from Word, Excel or a PDF routinely
  // carries a non-breaking space (U+00A0), a zero-width space/joiner (U+200B/U+200D), a direction
  // mark (U+200E), or an en/ideographic space (U+2002/U+3000).
  //
  // Getting this wrong is silent and expensive. Python creates the real folder with those
  // characters STRIPPED, so if the Worker snapshotted archive_folder_key with them intact, the
  // daemon would resolve against a folder name that was never created — a permanent archive
  // failure surfacing only as an unexplained archive_state='failed'.
  const printable = Array.from(projectName)
    .filter((ch) => ch === " " || !NONPRINTABLE.test(ch))
    .join("");
  const cleaned = printable.replaceAll("/", "-").trim();
  return cleaned.length > 0 ? cleaned : projectName.trim();
}

/** POST /:job_id/archive and /:job_id/unarchive — raise a relocation request for the daemon.
 *
 *  This route does NOT move anything. It records intent; the Mac-side pass drains the queue and
 *  reports back. Keeping those separate is what lets the UI say "Archiving…" honestly instead of
 *  claiming a job is archived the instant a flag flips.
 *
 *  The typed confirmation is checked SERVER-side against the row's own project_name. Client-only
 *  would not be a control at all (Invariant 2), would be untestable in workerd, and would turn
 *  "wrong job open in a second tab" from a refusal into an archive. House precedent:
 *  operator_dashboard/auth.verify_elevated. Trim-only and case-sensitive; deliberately NOT
 *  constant-time — the name is rendered in the modal and is not a secret, so the bearer-digest
 *  helpers in index.ts would be cargo-cult here. */
async function archiveTransition(
  c: Context<{ Bindings: Env; Variables: Vars }>,
  direction: "archive" | "unarchive",
): Promise<Response> {
  // `?? ""` because this handler takes a generic Context (shared by both routes), so Hono cannot
  // infer the param from the path the way an inline handler does. The empty case is then refused
  // explicitly rather than being allowed to query for a blank job_id.
  const jobId = c.req.param("job_id") ?? "";
  if (!jobId || jobId.length > MAX_JOB_ID) return c.json({ error: "invalid_job_id" }, 400);

  let body: Record<string, unknown>;
  try {
    body = (await c.req.json()) as Record<string, unknown>;
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON null/arrays parse fine but aren't objects; dereferencing on them throws → bare 500
  // (the "audit #1" class). Require a plain object first.
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const confirm = typeof body.confirm === "string" ? body.confirm : "";
  if (confirm.length < 1 || confirm.length > MAX_NAME) {
    return c.json({ error: "confirm_required" }, 400);
  }

  // origin='portal' scoping, same security fence as every other edit route here: the down-sync
  // only reconciles project_name/active for smartsheet-origin rows, so a stray archive write to
  // one would corrupt it permanently with no self-heal.
  const row = await c.env.DB
    .prepare(
      "SELECT project_name, lifecycle, archive_state, archive_direction FROM jobs " +
        "WHERE job_id=?1 AND origin='portal'",
    )
    .bind(jobId)
    .first<{ project_name: string; lifecycle: string; archive_state: string; archive_direction: string }>();
  if (!row) return c.json({ error: "not_found" }, 404);

  // Post-trim floor on BOTH sides. The length check above runs before trimming, so `confirm: " "`
  // clears it — and would then match a row whose project_name was somehow empty. That cannot
  // happen today (create and /contacts both require a non-empty name AFTER trimming), but this
  // makes the control independent of that invariant rather than quietly dependent on it: a future
  // bulk-import or migration path that skips those validators must not open a bypass here.
  const expected = (row.project_name ?? "").trim();
  if (confirm.trim().length < 1 || expected.length < 1) {
    return c.json({ error: "confirm_required" }, 400);
  }
  if (confirm.trim() !== expected) {
    return c.json({ error: "confirm_mismatch" }, 409); // zero writes — asserted in the tests
  }

  const state = row.archive_state || "none";
  const inFlightDir = row.archive_direction || "";

  // Idempotent double-click: the SAME direction already in flight is a 200 no-op, NOT a second
  // request — re-raising would reset attempts and re-stamp requested_at under the daemon.
  if (ARCHIVE_IN_FLIGHT.has(state) && inFlightDir === direction) {
    return c.json({ ok: true, job_id: jobId, archive: { state, direction: inFlightDir } }, 200);
  }
  // The OPPOSITE direction in flight: refuse rather than reverse mid-relocation.
  if (ARCHIVE_IN_FLIGHT.has(state)) return c.json({ error: "archive_in_flight" }, 409);

  if (direction === "archive") {
    if (state === "complete") return c.json({ error: "already_archived" }, 409);
    if (!ARCHIVE_RESTARTABLE.has(state)) return c.json({ error: "archive_in_flight" }, 409);
  } else {
    // Un-archiving is meaningful once an archive completed — OR as a RESUME of a stalled
    // un-archive of our own (`partial`/`failed` still carrying direction='unarchive'). Without
    // that second arm a half-restored job is stranded: `complete` is gone (the archive was
    // consumed), so every un-archive request 409s and the only control the UI offers on a
    // partial is "Try again", which would then have to run the OPPOSITE direction to move at
    // all. A `partial` carrying direction='archive' is NOT resumable here — that one is still
    // an archive, and belongs to the branch above. (issue #54)
    const resumable = ARCHIVE_RESUMABLE.has(state) && inFlightDir === "unarchive";
    if (state !== "complete" && !resumable) return c.json({ error: "not_archived" }, 409);
  }

  // Un-archive returns the job to INACTIVE, never active. Bringing folders back is a retrieval,
  // not a re-opening — auto-activating would silently re-add the job to every dropdown and both
  // weekly compiles as a side effect of a folder move.
  const lifecycle = direction === "archive" ? "archived" : "inactive";
  const active = lifecycleToActive(lifecycle);
  const status = "closed"; // neither end of this transition is an 'active' job
  const folderKey = jobFolderKey(row.project_name ?? "");
  const actor = c.get("session").username;

  // ATOMIC IN-WHERE GUARD. The state checks above ran against a SEPARATE read, so on their own
  // they are check-then-act: two concurrent requests can both observe 'none', both pass, and both
  // write — producing two audit rows and re-stamping archive_requested_at. Worse, once the daemon
  // exists it writes these same columns from another process, so a request holding a stale read
  // could stomp an `in_progress` claim back to 'requested' and zero archive_attempts UNDERNEATH an
  // active relocation.
  //
  // Re-asserting the permitted source state in the UPDATE's own WHERE closes that window: the
  // database, not the handler's snapshot, decides whether the transition is still legal. The JS
  // checks above are retained ONLY to choose the specific error code — they are the message, this
  // is the control. (Same posture as the publish claim/stamp state machine elsewhere in the Worker.)
  const stateGuard =
    direction === "archive"
      ? `AND archive_state IN ${ARCHIVE_RESTARTABLE_SQL}`
      : `AND (archive_state = 'complete' OR (archive_state IN ${ARCHIVE_RESUMABLE_SQL} ` +
        "AND archive_direction = 'unarchive'))";

  // ONE batch: the mutation and its audit row land together or not at all (the "W4" class).
  const res = await c.env.DB.batch([
    c.env.DB
      .prepare(
        "UPDATE jobs SET lifecycle=?2, active=?3, status=?4, " +
          "archive_state='requested', archive_direction=?5, archive_requested_at=unixepoch(), " +
          "archive_completed_at=NULL, archive_attempts=0, archive_detail='', archive_folder_key=?6, " +
          "mirror_version=mirror_version+1, sync_state='pending' " +
          `WHERE job_id=?1 AND origin='portal' ${stateGuard}`,
      )
      .bind(jobId, lifecycle, active, status, direction, folderKey),
    auditStmtIfChanged(c, actor, direction === "archive" ? "job_archive" : "job_unarchive", jobId, {
      job_id: jobId,
      direction,
      folder_key: folderKey,
    }),
  ]);
  // 0 changes now means one of two things, and they are worth distinguishing for the operator:
  // the row vanished / is not portal-origin (404 — the pre-read said otherwise, so it was deleted
  // between the two statements), or we LOST THE RACE and another writer moved the state after our
  // read (409 — retrying is meaningful; 404 would tell the user the job doesn't exist, which is a
  // lie). Re-read to tell them apart rather than folding both into one misleading code.
  if ((res[0].meta.changes ?? 0) === 0) {
    const now = await c.env.DB
      .prepare("SELECT archive_state FROM jobs WHERE job_id=?1 AND origin='portal'")
      .bind(jobId)
      .first<{ archive_state: string }>();
    if (!now) return c.json({ error: "not_found" }, 404);
    return c.json({ error: "archive_in_flight" }, 409);
  }

  return c.json(
    { ok: true, job_id: jobId, archive: { state: "requested", direction } },
    200,
  );
}
