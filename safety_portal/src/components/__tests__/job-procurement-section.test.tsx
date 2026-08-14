/**
 * JobProcurementSection (Track A8) — the job detail's read-only procurement view.
 * Pins: per-lane lists render with NAMES + stage labels; null lanes (no capability) render
 * nothing for that lane; the legacy-RFQ hint shows on an empty rfqs list; NO mutation
 * affordance exists (read-only by doctrine — the only buttons are the two deep links).
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { JobProcurementSection } from "../JobProcurementSection";

const RESPONSE = {
  purchase_orders: [
    { id: 1, po_number: "PO-2026.384-001", revision: 0, supersede_seq: 0, status: "sent", total_cents: 98700, updated_at: 1, filed: true, vendor_name: "Breaker Supply Co" },
  ],
  rfqs: [],
  subcontracts: [
    { id: 2, sc_number: "SC-2026.384-001", revision: 1, supersede_seq: 0, status: "executed", trade: "civil", contract_price_cents: 500000, updated_at: 1, filed: true, sub_name: "Trench Kings" },
  ],
};

function mockFetch(body: unknown): void {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("JobProcurementSection", () => {
  it("renders lanes with names + stage labels, the legacy-RFQ hint, and no mutation affordances", async () => {
    mockFetch(RESPONSE);
    render(<JobProcurementSection jobId="JOB-P" onOpenPurchaseOrders={() => {}} onOpenSubcontracts={() => {}} />);
    expect(await screen.findByText(/PO-2026\.384-001/)).toBeTruthy();
    expect(screen.getByText(/Breaker Supply Co/)).toBeTruthy();
    expect(screen.getByText("Sent")).toBeTruthy();
    expect(screen.getByText(/Trench Kings/)).toBeTruthy();
    expect(screen.getByText("Executed")).toBeTruthy();
    expect(screen.getByText(/RFQs drafted before the job link existed/)).toBeTruthy();
    // Read-only: the ONLY buttons are the two deep links.
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["Open Purchase Orders →", "Open Subcontracts →"]);
  });

  it("renders nothing for a lane the session cannot see (null ≠ empty)", async () => {
    mockFetch({ purchase_orders: null, rfqs: null, subcontracts: RESPONSE.subcontracts });
    render(<JobProcurementSection jobId="JOB-P" onOpenSubcontracts={() => {}} />);
    expect(await screen.findByText(/Trench Kings/)).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Purchase orders" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "RFQ rounds" })).toBeNull();
  });
});
