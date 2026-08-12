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

import type { ExpectedMaterialRow } from "./fieldops_expected_materials";

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
