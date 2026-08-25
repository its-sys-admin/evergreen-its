/**
 * Expected-materials Job Tracker section (Material receipts M1).
 * cap.materials.manage → full CRUD (catalog-pick OR free-text add, inline edit on expected rows,
 * ▲/▼ seq reorder, ConfirmDelete deactivate); cap.materials.receive without manage → the READ-ONLY
 * list with status pills + the "receive arrives via the daily form (M2)" note; neither cap →
 * renders nothing. Never-silent: empty state, load-error-with-Retry, catalog-picker failure that
 * keeps the free-text path working. Mocks the two lib modules + useAuth (the FieldOpsJobTracker
 * test convention).
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_expected_materials", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_expected_materials")>();
  return {
    ...actual,
    fetchExpectedMaterials: vi.fn(),
    createExpectedMaterial: vi.fn(),
    updateExpectedMaterial: vi.fn(),
    setExpectedMaterialSeq: vi.fn(),
    deactivateExpectedMaterial: vi.fn(),
    receiveExpectedMaterial: vi.fn(),
    flagExpectedMaterialIncident: vi.fn(),
  };
});
vi.mock("../../lib/fieldops_materials", () => ({ fetchMaterials: vi.fn() }));
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as api from "../../lib/fieldops_expected_materials";
import { fetchMaterials, type CatalogRow } from "../../lib/fieldops_materials";
import { useAuth } from "../../lib/auth";
import { ExpectedMaterialsSection } from "../ExpectedMaterialsSection";

function authWith(capabilities: string[]) {
  return {
    user: capabilities.length ? { username: "u", role: "admin" as const, capabilities } : null,
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

const CATALOG: CatalogRow[] = [
  { id: 7, model_id: "Q.PEAK_DUO", manufacturer: "Qcells", category: "module", key_specs: null, unit_cost: null, source_files: null, active: 1 },
  { id: 8, model_id: "OMCO Tracker", manufacturer: "OMCO Solar", category: "tracker", key_specs: null, unit_cost: null, source_files: null, active: 1 },
];

const ROWS: api.ExpectedMaterialRow[] = [
  {
    id: 1, material_id: 7, material_name: "Q.PEAK_DUO", description: null, qty: 40, unit: "panels",
    expected_date: "2026-07-10", status: "expected", received_at: null, received_by_name: null,
    qty_received: null, note: null, seq: 10, line_uuid: "lu-1", part_number: null, category: null, expected_ship_date: null, receipt_status: null, qty_received_total: null,
  },
  {
    id: 2, material_id: null, material_name: null, description: "Rebar bundles", qty: 12, unit: "pallets",
    expected_date: null, status: "received", received_at: 1_700_000_000, received_by_name: "Mo Manager",
    qty_received: 12, note: null, seq: 20, line_uuid: "lu-2", part_number: null, category: null, expected_ship_date: null, receipt_status: null, qty_received_total: null,
  },
  {
    id: 3, material_id: null, material_name: null, description: "Crushed crate", qty: null, unit: null,
    expected_date: null, status: "incident", received_at: 1_700_000_100, received_by_name: null,
    qty_received: null, note: "damaged", seq: 30, line_uuid: "lu-3", part_number: null, category: null, expected_ship_date: null, receipt_status: null, qty_received_total: null,
  },
];

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(useAuth).mockReturnValue(authWith([]));
  vi.mocked(api.fetchExpectedMaterials).mockResolvedValue({ expected_materials: ROWS, shipments: [], receipt_events: [], project_name: "Deep Lake" });
  vi.mocked(fetchMaterials).mockResolvedValue({ materials: CATALOG, next_cursor: null });
});

async function renderSection(caps: string[]) {
  vi.mocked(useAuth).mockReturnValue(authWith(caps));
  const utils = render(<ExpectedMaterialsSection jobId="JOB-A" />);
  if (caps.length) await waitFor(() => expect(api.fetchExpectedMaterials).toHaveBeenCalledWith("JOB-A"));
  return utils;
}

describe("ExpectedMaterialsSection — gating", () => {
  it("renders NOTHING without a materials cap (and fetches nothing)", () => {
    const { container } = render(<ExpectedMaterialsSection jobId="JOB-A" />);
    expect(container.innerHTML).toBe("");
    expect(api.fetchExpectedMaterials).not.toHaveBeenCalled();
  });

  it("receive-only: read-only list with status pills + received-by, the M2 note, and NO write controls", async () => {
    const { container } = await renderSection(["cap.materials.receive"]);
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK_DUO"));
    // Status pills: expected (plain), received (ok), incident (danger).
    expect(container.querySelector(".dash-pill--ok")?.textContent).toBe("Received");
    expect(container.querySelector(".dash-pill--danger")?.textContent).toBe("Incident");
    expect(container.textContent ?? "").toContain("by Mo Manager");
    expect(container.textContent ?? "").toContain("qty received 12");
    expect(container.textContent ?? "").toContain("damaged");
    // The read-only note names the surface that actually owns the action. It used to say "from
    // the daily report (My Tasks → Daily → Expected materials)" — controls #74 deleted on
    // 2026-08-11, leaving the instruction pointing at a screen with no such buttons for a
    // fortnight (forensic report 2026-08-24, defect D13). Pinned so it cannot rot back.
    expect(container.textContent ?? "").toContain("Materials tracking");
    expect(container.textContent ?? "").not.toContain("daily report");
    // No write affordances, and no catalog fetch for a read-only viewer.
    expect(container.textContent ?? "").not.toContain("+ Add expected material");
    expect(container.querySelector('[aria-label^="Edit expected material"]')).toBeNull();
    expect(container.querySelector('[aria-label^="Remove expected material"]')).toBeNull();
    expect(container.querySelector('[aria-label^="Move expected material"]')).toBeNull();
    expect(fetchMaterials).not.toHaveBeenCalled();
  });

  it("empty list → the explicit empty state (never a silent blank)", async () => {
    vi.mocked(api.fetchExpectedMaterials).mockResolvedValue({ expected_materials: [], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const { container } = await renderSection(["cap.materials.receive"]);
    await waitFor(() => expect(container.textContent ?? "").toContain("No expected materials for this job."));
  });

  it("load failure → error banner with a WORKING Retry", async () => {
    vi.mocked(api.fetchExpectedMaterials)
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce({ expected_materials: ROWS, shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const { container, getByText } = await renderSection(["cap.materials.receive"]);
    await waitFor(() => expect(container.textContent ?? "").toContain("Failed to load expected materials."));
    fireEvent.click(getByText("Retry"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK_DUO"));
  });
});

describe("ExpectedMaterialsSection — manage CRUD", () => {
  const MANAGE = ["cap.materials.manage", "cap.materials.receive"];

  it("adds from the CATALOG picker (material_id + nextSeq; no description required)", async () => {
    vi.mocked(api.createExpectedMaterial).mockResolvedValue({ id: 99 });
    const { container, getByText, getByLabelText } = await renderSection(MANAGE);
    await waitFor(() => expect(fetchMaterials).toHaveBeenCalled());
    fireEvent.click(getByText("+ Add expected material"));
    fireEvent.change(getByLabelText("Add expected material material"), { target: { value: "8" } });
    fireEvent.change(getByLabelText("Add expected material quantity"), { target: { value: "6" } });
    fireEvent.change(getByLabelText("Add expected material unit"), { target: { value: "rows" } });
    fireEvent.submit(container.querySelector('[aria-label="Add expected material"]')!);
    await waitFor(() =>
      expect(api.createExpectedMaterial).toHaveBeenCalledWith("JOB-A", {
        material_id: 8,
        qty: 6,
        unit: "rows",
        seq: 40, // nextSeq = max(10,20,30) + 10
      }),
    );
    // Reload after the write.
    await waitFor(() => expect(api.fetchExpectedMaterials).toHaveBeenCalledTimes(2));
  });

  it("adds FREE-TEXT (custom source; description required — a blank one is refused client-side)", async () => {
    vi.mocked(api.createExpectedMaterial).mockResolvedValue({ id: 100 });
    const { container, getByText, getByLabelText } = await renderSection(MANAGE);
    fireEvent.click(getByText("+ Add expected material"));
    fireEvent.change(getByLabelText("Add expected material source"), { target: { value: "custom" } });
    // Blank description → said out loud, no call.
    fireEvent.submit(container.querySelector('[aria-label="Add expected material"]')!);
    await waitFor(() => expect(container.textContent ?? "").toContain("A description is required"));
    expect(api.createExpectedMaterial).not.toHaveBeenCalled();

    fireEvent.change(getByLabelText("Add expected material description"), { target: { value: "Conduit spools" } });
    fireEvent.submit(container.querySelector('[aria-label="Add expected material"]')!);
    await waitFor(() =>
      expect(api.createExpectedMaterial).toHaveBeenCalledWith("JOB-A", { description: "Conduit spools", seq: 40 }),
    );
  });

  it("inline-edits an EXPECTED row (full-replace fields); received/incident rows offer no Edit", async () => {
    vi.mocked(api.updateExpectedMaterial).mockResolvedValue();
    const { container, getByLabelText } = await renderSection(MANAGE);
    await waitFor(() => expect(container.querySelector('[aria-label="Edit expected material 1"]')).not.toBeNull());
    // Only the status='expected' row is editable — a received/incident row is a receipt record.
    expect(container.querySelector('[aria-label="Edit expected material 2"]')).toBeNull();
    expect(container.querySelector('[aria-label="Edit expected material 3"]')).toBeNull();

    fireEvent.click(getByLabelText("Edit expected material 1"));
    fireEvent.change(getByLabelText("Edit expected material 1 quantity"), { target: { value: "50" } });
    fireEvent.submit(container.querySelector('form[aria-label="Edit expected material 1"]')!);
    await waitFor(() =>
      expect(api.updateExpectedMaterial).toHaveBeenCalledWith(1, {
        material_id: 7,
        qty: 50,
        unit: "panels",
        expected_date: "2026-07-10",
      }),
    );
  });

  it("▲/▼ reorder writes ONLY the changed seqs (planRenumber convention) then reloads", async () => {
    vi.mocked(api.setExpectedMaterialSeq).mockResolvedValue();
    const { container, getByLabelText } = await renderSection(MANAGE);
    await waitFor(() => expect(container.querySelector('[aria-label="Move expected material 1 down"]')).not.toBeNull());
    // First row can't move up; last can't move down.
    expect((getByLabelText("Move expected material 1 up") as HTMLButtonElement).disabled).toBe(true);
    expect((getByLabelText("Move expected material 3 down") as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(getByLabelText("Move expected material 1 down"));
    await waitFor(() => expect(api.setExpectedMaterialSeq).toHaveBeenCalledTimes(2));
    expect(api.setExpectedMaterialSeq).toHaveBeenCalledWith(2, 10); // swapped pair renumbered…
    expect(api.setExpectedMaterialSeq).toHaveBeenCalledWith(1, 20);
    expect(api.setExpectedMaterialSeq).not.toHaveBeenCalledWith(3, expect.anything()); // …row 3 untouched
    await waitFor(() => expect(api.fetchExpectedMaterials).toHaveBeenCalledTimes(2)); // reload
  });

  it("deactivate is TWO-STEP (ConfirmDelete): nothing fires before the explicit Confirm", async () => {
    vi.mocked(api.deactivateExpectedMaterial).mockResolvedValue();
    const { getByLabelText } = await renderSection(MANAGE);
    await waitFor(() => expect(getByLabelText("Remove expected material 3")).not.toBeNull());
    fireEvent.click(getByLabelText("Remove expected material 3"));
    expect(api.deactivateExpectedMaterial).not.toHaveBeenCalled();
    fireEvent.click(getByLabelText("Confirm Remove expected material 3"));
    await waitFor(() => expect(api.deactivateExpectedMaterial).toHaveBeenCalledWith(3));
  });

  it("a catalog-picker load failure is SAID and the free-text path keeps working", async () => {
    vi.mocked(fetchMaterials).mockRejectedValue(new Error("catalog down"));
    vi.mocked(api.createExpectedMaterial).mockResolvedValue({ id: 101 });
    const { container, getByText, getByLabelText } = await renderSection(MANAGE);
    await waitFor(() => expect(container.textContent ?? "").toContain("Couldn't load the material catalog"));
    fireEvent.click(getByText("+ Add expected material"));
    fireEvent.change(getByLabelText("Add expected material source"), { target: { value: "custom" } });
    fireEvent.change(getByLabelText("Add expected material description"), { target: { value: "Fence posts" } });
    fireEvent.submit(container.querySelector('[aria-label="Add expected material"]')!);
    await waitFor(() =>
      expect(api.createExpectedMaterial).toHaveBeenCalledWith("JOB-A", { description: "Fence posts", seq: 40 }),
    );
  });
});
