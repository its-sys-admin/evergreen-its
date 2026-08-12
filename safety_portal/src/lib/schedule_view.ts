// Pure view helpers for the per-job Schedule page (ADR-0006 PR-4/5/6).
//
// Everything here is a pure function of the task rows the Worker already sends. No fetch,
// no React, no Date.now() reached for implicitly — `today` is always passed in, so the
// page's "is this late" answer is testable and the timeline is deterministic.
//
// WHY THIS FILE EXISTS. `ScheduleTaskRow` carries 25 fields; the page rendered 10 of them
// and derived nothing. Three facts a construction schedule exists to answer were therefore
// invisible on the schedule page:
//
//   • LATE      — past its finish date and under 100%. Nothing said so.
//   • SLIP      — `baseline_*_date` is stamped at the task's first commit and never
//                 rewritten (the slip anchor). A revision that moved a finish date left no
//                 visible trace, even though the anchor was sitting right there in the row.
//   • ROLLUP    — a section's own progress, which is how a super reads a schedule.
//
// They are derived here rather than in the component so each one has a name, a rule, and a
// test. Percent aggregation is DURATION-WEIGHTED (a null duration counts as one day): a mean
// over task rows would let thirty one-day punch items outvote a hundred-day install, which
// is the kind of number that reads fine and lies.

import type { ScheduleTaskRow } from "./fieldops_schedules";

/** Today as the Pacific calendar date (YYYY-MM-DD; en-CA yields ISO order) — the crews' day,
 *  matching the Worker's own default for an omitted delivered_date. */
export function todayPacific(now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

/** `YYYY-MM-DD` → days since epoch, or null. UTC-anchored so no local timezone can shift a
 *  calendar date across a boundary (the dates are calendar days, never instants). */
export function dayNumber(date: string | null | undefined): number | null {
  if (!date || date.length < 10) return null;
  const y = Number(date.slice(0, 4));
  const m = Number(date.slice(5, 7));
  const d = Number(date.slice(8, 10));
  if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) return null;
  const ms = Date.UTC(y, m - 1, d);
  return Number.isFinite(ms) ? Math.floor(ms / 86_400_000) : null;
}

/** A task is LATE when its finish date has passed and it is not finished. Milestones and
 *  deliveries count the same way — an unreached milestone past its date is exactly what a
 *  super needs flagged. A task with no finish date can never be late: absent data is not
 *  evidence of a problem. */
export function isLate(t: ScheduleTaskRow, today: string): boolean {
  if (t.percent_done >= 100) return false;
  const fin = dayNumber(t.finish_date);
  const now = dayNumber(today);
  return fin !== null && now !== null && fin < now;
}

/** Days the current finish date has moved from the immutable baseline. Positive = later than
 *  promised. Null when either anchor is missing or nothing moved — the caller renders nothing
 *  rather than a reassuring "0d". */
export function slipDays(t: ScheduleTaskRow): number | null {
  const base = dayNumber(t.baseline_finish_date);
  const cur = dayNumber(t.finish_date);
  if (base === null || cur === null || base === cur) return null;
  return cur - base;
}

/** Duration in days for weighting. A null/zero duration weighs one day so that a milestone
 *  still counts for something and no task can weigh nothing. */
function weightOf(t: ScheduleTaskRow): number {
  const d = t.duration_days;
  return d !== null && d > 0 ? d : 1;
}

/** Duration-weighted mean percent, floored to a whole number. An empty set is 0, not NaN. */
export function weightedPercent(tasks: ScheduleTaskRow[]): number {
  if (tasks.length === 0) return 0;
  let num = 0;
  let den = 0;
  for (const t of tasks) {
    const w = weightOf(t);
    num += w * Math.max(0, Math.min(100, t.percent_done));
    den += w;
  }
  return den === 0 ? 0 : Math.floor(num / den);
}

export interface ScheduleStats {
  total: number;
  done: number;
  late: number;
  milestones: number;
  deliveries: number;
  percent: number;
  /** The soonest not-yet-reached milestone, for the hero line. */
  nextMilestone: { name: string; date: string } | null;
}

export function summarize(tasks: ScheduleTaskRow[], today: string): ScheduleStats {
  let done = 0;
  let late = 0;
  let milestones = 0;
  let deliveries = 0;
  let next: { name: string; date: string } | null = null;
  const nowNum = dayNumber(today);

  for (const t of tasks) {
    if (t.percent_done >= 100) done += 1;
    if (isLate(t, today)) late += 1;
    if (t.is_milestone) milestones += 1;
    if (t.is_delivery) deliveries += 1;

    // "Next" is the earliest unreached milestone at or after today — the date a super is
    // actually steering toward. A missed one is already counted as late above.
    if (t.is_milestone && t.percent_done < 100) {
      const d = dayNumber(t.finish_date ?? t.start_date);
      if (d !== null && nowNum !== null && d >= nowNum) {
        const cur = next ? dayNumber(next.date) : null;
        if (cur === null || d < cur) {
          next = { name: t.name, date: (t.finish_date ?? t.start_date) as string };
        }
      }
    }
  }

  return {
    total: tasks.length,
    done,
    late,
    milestones,
    deliveries,
    percent: weightedPercent(tasks),
    nextMilestone: next,
  };
}

export type ScheduleFilter = "all" | "open" | "late" | "milestones" | "deliveries";

/** Filter + search. Search matches the task name and its section, case-insensitively — a
 *  super types "trench" or "civil" and expects both to work. */
export function matchesFilter(
  t: ScheduleTaskRow,
  filter: ScheduleFilter,
  query: string,
  today: string,
): boolean {
  const q = query.trim().toLowerCase();
  if (q.length > 0) {
    const hay = `${t.name} ${t.section ?? ""}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  switch (filter) {
    case "open":
      return t.percent_done < 100;
    case "late":
      return isLate(t, today);
    case "milestones":
      return Boolean(t.is_milestone);
    case "deliveries":
      return Boolean(t.is_delivery);
    case "all":
    default:
      return true;
  }
}

export interface ScheduleGroup {
  name: string | null;
  tasks: ScheduleTaskRow[];
  percent: number;
  late: number;
}

/**
 * Group by section in DOCUMENT order (first-seen wins), never alphabetically — a schedule is
 * read top to bottom and alphabetizing would scramble the phases. Each group carries its own
 * rollup so a collapsed section still reports.
 */
export function groupTasks(tasks: ScheduleTaskRow[], today: string): ScheduleGroup[] {
  const out: ScheduleGroup[] = [];
  const byName = new Map<string, ScheduleGroup>();
  for (const t of tasks) {
    const key = t.section ?? "";
    let bucket = byName.get(key);
    if (!bucket) {
      bucket = { name: t.section, tasks: [], percent: 0, late: 0 };
      byName.set(key, bucket);
      out.push(bucket);
    }
    bucket.tasks.push(t);
  }
  for (const g of out) {
    g.percent = weightedPercent(g.tasks);
    g.late = g.tasks.filter((t) => isLate(t, today)).length;
  }
  return out;
}

// ── Timeline domain ────────────────────────────────────────────────────────────────────

export interface TimelineDomain {
  start: number;
  end: number;
  span: number;
  /** Month boundaries inside the domain, as {label, pct} for the ruler. */
  ticks: { label: string; pct: number }[];
  /** Today's position as a percentage, or null when today falls outside the job. */
  todayPct: number | null;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * The timeline's horizontal axis: the job's own date span, padded so a bar at either edge is
 * not flush against the frame. Returns null when no task carries a date — the caller shows an
 * honest empty state instead of an axis with nothing on it.
 */
export function timelineDomain(tasks: ScheduleTaskRow[], today: string): TimelineDomain | null {
  let min: number | null = null;
  let max: number | null = null;
  for (const t of tasks) {
    for (const d of [dayNumber(t.start_date), dayNumber(t.finish_date)]) {
      if (d === null) continue;
      if (min === null || d < min) min = d;
      if (max === null || d > max) max = d;
    }
  }
  if (min === null || max === null) return null;

  // A single-day job would divide by zero; give it a week of air.
  if (max - min < 7) max = min + 7;
  const pad = Math.max(2, Math.round((max - min) * 0.03));
  const start = min - pad;
  const end = max + pad;
  const span = end - start;

  const ticks: { label: string; pct: number }[] = [];
  const first = new Date(start * 86_400_000);
  const cursor = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), 1));
  // Step month by month across the domain. Bounded by the span, so a malformed date cannot
  // spin here.
  for (let guard = 0; guard < 400; guard += 1) {
    const dayNum = Math.floor(cursor.getTime() / 86_400_000);
    if (dayNum > end) break;
    if (dayNum >= start) {
      ticks.push({
        label: `${MONTHS[cursor.getUTCMonth()]} ${String(cursor.getUTCFullYear()).slice(2)}`,
        pct: ((dayNum - start) / span) * 100,
      });
    }
    cursor.setUTCMonth(cursor.getUTCMonth() + 1);
  }

  const nowNum = dayNumber(today);
  const todayPct =
    nowNum !== null && nowNum >= start && nowNum <= end ? ((nowNum - start) / span) * 100 : null;

  return { start, end, span, ticks, todayPct };
}

export interface BarGeometry {
  leftPct: number;
  widthPct: number;
  /** True when the task has no duration to draw — render it as a point, not a bar. */
  point: boolean;
}

/**
 * Where a task's bar sits on the axis. A milestone (or any task whose dates collapse to one
 * day) is a POINT — a schedule draws those as a diamond, not a hairline bar.
 * Returns null when the task carries no usable date.
 */
export function barGeometry(t: ScheduleTaskRow, domain: TimelineDomain): BarGeometry | null {
  const s = dayNumber(t.start_date);
  const f = dayNumber(t.finish_date);
  const from = s ?? f;
  const to = f ?? s;
  if (from === null || to === null) return null;

  const clampedFrom = Math.max(domain.start, Math.min(domain.end, from));
  // Finish dates are inclusive on a construction schedule: a Mon→Fri task occupies five days,
  // so the bar runs to the END of the finish date.
  const clampedTo = Math.max(domain.start, Math.min(domain.end, to + 1));
  const leftPct = ((clampedFrom - domain.start) / domain.span) * 100;
  const widthPct = Math.max(0, ((clampedTo - clampedFrom) / domain.span) * 100);

  return {
    leftPct,
    widthPct,
    point: Boolean(t.is_milestone) || to <= from,
  };
}

/** The 10-cell block-character bar. KEPT DELIBERATELY: it is the page's screen-reader and
 *  print rendering of progress, and it is pinned by test. The CSS meter beside it is the
 *  visual; this is the text that survives both a stylesheet and a screen reader. */
export function progressBar(percent: number): string {
  const filled = Math.max(0, Math.min(10, Math.floor(percent / 10)));
  return `${"█".repeat(filled)}${"░".repeat(10 - filled)} ${percent}%`;
}
