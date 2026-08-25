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
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
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
vi.mock("../../lib/fieldops_manifests", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_manifests")>();
  return {
    ...actual,
    fetchManifests: vi.fn(),
    uploadManifest: vi.fn(),
    discardManifest: vi.fn(),
  };
});
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as api from "../../lib/fieldops_expected_materials";
import * as manifestsApi from "../../lib/fieldops_manifests";
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
    project_name: "Deep Lake",
  });
  vi.mocked(fetchMaterials).mockResolvedValue({ materials: [], next_cursor: null });
  vi.mocked(manifestsApi.fetchManifests).mockResolvedValue({ manifests: [] as never });
});
afterEach(cleanup);

/** Delivery marks are two-step (arm → confirm) so a mis-tap cannot file a permanent ledger event.
 *  Click the button, then click its ARMED label. */
/** Delivered / Partially delivered will not even ARM without a quantity (2026-08-24) — every
 *  mark test that is not ABOUT that rule fills the box first. */
const setQty = (g: (t: string) => HTMLElement, title: string, v: string) =>
  fireEvent.change(g(`Quantity received for ${title}`), { target: { value: v } });

function markTwice(getByLabelText: (t: string) => HTMLElement, title: string, label: string) {
  fireEvent.click(getByLabelText(`Mark ${title} ${label}`));
  fireEvent.click(
    getByLabelText(`Confirm ${label} for ${title} — tap again to record`),
  );
}

describe("JobMaterialsPage — what it shows", () => {
  it("renders the part number, ship + delivery dates, the running total, loads and history", async () => {
    const { container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile W8x10"));
    const text = container.textContent ?? "";
    // The heading says the job's NAME, never the JOB-###### system key (operator
    // request 2026-08-11 — the field doesn't speak job ids).
    expect(text).toContain("Materials — Deep Lake");
    expect(text).not.toContain("Materials — JOB-");
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
    markTwice(getByLabelText, "1P driven pile W8x10", "delivered");

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
    markTwice(getByLabelText, "1P driven pile W8x10", "not delivered");

    await waitFor(() => expect(api.markReceipt).toHaveBeenCalled());
    const body = vi.mocked(api.markReceipt).mock.calls[0][1];
    expect(body.kind).toBe("not_delivered");
    expect(body).not.toHaveProperty("qty");
    expect(body.note).toBe("truck never came");
  });

  it("can mark against a specific load", async () => {
    const { getByLabelText, container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    setQty(getByLabelText, "1P driven pile W8x10", "12");
    fireEvent.change(getByLabelText("Load for 1P driven pile W8x10"), { target: { value: "5" } });
    markTwice(getByLabelText, "1P driven pile W8x10", "partially delivered");

    await waitFor(() => expect(api.markReceipt).toHaveBeenCalled());
    expect(vi.mocked(api.markReceipt).mock.calls[0][1].shipment_id).toBe(5);
  });

  // ── two-step confirm on a delivery mark ────────────────────────────────────────────────
  it("a SINGLE click does not record anything — it only arms the button", async () => {
    // The whole point: a mark is an append-only ledger event with no delete path, so one stray
    // tap must not be able to file one.
    const { getByLabelText } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
    setQty(getByLabelText, "1P driven pile W8x10", "30");
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 delivered"));
    expect(api.markReceipt).not.toHaveBeenCalled();
    // …and it SAYS it is armed, in the label — colour alone is not a confirmation prompt.
    expect(
      getByLabelText("Confirm delivered for 1P driven pile W8x10 — tap again to record"),
    ).toBeTruthy();
  });

  it("the second click on the SAME button records the mark", async () => {
    const { getByLabelText } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
    setQty(getByLabelText, "1P driven pile W8x10", "30");
    markTwice(getByLabelText, "1P driven pile W8x10", "delivered");
    await waitFor(() => expect(api.markReceipt).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.markReceipt).mock.calls[0][1]).toMatchObject({ kind: "delivered" });
  });

  it("clicking a DIFFERENT kind re-arms it instead of committing the armed one", async () => {
    // Changing your mind mid-decision must never file the kind you moved away from.
    const { getByLabelText } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
    setQty(getByLabelText, "1P driven pile W8x10", "30");
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 delivered"));
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 not delivered"));
    expect(api.markReceipt).not.toHaveBeenCalled();
    // The first button is back to normal; the second is the armed one.
    expect(getByLabelText("Mark 1P driven pile W8x10 delivered")).toBeTruthy();
    expect(
      getByLabelText("Confirm not delivered for 1P driven pile W8x10 — tap again to record"),
    ).toBeTruthy();
  });

  it("arming EXPIRES — a forgotten armed button cannot be committed later by a stray tap", async () => {
    vi.useFakeTimers();
    try {
      const { getByLabelText } = mountAs("manager", RECEIVE_ONLY);
      await vi.waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
      setQty(getByLabelText, "1P driven pile W8x10", "30");
      fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 delivered"));
      expect(
        getByLabelText("Confirm delivered for 1P driven pile W8x10 — tap again to record"),
      ).toBeTruthy();
      act(() => {
        vi.advanceTimersByTime(6001);
      });
      // Reverted to its normal label, so the next tap re-arms rather than records.
      expect(getByLabelText("Mark 1P driven pile W8x10 delivered")).toBeTruthy();
      fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 delivered"));
      expect(api.markReceipt).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  // ── quantity is REQUIRED on delivered / partial (2026-08-24) ───────────────────────────
  // A qty-less delivery flips the line green while `lineOwed` goes on counting the full amount
  // outstanding, and the §51 Material List then reads "received" where this page reads
  // "outstanding". Found live on JOB-000032 (37 lines) — forensic report 2026-08-24, defect D2.
  it("Delivered with an EMPTY qty: says so in red, does not arm, and sends NOTHING", async () => {
    const { getByLabelText, findByRole } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 delivered"));

    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("Enter the quantity received");
    // No request left the browser — the whole point. A silent no-op here is what let a
    // superintendent believe he had recorded 50 deliveries that never reached the server.
    expect(api.markReceipt).not.toHaveBeenCalled();
    // ...and it did NOT arm: an armed button promises the mark is ready to record.
    expect(getByLabelText("Mark 1P driven pile W8x10 delivered")).toBeTruthy();
    // The box itself is flagged, and points at the message.
    const box = getByLabelText("Quantity received for 1P driven pile W8x10");
    expect(box.getAttribute("aria-invalid")).toBe("true");
    expect(box.getAttribute("aria-describedby")).toBe(alert.id);
  });

  it("Partially delivered is held to the same rule", async () => {
    const { getByLabelText, findByRole } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 partially delivered"));
    expect((await findByRole("alert")).textContent ?? "").toContain("Enter the quantity received");
    expect(api.markReceipt).not.toHaveBeenCalled();
  });

  it("a qty that is not a positive number is refused BESIDE THE BOX, not in the page banner", async () => {
    // The page renders a whole BOM flat, so a banner at the top is off-screen for any line
    // below the fold — which is where this used to be reported.
    const { getByLabelText, findByRole } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
    setQty(getByLabelText, "1P driven pile W8x10", "0");
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 delivered"));
    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("greater than 0");
    expect(alert.id).toContain("mark-qty-error");
    expect(api.markReceipt).not.toHaveBeenCalled();
  });

  it("typing a quantity clears the message, and the mark then goes through", async () => {
    const { getByLabelText, queryByRole, findByRole } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(getByLabelText("HARDWARE")).toBeTruthy());
    fireEvent.click(getByLabelText("Mark 1P driven pile W8x10 delivered"));
    await findByRole("alert");

    setQty(getByLabelText, "1P driven pile W8x10", "30");
    expect(queryByRole("alert")).toBeNull();
    expect(getByLabelText("Quantity received for 1P driven pile W8x10").getAttribute("aria-invalid")).toBeNull();

    markTwice(getByLabelText, "1P driven pile W8x10", "delivered");
    await waitFor(() => expect(api.markReceipt).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.markReceipt).mock.calls[0][1]).toMatchObject({ kind: "delivered", qty: 30 });
  });

  it("Not delivered stays exempt — nothing arrived, so there is no quantity to state", async () => {
    const { getByLabelText, queryByRole, container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    fireEvent.change(getByLabelText("Note for 1P driven pile W8x10"), { target: { value: "truck never came" } });
    markTwice(getByLabelText, "1P driven pile W8x10", "not delivered");
    await waitFor(() => expect(api.markReceipt).toHaveBeenCalledTimes(1));
    expect(queryByRole("alert")).toBeNull();
  });

  it("a refused mark is SAID, translated from the Worker's machine code (never silent)", async () => {
    vi.mocked(api.markReceipt).mockRejectedValue(new Error("note_required"));
    const { getByLabelText, findByRole, container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("1P driven pile"));
    markTwice(getByLabelText, "1P driven pile W8x10", "not delivered");
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
      expected_materials: [LINE], shipments: [], receipt_events: [], project_name: "Deep Lake",
    });
    fireEvent.click(getByText("Retry"));
    await waitFor(() => expect(api.fetchExpectedMaterials).toHaveBeenCalledTimes(2));
  });

  it("an empty list says so rather than rendering nothing", async () => {
    vi.mocked(api.fetchExpectedMaterials).mockResolvedValue({
      expected_materials: [], shipments: [], receipt_events: [], project_name: "Deep Lake",
    });
    const { container } = mountAs("manager", RECEIVE_ONLY);
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("Nothing is on this job's materials list yet"),
    );
  });
});

describe("the manifest list's Remove (operator request 2026-08-11)", () => {
  const REFUSED = {
    id: 2, manifest_uuid: "u-2", job_id: "JOB-000018", filename: "Bad BOM.pdf",
    declared_mime: "application/pdf", size_bytes: 1024, status: "refused" as const,
    detail: "screen:suspicious:L2:pdf_active_content:OpenAction", profile: null,
    row_count: null, mode: null, committed_through_row: 0, uploaded_by: "office.admin",
    box_file_id: null, created_at: 1, parsed_at: null, committed_at: null,
  };
  const COMMITTED = {
    ...REFUSED, id: 3, manifest_uuid: "u-3", filename: "Good BOM.pdf",
    status: "committed" as const, row_count: 12, committed_at: 2,
  };

  it("a refused upload gets Remove; an imported one keeps its row", async () => {
    vi.mocked(manifestsApi.fetchManifests).mockResolvedValue({
      manifests: [REFUSED, COMMITTED] as never,
    });
    vi.mocked(manifestsApi.discardManifest).mockResolvedValue({ ok: true, id: 2 });
    const { container, getByLabelText, queryByLabelText } = mountAs("admin", ALL_CAPS);
    await waitFor(() => expect(container.textContent ?? "").toContain("Bad BOM.pdf"));

    // A refused upload used to sit in this list forever — now it has a way off it.
    expect(getByLabelText("Remove Bad BOM.pdf")).toBeTruthy();
    // An IMPORTED manifest keeps its row: it is the provenance of lines now on the
    // list, and the Worker refuses its discard anyway.
    expect(queryByLabelText("Remove Good BOM.pdf")).toBeNull();
    expect(container.textContent ?? "").toContain("Imported");

    fireEvent.click(getByLabelText("Remove Bad BOM.pdf"));
    fireEvent.click(getByLabelText("Confirm Remove Bad BOM.pdf"));
    await waitFor(() => expect(manifestsApi.discardManifest).toHaveBeenCalledWith(2));
    // The list refreshes so the discarded row (now server-filtered) disappears.
    await waitFor(() => expect(manifestsApi.fetchManifests).toHaveBeenCalledTimes(2));
  });
});
