import type { Context, MiddlewareHandler } from "hono";
import type { FieldopsApp } from "./fieldops_gates";
import type { Env, Vars } from "./types";
import type { ProductionReportResponse } from "./wire-types";
import { auditStmt } from "./audit";
import { pacificDateString } from "./fieldops_recurrence";

// Weekly Production Report — the SEND-FREE, READ-ONLY D1 aggregation behind the client-facing
// 5-page "Evergreen Weekly Production Report", plus the office-input record that supplies the
// three sections D1 structurally cannot answer.
//
// Invariant 1 (External Send Gate): ZERO external transmission. This module only reads D1, writes
// one office-owned D1 row, and returns JSON to a caller that initiated the request. No fetch, no
// email, no AI. The report it feeds is compiled on the Mac by `progress_weekly_generate` and sent
// only after a human approves the `WPR_human_review` row — a different process entirely.
//
// Invariant 2 (Adversarial Input): every query param is validated and bounded UP FRONT and the
// WHOLE request is rejected on any bad one (never a partial window silently returning zeros — the
// `fieldops_rollup.ts` discipline). All D1 access is parameter-bound. Every list leg is row-capped.
// Every office blob is shape-validated on write. Field-reported free text (crew names, photo
// captions, weather conditions) flows out as data and is escaped by the PDF renderer, never here.
//
// House Reflex §5 (display-name-only): any WHO value resolves through `personnel.name`, never
// `users.username`.
//
// TWO GATES, ONE BUILDER. `GET /api/internal/production-report` is bearer-gated for the Mac compile
// (`PORTAL_INTERNAL_API_TOKEN` — the same privilege class and the same data as the existing
// `/api/internal/progress-rollup`, so NO new secret). `GET /api/fieldops/weekly-report` is
// session+capability-gated for the office screen. Both return the identical payload from
// `buildReportData`, because the screen must show the office exactly what the report will render —
// a second derivation would be a second truth.
//
// PAGE 3 (`schedule`) reads the ADR-0006 living task list (`job_schedule_tasks`, migration 0071).
// It stays NULL for a job with no committed schedule, and the renderer prints "No schedule imported
// for this job" — never a fabricated percentage, the discipline that removed `jobs.progress` from
// the rollup (operator decision 2026-06-30, fieldops_rollup.ts:15-17).
//
// A task's percent is reported as NULL — the report prints an em dash — when NOTHING has ever
// asserted one: no portal mark (`last_marked_by IS NULL`) AND no committed schedule value
// (`schedule_percent IS NULL`) AND `percent_done = 0`. That combination is the table's "never
// reported" state, because `percent_done` is NOT NULL DEFAULT 0 and so cannot represent it. Printing
// 0% there would tell a client no work was done, which is a different and possibly false claim.

// ── Row caps ────────────────────────────────────────────────────────────────────
// Code constants, never user input. They bound a pathological job/week without changing the honest
// small-N result any real week produces.
const DAILY_CAP = 40;      // daily-report submissions in a week (7 + amendments, generously)
const CREW_ROWS_CAP = 300; // crew_progress rows across the week
const HAZARD_CAP = 100;    // distinct safety-meeting form codes in the week
const DELIVERY_CAP = 300;  // delivery events in the week
const PHOTO_CAP = 200;     // clean photos offered to the office for curation
const INCIDENT_CAP = 100;  // material incidents in the week
const SCHEDULE_TASK_CAP = 600; // active schedule tasks on one job (a real solar schedule is ~100-300)
const JHA_SIGNIN_CAP = 400;    // JHA worker-acknowledgement sign-ins in a week (~12 signers × 7 days, amend-collapsed)

/** Max photos the deterministic auto-selection will place on the report's photo page. */
export const AUTO_SELECT_MAX = 8;

/** Bound on every office free-text field (one document line; far above any real entry). */
const MAX_TEXT = 4000;
/** Bound on office list fields (subcontractors, hazard topics, labor rows, photo picks). */
const MAX_LIST = 60;

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Digits-only non-negative epoch-seconds parse; null for undefined/empty/negative/decimal/sci.
 *  Byte-identical in behaviour to `fieldops_rollup.parseEpoch` — the two routes bound the same
 *  windows the same way, and a divergence between them would be a silent reporting drift. */
function parseEpoch(raw: string | undefined): number | null {
  if (raw === undefined || raw === "" || !/^\d{4,12}$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) ? n : null;
}

// Per-module copy (the fieldops_manifests/po_attachments convention — a shared util module for
// 6 lines has been declined before; keep them byte-identical).
function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function parseIsoDate(raw: string | undefined): string | null {
  if (raw === undefined || !ISO_DATE.test(raw)) return null;
  return raw;
}

type Window = {
  jobId: string;
  weekStart: string;
  weekEnd: string;
  from: number;
  to: number;
};

/** Validate the whole window or reject it. Returns a string error code on any bad param — the
 *  caller turns that into a 400 and returns NOTHING, so a malformed request can never come back
 *  looking like a genuinely quiet week. */
function parseWindow(q: (k: string) => string | undefined): Window | string {
  const jobId = q("job_id") ?? "";
  if (!jobId || jobId.length > 64) return "invalid_job_id";
  const weekStart = parseIsoDate(q("week_start"));
  const weekEnd = parseIsoDate(q("week_end"));
  if (weekStart === null || weekEnd === null) return "invalid_week";
  if (weekEnd < weekStart) return "invalid_week";
  const from = parseEpoch(q("from"));
  const to = parseEpoch(q("to"));
  if (from === null || to === null) return "invalid_window";
  if (to <= from) return "invalid_window";
  return { jobId, weekStart, weekEnd, from, to };
}

// ── Office-input blob validation ────────────────────────────────────────────────
// The office blobs are display-only text bound for one document, so validation is about BOUNDS and
// SHAPE, not vocabulary: reject anything oversized or structurally wrong, store the rest verbatim.
// The renderer escapes on the way out; nothing here is ever interpreted as markup or SQL.

function cleanText(v: unknown): string {
  return typeof v === "string" ? v.slice(0, MAX_TEXT) : "";
}

function cleanList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.slice(0, MAX_LIST).map(cleanText).filter((s) => s !== "");
}

/** A non-negative integer count, or 0. Safety counts are OSHA case tallies — never negative. */
function cleanCount(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) && n >= 0 ? Math.min(Math.floor(n), 100_000) : 0;
}

const SAFETY_KEYS = [
  "lost_time", "lost_work_days", "job_transfer", "near_miss", "other_recordable", "first_aid",
] as const;

function normalizeSafety(v: unknown): Record<string, { month: number; to_date: number }> {
  const src = (v ?? {}) as Record<string, unknown>;
  const out: Record<string, { month: number; to_date: number }> = {};
  for (const k of SAFETY_KEYS) {
    const cur = (src[k] ?? {}) as Record<string, unknown>;
    out[k] = { month: cleanCount(cur.month), to_date: cleanCount(cur.to_date) };
  }
  return out;
}

function normalizeHeader(v: unknown) {
  const s = (v ?? {}) as Record<string, unknown>;
  return {
    site_location: cleanText(s.site_location),
    ess_management: cleanText(s.ess_management),
    mobilization_date: parseIsoDate(typeof s.mobilization_date === "string" ? s.mobilization_date : undefined) ?? "",
    subcontractors: cleanList(s.subcontractors),
    prepared_by: cleanText(s.prepared_by),
  };
}

function normalizeWeather(v: unknown) {
  const s = (v ?? {}) as Record<string, unknown>;
  // Only well-formed ISO dates survive — an inclement flag is a contractual delay claim and must
  // key to a real calendar day, not to whatever string a client sent.
  const dates = Array.isArray(s.inclement_dates)
    ? s.inclement_dates.slice(0, MAX_LIST)
        .map((d) => parseIsoDate(typeof d === "string" ? d : undefined))
        .filter((d): d is string => d !== null)
    : [];
  return {
    inclement_dates: [...new Set(dates)].sort(),
    weather_days_to_date: cleanCount(s.weather_days_to_date),
  };
}

function normalizeLabor(v: unknown) {
  const s = (v ?? {}) as Record<string, unknown>;
  const rows = Array.isArray(s.rows) ? s.rows.slice(0, MAX_LIST) : [];
  return {
    rows: rows.map((r) => {
      const o = (r ?? {}) as Record<string, unknown>;
      return {
        company: cleanText(o.company),
        // Blank-preserving: "" means the office has not answered, which the report prints as an
        // empty cell. A 0 would assert "nobody worked" — a different and possibly false claim.
        workers: typeof o.workers === "number" || typeof o.workers === "string" ? cleanText(String(o.workers)).slice(0, 12) : "",
        man_hours: typeof o.man_hours === "number" || typeof o.man_hours === "string" ? cleanText(String(o.man_hours)).slice(0, 12) : "",
      };
    }).filter((r) => r.company !== ""),
  };
}

/** Narrative fields are THREE-STATE, the same shape the photo selection uses and for the same
 *  reason: `null` = never touched (the report falls back to its deterministic seed), `""` =
 *  deliberately cleared (the report prints nothing), text = the office's words.
 *
 *  Before this, touched-ness was ROW-level: the moment the office saved anything — the OSHA
 *  counts, say — an untouched Critical Items rendered BLANK on a client's page instead of the
 *  assembled text. That was compensated in the SPA by pre-filling the textareas, which works but
 *  makes a client-facing document's correctness depend on a SCREEN behaviour; any other writer of
 *  this row (a script, a future client, a migration) silently produced blank narrative. It did,
 *  once — a hand-seeded row is how the coupling was found. Now the storage carries the
 *  distinction and no writer can lose it. */
function cleanNarrativeField(v: unknown): string | null {
  if (v === null || v === undefined) return null;  // never touched
  return typeof v === "string" ? v.slice(0, MAX_TEXT) : null;
}

function normalizeNarrative(v: unknown) {
  const s = (v ?? {}) as Record<string, unknown>;
  return {
    critical_items: cleanNarrativeField(s.critical_items),
    upcoming_activities: cleanNarrativeField(s.upcoming_activities),
    hazard_topics: cleanList(s.hazard_topics),
  };
}

function normalizePending(v: unknown) {
  const s = (v ?? {}) as Record<string, unknown>;
  return {
    rfis: cleanText(s.rfis),
    submittals: cleanText(s.submittals),
    ifc_review: cleanText(s.ifc_review),
    change_orders: cleanText(s.change_orders),
  };
}

/** photos_json is THREE-STATE (see migration 0067): undefined/null = auto-select, [] = explicitly
 *  none, [...] = the office's ordered picks. This returns `null` for the auto case so the caller
 *  can persist SQL NULL and keep that distinction alive across saves. */
function normalizePhotos(v: unknown): { pool_id: number; box_file_id: string; caption: string; work_date: string }[] | null {
  if (v === undefined || v === null) return null;
  if (!Array.isArray(v)) return null;
  return v.slice(0, MAX_LIST).map((p) => {
    const o = (p ?? {}) as Record<string, unknown>;
    const poolId = Number(o.pool_id);
    return {
      pool_id: Number.isSafeInteger(poolId) && poolId > 0 ? poolId : 0,
      // box_file_id is stored ALONGSIDE pool_id on purpose: the pool row is prunable (0037's
      // 7-day unclaimed eviction) while the Box file is permanent, so a curated selection must
      // not dangle when the pool row ages out.
      box_file_id: cleanText(o.box_file_id).slice(0, 64),
      caption: cleanText(o.caption).slice(0, 300),
      work_date: parseIsoDate(typeof o.work_date === "string" ? o.work_date : undefined) ?? "",
    };
  }).filter((p) => p.box_file_id !== "");
}

type OfficeRow = {
  header_json: string; safety_json: string; weather_json: string;
  labor_json: string; narrative_json: string; pending_json: string;
  photos_json: string | null; week_start: string;
  updated_by: string | null; updated_at: number | null;
};

function parseBlob(raw: string | null | undefined): unknown {
  if (!raw) return {};
  try { return JSON.parse(raw); } catch { return {}; }
}

/** Shape the stored row (or a carried-forward one, or nothing) into the office half of the
 *  contract. Always returns every key, so the renderer and the screen never branch on presence. */
function shapeOffice(row: OfficeRow | null, carriedFrom: string | null) {
  return {
    header: normalizeHeader(parseBlob(row?.header_json)),
    safety: normalizeSafety(parseBlob(row?.safety_json)),
    weather: normalizeWeather(parseBlob(row?.weather_json)),
    labor: normalizeLabor(parseBlob(row?.labor_json)),
    // NARRATIVE IS NEVER CARRIED FORWARD. The header, the running safety totals and the pending
    // items are STANDING facts that persist week to week — carrying them is the point. The
    // narrative is the opposite: it describes ONE week's work, so inheriting it would seed next
    // week's Critical Items with last week's rain day and put stale text in front of a client.
    //
    // This has to happen HERE rather than in the reader. Until the narrative became three-state
    // the row-level `saved` flag masked it (carried ⇒ not saved ⇒ seed wins); making touched-ness
    // per-field removed that accident and exposed the real behaviour, which a live read on a
    // carried week caught. Nulling it restores the intent explicitly instead of by side effect.
    narrative: carriedFrom === null
      ? normalizeNarrative(parseBlob(row?.narrative_json))
      : { critical_items: null, upcoming_activities: null, hazard_topics: [] },
    pending: normalizePending(parseBlob(row?.pending_json)),
    photos: row ? normalizePhotos(parseBlob(row.photos_json ?? null)) : null,
    // `saved` distinguishes "the office reviewed this week" from "these values were inherited".
    // The screen shows provenance; the compile uses it to decide whether an untouched week is
    // worth flagging. A write-on-read seed would have destroyed this distinction.
    saved: row !== null && carriedFrom === null,
    carried_from: carriedFrom,
    updated_by: row?.updated_by ?? null,
    updated_at: row?.updated_at ?? null,
  };
}

/**
 * Deterministic photo auto-selection: round-robin across the DAYS that have photos, taking each
 * day's first photo before any day's second, until AUTO_SELECT_MAX or exhaustion.
 *
 * WHY round-robin and not "the first 8": a busy Monday would otherwise consume the whole page and
 * the client would see one day of a five-day week. Day coverage is what makes the page read as
 * progress. Deterministic and pure so the same week always proposes the same spread — the office's
 * override is then a real signal, not a race against a shuffling default.
 */
export function autoSelectPhotos<T extends { work_date: string }>(available: T[], max = AUTO_SELECT_MAX): T[] {
  const byDay = new Map<string, T[]>();
  for (const p of available) {
    const day = byDay.get(p.work_date);
    if (day) day.push(p); else byDay.set(p.work_date, [p]);
  }
  const days = [...byDay.keys()].sort();
  const picked: T[] = [];
  for (let round = 0; picked.length < max; round += 1) {
    let placedThisRound = false;
    for (const d of days) {
      const bucket = byDay.get(d);
      if (bucket && round < bucket.length) {
        picked.push(bucket[round]);
        placedThisRound = true;
        if (picked.length >= max) break;
      }
    }
    if (!placedThisRound) break; // every day exhausted
  }
  return picked;
}

/**
 * The one derivation. Both the bearer route (Mac compile) and the session route (office screen)
 * return exactly this, because the screen must show what the report will render.
 *
 * Everything here is DERIVED — no human step, no invention. A section with no source data comes
 * back empty and the renderer prints an honest empty state rather than a plausible-looking value.
 */
async function buildReportData(
  c: Context<{ Bindings: Env; Variables: Vars }>,
  w: Window,
): Promise<ProductionReportResponse> {
  // Daily reports for the week, AMEND-COLLAPSED (exclude any row a later row amends — the 0003
  // amends_uuid chain, same shape as the rollup's time_entries collapse). Windowed on `work_date`,
  // NOT created_at: a report filed late still belongs to the day it describes.
  const dailySql = `
    SELECT s.submission_uuid, s.work_date,
           json_extract(s.payload_json, '$.weather')                        AS conditions,
           json_extract(s.payload_json, '$.average_temp')                   AS avg_temp,
           json_extract(s.payload_json, '$.tomorrows_goals')                AS tomorrows_goals,
           json_extract(s.payload_json, '$.comments')                       AS comments,
           json_extract(s.payload_json, '$.safety_observations')            AS safety_observations,
           json_extract(s.payload_json, '$.sign_in.manpower_total.response') AS manpower_total,
           (SELECT p.name FROM personnel p WHERE p.username = s.actor_username ORDER BY p.id ASC LIMIT 1)
             AS prepared_by_display
      FROM submissions s
     WHERE s.job_id = ?1
       AND s.form_code LIKE 'daily-report%'
       AND s.work_date >= ?2 AND s.work_date <= ?3
       AND NOT EXISTS (SELECT 1 FROM submissions x WHERE x.amends_uuid = s.submission_uuid)
     ORDER BY s.work_date ASC, s.created_at ASC
     LIMIT ?4
  `;

  // Crew / Subcontractor Progress rows — the repeating table the foreman fills each day. This is
  // BOTH the labor seed (company + headcount) and the current-period narrative source.
  //
  // The array guard lives INSIDE the json_each argument, NOT in the WHERE clause. SQLite evaluates
  // a table-valued function as it builds the row source, before the WHERE filter can exclude
  // anything, so a WHERE-side `json_type(...) = 'array'` check does not protect it: one daily
  // report whose `crew_progress` is a scalar or a string raises `malformed JSON` and 500s the
  // WHOLE weekly report — for every job, every week, until a human found it. Substituting '[]'
  // makes a malformed payload contribute no rows, which is the module's stated contract (a
  // malformed shape degrades, never raises). Caught by the "no crew_progress array" test.
  //
  // And the guard uses the TWO-ARGUMENT json_type(doc, path), not json_type(json_extract(...)).
  // json_extract returns a SQL value, so for a JSON string "not-an-array" it yields the bare TEXT
  // `not-an-array` — which is not well-formed JSON, so json_type() re-parsing it raises the very
  // error the guard exists to prevent. The two-arg form inspects the element in place and returns
  // NULL for an absent path. (The first fix here used the one-arg form and stayed red.)
  //
  // `v.type = 'object'` guards each ELEMENT the same way: a bare JSON-string element dequotes to
  // raw text that json_extract would re-parse (and raise on). Same fix as jhaSql's — adversarial
  // review 2026-08-13 found the crash live-reachable through both queries.
  const crewSql = `
    SELECT s.work_date,
           json_extract(v.value, '$.crew_subcontractor') AS crew,
           json_extract(v.value, '$.manpower')           AS manpower,
           json_extract(v.value, '$.todays_progress')    AS progress
      FROM submissions s
      JOIN json_each(
             CASE WHEN json_type(s.payload_json, '$.crew_progress') = 'array'
                  THEN json_extract(s.payload_json, '$.crew_progress')
                  ELSE '[]' END
           ) v
     WHERE s.job_id = ?1
       AND s.form_code LIKE 'daily-report%'
       AND s.work_date >= ?2 AND s.work_date <= ?3
       AND v.type = 'object'
       AND NOT EXISTS (SELECT 1 FROM submissions x WHERE x.amends_uuid = s.submission_uuid)
     ORDER BY s.work_date ASC
     LIMIT ?4
  `;

  // JHA sign-in rows — the Worker Acknowledgement signature table every JHA carries (worker_name +
  // company columns; the key is `worker_acknowledgement` in all published versions, jha-v1..v3).
  // This is the week's record of which COMPANIES were actually on site, so it is the preferred
  // labor seed: the crew_progress "Crew / Subcontractor" free-text field gets filled with PEOPLE
  // by some foremen (the Deep Lake people-as-companies bug), while a JHA signer states an employer.
  //
  // The array guard is crewSql's two-arg json_type pattern (see the comment above crewSql for why
  // both halves of it are load-bearing) — PLUS the `v.type = 'object'` element guard: json_each's
  // per-element `value` for a bare JSON-string element is the DEQUOTED text, which json_extract
  // then re-parses as a JSON document and raises `malformed JSON`, 500ing the whole report route.
  // The type filter drops non-object elements before json_extract ever sees them (adversarial
  // review 2026-08-13, live-reproduced). `.signature` (SVG path data) is deliberately never
  // selected — nothing here needs it and it must not cross this wire.
  //
  // Amend-collapse is REQUIRED here, unlike hazardSql (whose DISTINCT form_code makes duplicates
  // harmless): a re-signed JHA amendment re-carries the whole signer table, and counting both the
  // original and the amendment would double every signer on that day.
  const jhaSql = `
    SELECT s.work_date,
           json_extract(v.value, '$.worker_name') AS worker_name,
           json_extract(v.value, '$.company')     AS company
      FROM submissions s
      JOIN json_each(
             CASE WHEN json_type(s.payload_json, '$.worker_acknowledgement') = 'array'
                  THEN json_extract(s.payload_json, '$.worker_acknowledgement')
                  ELSE '[]' END
           ) v
     WHERE s.job_id = ?1
       AND s.form_code LIKE 'jha-%'
       AND s.work_date >= ?2 AND s.work_date <= ?3
       AND v.type = 'object'
       AND NOT EXISTS (SELECT 1 FROM submissions x WHERE x.amends_uuid = s.submission_uuid)
     ORDER BY s.work_date ASC, s.created_at ASC
     LIMIT ?4
  `;

  // Labor hours: the job total for the window, amend-collapsed. Deliberately NOT split by company —
  // `personnel` carries no employer column, so attributing a person's hours to a subcontractor
  // would be a guess. The office splits it on the weekly screen; this is the honest total.
  const laborSql = `
    SELECT COALESCE(SUM(t.hours), 0) AS total_hours
      FROM time_entries t
     WHERE t.job_id = ?1
       AND COALESCE(t.work_started_at, t.created_at) >= ?2
       AND COALESCE(t.work_started_at, t.created_at) < ?3
       AND NOT EXISTS (SELECT 1 FROM time_entries x WHERE x.amends_uuid = t.uuid)
  `;

  // Safety-meeting topics: the DISTINCT toolbox-talk / JHA form codes filed this week. Only the
  // CODE crosses the wire — the display name lives in the git-owned form definitions, which the
  // Mac already loads (`form_pdf.load_definition`). Duplicating that mapping here would be a
  // second source of truth for a name that changes when a form is republished.
  const hazardSql = `
    SELECT DISTINCT s.form_code
      FROM submissions s
     WHERE s.job_id = ?1
       AND (s.form_code LIKE 'toolbox-talk-%' OR s.form_code LIKE 'jha-%')
       AND s.work_date >= ?2 AND s.work_date <= ?3
     ORDER BY s.form_code ASC
     LIMIT ?4
  `;

  // Material deliveries received this week — the page-5 tracking log. Reads the structured
  // receipt ledger, not form payloads. `vendor` resolves to the catalog MANUFACTURER where the
  // line names a catalog type; material lines carry no vendor link, and the corpus reports read
  // "(Owner Provided)" in most of those cells, so a blank here matches the source documents.
  const deliverySql = `
    SELECT e.event_date, e.kind, e.qty,
           jem.description AS item, jem.part_number, jem.unit,
           mc.manufacturer AS vendor,
           sh.bol_number, sh.carrier
      FROM material_receipt_events e
      LEFT JOIN job_expected_materials jem ON jem.id = e.line_id
      LEFT JOIN material_catalog       mc  ON mc.id = jem.material_id
      LEFT JOIN material_shipments     sh  ON sh.id = e.shipment_id
     WHERE e.job_id = ?1
       AND e.event_date >= ?2 AND e.event_date <= ?3
       AND e.kind IN ('delivered', 'partial')
     ORDER BY e.event_date ASC, e.event_uuid ASC
     LIMIT ?4
  `;

  // Material incidents filed this week — feeds the assembled Critical Items text.
  const incidentSql = `
    SELECT s.work_date,
           json_extract(s.payload_json, '$.material_description') AS material,
           json_extract(s.payload_json, '$.issue')                AS issue,
           json_extract(s.payload_json, '$.details')              AS details
      FROM submissions s
     WHERE s.job_id = ?1
       AND s.form_code LIKE 'material-incident%'
       AND s.work_date >= ?2 AND s.work_date <= ?3
     ORDER BY s.work_date ASC
     LIMIT ?4
  `;

  // Photos offered for curation: clean, screened, Box-filed. `box_file_id IS NOT NULL` is the
  // structural control — a pool row only earns one on the Mac's §34 CLEAN disposition, so an
  // unscreened or refused photo CANNOT reach a client report through this route.
  // The caption lives in the claiming submission's `values.additional_photos[{pool_id, caption}]`
  // (0037: the pool row itself has no caption column), so it is looked up per photo.
  const photoSql = `
    SELECT dp.id AS pool_id, dp.work_date, dp.box_file_id,
           (dp.thumb_b64 IS NOT NULL) AS has_thumb,
           COALESCE(NULLIF(dp.caption, ''), (
             SELECT json_extract(v.value, '$.caption')
               FROM submissions s2,
                    json_each(
                      CASE WHEN json_type(s2.payload_json, '$.additional_photos') = 'array'
                           THEN json_extract(s2.payload_json, '$.additional_photos')
                           ELSE '[]' END
                    ) v
              WHERE s2.submission_uuid = dp.claimed_by_submission
                AND json_extract(v.value, '$.pool_id') = dp.id
              LIMIT 1
           ), '') AS caption
      FROM daily_photo_pool dp
     WHERE dp.job_id = ?1
       AND dp.work_date >= ?2 AND dp.work_date <= ?3
       AND dp.status = 'clean'
       AND dp.box_file_id IS NOT NULL
     ORDER BY dp.work_date ASC, dp.id ASC
     LIMIT ?4
  `;

  // Page 3 — the living schedule task list (ADR-0006, migration 0071). ACTIVE tasks only (the
  // table soft-deletes), in document order (`sort_order`, set to seq*10 at commit).
  //
  // `reported_percent` is NULL for the table's "never reported" state — no portal mark, no
  // committed schedule value, and percent_done still at its NOT NULL DEFAULT 0. The renderer
  // prints an em dash for it rather than 0%.
  const scheduleSql = `
    SELECT t.section, t.name, t.percent_done, t.schedule_percent, t.last_marked_by,
           t.start_date, t.finish_date, t.baseline_finish_date,
           t.is_milestone, t.is_contract_milestone, t.is_delivery, t.delivered_date,
           CASE WHEN t.last_marked_by IS NULL AND t.schedule_percent IS NULL AND t.percent_done = 0
                THEN NULL ELSE t.percent_done END AS reported_percent
      FROM job_schedule_tasks t
     WHERE t.job_id = ?1 AND t.active = 1
     ORDER BY t.sort_order ASC, t.id ASC
     LIMIT ?2
  `;

  // The office record for THIS week, and — separately — the most recent EARLIER one, so a missing
  // week can resolve by carry-forward without a write. Both fetched unconditionally in the batch;
  // choosing between them is pure logic below, which keeps the round-trip count fixed.
  const officeSql = `
    SELECT week_start, header_json, safety_json, weather_json, labor_json, narrative_json,
           pending_json, photos_json, updated_by, updated_at
      FROM job_weekly_report_inputs
     WHERE job_id = ?1 AND week_start = ?2
  `;
  const priorOfficeSql = `
    SELECT week_start, header_json, safety_json, weather_json, labor_json, narrative_json,
           pending_json, photos_json, updated_by, updated_at
      FROM job_weekly_report_inputs
     WHERE job_id = ?1 AND week_start < ?2
     ORDER BY week_start DESC
     LIMIT 1
  `;

  const jobSql = `
    SELECT project_name, address, address_city, address_state, job_no, site_phase, status
      FROM jobs WHERE job_id = ?1
  `;

  const [jobRes, dailyRes, crewRes, jhaRes, laborRes, hazardRes, deliveryRes, incidentRes, photoRes, officeRes, priorRes, schedRes] =
    await c.env.DB.batch([
      c.env.DB.prepare(jobSql).bind(w.jobId),
      c.env.DB.prepare(dailySql).bind(w.jobId, w.weekStart, w.weekEnd, DAILY_CAP),
      c.env.DB.prepare(crewSql).bind(w.jobId, w.weekStart, w.weekEnd, CREW_ROWS_CAP),
      c.env.DB.prepare(jhaSql).bind(w.jobId, w.weekStart, w.weekEnd, JHA_SIGNIN_CAP),
      c.env.DB.prepare(laborSql).bind(w.jobId, w.from, w.to),
      c.env.DB.prepare(hazardSql).bind(w.jobId, w.weekStart, w.weekEnd, HAZARD_CAP),
      c.env.DB.prepare(deliverySql).bind(w.jobId, w.weekStart, w.weekEnd, DELIVERY_CAP),
      c.env.DB.prepare(incidentSql).bind(w.jobId, w.weekStart, w.weekEnd, INCIDENT_CAP),
      c.env.DB.prepare(photoSql).bind(w.jobId, w.weekStart, w.weekEnd, PHOTO_CAP),
      c.env.DB.prepare(officeSql).bind(w.jobId, w.weekStart),
      c.env.DB.prepare(priorOfficeSql).bind(w.jobId, w.weekStart),
      c.env.DB.prepare(scheduleSql).bind(w.jobId, SCHEDULE_TASK_CAP),
    ]);

  const job = (jobRes.results?.[0] ?? null) as Record<string, unknown> | null;
  const daily = (dailyRes.results ?? []) as Record<string, unknown>[];
  const crew = (crewRes.results ?? []) as Record<string, unknown>[];
  const jhaRows = (jhaRes.results ?? []) as Record<string, unknown>[];
  const totalHours = (laborRes.results?.[0] as { total_hours: number } | undefined)?.total_hours ?? 0;
  const hazardCodes = ((hazardRes.results ?? []) as { form_code: string }[]).map((r) => r.form_code);
  const deliveries = (deliveryRes.results ?? []) as Record<string, unknown>[];
  const incidents = (incidentRes.results ?? []) as Record<string, unknown>[];
  // has_thumb rides as 0/1 from SQLite; normalize to a real boolean at the wire boundary.
  const photosAvailable = ((photoRes.results ?? []) as {
    pool_id: number; work_date: string; box_file_id: string; caption: string; has_thumb: number;
  }[]).map((r) => ({ ...r, has_thumb: r.has_thumb === 1 }));

  const scheduleTasks = (schedRes.results ?? []) as ScheduleTaskRow[];
  const todayPacific = pacificDateString(Date.now());

  const jhaCompanies = aggregateJhaSignins(jhaRows);

  const ownRow = (officeRes.results?.[0] ?? null) as OfficeRow | null;
  const priorRow = (priorRes.results?.[0] ?? null) as OfficeRow | null;
  // Own row wins. Only when this week has none does the prior week carry forward — and it is
  // returned flagged, never silently adopted.
  const office = ownRow
    ? shapeOffice(ownRow, null)
    : shapeOffice(priorRow, priorRow?.week_start ?? null);

  // Weather is one row per DAILY REPORT — the only weather ITS captures is the foreman's free-text
  // conditions and average temp. `inclement` is NOT derived: it comes from the office's marked
  // dates, because a weather day is a contractual delay claim rather than an observation.
  const inclement = new Set(office.weather.inclement_dates);
  const weatherDays = daily.map((d) => ({
    work_date: String(d.work_date ?? ""),
    conditions: d.conditions === null || d.conditions === undefined ? "" : String(d.conditions),
    avg_temp: d.avg_temp === null || d.avg_temp === undefined ? "" : String(d.avg_temp),
    inclement: inclement.has(String(d.work_date ?? "")),
  }));

  return {
    job_id: w.jobId,
    week: { start: w.weekStart, end: w.weekEnd, from: w.from, to: w.to },
    job: job === null ? null : {
      project_name: String(job.project_name ?? ""),
      address: String(job.address ?? ""),
      address_city: String(job.address_city ?? ""),
      address_state: String(job.address_state ?? ""),
      job_no: String(job.job_no ?? ""),
      site_phase: Number(job.site_phase ?? 0),
      status: String(job.status ?? ""),
    },
    // The report's own emptiness test. `progress_weekly_generate` HOLDs a week with no daily
    // reports for the office to decide rather than emailing a client a hollow document.
    daily_report_count: daily.length,
    weather: {
      days: weatherDays,
      weather_days_week: weatherDays.filter((d) => d.inclement).length,
      weather_days_to_date: office.weather.weather_days_to_date,
    },
    labor: {
      total_hours: totalHours,
      // Seed for the office's Labor Report. The week's JHA sign-in table (worker_acknowledgement)
      // is the record of which COMPANIES were actually on site, so it wins when present; the
      // foreman's free-text crew_progress rows (which some crews fill with PEOPLE — the Deep Lake
      // people-as-companies bug) are the fallback. No merging of the two: they are two messy
      // free-text namespaces and merging would double-list companies. The office's saved
      // labor_json outranks both — in the consumers, unchanged.
      crews: jhaCompanies.length > 0 ? jhaCompanies : aggregateCrews(crew),
      seed_source: jhaCompanies.length > 0 ? ("jha" as const) : ("daily" as const),
    },
    crew_progress: crew.map((r) => ({
      work_date: String(r.work_date ?? ""),
      crew: r.crew === null || r.crew === undefined ? "" : String(r.crew),
      manpower: r.manpower === null || r.manpower === undefined ? "" : String(r.manpower),
      progress: r.progress === null || r.progress === undefined ? "" : String(r.progress),
    })),
    daily_notes: daily.map((d) => ({
      work_date: String(d.work_date ?? ""),
      tomorrows_goals: d.tomorrows_goals === null || d.tomorrows_goals === undefined ? "" : String(d.tomorrows_goals),
      comments: d.comments === null || d.comments === undefined ? "" : String(d.comments),
      safety_observations: d.safety_observations === null || d.safety_observations === undefined ? "" : String(d.safety_observations),
      manpower_total: d.manpower_total === null || d.manpower_total === undefined ? "" : String(d.manpower_total),
      prepared_by: String(d.prepared_by_display ?? ""),
    })),
    hazard_form_codes: hazardCodes,
    deliveries: deliveries.map((r) => ({
      event_date: String(r.event_date ?? ""),
      kind: String(r.kind ?? ""),
      item: r.item === null || r.item === undefined ? "" : String(r.item),
      part_number: r.part_number === null || r.part_number === undefined ? "" : String(r.part_number),
      qty: r.qty === null || r.qty === undefined ? "" : String(r.qty),
      unit: r.unit === null || r.unit === undefined ? "" : String(r.unit),
      vendor: r.vendor === null || r.vendor === undefined ? "" : String(r.vendor),
      bol_number: r.bol_number === null || r.bol_number === undefined ? "" : String(r.bol_number),
      carrier: r.carrier === null || r.carrier === undefined ? "" : String(r.carrier),
    })),
    material_incidents: incidents.map((r) => ({
      work_date: String(r.work_date ?? ""),
      material: r.material === null || r.material === undefined ? "" : String(r.material),
      issue: r.issue === null || r.issue === undefined ? "" : String(r.issue),
      details: r.details === null || r.details === undefined ? "" : String(r.details),
    })),
    photos: {
      available: photosAvailable,
      // The office's explicit picks win outright — including the empty list, which means "no
      // photos this week" and must not re-populate on the next compile.
      selected: office.photos ?? autoSelectPhotos(photosAvailable).map((p) => ({
        pool_id: p.pool_id, box_file_id: p.box_file_id, caption: p.caption, work_date: p.work_date,
      })),
      auto_selected: office.photos === null,
    },
    // The living task list (0071). NULL — not an empty object — when the job has no committed
    // schedule, so the renderer prints its honest empty state rather than an empty table.
    schedule: scheduleTasks.length === 0 ? null : {
      sections: groupScheduleSections(scheduleTasks),
      behind: behindSchedule(scheduleTasks, todayPacific),
      today: todayPacific,
      task_count: scheduleTasks.length,
    },
    office,
    generated_at: Math.floor(Date.now() / 1000),
  };
}

type ScheduleTaskRow = {
  section: string | null; name: string; percent_done: number;
  schedule_percent: number | null; last_marked_by: string | null;
  start_date: string | null; finish_date: string | null; baseline_finish_date: string | null;
  is_milestone: number; is_contract_milestone: number; is_delivery: number;
  delivered_date: string | null; reported_percent: number | null;
};

/** Group the living task list into the report's page-3 sections, preserving document order for
 *  BOTH the sections and the tasks inside them (first appearance wins — the schedule's own order
 *  is the one the office and the client already read on the Gantt). A task with no `section`
 *  lands under "Other" rather than being dropped: `section` is nullable and a schedule whose
 *  parse found no phase rows would otherwise render an empty page 3 while holding real tasks. */
function groupScheduleSections(rows: ScheduleTaskRow[]) {
  const order: string[] = [];
  const byName = new Map<string, { label: string; items: { label: string; percent: number | null }[] }>();
  for (const t of rows) {
    const label = (t.section ?? "").trim() || "Other";
    const key = label.toLowerCase();
    let bucket = byName.get(key);
    if (!bucket) { bucket = { label, items: [] }; byName.set(key, bucket); order.push(key); }
    bucket.items.push({ label: t.name, percent: t.reported_percent });
  }
  return order.map((k) => ({ name: byName.get(k)!.label, items: byName.get(k)!.items }));
}

/** Tasks a client would call late: a finish date in the past and not complete. Ordered oldest
 *  finish first so the worst slip reads at the top of Critical Items. Contract milestones are
 *  flagged inline because a slipped one is a different conversation from a slipped task.
 *  Compared as YYYY-MM-DD strings, which sort chronologically — no date parsing in the path. */
function behindSchedule(rows: ScheduleTaskRow[], todayPacific: string) {
  return rows
    .filter((t) => t.finish_date !== null && t.finish_date < todayPacific && t.percent_done < 100)
    .sort((a, b) => (a.finish_date! < b.finish_date! ? -1 : a.finish_date! > b.finish_date! ? 1 : 0))
    .map((t) => ({
      name: t.name,
      section: (t.section ?? "").trim(),
      finish_date: t.finish_date!,
      percent: t.percent_done,
      is_contract_milestone: t.is_contract_milestone === 1,
    }));
}

/** Peak headcount per crew name across the week. Names are the foreman's free text, so they are
 *  grouped case-insensitively on a trimmed key while the first-seen spelling is what displays. */
function aggregateCrews(rows: Record<string, unknown>[]): { company: string; workers: number; days: number }[] {
  const acc = new Map<string, { company: string; workers: number; days: Set<string> }>();
  for (const r of rows) {
    const raw = r.crew === null || r.crew === undefined ? "" : String(r.crew).trim();
    if (raw === "") continue;
    const key = raw.toLowerCase();
    const n = Number(r.manpower);
    const workers = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
    const cur = acc.get(key);
    if (cur) {
      cur.workers = Math.max(cur.workers, workers);
      cur.days.add(String(r.work_date ?? ""));
    } else {
      acc.set(key, { company: raw, workers, days: new Set([String(r.work_date ?? "")]) });
    }
  }
  return [...acc.values()]
    .map((v) => ({ company: v.company, workers: v.workers, days: v.days.size }))
    .sort((a, b) => a.company.localeCompare(b.company));
}

// ── JHA labor seed ──────────────────────────────────────────────────────────────
// Canonical label for the self-perform row. A module constant, not config: ITS_Config is
// Mac-side and unreachable from the Worker, wrangler vars are feature flags, and this is report
// vocabulary with exactly one honest value — promote it only if it ever must change without a
// deploy. The prefix match covers every live spelling ("Evergreen", "Evergreen Renewables",
// "Evergreen renewables", "Evergree " — a real typo in filed JHAs). A genuine third-party company
// starting "Evergree…" would collapse into this row; none exists in live data, and the office's
// saved labor table always outranks the seed.
const EVERGREEN_COMPANY_LABEL = "Evergreen Renewables";
const EVERGREEN_COMPANY_PREFIX = "evergree";
// Named signers whose Company cell is blank stay VISIBLE under this row rather than being folded
// anywhere — the office must notice and resolve them, not have the seed guess an employer (§4).
const NO_COMPANY_LABEL = "(no company given)";

/** Free-text normalize for grouping: trim + collapse internal whitespace. Case is folded only in
 *  the grouping KEY so the display keeps a real spelling. */
function normFree(v: unknown): string {
  return v === null || v === undefined ? "" : String(v).trim().replace(/\s+/g, " ");
}

/** Per-company rows from the week's JHA Worker Acknowledgement sign-ins.
 *
 *  workers = PEAK across days of the per-day DISTINCT signer-name count for the company — the
 *  printed column means "workers on site", not worker-days, and the peak mirrors aggregateCrews'
 *  MAX semantics. The same person signing two JHAs on one day counts once (name-keyed set).
 *
 *  Unnamed rows are skipped entirely: the JHA signature table ships min_rows=4, so filed payloads
 *  carry blank filler rows, and an unnamed row identifies nobody countable.
 *
 *  Man-hours are deliberately NOT derived here: `personnel` has no employer column and
 *  subcontractors create crew + file time too (migration 0027), so any per-company hours split
 *  would be invented data. The office fills hours; the job-wide total rides beside the table.
 *
 *  Row order: Evergreen first, other companies alphabetically, "(no company given)" last. Display
 *  label per group = the most frequent original spelling (ties: longest, then lexicographically
 *  smallest) — deterministic, so the same week always renders the same table. */
function aggregateJhaSignins(rows: Record<string, unknown>[]): { company: string; workers: number; days: number }[] {
  // A LEADING SPACE cannot survive normFree (it trims), so these sentinel keys can never
  // collide with a real normalized company name.
  const EVERGREEN_KEY = " evergreen";
  const NO_COMPANY_KEY = " none";
  type Group = { spellings: Map<string, number>; byDay: Map<string, Set<string>> };
  const groups = new Map<string, Group>();
  for (const r of rows) {
    const name = normFree(r.worker_name);
    if (name === "") continue;
    const company = normFree(r.company);
    const lc = company.toLowerCase();
    const key = company === "" ? NO_COMPANY_KEY
      : lc.startsWith(EVERGREEN_COMPANY_PREFIX) ? EVERGREEN_KEY
      : lc;
    let g = groups.get(key);
    if (!g) { g = { spellings: new Map(), byDay: new Map() }; groups.set(key, g); }
    if (company !== "") g.spellings.set(company, (g.spellings.get(company) ?? 0) + 1);
    const day = String(r.work_date ?? "");
    const seen = g.byDay.get(day);
    if (seen) seen.add(name.toLowerCase()); else g.byDay.set(day, new Set([name.toLowerCase()]));
  }
  const displayLabel = (key: string, g: Group): string =>
    key === EVERGREEN_KEY ? EVERGREEN_COMPANY_LABEL
    : key === NO_COMPANY_KEY ? NO_COMPANY_LABEL
    : [...g.spellings.entries()]
        .sort((a, b) => b[1] - a[1] || b[0].length - a[0].length || (a[0] < b[0] ? -1 : 1))[0][0];
  const rank = (k: string): number => (k === EVERGREEN_KEY ? 0 : k === NO_COMPANY_KEY ? 2 : 1);
  return [...groups.entries()]
    .map(([key, g]) => ({
      key,
      company: displayLabel(key, g),
      // A group exists only once a named signer landed in it, so byDay is never empty here.
      workers: Math.max(...[...g.byDay.values()].map((set) => set.size)),
      days: g.byDay.size,
    }))
    .sort((a, b) => rank(a.key) - rank(b.key) || a.company.localeCompare(b.company))
    .map(({ company, workers, days }) => ({ company, workers, days }));
}

export function registerWeeklyReportRoutes(
  app: FieldopsApp,
  gates: {
    requireSession: MiddlewareHandler<{ Bindings: Env; Variables: Vars }>;
    requireCapability: (cap: string) => MiddlewareHandler<{ Bindings: Env; Variables: Vars }>;
    requireInternalToken: MiddlewareHandler<{ Bindings: Env; Variables: Vars }>;
  },
): void {
  const { requireSession, requireCapability, requireInternalToken } = gates;

  // ── The Mac compile's read ────────────────────────────────────────────────────
  // Bearer-gated, same privilege class as /api/internal/progress-rollup (no new secret).
  app.get("/api/internal/production-report", requireInternalToken, async (c) => {
    const w = parseWindow((k) => c.req.query(k));
    if (typeof w === "string") return c.json({ error: w }, 400);
    return c.json(await buildReportData(c, w), 200);
  });

  // ── The office screen's read ──────────────────────────────────────────────────
  // Identical payload, session+capability-gated. Office schedule/report ops ride the existing
  // office capability rather than minting a new one (the 0060 manifest precedent).
  app.get("/api/fieldops/weekly-report", requireSession, requireCapability("cap.jobtracker.manage"), async (c) => {
    const w = parseWindow((k) => c.req.query(k));
    if (typeof w === "string") return c.json({ error: w }, 400);
    return c.json(await buildReportData(c, w), 200);
  });

  // ── GET /api/fieldops/daily-photo/:id/thumb — the ONE image-serving route (0074). ────────────
  // Serves the SMALL screened thumbnail of a CLEAN pool row so the WPR picker stops choosing
  // photos blind. This deliberately relaxes the 0037 Option-D "record-only, no serving route"
  // posture — for thumbnails only (operator-approved 2026-08-13): the thumb is derived Mac-side
  // from the §34 CLEAN RE-ENCODE, never the raw upload, and ORIGINAL bytes are still never
  // served (pending rows' photo_json is untouchable here — the query reads thumb_b64 only).
  // Gate = the WPR screen's own read gate (session + cap.jobtracker.manage), po_estimates
  // preview shape: b64 → bytes, private/no-store (the global /api/* no-store agrees).
  app.get("/api/fieldops/daily-photo/:id/thumb", requireSession, requireCapability("cap.jobtracker.manage"), async (c) => {
    const photoId = parseInt(c.req.param("id"), 10);
    if (isNaN(photoId) || photoId < 1) return c.json({ error: "invalid_id" }, 400);
    const row = await c.env.DB
      .prepare("SELECT status, thumb_b64 FROM daily_photo_pool WHERE id = ?1")
      .bind(photoId)
      .first<{ status: string; thumb_b64: string | null }>();
    // Clean-with-thumb only; anything else — unknown row, pending, refused, thumbless — is one
    // indistinguishable 404 (no state oracle).
    if (!row || row.status !== "clean" || !row.thumb_b64) return c.json({ error: "not_found" }, 404);
    let bytes: Uint8Array;
    try {
      bytes = b64ToBytes(row.thumb_b64);
    } catch {
      return c.json({ error: "internal_error" }, 500);
    }
    return new Response(bytes as unknown as BodyInit, {
      headers: {
        "content-type": "image/jpeg",
        "cache-control": "private, no-store",
      },
    });
  });

  // ── The office screen's save ──────────────────────────────────────────────────
  // Upsert on (job_id, week_start) — the unique index is the target, so two office users editing
  // the same week converge on one row rather than forking it. Mutation + audit in ONE batch (W4).
  app.put("/api/fieldops/weekly-report", requireSession, requireCapability("cap.jobtracker.manage"), async (c) => {
    // `bad_request` is the house-wide malformed-body code (config.ts, fieldops_checklist.ts,
    // fieldops_expected_materials.ts and five more) and already carries plain-language copy.
    // Minting an `invalid_body` synonym would have grown the operator vocabulary for nothing.
    let body: Record<string, unknown>;
    try {
      const parsed = await c.req.json();
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return c.json({ error: "bad_request" }, 400);
      }
      body = parsed as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }

    const jobId = typeof body.job_id === "string" ? body.job_id : "";
    if (!jobId || jobId.length > 64) return c.json({ error: "invalid_job_id" }, 400);
    const weekStart = parseIsoDate(typeof body.week_start === "string" ? body.week_start : undefined);
    if (weekStart === null) return c.json({ error: "invalid_week" }, 400);

    // The job must exist. An office record for a job that does not exist would never be read and
    // would survive the purge cascade as an orphan.
    const job = await c.env.DB.prepare("SELECT job_id FROM jobs WHERE job_id = ?").bind(jobId).first();
    if (!job) return c.json({ error: "unknown_job" }, 404);

    // Normalize every blob before it is stored. Oversized or wrong-shaped input is trimmed to the
    // documented bounds rather than rejected — this is office text destined for one document, and
    // failing a whole weekly save over a long paste would be the wrong trade.
    const header = JSON.stringify(normalizeHeader(body.header));
    const safety = JSON.stringify(normalizeSafety(body.safety));
    const weather = JSON.stringify(normalizeWeather(body.weather));
    const labor = JSON.stringify(normalizeLabor(body.labor));
    const narrative = JSON.stringify(normalizeNarrative(body.narrative));
    const pending = JSON.stringify(normalizePending(body.pending));
    // Preserve the three-state contract: an ABSENT `photos` key means "leave curation to the
    // auto-selection" (SQL NULL); a present empty array means "no photos this week".
    //
    // A PRESENT-but-malformed `photos` is REJECTED rather than degraded. Everything else in this
    // body is clamped-not-rejected (office text bound for one document — failing a whole weekly
    // save over a long paste would be the wrong trade), but photos is the one field where the
    // degrade is indistinguishable from a legitimate state: silently treating junk as `null`
    // would mean "auto-select", so an office user who meant to clear the photo page would see
    // it silently re-populate on the next compile. A save that cannot be honoured must say so.
    if ("photos" in body && body.photos !== null && !Array.isArray(body.photos)) {
      return c.json({ error: "invalid_photos" }, 400);
    }
    const photosNorm = "photos" in body ? normalizePhotos(body.photos) : null;
    const photos = photosNorm === null ? null : JSON.stringify(photosNorm);

    const actor = c.get("session").username;
    const now = Math.floor(Date.now() / 1000);

    await c.env.DB.batch([
      c.env.DB.prepare(
        `INSERT INTO job_weekly_report_inputs
           (job_id, week_start, header_json, safety_json, weather_json, labor_json,
            narrative_json, pending_json, photos_json, updated_by, updated_at)
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)
         ON CONFLICT(job_id, week_start) DO UPDATE SET
           header_json=excluded.header_json, safety_json=excluded.safety_json,
           weather_json=excluded.weather_json, labor_json=excluded.labor_json,
           narrative_json=excluded.narrative_json, pending_json=excluded.pending_json,
           photos_json=excluded.photos_json, updated_by=excluded.updated_by,
           updated_at=excluded.updated_at`,
      ).bind(jobId, weekStart, header, safety, weather, labor, narrative, pending, photos, actor, now),
      auditStmt(c, actor, "weekly_report_inputs_save", jobId, {
        job_id: jobId,
        week_start: weekStart,
        photos: photosNorm === null ? "auto" : photosNorm.length,
      }),
    ]);

    return c.json({ ok: true, job_id: jobId, week_start: weekStart, updated_at: now }, 200);
  });
}
