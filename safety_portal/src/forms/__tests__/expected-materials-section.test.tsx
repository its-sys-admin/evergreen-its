/**
 * expected_materials section rendering + receipt actions (Material receipts M2).
 *
 * The section is a PLACEHOLDER in the definition (daily-report-v5); the content is the
 * `expectedMaterials` ADAPTER the HOST supplies (the Daily tab: the job's M1 rows + the two
 * receipt actions + per-row busy + action error). This file asserts the M2 render contract:
 *   • no adapter → the section renders NOTHING — the generic fill page and every other form
 *     are unaffected; and it contributes NO initialValues key (it files no values of its own);
 *   • adapter with zero rows → the explicit empty state;
 *   • a PENDING (status 'expected') row renders description/qty/unit/expected-date + the two
 *     actions ("Confirm receipt" / "Report a problem →"), wired to the adapter callbacks and
 *     disabled while the row is busy;
 *   • RECEIVED / INCIDENT rows render status pills + the received-by/at record line, with NO
 *     action buttons;
 *   • actionError renders inline (never silent);
 *   • the live material-incident "Filed ✓" indicator rides FormLinkAdapter.filedLabel
 *     ('material-incident' is a DAILY_STATUS_FAMILIES member since M2).
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FormRenderer,
  initialValues,
  seedExpectedMaterialsSnapshot,
  type ExpectedMaterialsAdapter,
} from "../FormRenderer";
import { formCatalog, getDefinition } from "../registry";
import { DAILY_STATUS_FAMILIES } from "../../lib/fieldops_daily_form";
import type { ExpectedMaterialRow } from "../../lib/fieldops_expected_materials";
import type { FormDefinition } from "../types";

afterEach(cleanup);

// Resolve the CURRENT daily report from the catalog rather than naming a version. The eager
// registry window is "current + immediately-previous" (vite-plugin-eager-forms), so a
// hard-coded code silently becomes null two cuts later — which is exactly what v7 did to the
// v5 references here. These tests are about renderer behaviour, not a historical version.
const DEF = getDefinition(
  formCatalog().find((p) => p.parent_form_code === "daily-report")!.form_code!,
) as FormDefinition;

const ROWS: ExpectedMaterialRow[] = [
  {
    id: 1, material_id: 7, material_name: "Q.PEAK DUO", description: null,
    qty: 40, unit: "panels", expected_date: "2026-07-10", status: "expected",
    received_at: null, received_by_name: null, qty_received: null, note: null, seq: 10, line_uuid: "lu-1", part_number: null, category: null, expected_ship_date: null, receipt_status: null, qty_received_total: null,
  },
  {
    id: 2, material_id: null, material_name: null, description: "Rebar bundles",
    qty: 12, unit: "pallets", expected_date: null, status: "received",
    received_at: 1_700_000_000, received_by_name: "Mo Manager", qty_received: 12, note: null, seq: 20, line_uuid: "lu-2", part_number: null, category: null, expected_ship_date: null, receipt_status: null, qty_received_total: null,
  },
  {
    id: 3, material_id: null, material_name: null, description: "Crate of clamps",
    qty: null, unit: null, expected_date: null, status: "incident",
    received_at: 1_700_000_100, received_by_name: null, qty_received: null, note: "crushed corner", seq: 30, line_uuid: "lu-3", part_number: null, category: null, expected_ship_date: null, receipt_status: null, qty_received_total: null,
  },
];

function adapter(overrides: Partial<ExpectedMaterialsAdapter> = {}): ExpectedMaterialsAdapter {
  return {
    rows: ROWS,
    busyIds: new Set<number>(),
    onConfirmReceipt: vi.fn(),
    onReportProblem: vi.fn(),
    ...overrides,
  };
}

describe("daily-report-v5 carries the receipt mount", () => {
  it("is bundled with ONE expected_materials section keyed expected_materials_receipt, in the D.13 region", () => {
    expect(DEF).not.toBeNull();
    const mounts = DEF.sections.filter((s) => s.type === "expected_materials");
    expect(mounts).toHaveLength(1);
    expect(mounts[0]).toMatchObject({ key: "expected_materials_receipt", title: "Expected materials" });
    // Right after the deliveries guidance, immediately before the Deliveries Received table.
    const idx = DEF.sections.findIndex((s) => s.type === "expected_materials");
    expect(DEF.sections[idx - 1]).toMatchObject({
      type: "guidance",
      heading: "13. Material & Equipment Deliveries",
    });
    expect(DEF.sections[idx + 1]).toMatchObject({ type: "repeating_table", key: "deliveries_received" });
  });

  it("contributes NO initialValues key (the HOST seeds it when the rows load)", () => {
    expect("expected_materials_receipt" in initialValues(DEF)).toBe(false);
  });

  it("material-incident is a DAILY_STATUS_FAMILIES member (the SPA mirror of the Worker list)", () => {
    expect(DAILY_STATUS_FAMILIES).toContain("material-incident");
  });
});

describe("empty states", () => {
  it("without the adapter (the generic fill page) the section renders NOTHING", () => {
    const { container } = render(
      <FormRenderer def={DEF} values={initialValues(DEF)} setValues={() => {}} />,
    );
    expect(container.querySelector(".fr__expected-materials")).toBeNull();
  });

  it("with zero rows renders the explicit empty copy", () => {
    const { container } = render(
      <FormRenderer
        def={DEF}
        values={initialValues(DEF)}
        setValues={() => {}}
        expectedMaterials={adapter({ rows: [] })}
      />,
    );
    expect(container.textContent ?? "").toContain("No expected materials for this job.");
  });
});

describe("row states + receipt actions", () => {
  it("a PENDING row renders description/qty/unit/date and fires the adapter actions", () => {
    const onConfirmReceipt = vi.fn();
    const onReportProblem = vi.fn();
    const { container, getByLabelText } = render(
      <FormRenderer
        def={DEF}
        values={initialValues(DEF)}
        setValues={() => {}}
        expectedMaterials={adapter({ onConfirmReceipt, onReportProblem })}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Q.PEAK DUO");
    expect(text).toContain("40 panels");
    expect(text).toContain("expected 2026-07-10");
    fireEvent.click(getByLabelText("Confirm receipt of Q.PEAK DUO"));
    expect(onConfirmReceipt).toHaveBeenCalledWith(ROWS[0]);
    fireEvent.click(getByLabelText("Report a problem with Q.PEAK DUO"));
    expect(onReportProblem).toHaveBeenCalledWith(ROWS[0]);
  });

  it("a busy row's actions are disabled (per-row busy)", () => {
    const { getByLabelText } = render(
      <FormRenderer
        def={DEF}
        values={initialValues(DEF)}
        setValues={() => {}}
        expectedMaterials={adapter({ busyIds: new Set([1]) })}
      />,
    );
    expect((getByLabelText("Confirm receipt of Q.PEAK DUO") as HTMLButtonElement).disabled).toBe(true);
    expect((getByLabelText("Report a problem with Q.PEAK DUO") as HTMLButtonElement).disabled).toBe(true);
  });

  it("RECEIVED / INCIDENT rows render pills + the record line, with NO action buttons", () => {
    const { container, queryByLabelText } = render(
      <FormRenderer
        def={DEF}
        values={initialValues(DEF)}
        setValues={() => {}}
        expectedMaterials={adapter()}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Rebar bundles");
    expect(text).toContain("by Mo Manager");
    expect(text).toContain("qty received 12");
    expect(text).toContain("Crate of clamps");
    expect(text).toContain("crushed corner");
    const pills = Array.from(container.querySelectorAll(".fr__expected-materials .dash-pill")).map(
      (el) => el.textContent,
    );
    expect(pills).toEqual(["Expected", "Received", "Incident"]);
    expect(queryByLabelText("Confirm receipt of Rebar bundles")).toBeNull();
    expect(queryByLabelText("Report a problem with Crate of clamps")).toBeNull();
  });

  it("actionError renders inline as an alert (never silent)", () => {
    const { container } = render(
      <FormRenderer
        def={DEF}
        values={initialValues(DEF)}
        setValues={() => {}}
        expectedMaterials={adapter({ actionError: "Couldn't confirm receipt — try again." })}
      />,
    );
    const alert = container.querySelector(".fr__expected-materials [role='alert']");
    expect(alert?.textContent).toContain("Couldn't confirm receipt — try again.");
  });

  it("the material-incident Filed ✓ indicator rides FormLinkAdapter.filedLabel", () => {
    const filedLabel = vi.fn((code: string) =>
      code === "material-incident" ? "Filed ✓ 2:14 PM by Mo Manager" : null,
    );
    const { container } = render(
      <FormRenderer
        def={DEF}
        values={initialValues(DEF)}
        setValues={() => {}}
        formLinks={{ open: vi.fn(), filedLabel }}
        expectedMaterials={adapter()}
      />,
    );
    expect(filedLabel).toHaveBeenCalledWith("material-incident");
    expect(container.textContent ?? "").toContain("Material incident report: Filed ✓ 2:14 PM by Mo Manager");
  });
});

describe("seedExpectedMaterialsSnapshot (the v7 day snapshot)", () => {
  const BASE: ExpectedMaterialRow = {
    id: 1, material_id: null, material_name: null, description: "Ground screws",
    qty: null, unit: null, expected_date: null, status: "expected",
    received_at: null, received_by_name: null, qty_received: null, note: null, seq: 1,
    line_uuid: "u-1", part_number: null, category: null, expected_ship_date: null,
    receipt_status: null, qty_received_total: null,
  };

  it("prefers the catalog name, falls back to the free-text description", () => {
    expect(seedExpectedMaterialsSnapshot([{ ...BASE, material_name: "Q.PEAK DUO" }])[0].material)
      .toBe("Q.PEAK DUO");
    expect(seedExpectedMaterialsSnapshot([BASE])[0].material).toBe("Ground screws");
  });

  it("the 0059 receipt_status rollup WINS over the coarse legacy status", () => {
    // receipt_status is the real three-way delivery state; status is the coarse projection.
    const row = { ...BASE, status: "received" as const, receipt_status: "partial" as const };
    expect(seedExpectedMaterialsSnapshot([row])[0].status).toBe("Partially delivered");
  });

  it("falls back to the coarse status, and `incident` is reported (it is sticky and orthogonal)", () => {
    expect(seedExpectedMaterialsSnapshot([{ ...BASE, status: "incident" }])[0].status)
      .toBe("Problem reported");
    expect(seedExpectedMaterialsSnapshot([{ ...BASE, status: "received" }])[0].status)
      .toBe("Received");
    expect(seedExpectedMaterialsSnapshot([BASE])[0].status).toBe("Expected");
  });

  it("renders qty with its unit, and a null running total as blank (NOT '0')", () => {
    // null means nothing has been quantified, which is deliberately distinct from a recorded 0.
    const row = { ...BASE, qty: 120, unit: "panels", qty_received_total: null };
    expect(seedExpectedMaterialsSnapshot([row])[0].expected).toBe("120 panels");
    expect(seedExpectedMaterialsSnapshot([row])[0].received).toBe("");
    expect(seedExpectedMaterialsSnapshot([{ ...row, qty_received_total: 0 }])[0].received).toBe("0");
  });

  it("carries only display strings — no ids, so a filed PDF never depends on a live join", () => {
    const entry = seedExpectedMaterialsSnapshot([{ ...BASE, part_number: "7000153" }])[0];
    expect(Object.keys(entry).sort()).toEqual(
      ["expected", "material", "part_number", "received", "status"],
    );
    expect(Object.values(entry).every((v) => typeof v === "string")).toBe(true);
  });
});
