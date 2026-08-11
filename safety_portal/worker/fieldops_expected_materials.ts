import type { Context } from "hono";
import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
import type { Env, Vars } from "./types";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { requireDailyReportRole, requireJob, requireJobScope } from "./fieldops_scope";
import { pacificDateString } from "./fieldops_recurrence";
import type {
  ExpectedMaterialRow,
  ExpectedMaterialsResponse,
  MaterialReceiptEventRow,
  MaterialReceiptKind,
  MaterialShipmentRow,
} from "./wire-types";

// Material receipts (M1) — per-job EXPECTED-materials list (`job_expected_materials`, migration
// 0031). The office records what a job is expecting; managers confirm receipt against that list.
// Send-free throughout (D1 status flips only — no transmission); bound params only; every mutation
// lands with its audit_log row in ONE D1 batch (W4).
//
//   • Expectation CRUD (cap.materials.manage — admin): add (catalog-pick OR free-text) / edit /
//     seq-reorder / deactivate (soft, active=0). material_id, when given, is validated against an
//     ACTIVE material_catalog row (the 0019 type vocabulary); description is REQUIRED when
//     material_id is null. Edits are confined to status='expected' rows — a received/incident row
//     is a historical receipt record and must not be rewritten (409 not_editable); reorder (seq)
//     stays allowed on any active row so the list keeps a coherent order.
//   • GET /api/fieldops/expected-materials?job_id — cap.materials.receive, PER-JOB ownership
//     scope for non-admins (the /daily-form/status pattern: the actor's linked ACTIVE
//     personnel.current_job must equal job_id, else 403 forbidden_job); cap.jobtracker.manage /
//     cap.materials.manage holders (admins) may query any job. Returns active rows in seq order
//     with display fields: the resolved catalog name for catalog rows, and received_by resolved
//     to the personnel DISPLAY NAME only (W9 — an unmatched account yields NULL, never the raw
//     username; the stored username never leaves the Worker).
//   • POST /api/fieldops/expected-material/:id/receive and .../:id/flag-incident —
//     cap.materials.receive + the SAME per-job scope. The status transition is guarded IN-WHERE
//     (status='expected'): the UPDATE flips expected→received (or →incident), stamps
//     received_at + the acting account, and the audit INSERT is conditional on changes()=1 in the
//     same batch — a repeat (or a lost race) is a clean 409 with NO second stamp and NO second
//     audit row. flag-incident additionally REQUIRES a note (why it's an incident).
//     M2 wires these two routes into the daily form (the D.13 deliveries region: "Confirm
//     receipt" + "Report material incident →" alongside filing the material-incident form);
//     in M1 they are exercised by tests + the read surface shows the resulting state.

type Ctx = Context<{ Bindings: Env; Variables: Vars }>;

const MAX_DESCRIPTION = 256;
const MAX_UNIT = 32;
const MAX_NOTE = 500;
const MAX_SEQ = 1_000_000;
const MAX_QTY = 1_000_000_000;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
// Materials tracking (PR2, 0059). Sized against the real manifests: the sample BOM part numbers
// run to ~14 chars and BOL/load numbers ("LD0867264") to ~10, so these are generous ceilings that
// still bound an untrusted write.
const MAX_PART_NUMBER = 64;
const MAX_CATEGORY = 64;
const MAX_BOL = 64;
const MAX_CARRIER = 64;
const RECEIPT_KINDS: readonly MaterialReceiptKind[] = ["delivered", "partial", "not_delivered"];

/** The derived receipt rollup, as a SQL fragment over an aliased `jem` row.
 *
 *  ONE definition on purpose: the SPA read route below AND the §51 material-list snapshot in
 *  index.ts both project it, and a drift between them would put a different delivery state on the
 *  portal than on the operator's Smartsheet.
 *
 *  `ORDER BY e.id DESC` — APPEND order, not event_date. A back-dated correction is DISPLAYED on
 *  its own date but must never rewrite "what the field most recently told us". `id` is a unique
 *  autoincrement, so no tie-break is needed. SUM over zero rows yields NULL — the honest
 *  "nothing recorded", deliberately distinct from a recorded 0. Literal SQL, no interpolation. */
export const RECEIPT_ROLLUP_SQL = `
  (SELECT e.kind FROM material_receipt_events e WHERE e.line_id = jem.id ORDER BY e.id DESC LIMIT 1) AS receipt_status,
  (SELECT SUM(e.qty) FROM material_receipt_events e WHERE e.line_id = jem.id) AS qty_received_total`;

// The two caps that bypass the per-job ownership scope (admins hold both; a manager/submitter
// holds neither) — the same admin set the /daily-form/status scope recognizes, plus
// cap.materials.manage (the office editor of this very list may naturally see any job's list).
const SCOPE_BYPASS_CAPS = ["cap.jobtracker.manage", "cap.materials.manage"] as const;

function badId(c: Ctx): number | null {
  const id = parseInt(c.req.param("id") ?? "", 10);
  return isNaN(id) ? null : id;
}

// requireJob / requireJobScope — shared scope machinery, see fieldops_scope.ts (extracted from the
// local copies this module used to carry; contracts unchanged; SCOPE_BYPASS_CAPS above stays THIS
// module's own divergent admin set, passed explicitly at each gate site).

// Shared expectation-field validation for create + update (content fields; seq is create-only —
// reorder has its own route). Returns the cleaned tuple or an error string. material_id is only
// SHAPE-checked here; its live-catalog check is async and runs at the call site.
// EXPORTED for the PR3b manifest-import commit route: a bulk-imported line must run the
// IDENTICAL validation a hand-authored one does, so the importer reuses this validator and
// this type rather than re-deriving the bounds (which is how two paths drift apart).
export type ExpectationFields = {
  material_id: number | null;
  description: string | null;
  qty: number | null;
  unit: string | null;
  expected_date: string | null;
  part_number: string | null;
  category: string | null;
  expected_ship_date: string | null;
};

/** Trim + bound an optional free-text field. Empty (or all-whitespace) collapses to null so a
 *  cleared input stores NULL rather than "". */
function optText(value: unknown, max: number): string | null | "invalid" {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string" || value.length > max) return "invalid";
  const t = value.trim();
  return t.length ? t : null;
}

/** Validate an optional YYYY-MM-DD date field. */
function optDate(value: unknown): string | null | "invalid" {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "string" || !DATE_RE.test(value)) return "invalid";
  return value;
}
/** EXPORTED for the PR3b manifest-import commit route (see the ExpectationFields note).
 *  Returns the cleaned fields, or a plain error-CODE string the caller renders as
 *  `{ error: <code> }` 400 — importers additionally report the offending row index. */
export function readExpectationFields(body: Record<string, unknown>): ExpectationFields | string {
  let material_id: number | null = null;
  if (body.material_id !== undefined && body.material_id !== null) {
    if (typeof body.material_id !== "number" || !Number.isInteger(body.material_id) || body.material_id < 1) {
      return "invalid_material_id";
    }
    material_id = body.material_id;
  }
  let description: string | null = null;
  if (body.description !== undefined && body.description !== null) {
    if (typeof body.description !== "string") return "invalid_description";
    const t = body.description.trim();
    if (t.length > MAX_DESCRIPTION) return "invalid_description";
    description = t.length ? t : null;
  }
  // Free-text rows carry their identity in description — required when no catalog pick.
  if (material_id === null && description === null) return "description_required";
  let qty: number | null = null;
  if (body.qty !== undefined && body.qty !== null && body.qty !== "") {
    if (typeof body.qty !== "number" || !Number.isFinite(body.qty) || body.qty <= 0 || body.qty > MAX_QTY) {
      return "invalid_qty";
    }
    qty = body.qty;
  }
  let unit: string | null = null;
  if (body.unit !== undefined && body.unit !== null) {
    if (typeof body.unit !== "string" || body.unit.length > MAX_UNIT) return "invalid_unit";
    const t = body.unit.trim();
    unit = t.length ? t : null;
  }
  // expected_date KEEPS its 0031 meaning — the expected DELIVERY date (see migration 0059).
  const expected_date = optDate(body.expected_date);
  if (expected_date === "invalid") return "invalid_expected_date";
  const expected_ship_date = optDate(body.expected_ship_date);
  if (expected_ship_date === "invalid") return "invalid_expected_ship_date";
  const part_number = optText(body.part_number, MAX_PART_NUMBER);
  if (part_number === "invalid") return "invalid_part_number";
  const category = optText(body.category, MAX_CATEGORY);
  if (category === "invalid") return "invalid_category";
  return { material_id, description, qty, unit, expected_date, part_number, category, expected_ship_date };
}

// material_id, when given, must name an ACTIVE catalog type (a retired type must not gain new
// expectations; existing rows referencing a later-retired type are untouched — soft-ref posture).
// EXPORTED for the PR3b manifest-import commit route. NOTE it is ONE D1 round-trip per call, so a
// bulk importer must pre-resolve the DISTINCT non-null material_ids rather than calling per row.
export async function catalogIdValid(c: Ctx, materialId: number | null): Promise<boolean> {
  if (materialId === null) return true;
  const row = await c.env.DB.prepare("SELECT id FROM material_catalog WHERE id = ?1 AND active = 1")
    .bind(materialId)
    .first();
  return row !== null;
}

async function readJsonBody(c: Ctx): Promise<Record<string, unknown> | null> {
  let body: unknown;
  try {
    body = await c.req.json();
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  return body as Record<string, unknown>;
}

// receive/flag-incident accept an OPTIONAL body ({} / absent both fine) — read text-first so an
// empty POST doesn't 400 on JSON.parse.
async function readOptionalJsonBody(c: Ctx): Promise<Record<string, unknown> | null> {
  const raw = await c.req.text();
  if (!raw.trim()) return {};
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  return body as Record<string, unknown>;
}

// Shared action-field validation for receive + flag-incident: optional qty_received, note
// (REQUIRED for incident — enforced at the call site by noteRequired).
type ActionFields = { qty_received: number | null; note: string | null };
function readActionFields(body: Record<string, unknown>): ActionFields | string {
  let qty_received: number | null = null;
  if (body.qty_received !== undefined && body.qty_received !== null && body.qty_received !== "") {
    if (
      typeof body.qty_received !== "number" ||
      !Number.isFinite(body.qty_received) ||
      body.qty_received <= 0 ||
      body.qty_received > MAX_QTY
    ) {
      return "invalid_qty_received";
    }
    qty_received = body.qty_received;
  }
  let note: string | null = null;
  if (body.note !== undefined && body.note !== null) {
    if (typeof body.note !== "string" || body.note.length > MAX_NOTE) return "invalid_note";
    const t = body.note.trim();
    note = t.length ? t : null;
  }
  return { qty_received, note };
}

// The read row — single-sourced as ExpectedMaterialRow in wire-types.ts (the SPA re-exports the
// same type, so a drift here fails the typecheck on both sides).

export function registerExpectedMaterialsRoutes(app: FieldopsApp, gates: FieldopsGates): void {
  // ── GET /api/fieldops/expected-materials?job_id — the job's active expectation list. ─────────────
  // cap.materials.receive + the per-job ownership scope (bypass: jobtracker.manage /
  // materials.manage). Active rows, seq order. Display fields only: material_name is the resolved
  // catalog model_id (NULL for free-text rows); received_by_name is DISPLAY-NAME-ONLY (W9 — the
  // stored account username is never returned; an unmatched account yields NULL).
  app.get(
    "/api/fieldops/expected-materials",
    gates.requireSession,
    gates.requireCapability("cap.materials.receive"),
    async (c) => {
      const jobId = c.req.query("job_id") ?? "";
      const jobErr = await requireJob(c, jobId); // 400 bad shape / 404 unknown job
      if (jobErr) return jobErr;
      const scopeErr = await requireJobScope(c, jobId, SCOPE_BYPASS_CAPS); // 403 forbidden_job outside own placement
      if (scopeErr) return scopeErr;

      // PR2 — one read, three projections, ONE batch. A separate "materials page" read would be
      // free to drift from the daily form's read; keeping one wire shape is what stops the two
      // surfaces disagreeing about a delivery.
      const [lines, shipments, events] = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `SELECT jem.id, jem.material_id,
                    (SELECT mc.model_id FROM material_catalog mc WHERE mc.id = jem.material_id) AS material_name,
                    jem.description, jem.qty, jem.unit, jem.expected_date, jem.status,
                    jem.received_at,
                    (SELECT p.name FROM personnel p WHERE p.username = jem.received_by ORDER BY p.id ASC LIMIT 1)
                      AS received_by_name,
                    jem.qty_received, jem.note, jem.seq, jem.line_uuid,
                    jem.part_number, jem.category, jem.expected_ship_date,
                    ${RECEIPT_ROLLUP_SQL}
             FROM job_expected_materials jem
             WHERE jem.job_id = ?1 AND jem.active = 1
             ORDER BY jem.seq ASC, jem.id ASC
             LIMIT 500`,
          )
          .bind(jobId),
        c.env.DB
          .prepare(
            `SELECT s.id, s.line_id, s.part_number, s.bol_number, s.carrier, s.qty, s.unit,
                    s.ship_date, s.delivery_date, s.seq, s.source
             FROM material_shipments s
             WHERE s.job_id = ?1 AND s.active = 1
             ORDER BY s.line_id ASC, s.seq ASC, s.id ASC
             LIMIT 1000`,
          )
          .bind(jobId),
        // Newest first per line. actor resolves to the personnel DISPLAY NAME only (W9) — the
        // stored account username never leaves the Worker.
        c.env.DB
          .prepare(
            `SELECT e.id, e.line_id, e.shipment_id, e.kind, e.qty, e.note, e.event_date, e.created_at,
                    (SELECT p.name FROM personnel p WHERE p.username = e.actor ORDER BY p.id ASC LIMIT 1)
                      AS actor_name
             FROM material_receipt_events e
             WHERE e.job_id = ?1
             ORDER BY e.line_id ASC, e.id DESC
             LIMIT 2000`,
          )
          .bind(jobId),
      ]);
      const payload: ExpectedMaterialsResponse = {
        expected_materials: (lines.results ?? []) as ExpectedMaterialRow[],
        shipments: (shipments.results ?? []) as MaterialShipmentRow[],
        receipt_events: (events.results ?? []) as MaterialReceiptEventRow[],
      };
      return c.json(payload, 200);
    },
  );

  // ── POST /api/fieldops/expected-material — add an expectation (office; cap.materials.manage). ────
  // Catalog-pick (material_id, validated ACTIVE) OR free-text (description required). seq accepted
  // at create (the UI seeds max+10); mutation + audit in ONE batch (W4).
  app.post(
    "/api/fieldops/expected-material",
    gates.requireSession,
    gates.requireCapability("cap.materials.manage"),
    async (c) => {
      const body = await readJsonBody(c);
      if (body === null) return c.json({ error: "bad_request" }, 400);
      const jobId = typeof body.job_id === "string" ? body.job_id : "";
      const jobErr = await requireJob(c, jobId);
      if (jobErr) return jobErr;
      const f = readExpectationFields(body);
      if (typeof f === "string") return c.json({ error: f }, 400);
      if (!(await catalogIdValid(c, f.material_id))) return c.json({ error: "invalid_material_id" }, 400);
      let seq = 0;
      if (body.seq !== undefined) {
        if (typeof body.seq !== "number" || !Number.isInteger(body.seq) || body.seq < 0 || body.seq > MAX_SEQ) {
          return c.json({ error: "invalid_seq" }, 400);
        }
        seq = body.seq;
      }

      const actor = c.get("session").username;
      // line_uuid — the stable per-line mirror key (0039) the M2 Material List up-sync finds/upserts
      // by. Minted here at the ONLY insert path so every authored line carries a distinct uuid.
      // `unplanned` is NOT set here: this is the office ON-MANIFEST add (cap.materials.manage), so it
      // keeps the schema default 0. When an OFF-MANIFEST field-add path lands it sets unplanned=1 and
      // the mirror already surfaces it ("Unplanned = Yes"); no such path exists in M2 (out of scope).
      const lineUuid = crypto.randomUUID();
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `INSERT INTO job_expected_materials
               (job_id, material_id, description, qty, unit, expected_date, seq, line_uuid,
                part_number, category, expected_ship_date)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) RETURNING id`,
          )
          .bind(
            jobId, f.material_id, f.description, f.qty, f.unit, f.expected_date, seq, lineUuid,
            f.part_number, f.category, f.expected_ship_date,
          ),
        auditStmt(c, actor, "expected_material_create", jobId, {
          job_id: jobId,
          material_id: f.material_id,
          description: f.description,
          qty: f.qty,
        }),
      ]);
      const newId = (res[0].results?.[0] as { id: number } | undefined)?.id ?? null;
      return c.json({ ok: true, id: newId }, 201);
    },
  );

  // ── POST /api/fieldops/expected-material/:id/update — edit the content fields (office). ──────────
  // Full-replace of material_id/description/qty/unit/expected_date, confined to status='expected'
  // rows (a received/incident row is a receipt record — 409 not_editable). Conditional audit in the
  // same batch (WHERE changes()=1), so a no-op guard miss never writes a lying audit row.
  app.post(
    "/api/fieldops/expected-material/:id/update",
    gates.requireSession,
    gates.requireCapability("cap.materials.manage"),
    async (c) => {
      const id = badId(c);
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const body = await readJsonBody(c);
      if (body === null) return c.json({ error: "bad_request" }, 400);
      const f = readExpectationFields(body);
      if (typeof f === "string") return c.json({ error: f }, 400);
      if (!(await catalogIdValid(c, f.material_id))) return c.json({ error: "invalid_material_id" }, 400);

      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `UPDATE job_expected_materials
             SET material_id = ?2, description = ?3, qty = ?4, unit = ?5, expected_date = ?6,
                 part_number = ?7, category = ?8, expected_ship_date = ?9
             WHERE id = ?1 AND active = 1 AND status = 'expected'`,
          )
          .bind(
            id, f.material_id, f.description, f.qty, f.unit, f.expected_date,
            f.part_number, f.category, f.expected_ship_date,
          ),
        auditStmtIfChanged(c, actor, "expected_material_update", String(id), { expectation_id: id, material_id: f.material_id, description: f.description }),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) {
        // 0 changes = unknown/deactivated (404) or already received/incident (409 — say which).
        const row = await c.env.DB.prepare("SELECT status FROM job_expected_materials WHERE id = ?1 AND active = 1").bind(id).first<{ status: string }>();
        return row ? c.json({ error: "not_editable", status: row.status }, 409) : c.json({ error: "not_found" }, 404);
      }
      return c.json({ ok: true, id }, 200);
    },
  );

  // ── POST /api/fieldops/expected-material/:id/seq — reorder (office). ─────────────────────────────
  // seq-only write, allowed on ANY active row (received/incident rows keep their place in a
  // reordered list — content edits stay locked above). The UI drives this with the checklist
  // planRenumber convention (10/20/30 renumber; one call per changed row).
  app.post(
    "/api/fieldops/expected-material/:id/seq",
    gates.requireSession,
    gates.requireCapability("cap.materials.manage"),
    async (c) => {
      const id = badId(c);
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const body = await readJsonBody(c);
      if (body === null) return c.json({ error: "bad_request" }, 400);
      if (typeof body.seq !== "number" || !Number.isInteger(body.seq) || body.seq < 0 || body.seq > MAX_SEQ) {
        return c.json({ error: "invalid_seq" }, 400);
      }

      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare("UPDATE job_expected_materials SET seq = ?2 WHERE id = ?1 AND active = 1")
          .bind(id, body.seq),
        auditStmtIfChanged(c, actor, "expected_material_reorder", String(id), { expectation_id: id, seq: body.seq }),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
      return c.json({ ok: true, id }, 200);
    },
  );

  // ── POST /api/fieldops/expected-material/:id/delete — deactivate (office; soft, idempotent). ─────
  // active=0 keeps the row (a received/incident row is history). Mirrors the catalog retire shape:
  // second call → 200 already_inactive with NO second audit.
  app.post(
    "/api/fieldops/expected-material/:id/delete",
    gates.requireSession,
    gates.requireCapability("cap.materials.manage"),
    async (c) => {
      const id = badId(c);
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB.prepare("UPDATE job_expected_materials SET active = 0 WHERE id = ?1 AND active = 1").bind(id),
        auditStmtIfChanged(c, actor, "expected_material_deactivate", String(id), { expectation_id: id }),
        // CASCADE (2026-08-11): a line's scheduled loads die with it. They were invisible
        // orphans before — the shipments read is job-scoped while the SPA buckets by
        // line, so a deleted line's loads were still fetched, still counted against the
        // read LIMIT, rendered nowhere, and were unreachable except by purge-job. Guarded
        // on the line actually deactivating IN THIS STATEMENT's view (the loads of an
        // already-inactive line were cascaded by the first call).
        c.env.DB
          .prepare(
            "UPDATE material_shipments SET active = 0 " +
              "WHERE line_id = ?1 AND active = 1 " +
              "AND EXISTS (SELECT 1 FROM job_expected_materials j WHERE j.id = ?1 AND j.active = 0)",
          )
          .bind(id),
        // The cascade's OWN audit row (W4 — every mutation records itself). Inline rather
        // than auditStmtIfChanged because that helper's predicate is changes()=1 and a
        // cascade legitimately deactivates SEVERAL loads at once; >=1 records the sweep,
        // 0 (no loads, or already cascaded) records nothing.
        c.env.DB
          .prepare(
            "INSERT INTO audit_log (actor_username, action, target_username, detail) " +
              "SELECT ?1, 'material_shipment_cascade_deactivate', ?2, ?3 WHERE changes() >= 1",
          )
          .bind(actor, String(id), JSON.stringify({ expectation_id: id })),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) {
        const row = await c.env.DB.prepare("SELECT id FROM job_expected_materials WHERE id = ?1").bind(id).first();
        return row ? c.json({ ok: true, id, already_inactive: true }, 200) : c.json({ error: "not_found" }, 404);
      }
      return c.json({ ok: true, id }, 200);
    },
  );

  // ── PR2 receipt EVENTS ───────────────────────────────────────────────────────────────────────
  // The three-way delivery mark. Unlike the M1 one-shot flip below, this APPENDS to the
  // material_receipt_events ledger, so a line can be marked partial today and delivered next week
  // — the whole point of 0059. The line's legacy status/received_*/qty_received columns are
  // refreshed in the same batch as a coarse projection (see the migration header).

  type ReceiptFields = {
    kind: MaterialReceiptKind;
    qty: number | null;
    note: string | null;
    event_date: string;
    shipment_id: number | null;
  };

  function readReceiptFields(body: Record<string, unknown>): ReceiptFields | string {
    const kind = body.kind;
    // `invalid_receipt_kind`, not `invalid_kind`: that code already means "pick a valid
    // daily-requirement kind" in src/lib/errorCopy.ts, and one code cannot carry two meanings.
    if (typeof kind !== "string" || !RECEIPT_KINDS.includes(kind as MaterialReceiptKind)) {
      return "invalid_receipt_kind";
    }
    let qty: number | null = null;
    if (body.qty !== undefined && body.qty !== null && body.qty !== "") {
      if (typeof body.qty !== "number" || !Number.isFinite(body.qty) || body.qty <= 0 || body.qty > MAX_QTY) {
        return "invalid_qty";
      }
      qty = body.qty;
    }
    // A "nothing arrived" mark carrying a received quantity is incoherent — refuse it rather than
    // silently drop the number and let the ledger sum disagree with what was typed.
    if (kind === "not_delivered" && qty !== null) return "invalid_qty";
    const note = optText(body.note, MAX_NOTE);
    if (note === "invalid") return "invalid_note";
    // A negative report must say why — the same posture flag-incident already takes.
    if (kind === "not_delivered" && note === null) return "note_required";
    const event_date = optDate(body.event_date);
    if (event_date === "invalid") return "invalid_event_date";
    return {
      kind: kind as MaterialReceiptKind,
      qty,
      note,
      // Pacific wall-clock — the crews' day, matching every other date the daily surfaces stamp.
      event_date: event_date ?? pacificDateString(Date.now()),
      shipment_id: null, // resolved + validated against the line at the call site
    };
  }

  /** Shared implementation for /receipt and the re-pointed /receive. */
  async function appendReceiptEvent(c: Ctx, forcedKind?: MaterialReceiptKind): Promise<Response> {
    // Role-checked FIRST — an ineligible role must learn nothing about row existence.
    const roleErr = requireDailyReportRole(c);
    if (roleErr) return roleErr;
    const id = badId(c);
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const body = await readOptionalJsonBody(c);
    if (body === null) return c.json({ error: "bad_request" }, 400);
    // /receive is the legacy alias: it carries no `kind` and speaks `qty_received`, not `qty`.
    const merged = forcedKind
      ? { ...body, kind: forcedKind, qty: body.qty ?? body.qty_received }
      : body;
    const f = readReceiptFields(merged);
    if (typeof f === "string") return c.json({ error: f }, 400);

    // Resolve the row first — its job_id anchors the ownership-scope check.
    const row = await c.env.DB.prepare(
      "SELECT id, job_id FROM job_expected_materials WHERE id = ?1 AND active = 1",
    )
      .bind(id)
      .first<{ id: number; job_id: string }>();
    if (!row) return c.json({ error: "not_found" }, 404);
    const scopeErr = await requireJobScope(c, row.job_id, SCOPE_BYPASS_CAPS);
    if (scopeErr) return scopeErr;

    // An event MAY name the load it closed — but only a load belonging to THIS line.
    let shipmentId: number | null = null;
    if (merged.shipment_id !== undefined && merged.shipment_id !== null) {
      if (typeof merged.shipment_id !== "number" || !Number.isInteger(merged.shipment_id)) {
        return c.json({ error: "invalid_shipment_id" }, 400);
      }
      const ship = await c.env.DB.prepare(
        "SELECT id FROM material_shipments WHERE id = ?1 AND line_id = ?2 AND active = 1",
      )
        .bind(merged.shipment_id, id)
        .first();
      if (!ship) return c.json({ error: "invalid_shipment_id" }, 400);
      shipmentId = merged.shipment_id;
    }

    const actor = c.get("session").username;
    const eventUuid = crypto.randomUUID();
    let res;
    try {
      res = await c.env.DB.batch([
        // 1. GUARDED append. The guard is the INSERT…SELECT's own WHERE: the line must still exist
        //    and be active, so a row deactivated between the resolve above and here writes nothing.
        c.env.DB
          .prepare(
            `INSERT INTO material_receipt_events
               (event_uuid, line_id, job_id, shipment_id, kind, qty, note, event_date, actor)
             SELECT ?2, jem.id, jem.job_id, ?3, ?4, ?5, ?6, ?7, ?8
               FROM job_expected_materials jem
              WHERE jem.id = ?1 AND jem.active = 1
                -- The shipment check is re-asserted HERE, not left to the SELECT above: a
                -- concurrent /material-shipment/:id/delete between that read and this batch would
                -- otherwise land an event pointing at a just-deactivated load. Matches this
                -- module's in-WHERE guard discipline (the line's own active=1 already is atomic).
                AND (?3 IS NULL OR EXISTS (
                      SELECT 1 FROM material_shipments s
                       WHERE s.id = ?3 AND s.line_id = jem.id AND s.active = 1))`,
          )
          .bind(id, eventUuid, shipmentId, f.kind, f.qty, f.note, f.event_date, actor),
        // 2. Audit IMMEDIATELY after the mutation it describes — changes() reads the LAST
        //    data-modifying statement, so anything between them would break the W4 contract.
        auditStmtIfChanged(c, actor, "expected_material_receipt", row.job_id, {
          expectation_id: id,
          job_id: row.job_id,
          kind: f.kind,
          qty: f.qty,
          shipment_id: shipmentId,
        }),
        // 3. Legacy projection refresh. qty_received is RECOMPUTED from the ledger, never
        //    incremented, so it cannot drift from it — and that stays true on a FLAGGED
        //    line: the row runs this update too, so Delivered Qty keeps tracking the
        //    ledger while only STATUS is sticky (the CASE's first arm — an incident is
        //    cleared exclusively by the explicit resolve route below, never by a delivery
        //    mark: a delivered-but-damaged pallet is still a problem). The old shape
        //    guarded the WHOLE statement `status <> 'incident'`, which froze
        //    qty_received/note on flag and let the two §51 sheets (Material List vs the
        //    Receipts ledger) disagree forever. `note` is COALESCEd so an un-noted mark
        //    keeps the last note instead of blanking it on the operator's sheet.
        c.env.DB
          .prepare(
            `UPDATE job_expected_materials
                SET status       = CASE WHEN status = 'incident' THEN 'incident'
                                        WHEN ?2 = 'delivered' THEN 'received'
                                        ELSE 'expected' END,
                    received_at  = unixepoch(),
                    received_by  = ?3,
                    note         = COALESCE(?4, note),
                    qty_received = (SELECT SUM(e.qty) FROM material_receipt_events e WHERE e.line_id = ?1)
              WHERE id = ?1 AND active = 1`,
          )
          .bind(id, f.kind, actor, f.note),
      ]);
    } catch (e) {
      // event_uuid collision — a genuinely lost race, not a client error worth 500ing over.
      if (isUniqueViolation(e)) return c.json({ error: "duplicate_event" }, 409);
      throw e;
    }
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
    // Report the ACTUAL persisted status, never a literal. Statement 3's CASE keeps a
    // flagged line at 'incident' — the sticky rule — so on a flagged delivery the row's
    // quantities move while its status does not. Returning an assumed "received" there
    // would tell the field crew a flagged delivery was resolved while the §51 Material
    // List mirror (which reads this same column) still showed the problem: the two
    // surfaces would disagree, silently.
    const after = await c.env.DB.prepare(
      "SELECT status FROM job_expected_materials WHERE id = ?1",
    )
      .bind(id)
      .first<{ status: string }>();
    return c.json(
      {
        ok: true,
        id,
        event_id: res[0].meta.last_row_id ?? null,
        kind: f.kind,
        status: after?.status ?? null,
      },
      200,
    );
  }

  // ── POST /api/fieldops/expected-material/:id/receipt — the three-way mark (manager/admin). ───────
  // Delivered / Partially delivered / Not delivered, each with an optional note + qty and an
  // optional shipment reference. Repeatable BY DESIGN: partial → partial → delivered is the
  // normal path for a part number arriving across several loads.
  app.post(
    "/api/fieldops/expected-material/:id/receipt",
    gates.requireSession,
    gates.requireCapability("cap.materials.receive"),
    (c) => appendReceiptEvent(c),
  );

  // Shared flag-incident implementation (M1). Flips status FROM 'expected' with the guard IN the
  // WHERE clause and the audit conditional on changes()=1 in the SAME batch (W4) — a repeat is a
  // 409 with exactly one stamp and exactly one audit row ever written.
  //
  // NOTE: /receive no longer routes through here. Under 0059 it appends a 'delivered' EVENT (see
  // appendReceiptEvent above), because the one-shot 409-on-repeat contract is exactly what the
  // partial-delivery model had to lift. flag-incident KEEPS the one-shot posture: an incident is a
  // distinct axis with its own ledger (M3), and re-flagging is not a workflow anyone asked for.
  async function actionExpectation(
    c: Ctx,
    nextStatus: "received" | "incident",
    auditAction: string,
    noteRequired: boolean,
  ): Promise<Response> {
    // Daily-report role gate (directive 2026-07-03): the receipt actions are DAILY-FORM surfaces
    // (M2 wires them into the Daily tab exclusively — the Job Tracker section is read-only for
    // receive-cap holders), and cap.materials.receive is held by all three roles. The READ above
    // deliberately keeps its cap-only gate: the Job Tracker's Expected-materials section is a
    // live submitter consumer of the list. Role-checked first — an ineligible role learns
    // nothing about row existence.
    const roleErr = requireDailyReportRole(c);
    if (roleErr) return roleErr;
    const id = badId(c);
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const body = await readOptionalJsonBody(c);
    if (body === null) return c.json({ error: "bad_request" }, 400);
    const f = readActionFields(body);
    if (typeof f === "string") return c.json({ error: f }, 400);
    if (noteRequired && f.note === null) return c.json({ error: "note_required" }, 400);

    // Resolve the row first — its job_id anchors the ownership-scope check.
    const row = await c.env.DB.prepare(
      "SELECT id, job_id FROM job_expected_materials WHERE id = ?1 AND active = 1",
    )
      .bind(id)
      .first<{ id: number; job_id: string }>();
    if (!row) return c.json({ error: "not_found" }, 404);
    const scopeErr = await requireJobScope(c, row.job_id, SCOPE_BYPASS_CAPS);
    if (scopeErr) return scopeErr;

    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          // qty_received deliberately NOT written here (2026-08-11): since 0059 the
          // receipt LEDGER owns that column — the projection above recomputes it from
          // SUM(events) — so a client-supplied number on flag would clobber the
          // ledger-derived value and the two §51 sheets would disagree until the next
          // mark. A quantity that matters on a flag belongs in the note and the incident
          // form; the body field is still accepted (wire-compat) and still audited, but
          // it no longer reaches the row.
          `UPDATE job_expected_materials
           SET status = ?2, received_at = unixepoch(), received_by = ?3, note = ?4
           WHERE id = ?1 AND active = 1 AND status = 'expected'`,
        )
        .bind(id, nextStatus, actor, f.note),
      auditStmtIfChanged(c, actor, auditAction, row.job_id, { expectation_id: id, job_id: row.job_id, qty_received: f.qty_received }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "already_actioned" }, 409);
    return c.json({ ok: true, id, status: nextStatus }, 200);
  }

  // ── POST /api/fieldops/expected-material/:id/receive — confirm receipt (manager/field). ──────────
  // KEPT for the daily form's existing "Confirm receipt" button: same path, same cap, same gate
  // stack, same `{qty_received?, note?}` body, same `{ok, id, status:"received"}`-shaped success.
  // Internally it is now a 'delivered' EVENT (0059), so the ledger has ONE writer.
  //
  // INTENTIONAL BEHAVIOUR CHANGE: a repeat call is now 200 + a second delivered event, where M1
  // returned 409 already_actioned. That one-shot guard is precisely what made a partial delivery
  // impossible to complete; the client's per-row busy flag still stops double-taps. Documented in
  // docs/runbooks/job_materials.md and in material_catalog_admin.md's changed-behaviour note.
  app.post(
    "/api/fieldops/expected-material/:id/receive",
    gates.requireSession,
    gates.requireCapability("cap.materials.receive"),
    async (c) => {
      const res = await appendReceiptEvent(c, "delivered");
      if (res.status !== 200) return res;
      const body = (await res.json()) as {
        id: number;
        event_id: number | null;
        status: string | null;
      };
      // Legacy success shape — the daily form reads `status` off this response. Pass through the
      // PERSISTED status rather than asserting "received": on an incident-flagged line the sticky
      // guard leaves it 'incident', and claiming otherwise would show the crew a resolved delivery
      // that the office's Material List still flags as a problem.
      return c.json({ ok: true, id: body.id, status: body.status, event_id: body.event_id }, 200);
    },
  );

  // ── POST /api/fieldops/expected-material/:id/flag-incident — flag a delivery problem. ────────────
  // note REQUIRED (what's wrong). M2 pairs this with filing the material-incident form.
  app.post(
    "/api/fieldops/expected-material/:id/flag-incident",
    gates.requireSession,
    gates.requireCapability("cap.materials.receive"),
    (c) => actionExpectation(c, "incident", "expected_material_incident", true),
  );

  // ── POST /api/fieldops/expected-material/:id/resolve-incident — clear the flag (2026-08-11). ─────
  // Until now NOTHING could move a line off 'incident' — the sticky rule had no counterpart, so a
  // shortfall that was later made good showed as a problem forever on every surface, and
  // material_incidents.py documented a Line-Status resolution signal no route could produce. This
  // is that counterpart, and it is DELIBERATELY an explicit human act (operator decision
  // 2026-08-11): a delivery mark still never clears the flag — a delivered-but-damaged pallet is
  // still a problem — only a person saying "this problem is resolved", with a REQUIRED note
  // saying how, does. Status lands where the delivery LEDGER says it should: latest event
  // 'delivered' → received, anything else (or no events) → expected — the same latest-event-wins
  // rule the read rollup uses, so the pill and the sheets agree on what resolution looks like.
  app.post(
    "/api/fieldops/expected-material/:id/resolve-incident",
    gates.requireSession,
    gates.requireCapability("cap.materials.receive"),
    async (c) => {
      const roleErr = requireDailyReportRole(c);
      if (roleErr) return roleErr;
      const id = badId(c);
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const body = await readOptionalJsonBody(c);
      if (body === null) return c.json({ error: "bad_request" }, 400);
      const f = readActionFields(body);
      if (typeof f === "string") return c.json({ error: f }, 400);
      if (f.note === null) return c.json({ error: "note_required" }, 400);

      const row = await c.env.DB.prepare(
        "SELECT id, job_id FROM job_expected_materials WHERE id = ?1 AND active = 1",
      )
        .bind(id)
        .first<{ id: number; job_id: string }>();
      if (!row) return c.json({ error: "not_found" }, 404);
      const scopeErr = await requireJobScope(c, row.job_id, SCOPE_BYPASS_CAPS);
      if (scopeErr) return scopeErr;

      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `UPDATE job_expected_materials
                SET status = CASE
                      WHEN (SELECT e.kind FROM material_receipt_events e
                             WHERE e.line_id = ?1 ORDER BY e.id DESC LIMIT 1) = 'delivered'
                      THEN 'received' ELSE 'expected' END,
                    received_at = unixepoch(),
                    received_by = ?2,
                    note = ?3
              WHERE id = ?1 AND active = 1 AND status = 'incident'`,
          )
          .bind(id, actor, f.note),
        auditStmtIfChanged(c, actor, "expected_material_incident_resolve", row.job_id, {
          expectation_id: id, job_id: row.job_id,
        }),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_flagged" }, 409);
      const after = await c.env.DB.prepare(
        "SELECT status FROM job_expected_materials WHERE id = ?1",
      )
        .bind(id)
        .first<{ status: string }>();
      return c.json({ ok: true, id, status: after?.status ?? null }, 200);
    },
  );

  // ── PR2 SHIPMENT CRUD (office; cap.materials.manage) ────────────────────────────────────────
  // A shipping-log row is a LOAD, not an expectation: one part number arrives across several
  // trucks, each with its own ship date, delivery date and BOL. These routes are also PR3's
  // insert target once the manifest importer lands.

  type ShipmentFields = {
    part_number: string | null;
    bol_number: string | null;
    carrier: string | null;
    qty: number | null;
    unit: string | null;
    ship_date: string | null;
    delivery_date: string | null;
    seq: number;
  };

  function readShipmentFields(body: Record<string, unknown>): ShipmentFields | string {
    const part_number = optText(body.part_number, MAX_PART_NUMBER);
    if (part_number === "invalid") return "invalid_part_number";
    const bol_number = optText(body.bol_number, MAX_BOL);
    if (bol_number === "invalid") return "invalid_bol_number";
    const carrier = optText(body.carrier, MAX_CARRIER);
    if (carrier === "invalid") return "invalid_carrier";
    const unit = optText(body.unit, MAX_UNIT);
    if (unit === "invalid") return "invalid_unit";
    const ship_date = optDate(body.ship_date);
    if (ship_date === "invalid") return "invalid_ship_date";
    const delivery_date = optDate(body.delivery_date);
    if (delivery_date === "invalid") return "invalid_delivery_date";
    let qty: number | null = null;
    if (body.qty !== undefined && body.qty !== null && body.qty !== "") {
      if (typeof body.qty !== "number" || !Number.isFinite(body.qty) || body.qty <= 0 || body.qty > MAX_QTY) {
        return "invalid_qty";
      }
      qty = body.qty;
    }
    let seq = 0;
    if (body.seq !== undefined) {
      if (typeof body.seq !== "number" || !Number.isInteger(body.seq) || body.seq < 0 || body.seq > MAX_SEQ) {
        return "invalid_seq";
      }
      seq = body.seq;
    }
    return { part_number, bol_number, carrier, qty, unit, ship_date, delivery_date, seq };
  }

  // ── POST /api/fieldops/material-shipment — attach a load to a line. ──────────────────────────
  app.post(
    "/api/fieldops/material-shipment",
    gates.requireSession,
    gates.requireCapability("cap.materials.manage"),
    async (c) => {
      const body = await readJsonBody(c);
      if (body === null) return c.json({ error: "bad_request" }, 400);
      if (typeof body.line_id !== "number" || !Number.isInteger(body.line_id) || body.line_id < 1) {
        return c.json({ error: "invalid_line_id" }, 400);
      }
      const f = readShipmentFields(body);
      if (typeof f === "string") return c.json({ error: f }, 400);
      // Resolve the line to inherit its job_id — the denormalized copy the read scope, the prune
      // guard and the purge cascade all key on.
      const line = await c.env.DB.prepare(
        "SELECT id, job_id FROM job_expected_materials WHERE id = ?1 AND active = 1",
      )
        .bind(body.line_id)
        .first<{ id: number; job_id: string }>();
      if (!line) return c.json({ error: "not_found" }, 404);

      const actor = c.get("session").username;
      const shipmentUuid = crypto.randomUUID();
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `INSERT INTO material_shipments
               (shipment_uuid, line_id, job_id, part_number, bol_number, carrier, qty, unit,
                ship_date, delivery_date, seq, source, created_by)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'manual', ?12) RETURNING id`,
          )
          .bind(
            shipmentUuid, line.id, line.job_id, f.part_number, f.bol_number, f.carrier, f.qty,
            f.unit, f.ship_date, f.delivery_date, f.seq, actor,
          ),
        auditStmt(c, actor, "material_shipment_create", line.job_id, {
          line_id: line.id, job_id: line.job_id, bol_number: f.bol_number, qty: f.qty,
        }),
      ]);
      const newId = (res[0].results?.[0] as { id: number } | undefined)?.id ?? null;
      return c.json({ ok: true, id: newId }, 201);
    },
  );

  // ── POST /api/fieldops/material-shipment/:id/update — full-replace the content fields. ───────
  // line_id is deliberately NOT re-assignable: moving a load to another line would have to keep
  // the denormalized job_id in step. Delete and re-add instead.
  app.post(
    "/api/fieldops/material-shipment/:id/update",
    gates.requireSession,
    gates.requireCapability("cap.materials.manage"),
    async (c) => {
      const id = badId(c);
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const body = await readJsonBody(c);
      if (body === null) return c.json({ error: "bad_request" }, 400);
      const f = readShipmentFields(body);
      if (typeof f === "string") return c.json({ error: f }, 400);

      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `UPDATE material_shipments
                SET part_number = ?2, bol_number = ?3, carrier = ?4, qty = ?5, unit = ?6,
                    ship_date = ?7, delivery_date = ?8, seq = ?9
              WHERE id = ?1 AND active = 1`,
          )
          .bind(
            id, f.part_number, f.bol_number, f.carrier, f.qty, f.unit, f.ship_date,
            f.delivery_date, f.seq,
          ),
        auditStmtIfChanged(c, actor, "material_shipment_update", String(id), {
          shipment_id: id, bol_number: f.bol_number, qty: f.qty,
        }),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
      return c.json({ ok: true, id }, 200);
    },
  );

  // ── POST /api/fieldops/material-shipment/:id/delete — deactivate (soft, idempotent). ─────────
  app.post(
    "/api/fieldops/material-shipment/:id/delete",
    gates.requireSession,
    gates.requireCapability("cap.materials.manage"),
    async (c) => {
      const id = badId(c);
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB.prepare("UPDATE material_shipments SET active = 0 WHERE id = ?1 AND active = 1").bind(id),
        auditStmtIfChanged(c, actor, "material_shipment_deactivate", String(id), { shipment_id: id }),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) {
        const row = await c.env.DB.prepare("SELECT id FROM material_shipments WHERE id = ?1").bind(id).first();
        return row ? c.json({ ok: true, id, already_inactive: true }, 200) : c.json({ error: "not_found" }, 404);
      }
      return c.json({ ok: true, id }, 200);
    },
  );
}
