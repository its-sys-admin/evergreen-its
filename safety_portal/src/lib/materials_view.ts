// Pure view helpers for the materials surfaces.
//
// WHY THIS EXISTS. The lane tracks an expected quantity and a received quantity and never
// subtracts them. The per-job page renders "Expected 120 ea · received 90" as two adjacent
// numbers; nothing in the SPA, the Worker, or the Smartsheet mirror computes a remainder,
// flags a short, or carries an "outstanding" concept. So the question a superintendent
// actually asks — WHAT IS STILL OWED TO THIS JOB — could not be answered by the page whose
// whole subject is that question.
//
// Every field needed was already on the wire (`qty`, `qty_received_total`,
// `receipt_status`), so this is derivation, not new data. It is pure and lives here so the
// rule has one definition and a test, rather than being inlined twice.

import type { ExpectedMaterialRow, MaterialReceiptKind } from "./fieldops_expected_materials";

export interface LineOwed {
  /** null when the line carries no expected quantity — absent data, not a zero. */
  outstanding: number | null;
  /** More arrived than was expected. Worth surfacing: it usually means a mis-count or a
   *  duplicate load, and it is invisible in a bare "received" figure. */
  over: boolean;
  /** The line is settled: everything expected has arrived (or more). */
  settled: boolean;
}

/**
 * What a single line still owes.
 *
 * `qty_received_total` is null until something is quantified, which is DELIBERATELY
 * distinct from a recorded zero — so a line marked `not_delivered` with no quantity still
 * owes its full expected amount, while a line marked with an explicit 0 owes the same but
 * has been looked at. Neither is treated as "received nothing recorded yet".
 */
export function lineOwed(r: ExpectedMaterialRow): LineOwed {
  if (r.qty === null) return { outstanding: null, over: false, settled: false };
  const got = r.qty_received_total ?? 0;
  const diff = r.qty - got;
  return {
    outstanding: diff > 0 ? diff : 0,
    over: diff < 0,
    settled: diff <= 0,
  };
}

export interface MaterialsRollup {
  lines: number;
  settled: number;
  /** Lines still owing something. The number the page exists to report. */
  short: number;
  /** Lines whose latest mark was `not_delivered`. */
  notDelivered: number;
  /** Lines carrying the orthogonal incident flag. */
  incidents: number;
  /** Lines that arrived over their expected quantity. */
  over: number;
  /** True when at least one line has no expected quantity, so `short` cannot be complete —
   *  the caller says so rather than implying a total it cannot stand behind. */
  unquantified: boolean;
}

export function rollupMaterials(rows: ExpectedMaterialRow[]): MaterialsRollup {
  let settled = 0, short = 0, notDelivered = 0, incidents = 0, over = 0, unquantified = false;
  for (const r of rows) {
    const owed = lineOwed(r);
    if (owed.outstanding === null) unquantified = true;
    else if (owed.settled) settled += 1;
    else short += 1;
    if (owed.over) over += 1;
    if (r.receipt_status === "not_delivered") notDelivered += 1;
    if (r.status === "incident") incidents += 1;
  }
  return { lines: rows.length, settled, short, notDelivered, incidents, over, unquantified };
}

/** The per-line chip copy. Says the UNIT, because "12 outstanding" of an unstated thing is
 *  not actionable on a job site. */
export function owedLabel(r: ExpectedMaterialRow): string | null {
  const owed = lineOwed(r);
  if (owed.outstanding === null) return null;
  const unit = r.unit ? ` ${r.unit}` : "";
  if (owed.over) return `${Math.abs(r.qty! - (r.qty_received_total ?? 0))}${unit} over`;
  if (owed.settled) return "Settled";
  return `${owed.outstanding}${unit} outstanding`;
}

/**
 * D9 (receipt-event VOID, 2026-10-21): must the page ask before recording this mark?
 *
 * WHY. The receipt-event void exists because a second "Delivered" on a line that already had
 * everything it was owed is easy to record — a double tap, two people marking the same truck —
 * and until 0080 it could never be taken back. Voiding cleans it up afterwards; this stops most
 * of them being made. A settled line can legitimately take another delivery (a replacement
 * pallet, a line under-ordered in the BOM), so this ASKS, it never refuses.
 *
 * Only delivered and partial add to the received total; not_delivered records that nothing came,
 * so asking "record ANOTHER delivery?" before it would be a question about the wrong thing. A line
 * with no expected quantity is never settled (lineOwed), and the explicit `qty` check keeps that
 * true even if lineOwed's null rule ever changes. ONE rule for every mark surface — call this
 * rather than re-deriving it.
 */
export function needsSettledConfirm(r: ExpectedMaterialRow, kind: MaterialReceiptKind): boolean {
  if (kind === "not_delivered") return false;
  return r.qty !== null && lineOwed(r).settled;
}

/** The D9 question, in the line's own numbers and unit — "already has 40 of 40 ea received" is
 *  something the person holding the delivery ticket can check; "this line is settled" is not.
 *  `action` names the act being confirmed: the correction form asks "Record the correction
 *  anyway?", because "another delivery" would describe something the person is not doing. */
export function settledConfirmText(r: ExpectedMaterialRow, action = "Record another delivery"): string {
  const unit = r.unit ? ` ${r.unit}` : "";
  return `This line already has ${r.qty_received_total ?? 0} of ${r.qty ?? 0}${unit} received. ` +
    `${action} anyway?`;
}

// ── Epoch instants in the crews' day ─────────────────────────────────────────────────────────────
// A mark's `event_date` is already a Pacific YYYY-MM-DD, but `created_at` and `voided_at` are
// epoch instants. Rendered with toLocaleDateString they printed "10/21/2026" beside "2026-10-19"
// in the same history row, in whatever zone the browser sat in. These put them on the same
// footing: Pacific, ISO-ordered, 24-hour. formatToParts rather than format() so no engine's
// separator or ordering choice can leak into the string.
const PACIFIC_PARTS = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  // h23, not hour12:false — under hour12:false some engines print midnight as "24".
  hourCycle: "h23",
});

function pacificParts(epochSeconds: number): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of PACIFIC_PARTS.formatToParts(new Date(epochSeconds * 1000))) out[p.type] = p.value;
  return out;
}

/** Epoch seconds → the Pacific calendar date as YYYY-MM-DD (the format event dates use). */
export function pacificDateOf(epochSeconds: number): string {
  const p = pacificParts(epochSeconds);
  return `${p.year}-${p.month}-${p.day}`;
}

/** Epoch seconds → Pacific wall-clock time as 24-hour HH:MM. */
export function pacificTimeOf(epochSeconds: number): string {
  const p = pacificParts(epochSeconds);
  return `${p.hour}:${p.minute}`;
}
