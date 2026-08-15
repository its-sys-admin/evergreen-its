/**
 * JobProcurementPage (Track D + D2) — per-job lifecycle tracking screen.
 * Pins: lane lists render as clickable items with CO DOCUMENTS nested under their parent;
 * the panel's three-stage timeline fills from the record; the right action button per state;
 * wrong-state 409 surfaces the current record; "Create change order" clones server-side and
 * hands off to the lane builder; RFQ close.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// PageShell calls useAuth, which throws outside a provider — the JobSchedulePage convention.
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));
import { useAuth } from "../../lib/auth";
import { JobProcurementPage } from "../JobProcurementPage";

const PO = {
  id: 1, po_number: "2026.384.1.0.0", revision: 0, supersede_seq: 0, status: "pending_review",
  total_cents: 98700, updated_at: 1, filed: true, vendor_name: "Breaker Supply Co",
  accepted_at: null, accepted_by: null, change_order_of: null, co_seq: null,
};
const RESPONSE = {
  purchase_orders: [PO],
  rfqs: [{ id: 9, rfq_number: "RFQ-2026.384-001", status: "sent", due_date: null, updated_at: 1, vendor_count: 3, sent_count: 3, responded_count: 1 }],
  subcontracts: [],
};

let fetchMock: ReturnType<typeof vi.fn>;

function routeFetch(overrides: Record<string, unknown> = {}) {
  fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/procurement") && u.includes("/api/fieldops/jobs/")) {
      return new Response(JSON.stringify(overrides.list ?? RESPONSE), { status: 200 });
    }
    if (u.includes("/lifecycle")) {
      return (overrides.lifecycle as Response | undefined) ?? new Response(JSON.stringify({ ok: true }), { status: 200 });
    }
    if (u.includes("/change-order") && init?.method === "POST") {
      return new Response(JSON.stringify({ ok: true, id: 55, change_order_of: 1, co_seq: 1 }), { status: 201 });
    }
    return new Response("{}", { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => {
  vi.stubGlobal("confirm", vi.fn(() => true));
  vi.mocked(useAuth).mockReturnValue({
    user: { username: "adm", role: "admin", capabilities: ["cap.po.manage", "cap.subcontracts.manage"] },
    loading: false, login: vi.fn(), logout: vi.fn(),
  });
  routeFetch();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("JobProcurementPage", () => {
  it("renders lanes, opens the panel, and offers Mark submitted ONLY once approved (confirm-gated)", async () => {
    routeFetch({ list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "approved" }] } });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open 2026.384.1.0.0"));
    expect(await screen.findByText("Generated")).toBeTruthy();
    expect(screen.getByText(/Submitted to vendor/)).toBeTruthy();
    const mark = screen.getByRole("button", { name: "Mark submitted" });
    fireEvent.click(mark);
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/procurement/po/1/lifecycle"))).toBe(true));
    expect(window.confirm).toHaveBeenCalled();
  });

  it("shows the awaiting-approval hint instead of a doomed Mark submitted on pending_review", async () => {
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open 2026.384.1.0.0"));
    expect(await screen.findByText(/Awaiting approval/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Mark submitted" })).toBeNull();
  });

  it("offers Open in the lane builder on a draft row", async () => {
    const openPoDraft = vi.fn();
    routeFetch({ list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "draft", po_number: null }] } });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} onOpenPoDraft={openPoDraft} />);
    fireEvent.click(await screen.findByLabelText("Open PO (unnumbered draft)"));
    fireEvent.click(await screen.findByRole("button", { name: "Open in the lane builder →" }));
    expect(openPoDraft).toHaveBeenCalledWith(1);
  });

  it("offers Mark accepted on a submitted document and Undo on an accepted one", async () => {
    routeFetch({ list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "sent" }] } });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open 2026.384.1.0.0"));
    expect(await screen.findByRole("button", { name: "Mark accepted" })).toBeTruthy();

    cleanup();
    routeFetch({ list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "sent", accepted_at: "2026-08-14", accepted_by: "adm" }] } });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open 2026.384.1.0.0"));
    expect(await screen.findByRole("button", { name: "Undo accepted" })).toBeTruthy();
    expect(screen.getByText(/2026-08-14 by adm/)).toBeTruthy();
  });

  it("surfaces a wrong-state refusal with the record's current stage", async () => {
    routeFetch({
      list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "approved" }] },
      lifecycle: new Response(JSON.stringify({ error: "wrong_state", current_status: "sent" }), { status: 409 }),
    });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open 2026.384.1.0.0"));
    fireEvent.click(await screen.findByRole("button", { name: "Mark submitted" }));
    expect(await screen.findByText(/currently "Submitted"/)).toBeTruthy();
  });

  it("nests CO documents under their parent and shows the CO context on the panel", async () => {
    routeFetch({
      list: {
        ...RESPONSE,
        purchase_orders: [
          { ...PO, status: "sent" },
          { ...PO, id: 55, po_number: "2026.384.1.0.0-CO1", status: "draft", filed: false, change_order_of: 1, co_seq: 1 },
        ],
      },
    });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    const coBtn = await screen.findByLabelText("Open 2026.384.1.0.0-CO1");
    // Nested list container carries the CO row.
    expect(coBtn.closest(".proc-colist")).toBeTruthy();
    fireEvent.click(coBtn);
    expect(await screen.findByText(/Change order CO1 —/)).toBeTruthy();
    expect(screen.getByText(/original\s+purchase order stays in force/)).toBeTruthy();
  });

  it("creates a change order server-side from a sent document and hands off to the lane builder", async () => {
    const openPoDraft = vi.fn();
    routeFetch({ list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "sent" }] } });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} onOpenPoDraft={openPoDraft} />);
    fireEvent.click(await screen.findByLabelText("Open 2026.384.1.0.0"));
    fireEvent.click(await screen.findByRole("button", { name: "Create change order" }));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => String(c[0]).endsWith("/api/po/1/change-order") && (c[1] as RequestInit)?.method === "POST");
      expect(call).toBeTruthy();
      expect(openPoDraft).toHaveBeenCalledWith(55);
    });
  });

  it("never offers Create change order on a document that is itself a CO", async () => {
    routeFetch({
      list: {
        ...RESPONSE,
        purchase_orders: [{ ...PO, id: 55, po_number: "2026.384.1.0.0-CO1", status: "sent", change_order_of: 1, co_seq: 1 }],
      },
    });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} onOpenPoDraft={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open 2026.384.1.0.0-CO1"));
    expect(await screen.findByRole("button", { name: "Mark accepted" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create change order" })).toBeNull();
  });

  it("closes an RFQ round and explains where acceptance lives", async () => {
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open RFQ-2026.384-001"));
    expect(await screen.findByText(/accepted quote becomes a\s+purchase order through the estimate import/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close this RFQ round" }));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/procurement/rfq/9/lifecycle"))).toBe(true));
  });
});
