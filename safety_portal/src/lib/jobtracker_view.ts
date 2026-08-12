// Pure view helpers for the job-tracker surfaces.
//
// WHY THIS EXISTS. The detail view's hero figures and status pills were inlined in
// FieldOpsJobTracker.tsx, which meant the two rules that carry real arithmetic — a VOIDED
// time entry counts as zero, a nullable `hours` (an open clock-in) counts as zero — lived
// only inside a 2,000-line component with no direct test. The house pattern
// (schedule_view.ts, materials_view.ts) is that every derivation a page renders has one
// pure definition and a test. Moved verbatim, not rewritten.
//
// The parameter types are structural minimums rather than the full api.* rows so the rules
// are testable without manufacturing whole JobDetail payloads.

/** "—" for absent hours (an open clock-in has none yet); two decimals otherwise. */
export function fmtHours(hours: number | null): string {
  if (hours == null || isNaN(hours)) return "—";
  return hours.toFixed(2);
}

/** Job status → pill class. Colour keys off the two-state open/closed `status` signal
 *  (the pill TEXT shows lifecycle — see the call sites). */
export function jobPillClass(s: string): string {
  if (s === "active") return "dash-pill dash-pill--ok";
  if (s === "on_hold") return "dash-pill dash-pill--warn";
  return "dash-pill"; // closed (and anything else)
}

/** Task status → pill class. */
export function taskPillClass(s: string): string {
  if (s === "in_progress") return "dash-pill dash-pill--warn";
  if (s === "done") return "dash-pill dash-pill--ok";
  return "dash-pill"; // open
}

/** Tasks not yet done — the hero's one number that asks somebody to do something.
 *  Counts what is ON SCREEN: the tasks leg is paged, so this is honest only as a
 *  "shown" figure, never a lifetime total (see the hero comment at the call site). */
export function openTaskCount(tasks: readonly { status: string }[]): number {
  return tasks.filter((t) => t.status !== "done").length;
}

/** Sum of logged hours across the loaded time entries. A VOIDED entry is a correction to
 *  zero, so excluding it is the same arithmetic the strike-through already shows. `hours`
 *  is nullable (an open clock-in has none yet) and counts as zero until quantified. */
export function loggedHours(entries: readonly { voided: boolean; hours: number | null }[]): number {
  return entries.reduce((sum, e) => sum + (e.voided ? 0 : (e.hours ?? 0)), 0);
}
