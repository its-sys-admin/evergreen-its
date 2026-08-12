import type { Context, MiddlewareHandler } from "hono";
import type { FieldopsApp } from "./fieldops_gates";
import type { Env, Vars } from "./types";
import { auditStmt } from "./audit";

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
// WHAT IS DELIBERATELY ABSENT: `job_schedule_tasks` (page 3's %-complete). The ADR-0006 schedule
// lane owns that table and has not landed it. `schedule` returns null here and the renderer prints
// "No schedule imported for this job" — never a fabricated percentage. The same discipline that
// removed `jobs.progress` from the rollup (operator decision 2026-06-30, fieldops_rollup.ts:15-17).
// PR-5 fills this in with no renderer change.

// ── Row caps ────────────────────────────────────────────────────────────────────
// Code constants, never user input. They bound a pathological job/week without changing the honest
// small-N result any real week produces.
const DAILY_CAP = 40;      // daily-report submissions in a week (7 + amendments, generously)
const CREW_ROWS_CAP = 300; // crew_progress rows across the week
const HAZARD_CAP = 100;    // distinct safety-meeting form codes in the week
const DELIVERY_CAP = 300;  // delivery events in the week
const PHOTO_CAP = 200;     // clean photos offered to the office for curation
const INCIDENT_CAP = 100;  // material incidents in the week

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

function normalizeNarrative(v: unknown) {
  const s = (v ?? {}) as Record<string, unknown>;
  return {
    critical_items: cleanText(s.critical_items),
    upcoming_activities: cleanText(s.upcoming_activities),
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
    narrative: normalizeNarrative(parseBlob(row?.narrative_json)),
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
) {
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
       AND NOT EXISTS (SELECT 1 FROM submissions x WHERE x.amends_uuid = s.submission_uuid)
     ORDER BY s.work_date ASC
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
           COALESCE((
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

  const [jobRes, dailyRes, crewRes, laborRes, hazardRes, deliveryRes, incidentRes, photoRes, officeRes, priorRes] =
    await c.env.DB.batch([
      c.env.DB.prepare(jobSql).bind(w.jobId),
      c.env.DB.prepare(dailySql).bind(w.jobId, w.weekStart, w.weekEnd, DAILY_CAP),
      c.env.DB.prepare(crewSql).bind(w.jobId, w.weekStart, w.weekEnd, CREW_ROWS_CAP),
      c.env.DB.prepare(laborSql).bind(w.jobId, w.from, w.to),
      c.env.DB.prepare(hazardSql).bind(w.jobId, w.weekStart, w.weekEnd, HAZARD_CAP),
      c.env.DB.prepare(deliverySql).bind(w.jobId, w.weekStart, w.weekEnd, DELIVERY_CAP),
      c.env.DB.prepare(incidentSql).bind(w.jobId, w.weekStart, w.weekEnd, INCIDENT_CAP),
      c.env.DB.prepare(photoSql).bind(w.jobId, w.weekStart, w.weekEnd, PHOTO_CAP),
      c.env.DB.prepare(officeSql).bind(w.jobId, w.weekStart),
      c.env.DB.prepare(priorOfficeSql).bind(w.jobId, w.weekStart),
    ]);

  const job = (jobRes.results?.[0] ?? null) as Record<string, unknown> | null;
  const daily = (dailyRes.results ?? []) as Record<string, unknown>[];
  const crew = (crewRes.results ?? []) as Record<string, unknown>[];
  const totalHours = (laborRes.results?.[0] as { total_hours: number } | undefined)?.total_hours ?? 0;
  const hazardCodes = ((hazardRes.results ?? []) as { form_code: string }[]).map((r) => r.form_code);
  const deliveries = (deliveryRes.results ?? []) as Record<string, unknown>[];
  const incidents = (incidentRes.results ?? []) as Record<string, unknown>[];
  const photosAvailable = ((photoRes.results ?? []) as {
    pool_id: number; work_date: string; box_file_id: string; caption: string;
  }[]);

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
      // Seed for the office's Labor Report: peak headcount per typed crew name across the week.
      // MAX, not sum — the same crew reported on five days is one crew, not five.
      crews: aggregateCrews(crew),
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
    // ADR-0006 job_schedule_tasks lands here (PR-5). null → the renderer prints
    // "No schedule imported for this job". NEVER a fabricated percentage.
    schedule: null,
    office,
    generated_at: Math.floor(Date.now() / 1000),
  };
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

  // ── The office screen's save ──────────────────────────────────────────────────
  // Upsert on (job_id, week_start) — the unique index is the target, so two office users editing
  // the same week converge on one row rather than forking it. Mutation + audit in ONE batch (W4).
  app.put("/api/fieldops/weekly-report", requireSession, requireCapability("cap.jobtracker.manage"), async (c) => {
    let body: Record<string, unknown>;
    try {
      const parsed = await c.req.json();
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return c.json({ error: "invalid_body" }, 400);
      }
      body = parsed as Record<string, unknown>;
    } catch {
      return c.json({ error: "invalid_body" }, 400);
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
