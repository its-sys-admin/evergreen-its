/**
 * expected_materials section rendering — the DEEP-LINK CARD (2026-08-11).
 *
 * THIRD CONTRACT for this section, each an operator decision, each rewrite deliberate:
 *   v5/v6 — placeholder, no values, per-line list with one-tap Confirm receipt;
 *   v7 (#45) — the same list + the day's values snapshot filed into the submission;
 *   2026-08-11 — the first day real BOMs put hundreds of lines behind this section, the
 *   inline list was cut entirely: the daily form shows the day's SHAPE (counts) and
 *   deep-links the Materials page, which owns every action (the two-tap three-way mark,
 *   Report-a-problem, resolve). No values are seeded — a NEW filing's PDF renders the
 *   classic note line; ALREADY-FILED v7 snapshots still render as tables (form_pdf keeps
 *   both paths; tests/test_form_pdf.py pins them).
 *
 * What this file asserts now:
 *   • no adapter → the section renders NOTHING — the generic fill page and every other
 *     form are unaffected; and it contributes NO initialValues key;
 *   • adapter with zero rows → the explicit empty state;
 *   • the count summary reads lines / still-expected / flagged from the rows;
 *   • the "Materials tracking →" deep link fires the adapter callback; absent callback →
 *     no button (mountable anywhere);
 *   • NO per-line content and NO per-line actions render, whatever the rows carry;
 *   • the live material-incident "Filed ✓" indicator rides FormLinkAdapter.filedLabel
 *     ('material-incident' is a DAILY_STATUS_FAMILIES member since M2).
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FormRenderer, initialValues, type ExpectedMaterialsAdapter } from "../FormRenderer";
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

function mount(adapter?: Partial<ExpectedMaterialsAdapter>) {
  const values = initialValues(DEF);
  return render(
    <FormRenderer
      def={DEF}
      values={values}
      setValues={vi.fn()}
      expectedMaterials={
        adapter === undefined ? undefined : ({ rows: ROWS, ...adapter } as ExpectedMaterialsAdapter)
      }
    />,
  );
}

describe("expected_materials — the deep-link card", () => {
  it("renders NOTHING without an adapter, and contributes no initialValues key", () => {
    const { container } = mount(undefined);
    expect(container.querySelector(".fr__expected-materials")).toBeNull();
    // No values key: a new filing carries no materials snapshot — the PDF's absent-key
    // path renders the classic note line (pinned Python-side in tests/test_form_pdf.py).
    expect("expected_materials_receipt" in initialValues(DEF)).toBe(false);
  });

  it("summarises the day's shape — lines, still expected, flagged — with no per-line content", () => {
    const { container } = mount({});
    const text = container.textContent ?? "";
    expect(text).toContain("3 lines on this job's list");
    expect(text).toContain("1 still expected");
    expect(text).toContain("1 flagged");
    // The list is GONE — none of the rows' own content renders here anymore.
    expect(text).not.toContain("Q.PEAK DUO");
    expect(text).not.toContain("Rebar bundles");
    expect(text).not.toContain("crushed corner");
    // And so are the old per-line actions — marking lives on the Materials page.
    expect(text).not.toContain("Confirm receipt");
    expect(text).not.toContain("Report a problem");
  });

  it("zero rows → the explicit empty state", () => {
    const { container } = render(
      <FormRenderer
        def={DEF}
        values={initialValues(DEF)}
        setValues={vi.fn()}
        expectedMaterials={{ rows: [] }}
      />,
    );
    expect(container.textContent ?? "").toContain("No expected materials for this job.");
  });

  it("the deep link fires the adapter callback; without one, no button renders", () => {
    const onOpenMaterials = vi.fn();
    const { getByText } = mount({ onOpenMaterials });
    fireEvent.click(getByText("Materials tracking →"));
    expect(onOpenMaterials).toHaveBeenCalledTimes(1);

    cleanup();
    const { queryByText } = mount({});
    expect(queryByText("Materials tracking →")).toBeNull();
  });

  it("says plainly that only the Materials page moves the numbers (the decoy guard)", () => {
    // The daily report carries a free-text "Deliveries Received" table that files with the
    // submission and prints, but never reaches the receipt ledger, the material list or the
    // client weekly report — a superintendent filling it in reasonably believes he has
    // recorded a delivery (forensic report 2026-08-24, defect D3). The card above it has to
    // say which one counts. Pinned so the sentence cannot quietly drop out again.
    const { getByText, container } = mount({ onOpenMaterials: vi.fn() });
    expect(getByText("Materials tracking →")).toBeTruthy();
    const text = (container.textContent ?? "").replace(/\s+/g, " ");
    expect(text).toContain("it is the only place that updates the material list");
    expect(text).toContain("a written note for the report");
  });

  it("the live material-incident Filed ✓ indicator rides FormLinkAdapter.filedLabel", () => {
    expect(DAILY_STATUS_FAMILIES).toContain("material-incident");
    const values = initialValues(DEF);
    const { container } = render(
      <FormRenderer
        def={DEF}
        values={values}
        setValues={vi.fn()}
        expectedMaterials={{ rows: ROWS }}
        formLinks={{
          open: vi.fn(),
          filedLabel: (code: string) => (code === "material-incident" ? "Filed ✓ 14:02" : null),
        }}
      />,
    );
    expect(container.textContent ?? "").toContain("Material incident report: Filed ✓ 14:02");
  });
});
