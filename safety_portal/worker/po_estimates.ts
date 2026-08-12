import type { MiddlewareHandler } from "hono";
import type { FieldopsApp } from "./fieldops_gates";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { hmacHex } from "./hmac";
import { b64DecodedLen, B64_RE } from "./photo_bounds";

// ─────────────────────────────────────────────────────────────────────────────
// Vendor-estimate importer E1 (ADR-0004, po_materials sub-lane) — worker/po_estimates.ts
//
// The Worker half of the estimate upload pool + disposition surface:
//   - Browser tier (session + cap.po.manage): upload an office-received vendor
//     estimate (bounds-gated, est:v1-signed, pooled in D1 SEND-FREE), track it,
//     read the latest ADVISORY extraction + rendered page previews, and DISPOSE
//     it (imported/rejected) — the only exits from the reviewable states.
//   - Internal tier (requireEstimateToken — the estimate daemon's OWN bearer,
//     ADR-0004 decision 4 / red-team #1: the highest-exposure process, which
//     decodes hostile PDF/xlsx bytes, holds a token that scopes ONLY
//     /api/po/estimates/internal/* — never the PO queue, never a send lane):
//     pending scan, claim-first marker, Mac-ward chunk read, the result post
//     (refused | needs_review | extracted [+ extraction payload]), and the
//     per-page PNG preview upsert.
//
// Invariants:
//   - Invariant 1: SEND-FREE, zero AI. Validates, signs, queues, records.
//   - Invariant 2: every body shape-guarded + bounded, all SQL ?-bound, every
//     mutation atomic with its audit row (W4). The est:v1 HMAC (same
//     HMAC_PAYLOAD_SECRET, own domain) binds est_uuid + job_no + filename +
//     mime + size + sha256 so the Mac verifies row AND reassembled bytes before
//     a single byte is parsed. Extraction rows are ADVISORY (ADR decision 2):
//     no dollar leaves this module toward a PO — the disposition screen routes
//     accepted lines through the EXISTING POST /api/po/drafts validators.
//   - Dedupe authority (ADR decision 7): the partial UNIQUE sha256 index over
//     live rows; the constraint violation maps to 409 duplicate_estimate.
//   - Status machine: pending → claimed → (refused | needs_review | extracted)
//     → (imported | rejected); plus superseded (E4). Chunks die at refusal and
//     at disposition (with the previews) — bytes never outlive the pipeline.
// ─────────────────────────────────────────────────────────────────────────────

export type PoEstimateGates = {
  requireSession: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  requireCapability: (cap: string) => MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  /** Bearer gate for /api/po/estimates/internal/* — the estimate_poll daemon's OWN token
   *  tier (PORTAL_ESTIMATE_API_TOKEN), privilege-separated from the PO / portal / admin /
   *  fieldops / config / subcontract tokens. Built in index.ts next to its siblings. */
  requireEstimateToken: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
};

const CAP_PO = "cap.po.manage";
export const ESTIMATE_HMAC_DOMAIN = "est:v1";
const SYSTEM_ACTOR = "system:estimate_poll";

// ── Bounds (Invariant 2) ────────────────────────────────────────────────────────
export const ESTIMATE_MAX_BYTES = 10_000_000; // decoded bytes per document (10 MB)
export const MAX_ESTIMATE_FILENAME = 120;
export const EST_CHUNK_DECODED_MAX = 1_000_000; // mirrors filed_pdfs / po_attachment_chunks
export const PREVIEW_MAX_BYTES = 1_000_000; // decoded bytes per preview page PNG
const MAX_PREVIEW_PAGES = 24; // Worker ceiling; the Mac's max_pages_preview config (12) is tighter
const EST_PENDING_CAP = 25;
const LIST_CAP = 200;
const MAX_DETAIL = 200;
const MAX_SHORT = 64;
const MAX_TEXT = 256;
const MAX_LINE_TEXT = 512;
const MAX_LINES = 500;
const MAX_LINE_DISPOSITIONS = 500;
const MAX_PAYLOAD_JSON = 400_000; // the full schema document, bounded
const MAX_ANOMALIES = 4000;
const MAX_EDITED_JSON = 4000;
const MAX_MONEY_CENTS = 1_000_000_000_000; // $10B — the po.ts ceiling
const MAX_QTY = 1_000_000_000;

const JOB_NO_RE = /^\d{4}\.\d{3}$/;
const VENDOR_KEY_RE = /^VEN-\d{6}$/;

// Doc types (mirrors the 0054 CHECK — the Mac classifier vocabulary).
const DOC_TYPES = new Set(["quote", "estimate", "proposal", "invoice", "ap_report", "filled_form", "other"]);
// The result-post statuses the daemon may stamp (never imported/rejected — those are
// the browser disposition's; never superseded — that is E4's family logic).
const RESULT_STATUSES = new Set(["refused", "needs_review", "extracted"]);
const LINE_DISPOSITIONS = new Set(["accepted", "rejected", "edited"]);
// The statuses a preview page may still LAND on: in-pipeline (pending/claimed — the
// daemon renders ahead of the result post) or reviewable (needs_review/extracted — a
// re-render). Terminal rows (refused/rejected/imported/superseded) refuse the write —
// dispose/refusal already dropped that row's evidence and a late daemon post must not
// resurrect it.
const PREVIEW_LIVE_STATUSES = new Set(["pending", "claimed", "needs_review", "extracted"]);

// ── Allowlist: declared MIME ⇄ extension ⇄ magic (the po_attachments set, verbatim —
// PDF + JPEG/PNG images + Office OpenXML only; NO legacy OLE, NO CAD). The magic sniff
// is the Worker's cheap gate; the real §34 inspection is Mac-side (po_attach_screen).
type Magic = "pdf" | "jpeg" | "png" | "zip";
const MIME_ALLOWLIST: Record<string, { exts: string[]; magic: Magic }> = {
  "application/pdf": { exts: [".pdf"], magic: "pdf" },
  "image/jpeg": { exts: [".jpg", ".jpeg"], magic: "jpeg" },
  "image/png": { exts: [".png"], magic: "png" },
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {
    exts: [".docx"],
    magic: "zip",
  },
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
    exts: [".xlsx"],
    magic: "zip",
  },
};

function magicMatches(head: Uint8Array, magic: Magic): boolean {
  if (head.length < 8) return false;
  switch (magic) {
    case "pdf": // "%PDF-"
      return head[0] === 0x25 && head[1] === 0x50 && head[2] === 0x44 && head[3] === 0x46 && head[4] === 0x2d;
    case "jpeg": // FF D8 FF
      return head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff;
    case "png": // 89 50 4E 47 0D 0A 1A 0A
      return (
        head[0] === 0x89 && head[1] === 0x50 && head[2] === 0x4e && head[3] === 0x47 &&
        head[4] === 0x0d && head[5] === 0x0a && head[6] === 0x1a && head[7] === 0x0a
      );
    case "zip": // PK\x03\x04
      return head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04;
  }
}

/** Filename gate — the po_attachments rules verbatim (bounded, no path separators /
 *  control chars / leading dot / Unicode bidi-zero-width spoofers; extension must belong
 *  to the declared MIME). The name lands in D1, the Estimate_Log row, and the Box file. */
function filenameProblem(filename: string, declaredMime: string): string | null {
  if (filename.length < 1 || filename.length > MAX_ESTIMATE_FILENAME) return "invalid_filename";
  // eslint-disable-next-line no-control-regex
  if (/[/\\\u0000-\u001f\u007f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]/.test(filename)) {
    return "invalid_filename";
  }
  if (filename.startsWith(".")) return "invalid_filename";
  const entry = MIME_ALLOWLIST[declaredMime];
  if (!entry) return "mime_not_allowed";
  const lower = filename.toLowerCase();
  if (!entry.exts.some((e) => lower.endsWith(e) && lower.length > e.length)) {
    return "extension_mime_mismatch";
  }
  return null;
}

/** The est:v1 canonical string — ORDER + "\n" SEPARATOR are load-bearing; the Mac
 *  (shared/portal_hmac.py estimate_canonical) recomputes it byte-for-byte before
 *  trusting a row. Binds identity (est_uuid, job_no), naming (filename, mime), and
 *  content (size_bytes, sha256) — a tampered row OR tampered bytes fail verify. */
export function estimateCanonical(
  estUuid: string, jobNo: string, filename: string, declaredMime: string,
  sizeBytes: number, sha256: string,
): string {
  return [ESTIMATE_HMAC_DOMAIN, estUuid, jobNo, filename, declaredMime, String(sizeBytes), sha256].join("\n");
}

// ── Small helpers (the po.ts idioms) ────────────────────────────────────────────
function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}
function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function isCents(v: unknown): v is number {
  return typeof v === "number" && Number.isSafeInteger(v) && v >= 0 && v <= MAX_MONEY_CENTS;
}
function optCents(v: unknown): number | null | "bad" {
  if (v === undefined || v === null) return null;
  return isCents(v) ? v : "bad";
}
function optStr(v: unknown, max: number): string | null | "bad" {
  if (v === undefined || v === null) return null;
  if (typeof v !== "string") return "bad";
  const t = v.trim();
  if (t.length === 0) return null;
  return t.length <= max ? t : "bad";
}
function parseIdParam(raw: string | undefined): number | null {
  const id = parseInt(raw ?? "", 10);
  return Number.isSafeInteger(id) && id > 0 && String(id) === (raw ?? "") ? id : null;
}
function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  const STEP = 0x8000;
  for (let i = 0; i < bytes.length; i += STEP) {
    bin += String.fromCharCode(...bytes.subarray(i, i + STEP));
  }
  return btoa(bin);
}
async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as unknown as BufferSource);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ── Extraction-body validation (the PR-B payload shape, accepted from E1 on) ────
export interface ExtractionLineBody {
  position: number;
  section: string | null;
  part_number: string | null;
  description: string;
  qty: number | null;
  unit: string | null;
  unit_cost_cents: number | null;
  extended_cents: number | null;
  math_ok: 0 | 1;
  line_note: string | null;
}

export interface ExtractionBody {
  tier: number;
  schema_version: string;
  doc_type: string;
  vendor_name: string | null;
  quote_number: string | null;
  revision_label: string | null;
  quote_date: string | null;
  valid_until: string | null;
  subtotal_cents: number | null;
  tax_cents: number | null;
  freight_cents: number | null;
  misc_cents: number | null;
  grand_total_cents: number | null;
  math_ok: 0 | 1;
  confidence: number | null;
  payload_json: string;
  anomalies: string | null;
  lines: ExtractionLineBody[];
}

function parseMathOk(v: unknown): 0 | 1 | "bad" {
  return v === 0 || v === 1 ? v : "bad";
}

/** Validate the ADVISORY extraction payload (the internal result contract). Returns an
 *  error-code string on any shape violation — the daemon's input is untrusted too
 *  (Invariant 2): it relays document-derived content. */
function parseExtraction(raw: unknown): ExtractionBody | string {
  if (!isPlainObject(raw)) return "invalid_extraction";
  const tier = raw.tier;
  if (typeof tier !== "number" || !Number.isSafeInteger(tier) || tier < 0 || tier > 3) return "invalid_tier";
  const schema_version = str(raw.schema_version);
  if (schema_version.length < 1 || schema_version.length > 32) return "invalid_schema_version";
  const doc_type = str(raw.doc_type);
  if (!DOC_TYPES.has(doc_type)) return "invalid_doc_type";
  const vendor_name = optStr(raw.vendor_name, MAX_TEXT);
  const quote_number = optStr(raw.quote_number, MAX_SHORT);
  const revision_label = optStr(raw.revision_label, MAX_SHORT);
  const quote_date = optStr(raw.quote_date, MAX_SHORT);
  const valid_until = optStr(raw.valid_until, MAX_SHORT);
  if (vendor_name === "bad" || quote_number === "bad" || revision_label === "bad" ||
      quote_date === "bad" || valid_until === "bad") {
    return "invalid_extraction_field";
  }
  const subtotal_cents = optCents(raw.subtotal_cents);
  const tax_cents = optCents(raw.tax_cents);
  const freight_cents = optCents(raw.freight_cents);
  const misc_cents = optCents(raw.misc_cents);
  const grand_total_cents = optCents(raw.grand_total_cents);
  if (subtotal_cents === "bad" || tax_cents === "bad" || freight_cents === "bad" ||
      misc_cents === "bad" || grand_total_cents === "bad") {
    return "invalid_extraction_money";
  }
  const math_ok = parseMathOk(raw.math_ok);
  if (math_ok === "bad") return "invalid_math_ok";
  let confidence: number | null = null;
  if (raw.confidence !== undefined && raw.confidence !== null) {
    if (typeof raw.confidence !== "number" || !Number.isFinite(raw.confidence) ||
        raw.confidence < 0 || raw.confidence > 1) {
      return "invalid_confidence";
    }
    confidence = raw.confidence;
  }
  const payload_json = typeof raw.payload_json === "string" ? raw.payload_json : "";
  if (payload_json.length < 2 || payload_json.length > MAX_PAYLOAD_JSON) return "invalid_payload_json";
  const anomalies = optStr(raw.anomalies, MAX_ANOMALIES);
  if (anomalies === "bad") return "invalid_extraction_field";

  if (!Array.isArray(raw.lines) || raw.lines.length < 1 || raw.lines.length > MAX_LINES) {
    return "invalid_lines";
  }
  const lines: ExtractionLineBody[] = [];
  const positions = new Set<number>();
  for (const r of raw.lines) {
    if (!isPlainObject(r)) return "invalid_lines";
    const position = r.position;
    if (typeof position !== "number" || !Number.isSafeInteger(position) || position < 1 || position > MAX_LINES) {
      return "invalid_line_position";
    }
    if (positions.has(position)) return "duplicate_line_position";
    positions.add(position);
    const section = optStr(r.section, MAX_TEXT);
    const part_number = optStr(r.part_number, MAX_SHORT);
    const description = str(r.description);
    if (description.length < 1 || description.length > MAX_LINE_TEXT) return "invalid_line_description";
    const unit = optStr(r.unit, 32);
    const line_note = optStr(r.line_note, MAX_TEXT);
    if (section === "bad" || part_number === "bad" || unit === "bad" || line_note === "bad") {
      return "invalid_line_field";
    }
    let qty: number | null = null;
    if (r.qty !== undefined && r.qty !== null) {
      if (typeof r.qty !== "number" || !Number.isFinite(r.qty) || r.qty < 0 || r.qty > MAX_QTY) {
        return "invalid_line_qty";
      }
      qty = r.qty;
    }
    const unit_cost_cents = optCents(r.unit_cost_cents);
    const extended_cents = optCents(r.extended_cents);
    if (unit_cost_cents === "bad" || extended_cents === "bad") return "invalid_line_money";
    const lineMathOk = parseMathOk(r.math_ok);
    if (lineMathOk === "bad") return "invalid_math_ok";
    lines.push({
      position, section, part_number, description, qty, unit,
      unit_cost_cents, extended_cents, math_ok: lineMathOk, line_note,
    });
  }
  return {
    tier, schema_version, doc_type, vendor_name, quote_number, revision_label,
    quote_date, valid_until, subtotal_cents, tax_cents, freight_cents, misc_cents,
    grand_total_cents, math_ok, confidence, payload_json, anomalies, lines,
  };
}

// The projection the list/detail reads serve (never chunk bytes, never the hmac).
const ROW_COLS =
  // job_id (0069): the UNAMBIGUOUS job identity. job_no alone cannot resolve the site —
  // 2026.384 is both MH405 (site 1) and OG593 (site 2) — so the disposition screen needs
  // this to look the site up and seed the PO draft with it.
  "id, est_uuid, job_no, job_id, job_name, vendor_key, filename, declared_mime, size_bytes, sha256, " +
  "status, doc_type, detail, uploaded_by, box_file_id, family_key, supersedes_estimate_id, " +
  "po_id, rfq_id, rfq_vendor_key, created_at, screened_at, extracted_at, disposed_at";

// ── Route registration ──────────────────────────────────────────────────────────
export function registerPoEstimateRoutes(app: FieldopsApp, gates: PoEstimateGates): void {
  // ══ Internal surface (requireEstimateToken — the Mac-side estimate_poll daemon) ══
  // Registered FIRST so the static /internal/* segment can never be captured by the
  // browser tier's /:id parameter.

  // GET /api/po/estimates/internal/pending — live pool rows oldest-first: pending +
  // claimed (claimed re-served for crash recovery — the pass is idempotent). Serves
  // metadata + the HMAC (the Mac's verify input); bytes ride the chunks read below.
  app.get("/api/po/estimates/internal/pending", gates.requireEstimateToken, async (c) => {
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "25", 10) || 25, 1), EST_PENDING_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT id, est_uuid, job_no, job_name, vendor_key, filename, declared_mime, size_bytes, " +
          "sha256, status, hmac, uploaded_by, created_at " +
          "FROM po_estimates WHERE status IN ('pending','claimed') " +
          "ORDER BY created_at ASC, id ASC LIMIT ?1",
      )
      .bind(limit)
      .all<Record<string, unknown>>();
    return c.json({ estimates: results ?? [] });
  });

  // POST /api/po/estimates/internal/:id/claim — claim-first marker: pending→claimed,
  // guarded in-WHERE + changes()-gated audit (W4). Idempotent: found:false when already
  // claimed/disposed — the daemon proceeds on a row it already claimed (crash recovery).
  app.post("/api/po/estimates/internal/:id/claim", gates.requireEstimateToken, async (c) => {
    const estId = parseIdParam(c.req.param("id"));
    if (estId === null) return c.json({ error: "invalid_id" }, 400);
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare("UPDATE po_estimates SET status='claimed' WHERE id = ?1 AND status = 'pending'")
        .bind(estId),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "po_estimate_claim", String(estId), { estimate_id: estId }),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // GET /api/po/estimates/internal/:id/chunks — the Mac-ward byte read (the ONLY route
  // that ever serves original document bytes, bearer-gated). Live rows only.
  app.get("/api/po/estimates/internal/:id/chunks", gates.requireEstimateToken, async (c) => {
    const estId = parseIdParam(c.req.param("id"));
    if (estId === null) return c.json({ error: "invalid_id" }, 400);
    const row = await c.env.DB
      .prepare("SELECT status FROM po_estimates WHERE id = ?1")
      .bind(estId)
      .first<{ status: string }>();
    if (!row || (row.status !== "pending" && row.status !== "claimed")) {
      return c.json({ error: "not_found" }, 404);
    }
    const { results } = await c.env.DB
      .prepare(
        "SELECT chunk_index, chunk_total, chunk_b64 FROM po_estimate_chunks " +
          "WHERE estimate_id = ?1 ORDER BY chunk_index ASC",
      )
      .bind(estId)
      .all<Record<string, unknown>>();
    return c.json({ chunks: results ?? [] });
  });

  // POST /api/po/estimates/internal/result — apply one screening/extraction outcome.
  // Body: { estimate_id, status: 'refused'|'needs_review'|'extracted', detail?,
  // box_file_id? (forbidden on refused — a refused doc is never filed), extraction?
  // (REQUIRED iff status='extracted' — the PR-B advisory payload, validated in full;
  // in E1 the daemon posts only refused/needs_review with no extraction) }.
  // ONE atomic batch (W4): guarded status UPDATE → changes()-gated audit →
  // [refused: chunk DELETE — delete-on-refusal, its subselect reads the POST-update
  // status] / [extracted: extraction INSERT + line INSERTs, each guarded on the
  // post-update status AND first-extraction (a lost race inserts nothing; the loser's
  // UNIQUE(extraction_id,position) backstop aborts its whole batch)]. Idempotent: a
  // re-post for an already-disposed/unknown row is { ok:true, found:false }.
  app.post("/api/po/estimates/internal/result", gates.requireEstimateToken, async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const estId = typeof body.estimate_id === "number" && Number.isSafeInteger(body.estimate_id) && body.estimate_id > 0
      ? body.estimate_id
      : null;
    if (estId === null) return c.json({ error: "invalid_estimate_id" }, 400);
    const status = typeof body.status === "string" && RESULT_STATUSES.has(body.status)
      ? (body.status as "refused" | "needs_review" | "extracted")
      : "";
    if (!status) return c.json({ error: "invalid_result", detail: "status" }, 400);
    const detail = optStr(body.detail, MAX_DETAIL);
    if (detail === "bad") return c.json({ error: "invalid_result", detail: "detail" }, 400);
    const boxFileId = optStr(body.box_file_id, 200);
    if (boxFileId === "bad") return c.json({ error: "invalid_result", detail: "box_file_id" }, 400);
    // A refused doc is never filed — a box_file_id alongside 'refused' is a contract
    // violation (Invariant 2: the daemon's input is untrusted too).
    if (status === "refused" && boxFileId) {
      return c.json({ error: "invalid_result", detail: "box_file_id_forbidden" }, 400);
    }
    let extraction: ExtractionBody | null = null;
    if (status === "extracted") {
      const parsed = parseExtraction(body.extraction);
      if (typeof parsed === "string") return c.json({ error: parsed }, 400);
      extraction = parsed;
    } else if (body.extraction !== undefined && body.extraction !== null) {
      return c.json({ error: "invalid_result", detail: "extraction_forbidden" }, 400);
    }

    // R4 round-trip auto-bind (additive, ADVISORY — the daemon's verified Tier-0
    // rfq-form:v1 hint). A shape-bad value is IGNORED (never 400s the whole result —
    // Invariant 2: the daemon's bind hint is untrusted; a bad hint degrades to "no bind").
    const rfqNumberHint = optStr(body.rfq_number, MAX_SHORT);
    const rfqVendorKeyHint = optStr(body.rfq_vendor_key, MAX_SHORT);
    const rfqHintWellFormed =
      rfqNumberHint !== null && rfqNumberHint !== "bad" &&
      rfqVendorKeyHint !== null && rfqVendorKeyHint !== "bad" &&
      VENDOR_KEY_RE.test(rfqVendorKeyHint);

    const row = await c.env.DB
      .prepare("SELECT id, status, job_no FROM po_estimates WHERE id = ?1")
      .bind(estId)
      .first<{ id: number; status: string; job_no: string }>();
    if (!row || (row.status !== "pending" && row.status !== "claimed")) {
      return c.json({ ok: true, found: false, status: row?.status ?? null });
    }

    // Resolve the hint to real rows: bind ONLY when the RFQ number names an rfqs row FOR THIS
    // ESTIMATE'S OWN JOB (job_no cross-check — a vendor is routinely on RFQs for several
    // concurrent jobs, and rfq_vendors.vendor_key is per-RFQ, not global; without this a
    // cross-job hint would bind cleanly) AND that rfq has an rfq_vendors row for this vendor.
    // An unknown / cross-job rfq / vendor → no bind (stored nothing; recorded in the audit as
    // rfq_bound:false — visible, never silent).
    let bindRfqId: number | null = null;
    let bindVendorKey: string | null = null;
    if (rfqHintWellFormed) {
      const rfqRow = await c.env.DB
        .prepare("SELECT id FROM rfqs WHERE rfq_number = ?1 AND job_no = ?2")
        .bind(rfqNumberHint, row.job_no)
        .first<{ id: number }>();
      if (rfqRow) {
        const vRow = await c.env.DB
          .prepare("SELECT 1 AS x FROM rfq_vendors WHERE rfq_id = ?1 AND vendor_key = ?2")
          .bind(rfqRow.id, rfqVendorKeyHint)
          .first<{ x: number }>();
        if (vRow) {
          bindRfqId = rfqRow.id;
          bindVendorKey = rfqVendorKeyHint as string;
        }
      }
    }

    const stmts = [
      c.env.DB
        .prepare(
          "UPDATE po_estimates SET status = ?1, detail = ?2, box_file_id = ?3, " +
            "doc_type = COALESCE(?4, doc_type), screened_at = unixepoch(), " +
            "extracted_at = CASE WHEN ?1 = 'extracted' THEN unixepoch() ELSE extracted_at END " +
            "WHERE id = ?5 AND status IN ('pending','claimed')",
        )
        .bind(status, detail, boxFileId, extraction ? extraction.doc_type : null, estId),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "po_estimate_result", String(estId), {
        estimate_id: estId, status, box_file_id: boxFileId, detail,
        tier: extraction?.tier ?? null, lines: extraction?.lines.length ?? null,
        // The rfq-bind observability: whether a hint arrived (well-formed) and whether it
        // actually resolved to a bind. An unresolved hint is recorded, never silently dropped.
        rfq_hint: rfqHintWellFormed ? rfqNumberHint : null,
        rfq_bound: bindRfqId !== null,
      }),
    ];
    if (status === "refused") {
      // Delete-on-refusal: the subselect reads the status AFTER the UPDATE above
      // (same batch), so bytes are dropped exactly when the row is now refused.
      stmts.push(
        c.env.DB
          .prepare(
            "DELETE FROM po_estimate_chunks WHERE estimate_id = ?1 " +
              "AND (SELECT status FROM po_estimates WHERE id = ?1) = 'refused'",
          )
          .bind(estId),
      );
    }
    if (extraction) {
      const e = extraction;
      // Guarded on the row being 'extracted' NOW (the UPDATE above flipped it) AND on
      // no extraction existing yet — a lost race (a concurrent result post won) leaves
      // the winner's extraction alone and inserts nothing here.
      stmts.push(
        c.env.DB
          .prepare(
            "INSERT INTO estimate_extractions (estimate_id, tier, schema_version, doc_type, " +
              "vendor_name, quote_number, revision_label, quote_date, valid_until, " +
              "subtotal_cents, tax_cents, freight_cents, misc_cents, grand_total_cents, " +
              "math_ok, confidence, payload_json, anomalies) " +
              "SELECT ?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18 " +
              "WHERE (SELECT status FROM po_estimates WHERE id = ?1) = 'extracted' " +
              "AND NOT EXISTS (SELECT 1 FROM estimate_extractions WHERE estimate_id = ?1 AND superseded = 0)",
          )
          .bind(
            estId, e.tier, e.schema_version, e.doc_type, e.vendor_name, e.quote_number,
            e.revision_label, e.quote_date, e.valid_until, e.subtotal_cents, e.tax_cents,
            e.freight_cents, e.misc_cents, e.grand_total_cents, e.math_ok, e.confidence,
            e.payload_json, e.anomalies,
          ),
        ...e.lines.map((l) =>
          c.env.DB
            .prepare(
              "INSERT INTO estimate_extraction_lines (extraction_id, position, section, part_number, " +
                "description, qty, unit, unit_cost_cents, extended_cents, math_ok, line_note) " +
                "SELECT (SELECT id FROM estimate_extractions WHERE estimate_id = ?1 AND superseded = 0 " +
                "ORDER BY id DESC LIMIT 1), ?2,?3,?4,?5,?6,?7,?8,?9,?10,?11 " +
                "WHERE EXISTS (SELECT 1 FROM estimate_extractions WHERE estimate_id = ?1 AND superseded = 0)",
            )
            .bind(
              estId, l.position, l.section, l.part_number, l.description, l.qty, l.unit,
              l.unit_cost_cents, l.extended_cents, l.math_ok, l.line_note,
            ),
        ),
      );
    }
    if (bindRfqId !== null && bindVendorKey !== null) {
      stmts.push(
        // Bind the estimate to its RFQ (idempotent: rfq_id IS NULL binds once; guarded on
        // the row now carrying a live result status — same batch, so it sees the flip above).
        c.env.DB
          .prepare(
            "UPDATE po_estimates SET rfq_id = ?2, rfq_vendor_key = ?3 " +
              "WHERE id = ?1 AND rfq_id IS NULL AND status IN ('needs_review','extracted')",
          )
          .bind(estId, bindRfqId, bindVendorKey),
        // Flip the vendor's rfq_vendors row to responded — FORWARD-ONLY (filed|sent →
        // responded): a vendor may return the form BEFORE the RFQ email is even sent (office
        // upload), so BOTH 'filed' and 'sent' are valid predecessors. responded_estimate_id
        // records the estimate the reply landed as. rfqs.status is deliberately UNTOUCHED
        // (its CHECK set has no 'responded'). GATED on the estimate's OWN status being a
        // genuine result (same batch, sees the flip above) — mirrors the po_estimates bind
        // guard so a `refused` result post carrying rfq hints can NEVER forge a 'responded'
        // pill for an unbound (garbage/invoice) upload.
        c.env.DB
          .prepare(
            "UPDATE rfq_vendors SET status='responded', responded_estimate_id=?3 " +
              "WHERE rfq_id=?1 AND vendor_key=?2 AND status IN ('filed','sent') " +
              "AND (SELECT status FROM po_estimates WHERE id=?3) IN ('needs_review','extracted')",
          )
          .bind(bindRfqId, bindVendorKey, estId),
        auditStmtIfChanged(c, SYSTEM_ACTOR, "po_estimate_rfq_bound", String(estId), {
          estimate_id: estId, rfq_id: bindRfqId, vendor_key: bindVendorKey,
        }),
      );
    }
    let res;
    try {
      res = await c.env.DB.batch(stmts);
    } catch (err) {
      // The UNIQUE(extraction_id, position) backstop on a lost concurrent-result race:
      // the loser's whole batch rolled back (the winner's data is intact) — report the
      // idempotent no-op the daemon already treats as benign.
      if (isUniqueViolation(err)) return c.json({ ok: true, found: false });
      throw err;
    }
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // POST /api/po/estimates/internal/:id/preview — upsert one rendered page PNG (the
  // disposition screen's source-fidelity surface, ADR decision 3). ≤1MB decoded,
  // PNG-magic-checked, page-bounded; upsert + changes()-gated audit in ONE batch (W4).
  // LIVENESS-GUARDED in-WHERE (the /chunks-read status idiom): the upsert lands only
  // while the estimate is still live (PREVIEW_LIVE_STATUSES) — a post on a terminal row
  // is 409 estimate_terminal, and a row that goes terminal BETWEEN the pre-check and the
  // batch inserts nothing (changes()=0 → no audit → the same 409).
  app.post("/api/po/estimates/internal/:id/preview", gates.requireEstimateToken, async (c) => {
    const estId = parseIdParam(c.req.param("id"));
    if (estId === null) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const page = typeof body.page === "number" && Number.isSafeInteger(body.page) &&
      body.page >= 1 && body.page <= MAX_PREVIEW_PAGES
      ? body.page
      : null;
    if (page === null) return c.json({ error: "invalid_page" }, 400);
    const pngB64 = typeof body.png_b64 === "string" ? body.png_b64 : "";
    if (pngB64.length === 0 || pngB64.length % 4 !== 0) return c.json({ error: "invalid_data" }, 400);
    if (b64DecodedLen(pngB64) > PREVIEW_MAX_BYTES) return c.json({ error: "preview_too_large" }, 413);
    if (!B64_RE.test(pngB64)) return c.json({ error: "invalid_data" }, 400);
    let head: Uint8Array;
    try {
      head = b64ToBytes(pngB64.slice(0, 12));
    } catch {
      return c.json({ error: "invalid_data" }, 400);
    }
    if (!magicMatches(head.subarray(0, 8), "png")) return c.json({ error: "not_png" }, 422);

    const row = await c.env.DB
      .prepare("SELECT status FROM po_estimates WHERE id = ?1")
      .bind(estId)
      .first<{ status: string }>();
    if (!row) return c.json({ error: "not_found" }, 404);
    if (!PREVIEW_LIVE_STATUSES.has(row.status)) return c.json({ error: "estimate_terminal" }, 409);

    // The in-WHERE guard is the authority (TOCTOU-safe) — the pre-check above only picks
    // the error shape. The guarded SELECT satisfies both branches of the upsert: a
    // terminal status yields zero source rows, so neither the INSERT nor the DO UPDATE
    // can land.
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "INSERT INTO estimate_previews (estimate_id, page, png_b64) " +
            "SELECT ?1, ?2, ?3 " +
            "WHERE (SELECT status FROM po_estimates WHERE id = ?1) " +
            "IN ('pending','claimed','needs_review','extracted') " +
            "ON CONFLICT(estimate_id, page) DO UPDATE SET png_b64 = excluded.png_b64",
        )
        .bind(estId, page, pngB64),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "po_estimate_preview", String(estId), {
        estimate_id: estId, page, size_b64: pngB64.length,
      }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      // The row went terminal mid-flight — same refusal as the pre-check.
      return c.json({ error: "estimate_terminal" }, 409);
    }
    return c.json({ ok: true, found: true });
  });

  // ══ Browser surface (session + cap.po.manage — the po.ts gate pair) ═════════════

  // POST /api/po/estimates — office upload. Body: { job_no, job_name?, vendor_key?,
  // filename, mime, data_b64 }. The whole file rides ONE request (base64 in JSON — the
  // attachment wire); the Worker decodes once, signs est:v1, splits into ≤1MB-decoded
  // chunks, and lands parent + ALL chunks + audit in ONE db.batch (W4). The partial
  // UNIQUE sha256 index is the dedupe authority → 409 duplicate_estimate.
  app.post("/api/po/estimates", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);

    const jobNo = str(body.job_no);
    if (!JOB_NO_RE.test(jobNo)) return c.json({ error: "invalid_job_no" }, 400);
    // 0069 — the job identity, OPTIONAL so the route stays backward-compatible with any
    // caller that only knows the project number. Bounded but not existence-checked: an
    // estimate is a snapshot, and refusing an upload because a job row moved would lose the
    // vendor's document over a bookkeeping mismatch. A job_id that resolves to nothing simply
    // leaves the site un-seeded, which is exactly today's behaviour.
    const jobIdRaw = optStr(body.job_id, MAX_SHORT);
    if (jobIdRaw === "bad") return c.json({ error: "invalid_job_id" }, 400);
    // optStr returns NULL for absent/blank, but the column is NOT NULL DEFAULT '' — binding
    // null would be a constraint violation, i.e. a 500 on every upload that omits the job.
    const jobId = jobIdRaw ?? "";
    const jobName = optStr(body.job_name, MAX_TEXT);
    if (jobName === "bad") return c.json({ error: "invalid_job_name" }, 400);
    const vendorKey = optStr(body.vendor_key, MAX_SHORT);
    if (vendorKey === "bad" || (vendorKey && !VENDOR_KEY_RE.test(vendorKey))) {
      return c.json({ error: "invalid_vendor_key" }, 400);
    }
    const filename = str(body.filename);
    const declaredMime = str(body.mime);
    const dataB64 = typeof body.data_b64 === "string" ? body.data_b64 : "";

    // Cheap gates first — allowlist + filename/extension consistency, then base64
    // shape + size BEFORE any decode materializes bytes (the po_attachments order).
    if (!Object.prototype.hasOwnProperty.call(MIME_ALLOWLIST, declaredMime)) {
      return c.json({ error: "mime_not_allowed" }, 422);
    }
    const nameProblem = filenameProblem(filename, declaredMime);
    if (nameProblem) return c.json({ error: nameProblem }, nameProblem === "extension_mime_mismatch" ? 422 : 400);
    if (dataB64.length === 0 || dataB64.length % 4 !== 0) {
      return c.json({ error: "invalid_data" }, 400);
    }
    if (b64DecodedLen(dataB64) > ESTIMATE_MAX_BYTES) {
      return c.json({ error: "estimate_too_large" }, 413);
    }
    if (!B64_RE.test(dataB64)) {
      return c.json({ error: "invalid_data" }, 400);
    }
    let bytes: Uint8Array;
    try {
      bytes = b64ToBytes(dataB64);
    } catch {
      return c.json({ error: "invalid_data" }, 400);
    }
    if (bytes.length === 0 || bytes.length > ESTIMATE_MAX_BYTES) {
      return c.json({ error: "estimate_too_large" }, 413);
    }
    // Magic sniff vs the DECLARED MIME — a PNG named .pdf is a mismatch even though
    // PNG itself is allowlisted.
    if (!magicMatches(bytes.subarray(0, 8), MIME_ALLOWLIST[declaredMime].magic)) {
      return c.json({ error: "magic_mime_mismatch" }, 422);
    }

    if (!c.env.HMAC_PAYLOAD_SECRET) return c.json({ error: "hmac_secret_missing" }, 500);
    const sha256 = await sha256Hex(bytes);
    const estUuid = crypto.randomUUID();
    const hmac = await hmacHex(
      c.env.HMAC_PAYLOAD_SECRET,
      estimateCanonical(estUuid, jobNo, filename, declaredMime, bytes.length, sha256),
    );

    const chunkTotal = Math.ceil(bytes.length / EST_CHUNK_DECODED_MAX);
    const chunkStmts = [];
    for (let i = 0; i < chunkTotal; i++) {
      const slice = bytes.subarray(i * EST_CHUNK_DECODED_MAX, (i + 1) * EST_CHUNK_DECODED_MAX);
      chunkStmts.push(
        c.env.DB
          .prepare(
            "INSERT INTO po_estimate_chunks (estimate_id, chunk_index, chunk_total, chunk_b64) " +
              "SELECT (SELECT id FROM po_estimates WHERE est_uuid = ?1), ?2, ?3, ?4 " +
              "WHERE EXISTS (SELECT 1 FROM po_estimates WHERE est_uuid = ?1)",
          )
          .bind(estUuid, i, chunkTotal, bytesToB64(slice)),
      );
    }

    const actor = c.get("session").username;
    // ONE batch (W4): parent INSERT → audit → chunk INSERTs (each guarded on the parent
    // row existing). A dedupe hit (partial UNIQUE sha256 over live rows) aborts the
    // WHOLE batch — no parent, no chunks, no audit — and maps to 409.
    try {
      await c.env.DB.batch([
        c.env.DB
          .prepare(
            "INSERT INTO po_estimates (est_uuid, job_no, job_id, job_name, vendor_key, filename, " +
              "declared_mime, size_bytes, sha256, status, hmac, uploaded_by, family_key) " +
              "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,'pending',?10,?11,?12)",
          )
          .bind(
            estUuid, jobNo, jobId, jobName, vendorKey, filename, declaredMime, bytes.length,
            sha256, hmac, actor,
            // family_key: sha256 fallback until a body-derived vendor|quote_number
            // identity lands at extraction (E4 — ADR decision 7).
            sha256,
          ),
        auditStmt(c, actor, "po_estimate_upload", estUuid, {
          est_uuid: estUuid, job_no: jobNo, filename, declared_mime: declaredMime,
          size_bytes: bytes.length, sha256,
        }),
        ...chunkStmts,
      ]);
    } catch (e) {
      if (isUniqueViolation(e)) return c.json({ error: "duplicate_estimate" }, 409);
      throw e;
    }
    const row = await c.env.DB
      .prepare("SELECT id FROM po_estimates WHERE est_uuid = ?1")
      .bind(estUuid)
      .first<{ id: number }>();
    return c.json({ ok: true, id: row?.id ?? null, filename, size_bytes: bytes.length }, 201);
  });

  // GET /api/po/estimates?status=&limit= — the tracker list (metadata only; never the
  // hmac, never bytes).
  app.get("/api/po/estimates", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const q = c.req.query();
    const STATUSES = new Set([
      "pending", "claimed", "refused", "needs_review", "extracted", "imported", "rejected", "superseded",
    ]);
    const status = STATUSES.has(q.status ?? "") ? (q.status as string) : "all";
    const limit = Math.min(Math.max(parseInt(q.limit || "50", 10) || 50, 1), LIST_CAP);
    const { results } = await c.env.DB
      .prepare(
        `SELECT ${ROW_COLS} FROM po_estimates WHERE (?1 = 'all' OR status = ?1) ` +
          "ORDER BY created_at DESC, id DESC LIMIT ?2",
      )
      .bind(status, limit)
      .all<Record<string, unknown>>();
    return c.json({ estimates: results ?? [] });
  });

  // GET /api/po/estimates/:id — one estimate + its latest live extraction (+ lines) +
  // the preview page count (the disposition screen read).
  app.get("/api/po/estimates/:id", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const estId = parseIdParam(c.req.param("id"));
    if (estId === null) return c.json({ error: "invalid_id" }, 400);
    const estimate = await c.env.DB
      .prepare(`SELECT ${ROW_COLS} FROM po_estimates WHERE id = ?1`)
      .bind(estId)
      .first<Record<string, unknown>>();
    if (!estimate) return c.json({ error: "not_found" }, 404);
    const extraction = await c.env.DB
      .prepare(
        "SELECT id, estimate_id, tier, schema_version, doc_type, vendor_name, quote_number, " +
          "revision_label, quote_date, valid_until, subtotal_cents, tax_cents, freight_cents, " +
          "misc_cents, grand_total_cents, math_ok, confidence, anomalies, created_at " +
          "FROM estimate_extractions WHERE estimate_id = ?1 AND superseded = 0 " +
          "ORDER BY id DESC LIMIT 1",
      )
      .bind(estId)
      .first<Record<string, unknown>>();
    let lines: Record<string, unknown>[] = [];
    if (extraction) {
      const { results } = await c.env.DB
        .prepare(
          "SELECT id, position, section, part_number, description, qty, unit, unit_cost_cents, " +
            "extended_cents, math_ok, line_note, disposition, edited_json " +
            "FROM estimate_extraction_lines WHERE extraction_id = ?1 ORDER BY position ASC",
        )
        .bind(extraction.id as number)
        .all<Record<string, unknown>>();
      lines = results ?? [];
    }
    const pv = await c.env.DB
      .prepare("SELECT COUNT(*) AS n FROM estimate_previews WHERE estimate_id = ?1")
      .bind(estId)
      .first<{ n: number }>();
    return c.json({
      estimate,
      extraction: extraction ?? null,
      lines,
      preview_count: pv?.n ?? 0,
    });
  });

  // GET /api/po/estimates/:id/preview/:page — one rendered page PNG for the disposition
  // screen's side-by-side (session-gated; serves the RENDER, never the original bytes).
  app.get(
    "/api/po/estimates/:id/preview/:page",
    gates.requireSession,
    gates.requireCapability(CAP_PO),
    async (c) => {
      const estId = parseIdParam(c.req.param("id"));
      const page = parseIdParam(c.req.param("page"));
      if (estId === null || page === null) return c.json({ error: "invalid_id" }, 400);
      // Read-side liveness backstop: a refused/rejected estimate's evidence never serves
      // (refusal deletes chunks but NOT previews synchronously — a lingering page must
      // 404 here until the prune backstop reaps it). IMPORTED and SUPERSEDED rows
      // deliberately MAY still serve: the operator may revisit an imported estimate's
      // evidence while any backstop-retained page remains (normally none — dispose
      // deletes previews in the same batch).
      const est = await c.env.DB
        .prepare("SELECT status FROM po_estimates WHERE id = ?1")
        .bind(estId)
        .first<{ status: string }>();
      if (!est || est.status === "refused" || est.status === "rejected") {
        return c.json({ error: "not_found" }, 404);
      }
      const row = await c.env.DB
        .prepare("SELECT png_b64 FROM estimate_previews WHERE estimate_id = ?1 AND page = ?2")
        .bind(estId, page)
        .first<{ png_b64: string }>();
      if (!row) return c.json({ error: "not_found" }, 404);
      let bytes: Uint8Array;
      try {
        bytes = b64ToBytes(row.png_b64);
      } catch {
        return c.json({ error: "internal_error" }, 500);
      }
      return new Response(bytes as unknown as BodyInit, {
        headers: {
          "content-type": "image/png",
          "cache-control": "private, no-store",
        },
      });
    },
  );

  // POST /api/po/estimates/:id/dispose — the human exit from the reviewable states.
  // Body: { action: 'imported'|'rejected', po_id? (imported — the draft just minted
  // through the EXISTING /api/po/drafts route), line_dispositions?,
  // no_preview_verified? }. ONE db.batch (W4): the guarded status UPDATE
  // (extracted|needs_review only — second call → 409 already_disposed; for 'imported'
  // ALSO cross-checked in-WHERE against purchase_orders.estimate_id — a po_id that does
  // not reference THIS estimate lands 0 rows → 409 po_estimate_mismatch) → audit →
  // line-disposition UPDATEs → previews + chunks DELETE (bytes leave D1 at disposition).
  // Line/cleanup statements are guarded in-WHERE on the row now carrying the target
  // status (?-bound — never interpolated); on a lost same-action double-click race the
  // loser's identical metadata writes are benign and its reply is the 409.
  //
  // PREVIEW-EVIDENCE GATE (server-side twin of the SPA's red-team-#2 gate — ADR
  // decision 3): an 'imported' disposal that ACCEPTS (or edits) extraction lines must
  // carry evidence the reviewer could see the source — ≥1 rendered preview page for this
  // estimate OR the explicit no_preview_verified:true acknowledgment; neither → 422
  // preview_evidence_required. Manual-only disposals (no accepted/edited extraction
  // lines) are exempt, exactly the SPA gate's scope. The SPA enforces this for UX; THIS
  // is the boundary (Invariant 2 — the browser is never trusted). Whichever evidence
  // path passed is recorded in the audit payload (preview_pages / no_preview_verified).
  app.post("/api/po/estimates/:id/dispose", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const estId = parseIdParam(c.req.param("id"));
    if (estId === null) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const action = body.action === "imported" || body.action === "rejected" ? body.action : null;
    if (action === null) return c.json({ error: "invalid_action" }, 400);
    let poId: number | null = null;
    if (body.po_id !== undefined && body.po_id !== null) {
      if (typeof body.po_id !== "number" || !Number.isSafeInteger(body.po_id) || body.po_id <= 0) {
        return c.json({ error: "invalid_po_id" }, 400);
      }
      poId = body.po_id;
    }
    if (action === "imported" && poId === null) return c.json({ error: "invalid_po_id" }, 400);
    if (action === "rejected" && poId !== null) return c.json({ error: "invalid_po_id" }, 400);

    interface LineDisposition { line_id: number; disposition: string; edited_json: string | null }
    const lineDispositions: LineDisposition[] = [];
    if (body.line_dispositions !== undefined && body.line_dispositions !== null) {
      if (!Array.isArray(body.line_dispositions) || body.line_dispositions.length > MAX_LINE_DISPOSITIONS) {
        return c.json({ error: "invalid_line_dispositions" }, 400);
      }
      const seen = new Set<number>();
      for (const r of body.line_dispositions) {
        if (!isPlainObject(r)) return c.json({ error: "invalid_line_dispositions" }, 400);
        const lineId = typeof r.line_id === "number" && Number.isSafeInteger(r.line_id) && r.line_id > 0
          ? r.line_id
          : null;
        const disposition = typeof r.disposition === "string" && LINE_DISPOSITIONS.has(r.disposition)
          ? r.disposition
          : null;
        if (lineId === null || disposition === null || seen.has(lineId)) {
          return c.json({ error: "invalid_line_dispositions" }, 400);
        }
        seen.add(lineId);
        const editedJson = optStr(r.edited_json, MAX_EDITED_JSON);
        if (editedJson === "bad") return c.json({ error: "invalid_line_dispositions" }, 400);
        if (disposition === "edited" && !editedJson) return c.json({ error: "invalid_line_dispositions" }, 400);
        if (disposition !== "edited" && editedJson) return c.json({ error: "invalid_line_dispositions" }, 400);
        lineDispositions.push({ line_id: lineId, disposition, edited_json: editedJson });
      }
    }

    // The explicit "no preview available — I verified against the original document"
    // acknowledgment (the disposition screen's checkbox). Shape-guarded: a present
    // non-boolean is a contract violation.
    let noPreviewVerified = false;
    if (body.no_preview_verified !== undefined && body.no_preview_verified !== null) {
      if (typeof body.no_preview_verified !== "boolean") {
        return c.json({ error: "invalid_no_preview_verified" }, 400);
      }
      noPreviewVerified = body.no_preview_verified;
    }

    const row = await c.env.DB
      .prepare("SELECT status FROM po_estimates WHERE id = ?1")
      .bind(estId)
      .first<{ status: string }>();
    if (!row) return c.json({ error: "not_found" }, 404);
    if (row.status !== "extracted" && row.status !== "needs_review") {
      return c.json({ error: "already_disposed", status: row.status }, 409);
    }

    // ── The preview-evidence fidelity gate (see the route header). Counted BEFORE the
    // batch below deletes the pages, so the audit records the evidence that existed at
    // decision time.
    const acceptsExtraction = lineDispositions.some(
      (l) => l.disposition === "accepted" || l.disposition === "edited",
    );
    const pv = await c.env.DB
      .prepare("SELECT COUNT(*) AS n FROM estimate_previews WHERE estimate_id = ?1")
      .bind(estId)
      .first<{ n: number }>();
    const previewPages = pv?.n ?? 0;
    if (action === "imported" && acceptsExtraction && previewPages === 0 && !noPreviewVerified) {
      return c.json({ error: "preview_evidence_required" }, 422);
    }

    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE po_estimates SET status = ?2, po_id = ?3, disposed_at = unixepoch() " +
            "WHERE id = ?1 AND status IN ('extracted','needs_review') " +
            // po_id cross-check: an 'imported' flip must name a draft PO that ACTUALLY
            // carries this estimate's provenance (purchase_orders.estimate_id, 0055).
            // ?3 IS NULL is the 'rejected' path (poId is null exactly then); a foreign /
            // mismatched po_id lands 0 rows → 409 po_estimate_mismatch below.
            "AND (?3 IS NULL OR EXISTS " +
            "(SELECT 1 FROM purchase_orders WHERE id = ?3 AND estimate_id = ?1))",
        )
        .bind(estId, action, poId),
      auditStmtIfChanged(c, actor, "po_estimate_dispose", String(estId), {
        estimate_id: estId, action, po_id: poId, line_dispositions: lineDispositions.length,
        // The fidelity-gate evidence record: which path (rendered pages vs the explicit
        // acknowledgment) authorized an accepting import.
        preview_pages: previewPages, no_preview_verified: noPreviewVerified,
      }),
      // The human color-coding record — scoped to THIS estimate's lines and guarded on
      // the disposition having landed (the ?1-anchored status subquery, action ?-bound).
      ...lineDispositions.map((l) =>
        c.env.DB
          .prepare(
            "UPDATE estimate_extraction_lines SET disposition = ?2, edited_json = ?3 " +
              "WHERE id = ?4 AND extraction_id IN " +
              "(SELECT id FROM estimate_extractions WHERE estimate_id = ?1) " +
              "AND (SELECT status FROM po_estimates WHERE id = ?1) = ?5",
          )
          .bind(estId, l.disposition, l.edited_json, l.line_id, action),
      ),
      // Delete-on-disposition: previews + original bytes leave D1 (byte-free rows stay
      // as the manifest / provenance record).
      c.env.DB
        .prepare(
          "DELETE FROM estimate_previews WHERE estimate_id = ?1 " +
            "AND (SELECT status FROM po_estimates WHERE id = ?1) = ?2",
        )
        .bind(estId, action),
      c.env.DB
        .prepare(
          "DELETE FROM po_estimate_chunks WHERE estimate_id = ?1 " +
            "AND (SELECT status FROM po_estimates WHERE id = ?1) = ?2",
        )
        .bind(estId, action),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const now = await c.env.DB.prepare("SELECT status FROM po_estimates WHERE id = ?1").bind(estId).first<{ status: string }>();
      // Still reviewable → the status guard passed and it was the po_id cross-check that
      // refused the flip: the supplied draft does not reference THIS estimate.
      if (now && (now.status === "extracted" || now.status === "needs_review")) {
        return c.json({ error: "po_estimate_mismatch" }, 409);
      }
      return c.json({ error: "already_disposed", status: now?.status ?? null }, 409);
    }
    return c.json({ ok: true, id: estId, status: action });
  });
}
