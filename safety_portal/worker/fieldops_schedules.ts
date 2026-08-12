import type { Context, MiddlewareHandler } from "hono";
import type { Env, Vars } from "./types";
import type { FieldopsApp } from "./fieldops_gates";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { hmacHex } from "./hmac";
import { b64DecodedLen, B64_RE } from "./photo_bounds";
import { readScheduleTaskFields, type ScheduleTaskFields } from "./fieldops_schedule_tasks";
import { scheduleMatchKey } from "./schedule_normalize";
import {
  diffSchedule,
  percentPlan,
  toPlanFresh,
  toPlanMatched,
  toPlanRemoved,
  type DiffLivingTask,
  type DiffMatchedPair,
} from "./schedule_diff";
import type { ScheduleCommitResponse, SchedulePlanResponse } from "./wire-types";

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
// one supersede-FIRST in the commit route's final batch. Since PR-6 the commit is a
// full REVISION RECONCILE: the three-way diff (worker/schedule_diff.ts) classifies the
// upload against the living task list and the human's `resolutions` settle renames,
// removals, and percent conflicts.
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

// One /commit call's worth of TASKS (the 0060 watermark protocol's page size — a ~60-task
// schedule lands in one page; a 300-task utility schedule takes 3). Mirrored in
// src/lib/fieldops_schedules.ts so the SPA's paged loop makes exactly as many round trips
// as the server would page into.
const COMMIT_PAGE_TASKS = 100;

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

// ── The plan/commit surface (PR-4 degenerate first-commit + PR-6 revision reconcile) ──
// Plan/commit is ONE surface by design (ADR-0006 decision 9): a job with NO existing
// tasks is simply the DEGENERATE arm of the same three-way diff (an empty living set
// classifies every incoming row `fresh`), so first import and revision reconcile share
// one reader, one diff engine (worker/schedule_diff.ts), one watermark protocol. The
// PLAN is a pure read of the classification; the COMMIT re-derives it server-side every
// page (client classes are never trusted) and applies the human's `resolutions`.

type Ctx = Context<{ Bindings: Env; Variables: Vars }>;

/** One posted commit/plan row. `section` rows are NOT committed — they ride only as each
 *  task's section context (the SPA sends data rows carrying their resolved section
 *  string, and may include section rows for count transparency; both are accepted). */
type CommitRow = {
  source_row_index: number;
  kind: "data" | "section";
  /** Validated content fields — null exactly when kind === 'section'. */
  fields: ScheduleTaskFields | null;
};

/** Validate one posted row. Returns the resolved shape or an error CODE string.
 *  Data rows delegate to the SAME `readScheduleTaskFields` the manual add/edit routes
 *  use (fieldops_schedule_tasks.ts) — an imported task runs the identical bounds, the
 *  identical error vocabulary, so the two authoring paths cannot drift. */
function readCommitRow(raw: unknown): CommitRow | string {
  if (!isPlainObject(raw)) return "invalid_task_rows";
  const idx = raw.source_row_index;
  if (typeof idx !== "number" || !Number.isSafeInteger(idx) || idx < 1 || idx > MAX_ROWS_TOTAL) {
    return "invalid_source_row_index";
  }
  const kind = str(raw.kind);
  if (kind !== "data" && kind !== "section") return "invalid_task_kind";
  if (kind === "section") return { source_row_index: idx, kind, fields: null };
  const fields = readScheduleTaskFields(raw);
  if (typeof fields === "string") return fields;
  return { source_row_index: idx, kind, fields };
}

/** Read + validate the posted `rows` array (plan and commit share it — one reader, so
 *  the preview and the commit cannot disagree about what is well-formed). */
function readCommitRows(body: Record<string, unknown>): CommitRow[] | { error: string; row?: number } {
  if (!Array.isArray(body.rows) || body.rows.length === 0 || body.rows.length > MAX_ROWS_TOTAL) {
    return { error: "invalid_task_rows" };
  }
  const out: CommitRow[] = [];
  for (const [i, raw] of body.rows.entries()) {
    const row = readCommitRow(raw);
    if (typeof row === "string") return { error: row, row: i };
    out.push(row);
  }
  return out;
}

/** The schedule row a plan/commit call is operating on. `resolutions_json` is the
 *  accumulated reconcile ledger (merged across pages — the manifest merge_options_json
 *  pattern). */
type ScheduleRow = {
  id: number;
  job_id: string;
  status: string;
  committed_through_row: number;
  resolutions_json: string | null;
};
async function loadSchedule(c: Ctx, id: number): Promise<ScheduleRow | null> {
  return c.env.DB
    .prepare(
      "SELECT id, job_id, status, committed_through_row, resolutions_json " +
        "FROM job_schedules WHERE id = ?1",
    )
    .bind(id)
    .first<ScheduleRow>();
}

/** The commit's FINALIZE — supersede-FIRST, then committing→committed, one batch (the
 *  ordering idx_job_schedules_one_committed requires; both UPDATEs no-op harmlessly when
 *  already done). IDEMPOTENT by construction, because it runs from TWO places: the happy
 *  path's final page, and the replay guard's stuck-'committing' repair — a Worker
 *  eviction between the page batch and this batch would otherwise strand the schedule in
 *  'committing' forever with the client's own retry reporting false success (the
 *  2026-08-11 security-review BLOCK finding). */
async function finalizeScheduleCommit(
  c: Ctx, actor: string, jobId: string, scheduleId: number, inserted: number,
): Promise<void> {
  await c.env.DB.batch([
    c.env.DB
      .prepare(
        "UPDATE job_schedules SET status='superseded', superseded_at = unixepoch() " +
          "WHERE job_id = ?1 AND status = 'committed' AND id <> ?2",
      )
      .bind(jobId, scheduleId),
    auditStmtIfChanged(c, actor, "job_schedule_supersede", String(scheduleId), {
      schedule_id: scheduleId, job_id: jobId,
    }),
    c.env.DB
      .prepare(
        "UPDATE job_schedules SET status='committed', committed_at = unixepoch() " +
          "WHERE id = ?1 AND status = 'committing'",
      )
      .bind(scheduleId),
    auditStmtIfChanged(c, actor, "job_schedule_commit", String(scheduleId), {
      schedule_id: scheduleId, job_id: jobId, inserted,
    }),
  ]);
}

// Living tasks read for the diff — matches MAX_ROWS_TOTAL: no committable document can
// author more tasks than this, so the living side can never outgrow what fits.
const LIVING_TASKS_CAP = 2000;

/** Active tasks the job holds that did NOT come from THIS schedule's own commit — the
 *  LIVING side of the reconcile diff (full rows, document order). The exclusion is what
 *  keeps a PAGED commit self-consistent: page 2 must not diff against page 1's
 *  freshly-INSERTED fresh rows (they carry source_schedule_id = this schedule) — while a
 *  task page 1 merely UPDATED keeps its original source_schedule_id and stays included,
 *  where its (now below-watermark) row re-matches it idempotently. */
async function loadForeignActiveTasks(
  c: Ctx,
  jobId: string,
  scheduleId: number,
): Promise<DiffLivingTask[]> {
  const { results } = await c.env.DB
    .prepare(
      "SELECT id, task_uuid, match_key, section, name, duration_days, start_date, finish_date, " +
        "percent_done, schedule_percent, last_marked_by, delivered_date, " +
        "is_milestone, is_contract_milestone, is_delivery, predecessors_raw, sort_order " +
        "FROM job_schedule_tasks " +
        "WHERE job_id = ?1 AND active = 1 " +
        "AND (source_schedule_id IS NULL OR source_schedule_id <> ?2) " +
        `ORDER BY sort_order ASC, id ASC LIMIT ${LIVING_TASKS_CAP}`,
    )
    .bind(jobId, scheduleId)
    .all<DiffLivingTask>();
  return results ?? [];
}

/** The human's reconcile decisions (PR-6), validated for SHAPE here and for MEANING
 *  against the server-derived diff in the commit route. Keys arrive as JSON object keys
 *  (strings) and are re-validated as canonical positive integers; a shape error refuses
 *  the whole call — a half-read resolution set is worse than none. */
type ScheduleResolutionMaps = {
  /** fresh source_row_index → removed task id: the human names a rename. */
  links: Map<number, number>;
  /** removed task id → explicit disposition. BLOCKING removals REQUIRE an entry. */
  removals: Map<number, "remove" | "keep">;
  /** matched/linked source_row_index → percent pick on a CONFLICT (default 'portal'). */
  percents: Map<number, "portal" | "revision">;
};

function readResolutions(
  body: Record<string, unknown>,
): ScheduleResolutionMaps | { error: string } {
  const out: ScheduleResolutionMaps = { links: new Map(), removals: new Map(), percents: new Map() };
  const raw = body.resolutions;
  if (raw === undefined || raw === null) return out;
  if (!isPlainObject(raw)) return { error: "invalid_resolutions" };
  for (const key of Object.keys(raw)) {
    if (key !== "links" && key !== "removals" && key !== "percents") {
      return { error: "invalid_resolutions" };
    }
  }
  const readMap = (v: unknown): [number, unknown][] | null => {
    if (v === undefined || v === null) return [];
    if (!isPlainObject(v)) return null;
    const entries = Object.entries(v);
    if (entries.length > MAX_ROWS_TOTAL) return null;
    const pairs: [number, unknown][] = [];
    for (const [k, val] of entries) {
      const key = parseIdParam(k);
      if (key === null) return null;
      pairs.push([key, val]);
    }
    return pairs;
  };
  const links = readMap(raw.links);
  if (links === null) return { error: "invalid_resolutions" };
  for (const [k, v] of links) {
    if (k > MAX_ROWS_TOTAL) return { error: "invalid_resolutions" };
    if (typeof v !== "number" || !Number.isSafeInteger(v) || v <= 0) {
      return { error: "invalid_resolutions" };
    }
    out.links.set(k, v);
  }
  const removals = readMap(raw.removals);
  if (removals === null) return { error: "invalid_resolutions" };
  for (const [k, v] of removals) {
    if (v !== "remove" && v !== "keep") return { error: "invalid_resolutions" };
    out.removals.set(k, v);
  }
  const percents = readMap(raw.percents);
  if (percents === null) return { error: "invalid_resolutions" };
  for (const [k, v] of percents) {
    if (k > MAX_ROWS_TOTAL) return { error: "invalid_resolutions" };
    if (v !== "portal" && v !== "revision") return { error: "invalid_resolutions" };
    out.percents.set(k, v);
  }
  return out;
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

  // POST /api/fieldops/schedules/:id/plan — DRY RUN. Body: { rows }. Writes nothing.
  // Returns the FULL three-way classification (PR-6): a job with no foreign active
  // tasks is the degenerate arm (every data row `fresh`); otherwise the diff engine
  // classifies matched (with per-pair date/percent/info changes) / BLOCKING ambiguous /
  // fresh / removed (flagged blocking when marked / delivered / contract-milestone).
  // Deliberately NOT a 409 under any classification: asking "what would this do" must
  // never error just because the answer needs human decisions — only the COMMIT refuses.
  app.post(
    "/api/fieldops/schedules/:id/plan",
    gates.requireSession,
    gates.requireCapability(CAP_SCHEDULE),
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
      const schedule = await loadSchedule(c, id);
      if (!schedule) return c.json({ error: "not_found" }, 404);
      const rows = readCommitRows(body);
      if (!Array.isArray(rows)) return c.json(rows, 400);
      const dataRows = rows.filter(
        (r): r is CommitRow & { fields: ScheduleTaskFields } => r.kind === "data" && r.fields !== null,
      );
      const living = await loadForeignActiveTasks(c, schedule.job_id, id);
      const diff = diffSchedule({
        proposed: dataRows.map((r) => ({ source_row_index: r.source_row_index, fields: r.fields })),
        living,
      });
      const payload: SchedulePlanResponse = {
        ok: true,
        degenerate: living.length === 0,
        revision_reconcile_available: true,
        counts: {
          incoming: dataRows.length,
          new: diff.fresh.length,
          existing: living.length,
          matched: diff.matched.length,
          ambiguous: diff.ambiguous.length,
          removed: diff.removed.length,
          blocking_removals: diff.removed.filter((r) => r.blocking).length,
          percent_conflicts: diff.matched.filter((m) => m.percent.rule === "conflict").length,
        },
        matched: diff.matched.map(toPlanMatched),
        ambiguous: diff.ambiguous,
        fresh: diff.fresh.map(toPlanFresh),
        removed: diff.removed.map(toPlanRemoved),
      };
      return c.json(payload);
    },
  );

  // POST /api/fieldops/schedules/:id/commit — ONE PAGE of the commit (first import AND
  // revision reconcile — one surface, ADR-0006 decision 9). Body: { rows, resolutions? }
  // (same rows shape /plan validates; section rows are context, never committed).
  //
  // THE DIFF IS RE-DERIVED HERE, EVERY PAGE, from the posted rows and the LIVE task
  // list — the /plan payload the human reviewed is never trusted as state. `resolutions`
  // carries the human's decisions and is validated for MEANING against the server's own
  // classification: links (fresh row → removed task, a rename), removals (every BLOCKING
  // removal MUST be explicit 'remove'|'keep'; non-blocking defaults to remove), percents
  // (a CONFLICT's 'portal'|'revision' pick, default portal). Ambiguity has NO resolution
  // channel — it refuses 409 until the human fixes the duplication itself.
  //
  // PAGED WITH THE 0066 WATERMARK PROTOCOL: statement 0 is the state transition
  // (parsed/committing → committing + watermark advance + the resolutions ledger merge,
  // guarded on the watermark strictly advancing AND on NO OTHER schedule of this job
  // being mid-commit — the two-uploads serialization), and every task write after it is
  // guarded on the schedule being 'committing' AT THIS PAGE'S WATERMARK (the 0060
  // MANIFEST_LIVE_GUARD pattern), so a page racing a discard lands nothing and
  // meta.changes on statement 0 turns that into an honest 409 instead of ok:true on
  // writes that never happened.
  //
  // REPLAY GUARD FIRST, before the status guard: everything at or below the watermark
  // is already committed, so a fully-replayed payload — the retry of a LOST final
  // response — is an idempotent { done:true } no-op, never a 409 for work that
  // succeeded.
  //
  // REMOVALS APPLY ON THE FINAL PAGE ONLY: "removed" is defined against the WHOLE
  // document, so deactivating before the last page could sweep a task a later page's
  // rows still claim. The supersede-then-commit FINALIZE also runs on the final page
  // (supersede-FIRST — the 0066 one-committed partial UNIQUE's ordering).
  app.post(
    "/api/fieldops/schedules/:id/commit",
    gates.requireSession,
    gates.requireCapability(CAP_SCHEDULE),
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
      const schedule = await loadSchedule(c, id);
      if (!schedule) return c.json({ error: "not_found" }, 404);
      const rows = readCommitRows(body);
      if (!Array.isArray(rows)) return c.json(rows, 400);
      const resolutions = readResolutions(body);
      if ("error" in resolutions) return c.json(resolutions, 400);

      // The FULL ordered data-row set — `seq` (1-based position here) drives
      // sort_order = seq*10, so it must be derived from the whole payload, not the
      // page: the client re-posts the same full payload every round (the commitAll
      // contract), which keeps every task's ordinal stable across pages and replays.
      const dataRows = rows
        .filter((r): r is CommitRow & { fields: ScheduleTaskFields } => r.kind === "data" && r.fields !== null)
        .sort((a, b) => a.source_row_index - b.source_row_index);
      const remaining = dataRows.filter((r) => r.source_row_index > schedule.committed_through_row);
      if (remaining.length === 0) {
        // Fully-replayed payload — every row is at/below the watermark. BUT the watermark
        // alone does not prove the FINALIZE ran: the page batch and the finalize batch are
        // two separate transactions, and a Worker eviction / transient D1 error between
        // them leaves status='committing' with a maxed watermark. The client's documented
        // recovery is exactly this re-post, so returning done:true on watermark alone
        // would report success while the schedule stays stuck in 'committing' FOREVER
        // (no other route can finish it) — the 2026-08-11 security-review BLOCK finding.
        // Finalize is idempotent, so the stuck state is repaired HERE, on the replay.
        // (Reconcile inherits this unchanged: removals ride the final PAGE batch, so a
        // maxed watermark proves they already applied.)
        if (schedule.status === "committing") {
          const actor = c.get("session").username;
          await finalizeScheduleCommit(c, actor, schedule.job_id, id, 0);
        }
        const payload: ScheduleCommitResponse = {
          ok: true, done: true, inserted: 0, updated: 0, linked: 0, removed: 0,
          committed_through_row: schedule.committed_through_row,
        };
        return c.json(payload);
      }
      // Only a parsed (or mid-commit) schedule may author tasks — a refused, discarded,
      // committed or superseded one has no business doing so; any payload reaching here
      // carries rows above its watermark, so this is a real attempt, not a replay.
      if (schedule.status !== "parsed" && schedule.status !== "committing") {
        return c.json({ error: "schedule_not_committable", status: schedule.status }, 409);
      }

      // ── The three-way diff, derived from the LIVE list (never the client's classes). ──
      const living = await loadForeignActiveTasks(c, schedule.job_id, id);
      const diff = diffSchedule({
        proposed: dataRows.map((r) => ({ source_row_index: r.source_row_index, fields: r.fields })),
        living,
      });

      // BLOCKING ambiguity — refused on EVERY page, before any write. There is no
      // resolution channel for it (decision 9): the human resolves the duplication
      // itself (rename/deactivate a duplicate task, or fix the upload) and re-plans.
      //
      // KNOWN FAIL-CLOSED EDGE (2026-08-12 security review, WARN — deliberate): on a
      // MULTI-PAGE commit, a below-watermark row whose LINK already rewrote its target
      // task's match_key stays in the re-derived diff, so a later-page row with the
      // IDENTICAL (section, name) now collides with it → duplicate_proposed → this 409
      // blocks the remainder. Reaching it takes a >100-task document + a rename link +
      // a coincidental exact duplicate of the new name later in the same document; the
      // refusal is honest (nothing corrupts, landed pages stay, discard remains the
      // exit). Preferring the loud 409 over teaching the diff to silently settle
      // below-watermark claims — revisit if it ever bites a real document.
      if (diff.ambiguous.length > 0) {
        return c.json(
          {
            error: "ambiguous_unresolved",
            rows: diff.ambiguous.map((a) => a.source_row_index).slice(0, 50),
          },
          409,
        );
      }

      const matchedByIndex = new Map(diff.matched.map((m) => [m.source_row_index, m]));
      const freshByIndex = new Map(diff.fresh.map((f) => [f.source_row_index, f]));
      const removedById = new Map(diff.removed.map((r) => [r.task.id, r]));

      // links: each maps a FRESH row onto an "active, otherwise-unclaimed task of this
      // job" — after the ambiguity gate, that set IS the removed set. A link whose
      // source row isn't fresh, whose target isn't removed-classified, or whose target
      // is claimed twice refuses loudly: a mis-aimed rename would graft one task's
      // history onto another.
      const linkTargetByIndex = new Map<number, DiffLivingTask>();
      const linkedTargets = new Set<number>();
      for (const [srcIdx, taskId] of resolutions.links) {
        const freshRow = freshByIndex.get(srcIdx);
        const target = removedById.get(taskId);
        if (!freshRow || !target || linkedTargets.has(taskId)) {
          return c.json({ error: "invalid_link_target", source_row_index: srcIdx }, 400);
        }
        linkedTargets.add(taskId);
        linkTargetByIndex.set(srcIdx, target.task);
      }

      // removals: keys must reference tasks that are STILL removed once links are
      // applied (an entry for anything else means the client's plan is stale — re-plan);
      // every BLOCKING removal needs an explicit disposition before ANY page writes, so
      // the refusal comes on page 1, not after half the document landed.
      const effectiveRemoved = diff.removed.filter((r) => !linkedTargets.has(r.task.id));
      const effectiveRemovedIds = new Set(effectiveRemoved.map((r) => r.task.id));
      for (const taskId of resolutions.removals.keys()) {
        if (!effectiveRemovedIds.has(taskId)) return c.json({ error: "invalid_resolutions" }, 400);
      }
      const blockingUnresolved = effectiveRemoved
        .filter((r) => r.blocking && !resolutions.removals.has(r.task.id))
        .map((r) => r.task.id);
      if (blockingUnresolved.length > 0) {
        return c.json(
          { error: "blocking_removals_unresolved", task_ids: blockingUnresolved.slice(0, 50) },
          409,
        );
      }

      // percents: keys must reference a matched pair or a linked row. The pick only
      // ever APPLIES to a derived CONFLICT — on a settled rule (keep_portal /
      // take_revision) it is ignored, so a benign race (a field mark landing between
      // plan and commit) cannot turn a stored pick into a wrong write.
      for (const idx of resolutions.percents.keys()) {
        if (!matchedByIndex.has(idx) && !linkTargetByIndex.has(idx)) {
          return c.json({ error: "invalid_resolutions" }, 400);
        }
      }

      const page = remaining.slice(0, COMMIT_PAGE_TASKS);
      const watermark = page[page.length - 1].source_row_index;
      const seqByIndex = new Map(dataRows.map((r, i) => [r.source_row_index, i + 1]));

      // The resolutions LEDGER — merged across pages onto the pool row (the manifest
      // merge_options_json pattern), so the audit trail of "what the human decided"
      // survives paging, replays, and the commit itself.
      const priorLedger = ((): Record<string, unknown> => {
        try {
          return schedule.resolutions_json
            ? (JSON.parse(schedule.resolutions_json) as Record<string, unknown>)
            : {};
        } catch {
          return {};
        }
      })();
      const priorMap = (k: string): Record<string, unknown> =>
        isPlainObject(priorLedger[k]) ? (priorLedger[k] as Record<string, unknown>) : {};
      const hasCurrent =
        resolutions.links.size + resolutions.removals.size + resolutions.percents.size > 0;
      const mergedLedger = hasCurrent
        ? JSON.stringify({
            links: { ...priorMap("links"), ...Object.fromEntries(resolutions.links) },
            removals: { ...priorMap("removals"), ...Object.fromEntries(resolutions.removals) },
            percents: { ...priorMap("percents"), ...Object.fromEntries(resolutions.percents) },
          })
        : null;

      const actor = c.get("session").username;
      // Statement 0 — THE state transition, decided once for the whole batch (D1 runs a
      // batch as one serialized transaction). Its three guards: committable status,
      // watermark strictly advancing, and the TWO-UPLOADS SERIALIZATION — no other
      // schedule of this job may be mid-commit (two governing candidates interleaving
      // pages would race the one-committed UNIQUE at their final pages). COALESCE keeps
      // an earlier page's ledger when this call carried no resolutions.
      const stmts = [
        c.env.DB
          .prepare(
            "UPDATE job_schedules SET status='committing', committed_through_row = ?2, " +
              "resolutions_json = COALESCE(?4, resolutions_json) " +
              "WHERE id = ?1 AND status IN ('parsed','committing') AND committed_through_row < ?2 " +
              "AND NOT EXISTS (SELECT 1 FROM job_schedules WHERE job_id = ?3 AND status = 'committing' AND id <> ?1)",
          )
          .bind(id, watermark, schedule.job_id, mergedLedger),
      ];
      // Which batch index holds each row's MUTATE — the response counts derive from the
      // ACTUAL per-statement meta.changes, never the pre-write classification.
      const insertIdx: number[] = [];
      const updateIdx: number[] = [];
      const linkIdx: number[] = [];
      const removeIdx: number[] = [];

      /** The matched/linked UPDATE: content fields + sort_order from the revision,
       *  schedule_percent = the revision's %, percent_done per the resolved three-way
       *  rule. BASELINE IMMUTABILITY (0071): baseline_start_date/baseline_finish_date
       *  are deliberately ABSENT from the SET — they keep the task's own first-commit
       *  values forever, whatever revisions later move the working dates. Also
       *  untouched: task_uuid (the stable identity a link exists to preserve),
       *  last_marked_by/at (commits never field-mark), the delivered mark, and
       *  is_contract_milestone — the CM flag has no OCR signal (ADR-0006: "always a
       *  human decision"), and taking the revision's always-false value would both wipe
       *  the office's mark AND strip the task's blocking-removal protection. */
      const pushTaskUpdate = (
        task: DiffLivingTask,
        row: CommitRow & { fields: ScheduleTaskFields },
        kind: "update" | "link",
      ) => {
        const f = row.fields;
        const plan = percentPlan(f, task);
        // The three-way rule's STABLE inputs are decided here from the pre-read (both
        // are safe: a percents pick only exists on a derived CONFLICT, and marks are
        // never un-set, so a conflict cannot un-conflict; schedule_percent is written
        // only by a committing reconcile, and the two-uploads serialization makes us
        // the only one). The VOLATILE input — last_marked_by — is deliberately NOT
        // decided here: the CASE below re-reads it atomically at write time, so a
        // field mark landing between our SELECT and this batch flips take_revision →
        // keep-portal instead of being clobbered by a stale decision (2026-08-12 PR-6
        // security-review WARN; operator decision 2 — the portal % is preserved unless
        // the human explicitly takes the revision's value).
        const explicitRevision =
          plan.rule === "conflict" && resolutions.percents.get(row.source_row_index) === "revision";
        const revEqualsSchedule = plan.rule === "keep_portal";
        const matchKey = scheduleMatchKey(f.section ?? "", f.name);
        (kind === "update" ? updateIdx : linkIdx).push(stmts.length);
        stmts.push(
          c.env.DB
            .prepare(
              // percent_done, atomically: the human's explicit 'revision' pick wins
              // (informed by the surfaced conflict); an unchanged schedule assertion
              // keeps the portal %; otherwise the revision's % lands ONLY if the row
              // is unmarked AT WRITE TIME — a marked row keeps its mark.
              `UPDATE job_schedule_tasks
                  SET section = ?2, name = ?3, match_key = ?4, duration_days = ?5,
                      start_date = ?6, finish_date = ?7,
                      is_milestone = ?8, is_delivery = ?9,
                      predecessors_raw = ?10, sort_order = ?11,
                      schedule_percent = ?12,
                      percent_done = CASE
                        WHEN ?13 = 1 THEN ?12
                        WHEN ?16 = 1 THEN percent_done
                        WHEN last_marked_by IS NULL THEN ?12
                        ELSE percent_done
                      END,
                      updated_schedule_id = ?14, updated_at = unixepoch()
                WHERE id = ?1 AND active = 1
                  AND EXISTS (SELECT 1 FROM job_schedules
                               WHERE id = ?14 AND status = 'committing' AND committed_through_row = ?15)`,
            )
            .bind(
              task.id, f.section, f.name, matchKey, f.duration_days,
              f.start_date, f.finish_date, f.is_milestone, f.is_delivery,
              f.predecessors_raw, (seqByIndex.get(row.source_row_index) ?? 0) * 10,
              f.percent_done, explicitRevision ? 1 : 0, id, watermark,
              revEqualsSchedule ? 1 : 0,
            ),
          // IfChanged, directly after its own mutate (W4): a guard-blocked UPDATE
          // leaves no lying audit row.
          auditStmtIfChanged(
            c,
            actor,
            kind === "update" ? "schedule_task_revision_update" : "schedule_task_revision_link",
            schedule.job_id,
            // from/to detail on purpose (decision 8 — audit_log IS the history): a
            // reconcile has no undo, so the pre-revision dates live only here.
            kind === "update"
              ? {
                  schedule_id: id, job_id: schedule.job_id, task_uuid: task.task_uuid,
                  source_row_index: row.source_row_index, name: f.name,
                  start_from: task.start_date, start_to: f.start_date,
                  finish_from: task.finish_date, finish_to: f.finish_date,
                  // The rule + the human's pick (the decision INPUTS) — never a claim
                  // about the landed value, which the CASE decides at write time.
                  percent_rule: plan.rule,
                  percent_pick: explicitRevision ? "revision" : "portal",
                }
              : {
                  schedule_id: id, job_id: schedule.job_id, task_uuid: task.task_uuid,
                  source_row_index: row.source_row_index,
                  from_name: task.name, to_name: f.name,
                  start_from: task.start_date, start_to: f.start_date,
                  finish_from: task.finish_date, finish_to: f.finish_date,
                },
          ),
        );
      };

      for (const row of page) {
        const pair: DiffMatchedPair | undefined = matchedByIndex.get(row.source_row_index);
        if (pair) {
          pushTaskUpdate(pair.task, row, "update");
          continue;
        }
        const linkTarget = linkTargetByIndex.get(row.source_row_index);
        if (linkTarget) {
          pushTaskUpdate(linkTarget, row, "link");
          continue;
        }
        // Fresh (unlinked) → INSERT exactly like the first import: a task's baseline is
        // ITS OWN first commit, so a task new in this revision baselines at these dates.
        const f = row.fields;
        const taskUuid = crypto.randomUUID();
        // match_key through the ONE shared normalizer (schedule_normalize.ts) — the
        // same function the manual add/edit routes and the diff engine use.
        const matchKey = scheduleMatchKey(f.section ?? "", f.name);
        insertIdx.push(stmts.length);
        stmts.push(
          c.env.DB
            .prepare(
              // baseline_start/finish = the committed dates (?7/?8 bound once, referenced
              // twice) — stamped at the task's OWN first commit, never rewritten (0071).
              // percent_done = schedule_percent = the row's percent (?9 twice): the
              // schedule's asserted % IS the portal's starting belief, and the copy in
              // schedule_percent is the three-way diff base. last_marked_by/at stay
              // NULL — commits never field-mark (0071 header).
              `INSERT INTO job_schedule_tasks
                 (task_uuid, job_id, section, name, match_key, duration_days,
                  start_date, finish_date, baseline_start_date, baseline_finish_date,
                  percent_done, schedule_percent, is_milestone, is_contract_milestone,
                  is_delivery, predecessors_raw, sort_order, source_row_index, source_schedule_id)
               SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?7, ?8, ?9, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16
                WHERE EXISTS (SELECT 1 FROM job_schedules
                               WHERE id = ?16 AND status = 'committing' AND committed_through_row = ?17)`,
            )
            .bind(
              taskUuid, schedule.job_id, f.section, f.name, matchKey, f.duration_days,
              f.start_date, f.finish_date, f.percent_done,
              f.is_milestone, f.is_contract_milestone, f.is_delivery,
              f.predecessors_raw, (seqByIndex.get(row.source_row_index) ?? 0) * 10,
              row.source_row_index, id, watermark,
            ),
          // IfChanged, directly after its own mutate (W4): a guard-blocked INSERT leaves
          // no lying audit row.
          auditStmtIfChanged(c, actor, "job_schedule_task_import", schedule.job_id, {
            schedule_id: id, job_id: schedule.job_id, task_uuid: taskUuid,
            source_row_index: row.source_row_index, name: f.name,
          }),
        );
      }

      const done = remaining.length <= page.length;
      if (done) {
        // Removals ride the FINAL page's batch (they are defined against the whole
        // document), guarded like every other write — a page racing a discard
        // deactivates nothing. 'keep' resolutions are simply not swept; non-blocking
        // removals default to remove (the plan showed them, the human left the default).
        for (const entry of effectiveRemoved) {
          if ((resolutions.removals.get(entry.task.id) ?? "remove") === "keep") continue;
          removeIdx.push(stmts.length);
          stmts.push(
            c.env.DB
              .prepare(
                `UPDATE job_schedule_tasks
                    SET active = 0, updated_schedule_id = ?2, updated_at = unixepoch()
                  WHERE id = ?1 AND active = 1
                    AND EXISTS (SELECT 1 FROM job_schedules
                                 WHERE id = ?2 AND status = 'committing' AND committed_through_row = ?3)`,
              )
              .bind(entry.task.id, id, watermark),
            auditStmtIfChanged(c, actor, "schedule_task_revision_remove", schedule.job_id, {
              schedule_id: id, job_id: schedule.job_id, task_uuid: entry.task.task_uuid,
              name: entry.task.name, blocking: entry.blocking, reasons: entry.reasons,
            }),
          );
        }
      }

      const res = await c.env.DB.batch(stmts);
      // Statement 0 changed nothing → the schedule left the committable states, the
      // watermark didn't advance, or ANOTHER schedule of this job is mid-commit —
      // every guarded write above then landed NOTHING. Say which, honestly.
      if ((res[0].meta.changes ?? 0) === 0) {
        const other = await c.env.DB
          .prepare(
            "SELECT id FROM job_schedules WHERE job_id = ?1 AND status = 'committing' AND id <> ?2 LIMIT 1",
          )
          .bind(schedule.job_id, id)
          .first<{ id: number }>();
        if (other) return c.json({ error: "another_commit_in_progress", other_id: other.id }, 409);
        const now = await loadSchedule(c, id);
        return c.json({ error: "schedule_not_committable", status: now?.status ?? "unknown" }, 409);
      }
      const countOf = (idx: number[]) => idx.filter((i) => (res[i].meta.changes ?? 0) > 0).length;
      const inserted = countOf(insertIdx);
      const updated = countOf(updateIdx);
      const linked = countOf(linkIdx);
      const removed = countOf(removeIdx);

      if (done) {
        await finalizeScheduleCommit(c, actor, schedule.job_id, id, inserted);
      }
      const payload: ScheduleCommitResponse = {
        ok: true, done, inserted, updated, linked, removed, committed_through_row: watermark,
      };
      return c.json(payload);
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
        // ORPHAN-TASK CASCADE (2026-08-11 security-review BLOCK finding 2): a schedule
        // discarded mid-commit has already landed active tasks (source_schedule_id =
        // this row) across its committed pages. Left active, they are attributed to a
        // TERMINAL schedule that never governed — and every FUTURE upload would then
        // diff against them (when PR-4 counted instead of diffing, this was a permanent
        // commit lockout; post-PR-6 it would silently pollute every reconcile with
        // phantom matches/removals). Deactivating them WITH the discard restores the
        // invariant that active imported tasks always trace to a committing/committed
        // schedule. Safe by construction: committed/superseded rows are not discardable,
        // so a governing schedule's tasks can never be swept by this statement.
        c.env.DB
          .prepare(
            "UPDATE job_schedule_tasks SET active = 0, updated_at = unixepoch() " +
              "WHERE source_schedule_id = ?1 AND active = 1 " +
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
      const orphanedTasksDeactivated = res[5]?.meta?.changes ?? 0;
      if (orphanedTasksDeactivated > 0) {
        // The cascade is multi-row, so it carries its own audit record naming the count
        // (the expected-materials cascade pattern) — recorded AFTER the batch so the
        // count is the real one, on a path that just proved the discard landed.
        await c.env.DB.batch([
          auditStmt(c, actor, "job_schedule_discard_orphan_tasks", String(id), {
            schedule_id: id, deactivated: orphanedTasksDeactivated,
          }),
        ]);
      }
      return c.json({ ok: true, id, orphaned_tasks_deactivated: orphanedTasksDeactivated });
    },
  );
}
