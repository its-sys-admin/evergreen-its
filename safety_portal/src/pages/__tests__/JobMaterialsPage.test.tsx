/**
 * Per-job Materials tracking page (PR2) — the deep-link target from the Job Tracker and from the
 * daily field report's material-receipt region.
 *
 * What this pins:
 *   • The affordance split. cap.materials.manage (admin) edits the expected list and its loads;
 *     cap.materials.receive + a manager/admin ROLE marks deliveries; a submitter (who also holds
 *     cap.materials.receive) reads only. The Worker re-gates all of it — these are affordances.
 *   • The three-way mark posts the right `kind`, and not_delivered deliberately sends NO qty
 *     (the Worker refuses one rather than dropping it silently, so the client must not send it).
 *   • Ship vs delivery dates, the running received total, the scheduled loads, and the delivery
 *     history all render — the reason the page exists.
 *   • Never-silent: a failed mark shows the Worker's machine code as actionable copy.
 *
 * Mocks the two lib modules + useAuth (the FieldOpsJobTracker / ExpectedMaterialsSection convention).
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
    markReceipt: vi.fn(),
    createMaterialShipment: vi.fn(),
    deactivateMaterialShipment: vi.fn(),
  };
});
vi.mock("../../lib/fieldops_materials", () => ({ fetchMaterials: vi.fn() }));
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as api from "../../lib/fieldops_expected_materials";
import { fetchMaterials } from "../../lib/fieldops_materials";
import { useAuth } from "../../lib/auth";
import { JobMaterialsPage } from "../JobMaterialsPage";

type Role = "admin" | "manager" | "submitter";
function authWith(role: Role, capabilities: string[]) {
  return {
    user: { username: "u", role, capabilities },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

const LINE: api.ExpectedMaterialRow = {
  id: 1, material_id: null, material_name: null, description: "1P driven pile W8x10",
  qty: 120, unit: "ea", expected_date: "2026-06-29", status: "expected",
  received_at: null, received_by_name: null, qty_received: 90, note: null, seq: 10,
  line_uuid: "lu-1", part_number: "805275", category: "HARDWARE",
  expected_ship_date: "2026-06-26", receipt_status: "partial", qty_received_total: 90,
};

const SHIPMENT: api.MaterialShipmentRow = {
  id: 5, line_id: 1, part_number: "805275", bol_number: "LD0867264", carrier: "Delta",
  qty: 50, unit: "ea", ship_date: "2026-06-26", delivery_date: "2026-06-29", seq: 0, source: "import",
};

const EVENT: api.MaterialReceiptEventRow = {
  id: 9, line_id: 1, shipment_id: 5, kind: "partial", qty: 50, note: "two bundles short",
  event_date: "2026-06-29", created_at: 1_700_000_000, actor_name: "Mo Manager",
};

function mountAs(role: Role, caps: string[]) {
  vi.mocked(useAuth).mockReturnValue(authWith(role, caps));
  return render(<JobMaterialsPage jobId="JOB-000018" onHome={vi.fn()} onOpenJob={vi.fn()} />);
}

const ALL_CAPS = ["cap.materials.receive", "cap.materials.manage"];
const RECEIVE_ONLY = ["cap.materials.receive"];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchExpectedMaterials).mockResolvedValue({
    expected_materials: [LINE],
    shipments: [SHIPMENT],
    receipt_events: [EVENT],
  });
  vi.mocked(fetchMaterials).mockResolvedValue({ materials: [], next_cursor: null });
});
afterEach(cleanup);

describe("JobMaterialsPage — what it shows", () => {
  it("renders the part number, ship + delivery dates, the running total, loads and history", async () => {
    const { container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile W8x10"));
    const text = container.textContent ?? "";
    expect(text).toContain("Part 805275");
    expect(text).toContain("ships 2026-06-26"); // the SHIP date — new in PR2
    expect(text).toContain("due 2026-06-29"); // expected_date, relabelled as delivery
    expect(text).toContain("received 90"); // the ledger rollup, not the last mark
    expect(text).toContain("Partially delivered"); // rollup pill, not the coarse status
    expect(text).toContain("LD0867264"); // the scheduled load
    expect(text).toContain("two bundles short"); // the event history
    expect(text).toContain("Mo Manager");
  });

  it("groups by BOM category when lines carry one", async () => {
    const { getByLabelText } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
  });
});

describe("JobMaterialsPage — the affordance split", () => {
  it("admin: edits lines and loads AND marks deliveries", async () => {
    const { getByLabelText, container } = mountAs("admin", ALL_CAPS);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    expect(getByLabelText("Edit 1P driven pile W8x10")).toBeTruthy();
    expect(getByLabelText("Add a load to 1P driven pile W8x10")).toBeTruthy();
    expect(getByLabelText("Mark 1P driven pile W8x10 delivered")).toBeTruthy();
    expect(container.textContent ?? "").toContain("Add a line");
  });

  it("manager: marks deliveries but CANNOT edit the expected list", async () => {
    const { getByLabelText, queryByLabelText, container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    expect(getByLabelText("Mark 1P driven pile W8x10 partially delivered")).toBeTruthy();
    expect(queryByLabelText("Edit 1P driven pile W8x10")).toBeNull();
    expect(container.textContent ?? "").not.toContain("Add a line");
  });

  it("submitter: reads only — no marking, no editing (the daily-report role gate, mirrored)", async () => {
    const { queryByLabelText, container } = mountAs("submitter", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    expect(queryByLabelText("Mark 1P driven pile W8x10 delivered")).toBeNull();
    expect(queryByLabelText("Edit 1P driven pile W8x10")).toBeNull();
  });
});

describe("JobMaterialsPage — marking a delivery", () => {
  it("each button posts its own kind, carrying the note, qty and date", async () => {
    const { getByLabelText, container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    fireEvent.change(getByLabelText("Quantity received for 1P driven pile W8x10"), { target: { value: "30" } });
    fireEvent.change(getByLabelText("Note for 1P driven pile W8x10"), { target: { value: "third load" } });
    fireEvent.change(getByLabelText("Delivery date for 1P driven pile W8x10"), { target: { value: "2026-07-08" } });
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 delivered"));

    await waitFor(() => expect(api.markReceipt).toHaveBeenCalled());
    expect(api.markReceipt).toHaveBeenCalledWith(1, {
      kind: "delivered", qty: 30, note: "third load", event_date: "2026-07-08",
    });
  });

  it("not_delivered sends NO qty — the Worker refuses one, so the client must not send it", async () => {
    const { getByLabelText, container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    fireEvent.change(getByLabelText("Quantity received for 1P driven pile W8x10"), { target: { value: "30" } });
    fireEvent.change(getByLabelText("Note for 1P driven pile W8x10"), { target: { value: "truck never came" } });
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 not delivered"));

    await waitFor(() => expect(api.markReceipt).toHaveBeenCalled());
    const body = vi.mocked(api.markReceipt).mock.calls[0][1];
    expect(body.kind).toBe("not_delivered");
    expect(body).not.toHaveProperty("qty");
    expect(body.note).toBe("truck never came");
  });

  it("can mark against a specific load", async () => {
    const { getByLabelText, container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    fireEvent.change(getByLabelText("Load for 1P driven pile W8x10"), { target: { value: "5" } });
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 partially delivered"));

    await waitFor(() => expect(api.markReceipt).toHaveBeenCalled());
    expect(vi.mocked(api.markReceipt).mock.calls[0][1].shipment_id).toBe(5);
  });

  it("a refused mark is SAID, translated from the Worker's machine code (never silent)", async () => {
    vi.mocked(api.markReceipt).mockRejectedValue(new Error("note_required"));
    const { getByLabelText, findByRole, container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 not delivered"));
    const alert = await findByRole("alert");
    // The copy comes from the CANONICAL registry (src/lib/errorCopy.ts), which
    // tests/test_error_copy_parity.py forces every field-ops code to have — not a local map.
    expect(alert.textContent ?? "").toContain("Add a note explaining the shortfall");
  });
});

describe("JobMaterialsPage — never-silent load states", () => {
  it("a failed load says so and offers a working Retry", async () => {
    vi.mocked(api.fetchExpectedMaterials).mockRejectedValueOnce(new Error("boom"));
    const { findByRole, getByText } = mountAs("manager", RECEIVE_ONLY);
    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("Could not load");

    vi.mocked(api.fetchExpectedMaterials).mockResolvedValue({
      expected_materials: [LINE], shipments: [], receipt_events: [],
    });
    fireEvent.click(getByText("Retry"));
    await waitFor(() => expect(api.fetchExpectedMaterials).toHaveBeenCalledTimes(2));
  });

  it("an empty list says so rather than rendering nothing", async () => {
    vi.mocked(api.fetchExpectedMaterials).mockResolvedValue({
      expected_materials: [], shipments: [], receipt_events: [],
    });
    const { container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("Nothing is on this job's materials list yet"),
    );
  });
});
