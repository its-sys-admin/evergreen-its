import type { MiddlewareHandler } from "hono";
import type { FieldopsApp } from "./fieldops_gates";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { hmacHex } from "./hmac";
import { b64DecodedLen, B64_RE } from "./photo_bounds";

// ─────────────────────────────────────────────────────────────────────────────
// Job-schedule import pool (ADR-0006) — worker/fieldops_schedules.ts
//
// The SEND-FREE D1 landing area for an office-uploaded PROJECT SCHEDULE (a Smartsheet
// Gantt PDF export) plus the bearer-gated internal tier the Mac daemon
// (field_ops/schedule_poll.py) drains. The §34 Option-D pattern — po_attachments (0053)
// → po_estimates (0054) → job_manifests (0060) — applied to the schedule lane, and a
// disciplined clone of the manifest pool: same claim-first lifecycle, same
// content-covered HMAC, same chunked byte wire, same W4 mutation+audit atomicity.
//
// WHAT THIS WORKER DOES AND DOES NOT DO. It bounds-gates the upload (size / filename /
// declared-MIME allowlist / magic sniff), signs schedule:v1 over the decoded bytes, and
// queues them. It NEVER parses a schedule: the corpus PDFs carry NO text layer (the
// table text is vector glyph outlines), so extraction is a Quartz render + Apple Vision
// OCR + geometry reconstruction that runs on the Mac inside a killable child
// (po_materials.estimate_sandbox). Bytes only ever flow Mac-ward, over the chunks route
// below and no other; the browser reads the PROPOSED GRID and rendered PNG previews,
// never the original bytes.
//
// LIFECYCLE: pending → claimed (Mac claim-FIRST marker) → (refused | parsed) →
// (committing → committed | superseded | discarded). `superseded` is the lane's own
// divergence from 0060 — schedule REVISIONS are expected (the corpus shows one job
// re-exported up to 14 times), so exactly one upload may govern a job at a time
// (idx_job_schedules_one_committed) and a newly-committed revision displaces the old
// one supersede-FIRST in the commit route's final batch (PR-4/PR-6 of the lane; this
// module ships the pool + validate-read surface, not the commit).
//
// Chunks die at refusal AND at parse. The proposed GRID and the page PREVIEWS outlive
// the bytes AND the commit — they are the evidence the validate screen edits against and
// the next revision's reconcile screen shows.
//
// DEDUPE IS PER-JOB, EXACT-SHA, over rows still in play. A revision is by definition a
// different PDF so it never collides; only a byte-identical replay of a live document
// 409s, while refused / discarded / SUPERSEDED rows leave the index — re-uploading a
// superseded revision's exact bytes IS the rollback path for a wrong-file commit. The
// signature binds job_id precisely so the per-job dedupe cannot become a cross-job
// replay.
//
// MIME ALLOWLIST IS PDF ONLY — narrower again than the manifest lane. Every allowed type
// must be one the parser can actually read (the 0060 rule), and the schedule parser
// reads Smartsheet Gantt PDF exports.
//
// ORDER DEPENDENCY: migration 0066 must be applied to the live D1 BEFORE this Worker
// deploys, or every route here 500s. (And `git -C ~/its pull origin main` to latest
// FIRST — the stale-migrations-list lockout class.)
// ─────────────────────────────────────────────────────────────────────────────

export type ScheduleGates = {
  requireSession: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  requireCapability: (cap: string) => MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  /** Bearer gate for /api/fieldops/schedules/internal/* — the schedule_poll daemon's OWN
   *  token tier (PORTAL_SCHEDULE_API_TOKEN), privilege-separated from the PO / estimate /
   *  RFQ / manifest / portal / admin / fieldops / config / subcontract tokens. Built in
   *  index.ts next to its siblings. Same reasoning as the manifest tier: this process
   *  decodes hostile PDF bytes (Quartz render + Vision OCR inside a killable child), so a
   *  compromise of it must reach nothing but this pool. */
  requireScheduleToken: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
};

// The office owns the job lifecycle, and a project schedule is a job-lifecycle artifact —
// so the import rides cap.jobtracker.manage (admin-only in practice: manager is withheld
// it, migration 0023), the 0060 precedent of riding an existing office cap rather than
// minting a new one. Field mark-off gets its own cap in the PR-5 slice; task-list READ
// rides cap.jobtracker.read.
const CAP_SCHEDULE = "cap.jobtracker.manage";
export const SCHEDULE_HMAC_DOMAIN = "schedule:v1";
const SYSTEM_ACTOR = "system:schedule_poll";

// The whole corpus tops out near 800KB per export; 10MB leaves room for a heavier export
// style without inviting arbitrary bulk (the po_attachments ceiling, not the manifest
// lane's 25MB — a schedule is never a 900-row master BOM scan).
const SCHEDULE_MAX_BYTES = 10 * 1024 * 1024;
const SCHEDULE_CHUNK_DECODED_MAX = 1_000_000; // ≤1MB decoded per chunk (0053/0054/0060 shape)
const MAX_SCHEDULE_FILENAME = 200;
const SCHEDULE_PENDING_CAP = 25;
const LIST_CAP = 100;
const MAX_DETAIL = 200;
const MAX_PROFILE = 32;
const MAX_PARSE_NOTES = 4000;
const MAX_COLUMN_MAP_JSON = 40_000;
const MAX_HEADER_META_JSON = 20_000;
// Corpus schedules are 2–4 pages; 12 is generous headroom without letting a mis-uploaded
// tome fill the previews table.
const MAX_PREVIEW_PAGES = 12;
const PREVIEW_MAX_DECODED = 1_000_000;
// One posted page of the proposed grid. A ~60-task schedule lands in one post; the page
// size is kept at the manifest lane's value for uniform daemon plumbing.
const MAX_ROWS_PER_POST = 200;
const MAX_ROW_CELLS = 40; // a schedule grid is ~10 concepts wide, never a 200-col BOM
const MAX_CELL_CHARS = 2000;
const MAX_ROWS_TOTAL = 2_000; // absolute ceiling — a 300-task utility schedule fits 6×
const MAX_FLAGS = 200;
const MAX_SOURCE_LABEL = 64;
const ROWS_READ_CAP = 2000; // browser grid page size ceiling

// Mirrors the 0066 CHECK. The daemon may stamp only these two — `committing`/`committed`/
// `superseded` belong to the browser commit (PR-4/PR-6) and `discarded` to the browser
// discard.
const RESULT_STATUSES = new Set(["refused", "parsed"]);
// Proposed-row kind — mirrors the 0066 CHECK. `section` rows are the phase headers
// (LNTP Work / Deliveries / Civil / Mechanical / …).
const ROW_KINDS = new Set(["header", "data", "continuation", "section", "meta"]);
// The statuses a row/preview post may still LAND on: in-pipeline (the daemon posts ahead
// of its own result) or reviewable (a re-parse). Terminal rows refuse the write, so a late
// daemon post cannot resurrect a discarded or refused schedule's evidence — nor touch a
// committed one's.
const GRID_LIVE_STATUSES = new Set(["pending", "claimed", "parsed"]);

// ── Allowlist: declared MIME ⇄ extension ⇄ magic. PDF ONLY (see the header).
type Magic = "pdf";
const MIME_ALLOWLIST: Record<string, { exts: string[]; magic: Magic }> = {
  "application/pdf": { exts: [".pdf"], magic: "pdf" },
};

function magicMatches(head: Uint8Array, magic: Magic): boolean {
  if (head.length < 8) return false;
  switch (magic) {
    case "pdf": // "%PDF-"
      return head[0] === 0x25 && head[1] === 0x50 && head[2] === 0x44 && head[3] === 0x46 && head[4] === 0x2d;
  }
}

/** Filename gate — the po_attachments / po_estimates / manifest rules verbatim (bounded,
 *  no path separators / control chars / leading dot / Unicode bidi-zero-width spoofers;
 *  extension must belong to the declared MIME). The name lands in D1 and in the Box file. */
function filenameProblem(filename: string, declaredMime: string): string | null {
  if (filename.length < 1 || filename.length > MAX_SCHEDULE_FILENAME) return "invalid_filename";
  // eslint-disable-next-line no-control-regex
  if (/[/\\\u0000-\u001f\u007f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]/.test(filename)) {
    return "invalid_filename";
  }
  if (filename.startsWith(".")) return "invalid_filename";
  const entry = MIME_ALLOWLIST[declaredMime];
  if (!entry) return "schedule_mime_not_allowed";
  const lower = filename.toLowerCase();
  if (!entry.exts.some((e) => lower.endsWith(e) && lower.length > e.length)) {
    return "extension_mime_mismatch";
  }
  return null;
}

/** The schedule:v1 canonical string — ORDER + "\n" SEPARATOR are load-bearing; the Mac
 *  (shared/portal_hmac.py `schedule_canonical`) recomputes it byte-for-byte before
 *  trusting a row. Binds identity (schedule_uuid, job_id), naming (filename, mime), and
 *  content (size_bytes, sha256) — a tampered row OR tampered bytes fail verify. The
 *  job_id slot is what makes the per-job dedupe safe: a superseded revision's exact bytes
 *  may legally re-enter under the SAME job (rollback), and only this binding stops a row
 *  signed for one job being replayed onto another. */
export function scheduleCanonical(
  scheduleUuid: string, jobId: string, filename: string, declaredMime: string,
  sizeBytes: number, sha256: string,
): string {
  return [SCHEDULE_HMAC_DOMAIN, scheduleUuid, jobId, filename, declaredMime, String(sizeBytes), sha256].join("\n");
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
 *  VERBATIM — the validate screen edits against it, so nothing is pre-collapsed. OCR
 *  digit misreads are REAL at confidence 1.0 (the corpus shows 12/01/25 → 72/01/25), so
 *  the human edit floor, not any automated filter, is the fidelity control. */
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

export function registerScheduleRoutes(app: FieldopsApp, gates: ScheduleGates): void {
  // ══ Internal surface (requireScheduleToken — the Mac-side schedule_poll daemon) ══
  // Registered FIRST so the static /internal/* segment can never be captured by the
  // browser tier's /:id parameter.

  // GET /api/fieldops/schedules/internal/pending — live pool rows oldest-first: pending +
  // claimed (claimed re-served for crash recovery — every servicing step is idempotent).
  // Serves metadata + the HMAC (the Mac's verify input); bytes ride the chunks read below.
  // project_name is JOINED from jobs (NOT signed — display/foldering metadata only): the
  // Mac files the schedule under the job's PROJECT-NAME Box folder, the same folder every
  // other artifact uses. A schedule whose job row vanished serves NULL and the daemon
  // falls back to job_id.
  app.get("/api/fieldops/schedules/internal/pending", gates.requireScheduleToken, async (c) => {
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "25", 10) || 25, 1), SCHEDULE_PENDING_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT s.id, s.schedule_uuid, s.job_id, s.filename, s.declared_mime, s.size_bytes, " +
          "s.sha256, s.status, s.hmac, s.uploaded_by, s.created_at, j.project_name " +
          "FROM job_schedules s LEFT JOIN jobs j ON j.job_id = s.job_id " +
          "WHERE s.status IN ('pending','claimed') " +
          "ORDER BY s.created_at ASC, s.id ASC LIMIT ?1",
      )
      .bind(limit)
      .all<Record<string, unknown>>();
    return c.json({ schedules: results ?? [] });
  });

  // POST /api/fieldops/schedules/internal/:id/claim — claim-first marker: pending→claimed,
  // guarded in-WHERE + changes()-gated audit (W4). Idempotent: found:false when already
  // claimed/disposed — the daemon proceeds on a row it already claimed (crash recovery).
  app.post("/api/fieldops/schedules/internal/:id/claim", gates.requireScheduleToken, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare("UPDATE job_schedules SET status='claimed' WHERE id = ?1 AND status = 'pending'")
        .bind(id),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "job_schedule_claim", String(id), { schedule_id: id }),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // GET /api/fieldops/schedules/internal/:id/chunks — the Mac-ward byte read (the ONLY
  // route that ever serves original document bytes, bearer-gated). Live rows only.
  app.get("/api/fieldops/schedules/internal/:id/chunks", gates.requireScheduleToken, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const row = await c.env.DB
      .prepare("SELECT status FROM job_schedules WHERE id = ?1")
      .bind(id)
      .first<{ status: string }>();
    if (!row || (row.status !== "pending" && row.status !== "claimed")) {
      return c.json({ error: "not_found" }, 404);
    }
    const { results } = await c.env.DB
      .prepare(
        "SELECT chunk_index, chunk_total, chunk_b64 FROM job_schedule_chunks " +
          "WHERE schedule_id = ?1 ORDER BY chunk_index ASC",
      )
      .bind(id)
      .all<Record<string, unknown>>();
    return c.json({ chunks: results ?? [] });
  });

  // POST /api/fieldops/schedules/internal/:id/rows — one PAGE of the proposed grid.
  // Body: { rows: [{ row_index, source_page?, kind, cells: [...], flags? }] }.
  // UPSERT on (schedule_id, row_index) so a re-posted page is a no-op and a crashed daemon
  // simply re-posts from the start next cycle. Every INSERT is guarded on the parent row
  // still being live, so a page racing a discard lands nothing.
  app.post("/api/fieldops/schedules/internal/:id/rows", gates.requireScheduleToken, async (c) => {
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
      .prepare("SELECT status FROM job_schedules WHERE id = ?1")
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
          "INSERT INTO job_schedule_rows (schedule_id, row_index, source_page, kind, cells_json, flags) " +
            "SELECT ?1, ?2, ?3, ?4, ?5, ?6 WHERE EXISTS " +
            "(SELECT 1 FROM job_schedules WHERE id = ?1 AND status IN ('pending','claimed','parsed')) " +
            "ON CONFLICT (schedule_id, row_index) DO UPDATE SET " +
            "source_page=excluded.source_page, kind=excluded.kind, " +
            "cells_json=excluded.cells_json, flags=excluded.flags",
        )
        .bind(id, r.row_index, r.source_page, r.kind, r.cells_json, r.flags),
    );
    const res = await c.env.DB.batch(stmts);
    const written = res.reduce((n, r) => n + (r.meta.changes ?? 0), 0);
    return c.json({ ok: true, written });
  });

  // POST /api/fieldops/schedules/internal/:id/preview — one rendered source page, so the
  // validate screen can show the SOURCE Gantt page beside the editable grid without the
  // browser ever touching the original untrusted bytes.
  app.post("/api/fieldops/schedules/internal/:id/preview", gates.requireScheduleToken, async (c) => {
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
      .prepare("SELECT status FROM job_schedules WHERE id = ?1")
      .bind(id)
      .first<{ status: string }>();
    if (!parent || !GRID_LIVE_STATUSES.has(parent.status)) return c.json({ error: "not_found" }, 404);
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "INSERT INTO job_schedule_previews (schedule_id, page, png_b64) " +
            "SELECT ?1, ?2, ?3 WHERE EXISTS " +
            "(SELECT 1 FROM job_schedules WHERE id = ?1 AND status IN ('pending','claimed','parsed')) " +
            "ON CONFLICT (schedule_id, page) DO UPDATE SET png_b64=excluded.png_b64",
        )
        .bind(id, page, pngB64),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // POST /api/fieldops/schedules/internal/result — apply one servicing outcome.
  // Body: { schedule_id, status: 'refused'|'parsed', detail?, box_file_id? (forbidden on
  // refused — a refused doc is never filed), profile?, row_count?, column_map?,
  // header_meta?, parse_notes? }. ONE atomic batch (W4): guarded status UPDATE →
  // changes()-gated audit → chunk DELETE (on BOTH outcomes: a refused document's bytes are
  // dropped, and a parsed one's are no longer needed because the grid now exists).
  // Idempotent: a re-post for an already-disposed/unknown row is { ok:true, found:false }.
  app.post("/api/fieldops/schedules/internal/result", gates.requireScheduleToken, async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const id =
      typeof body.schedule_id === "number" && Number.isSafeInteger(body.schedule_id) && body.schedule_id > 0
        ? body.schedule_id
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
    // A refused schedule is never filed, so a box_file_id on a refusal is a contradiction
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

    // Guarded in-WHERE on the CLAIMABLE states so a result cannot resurrect a discarded,
    // committed or superseded row. The chunk DELETE's subselect reads the POST-update
    // status, so it fires exactly when this batch is the one that disposed the row.
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE job_schedules SET status = ?2, detail = ?3, profile = COALESCE(?4, profile), " +
            "box_file_id = COALESCE(?5, box_file_id), row_count = COALESCE(?6, row_count), " +
            "column_map_json = COALESCE(?7, column_map_json), " +
            "header_meta_json = COALESCE(?8, header_meta_json), " +
            "parse_notes = COALESCE(?9, parse_notes), " +
            "screened_at = unixepoch(), parsed_at = CASE WHEN ?2 = 'parsed' THEN unixepoch() ELSE parsed_at END " +
            "WHERE id = ?1 AND status IN ('pending','claimed')",
        )
        .bind(id, status, detail, profile, boxFileId, rowCount, columnMapJson, headerMetaJson, parseNotes),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "job_schedule_result", String(id), {
        schedule_id: id, status, detail, profile, row_count: rowCount,
      }),
      c.env.DB
        .prepare(
          "DELETE FROM job_schedule_chunks WHERE schedule_id = ?1 " +
            "AND EXISTS (SELECT 1 FROM job_schedules WHERE id = ?1 AND status IN ('refused','parsed'))",
        )
        .bind(id),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // ══ Browser surface (session + cap.jobtracker.manage) ═════════════════════════════

  // POST /api/fieldops/schedules — office upload. Body: { job_id, filename, mime,
  // data_b64 }. The whole file rides ONE request (base64 in JSON — the attachment wire);
  // the Worker decodes once, signs schedule:v1, splits into ≤1MB-decoded chunks, and lands
  // parent + ALL chunks + audit in ONE db.batch (W4). The PER-JOB partial UNIQUE
  // (job_id, sha256) index is the dedupe authority → 409 duplicate_schedule.
  app.post("/api/fieldops/schedules", gates.requireSession, gates.requireCapability(CAP_SCHEDULE), async (c) => {
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
    // size BEFORE any decode materializes bytes (the po_attachments order). The MIME codes
    // are LANE-SPECIFIC (schedule_*): the shared codes' human copy names the manifest
    // lane's PDF+xlsx allowlist, which would be actively-false advice for a PDF-only lane.
    if (!Object.prototype.hasOwnProperty.call(MIME_ALLOWLIST, declaredMime)) {
      return c.json({ error: "schedule_mime_not_allowed" }, 422);
    }
    const nameProblem = filenameProblem(filename, declaredMime);
    if (nameProblem) return c.json({ error: nameProblem }, nameProblem === "extension_mime_mismatch" ? 422 : 400);
    if (dataB64.length === 0 || dataB64.length % 4 !== 0) return c.json({ error: "invalid_data" }, 400);
    if (b64DecodedLen(dataB64) > SCHEDULE_MAX_BYTES) return c.json({ error: "schedule_too_large" }, 413);
    if (!B64_RE.test(dataB64)) return c.json({ error: "invalid_data" }, 400);
    let bytes: Uint8Array;
    try {
      bytes = b64ToBytes(dataB64);
    } catch {
      return c.json({ error: "invalid_data" }, 400);
    }
    if (bytes.length === 0 || bytes.length > SCHEDULE_MAX_BYTES) {
      return c.json({ error: "schedule_too_large" }, 413);
    }
    if (!magicMatches(bytes.subarray(0, 8), MIME_ALLOWLIST[declaredMime].magic)) {
      return c.json({ error: "schedule_magic_mime_mismatch" }, 422);
    }

    if (!c.env.HMAC_PAYLOAD_SECRET) return c.json({ error: "hmac_secret_missing" }, 500);
    const sha256 = await sha256Hex(bytes);
    const scheduleUuid = crypto.randomUUID();
    const hmac = await hmacHex(
      c.env.HMAC_PAYLOAD_SECRET,
      scheduleCanonical(scheduleUuid, jobId, filename, declaredMime, bytes.length, sha256),
    );

    const chunkTotal = Math.ceil(bytes.length / SCHEDULE_CHUNK_DECODED_MAX);
    const chunkStmts = [];
    for (let i = 0; i < chunkTotal; i++) {
      const slice = bytes.subarray(i * SCHEDULE_CHUNK_DECODED_MAX, (i + 1) * SCHEDULE_CHUNK_DECODED_MAX);
      chunkStmts.push(
        c.env.DB
          .prepare(
            "INSERT INTO job_schedule_chunks (schedule_id, chunk_index, chunk_total, chunk_b64) " +
              "SELECT (SELECT id FROM job_schedules WHERE schedule_uuid = ?1), ?2, ?3, ?4 " +
              "WHERE EXISTS (SELECT 1 FROM job_schedules WHERE schedule_uuid = ?1)",
          )
          .bind(scheduleUuid, i, chunkTotal, bytesToB64(slice)),
      );
    }

    const actor = c.get("session").username;
    // ONE batch (W4): parent INSERT → audit → chunk INSERTs (each guarded on the parent row
    // existing). A dedupe hit (the PER-JOB partial UNIQUE over in-play rows) aborts the
    // WHOLE batch — no parent, no chunks, no audit — and maps to 409.
    try {
      await c.env.DB.batch([
        c.env.DB
          .prepare(
            "INSERT INTO job_schedules (schedule_uuid, job_id, filename, declared_mime, " +
              "size_bytes, sha256, status, hmac, uploaded_by) " +
              "VALUES (?1,?2,?3,?4,?5,?6,'pending',?7,?8)",
          )
          .bind(scheduleUuid, jobId, filename, declaredMime, bytes.length, sha256, hmac, actor),
        auditStmt(c, actor, "job_schedule_upload", jobId, {
          schedule_uuid: scheduleUuid, job_id: jobId, filename,
          declared_mime: declaredMime, size_bytes: bytes.length, sha256,
        }),
        ...chunkStmts,
      ]);
    } catch (e) {
      if (isUniqueViolation(e)) return c.json({ error: "duplicate_schedule" }, 409);
      throw e;
    }
    const row = await c.env.DB
      .prepare("SELECT id FROM job_schedules WHERE schedule_uuid = ?1")
      .bind(scheduleUuid)
      .first<{ id: number }>();
    return c.json({ ok: true, id: row?.id ?? null, filename, size_bytes: bytes.length }, 201);
  });

  // GET /api/fieldops/schedules?job_id=&limit= — the per-job list (metadata only; never
  // the hmac, never bytes). Discarded rows are EXCLUDED (the 0060 list rule): "Remove" is
  // a discard, and a removed row reappearing as `discarded` would make the button look
  // broken. Superseded rows ARE served — they are the job's revision history.
  app.get("/api/fieldops/schedules", gates.requireSession, gates.requireCapability(CAP_SCHEDULE), async (c) => {
    const jobId = str(c.req.query("job_id"));
    if (!jobId || jobId.length > 64) return c.json({ error: "invalid_job_id" }, 400);
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "50", 10) || 50, 1), LIST_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT id, schedule_uuid, job_id, filename, declared_mime, size_bytes, status, detail, " +
          "profile, row_count, committed_through_row, uploaded_by, box_file_id, " +
          "created_at, parsed_at, committed_at, superseded_at " +
          "FROM job_schedules WHERE job_id = ?1 AND status <> 'discarded' " +
          "ORDER BY created_at DESC, id DESC LIMIT ?2",
      )
      .bind(jobId, limit)
      .all<Record<string, unknown>>();
    return c.json({ schedules: results ?? [] });
  });

  // GET /api/fieldops/schedules/:id — one schedule's header, the proposed column map and
  // the parser's notes (the validate screen's evidence pane). Never the hmac, never bytes.
  app.get("/api/fieldops/schedules/:id", gates.requireSession, gates.requireCapability(CAP_SCHEDULE), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const row = await c.env.DB
      .prepare(
        "SELECT id, schedule_uuid, job_id, filename, declared_mime, size_bytes, status, detail, " +
          "profile, row_count, column_map_json, header_meta_json, parse_notes, " +
          "resolutions_json, committed_through_row, uploaded_by, box_file_id, " +
          "created_at, screened_at, parsed_at, committed_at, superseded_at " +
          "FROM job_schedules WHERE id = ?1",
      )
      .bind(id)
      .first<Record<string, unknown>>();
    if (!row) return c.json({ error: "not_found" }, 404);
    const pages = await c.env.DB
      .prepare("SELECT page FROM job_schedule_previews WHERE schedule_id = ?1 ORDER BY page ASC")
      .bind(id)
      .all<{ page: number }>();
    return c.json({ schedule: row, preview_pages: (pages.results ?? []).map((p) => p.page) });
  });

  // GET /api/fieldops/schedules/:id/rows?after=&limit= — the proposed grid, paged. `after`
  // is a row_index cursor (the grid is naturally ordered and stable).
  app.get("/api/fieldops/schedules/:id/rows", gates.requireSession, gates.requireCapability(CAP_SCHEDULE), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const after = Math.max(parseInt(c.req.query("after") || "0", 10) || 0, 0);
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "500", 10) || 500, 1), ROWS_READ_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT row_index, source_page, kind, cells_json, flags FROM job_schedule_rows " +
          "WHERE schedule_id = ?1 AND row_index > ?2 ORDER BY row_index ASC LIMIT ?3",
      )
      .bind(id, after, limit)
      .all<Record<string, unknown>>();
    return c.json({ rows: results ?? [] });
  });

  // GET /api/fieldops/schedules/:id/preview/:page — one rendered source page as a PNG
  // data payload. This is the ONLY view a browser ever gets of the source document; the
  // original bytes never leave the Mac-ward chunks route.
  app.get(
    "/api/fieldops/schedules/:id/preview/:page",
    gates.requireSession,
    gates.requireCapability(CAP_SCHEDULE),
    async (c) => {
      const id = parseIdParam(c.req.param("id"));
      const page = parseIdParam(c.req.param("page"));
      if (id === null || page === null) return c.json({ error: "invalid_id" }, 400);
      const row = await c.env.DB
        .prepare("SELECT png_b64 FROM job_schedule_previews WHERE schedule_id = ?1 AND page = ?2")
        .bind(id, page)
        .first<{ png_b64: string }>();
      if (!row) return c.json({ error: "not_found" }, 404);
      return c.json({ page, png_b64: row.png_b64 });
    },
  );

  // POST /api/fieldops/schedules/:id/discard — the office abandons a schedule upload.
  // Guarded in-WHERE on the non-terminal states ('committing' IS discardable — the 0060
  // mid-commit-refusal lesson), and drops the grid, previews and any surviving chunks in
  // the SAME batch. A discarded row leaves the per-job dedupe index, so the same document
  // may be re-uploaded. `committed` / `superseded` rows are NOT discardable: a governing
  // schedule is displaced by committing its replacement (the supersede flow), never by
  // deleting history.
  app.post(
    "/api/fieldops/schedules/:id/discard",
    gates.requireSession,
    gates.requireCapability(CAP_SCHEDULE),
    async (c) => {
      const id = parseIdParam(c.req.param("id"));
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            "UPDATE job_schedules SET status='discarded' WHERE id = ?1 " +
              "AND status IN ('pending','claimed','refused','parsed','committing')",
          )
          .bind(id),
        auditStmtIfChanged(c, actor, "job_schedule_discard", String(id), { schedule_id: id }),
        c.env.DB
          .prepare(
            "DELETE FROM job_schedule_chunks WHERE schedule_id = ?1 " +
              "AND EXISTS (SELECT 1 FROM job_schedules WHERE id = ?1 AND status = 'discarded')",
          )
          .bind(id),
        c.env.DB
          .prepare(
            "DELETE FROM job_schedule_rows WHERE schedule_id = ?1 " +
              "AND EXISTS (SELECT 1 FROM job_schedules WHERE id = ?1 AND status = 'discarded')",
          )
          .bind(id),
        c.env.DB
          .prepare(
            "DELETE FROM job_schedule_previews WHERE schedule_id = ?1 " +
              "AND EXISTS (SELECT 1 FROM job_schedules WHERE id = ?1 AND status = 'discarded')",
          )
          .bind(id),
      ]);
      const changed = (res[0].meta.changes ?? 0) > 0;
      if (!changed) {
        const row = await c.env.DB
          .prepare("SELECT status FROM job_schedules WHERE id = ?1")
          .bind(id)
          .first<{ status: string }>();
        if (!row) return c.json({ error: "not_found" }, 404);
        return c.json({ error: "not_discardable", status: row.status }, 409);
      }
      return c.json({ ok: true, id });
    },
  );
}
