import type { MiddlewareHandler } from "hono";
import type { FieldopsApp } from "./fieldops_gates";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { hmacHex } from "./hmac";
import { MAX_ADDRESS } from "./constants";

// ─────────────────────────────────────────────────────────────────────────────
// RFQ composer R1 (ADR-0004, po_materials sub-lane) — worker/rfq.ts
//
// The Worker half of the outbound Request-for-Quote pipeline (the po.ts mirror):
//   - Browser tier (session + cap.po.manage): multi-vendor RFQ drafts (job +
//     ship-to snapshot + scope + due date + PRICE-FREE line items + 1..12 vendor
//     rows), draft-only full-replace update under the draft_version optimistic
//     lock, the tracker list/detail reads, generate (number allocation + rfq:v1
//     signing → queued), and cancel.
//   - Internal tier (requireRfqToken — the RFQ daemon's OWN bearer, ADR-0004
//     decision 4 / red-team #1: SEPARATE from BOTH the PO token and the estimate
//     token; a compromised extraction daemon must never reach the RFQ lane's
//     control surface, and the RFQ token opens nothing but /api/po/rfqs/internal/*):
//     the queued pull, per-vendor mark-filed (→ 'generated' when all filed), and
//     the forward-only per-vendor status-sync ('sent'/'responded' + the derived
//     rfq status).
//
// Invariants:
//   - Invariant 1: SEND-FREE, zero AI. Validates, signs, queues in D1; the Mac
//     rfq_poll daemon (R2) renders/files; the SEPARATE rfq_send lane (R3)
//     transmits only after F22-verified human approval.
//   - Invariant 2: every body shape-guarded + bounded, all SQL ?-bound, every
//     mutation atomic with its audit row (W4). NO PRICE FIELDS anywhere on this
//     surface — an RFQ asks for prices; dollars enter only through the estimate
//     importer + the human disposition (ADR-0004 decision 2). The rfq:v1 HMAC
//     (same HMAC_PAYLOAD_SECRET, own domain) signs the canonical fixed-key-order
//     JSON at generate; the Mac recomputes it byte-for-byte before rendering.
//   - Vendor rows are validated against po_vendors (must exist AND be active) at
//     draft time and are READ-ONLY against the vendor SoR (ADR decision 9): no
//     route here ever writes po_vendors, and recipients resolve at send time from
//     ITS_Vendors by Vendor Key — never from anything a document said.
//
// Numbering: rfq_number = 'RFQ-{job_no}-{NNN}', allocated at generate as
// MAX(seq)+1 over the job's allocated numbers, with the UNIQUE(rfq_number) index
// as the race backstop (migration 0056 header records why there is no counter
// table — the po_number revision-allocation pattern, simpler-correct).
// ─────────────────────────────────────────────────────────────────────────────

export type RfqGates = {
  requireSession: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  requireCapability: (cap: string) => MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  /** Bearer gate for /api/po/rfqs/internal/* — the Mac-side rfq_poll / rfq_send_poll
   *  daemons' OWN token tier (PORTAL_RFQ_API_TOKEN), privilege-separated from BOTH the
   *  PO and estimate tokens (ADR-0004 decision 4 / red-team #1). Built in index.ts next
   *  to its siblings (same fail-closed constant-time shape). */
  requireRfqToken: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
};

const CAP_PO = "cap.po.manage";
export const RFQ_HMAC_DOMAIN = "rfq:v1";
const SYSTEM_ACTOR = "system:rfq_poll";

// ── Bounds (Invariant 2) ────────────────────────────────────────────────────────
const MAX_NAME = 256;
const MAX_SHORT = 64;
const MAX_PHONE = 40;
const MAX_EMAIL = 320;
const MAX_SCOPE = 8000; // mirrors po.ts MAX_SOW
export const MAX_RFQ_LINES = 100;
const MAX_LINE_TEXT = 512;
const MAX_LINE_NOTE = 256;
const MAX_QTY = 1_000_000_000;
export const MAX_RFQ_VENDORS = 12;
const RFQ_PENDING_CAP = 25;
const LIST_CAP = 200;

const JOB_NO_RE = /^\d{4}\.\d{3}$/;
const VENDOR_KEY_RE = /^VEN-\d{6}$/;
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const STATE_RE = /^[A-Z]{2}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

const RFQ_STATUSES = new Set([
  "draft", "queued", "generated", "partially_sent", "sent", "closed", "canceled",
]);
// The per-vendor outcomes the Mac-side status-sync may stamp (pending/filed/canceled are
// Worker-owned transitions; the daemon reports only the Mac-side machine's outcomes).
const SYNCABLE_VENDOR_STATUSES = new Set(["sent", "responded"]);

// ── Small helpers (the po.ts idioms) ────────────────────────────────────────────
function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}
function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function parseIdParam(raw: string | undefined): number | null {
  const id = parseInt(raw ?? "", 10);
  return Number.isSafeInteger(id) && id > 0 && String(id) === (raw ?? "") ? id : null;
}
function optStr(v: unknown, max: number): string | null | "bad" {
  if (v === undefined || v === null) return null;
  if (typeof v !== "string") return "bad";
  const t = v.trim();
  if (t.length === 0) return null;
  return t.length <= max ? t : "bad";
}

// ── Line-item validation — PRICE-FREE by contract ───────────────────────────────
export interface RfqLine {
  position: number;
  part_number: string;
  description: string;
  qty: number | null;
  unit: string;
  line_note: string;
}

/** Parse + bound the client line array; positions are SERVER-assigned (1-based array
 *  order). NO price fields exist on the shape — anything money-like the client sends is
 *  simply ignored (never stored, never signed): the RFQ is the ask, not the answer. */
function parseLines(raw: unknown): RfqLine[] | string {
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > MAX_RFQ_LINES) return "invalid_line_items";
  const out: RfqLine[] = [];
  for (let i = 0; i < raw.length; i++) {
    const r = raw[i];
    if (!isPlainObject(r)) return "invalid_line_items";
    const part_number = str(r.part_number);
    if (part_number.length > MAX_SHORT) return "invalid_part_number";
    const description = str(r.description);
    if (description.length < 1 || description.length > MAX_LINE_TEXT) return "invalid_description";
    const unit = str(r.unit);
    if (unit.length > 32) return "invalid_unit";
    const line_note = str(r.line_note);
    if (line_note.length > MAX_LINE_NOTE) return "invalid_line_note";
    // qty: OPTIONAL (null = "quote per unit"); finite, bounded, normalized to ≤3 decimal
    // places so the canonical-JSON HMAC serialization is bit-stable across the JS/Python
    // recompute (the po.ts qty rule).
    let qty: number | null = null;
    if (r.qty !== undefined && r.qty !== null) {
      if (typeof r.qty !== "number" || !Number.isFinite(r.qty) || r.qty < 0 || r.qty > MAX_QTY) {
        return "invalid_qty";
      }
      qty = Math.round(r.qty * 1000) / 1000;
    }
    out.push({ position: i + 1, part_number, description, qty, unit, line_note });
  }
  return out;
}

// ── Draft body validation ───────────────────────────────────────────────────────
interface RfqDraftFields {
  /** The canonical JOB-###### (0075) — captured because the SPA resolves the job BY this id and
   *  was throwing it away (the exact po_estimates 0069 defect). '' when a caller omits it; the
   *  per-job Procurement read simply never matches those. Snapshot semantics, no FK. */
  job_id: string;
  job_no: string;
  /** 0070 — the Evergreen identifier's SITE segment. job_no stays two-segment (0064's model);
   *  this is what makes MH405's and OG593's RFQ numbers distinguishable. */
  site_phase: number;
  job_name: string;
  ship_to_name: string;
  ship_to_address: string;
  ship_to_city: string;
  ship_to_state: string;
  ship_to_zip: string;
  delivery_contact_name: string;
  delivery_contact_phone: string;
  delivery_contact_email: string;
  scope_text: string;
  due_date: string | null;
  lines: RfqLine[];
  vendor_keys: string[];
}

function parseRfqDraftBody(body: Record<string, unknown>): RfqDraftFields | string {
  const job_no = str(body.job_no);
  if (!JOB_NO_RE.test(job_no)) return "invalid_job_no";
  // 0075 — optional, bounded; existence is deliberately NOT checked (snapshot semantics, like
  // every other job field here: the drafting office picked the job from the live dropdown).
  const job_id = str(body.job_id);
  if (job_id.length > 64) return "invalid_job_id";
  // 0070 — same shape and bounds parseDraftBody enforces on a PO's site_phase (po.ts), so a
  // value that is legal on one procurement lane is legal on the other. job_no itself is NOT
  // widened: 0064's model is that job_no stays two-segment everywhere.
  const site_phase = body.site_phase === undefined ? 0 : body.site_phase;
  if (typeof site_phase !== "number" || !Number.isSafeInteger(site_phase) || site_phase < 0 || site_phase > 9999) {
    return "invalid_site_phase";
  }
  const job_name = str(body.job_name);
  if (job_name.length > MAX_NAME) return "invalid_job_name";
  const ship_to_name = str(body.ship_to_name);
  const ship_to_address = str(body.ship_to_address);
  const ship_to_city = str(body.ship_to_city);
  const ship_to_zip = str(body.ship_to_zip);
  if (ship_to_name.length > MAX_NAME || ship_to_address.length > MAX_ADDRESS ||
      ship_to_city.length > MAX_NAME || ship_to_zip.length > 16) {
    return "invalid_ship_to";
  }
  const ship_to_state = str(body.ship_to_state).toUpperCase();
  if (ship_to_state && !STATE_RE.test(ship_to_state)) return "invalid_ship_to_state";
  const delivery_contact_name = str(body.delivery_contact_name);
  if (delivery_contact_name.length > MAX_NAME) return "invalid_delivery_contact";
  const delivery_contact_phone = str(body.delivery_contact_phone);
  if (delivery_contact_phone.length > MAX_PHONE) return "invalid_delivery_contact";
  const delivery_contact_email = str(body.delivery_contact_email);
  if (delivery_contact_email.length > MAX_EMAIL || (delivery_contact_email && !EMAIL_RE.test(delivery_contact_email))) {
    return "invalid_delivery_contact";
  }
  const scope_text = str(body.scope_text);
  if (scope_text.length > MAX_SCOPE) return "invalid_scope_text";
  const dd = optStr(body.due_date, MAX_SHORT);
  if (dd === "bad" || (dd !== null && !DATE_RE.test(dd))) return "invalid_due_date";

  const lines = parseLines(body.line_items);
  if (typeof lines === "string") return lines;

  // vendor_keys: 1..12, each shape-valid and UNIQUE. Existence + active is a DB check the
  // routes run after this parse (400 unknown_vendor — the R1 contract).
  if (!Array.isArray(body.vendor_keys) || body.vendor_keys.length < 1 ||
      body.vendor_keys.length > MAX_RFQ_VENDORS) {
    return "invalid_vendor_keys";
  }
  const vendor_keys: string[] = [];
  const seen = new Set<string>();
  for (const v of body.vendor_keys) {
    if (typeof v !== "string" || !VENDOR_KEY_RE.test(v)) return "invalid_vendor_keys";
    if (seen.has(v)) return "duplicate_vendor_key";
    seen.add(v);
    vendor_keys.push(v);
  }

  return {
    job_id, job_no, site_phase, job_name,
    ship_to_name, ship_to_address, ship_to_city, ship_to_state, ship_to_zip,
    delivery_contact_name, delivery_contact_phone, delivery_contact_email,
    scope_text, due_date: dd, lines, vendor_keys,
  };
}

/** Every supplied key must name an ACTIVE po_vendors row (the vendor SoR is read-only
 *  here — ADR decision 9). Returns null when all resolve; else the first offender. */
async function findUnknownVendor(db: D1Database, keys: string[]): Promise<string | null> {
  const placeholders = keys.map((_, i) => `?${i + 1}`).join(",");
  const { results } = await db
    .prepare(`SELECT vendor_key FROM po_vendors WHERE active = 1 AND vendor_key IN (${placeholders})`)
    .bind(...keys)
    .all<{ vendor_key: string }>();
  const found = new Set((results ?? []).map((r) => r.vendor_key));
  for (const k of keys) if (!found.has(k)) return k;
  return null;
}

// ── HMAC canonical payload (domain 'rfq:v1') ────────────────────────────────────
// The Mac-side rfq_poll daemon (R2, shared/portal_hmac.py RFQ_DOMAIN) recomputes this
// byte-for-byte before rendering/filing. ORDER + SEPARATOR are load-bearing:
//   "rfq:v1" \n rfq_id \n rfq_number \n canonical_json
// canonical_json is JSON.stringify of the FIXED-KEY-ORDER object below (insertion order
// preserved; the Python side mirrors with json.dumps(..., separators=(",", ":")) over the
// SAME key order):
//   rfq_number, job_no, job_name, ship_to_name, ship_to_address, ship_to_city,
//   ship_to_state, ship_to_zip, delivery_contact_name, delivery_contact_phone,
//   delivery_contact_email, scope_text, due_date,
//   line_items[{position, part_number, description, qty, unit, line_note}],
//   vendor_keys (SORTED ascending — vendor-row read order must never change the signature)
// qty is a ≤3dp double (or null) whose shortest-roundtrip serialization agrees across
// JS/Python; due_date is 'YYYY-MM-DD' or null. NO price keys exist — the vitest suite
// red-lines the canonical on any *_cents/price/cost key ever appearing.
export interface RfqRow {
  id: number;
  rfq_number: string | null;
  job_no: string;
  /** 0070 — the Evergreen identifier's site segment; 0 = no site, and the composed
   *  rfq_number then omits it entirely (see the generate route). */
  site_phase: number;
  job_name: string;
  ship_to_name: string;
  ship_to_address: string;
  ship_to_city: string;
  ship_to_state: string;
  ship_to_zip: string;
  delivery_contact_name: string;
  delivery_contact_phone: string;
  delivery_contact_email: string;
  scope_text: string;
  due_date: string | null;
}

export function canonicalRfqJson(rfq: RfqRow, lines: RfqLine[], vendorKeys: string[]): string {
  return JSON.stringify({
    rfq_number: rfq.rfq_number,
    job_no: rfq.job_no,
    job_name: rfq.job_name,
    ship_to_name: rfq.ship_to_name,
    ship_to_address: rfq.ship_to_address,
    ship_to_city: rfq.ship_to_city,
    ship_to_state: rfq.ship_to_state,
    ship_to_zip: rfq.ship_to_zip,
    delivery_contact_name: rfq.delivery_contact_name,
    delivery_contact_phone: rfq.delivery_contact_phone,
    delivery_contact_email: rfq.delivery_contact_email,
    scope_text: rfq.scope_text,
    due_date: rfq.due_date,
    line_items: lines.map((l) => ({
      position: l.position,
      part_number: l.part_number,
      description: l.description,
      qty: l.qty,
      unit: l.unit,
      line_note: l.line_note,
    })),
    vendor_keys: [...vendorKeys].sort(),
  });
}

export function rfqCanonicalString(rfqId: number, rfqNumber: string, canonicalJson: string): string {
  return [RFQ_HMAC_DOMAIN, String(rfqId), rfqNumber, canonicalJson].join("\n");
}

// One definition, N readers — no drift.
const LINE_COLS = "position, part_number, description, qty, unit, line_note";
const VENDOR_COLS =
  "vendor_key, status, box_pdf_file_id, box_form_file_id, review_row_id, responded_estimate_id, sent_at";
// The list/detail projection — never the hmac (internal pending serves it explicitly).
const ROW_COLS =
  "id, rfq_number, job_no, site_phase, job_name, ship_to_name, ship_to_address, ship_to_city, " +
  "ship_to_state, ship_to_zip, delivery_contact_name, delivery_contact_phone, " +
  "delivery_contact_email, scope_text, due_date, status, draft_version, created_by, " +
  "created_at, updated_at";

async function loadLines(db: D1Database, rfqId: number): Promise<RfqLine[]> {
  const { results } = await db
    .prepare(`SELECT ${LINE_COLS} FROM rfq_line_items WHERE rfq_id = ?1 ORDER BY position ASC`)
    .bind(rfqId)
    .all<RfqLine>();
  return (results ?? []) as RfqLine[];
}

async function loadVendorRows(db: D1Database, rfqId: number): Promise<Record<string, unknown>[]> {
  const { results } = await db
    .prepare(`SELECT id, ${VENDOR_COLS} FROM rfq_vendors WHERE rfq_id = ?1 ORDER BY vendor_key ASC`)
    .bind(rfqId)
    .all<Record<string, unknown>>();
  return (results ?? []) as Record<string, unknown>[];
}

// ── Route registration ──────────────────────────────────────────────────────────
export function registerRfqRoutes(app: FieldopsApp, gates: RfqGates): void {
  // ══ Internal surface (requireRfqToken — the Mac-side rfq_poll daemon) ═══════════
  // Registered FIRST so the static /internal/* segment can never be captured by the
  // browser tier's /:id parameter (the po_estimates ordering rule).

  // GET /api/po/rfqs/internal/pending — queued RFQs + lines + vendor rows + the hmac,
  // oldest-first. The daemon recomputes the rfq:v1 canonical HMAC before trusting a row
  // (same pull-model trust chain as PO/submissions).
  app.get("/api/po/rfqs/internal/pending", gates.requireRfqToken, async (c) => {
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "25", 10) || 25, 1), RFQ_PENDING_CAP);
    const { results } = await c.env.DB
      .prepare("SELECT * FROM rfqs WHERE status = 'queued' ORDER BY updated_at ASC, id ASC LIMIT ?1")
      .bind(limit)
      .all<Record<string, unknown>>();
    const rows = (results ?? []) as Record<string, unknown>[];
    for (const r of rows) {
      r.line_items = await loadLines(c.env.DB, r.id as number);
      r.vendors = await loadVendorRows(c.env.DB, r.id as number);
    }
    return c.json({ pending: rows });
  });

  // POST /api/po/rfqs/internal/mark-filed — the per-vendor receipt: the daemon rendered
  // this vendor's RFQ PDF + quote form, filed both to Box, and wrote the vendor's
  // RFQ_Pending_Review row. Body: { rfq_id, vendor_results: [{ vendor_key,
  // box_pdf_file_id, review_row_id, box_form_file_id? }] }. ONE batch (W4): each vendor
  // row flips pending→filed in-WHERE (+ its changes()-gated audit), then the rfq flips
  // queued→generated ONLY when no pending vendor row remains — evaluated AFTER the
  // vendor UPDATEs in the same batch, so a partial filing (daemon crashed mid-fan-out)
  // leaves the rfq queued and the re-served pass finishes idempotently (a replayed
  // vendor result no-ops in-WHERE; found reports per-vendor truth).
  app.post("/api/po/rfqs/internal/mark-filed", gates.requireRfqToken, async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const rfqId = typeof body.rfq_id === "number" && Number.isSafeInteger(body.rfq_id) && body.rfq_id > 0
      ? body.rfq_id
      : null;
    if (rfqId === null) return c.json({ error: "invalid_rfq_id" }, 400);
    const raw = body.vendor_results;
    if (!Array.isArray(raw) || raw.length < 1 || raw.length > MAX_RFQ_VENDORS) {
      return c.json({ error: "invalid_vendor_results" }, 400);
    }
    interface VendorResult {
      vendor_key: string;
      box_pdf_file_id: string | null;
      box_form_file_id: string | null;
      review_row_id: string | null;
    }
    const updates: VendorResult[] = [];
    const seen = new Set<string>();
    for (const r of raw) {
      if (!isPlainObject(r)) return c.json({ error: "invalid_vendor_results" }, 400);
      const vendor_key = str(r.vendor_key);
      if (!VENDOR_KEY_RE.test(vendor_key) || seen.has(vendor_key)) {
        return c.json({ error: "invalid_vendor_results" }, 400);
      }
      seen.add(vendor_key);
      const box_pdf_file_id = optStr(r.box_pdf_file_id, 200);
      const box_form_file_id = optStr(r.box_form_file_id, 200);
      const review_row_id = optStr(r.review_row_id, MAX_SHORT);
      if (box_pdf_file_id === "bad" || box_form_file_id === "bad" || review_row_id === "bad") {
        return c.json({ error: "invalid_vendor_results" }, 400);
      }
      updates.push({ vendor_key, box_pdf_file_id, box_form_file_id, review_row_id });
    }

    const stmts = updates.flatMap((u) => [
      c.env.DB
        .prepare(
          "UPDATE rfq_vendors SET status='filed', box_pdf_file_id=?3, box_form_file_id=?4, " +
            "review_row_id=?5 WHERE rfq_id=?1 AND vendor_key=?2 AND status='pending' " +
            "AND (SELECT status FROM rfqs WHERE id=?1)='queued'",
        )
        .bind(rfqId, u.vendor_key, u.box_pdf_file_id, u.box_form_file_id, u.review_row_id),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "rfq_vendor_filed", `${rfqId}:${u.vendor_key}`, {
        rfq_id: rfqId, vendor_key: u.vendor_key, box_pdf_file_id: u.box_pdf_file_id,
        review_row_id: u.review_row_id,
      }),
    ]);
    stmts.push(
      // The all-filed flip — evaluated at execution time, after the vendor UPDATEs above.
      c.env.DB
        .prepare(
          "UPDATE rfqs SET status='generated', updated_at=unixepoch() " +
            "WHERE id=?1 AND status='queued' " +
            "AND NOT EXISTS (SELECT 1 FROM rfq_vendors WHERE rfq_id=?1 AND status='pending')",
        )
        .bind(rfqId),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "rfq_all_filed", String(rfqId), { rfq_id: rfqId }),
    );
    const res = await c.env.DB.batch(stmts);
    let filed = 0;
    for (let i = 0; i < updates.length; i++) if ((res[i * 2]?.meta?.changes ?? 0) > 0) filed++;
    const allFiled = (res[updates.length * 2]?.meta?.changes ?? 0) > 0;
    return c.json({ ok: true, filed, replayed: updates.length - filed, all_filed: allFiled });
  });

  // POST /api/po/rfqs/internal/status-sync — Mac-side per-vendor machine outcomes.
  // Body: { rfq_id, vendor_key, status: 'sent'|'responded', responded_estimate_id? }.
  // FORWARD-ONLY, in-WHERE: 'sent' only from 'filed' (stamps sent_at); 'responded' only
  // from 'sent' (records the estimate the reply landed as). D1 vendor status here is a
  // CACHE of the Mac/Smartsheet-side authoritative state — the guards prevent REGRESSION
  // (a stale/replayed sync can never move a row backwards), not re-enforce F22. The rfq's
  // own status is DERIVED in the SAME batch: no live vendor row left pending/filed →
  // 'sent'; some sent/responded while others aren't → 'partially_sent' (the two derive
  // UPDATEs are guard-disjoint — exactly one can match).
  app.post("/api/po/rfqs/internal/status-sync", gates.requireRfqToken, async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const rfqId = typeof body.rfq_id === "number" && Number.isSafeInteger(body.rfq_id) && body.rfq_id > 0
      ? body.rfq_id
      : null;
    if (rfqId === null) return c.json({ error: "invalid_rfq_id" }, 400);
    const vendorKey = str(body.vendor_key);
    if (!VENDOR_KEY_RE.test(vendorKey)) return c.json({ error: "invalid_vendor_key" }, 400);
    const status = typeof body.status === "string" && SYNCABLE_VENDOR_STATUSES.has(body.status)
      ? (body.status as "sent" | "responded")
      : null;
    if (status === null) return c.json({ error: "invalid_status" }, 400);
    let respondedEstimateId: number | null = null;
    if (body.responded_estimate_id !== undefined && body.responded_estimate_id !== null) {
      if (status !== "responded" || typeof body.responded_estimate_id !== "number" ||
          !Number.isSafeInteger(body.responded_estimate_id) || body.responded_estimate_id <= 0) {
        return c.json({ error: "invalid_responded_estimate_id" }, 400);
      }
      respondedEstimateId = body.responded_estimate_id;
    }

    const vendorUpdate = status === "sent"
      ? c.env.DB
          .prepare(
            "UPDATE rfq_vendors SET status='sent', sent_at=unixepoch() " +
              "WHERE rfq_id=?1 AND vendor_key=?2 AND status='filed'",
          )
          .bind(rfqId, vendorKey)
      : c.env.DB
          .prepare(
            "UPDATE rfq_vendors SET status='responded', " +
              "responded_estimate_id=COALESCE(?3, responded_estimate_id) " +
              "WHERE rfq_id=?1 AND vendor_key=?2 AND status='sent'",
          )
          .bind(rfqId, vendorKey, respondedEstimateId);
    const res = await c.env.DB.batch([
      vendorUpdate,
      auditStmtIfChanged(c, SYSTEM_ACTOR, `rfq_vendor_${status}`, `${rfqId}:${vendorKey}`, {
        rfq_id: rfqId, vendor_key: vendorKey, status,
        responded_estimate_id: respondedEstimateId,
      }),
      // Derive the rfq status (guard-disjoint pair — see route comment). 'sent' first:
      // every live vendor row has left pending/filed.
      c.env.DB
        .prepare(
          "UPDATE rfqs SET status='sent', updated_at=unixepoch() " +
            "WHERE id=?1 AND status IN ('generated','partially_sent') " +
            "AND NOT EXISTS (SELECT 1 FROM rfq_vendors WHERE rfq_id=?1 AND status IN ('pending','filed'))",
        )
        .bind(rfqId),
      c.env.DB
        .prepare(
          "UPDATE rfqs SET status='partially_sent', updated_at=unixepoch() " +
            "WHERE id=?1 AND status='generated' " +
            "AND EXISTS (SELECT 1 FROM rfq_vendors WHERE rfq_id=?1 AND status IN ('sent','responded')) " +
            "AND EXISTS (SELECT 1 FROM rfq_vendors WHERE rfq_id=?1 AND status IN ('pending','filed'))",
        )
        .bind(rfqId),
    ]);
    const found = (res[0].meta.changes ?? 0) > 0;
    const now = await c.env.DB
      .prepare("SELECT status FROM rfqs WHERE id = ?1")
      .bind(rfqId)
      .first<{ status: string }>();
    return c.json({ ok: true, found, rfq_status: now?.status ?? null });
  });

  // ══ Browser surface (session + cap.po.manage — the po.ts gate pair) ═════════════

  // POST /api/po/rfqs — create a multi-vendor draft. Ship-to arrives from the SPA like a
  // PO draft (the jobs ship-to autofill feed — no server-side re-resolution here). Every
  // vendor key must name an ACTIVE po_vendors row (400 unknown_vendor — the R1 contract).
  // Parent + audit + lines + vendor rows in ONE batch (W4); child INSERTs resolve rfq_id
  // via the rfq_uuid scalar subquery (the 0043/0054 pattern).
  app.post("/api/po/rfqs", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const d = parseRfqDraftBody(body);
    if (typeof d === "string") return c.json({ error: d }, 400);
    const unknown = await findUnknownVendor(c.env.DB, d.vendor_keys);
    if (unknown !== null) return c.json({ error: "unknown_vendor", vendor_key: unknown }, 400);

    const actor = c.get("session").username;
    const rfqUuid = crypto.randomUUID();
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "INSERT INTO rfqs (rfq_uuid, job_no, site_phase, job_name, ship_to_name, ship_to_address, " +
            "ship_to_city, ship_to_state, ship_to_zip, delivery_contact_name, " +
            "delivery_contact_phone, delivery_contact_email, scope_text, due_date, status, created_by, job_id) " +
            "VALUES (?1,?2,?15,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,'draft',?14,?16) RETURNING id",
        )
        .bind(
          rfqUuid, d.job_no, d.job_name, d.ship_to_name, d.ship_to_address, d.ship_to_city,
          d.ship_to_state, d.ship_to_zip, d.delivery_contact_name, d.delivery_contact_phone,
          d.delivery_contact_email, d.scope_text, d.due_date, actor, d.site_phase, d.job_id,
        ),
      auditStmt(c, actor, "rfq_draft_create", rfqUuid, {
        rfq_uuid: rfqUuid, job_no: d.job_no, lines: d.lines.length, vendors: d.vendor_keys.length,
      }),
      ...d.lines.map((l) =>
        c.env.DB
          .prepare(
            `INSERT INTO rfq_line_items (rfq_id, ${LINE_COLS}) ` +
              "SELECT (SELECT id FROM rfqs WHERE rfq_uuid = ?1), ?2,?3,?4,?5,?6,?7 " +
              "WHERE EXISTS (SELECT 1 FROM rfqs WHERE rfq_uuid = ?1)",
          )
          .bind(rfqUuid, l.position, l.part_number, l.description, l.qty, l.unit, l.line_note),
      ),
      ...d.vendor_keys.map((v) =>
        c.env.DB
          .prepare(
            "INSERT INTO rfq_vendors (rfq_id, vendor_key, status) " +
              "SELECT (SELECT id FROM rfqs WHERE rfq_uuid = ?1), ?2, 'pending' " +
              "WHERE EXISTS (SELECT 1 FROM rfqs WHERE rfq_uuid = ?1)",
          )
          .bind(rfqUuid, v),
      ),
    ]);
    const id = (res[0].results?.[0] as { id: number } | undefined)?.id ?? null;
    return c.json({ ok: true, id }, 201);
  });

  // GET /api/po/rfqs?status=&limit= — the tracker list (+ each RFQ's vendor rows, for
  // the per-vendor status badges). Never the hmac.
  app.get("/api/po/rfqs", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const q = c.req.query();
    const status = RFQ_STATUSES.has(q.status ?? "") ? (q.status as string) : "all";
    const limit = Math.min(Math.max(parseInt(q.limit || "50", 10) || 50, 1), LIST_CAP);
    const { results } = await c.env.DB
      .prepare(
        `SELECT ${ROW_COLS} FROM rfqs WHERE (?1 = 'all' OR status = ?1) ` +
          "ORDER BY updated_at DESC, id DESC LIMIT ?2",
      )
      .bind(status, limit)
      .all<Record<string, unknown>>();
    const rows = (results ?? []) as Record<string, unknown>[];
    for (const r of rows) r.vendors = await loadVendorRows(c.env.DB, r.id as number);
    return c.json({ rfqs: rows });
  });

  // GET /api/po/rfqs/:id — one RFQ + lines + vendor rows (the builder/detail read).
  app.get("/api/po/rfqs/:id", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const rfq = await c.env.DB
      .prepare(`SELECT ${ROW_COLS} FROM rfqs WHERE id = ?1`)
      .bind(id)
      .first<Record<string, unknown>>();
    if (!rfq) return c.json({ error: "not_found" }, 404);
    return c.json({
      rfq,
      line_items: await loadLines(c.env.DB, id),
      vendors: await loadVendorRows(c.env.DB, id),
    });
  });

  // POST /api/po/rfqs/:id/update — full-replace edit, DRAFT-ONLY (the po drafts pattern:
  // guarded in-WHERE; every child DELETE/INSERT is guarded on the live status so a lost
  // race writes nothing; draft_version+1 covers the whole snapshot for generate's lock).
  // Vendor rows are full-replaced too — safe pre-generate: a draft's rows are all
  // 'pending' and carry no filing state yet.
  app.post("/api/po/rfqs/:id/update", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const d = parseRfqDraftBody(body);
    if (typeof d === "string") return c.json({ error: d }, 400);
    const unknown = await findUnknownVendor(c.env.DB, d.vendor_keys);
    if (unknown !== null) return c.json({ error: "unknown_vendor", vendor_key: unknown }, 400);

    const actor = c.get("session").username;
    const guard = "(SELECT status FROM rfqs WHERE id = ?1) = 'draft'";
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE rfqs SET job_no=?2, site_phase=?14, job_name=?3, ship_to_name=?4, ship_to_address=?5, " +
            "ship_to_city=?6, ship_to_state=?7, ship_to_zip=?8, delivery_contact_name=?9, " +
            "delivery_contact_phone=?10, delivery_contact_email=?11, scope_text=?12, " +
            "due_date=?13, job_id=?15, updated_at=unixepoch(), draft_version=draft_version+1 " +
            "WHERE id=?1 AND status='draft'",
        )
        .bind(
          id, d.job_no, d.job_name, d.ship_to_name, d.ship_to_address, d.ship_to_city,
          d.ship_to_state, d.ship_to_zip, d.delivery_contact_name, d.delivery_contact_phone,
          d.delivery_contact_email, d.scope_text, d.due_date, d.site_phase, d.job_id,
        ),
      auditStmtIfChanged(c, actor, "rfq_draft_update", String(id), {
        rfq_id: id, lines: d.lines.length, vendors: d.vendor_keys.length,
      }),
      c.env.DB.prepare(`DELETE FROM rfq_line_items WHERE rfq_id = ?1 AND ${guard}`).bind(id),
      ...d.lines.map((l) =>
        c.env.DB
          .prepare(
            `INSERT INTO rfq_line_items (rfq_id, ${LINE_COLS}) SELECT ?1, ?2,?3,?4,?5,?6,?7 WHERE ${guard}`,
          )
          .bind(id, l.position, l.part_number, l.description, l.qty, l.unit, l.line_note),
      ),
      c.env.DB.prepare(`DELETE FROM rfq_vendors WHERE rfq_id = ?1 AND ${guard}`).bind(id),
      ...d.vendor_keys.map((v) =>
        c.env.DB
          .prepare(`INSERT INTO rfq_vendors (rfq_id, vendor_key, status) SELECT ?1, ?2, 'pending' WHERE ${guard}`)
          .bind(id, v),
      ),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const row = await c.env.DB.prepare("SELECT status FROM rfqs WHERE id = ?1").bind(id).first();
      return row ? c.json({ error: "not_draft" }, 409) : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id });
  });

  // POST /api/po/rfqs/:id/generate — the draft→queued transition. Allocates
  // rfq_number = 'RFQ-<job_no>-<seq3>' (seq = MAX+1 over the job's allocated numbers;
  // the UNIQUE(rfq_number) index is the race backstop — see migration 0056 header), signs
  // "rfq:v1"\n<rfq_id>\n<rfq_number>\n<canonical_json>, and flips the status pinned on the
  // draft_version it read (the po.ts W5/W8 guard: a concurrent edit → clean 409
  // draft_changed, never a queued row whose HMAC signed a stale snapshot).
  app.post("/api/po/rfqs/:id/generate", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);

    const rfq = await c.env.DB
      .prepare("SELECT * FROM rfqs WHERE id = ?1 AND status = 'draft'")
      .bind(id)
      .first<RfqRow & { status: string; draft_version: number }>();
    if (!rfq) return c.json({ error: "not_found" }, 404);
    const lines = await loadLines(c.env.DB, id);
    if (lines.length === 0) return c.json({ error: "no_line_items" }, 422);
    const vendorRows = await loadVendorRows(c.env.DB, id);
    const vendorKeys = vendorRows.map((v) => v.vendor_key as string);
    if (vendorKeys.length === 0) return c.json({ error: "no_vendors" }, 422);

    // TOCTOU re-check (ADR decision 9): findUnknownVendor (active=1) runs only at
    // draft create/update. A vendor DEACTIVATED between the last draft-save and this
    // generate must never be signed, queued, or filed — re-validate every vendor's
    // active-status HERE and sign NOTHING on a miss. The draft is left untouched
    // (the operator re-activates the vendor or drops it from the draft, then retries).
    const inactive = await findUnknownVendor(c.env.DB, vendorKeys);
    if (inactive !== null) return c.json({ error: "vendor_inactive", vendor_key: inactive }, 409);

    // 0070 — the number prefix, built HERE rather than in SQL so site-0 emits NO segment.
    // That rule is load-bearing, not cosmetic: the allocator below recovers NNN by measuring
    // PAST this prefix, so if a site-0 job started emitting `.0`, the already-issued
    // `RFQ-2026.384-001` (16 chars) measured against the longer `RFQ-2026.384.0-` (15 chars)
    // yields "1" instead of "001" — MAX+1 = 2 — and generate mints `-002` beside an existing
    // `-001`. Two DIFFERENT strings, so the UNIQUE(rfq_number) index does not catch it; the
    // sequence is simply, silently wrong. Matches src/lib/jobNumber.formatJobNumber.
    const sitePhase = rfq.site_phase ?? 0;
    const numberPrefix = sitePhase > 0 ? `RFQ-${rfq.job_no}.${sitePhase}-` : `RFQ-${rfq.job_no}-`;

    // MAX(seq)+1 over the (job, SITE) family's ALLOCATED numbers. Scoping to site_phase as
    // well as job_no is the other half of the guard above: a legacy site-less row must only
    // ever be counted for site-0 RFQs, never against a site-bearing family whose prefix it
    // does not share. seq > 999 simply widens past 3 digits (padStart pads, never truncates).
    const seqRow = await c.env.DB
      .prepare(
        "SELECT COALESCE(MAX(CAST(substr(rfq_number, length(?2) + 1) AS INTEGER)), 0) + 1 AS seq " +
          "FROM rfqs WHERE job_no = ?1 AND site_phase = ?3 AND rfq_number IS NOT NULL",
      )
      .bind(rfq.job_no, numberPrefix, sitePhase)
      .first<{ seq: number }>();
    const seq = seqRow?.seq ?? 1;
    const rfqNumber = `${numberPrefix}${String(seq).padStart(3, "0")}`;

    // Fail closed on a missing HMAC secret — signing with undefined would mint
    // signatures the Mac side can never verify (silent loss).
    if (!c.env.HMAC_PAYLOAD_SECRET) return c.json({ error: "hmac_secret_missing" }, 500);
    const signed: RfqRow = { ...rfq, rfq_number: rfqNumber };
    const hmac = await hmacHex(
      c.env.HMAC_PAYLOAD_SECRET,
      rfqCanonicalString(id, rfqNumber, canonicalRfqJson(signed, lines, vendorKeys)),
    );

    const actor = c.get("session").username;
    let res;
    try {
      res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            "UPDATE rfqs SET rfq_number=?2, hmac=?3, status='queued', updated_at=unixepoch() " +
              "WHERE id=?1 AND status='draft' AND draft_version=?4",
          )
          .bind(id, rfqNumber, hmac, rfq.draft_version),
        auditStmtIfChanged(c, actor, "rfq_generate", String(id), {
          rfq_id: id, rfq_number: rfqNumber, vendors: vendorKeys.length,
        }),
      ]);
    } catch (e) {
      // The UNIQUE(rfq_number) backstop — a lost allocation race. The draft is untouched;
      // the client simply retries generate and reads a fresh MAX.
      if (isUniqueViolation(e)) return c.json({ error: "rfq_number_conflict" }, 409);
      throw e;
    }
    if ((res[0].meta.changes ?? 0) === 0) {
      const now = await c.env.DB
        .prepare("SELECT status, draft_version FROM rfqs WHERE id = ?1")
        .bind(id)
        .first<{ status: string; draft_version: number }>();
      if (now && now.status === "draft" && now.draft_version !== rfq.draft_version) {
        return c.json({ error: "draft_changed" }, 409);
      }
      return c.json({ error: "not_draft" }, 409);
    }
    return c.json({ ok: true, id, rfq_number: rfqNumber });
  });

  // POST /api/po/rfqs/:id/cancel — off-path terminal, ONLY from draft/queued (a
  // generated/sent RFQ has live Box/Smartsheet artifacts and per-vendor state — 'closed'
  // is its R4 exit). The vendor rows cancel in the SAME batch, guarded on the rfq now
  // being canceled (a queued cancel is honored Mac-side by the daemon's status read).
  app.post("/api/po/rfqs/:id/cancel", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE rfqs SET status='canceled', updated_at=unixepoch() " +
            "WHERE id=?1 AND status IN ('draft','queued')",
        )
        .bind(id),
      auditStmtIfChanged(c, actor, "rfq_cancel", String(id), { rfq_id: id }),
      c.env.DB
        .prepare(
          "UPDATE rfq_vendors SET status='canceled' WHERE rfq_id=?1 AND status='pending' " +
            "AND (SELECT status FROM rfqs WHERE id=?1) = 'canceled'",
        )
        .bind(id),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const row = await c.env.DB.prepare("SELECT status FROM rfqs WHERE id = ?1").bind(id).first();
      return row ? c.json({ error: "not_cancelable" }, 409) : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id });
  });
}
