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
import { act, cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
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
    voidReceiptEvent: vi.fn(),
    resolveExpectedMaterialIncident: vi.fn(),
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
  voided_at: null, voided_by_name: null, void_reason: null, supersedes_id: null, superseded_by_id: null,
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
    removed_lines: [],
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
      removed_lines: [],
    });
    fireEvent.click(getByText("Retry"));
    await waitFor(() => expect(api.fetchExpectedMaterials).toHaveBeenCalledTimes(2));
  });

  it("an empty list says so rather than rendering nothing", async () => {
    vi.mocked(api.fetchExpectedMaterials).mockResolvedValue({
      expected_materials: [], shipments: [], receipt_events: [], project_name: "Deep Lake",
      removed_lines: [],
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

// ── Receipt-event VOID (migration 0080; operator decisions D1-D4, D9 — 2026-10-21) ─────────────
// A delivery mark is an append-only ledger event. Until 0080 a wrong one could only be offset,
// so a double-tapped "Delivered" read as twice the material forever. A void keeps the row (struck
// through, with who/when/why) and takes it out of every rollup; a correction can then be linked
// to it. Every fixture here is synthetic.
const VLINE: api.ExpectedMaterialRow = {
  id: 2, material_id: null, material_name: null, description: "Test conduit 2in",
  qty: 40, unit: "ea", expected_date: null, status: "received",
  received_at: null, received_by_name: null, qty_received: 40, note: null, seq: 10,
  line_uuid: "lu-t2", part_number: "TP-0001", category: null,
  expected_ship_date: null, receipt_status: "delivered", qty_received_total: 40,
};
const VTITLE = "Test conduit 2in";
// Recorded times, Pacific: AT_0820 = 2026-10-01 08:20, AT_0905 = 2026-10-02 09:05,
// AT_0947 = 2026-10-02 09:47. Every fixture states one so a summary's "recorded HH:MM" is fixed.
const AT_0820 = 1_790_868_000;
const AT_0905 = 1_790_957_100;
const AT_0947 = 1_790_959_620;
const ev = (over: Partial<api.MaterialReceiptEventRow>): api.MaterialReceiptEventRow => ({
  id: 0, line_id: 2, shipment_id: null, kind: "delivered", qty: 40, note: null,
  event_date: "2026-10-01", created_at: AT_0820, actor_name: "Test Crew Lead",
  voided_at: null, voided_by_name: null, void_reason: null, supersedes_id: null, superseded_by_id: null,
  ...over,
});
const MARK_A = ev({ id: 21, note: "first truck", event_date: "2026-10-01", created_at: AT_0905 });
const MARK_B = ev({ id: 22, note: "same truck again", event_date: "2026-10-02", created_at: AT_0947 });
// 2026-10-06T03:15Z — the evening of 2026-10-05 in Pacific, already the 6th in UTC.
const VOIDED = { voided_at: 1_791_256_500, voided_by_name: "Test Office Admin", void_reason: "Entered twice" };

function serve(
  lines: api.ExpectedMaterialRow[],
  events: api.MaterialReceiptEventRow[],
  opts: { removed?: api.ExpectedMaterialRow[]; shipments?: api.MaterialShipmentRow[] } = {},
) {
  vi.mocked(api.fetchExpectedMaterials).mockResolvedValue({
    expected_materials: lines, shipments: opts.shipments ?? [], receipt_events: events,
    project_name: "Test Project T0001", removed_lines: opts.removed ?? [],
  });
}
function mountT(role: Role = "manager", caps: string[] = RECEIVE_ONLY) {
  vi.mocked(useAuth).mockReturnValue(authWith(role, caps));
  return render(<JobMaterialsPage jobId="JOB-T0001" onHome={vi.fn()} onOpenJob={vi.fn()} />);
}
const voidLabel = (summary: string) => `Void entry ${summary} for ${VTITLE}`;

describe("JobMaterialsPage — voiding a delivery mark", () => {
  beforeEach(() => {
    // clearAllMocks keeps implementations, and an earlier test leaves markReceipt rejecting —
    // so every test in this block states the outcome it needs.
    vi.mocked(api.markReceipt).mockResolvedValue({ event_id: 99 });
    vi.mocked(api.voidReceiptEvent).mockResolvedValue({
      ok: true, event_id: 22, line_id: 2, status: "received", qty_received: 40,
    });
  });

  it("Void → reason → Confirm void posts the reason and refreshes the list", async () => {
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByLabelText, getByText } = mountT();
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    fireEvent.click(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47")));
    fireEvent.change(getByLabelText("Reason for voiding this entry"), { target: { value: "Entered twice" } });
    fireEvent.click(getByText("Confirm void"));
    await waitFor(() => expect(api.voidReceiptEvent).toHaveBeenCalledWith(22, "Entered twice"));
    // The page re-reads the ledger: the rollup the server just recomputed is what it must show.
    await waitFor(() => expect(api.fetchExpectedMaterials).toHaveBeenCalledTimes(2));
  });

  it("a blank reason posts NOTHING and says why, beside the box", async () => {
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByLabelText, getByText, findByRole } = mountT();
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    fireEvent.click(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47")));
    fireEvent.change(getByLabelText("Reason for voiding this entry"), { target: { value: "   " } });
    fireEvent.click(getByText("Confirm void"));
    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("Say why this entry is being voided");
    expect(api.voidReceiptEvent).not.toHaveBeenCalled();
    expect(getByLabelText("Reason for voiding this entry").getAttribute("aria-invalid")).toBe("true");
  });

  it("the reason box is capped at 500 characters, the Worker's limit", async () => {
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByLabelText, getByText } = mountT();
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    fireEvent.click(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47")));
    expect(getByLabelText("Reason for voiding this entry").getAttribute("maxlength")).toBe("500");
  });

  it("a FAILED void keeps the typed reason, keeps the panel open, and says what went wrong", async () => {
    vi.mocked(api.voidReceiptEvent).mockRejectedValue(new Error("already_voided"));
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByLabelText, getByText, findAllByRole } = mountT();
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    fireEvent.click(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47")));
    fireEvent.change(getByLabelText("Reason for voiding this entry"), { target: { value: "Entered twice" } });
    fireEvent.click(getByText("Confirm void"));
    const alerts = await findAllByRole("alert");
    expect(alerts.map((a) => a.textContent).join(" ")).toContain("That entry was already voided");
    // The typed words survive: retyping a reason after every refusal is how reasons get skipped.
    expect((getByLabelText("Reason for voiding this entry") as HTMLInputElement).value).toBe("Entered twice");
  });

  it("Confirm void is the solid red lifecycle button (btn--retire), not the small utility-delete", async () => {
    // A void changes the client's report and the §51 mirror — it is the destructive lifecycle
    // action of this screen, so it wears that variant (global.css: Retire = solid red + gold).
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByLabelText, getByText } = mountT();
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    fireEvent.click(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47")));
    expect(getByText("Confirm void").classList.contains("btn--retire")).toBe(true);
  });

  it("a server refusal that is NOT about the reason leaves the reason box valid (S3)", async () => {
    // aria-invalid now draws the red border, so it must only claim what is true: "already
    // voided" is about the mark, not the words typed — a red box would point at the wrong thing.
    vi.mocked(api.voidReceiptEvent).mockRejectedValue(new Error("already_voided"));
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByLabelText, getByText, findAllByRole } = mountT();
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    fireEvent.click(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47")));
    fireEvent.change(getByLabelText("Reason for voiding this entry"), { target: { value: "Entered twice" } });
    fireEvent.click(getByText("Confirm void"));
    await findAllByRole("alert");
    const box = getByLabelText("Reason for voiding this entry");
    expect(box.getAttribute("aria-invalid")).toBeNull();
    // …but the message is still tied to it for a screen reader.
    expect(box.getAttribute("aria-describedby")).toBe("void-error-22");
  });

  it("the Worker refusing the reason itself (invalid_reason) DOES mark the box invalid", async () => {
    vi.mocked(api.voidReceiptEvent).mockRejectedValue(new Error("invalid_reason"));
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByLabelText, getByText, findAllByRole } = mountT();
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    fireEvent.click(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47")));
    fireEvent.change(getByLabelText("Reason for voiding this entry"), { target: { value: "Entered twice" } });
    fireEvent.click(getByText("Confirm void"));
    await findAllByRole("alert");
    expect(getByLabelText("Reason for voiding this entry").getAttribute("aria-invalid")).toBe("true");
  });

  it("only a marking role sees Void — a submitter reads the history but cannot change it", async () => {
    serve([VLINE], [MARK_A, MARK_B]);
    const { queryByLabelText, getByText } = mountT("submitter", RECEIVE_ONLY);
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    expect(queryByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47"))).toBeNull();
  });

  it("the action is called Void, never Clear (operator decision D2)", async () => {
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByLabelText, getByText, container } = mountT();
    await waitFor(() => expect(getByText(/same truck again/)).toBeTruthy());
    expect(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47")).textContent).toBe("Void");
    expect(container.textContent ?? "").not.toMatch(/\bClear\b/);
  });
});

describe("JobMaterialsPage — exact double-marks can be told apart (S6)", () => {
  it("each row and each Void button carries the time the mark was RECORDED, in Pacific", async () => {
    // The case the void exists for: the same truck marked twice — same date, kind, qty and note.
    // Without the recorded time the two rows, and the two Void buttons a screen reader announces,
    // are word-for-word identical.
    const first = ev({ id: 61, note: "Truck T-1", event_date: "2026-10-02", created_at: AT_0905 });
    const again = ev({ id: 62, note: "Truck T-1", event_date: "2026-10-02", created_at: AT_0947 });
    serve([{ ...VLINE, qty_received_total: 80 }], [first, again]);
    const { getByLabelText, getAllByText } = mountT();
    await waitFor(() => expect(getAllByText(/Truck T-1/).length).toBe(2));
    const history = getByLabelText(`Delivery history for ${VTITLE}`);
    const rows = within(history).getAllByRole("listitem").map((li) => li.textContent ?? "");
    expect(rows[0]).toContain("2026-10-02 · Delivered · 40 · recorded 09:05");
    expect(rows[1]).toContain("2026-10-02 · Delivered · 40 · recorded 09:47");
    expect(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:05"))).toBeTruthy();
    expect(getByLabelText(voidLabel("2026-10-02 · Delivered · 40 · recorded 09:47"))).toBeTruthy();
  });
});

describe("JobMaterialsPage — voided rows in the history", () => {
  it("are hidden by default; Show voided (n) reveals them struck through with who, when and why", async () => {
    serve([VLINE], [MARK_A, ev({ ...MARK_B, ...VOIDED })]);
    const { getByText, queryByText, getByLabelText } = mountT();
    await waitFor(() => expect(getByText(/first truck/)).toBeTruthy());
    expect(queryByText(/same truck again/)).toBeNull();

    fireEvent.click(getByText("Show voided (1)"));
    const history = getByLabelText(`Delivery history for ${VTITLE}`);
    const row = within(history).getByText(/same truck again/).closest("li")!;
    expect(row.textContent ?? "").toContain("Voided");
    expect(row.textContent ?? "").toContain("Test Office Admin");
    expect(row.textContent ?? "").toContain("Entered twice");
    // The void date in the SAME form as the event dates beside it, and in the crews' day: this
    // stamp is already the 6th in UTC (S8).
    expect(row.textContent ?? "").toContain("on 2026-10-05");
    expect(row.querySelector(".mat-event--voided")).toBeTruthy();
    // A voided row cannot be voided again.
    expect(within(row).queryByText("Void")).toBeNull();
    expect(getByText("Hide voided (1)")).toBeTruthy();
  });

  it("a subcontractor sees THAT a mark was voided, and when — the Worker withholds who and why (W7)", async () => {
    serve([VLINE], [MARK_A, ev({ ...MARK_B, voided_at: VOIDED.voided_at })]);
    const { getByText } = mountT("submitter", RECEIVE_ONLY);
    await waitFor(() => expect(getByText(/first truck/)).toBeTruthy());
    fireEvent.click(getByText("Show voided (1)"));
    const row = getByText(/same truck again/).closest("li")!;
    // A sentence, not a dangling "on 2026-10-05".
    expect(row.querySelector(".mat-event__void-note")?.textContent).toBe("Voided on 2026-10-05");
  });

  it("no toggle at all when nothing on the line is voided", async () => {
    serve([VLINE], [MARK_A, MARK_B]);
    const { getByText, queryByText } = mountT();
    await waitFor(() => expect(getByText(/first truck/)).toBeTruthy());
    expect(queryByText(/Show voided/)).toBeNull();
  });

  it("labels a correction and the voided entry it corrected — each NAMING the other (S7)", async () => {
    const wrong = ev({
      id: 23, kind: "partial", qty: 25, note: "miscounted", event_date: "2026-10-01", ...VOIDED,
      superseded_by_id: 24,
    });
    const right = ev({ id: 24, kind: "partial", qty: 15, note: "recounted", event_date: "2026-10-01", supersedes_id: 23 });
    serve([{ ...VLINE, qty_received_total: 15, receipt_status: "partial", status: "expected" }], [wrong, right]);
    const { getByText } = mountT();
    await waitFor(() => expect(getByText(/recounted/)).toBeTruthy());
    // "Corrects an earlier entry" left the reader to work out WHICH one.
    expect(getByText(/recounted/).closest("li")!.textContent ?? "").toContain(
      "Corrects 2026-10-01 · Partially delivered · 25",
    );

    fireEvent.click(getByText("Show voided (1)"));
    const voidedRow = getByText(/miscounted/).closest("li")!;
    expect(voidedRow.textContent ?? "").toContain("Corrected by 2026-10-01 · Partially delivered · 15");
    // Already corrected: offering a second correction would only meet 409 already_corrected.
    expect(within(voidedRow).queryByText("Record the correct entry")).toBeNull();
  });
});

describe("JobMaterialsPage — recording the correct entry (D3, D4)", () => {
  const LINE_SHORT: api.ExpectedMaterialRow = {
    ...VLINE, qty_received_total: 40, receipt_status: "delivered",
  };
  const WRONG = ev({ id: 31, kind: "delivered", qty: 40, note: "whole order", event_date: "2026-10-03" });

  beforeEach(() => {
    vi.mocked(api.markReceipt).mockResolvedValue({ event_id: 32 });
    vi.mocked(api.voidReceiptEvent).mockResolvedValue({
      ok: true, event_id: 31, line_id: 2, status: "expected", qty_received: null,
    });
  });

  /** Void WRONG through the UI; resolves to the "what next" group shown after it. */
  async function voidWrong(g: ReturnType<typeof mountT>, shipments: api.MaterialShipmentRow[] = []) {
    await waitFor(() => expect(g.getByText(/whole order/)).toBeTruthy());
    fireEvent.click(g.getByLabelText(voidLabel("2026-10-03 · Delivered · 40 · recorded 08:20")));
    fireEvent.change(g.getByLabelText("Reason for voiding this entry"), { target: { value: "Only 30 came" } });
    // After the void the server reports the line owing everything again.
    serve([{ ...LINE_SHORT, qty_received_total: null, receipt_status: null, status: "expected" }],
      [{ ...WRONG, ...VOIDED, void_reason: "Only 30 came" }], { shipments });
    fireEvent.click(g.getByText("Confirm void"));
    return waitFor(() => g.getByRole("group", { name: /^After voiding/ }));
  }

  it("after a void, ASKS: 'Done — it was a duplicate' first, 'Record the correct entry' second (S5)", async () => {
    // Most voids are duplicates, which need nothing more. Opening the correction form straight
    // away made the common case look unfinished and invited a second, wrong mark.
    serve([LINE_SHORT], [WRONG]);
    const g = mountT();
    const choice = await voidWrong(g);
    const buttons = within(choice).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["Done — it was a duplicate", "Record the correct entry"]);
    expect(buttons[0].classList.contains("btn--primary")).toBe(true);
    expect(buttons[1].classList.contains("btn--secondary")).toBe(true);
    // Nothing is open behind the question.
    expect(g.queryByLabelText(`Record the correct entry for ${VTITLE}`)).toBeNull();
  });

  it("'Done — it was a duplicate' closes the question and records nothing", async () => {
    serve([LINE_SHORT], [WRONG]);
    const g = mountT();
    const choice = await voidWrong(g);
    fireEvent.click(within(choice).getByText("Done — it was a duplicate"));
    expect(g.queryByRole("group", { name: /^After voiding/ })).toBeNull();
    expect(g.queryByLabelText(`Record the correct entry for ${VTITLE}`)).toBeNull();
    expect(api.markReceipt).not.toHaveBeenCalled();
  });

  it("the correction form has VISIBLE labels, sits in its own panel, and shows the voided row it replaces (S4)", async () => {
    const loads: api.MaterialShipmentRow[] = [{
      id: 7, line_id: 2, part_number: "TP-0001", bol_number: "TB-0007", carrier: "Test Carrier",
      qty: 40, unit: "ea", ship_date: "2026-09-30", delivery_date: "2026-10-03", seq: 0, source: "manual",
    }];
    serve([LINE_SHORT], [WRONG], { shipments: loads });
    const g = mountT();
    const choice = await voidWrong(g, loads);
    // The voided row is hidden by default; it must not be while it is the thing being replaced.
    expect(g.queryByText("Hide voided (1)")).toBeNull();
    fireEvent.click(within(choice).getByText("Record the correct entry"));
    const form = g.getByLabelText(`Record the correct entry for ${VTITLE}`);
    expect(form.classList.contains("mat-correct")).toBe(true);
    // A placeholder vanishes as soon as the box is prefilled — which this form always is — so the
    // fields need labels a sighted person can read, each wrapping its own control.
    for (const [text, name] of [
      ["Kind", "Correct entry kind"],
      ["Quantity", "Correct entry quantity"],
      ["Note", "Correct entry note"],
      ["Date", "Correct entry date"],
      ["Load", "Correct entry load"],
    ] as const) {
      const label = within(form).getByText(text).closest("label");
      expect(label, `${text} has a <label>`).toBeTruthy();
      expect(label!.contains(within(form).getByLabelText(name))).toBe(true);
    }
    expect(g.getByText("Hide voided (1)")).toBeTruthy();
    const struck = within(g.getByLabelText(`Delivery history for ${VTITLE}`)).getByText(/whole order/);
    expect(struck.closest(".mat-event--voided")).toBeTruthy();
  });

  it("after a void, offers the form PREFILLED from the voided mark and posts supersedes_id", async () => {
    serve([LINE_SHORT], [WRONG]);
    const g = mountT();
    const { getByLabelText } = g;
    fireEvent.click(within(await voidWrong(g)).getByText("Record the correct entry"));

    const form = await waitFor(() => getByLabelText(`Record the correct entry for ${VTITLE}`));
    expect((within(form).getByLabelText("Correct entry kind") as HTMLSelectElement).value).toBe("delivered");
    expect((within(form).getByLabelText("Correct entry quantity") as HTMLInputElement).value).toBe("40");
    expect((within(form).getByLabelText("Correct entry note") as HTMLInputElement).value).toBe("whole order");
    expect((within(form).getByLabelText("Correct entry date") as HTMLInputElement).value).toBe("2026-10-03");

    fireEvent.change(within(form).getByLabelText("Correct entry kind"), { target: { value: "partial" } });
    fireEvent.change(within(form).getByLabelText("Correct entry quantity"), { target: { value: "30" } });
    fireEvent.click(within(form).getByText("Record correct entry"));
    await waitFor(() => expect(api.markReceipt).toHaveBeenCalledTimes(1));
    expect(api.markReceipt).toHaveBeenCalledWith(2, {
      kind: "partial", qty: 30, note: "whole order", event_date: "2026-10-03", supersedes_id: 31,
    });
  });

  it("still works when the user comes back LATER — a voided, uncorrected row keeps the action", async () => {
    // D4: no time window. Weeks later, other marks landed in between; the voided row still offers it.
    const later = ev({ id: 40, kind: "partial", qty: 10, note: "next load", event_date: "2026-10-20" });
    serve([{ ...LINE_SHORT, qty_received_total: 10, receipt_status: "partial", status: "expected" }],
      [{ ...WRONG, ...VOIDED }, later]);
    const { getByLabelText, getByText } = mountT();
    await waitFor(() => expect(getByText(/next load/)).toBeTruthy());
    fireEvent.click(getByText("Show voided (1)"));
    const voidedRow = getByText(/whole order/).closest("li")!;
    // Quieter than the post-void choice: the row is history, and its action should not shout.
    expect(within(voidedRow).getByText("Record the correct entry").classList.contains("mat-quiet-btn")).toBe(true);
    fireEvent.click(within(voidedRow).getByText("Record the correct entry"));
    const form = getByLabelText(`Record the correct entry for ${VTITLE}`);
    fireEvent.change(within(form).getByLabelText("Correct entry quantity"), { target: { value: "30" } });
    fireEvent.click(within(form).getByText("Record correct entry"));
    await waitFor(() => expect(api.markReceipt).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.markReceipt).mock.calls[0][1]).toMatchObject({ qty: 30, supersedes_id: 31 });
  });

  it("a correction is held to the quantity rule like any mark — nothing posts without one", async () => {
    serve([{ ...LINE_SHORT, qty_received_total: null, receipt_status: null }], [{ ...WRONG, ...VOIDED }]);
    const { getByLabelText, getByText, findAllByRole } = mountT();
    await waitFor(() => expect(getByText("Show voided (1)")).toBeTruthy());
    fireEvent.click(getByText("Show voided (1)"));
    fireEvent.click(getByText("Record the correct entry"));
    const form = getByLabelText(`Record the correct entry for ${VTITLE}`);
    fireEvent.change(within(form).getByLabelText("Correct entry quantity"), { target: { value: "" } });
    fireEvent.click(within(form).getByText("Record correct entry"));
    const alerts = await findAllByRole("alert");
    expect(alerts.map((a) => a.textContent).join(" ")).toContain("Enter the quantity received");
    expect(api.markReceipt).not.toHaveBeenCalled();
    expect(within(form).getByLabelText("Correct entry quantity").getAttribute("aria-invalid")).toBe("true");
  });

  it("asks the D9 question too when the line is STILL fully received after the void", async () => {
    // Voiding one of two identical marks leaves the line settled; a "correction" on top of that
    // would count the material a second time — the very double-mark the void just removed.
    serve([LINE_SHORT], [{ ...WRONG, ...VOIDED }, ev({ id: 33, qty: 40, note: "the real one" })]);
    const { getByLabelText, getByText, queryByText } = mountT();
    await waitFor(() => expect(getByText("Show voided (1)")).toBeTruthy());
    fireEvent.click(getByText("Show voided (1)"));
    fireEvent.click(getByText("Record the correct entry"));
    const form = getByLabelText(`Record the correct entry for ${VTITLE}`);
    fireEvent.click(within(form).getByText("Record correct entry"));
    // Asked in the correction's own words — "another delivery" is not what is being recorded.
    expect(
      within(form).getByText("This line already has 40 of 40 ea received. Record the correction anyway?"),
    ).toBeTruthy();
    expect(api.markReceipt).not.toHaveBeenCalled();
    fireEvent.click(within(form).getByLabelText(`Cancel — do not record the correction for ${VTITLE}`));
    expect(queryByText(/This line already has/)).toBeNull();
    expect(api.markReceipt).not.toHaveBeenCalled();
    fireEvent.click(within(form).getByText("Record correct entry"));
    fireEvent.click(within(form).getByText("Record the correction"));
    await waitFor(() => expect(api.markReceipt).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.markReceipt).mock.calls[0][1]).toMatchObject({ qty: 40, supersedes_id: 31 });
  });

  it("a FAILED correction keeps what was typed", async () => {
    vi.mocked(api.markReceipt).mockRejectedValue(new Error("already_corrected"));
    serve([{ ...LINE_SHORT, qty_received_total: null, receipt_status: null }], [{ ...WRONG, ...VOIDED }]);
    const { getByLabelText, getByText, findAllByRole } = mountT();
    await waitFor(() => expect(getByText("Show voided (1)")).toBeTruthy());
    fireEvent.click(getByText("Show voided (1)"));
    fireEvent.click(getByText("Record the correct entry"));
    const form = getByLabelText(`Record the correct entry for ${VTITLE}`);
    fireEvent.change(within(form).getByLabelText("Correct entry quantity"), { target: { value: "30" } });
    fireEvent.click(within(form).getByText("Record correct entry"));
    const alerts = await findAllByRole("alert");
    expect(alerts.map((a) => a.textContent).join(" ")).toContain("already has a correction");
    expect((within(form).getByLabelText("Correct entry quantity") as HTMLInputElement).value).toBe("30");
    // The refusal is about the voided entry, not the quantity — no red box on the quantity (S3).
    expect(within(form).getByLabelText("Correct entry quantity").getAttribute("aria-invalid")).toBeNull();
  });
});

describe("JobMaterialsPage — a failed request never eats typed input (run())", () => {
  it("a refused Resolve keeps the typed note and the panel open", async () => {
    vi.mocked(api.resolveExpectedMaterialIncident).mockRejectedValue(new Error("note_required"));
    serve([{ ...VLINE, status: "incident" }], [MARK_A]);
    const { getByLabelText, getByText, findAllByRole } = mountT();
    await waitFor(() => expect(getByText("Resolve problem")).toBeTruthy());
    fireEvent.click(getByText("Resolve problem"));
    fireEvent.change(getByLabelText("How was it resolved"), { target: { value: "Supplier re-sent the pallet" } });
    fireEvent.click(getByText("Mark resolved"));
    await waitFor(() => expect(api.resolveExpectedMaterialIncident).toHaveBeenCalled());
    const alerts = await findAllByRole("alert");
    expect(alerts.map((a) => a.textContent).join(" ")).toContain("Add a note explaining the shortfall");
    expect((getByLabelText("How was it resolved") as HTMLInputElement).value).toBe("Supplier re-sent the pallet");
  });
});

describe("JobMaterialsPage — D9: asking before another delivery on a fully received line", () => {
  const D9 = "This line already has 40 of 40 ea received. Record another delivery anyway?";
  beforeEach(() => {
    vi.mocked(api.markReceipt).mockResolvedValue({ event_id: 50 });
  });

  it("Delivered on a settled line asks first; Cancel posts NOTHING", async () => {
    serve([VLINE], [MARK_A]);
    const { getByLabelText, getByText, queryByText } = mountT();
    await waitFor(() => expect(getByText(/first truck/)).toBeTruthy());
    setQty(getByLabelText, VTITLE, "40");
    fireEvent.click(getByLabelText(`Mark ${VTITLE} delivered`));
    expect(getByText(D9)).toBeTruthy();
    fireEvent.click(getByLabelText(`Cancel — do not record another delivery for ${VTITLE}`));
    expect(queryByText(D9)).toBeNull();
    expect(api.markReceipt).not.toHaveBeenCalled();
  });

  it("Record another delivery posts the mark the user asked for", async () => {
    serve([VLINE], [MARK_A]);
    const { getByLabelText, getByText } = mountT();
    await waitFor(() => expect(getByText(/first truck/)).toBeTruthy());
    setQty(getByLabelText, VTITLE, "5");
    fireEvent.click(getByLabelText(`Mark ${VTITLE} partially delivered`));
    fireEvent.click(getByText("Record another delivery"));
    await waitFor(() => expect(api.markReceipt).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.markReceipt).mock.calls[0][1]).toMatchObject({ kind: "partial", qty: 5 });
  });

  it("Not delivered on a settled line does NOT ask — it adds nothing to the total", async () => {
    serve([VLINE], [MARK_A]);
    const { getByLabelText, getByText, queryByText } = mountT();
    await waitFor(() => expect(getByText(/first truck/)).toBeTruthy());
    fireEvent.click(getByLabelText(`Mark ${VTITLE} not delivered`));
    expect(queryByText(D9)).toBeNull();
    // The ordinary two-step arm still applies.
    expect(getByLabelText(`Confirm not delivered for ${VTITLE} — tap again to record`)).toBeTruthy();
  });

  it("a line that still owes something never asks", async () => {
    serve([{ ...VLINE, qty_received_total: 30 }], [MARK_A]);
    const { getByLabelText, getByText, queryByText } = mountT();
    await waitFor(() => expect(getByText(/first truck/)).toBeTruthy());
    setQty(getByLabelText, VTITLE, "10");
    fireEvent.click(getByLabelText(`Mark ${VTITLE} delivered`));
    expect(queryByText(/already has/)).toBeNull();
    expect(getByLabelText(`Confirm delivered for ${VTITLE} — tap again to record`)).toBeTruthy();
  });
});

// ── D11 (2026-10-21): a line with deliveries still counting cannot be removed; a line removed
// BEFORE that rule still carries marks, and those must stay voidable (W2/W3). Synthetic data. ──
const RLINE: api.ExpectedMaterialRow = {
  ...VLINE, id: 3, description: "Test bracket 4in", line_uuid: "lu-t3", part_number: "TP-0003",
  qty: 5, qty_received: 5, qty_received_total: 5, receipt_status: "delivered", status: "received",
};
const RTITLE = "Test bracket 4in";
const R_COUNTING = ev({ id: 51, line_id: 3, qty: 5, note: "bracket crate", created_at: AT_0905 });
const R_VOIDED = ev({ id: 52, line_id: 3, qty: 5, note: "bracket crate twice", created_at: AT_0947, ...VOIDED });
const REMOVED_COPY = "Removed from this job's list. Deliveries recorded against it still count until voided.";

describe("JobMaterialsPage — 'Removed from the list' (S11)", () => {
  beforeEach(() => {
    vi.mocked(api.voidReceiptEvent).mockResolvedValue({
      ok: true, event_id: 51, line_id: 3, status: "received", qty_received: null,
    });
  });

  it("shows removed lines that still carry marks, with the copy and their history", async () => {
    serve([VLINE], [MARK_A, R_COUNTING, R_VOIDED], { removed: [RLINE] });
    const { findByLabelText } = mountT();
    const group = await findByLabelText("Removed from the list");
    expect(group.textContent ?? "").toContain(RTITLE);
    expect(group.textContent ?? "").toContain(REMOVED_COPY);
    expect(within(group).getByText(/bracket crate/)).toBeTruthy();
  });

  it("no such group when nothing removed carries a mark", async () => {
    serve([VLINE], [MARK_A]);
    const { queryByLabelText, getByText } = mountT();
    await waitFor(() => expect(getByText(/first truck/)).toBeTruthy());
    expect(queryByLabelText("Removed from the list")).toBeNull();
  });

  it("is VOID-ONLY: Void on a counting mark, and no mark, correction or edit controls anywhere", async () => {
    serve([VLINE], [MARK_A, R_COUNTING, R_VOIDED], { removed: [RLINE] });
    const { findByLabelText, queryByLabelText } = mountT("admin", ALL_CAPS);
    const group = await findByLabelText("Removed from the list");
    expect(within(group).getByLabelText(`Void entry 2026-10-01 · Delivered · 5 · recorded 09:05 for ${RTITLE}`))
      .toBeTruthy();
    expect(queryByLabelText(`Mark ${RTITLE} delivered`)).toBeNull();
    expect(queryByLabelText(`Edit ${RTITLE}`)).toBeNull();
    expect(queryByLabelText(`Remove ${RTITLE}`)).toBeNull();
    // A voided mark on a removed line cannot be corrected (the Worker refuses: inactive line).
    fireEvent.click(within(group).getByText("Show voided (1)"));
    const voidedRow = within(group).getByText(/bracket crate twice/).closest("li")!;
    expect(voidedRow.textContent ?? "").toContain("Voided");
    expect(within(voidedRow).queryByText("Record the correct entry")).toBeNull();
  });

  it("voids a removed line's mark — and offers no correction afterwards", async () => {
    serve([VLINE], [MARK_A, R_COUNTING], { removed: [RLINE] });
    const { findByLabelText, getByLabelText, getByText, queryByRole } = mountT();
    const group = await findByLabelText("Removed from the list");
    fireEvent.click(within(group).getByLabelText(`Void entry 2026-10-01 · Delivered · 5 · recorded 09:05 for ${RTITLE}`));
    fireEvent.change(getByLabelText("Reason for voiding this entry"), { target: { value: "Entered twice" } });
    fireEvent.click(getByText("Confirm void"));
    await waitFor(() => expect(api.voidReceiptEvent).toHaveBeenCalledWith(51, "Entered twice"));
    await waitFor(() => expect(api.fetchExpectedMaterials).toHaveBeenCalledTimes(2));
    expect(queryByRole("group", { name: /^After voiding/ })).toBeNull();
  });

  it("a submitter reads the group but cannot void", async () => {
    serve([VLINE], [MARK_A, R_COUNTING], { removed: [RLINE] });
    const { findByLabelText } = mountT("submitter", RECEIVE_ONLY);
    const group = await findByLabelText("Removed from the list");
    expect(within(group).getByText(/bracket crate/)).toBeTruthy();
    expect(within(group).queryByText("Void")).toBeNull();
  });

  it("removed lines stay out of the page's owed/settled summary", async () => {
    serve([VLINE], [MARK_A, R_COUNTING], { removed: [RLINE] });
    const { findByLabelText, getByLabelText } = mountT();
    await findByLabelText("Removed from the list");
    const summary = getByLabelText("Materials summary");
    const total = within(summary).getByText("Lines total").previousElementSibling;
    expect(total?.textContent).toBe("1");
  });
});

describe("JobMaterialsPage — removing a line that still has deliveries (S12)", () => {
  it("the refusal is said in words, beside the line being removed", async () => {
    vi.mocked(api.deactivateExpectedMaterial).mockRejectedValue(new Error("line_has_deliveries"));
    serve([VLINE], [MARK_A]);
    const { getByLabelText, findByRole } = mountT("admin", ALL_CAPS);
    await waitFor(() => expect(getByLabelText(`Remove ${VTITLE}`)).toBeTruthy());
    fireEvent.click(getByLabelText(`Remove ${VTITLE}`));
    fireEvent.click(getByLabelText(`Confirm Remove ${VTITLE}`));
    const alert = await findByRole("alert");
    expect(alert.textContent).toBe("This line has deliveries recorded. Void them first, then remove the line.");
    // At the line, not only in the page banner thousands of pixels above a long BOM.
    expect(alert.closest(".dash-card")?.textContent ?? "").toContain(VTITLE);
  });
});
