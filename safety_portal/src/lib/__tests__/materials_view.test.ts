/**
 * "What is still owed to this job" — the question the materials lane could not answer.
 *
 * The page rendered expected and received as two adjacent numbers and never subtracted
 * them. These pin the subtraction, and specifically the three cases a naive `qty - got`
 * gets wrong: an absent expected quantity is not a zero, an absent RECEIVED total is not
 * the same as a recorded zero, and more-than-expected is a real condition worth naming
 * rather than a negative outstanding.
 */
import { describe, expect, it } from "vitest";
import type { ExpectedMaterialRow } from "../fieldops_expected_materials";
import {
  lineOwed,
  needsSettledConfirm,
  owedLabel,
  rollupMaterials,
  settledConfirmText,
} from "../materials_view";

const row = (over: Partial<ExpectedMaterialRow> = {}): ExpectedMaterialRow => ({
  id: 1, material_id: null, material_name: null, description: "Torque tube", qty: 40,
  unit: "ea", expected_date: null, status: "expected", received_at: null,
  received_by_name: null, qty_received: null, note: null, seq: 1, line_uuid: "l1",
  part_number: null, category: null, expected_ship_date: null,
  receipt_status: null, qty_received_total: null, ...over,
});

describe("lineOwed", () => {
  it("owes the full quantity when nothing has been quantified", () => {
    expect(lineOwed(row())).toEqual({ outstanding: 40, over: false, settled: false });
  });

  it("subtracts what has arrived", () => {
    expect(lineOwed(row({ qty_received_total: 28 }))).toMatchObject({ outstanding: 12, settled: false });
  });

  it("settles at exactly the expected quantity", () => {
    expect(lineOwed(row({ qty_received_total: 40 }))).toEqual({ outstanding: 0, over: false, settled: true });
  });

  it("names an OVER delivery instead of reporting a negative outstanding", () => {
    const o = lineOwed(row({ qty_received_total: 44 }));
    expect(o.over).toBe(true);
    expect(o.outstanding).toBe(0); // never negative
    expect(o.settled).toBe(true);
  });

  it("returns null — not 0 — when the line carries no expected quantity", () => {
    // Absent data is not a settled line. Reporting 0 outstanding here would let an
    // unquantified line count as complete in the rollup.
    expect(lineOwed(row({ qty: null })).outstanding).toBeNull();
    expect(lineOwed(row({ qty: null })).settled).toBe(false);
  });

  it("treats a RECORDED zero the same as nothing received, but both still owe", () => {
    expect(lineOwed(row({ qty_received_total: 0 })).outstanding).toBe(40);
  });
});

describe("rollupMaterials", () => {
  it("counts settled, short, not-delivered, incidents and overs", () => {
    const r = rollupMaterials([
      row({ id: 1, qty_received_total: 40 }),
      row({ id: 2, qty_received_total: 28 }),
      row({ id: 3, qty_received_total: null, receipt_status: "not_delivered" }),
      row({ id: 4, qty_received_total: 44 }),
      row({ id: 5, qty_received_total: 10, status: "incident" }),
    ]);
    expect(r).toMatchObject({
      lines: 5, settled: 2, short: 3, notDelivered: 1, incidents: 1, over: 1, unquantified: false,
    });
  });

  it("flags that the short count is INCOMPLETE when a line has no expected quantity", () => {
    // Never let the page imply a total it cannot stand behind.
    const r = rollupMaterials([row({ qty: null }), row({ id: 2, qty_received_total: 40 })]);
    expect(r.unquantified).toBe(true);
    expect(r.short).toBe(0);
    expect(r.settled).toBe(1);
  });

  it("is all zeroes on an empty list, never NaN", () => {
    expect(rollupMaterials([])).toMatchObject({ lines: 0, settled: 0, short: 0 });
  });
});

describe("owedLabel", () => {
  it("says the unit, because a bare number is not actionable on a job site", () => {
    expect(owedLabel(row({ qty_received_total: 28 }))).toBe("12 ea outstanding");
    expect(owedLabel(row({ qty_received_total: 28, unit: null }))).toBe("12 outstanding");
  });

  it("reads Settled and names an over-delivery", () => {
    expect(owedLabel(row({ qty_received_total: 40 }))).toBe("Settled");
    expect(owedLabel(row({ qty_received_total: 44 }))).toBe("4 ea over");
  });

  it("renders nothing when there is no expected quantity to measure against", () => {
    expect(owedLabel(row({ qty: null }))).toBeNull();
  });
});

// D9 (receipt-event VOID, 2026-10-21). A second "Delivered" on a line that has already got
// everything it was owed is how the double-mark the void feature exists to clean up gets made
// in the first place. The page asks before recording it — this is the one rule for WHEN.
describe("needsSettledConfirm (D9)", () => {
  it("asks before a delivered or partial mark on a fully received line", () => {
    const settled = row({ qty_received_total: 40 });
    expect(needsSettledConfirm(settled, "delivered")).toBe(true);
    expect(needsSettledConfirm(settled, "partial")).toBe(true);
  });

  it("asks on an OVER-delivered line too — it is settled, and more is even less likely right", () => {
    expect(needsSettledConfirm(row({ qty_received_total: 44 }), "delivered")).toBe(true);
  });

  it("never asks before Not delivered — that mark adds nothing to the received total", () => {
    expect(needsSettledConfirm(row({ qty_received_total: 40 }), "not_delivered")).toBe(false);
  });

  it("never asks on a line that still owes something", () => {
    expect(needsSettledConfirm(row({ qty_received_total: 28 }), "delivered")).toBe(false);
    expect(needsSettledConfirm(row({ qty_received_total: null }), "partial")).toBe(false);
  });

  it("never asks on a line with no expected quantity — there is nothing to be settled against", () => {
    expect(needsSettledConfirm(row({ qty: null, qty_received_total: 40 }), "delivered")).toBe(false);
  });
});

describe("settledConfirmText (D9)", () => {
  it("states what is already in, against what was expected, in the line's unit", () => {
    expect(settledConfirmText(row({ qty_received_total: 40 }))).toBe(
      "This line already has 40 of 40 ea received. Record another delivery anyway?",
    );
  });

  it("drops the unit cleanly when the line has none, and reports an over-delivery as-is", () => {
    expect(settledConfirmText(row({ qty_received_total: 44, unit: null }))).toBe(
      "This line already has 44 of 40 received. Record another delivery anyway?",
    );
  });
});
