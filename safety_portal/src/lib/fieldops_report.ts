// Weekly Production Report API client (0067). Same-origin cookie fetch; no auth header.
// READ + SAVE both gated cap.jobtracker.manage (the Worker re-gates every call — the SPA hiding
// a control is never the boundary).
//
// The GET returns the SAME payload the Mac compile reads, so the screen shows exactly what the
// report will render. Wire shapes are single-sourced in worker/wire-types.ts and re-exported here
// so a drift between the two sides fails the typecheck rather than surprising a client.

import type {
  ProductionReportResponse,
  WeeklyReportSaveBody,
} from "../../worker/wire-types";

export type {
  ProductionReportResponse,
  WeeklyReportCrew,
  WeeklyReportLaborRow,
  WeeklyReportOffice,
  WeeklyReportPhoto,
  WeeklyReportSafetyCount,
  WeeklyReportSaveBody,
  WeeklyReportWeatherDay,
} from "../../worker/wire-types";

/** The six OSHA case rows, in the order the printed report lays them out. */
export const SAFETY_ROWS: { key: string; label: string }[] = [
  { key: "lost_time", label: "Lost Time Accident Cases" },
  { key: "lost_work_days", label: "Lost Work Days" },
  { key: "job_transfer", label: "Job Transfer or Restriction" },
  { key: "near_miss", label: "Near Misses" },
  { key: "other_recordable", label: "Other Recordable Cases" },
  { key: "first_aid", label: "First Aid Cases" },
];

/** Saturday of the week containing `d` — the Sat→Fri week the whole progress lane keys on
 *  (`shared/safety_week.py` is the Python twin; both must agree or the screen would edit a
 *  different week than the compile reads). */
export function weekStartFor(d: Date): string {
  const copy = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  // JS: 0=Sun … 6=Sat. Days since the most recent Saturday.
  const back = (copy.getUTCDay() + 1) % 7;
  copy.setUTCDate(copy.getUTCDate() - back);
  return copy.toISOString().slice(0, 10);
}

export function weekEndFor(weekStart: string): string {
  const d = new Date(`${weekStart}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 6);
  return d.toISOString().slice(0, 10);
}

/** The epoch window the labor legs bound on. Pacific midnight, matching the daemon's
 *  `_week_epoch_window` — the workstream's calendar week, not UTC's. */
function epochWindow(weekStart: string): { from: number; to: number } {
  // Pacific is UTC-7 (PDT) / UTC-8 (PST). The Worker only uses these to bound `time_entries`,
  // and a one-hour DST edge cannot move an entry across a week boundary in practice; the daemon
  // remains the authority for the compile itself.
  const from = Math.floor(new Date(`${weekStart}T07:00:00Z`).getTime() / 1000);
  return { from, to: from + 7 * 86400 };
}

export async function fetchWeeklyReport(
  jobId: string,
  weekStart: string,
): Promise<ProductionReportResponse> {
  const { from, to } = epochWindow(weekStart);
  const qs = new URLSearchParams({
    job_id: jobId,
    week_start: weekStart,
    week_end: weekEndFor(weekStart),
    from: String(from),
    to: String(to),
  });
  const res = await fetch(`/api/fieldops/weekly-report?${qs}`, { credentials: "same-origin" });
  if (!res.ok) {
    const err = (await res.json().catch(() => ({}))) as { error?: string };
    throw new Error(err.error ?? `Could not load the weekly report (${res.status}).`);
  }
  return (await res.json()) as ProductionReportResponse;
}

export async function saveWeeklyReport(body: WeeklyReportSaveBody): Promise<void> {
  const res = await fetch("/api/fieldops/weekly-report", {
    method: "PUT",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = (await res.json().catch(() => ({}))) as { error?: string };
    throw new Error(err.error ?? `Save failed (${res.status}).`);
  }
}
