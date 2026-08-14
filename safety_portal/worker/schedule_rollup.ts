import type { JobScheduleSummary } from "./wire-types";

// Schedule signal for the Job Tracker (Track A2) — ONE grouped-SQL derivation of each job's
// schedule state from the ADR-0006 living task list, shared by the jobs LIST, the job DETAIL,
// and (A6) the portfolio strip, so the late/percent predicates can never drift between them.
//
// The math is deliberately IN SQL: percent is the duration-weighted mean
// (schedule_view.weightedPercent parity — weight = duration_days when > 0 else 1, floored), and
// fetching up to 600–2000 rows per job to compute a number SQL produces in one grouped scan of
// idx_job_schedule_tasks_job would be page-cost for nothing. The aggregate has NO row cap —
// COUNT/SUM return one row per job, and this is the honest full-schedule number: on a >600-task
// job it may differ from the Schedule page's capped display, which truncates and says so.
//
// LATE predicate — byte-for-byte the report's behindSchedule rule (fieldops_report.ts) and
// schedule_view.isLate: finish_date < today AND percent_done < 100. If you change one, change
// all three (the A6 parity test compares this aggregate to the report's behind list).
//
// Cap posture: job_schedule_tasks is served whole under cap.jobtracker.read
// (fieldops_schedule_tasks.ts; ADR-0006 decision 7), so an aggregate of the same table on the
// same-cap jobs routes leaks nothing. No W9 fields ride out (name/date only — no usernames).

/** The SQL fragment both queries share. `?T` = the Pacific today the caller derives once. */
const WEIGHT = "CASE WHEN t.duration_days IS NOT NULL AND t.duration_days > 0 THEN t.duration_days ELSE 1 END";

const AGGREGATE_SQL = `
  SELECT t.job_id,
         COUNT(*) AS task_count,
         SUM(${WEIGHT}) AS weight_total,
         SUM((${WEIGHT}) * t.percent_done) AS weight_done,
         SUM(CASE WHEN t.finish_date IS NOT NULL AND t.finish_date < ?1 AND t.percent_done < 100
                  THEN 1 ELSE 0 END) AS late_count
  FROM job_schedule_tasks t
  WHERE t.active = 1 AND t.job_id IN `;

// Next milestone per job — the earliest UNREACHED milestone at/after today (schedule_view
// .summarize parity: date = COALESCE(finish_date, start_date); ISO strings sort chronologically).
const MILESTONE_SQL = `
  SELECT job_id, name, next_date FROM (
    SELECT t.job_id, t.name, COALESCE(t.finish_date, t.start_date) AS next_date,
           ROW_NUMBER() OVER (PARTITION BY t.job_id
                              ORDER BY COALESCE(t.finish_date, t.start_date) ASC, t.id ASC) AS rn
    FROM job_schedule_tasks t
    WHERE t.active = 1 AND t.is_milestone = 1 AND t.percent_done < 100
      AND COALESCE(t.finish_date, t.start_date) >= ?1
      AND t.job_id IN `;

type AggRow = { job_id: string; task_count: number; weight_total: number; weight_done: number; late_count: number };
type MsRow = { job_id: string; name: string; next_date: string };

/** Per-job schedule summaries for `jobIds` (page-scoped — pass the page's ids, never all jobs).
 *  Jobs with no active schedule tasks are simply absent from the map; callers serve `null`
 *  ("No schedule imported" — the honest state, never a fabricated percentage). */
export async function scheduleSummaries(
  db: D1Database,
  jobIds: string[],
  today: string,
): Promise<Map<string, JobScheduleSummary>> {
  const out = new Map<string, JobScheduleSummary>();
  if (jobIds.length === 0) return out;
  const placeholders = jobIds.map((_x, i) => `?${i + 2}`).join(",");
  const [aggRes, msRes] = await db.batch([
    db.prepare(AGGREGATE_SQL + `(${placeholders}) GROUP BY t.job_id`).bind(today, ...jobIds),
    db.prepare(MILESTONE_SQL + `(${placeholders})\n  ) WHERE rn = 1`).bind(today, ...jobIds),
  ]);
  const milestones = new Map<string, { name: string; date: string }>();
  for (const r of (msRes.results ?? []) as MsRow[]) {
    milestones.set(r.job_id, { name: r.name, date: r.next_date });
  }
  for (const r of (aggRes.results ?? []) as AggRow[]) {
    out.set(r.job_id, {
      task_count: r.task_count,
      // weightedPercent parity: floored; the D1 CHECK bounds percent_done 0–100 so no clamp.
      percent: r.weight_total === 0 ? 0 : Math.floor(r.weight_done / r.weight_total),
      late_count: r.late_count,
      next_milestone: milestones.get(r.job_id) ?? null,
      today,
    });
  }
  return out;
}
