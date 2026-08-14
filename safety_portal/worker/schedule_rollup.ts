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

// ── A6: the cross-job portfolio strip ───────────────────────────────────────────
// One batch over ACTIVE jobs: the A2 aggregate extended with the delivery/milestone windows,
// plus the expected-materials due leg. The predicates live HERE, beside the per-job aggregate,
// so the strip and the job cards can never disagree; the A6 parity test additionally pins the
// late rule against the weekly report's behind list. Windows: deliveries "due this week"
// includes overdue-undelivered (the ADR-0006 decision-12 delivery-slip predicate folds in);
// milestones "at risk" = unreached and due inside RISK_DAYS or already past. Both horizons are
// recommended defaults flagged for operator confirmation in the A6 PR.
export const PORTFOLIO_WEEK_DAYS = 7;
export const PORTFOLIO_RISK_DAYS = 14;

export interface PortfolioJobRow {
  job_id: string;
  project_name: string;
  task_count: number;
  percent: number;
  late_count: number;
  deliveries_due: number;
  materials_due: number;
  milestones_at_risk: number;
  next_milestone: { name: string; date: string } | null;
}

function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** Portfolio rollup across every ACTIVE job (status='active' — the tracker's default filter).
 *  Only jobs with ≥1 signal (a schedule or materials due) return. No stored state. */
export async function portfolioRollup(
  db: D1Database,
  today: string,
): Promise<{ jobs: PortfolioJobRow[]; today: string; week_end: string }> {
  const weekEnd = addDays(today, PORTFOLIO_WEEK_DAYS - 1);
  const riskEnd = addDays(today, PORTFOLIO_RISK_DAYS);
  const [aggRes, msRes, matRes] = await db.batch([
    db.prepare(
      `SELECT t.job_id, j.project_name,
              COUNT(*) AS task_count,
              SUM(${WEIGHT}) AS weight_total,
              SUM((${WEIGHT}) * t.percent_done) AS weight_done,
              SUM(CASE WHEN t.finish_date IS NOT NULL AND t.finish_date < ?1 AND t.percent_done < 100
                       THEN 1 ELSE 0 END) AS late_count,
              SUM(CASE WHEN t.is_delivery = 1 AND t.delivered_date IS NULL
                        AND t.finish_date IS NOT NULL AND t.finish_date <= ?2
                       THEN 1 ELSE 0 END) AS deliveries_due,
              SUM(CASE WHEN t.is_milestone = 1 AND t.percent_done < 100
                        AND COALESCE(t.finish_date, t.start_date) IS NOT NULL
                        AND COALESCE(t.finish_date, t.start_date) <= ?3
                       THEN 1 ELSE 0 END) AS milestones_at_risk
       FROM job_schedule_tasks t
       JOIN jobs j ON j.job_id = t.job_id AND j.status = 'active'
       WHERE t.active = 1
       GROUP BY t.job_id`,
    ).bind(today, weekEnd, riskEnd),
    db.prepare(
      `SELECT job_id, name, next_date FROM (
         SELECT t.job_id, t.name, COALESCE(t.finish_date, t.start_date) AS next_date,
                ROW_NUMBER() OVER (PARTITION BY t.job_id
                                   ORDER BY COALESCE(t.finish_date, t.start_date) ASC, t.id ASC) AS rn
         FROM job_schedule_tasks t
         JOIN jobs j ON j.job_id = t.job_id AND j.status = 'active'
         WHERE t.active = 1 AND t.is_milestone = 1 AND t.percent_done < 100
           AND COALESCE(t.finish_date, t.start_date) >= ?1
       ) WHERE rn = 1`,
    ).bind(today),
    db.prepare(
      // Mirrors the expected-materials read's active-line filter: an expected line with a date
      // inside the window that has not been fully received.
      `SELECT m.job_id, j.project_name, COUNT(*) AS n
       FROM job_expected_materials m
       JOIN jobs j ON j.job_id = m.job_id AND j.status = 'active'
       WHERE m.active = 1 AND m.status != 'received'
         AND m.expected_date IS NOT NULL AND m.expected_date >= ?1 AND m.expected_date <= ?2
       GROUP BY m.job_id`,
    ).bind(today, weekEnd),
  ]);
  const milestones = new Map<string, { name: string; date: string }>();
  for (const r of (msRes.results ?? []) as { job_id: string; name: string; next_date: string }[]) {
    milestones.set(r.job_id, { name: r.name, date: r.next_date });
  }
  const byJob = new Map<string, PortfolioJobRow>();
  for (const r of (aggRes.results ?? []) as Record<string, unknown>[]) {
    const weightTotal = Number(r.weight_total ?? 0);
    byJob.set(String(r.job_id), {
      job_id: String(r.job_id),
      project_name: String(r.project_name ?? ""),
      task_count: Number(r.task_count ?? 0),
      percent: weightTotal === 0 ? 0 : Math.floor(Number(r.weight_done ?? 0) / weightTotal),
      late_count: Number(r.late_count ?? 0),
      deliveries_due: Number(r.deliveries_due ?? 0),
      materials_due: 0,
      milestones_at_risk: Number(r.milestones_at_risk ?? 0),
      next_milestone: milestones.get(String(r.job_id)) ?? null,
    });
  }
  for (const r of (matRes.results ?? []) as { job_id: string; project_name: string; n: number }[]) {
    const row = byJob.get(r.job_id);
    if (row) row.materials_due = r.n;
    else byJob.set(r.job_id, {
      job_id: r.job_id, project_name: r.project_name, task_count: 0, percent: 0,
      late_count: 0, deliveries_due: 0, materials_due: r.n, milestones_at_risk: 0,
      next_milestone: null,
    });
  }
  const jobs = [...byJob.values()]
    .filter((r) => r.task_count > 0 || r.materials_due > 0)
    .sort((a, b) => b.late_count - a.late_count || b.deliveries_due - a.deliveries_due || a.project_name.localeCompare(b.project_name));
  return { jobs, today, week_end: weekEnd };
}

