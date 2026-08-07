import type { Context, MiddlewareHandler } from "hono";
import type { Env, Vars } from "./types";
import type { FieldopsApp } from "./fieldops_gates";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { hmacHex } from "./hmac";
import { b64DecodedLen, B64_RE } from "./photo_bounds";
import {
  readExpectationFields,
  catalogIdValid,
  type ExpectationFields,
} from "./fieldops_expected_materials";

// ─────────────────────────────────────────────────────────────────────────────
// Materials-manifest import pool (PR3b) — worker/fieldops_manifests.ts
//
// The SEND-FREE D1 landing area for an office-uploaded materials MANIFEST (a BOM or a
// shipping log) plus the bearer-gated internal tier the Mac daemon
// (field_ops/manifest_poll.py) drains. The §34 Option-D pattern — po_attachments (0053)
// → po_estimates (0054) — applied to field-ops, and a near-verbatim clone of the estimate
// pool: same claim-first lifecycle, same content-covered HMAC, same chunked byte wire,
// same W4 mutation+audit atomicity.
//
// WHAT THIS WORKER DOES AND DOES NOT DO. It bounds-gates the upload (size / filename /
// declared-MIME allowlist / magic sniff), signs manifest:v1 over the decoded bytes, and
// queues them. It NEVER parses a manifest: the cell-grid extraction is openpyxl /
// pdfplumber over untrusted bytes and runs on the Mac inside a killable child
// (po_materials.estimate_sandbox). Bytes only ever flow Mac-ward, over the chunks route
// below and no other; the browser reads the PARSED GRID and rendered PNG previews, never
// the original bytes.
//
// LIFECYCLE: pending → claimed (Mac claim-FIRST marker) → (refused | parsed) →
// (committing → committed | discarded). Chunks die at refusal AND at parse — D1 holds
// manifest bytes only while the document is mid-pipeline. The parsed GRID outlives the
// bytes, because the validate screen edits against it.
//
// DEDUPE IS PER-JOB, THE ONE DELIBERATE DIVERGENCE FROM THE ESTIMATE LANE. po_estimates
// carries a GLOBAL partial-unique sha256 index. Here it is (job_id, sha256) over live
// rows, because a master BOM legitimately covers sibling jobs (Bradley 1 / Bradley 2 are
// separate jobs served by one document) and a global index would let whichever job
// imported it first lock the second one out with a 409 it could never clear. The
// signature binds job_id precisely so that this widened dedupe cannot become a
// cross-job replay.
//
// MIME ALLOWLIST IS NARROWER THAN THE ESTIMATE LANE — PDF + XLSX only. Every allowed type
// must be one the parser can actually read; accepting a .docx here would queue a document
// that can only ever end in `refused`, after it had already been screened and stored.
//
// ORDER DEPENDENCY: migration 0060 must be applied to the live D1 BEFORE this Worker
// deploys, or every route here 500s. (And `git -C ~/its pull origin main` to latest FIRST
// — the stale-migrations-list lockout class.)
// ─────────────────────────────────────────────────────────────────────────────

export type ManifestGates = {
  requireSession: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  requireCapability: (cap: string) => MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  /** Bearer gate for /api/fieldops/manifests/internal/* — the manifest_poll daemon's OWN
   *  token tier (PORTAL_MANIFEST_API_TOKEN), privilege-separated from the PO / estimate /
   *  RFQ / portal / admin / fieldops / config / subcontract tokens. Built in index.ts next
   *  to its siblings. Same reasoning as the estimate tier: this process decodes hostile
   *  PDF/xlsx bytes, so a compromise of it must reach nothing but this pool. */
  requireManifestToken: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
};

type Ctx = Context<{ Bindings: Env; Variables: Vars }>;

// The office authors a job's material list, so the import rides the SAME capability the
// hand-authored line create does (cap.materials.manage, itself a job-scope bypass cap).
const CAP_MANIFEST = "cap.materials.manage";
export const MANIFEST_HMAC_DOMAIN = "manifest:v1";
const SYSTEM_ACTOR = "system:manifest_poll";

// A BOM PDF in this corpus runs to a few hundred KB; the ceiling is generous for a scanned
// master BOM and still far under the Worker request budget.
const MANIFEST_MAX_BYTES = 25 * 1024 * 1024;
const MANIFEST_CHUNK_DECODED_MAX = 1_000_000; // ≤1MB decoded per chunk (0053/0054 shape)
const MAX_MANIFEST_FILENAME = 200;
const MANIFEST_PENDING_CAP = 25;
const LIST_CAP = 100;
const MAX_DETAIL = 200;
const MAX_PROFILE = 32;
const MAX_PARSE_NOTES = 4000;
const MAX_COLUMN_MAP_JSON = 40_000;
const MAX_HEADER_META_JSON = 20_000;
const MAX_PREVIEW_PAGES = 24;
const PREVIEW_MAX_DECODED = 1_000_000;
// One posted page of the parsed grid. A 900-row master BOM therefore takes ~5 posts; the
// daemon pages so no single request carries a whole document.
const MAX_ROWS_PER_POST = 200;
const MAX_ROW_CELLS = 200; // mirrors estimate_sandbox.XLSX_MAX_COLS_PER_ROW
const MAX_CELL_CHARS = 2000; // mirrors estimate_sandbox.XLSX_MAX_CELL_CHARS
const MAX_ROWS_TOTAL = 20_000; // absolute ceiling on one manifest's grid
const MAX_FLAGS = 200;
const MAX_SOURCE_LABEL = 64;
const ROWS_READ_CAP = 2000; // browser grid page size ceiling

// One /commit call's worth of LINES. Deliberately well under the read route's own
// LIMIT 500 per job so a single page can never be the thing that overflows the
// materials page; the watermark makes as many pages as a document needs.
const COMMIT_PAGE_LINES = 100;
// A whole import's ceiling, checked against what the job ALREADY holds. The read route
// caps a job's line list at LIMIT 500 — an import that pushes past it would SILENTLY
// truncate the materials page and the daily form, which is worse than refusing.
const MAX_JOB_LINES = 500;
// A /plan is a dry run over the same set, so it may see the whole document at once.
const MAX_PLAN_LINES = 2000;

// Mirrors the 0060 CHECK. The daemon may stamp only these two — `committing`/`committed`
// belong to the browser commit and `discarded` to the browser discard.
const RESULT_STATUSES = new Set(["refused", "parsed"]);
// ParsedRow.kind — mirrors the 0060 CHECK and manifest_parse's KIND_* vocabulary.
const ROW_KINDS = new Set(["header", "data", "continuation", "section", "meta"]);
// The statuses a row/preview post may still LAND on: in-pipeline (the daemon posts ahead
// of its own result) or reviewable (a re-parse). Terminal rows refuse the write, so a late
// daemon post cannot resurrect a discarded or refused manifest's evidence.
const GRID_LIVE_STATUSES = new Set(["pending", "claimed", "parsed"]);

// ── Allowlist: declared MIME ⇄ extension ⇄ magic. PDF + XLSX ONLY (see the header).
type Magic = "pdf" | "zip";
const MIME_ALLOWLIST: Record<string, { exts: string[]; magic: Magic }> = {
  "application/pdf": { exts: [".pdf"], magic: "pdf" },
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
    case "zip": // PK\x03\x04
      return head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04;
  }
}

/** Filename gate — the po_attachments / po_estimates rules verbatim (bounded, no path
 *  separators / control chars / leading dot / Unicode bidi-zero-width spoofers; extension
 *  must belong to the declared MIME). The name lands in D1 and in the Box file. */
function filenameProblem(filename: string, declaredMime: string): string | null {
  if (filename.length < 1 || filename.length > MAX_MANIFEST_FILENAME) return "invalid_filename";
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

/** The manifest:v1 canonical string — ORDER + "\n" SEPARATOR are load-bearing; the Mac
 *  (shared/portal_hmac.py `manifest_canonical`) recomputes it byte-for-byte before trusting
 *  a row. Binds identity (manifest_uuid, job_id), naming (filename, mime), and content
 *  (size_bytes, sha256) — a tampered row OR tampered bytes fail verify. The job_id slot is
 *  what makes the per-job dedupe safe: byte-identical manifests legitimately exist under
 *  two jobs, and only this binding stops one being replayed onto the other. */
export function manifestCanonical(
  manifestUuid: string, jobId: string, filename: string, declaredMime: string,
  sizeBytes: number, sha256: string,
): string {
  return [MANIFEST_HMAC_DOMAIN, manifestUuid, jobId, filename, declaredMime, String(sizeBytes), sha256].join("\n");
}

// ── Small helpers (the po_estimates idioms; each route module keeps its own copies) ──
function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}
function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
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

/** One posted grid row, already shape-validated. `cells` is stored as a JSON array
 *  VERBATIM — the validate screen edits against it, so nothing is pre-collapsed and
 *  duplicate part numbers survive to get a per-row human decision. */
type GridRow = {
  row_index: number;
  source_page: string | null;
  kind: string;
  cells_json: string;
  flags: string | null;
};

/** Validate one posted row. Returns the storable shape or an error CODE string. */
function readGridRow(raw: unknown): GridRow | string {
  if (!isPlainObject(raw)) return "invalid_row";
  const rowIndex = raw.row_index;
  if (typeof rowIndex !== "number" || !Number.isSafeInteger(rowIndex) || rowIndex < 1 || rowIndex > MAX_ROWS_TOTAL) {
    return "invalid_row_index";
  }
  const kind = str(raw.kind);
  if (!ROW_KINDS.has(kind)) return "invalid_row_kind";
  const sourcePage = optStr(raw.source_page, MAX_SOURCE_LABEL);
  if (sourcePage === "bad") return "invalid_source_page";
  const flags = optStr(raw.flags, MAX_FLAGS);
  if (flags === "bad") return "invalid_row_flags";
  if (!Array.isArray(raw.cells) || raw.cells.length > MAX_ROW_CELLS) return "invalid_row_cells";
  // Cells arrive already normalized to strings by the Mac parser. Re-bound them here
  // anyway — this is a bearer-gated route, not a trusted one, and the grid is rendered
  // into a browser.
  const cells: string[] = [];
  for (const cell of raw.cells) {
    if (typeof cell !== "string" || cell.length > MAX_CELL_CHARS) return "invalid_row_cells";
    cells.push(cell);
  }
  return { row_index: rowIndex, source_page: sourcePage, kind, cells_json: JSON.stringify(cells), flags };
}

/** One line the human has resolved on the validate screen, ready to become a
 *  `job_expected_materials` row. `source_row_index` is the ParsedRow.index it came from —
 *  it is what makes the paged commit replay-safe, because the watermark is expressed in
 *  SOURCE rows rather than in "how many lines did we insert last time". */
type ResolvedLine = { fields: ExpectationFields; source_row_index: number };

/** Validate one posted line. Returns the resolved shape or an error CODE string.
 *
 *  NOTE it delegates to the SAME `readExpectationFields` the hand-authored create route
 *  uses. That is the point: an imported line runs the identical bounds, the identical
 *  cross-field rule and the identical error vocabulary, so the two paths cannot drift.
 *  In particular `description_required` fires here exactly as it does there — a manifest
 *  row carrying only a part number and a quantity is REFUSED with its row index rather
 *  than silently given a synthesized description, because inventing field data is the one
 *  thing an importer must never do (§4). The validate screen maps the document's
 *  description column, so a well-formed import already carries one. */
function readResolvedLine(raw: unknown): ResolvedLine | string {
  if (!isPlainObject(raw)) return "invalid_line";
  const idx = raw.source_row_index;
  if (typeof idx !== "number" || !Number.isSafeInteger(idx) || idx < 1 || idx > MAX_ROWS_TOTAL) {
    return "invalid_source_row_index";
  }
  const fields = readExpectationFields(raw);
  if (typeof fields === "string") return fields;
  return { fields, source_row_index: idx };
}

/** Read + validate a posted `lines` array. */
function readLines(body: Record<string, unknown>, cap: number): ResolvedLine[] | { error: string; row?: number } {
  if (!Array.isArray(body.lines) || body.lines.length === 0 || body.lines.length > cap) {
    return { error: "invalid_lines" };
  }
  const out: ResolvedLine[] = [];
  for (const [i, raw] of body.lines.entries()) {
    const line = readResolvedLine(raw);
    if (typeof line === "string") return { error: line, row: i };
    out.push(line);
  }
  return out;
}

/** The manifest row a plan/commit call is operating on, plus its job. */
async function loadManifest(
  c: Ctx,
  id: number,
): Promise<{ id: number; job_id: string; status: string; committed_through_row: number } | null> {
  return c.env.DB
    .prepare("SELECT id, job_id, status, committed_through_row FROM job_manifests WHERE id = ?1")
    .bind(id)
    .first<{ id: number; job_id: string; status: string; committed_through_row: number }>();
}

export function registerManifestRoutes(app: FieldopsApp, gates: ManifestGates): void {
  // ══ Internal surface (requireManifestToken — the Mac-side manifest_poll daemon) ══
  // Registered FIRST so the static /internal/* segment can never be captured by the
  // browser tier's /:id parameter.

  // GET /api/fieldops/manifests/internal/pending — live pool rows oldest-first: pending +
  // claimed (claimed re-served for crash recovery — every servicing step is idempotent).
  // Serves metadata + the HMAC (the Mac's verify input); bytes ride the chunks read below.
  app.get("/api/fieldops/manifests/internal/pending", gates.requireManifestToken, async (c) => {
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "25", 10) || 25, 1), MANIFEST_PENDING_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT id, manifest_uuid, job_id, filename, declared_mime, size_bytes, sha256, " +
          "status, hmac, uploaded_by, created_at " +
          "FROM job_manifests WHERE status IN ('pending','claimed') " +
          "ORDER BY created_at ASC, id ASC LIMIT ?1",
      )
      .bind(limit)
      .all<Record<string, unknown>>();
    return c.json({ manifests: results ?? [] });
  });

  // POST /api/fieldops/manifests/internal/:id/claim — claim-first marker: pending→claimed,
  // guarded in-WHERE + changes()-gated audit (W4). Idempotent: found:false when already
  // claimed/disposed — the daemon proceeds on a row it already claimed (crash recovery).
  app.post("/api/fieldops/manifests/internal/:id/claim", gates.requireManifestToken, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare("UPDATE job_manifests SET status='claimed' WHERE id = ?1 AND status = 'pending'")
        .bind(id),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "job_manifest_claim", String(id), { manifest_id: id }),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // GET /api/fieldops/manifests/internal/:id/chunks — the Mac-ward byte read (the ONLY
  // route that ever serves original document bytes, bearer-gated). Live rows only.
  app.get("/api/fieldops/manifests/internal/:id/chunks", gates.requireManifestToken, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const row = await c.env.DB
      .prepare("SELECT status FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<{ status: string }>();
    if (!row || (row.status !== "pending" && row.status !== "claimed")) {
      return c.json({ error: "not_found" }, 404);
    }
    const { results } = await c.env.DB
      .prepare(
        "SELECT chunk_index, chunk_total, chunk_b64 FROM job_manifest_chunks " +
          "WHERE manifest_id = ?1 ORDER BY chunk_index ASC",
      )
      .bind(id)
      .all<Record<string, unknown>>();
    return c.json({ chunks: results ?? [] });
  });

  // POST /api/fieldops/manifests/internal/:id/rows — one PAGE of the parsed grid.
  // Body: { rows: [{ row_index, source_page?, kind, cells: [...], flags? }] }.
  // UPSERT on (manifest_id, row_index) so a re-posted page is a no-op and a crashed daemon
  // simply re-posts from the start next cycle. Every INSERT is guarded on the parent row
  // still being live, so a page racing a discard lands nothing.
  app.post("/api/fieldops/manifests/internal/:id/rows", gates.requireManifestToken, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    if (!Array.isArray(body.rows) || body.rows.length === 0 || body.rows.length > MAX_ROWS_PER_POST) {
      return c.json({ error: "invalid_rows" }, 400);
    }
    const parent = await c.env.DB
      .prepare("SELECT status FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<{ status: string }>();
    if (!parent || !GRID_LIVE_STATUSES.has(parent.status)) return c.json({ error: "not_found" }, 404);

    const rows: GridRow[] = [];
    for (const [i, raw] of body.rows.entries()) {
      const row = readGridRow(raw);
      if (typeof row === "string") return c.json({ error: row, row: i }, 400);
      rows.push(row);
    }
    const stmts = rows.map((r) =>
      c.env.DB
        .prepare(
          "INSERT INTO job_manifest_rows (manifest_id, row_index, source_page, kind, cells_json, flags) " +
            "SELECT ?1, ?2, ?3, ?4, ?5, ?6 WHERE EXISTS " +
            "(SELECT 1 FROM job_manifests WHERE id = ?1 AND status IN ('pending','claimed','parsed')) " +
            "ON CONFLICT (manifest_id, row_index) DO UPDATE SET " +
            "source_page=excluded.source_page, kind=excluded.kind, " +
            "cells_json=excluded.cells_json, flags=excluded.flags",
        )
        .bind(id, r.row_index, r.source_page, r.kind, r.cells_json, r.flags),
    );
    const res = await c.env.DB.batch(stmts);
    const written = res.reduce((n, r) => n + (r.meta.changes ?? 0), 0);
    return c.json({ ok: true, written });
  });

  // POST /api/fieldops/manifests/internal/:id/preview — one rendered source page, so the
  // validate screen can show the SOURCE beside the editable grid without the browser ever
  // touching the original untrusted bytes.
  app.post("/api/fieldops/manifests/internal/:id/preview", gates.requireManifestToken, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const page = body.page;
    if (typeof page !== "number" || !Number.isSafeInteger(page) || page < 1 || page > MAX_PREVIEW_PAGES) {
      return c.json({ error: "invalid_page" }, 400);
    }
    const pngB64 = typeof body.png_b64 === "string" ? body.png_b64 : "";
    if (pngB64.length === 0 || pngB64.length % 4 !== 0 || !B64_RE.test(pngB64)) {
      return c.json({ error: "invalid_data" }, 400);
    }
    if (b64DecodedLen(pngB64) > PREVIEW_MAX_DECODED) return c.json({ error: "preview_too_large" }, 413);
    const parent = await c.env.DB
      .prepare("SELECT status FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<{ status: string }>();
    if (!parent || !GRID_LIVE_STATUSES.has(parent.status)) return c.json({ error: "not_found" }, 404);
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "INSERT INTO job_manifest_previews (manifest_id, page, png_b64) " +
            "SELECT ?1, ?2, ?3 WHERE EXISTS " +
            "(SELECT 1 FROM job_manifests WHERE id = ?1 AND status IN ('pending','claimed','parsed')) " +
            "ON CONFLICT (manifest_id, page) DO UPDATE SET png_b64=excluded.png_b64",
        )
        .bind(id, page, pngB64),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // POST /api/fieldops/manifests/internal/result — apply one servicing outcome.
  // Body: { manifest_id, status: 'refused'|'parsed', detail?, box_file_id? (forbidden on
  // refused — a refused doc is never filed), profile?, row_count?, column_map?,
  // header_meta?, parse_notes? }. ONE atomic batch (W4): guarded status UPDATE →
  // changes()-gated audit → chunk DELETE (on BOTH outcomes: a refused document's bytes are
  // dropped, and a parsed one's are no longer needed because the grid now exists).
  // Idempotent: a re-post for an already-disposed/unknown row is { ok:true, found:false }.
  app.post("/api/fieldops/manifests/internal/result", gates.requireManifestToken, async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const id =
      typeof body.manifest_id === "number" && Number.isSafeInteger(body.manifest_id) && body.manifest_id > 0
        ? body.manifest_id
        : null;
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const status = str(body.status);
    if (!RESULT_STATUSES.has(status)) return c.json({ error: "invalid_status" }, 400);
    const detail = optStr(body.detail, MAX_DETAIL);
    if (detail === "bad") return c.json({ error: "invalid_detail" }, 400);
    const profile = optStr(body.profile, MAX_PROFILE);
    if (profile === "bad") return c.json({ error: "invalid_profile" }, 400);
    const boxFileId = optStr(body.box_file_id, 64);
    if (boxFileId === "bad") return c.json({ error: "invalid_box_file_id" }, 400);
    // A refused manifest is never filed, so a box_file_id on a refusal is a contradiction
    // rather than a harmless extra — refuse it loudly instead of storing a dangling ref.
    if (status === "refused" && boxFileId !== null) return c.json({ error: "box_file_id_on_refusal" }, 400);
    const parseNotes = optStr(body.parse_notes, MAX_PARSE_NOTES);
    if (parseNotes === "bad") return c.json({ error: "invalid_parse_notes" }, 400);
    let rowCount: number | null = null;
    if (body.row_count !== undefined && body.row_count !== null) {
      if (
        typeof body.row_count !== "number" || !Number.isSafeInteger(body.row_count) ||
        body.row_count < 0 || body.row_count > MAX_ROWS_TOTAL
      ) {
        return c.json({ error: "invalid_row_count" }, 400);
      }
      rowCount = body.row_count;
    }
    let columnMapJson: string | null = null;
    if (body.column_map !== undefined && body.column_map !== null) {
      if (!isPlainObject(body.column_map)) return c.json({ error: "invalid_column_map" }, 400);
      columnMapJson = JSON.stringify(body.column_map);
      if (columnMapJson.length > MAX_COLUMN_MAP_JSON) return c.json({ error: "invalid_column_map" }, 400);
    }
    let headerMetaJson: string | null = null;
    if (body.header_meta !== undefined && body.header_meta !== null) {
      if (!isPlainObject(body.header_meta)) return c.json({ error: "invalid_header_meta" }, 400);
      headerMetaJson = JSON.stringify(body.header_meta);
      if (headerMetaJson.length > MAX_HEADER_META_JSON) return c.json({ error: "invalid_header_meta" }, 400);
    }

    // Guarded in-WHERE on the CLAIMABLE states so a result cannot resurrect a discarded or
    // already-committed row. The chunk DELETE's subselect reads the POST-update status, so
    // it fires exactly when this batch is the one that disposed the row.
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE job_manifests SET status = ?2, detail = ?3, profile = COALESCE(?4, profile), " +
            "box_file_id = COALESCE(?5, box_file_id), row_count = COALESCE(?6, row_count), " +
            "column_map_json = COALESCE(?7, column_map_json), " +
            "header_meta_json = COALESCE(?8, header_meta_json), " +
            "parse_notes = COALESCE(?9, parse_notes), " +
            "screened_at = unixepoch(), parsed_at = CASE WHEN ?2 = 'parsed' THEN unixepoch() ELSE parsed_at END " +
            "WHERE id = ?1 AND status IN ('pending','claimed')",
        )
        .bind(id, status, detail, profile, boxFileId, rowCount, columnMapJson, headerMetaJson, parseNotes),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "job_manifest_result", String(id), {
        manifest_id: id, status, detail, profile, row_count: rowCount,
      }),
      c.env.DB
        .prepare(
          "DELETE FROM job_manifest_chunks WHERE manifest_id = ?1 " +
            "AND EXISTS (SELECT 1 FROM job_manifests WHERE id = ?1 AND status IN ('refused','parsed'))",
        )
        .bind(id),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // ══ Browser surface (session + cap.materials.manage) ═════════════════════════════

  // POST /api/fieldops/manifests — office upload. Body: { job_id, filename, mime,
  // data_b64 }. The whole file rides ONE request (base64 in JSON — the attachment wire);
  // the Worker decodes once, signs manifest:v1, splits into ≤1MB-decoded chunks, and lands
  // parent + ALL chunks + audit in ONE db.batch (W4). The PER-JOB partial UNIQUE
  // (job_id, sha256) index is the dedupe authority → 409 duplicate_manifest.
  app.post("/api/fieldops/manifests", gates.requireSession, gates.requireCapability(CAP_MANIFEST), async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);

    const jobId = str(body.job_id);
    if (!jobId || jobId.length > 64) return c.json({ error: "invalid_job_id" }, 400);
    const jobRow = await c.env.DB.prepare("SELECT job_id FROM jobs WHERE job_id = ?1").bind(jobId).first();
    if (!jobRow) return c.json({ error: "not_found" }, 404);

    const filename = str(body.filename);
    const declaredMime = str(body.mime);
    const dataB64 = typeof body.data_b64 === "string" ? body.data_b64 : "";

    // Cheap gates first — allowlist + filename/extension consistency, then base64 shape +
    // size BEFORE any decode materializes bytes (the po_attachments order).
    if (!Object.prototype.hasOwnProperty.call(MIME_ALLOWLIST, declaredMime)) {
      return c.json({ error: "mime_not_allowed" }, 422);
    }
    const nameProblem = filenameProblem(filename, declaredMime);
    if (nameProblem) return c.json({ error: nameProblem }, nameProblem === "extension_mime_mismatch" ? 422 : 400);
    if (dataB64.length === 0 || dataB64.length % 4 !== 0) return c.json({ error: "invalid_data" }, 400);
    if (b64DecodedLen(dataB64) > MANIFEST_MAX_BYTES) return c.json({ error: "manifest_too_large" }, 413);
    if (!B64_RE.test(dataB64)) return c.json({ error: "invalid_data" }, 400);
    let bytes: Uint8Array;
    try {
      bytes = b64ToBytes(dataB64);
    } catch {
      return c.json({ error: "invalid_data" }, 400);
    }
    if (bytes.length === 0 || bytes.length > MANIFEST_MAX_BYTES) {
      return c.json({ error: "manifest_too_large" }, 413);
    }
    // Magic sniff vs the DECLARED MIME — an xlsx named .pdf is a mismatch even though
    // xlsx itself is allowlisted.
    if (!magicMatches(bytes.subarray(0, 8), MIME_ALLOWLIST[declaredMime].magic)) {
      return c.json({ error: "magic_mime_mismatch" }, 422);
    }

    if (!c.env.HMAC_PAYLOAD_SECRET) return c.json({ error: "hmac_secret_missing" }, 500);
    const sha256 = await sha256Hex(bytes);
    const manifestUuid = crypto.randomUUID();
    const hmac = await hmacHex(
      c.env.HMAC_PAYLOAD_SECRET,
      manifestCanonical(manifestUuid, jobId, filename, declaredMime, bytes.length, sha256),
    );

    const chunkTotal = Math.ceil(bytes.length / MANIFEST_CHUNK_DECODED_MAX);
    const chunkStmts = [];
    for (let i = 0; i < chunkTotal; i++) {
      const slice = bytes.subarray(i * MANIFEST_CHUNK_DECODED_MAX, (i + 1) * MANIFEST_CHUNK_DECODED_MAX);
      chunkStmts.push(
        c.env.DB
          .prepare(
            "INSERT INTO job_manifest_chunks (manifest_id, chunk_index, chunk_total, chunk_b64) " +
              "SELECT (SELECT id FROM job_manifests WHERE manifest_uuid = ?1), ?2, ?3, ?4 " +
              "WHERE EXISTS (SELECT 1 FROM job_manifests WHERE manifest_uuid = ?1)",
          )
          .bind(manifestUuid, i, chunkTotal, bytesToB64(slice)),
      );
    }

    const actor = c.get("session").username;
    // ONE batch (W4): parent INSERT → audit → chunk INSERTs (each guarded on the parent row
    // existing). A dedupe hit (the PER-JOB partial UNIQUE over live rows) aborts the WHOLE
    // batch — no parent, no chunks, no audit — and maps to 409.
    try {
      await c.env.DB.batch([
        c.env.DB
          .prepare(
            "INSERT INTO job_manifests (manifest_uuid, job_id, filename, declared_mime, " +
              "size_bytes, sha256, status, hmac, uploaded_by) " +
              "VALUES (?1,?2,?3,?4,?5,?6,'pending',?7,?8)",
          )
          .bind(manifestUuid, jobId, filename, declaredMime, bytes.length, sha256, hmac, actor),
        auditStmt(c, actor, "job_manifest_upload", jobId, {
          manifest_uuid: manifestUuid, job_id: jobId, filename,
          declared_mime: declaredMime, size_bytes: bytes.length, sha256,
        }),
        ...chunkStmts,
      ]);
    } catch (e) {
      if (isUniqueViolation(e)) return c.json({ error: "duplicate_manifest" }, 409);
      throw e;
    }
    const row = await c.env.DB
      .prepare("SELECT id FROM job_manifests WHERE manifest_uuid = ?1")
      .bind(manifestUuid)
      .first<{ id: number }>();
    return c.json({ ok: true, id: row?.id ?? null, filename, size_bytes: bytes.length }, 201);
  });

  // GET /api/fieldops/manifests?job_id=&limit= — the per-job list (metadata only; never
  // the hmac, never bytes).
  app.get("/api/fieldops/manifests", gates.requireSession, gates.requireCapability(CAP_MANIFEST), async (c) => {
    const jobId = str(c.req.query("job_id"));
    if (!jobId || jobId.length > 64) return c.json({ error: "invalid_job_id" }, 400);
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "50", 10) || 50, 1), LIST_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT id, manifest_uuid, job_id, filename, declared_mime, size_bytes, status, detail, " +
          "profile, row_count, mode, committed_through_row, uploaded_by, box_file_id, " +
          "created_at, parsed_at, committed_at " +
          "FROM job_manifests WHERE job_id = ?1 ORDER BY created_at DESC, id DESC LIMIT ?2",
      )
      .bind(jobId, limit)
      .all<Record<string, unknown>>();
    return c.json({ manifests: results ?? [] });
  });

  // GET /api/fieldops/manifests/:id — one manifest's header, the proposed column map and
  // the parser's notes (the validate screen's evidence pane). Never the hmac, never bytes.
  app.get("/api/fieldops/manifests/:id", gates.requireSession, gates.requireCapability(CAP_MANIFEST), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const row = await c.env.DB
      .prepare(
        "SELECT id, manifest_uuid, job_id, filename, declared_mime, size_bytes, status, detail, " +
          "profile, row_count, column_map_json, header_meta_json, parse_notes, mode, " +
          "merge_options_json, committed_through_row, uploaded_by, box_file_id, " +
          "created_at, screened_at, parsed_at, committed_at " +
          "FROM job_manifests WHERE id = ?1",
      )
      .bind(id)
      .first<Record<string, unknown>>();
    if (!row) return c.json({ error: "not_found" }, 404);
    const pages = await c.env.DB
      .prepare("SELECT page FROM job_manifest_previews WHERE manifest_id = ?1 ORDER BY page ASC")
      .bind(id)
      .all<{ page: number }>();
    return c.json({ manifest: row, preview_pages: (pages.results ?? []).map((p) => p.page) });
  });

  // GET /api/fieldops/manifests/:id/rows?after=&limit= — the parsed grid, paged. `after` is
  // a row_index cursor (the grid is naturally ordered and stable), so the validate screen
  // can stream a 900-row master BOM without one enormous response.
  app.get("/api/fieldops/manifests/:id/rows", gates.requireSession, gates.requireCapability(CAP_MANIFEST), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const after = Math.max(parseInt(c.req.query("after") || "0", 10) || 0, 0);
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "500", 10) || 500, 1), ROWS_READ_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT row_index, source_page, kind, cells_json, flags FROM job_manifest_rows " +
          "WHERE manifest_id = ?1 AND row_index > ?2 ORDER BY row_index ASC LIMIT ?3",
      )
      .bind(id, after, limit)
      .all<Record<string, unknown>>();
    return c.json({ rows: results ?? [] });
  });

  // GET /api/fieldops/manifests/:id/preview/:page — one rendered source page as a PNG data
  // payload. This is the ONLY view a browser ever gets of the source document; the original
  // bytes never leave the Mac-ward chunks route.
  app.get(
    "/api/fieldops/manifests/:id/preview/:page",
    gates.requireSession,
    gates.requireCapability(CAP_MANIFEST),
    async (c) => {
      const id = parseIdParam(c.req.param("id"));
      const page = parseIdParam(c.req.param("page"));
      if (id === null || page === null) return c.json({ error: "invalid_id" }, 400);
      const row = await c.env.DB
        .prepare("SELECT png_b64 FROM job_manifest_previews WHERE manifest_id = ?1 AND page = ?2")
        .bind(id, page)
        .first<{ png_b64: string }>();
      if (!row) return c.json({ error: "not_found" }, 404);
      return c.json({ page, png_b64: row.png_b64 });
    },
  );

  // POST /api/fieldops/manifests/:id/plan — DRY RUN. Body: { lines: [...] }. Computes,
  // against the job's LIVE lines, what committing this set would do. Writes nothing.
  //
  // AMBIGUOUS IS A FIRST-CLASS OUTCOME, not a rounding error. Duplicate part numbers are
  // universal in the real BOMs — 7000153 appears twice in three of the four sample
  // Customer BOMs under different groupings — so an incoming part number that matches
  // MORE THAN ONE existing line has no single correct target, and the screen must ask.
  // Silently picking the first match is the failure this route exists to prevent.
  app.post(
    "/api/fieldops/manifests/:id/plan",
    gates.requireSession,
    gates.requireCapability(CAP_MANIFEST),
    async (c) => {
      const id = parseIdParam(c.req.param("id"));
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      let body: Record<string, unknown>;
      try {
        body = (await c.req.json()) as Record<string, unknown>;
      } catch {
        return c.json({ error: "bad_request" }, 400);
      }
      if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
      const manifest = await loadManifest(c, id);
      if (!manifest) return c.json({ error: "not_found" }, 404);
      const parsed = readLines(body, MAX_PLAN_LINES);
      if (!Array.isArray(parsed)) return c.json(parsed, 400);

      const { results } = await c.env.DB
        .prepare(
          "SELECT id, part_number FROM job_expected_materials " +
            "WHERE job_id = ?1 AND active = 1 ORDER BY id ASC LIMIT ?2",
        )
        .bind(manifest.job_id, MAX_JOB_LINES)
        .all<{ id: number; part_number: string | null }>();
      const existing = results ?? [];
      const byPart = new Map<string, number[]>();
      for (const row of existing) {
        if (!row.part_number) continue;
        const ids = byPart.get(row.part_number) ?? [];
        ids.push(row.id);
        byPart.set(row.part_number, ids);
      }

      const matched: { source_row_index: number; part_number: string; line_id: number }[] = [];
      const ambiguous: { source_row_index: number; part_number: string; line_ids: number[] }[] = [];
      const fresh: number[] = [];
      const seenParts = new Set<string>();
      for (const line of parsed) {
        const part = line.fields.part_number;
        if (part) seenParts.add(part);
        const hits = part ? (byPart.get(part) ?? []) : [];
        if (hits.length === 1) {
          matched.push({ source_row_index: line.source_row_index, part_number: part!, line_id: hits[0] });
        } else if (hits.length > 1) {
          ambiguous.push({ source_row_index: line.source_row_index, part_number: part!, line_ids: hits });
        } else {
          fresh.push(line.source_row_index);
        }
      }
      // ON LIST BUT ABSENT: a line the job already expects that this document does NOT
      // mention. Reported, never auto-retired — a partial BOM is not a deletion order.
      const absent = existing
        .filter((row) => row.part_number && !seenParts.has(row.part_number))
        .map((row) => ({ line_id: row.id, part_number: row.part_number }));

      return c.json({
        ok: true,
        job_id: manifest.job_id,
        committed_through_row: manifest.committed_through_row,
        counts: {
          incoming: parsed.length,
          matched: matched.length,
          ambiguous: ambiguous.length,
          new: fresh.length,
          absent: absent.length,
          existing: existing.length,
        },
        matched,
        ambiguous,
        absent,
        // What committing every remaining line would leave the job holding, against the
        // read route's own LIMIT 500 — over that, the materials page SILENTLY truncates.
        projected_total: existing.length + fresh.length,
        would_exceed_line_cap: existing.length + fresh.length > MAX_JOB_LINES,
      });
    },
  );

  // POST /api/fieldops/manifests/:id/commit — ONE PAGE of the import. Body:
  // { mode: 'merge'|'add_new', lines: [...] }.
  //
  // PAGED WITH A WATERMARK. A 900-row master BOM cannot commit inside one Worker request,
  // so each call lands at most COMMIT_PAGE_LINES lines and advances
  // `committed_through_row` IN THE SAME db.batch() as the inserts. Two consequences, both
  // load-bearing: a page either fully lands or fully rolls back, and a REPLAYED page is a
  // no-op because every line at or below the watermark is dropped before any write. The
  // caller simply re-posts the remainder until `done` comes back true.
  app.post(
    "/api/fieldops/manifests/:id/commit",
    gates.requireSession,
    gates.requireCapability(CAP_MANIFEST),
    async (c) => {
      const id = parseIdParam(c.req.param("id"));
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      let body: Record<string, unknown>;
      try {
        body = (await c.req.json()) as Record<string, unknown>;
      } catch {
        return c.json({ error: "bad_request" }, 400);
      }
      if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
      const mode = str(body.mode);
      if (mode !== "merge" && mode !== "add_new") return c.json({ error: "invalid_mode" }, 400);
      const manifest = await loadManifest(c, id);
      if (!manifest) return c.json({ error: "not_found" }, 404);
      const parsedLines = readLines(body, MAX_PLAN_LINES);
      if (!Array.isArray(parsedLines)) return c.json(parsedLines, 400);

      // REPLAY GUARD FIRST, before the status guard. Everything at or below the watermark
      // is already committed, so a fully-replayed payload is an idempotent no-op — and
      // that ordering matters: if the LAST page's response is lost in flight the client
      // retries, and answering a 409 there would report failure for work that succeeded,
      // which is exactly the ambiguity the watermark exists to remove.
      const remaining = parsedLines
        .filter((l) => l.source_row_index > manifest.committed_through_row)
        .sort((a, b) => a.source_row_index - b.source_row_index);
      if (remaining.length === 0) {
        return c.json({
          ok: true, done: true, inserted: 0,
          committed_through_row: manifest.committed_through_row,
        });
      }
      // Only a parsed (or mid-commit) manifest may author NEW lines. A refused or
      // discarded one has no business doing so, and a committed one has nothing left —
      // any payload reaching here carries rows above its watermark, so this is a real
      // attempt, not a replay.
      if (manifest.status !== "parsed" && manifest.status !== "committing") {
        return c.json({ error: "not_committable", status: manifest.status }, 409);
      }
      const page = remaining.slice(0, COMMIT_PAGE_LINES);
      const watermark = page[page.length - 1].source_row_index;

      // The job's line list has a hard read cap; blowing past it would silently truncate
      // the materials page and the daily form rather than fail visibly.
      const existing = await c.env.DB
        .prepare("SELECT COUNT(*) n FROM job_expected_materials WHERE job_id = ?1 AND active = 1")
        .bind(manifest.job_id)
        .first<{ n: number }>();
      if ((existing?.n ?? 0) + page.length > MAX_JOB_LINES) {
        return c.json({ error: "line_cap_exceeded", cap: MAX_JOB_LINES }, 409);
      }

      // catalogIdValid is ONE D1 round-trip per call, so resolve the DISTINCT ids once
      // rather than awaiting per line (a 100-line page would otherwise be 100 reads).
      const distinct = [...new Set(page.map((l) => l.fields.material_id).filter((m): m is number => m !== null))];
      for (const materialId of distinct) {
        if (!(await catalogIdValid(c, materialId))) {
          return c.json({ error: "invalid_material_id" }, 400);
        }
      }

      const actor = c.get("session").username;
      const stmts = [];
      let seq = ((existing?.n ?? 0) + 1) * 10;
      for (const line of page) {
        const f = line.fields;
        stmts.push(
          c.env.DB
            .prepare(
              `INSERT INTO job_expected_materials
                 (job_id, material_id, description, qty, unit, expected_date, seq, line_uuid,
                  part_number, category, expected_ship_date)
               VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)`,
            )
            // line_uuid MUST be minted: it is UNIQUE, and the §51 Material List mirror
            // uses it as its find-or-create key. SQLite permits multiple NULLs there, so
            // omitting it does not error — it silently breaks the mirror's upsert.
            // status stays 'expected' and unplanned stays 0 (schema defaults): an import
            // is on-manifest by definition, and the delivery SoR is the receipt ledger,
            // never a shipping log's delivery_date column.
            .bind(
              manifest.job_id, f.material_id, f.description, f.qty, f.unit, f.expected_date,
              (seq += 10), crypto.randomUUID(), f.part_number, f.category, f.expected_ship_date,
            ),
          // UNCONDITIONAL auditStmt, never auditStmtIfChanged: `changes()` reads the LAST
          // data-modifying statement, so in a batch interleaving N inserts every IfChanged
          // after the first would read the wrong statement's result.
          auditStmt(c, actor, "expected_material_create", manifest.job_id, {
            job_id: manifest.job_id, manifest_id: id, source_row_index: line.source_row_index,
            part_number: f.part_number, description: f.description, qty: f.qty,
          }),
        );
      }
      // The watermark advances IN THE SAME BATCH as the inserts — that pairing is what
      // makes a page atomic and a replay a no-op. Guarded in-WHERE so a concurrent
      // discard cannot be overwritten, and monotonic so an out-of-order page cannot
      // rewind it.
      stmts.push(
        c.env.DB
          .prepare(
            "UPDATE job_manifests SET status='committing', committed_through_row = ?2, mode = ?3 " +
              "WHERE id = ?1 AND status IN ('parsed','committing') AND committed_through_row < ?2",
          )
          .bind(id, watermark, mode),
      );

      try {
        await c.env.DB.batch(stmts);
      } catch (e) {
        if (isUniqueViolation(e)) return c.json({ error: "duplicate_line" }, 409);
        throw e;
      }

      const done = remaining.length <= page.length;
      if (done) {
        await c.env.DB.batch([
          c.env.DB
            .prepare(
              "UPDATE job_manifests SET status='committed', committed_at = unixepoch() " +
                "WHERE id = ?1 AND status = 'committing'",
            )
            .bind(id),
          auditStmtIfChanged(c, actor, "job_manifest_commit", String(id), {
            manifest_id: id, job_id: manifest.job_id, mode,
          }),
        ]);
      }
      return c.json({ ok: true, done, inserted: page.length, committed_through_row: watermark });
    },
  );

  // POST /api/fieldops/manifests/:id/discard — the office abandons a manifest. Guarded
  // in-WHERE on the non-terminal states, and drops the grid, previews and any surviving
  // chunks in the SAME batch so a discarded manifest leaves no evidence behind. A
  // discarded row leaves the per-job dedupe index, so the same document may be re-uploaded.
  app.post(
    "/api/fieldops/manifests/:id/discard",
    gates.requireSession,
    gates.requireCapability(CAP_MANIFEST),
    async (c) => {
      const id = parseIdParam(c.req.param("id"));
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            "UPDATE job_manifests SET status='discarded' WHERE id = ?1 " +
              "AND status IN ('pending','claimed','refused','parsed')",
          )
          .bind(id),
        auditStmtIfChanged(c, actor, "job_manifest_discard", String(id), { manifest_id: id }),
        c.env.DB
          .prepare(
            "DELETE FROM job_manifest_chunks WHERE manifest_id = ?1 " +
              "AND EXISTS (SELECT 1 FROM job_manifests WHERE id = ?1 AND status = 'discarded')",
          )
          .bind(id),
        c.env.DB
          .prepare(
            "DELETE FROM job_manifest_rows WHERE manifest_id = ?1 " +
              "AND EXISTS (SELECT 1 FROM job_manifests WHERE id = ?1 AND status = 'discarded')",
          )
          .bind(id),
        c.env.DB
          .prepare(
            "DELETE FROM job_manifest_previews WHERE manifest_id = ?1 " +
              "AND EXISTS (SELECT 1 FROM job_manifests WHERE id = ?1 AND status = 'discarded')",
          )
          .bind(id),
      ]);
      const changed = (res[0].meta.changes ?? 0) > 0;
      if (!changed) {
        const row = await c.env.DB
          .prepare("SELECT status FROM job_manifests WHERE id = ?1")
          .bind(id)
          .first<{ status: string }>();
        if (!row) return c.json({ error: "not_found" }, 404);
        return c.json({ error: "not_discardable", status: row.status }, 409);
      }
      return c.json({ ok: true, id });
    },
  );
}
