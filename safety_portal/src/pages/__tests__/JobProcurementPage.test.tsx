/**
 * JobProcurementPage (Track D) — per-job lifecycle tracking screen.
 * Pins: lane lists render as clickable items; the panel's three-stage timeline fills from the
 * record; the right action button per state; wrong-state 409 surfaces the current record;
 * change orders add/decide round-trips; RFQ close.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// PageShell calls useAuth, which throws outside a provider — the JobSchedulePage convention.
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));
import { useAuth } from "../../lib/auth";
import { JobProcurementPage } from "../JobProcurementPage";

const PO = {
  id: 1, po_number: "PO-2026.384-001", revision: 0, supersede_seq: 0, status: "pending_review",
  total_cents: 98700, updated_at: 1, filed: true, vendor_name: "Breaker Supply Co",
  accepted_at: null, accepted_by: null, change_order_count: 0,
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
    if (u.includes("/change-orders") && (!init || init.method !== "POST")) {
      return new Response(JSON.stringify(overrides.cos ?? { change_orders: [] }), { status: 200 });
    }
    if (u.includes("/lifecycle")) {
      return (overrides.lifecycle as Response | undefined) ?? new Response(JSON.stringify({ ok: true }), { status: 200 });
    }
    if (u.endsWith("/change-orders") && init?.method === "POST") {
      return new Response(JSON.stringify({ ok: true, id: 5, seq: 1 }), { status: 201 });
    }
    if (u.includes("/decide")) {
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }
    return new Response("{}", { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => {
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
  it("renders lanes, opens the panel, and offers Mark submitted for a generated document", async () => {
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open PO-2026.384-001"));
    expect(await screen.findByText("Generated")).toBeTruthy();
    expect(screen.getByText(/Submitted to vendor/)).toBeTruthy();
    const mark = screen.getByRole("button", { name: "Mark submitted" });
    fireEvent.click(mark);
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/procurement/po/1/lifecycle"))).toBe(true));
  });

  it("offers Mark accepted on a submitted document and Undo on an accepted one", async () => {
    routeFetch({ list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "sent" }] } });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open PO-2026.384-001"));
    expect(await screen.findByRole("button", { name: "Mark accepted" })).toBeTruthy();

    cleanup();
    routeFetch({ list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "sent", accepted_at: "2026-08-14", accepted_by: "adm" }] } });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open PO-2026.384-001"));
    expect(await screen.findByRole("button", { name: "Undo accepted" })).toBeTruthy();
    expect(screen.getByText(/2026-08-14 by adm/)).toBeTruthy();
  });

  it("surfaces a wrong-state refusal with the record's current stage", async () => {
    routeFetch({
      lifecycle: new Response(JSON.stringify({ error: "wrong_state", current_status: "sent" }), { status: 409 }),
    });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open PO-2026.384-001"));
    fireEvent.click(await screen.findByRole("button", { name: "Mark submitted" }));
    expect(await screen.findByText(/currently "Submitted"/)).toBeTruthy();
  });

  it("adds a change order (dollars → signed cents) and decides a pending one", async () => {
    routeFetch({
      list: { ...RESPONSE, purchase_orders: [{ ...PO, status: "sent" }] },
      cos: { change_orders: [{ id: 5, seq: 1, description: "Add piles", amount_cents: 250000, status: "pending", created_by: "adm", created_at: 1, decided_by: null, decided_at: null }] },
    });
    render(<JobProcurementPage jobId="JOB-P" onOpenJob={() => {}} />);
    fireEvent.click(await screen.findByLabelText("Open PO-2026.384-001"));
    fireEvent.change(await screen.findByLabelText("Change order description"), { target: { value: "Deduct conduit" } });
    fireEvent.change(screen.getByLabelText("Change order amount in dollars"), { target: { value: "-500" } });
    fireEvent.click(screen.getByRole("button", { name: "Add change order" }));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => String(c[0]).endsWith("/change-orders") && (c[1] as RequestInit)?.method === "POST");
      expect(call).toBeTruthy();
      expect(JSON.parse(String((call![1] as RequestInit).body))).toMatchObject({ amount_cents: -50000 });
    });
    fireEvent.click(screen.getByLabelText("Approve change order 1"));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/change-orders/5/decide"))).toBe(true));
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
