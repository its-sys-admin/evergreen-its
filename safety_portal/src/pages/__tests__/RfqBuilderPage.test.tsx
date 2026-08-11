/**
 * RFQ Builder page (R1, ADR-0004) — SPA render-smoke for the materials-catalog line picker
 * (the operator ask: mirror the PO builder's easy catalog linking on the RFQ line grid, while
 * still allowing free text). Selecting a catalog TYPE fills a line's Part # + Description; the
 * catalog is PRICE-FREE (GET /api/po/materials carries no cost), so Qty/Unit/Note stay
 * operator-entered. `catalogLineFields` is the REAL fn (importOriginal); fetches + auth mocked.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/po", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/po")>();
  return {
    ...actual, // keeps the REAL catalogLineFields (the identity-fill under test)
    fetchVendors: vi.fn(),
    fetchJobShipTo: vi.fn(),
    fetchPoMaterials: vi.fn(),
    createVendor: vi.fn(),
  };
});
vi.mock("../../lib/rfq", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/rfq")>();
  return {
    ...actual, // keeps MAX_RFQ_* + the status-label maps
    fetchRfqs: vi.fn(),
    fetchRfq: vi.fn(),
    createRfqDraft: vi.fn(),
    updateRfqDraft: vi.fn(),
    generateRfq: vi.fn(),
    cancelRfq: vi.fn(),
  };
});
vi.mock("../../lib/api", () => ({ fetchJobs: vi.fn() }));
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as po from "../../lib/po";
import * as rfq from "../../lib/rfq";
import { fetchJobs } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { RfqBuilderPage } from "../RfqBuilderPage";

const CATALOG: po.CatalogMaterial[] = [
  { id: 11, model_id: "Q.PEAK_DUO_XL-G11.3_BFG", manufacturer: "Qcells", category: "module", key_specs: "570-585Wp bifacial" },
  { id: 12, model_id: "Generic-Crane", manufacturer: null, category: "other", key_specs: null },
];
const JOBS = [{ job_id: "JOB-000001", project_name: "2023.126 Kendall Solar", job_no: "", site_phase: 0 }];

function authWith(capabilities: string[]) {
  return {
    user: { username: "office", role: "admin" as const, capabilities },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(useAuth).mockReturnValue(authWith(["cap.po.manage"]));
  vi.mocked(rfq.fetchRfqs).mockResolvedValue([]);
  vi.mocked(po.fetchVendors).mockResolvedValue([]);
  vi.mocked(po.fetchPoMaterials).mockResolvedValue(CATALOG);
  vi.mocked(fetchJobs).mockResolvedValue(JOBS);
});

describe("RfqBuilderPage — materials-catalog line picker", () => {
  it("pick from catalog fills a line's Part # + Description; Qty/Unit/Note stay free-text (price-free)", async () => {
    const r = render(<RfqBuilderPage />);
    await waitFor(() => expect(rfq.fetchRfqs).toHaveBeenCalled());
    fireEvent.click(r.getByText("New RFQ"));
    await waitFor(() => expect(po.fetchPoMaterials).toHaveBeenCalled());

    // The per-row picker is present (catalog loaded); pick a TYPE for line 1.
    const pick = await waitFor(() => r.getByLabelText("Line 1 pick from catalog") as HTMLSelectElement);
    fireEvent.change(pick, { target: { value: "11" } });

    // Identity fills from the catalog TYPE via the REAL catalogLineFields (importOriginal):
    // part_number ← model_id, description ← manufacturer + model + key_specs.
    expect((r.getByLabelText("Part #") as HTMLInputElement).value).toBe("Q.PEAK_DUO_XL-G11.3_BFG");
    expect((r.getByLabelText("Description") as HTMLInputElement).value).toBe(
      "Qcells Q.PEAK_DUO_XL-G11.3_BFG — 570-585Wp bifacial",
    );
    // Price-free — Qty/Unit/Note are untouched (free-text entry is the fallback).
    expect((r.getByLabelText("Qty") as HTMLInputElement).value).toBe("");
    expect((r.getByLabelText("Unit") as HTMLInputElement).value).toBe("");
    expect((r.getByLabelText("Note") as HTMLInputElement).value).toBe("");
  });

  it("with an empty catalog, no picker renders and Part #/Description stay free-text", async () => {
    vi.mocked(po.fetchPoMaterials).mockResolvedValue([]);
    const r = render(<RfqBuilderPage />);
    await waitFor(() => expect(rfq.fetchRfqs).toHaveBeenCalled());
    fireEvent.click(r.getByText("New RFQ"));
    await waitFor(() => expect(po.fetchPoMaterials).toHaveBeenCalled());

    expect(r.queryByLabelText("Line 1 pick from catalog")).toBeNull();
    const part = r.getByLabelText("Part #") as HTMLInputElement;
    fireEvent.change(part, { target: { value: "CUSTOM-123" } });
    expect(part.value).toBe("CUSTOM-123"); // free-text entry still works with no catalog
  });
});

describe("RfqBuilderPage — Evergreen job-number autofill (0057)", () => {
  it("selecting a job fills the STORED job_no even when the name has no YYYY.NNN prefix", async () => {
    vi.mocked(fetchJobs).mockResolvedValue([
      // CONFLICTING name prefix: the STORED number must win over the parse (precedence teeth).
      { job_id: "JOB-000028", project_name: "2020.999 Coker", job_no: "2026.123", site_phase: 0 },
    ]);
    vi.mocked(po.fetchJobShipTo).mockResolvedValue({
      job_id: "JOB-000028", job_no: "", site_phase: 0, ship_to_name: "", ship_to_address: "",
      ship_to_city: "", ship_to_state: "", ship_to_zip: "",
      delivery_contact_name: "", delivery_contact_phone: "", delivery_contact_email: "",
    });
    const r = render(<RfqBuilderPage />);
    await waitFor(() => expect(rfq.fetchRfqs).toHaveBeenCalled());
    fireEvent.click(r.getByText("New RFQ"));
    fireEvent.change(r.getByLabelText("Job (autofills ship-to)"), { target: { value: "JOB-000028" } });
    expect((r.getByLabelText("Job number (YYYY.NNN)") as HTMLInputElement).value).toBe("2026.123");
  });
});

describe("RfqBuilderPage — quick-add vendor (free text, 2026-07-20)", () => {
  it("typing a new vendor creates a DIRECTORY row through the existing route and joins it to the RFQ", async () => {
    vi.mocked(po.createVendor).mockResolvedValue({ vendor_key: "VEN-000042" });
    const r = render(<RfqBuilderPage />);
    await waitFor(() => expect(rfq.fetchRfqs).toHaveBeenCalled());
    fireEvent.click(r.getByText("New RFQ"));

    fireEvent.click(r.getByText("+ New vendor (not in the list)"));
    const addBtn = r.getByText("Add vendor") as HTMLButtonElement;
    expect(addBtn.disabled).toBe(true); // name + valid email required BEFORE the route is hit
    fireEvent.change(r.getByLabelText("New vendor name"), { target: { value: "Prairie Steel Co" } });
    fireEvent.change(r.getByLabelText("New vendor quote contact email"), { target: { value: "quotes@prairiesteel.example" } });
    expect(addBtn.disabled).toBe(false);
    fireEvent.click(addBtn);

    // The EXISTING vendor-create route is the write path (never a keyless free-text vendor —
    // the send lane resolves the recipient from the directory by Vendor Key).
    await waitFor(() =>
      expect(po.createVendor).toHaveBeenCalledWith({
        vendor_name: "Prairie Steel Co",
        contact_email: "quotes@prairiesteel.example",
        contact_name: undefined,
      }),
    );
    // The minted key joined THIS RFQ: the vendor chip shows the typed name immediately.
    await waitFor(() => expect(r.getByText(/Vendor added to the directory and this RFQ/)).toBeTruthy());
    expect(r.getByLabelText("Remove Prairie Steel Co")).toBeTruthy();
  });

  it("a malformed email keeps Add disabled — createVendor is never hit (the sendability gate's rejecting side)", async () => {
    const r = render(<RfqBuilderPage />);
    await waitFor(() => expect(rfq.fetchRfqs).toHaveBeenCalled());
    fireEvent.click(r.getByText("New RFQ"));

    fireEvent.click(r.getByText("+ New vendor (not in the list)"));
    fireEvent.change(r.getByLabelText("New vendor name"), { target: { value: "Prairie Steel Co" } });
    // No dot after the @ — the exact shape worker/po.ts EMAIL_RE also rejects.
    fireEvent.change(r.getByLabelText("New vendor quote contact email"), { target: { value: "quotes@prairiesteel" } });
    const addBtn = r.getByText("Add vendor") as HTMLButtonElement;
    expect(addBtn.disabled).toBe(true);
    fireEvent.click(addBtn);
    expect(po.createVendor).not.toHaveBeenCalled();
  });

  it("at the vendor cap the quick-add REFUSES before creating — never a directory row silently absent from the RFQ", async () => {
    // 12 active directory vendors — enough to reach MAX_RFQ_VENDORS via the select.
    const directory = Array.from({ length: rfq.MAX_RFQ_VENDORS }, (_, i) => ({
      vendor_key: `VEN-1000${String(10 + i)}`,
      vendor_name: `Vendor ${i}`,
      address: "", contact_name: "", contact_email: `v${i}@x.example`, contact_phone: "",
      region: "", supply_categories: [], default_terms_profile: "", gtc_reference: "",
      active: 1, notes: "", origin: "portal", sync_state: "synced", mirror_version: 1,
    })) as po.Vendor[];
    vi.mocked(po.fetchVendors).mockResolvedValue(directory);

    const r = render(<RfqBuilderPage />);
    await waitFor(() => expect(po.fetchVendors).toHaveBeenCalled());
    fireEvent.click(r.getByText("New RFQ"));

    // The reviewed silent-drop sequence: join 11 via the select, open the quick-add form
    // (still below the cap), THEN join the 12th via the select while the form sits open.
    const select = r.getByLabelText("Add a vendor") as HTMLSelectElement;
    for (let i = 0; i < rfq.MAX_RFQ_VENDORS - 1; i++) {
      fireEvent.change(select, { target: { value: directory[i].vendor_key } });
    }
    fireEvent.click(r.getByText("+ New vendor (not in the list)")); // enabled at 11
    fireEvent.change(r.getByLabelText("New vendor name"), { target: { value: "Prairie Steel Co" } });
    fireEvent.change(r.getByLabelText("New vendor quote contact email"), { target: { value: "quotes@prairiesteel.example" } });
    fireEvent.change(select, { target: { value: directory[11].vendor_key } }); // now AT the cap

    // Submit: the handler refuses BEFORE the create — visible error, no directory row minted.
    fireEvent.click(r.getByText("Add vendor"));
    await waitFor(() => expect(r.getByText(/already has the maximum/)).toBeTruthy());
    expect(po.createVendor).not.toHaveBeenCalled();
  });

  it("a create failure surfaces in the banner and joins nothing", async () => {
    vi.mocked(po.createVendor).mockRejectedValue(new Error("vendor_exists"));
    const r = render(<RfqBuilderPage />);
    await waitFor(() => expect(rfq.fetchRfqs).toHaveBeenCalled());
    fireEvent.click(r.getByText("New RFQ"));

    fireEvent.click(r.getByText("+ New vendor (not in the list)"));
    fireEvent.change(r.getByLabelText("New vendor name"), { target: { value: "Prairie Steel Co" } });
    fireEvent.change(r.getByLabelText("New vendor quote contact email"), { target: { value: "quotes@prairiesteel.example" } });
    fireEvent.click(r.getByText("Add vendor"));

    await waitFor(() => expect(r.getByText("vendor_exists")).toBeTruthy());
    expect(r.getByText("No vendors picked yet.")).toBeTruthy();
  });
});
