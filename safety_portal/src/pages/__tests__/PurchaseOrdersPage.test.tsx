/**
 * Purchase-Orders HUB (2026-07 fold) — the tab strip, the keep-alive panels, and the two
 * cross-tab handoffs that are the fold's point:
 *   • Orders "New PO from a vendor estimate" → the DISPOSITION screen on the Estimates tab
 *     (the ADR-0004 decision-3 fidelity gate stays the only estimate→PO path), and
 *   • a disposition import → back on the Orders tab with the minted draft OPEN in the
 *     builder, still editable.
 * The REAL panel components mount inside the hub (integration — the handoff wiring is what
 * this file locks); every network read/write is mocked at the lib layer.
 */
import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/po", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/po")>();
  return {
    ...actual, // keeps the REAL money math + catalogLineFields
    fetchVendors: vi.fn(),
    fetchTerms: vi.fn(),
    fetchPoConfig: vi.fn(),
    fetchPoMaterials: vi.fn(),
    fetchPos: vi.fn(),
    fetchPo: vi.fn(),
    fetchJobShipTo: vi.fn(),
    createDraft: vi.fn(),
    updateDraft: vi.fn(),
    generateDraft: vi.fn(),
    supersedePo: vi.fn(),
    cancelPo: vi.fn(),
    deletePoDraft: vi.fn(),
    fetchPoAttachments: vi.fn(),
    uploadPoAttachment: vi.fn(),
    deletePoAttachment: vi.fn(),
  };
});
vi.mock("../../lib/estimates", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/estimates")>();
  return {
    ...actual, // keeps ESTIMATE_STATUS_LABEL + the upload constants
    fetchEstimates: vi.fn(),
    fetchEstimate: vi.fn(),
    uploadEstimate: vi.fn(),
    disposeEstimate: vi.fn(),
  };
});
vi.mock("../../lib/rfq", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/rfq")>();
  return {
    ...actual,
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

import * as api from "../../lib/po";
import * as est from "../../lib/estimates";
import * as rfq from "../../lib/rfq";
import { fetchJobs } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { PurchaseOrdersPage, type PoTab } from "../PurchaseOrdersPage";

function authWith(capabilities: string[]) {
  return {
    user: { username: "office", role: "admin" as const, capabilities },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

const VENDORS: api.Vendor[] = [
  {
    vendor_key: "VEN-000001",
    vendor_name: "Apex Racking",
    address: "1 Steel Way",
    contact_name: "Sam Orders",
    contact_email: "orders@apexracking.com",
    contact_phone: "555-0101",
    region: "West",
    supply_categories: ["racking"],
    default_terms_profile: "standard_17",
    gtc_reference: "",
    active: 1,
    notes: "",
    origin: "portal",
    sync_state: "synced",
    mirror_version: 1,
  },
];

/** One reviewable (manual-entry) estimate — no extraction, no previews, so the disposition's
 *  preview gate is legitimately skipped (hasExtractionLines false) and Tier-3 manual entry
 *  is the import path. */
const ESTIMATE_ROW: est.EstimateRow = {
  id: 5,
  est_uuid: "e-5",
  job_no: "2023.126",
  job_id: "JOB-000001",
  job_name: "Kendall Solar",
  vendor_key: null,
  filename: "apex-quote.pdf",
  declared_mime: "application/pdf",
  size_bytes: 1000,
  sha256: "abc",
  status: "needs_review",
  doc_type: "quote",
  detail: null,
  uploaded_by: "office",
  box_file_id: null,
  family_key: null,
  supersedes_estimate_id: null,
  po_id: null,
  rfq_id: null,
  rfq_vendor_key: null,
  created_at: 1,
  screened_at: 1,
  extracted_at: null,
  disposed_at: null,
};

/** A second reviewable estimate WITH advisory extraction lines + a rendered preview — the
 *  fixture for the retarget-resets-the-fidelity-gate test. */
const ESTIMATE_ROW_B: est.EstimateRow = {
  ...ESTIMATE_ROW,
  id: 6,
  est_uuid: "e-6",
  filename: "beta-quote.pdf",
  status: "extracted",
};
const EXTRACTION_DETAIL = (row: est.EstimateRow): est.EstimateDetail => ({
  estimate: row,
  extraction: {
    id: row.id * 10,
    estimate_id: row.id,
    tier: 1,
    schema_version: "1.0.0",
    doc_type: "quote",
    vendor_name: "Apex Racking",
    quote_number: `Q-${row.id}`,
    revision_label: null,
    quote_date: null,
    valid_until: null,
    subtotal_cents: 1000,
    tax_cents: 0,
    freight_cents: null,
    misc_cents: null,
    grand_total_cents: 1000,
    math_ok: 1,
    confidence: 0.95,
    anomalies: null,
    created_at: 1,
  },
  lines: [
    {
      id: row.id * 100,
      position: 1,
      section: null,
      part_number: null,
      description: `Extracted line of ${row.filename}`,
      qty: 1,
      unit: "EA",
      unit_cost_cents: 1000,
      extended_cents: 1000,
      math_ok: 1,
      line_note: null,
      disposition: "pending",
      edited_json: null,
    },
  ],
  preview_count: 1,
});

const PO_DETAIL = {
  po: {
    id: 77,
    po_number: null,
    job_no: "2023.126",
    site_phase: 0,
    supersede_seq: 0,
    revision: null,
    job_id: "",
    job_name: "Kendall Solar",
    vendor_key: "VEN-000001",
    status: "draft",
    supersedes_po_id: null,
    total_cents: 3702,
    updated_at: 1,
    created_at: 1,
    ship_to_name: "Kendall Solar",
    ship_to_address: "",
    ship_to_city: "",
    ship_to_state: "IL",
    ship_to_zip: "",
    delivery_contact_name: "",
    delivery_contact_phone: "",
    delivery_contact_email: "",
    sow_text: "",
    delivery_instructions: "",
    payment_terms_text: "",
    terms_profile_id: "",
    terms_version: "",
    subtotal_cents: 3702,
    tax_mode: "auto",
    tax_rate_bp: 900,
    tax_cents: 333,
    shipping_cents: 0,
    line_column_variant: "default",
    approver_name: "",
    approver_title: "",
  } as api.PoDetail,
  line_items: [
    {
      position: 1,
      part_number: "",
      description: "Rail 208in",
      qty: 3,
      unit: "EA",
      unit_cost_cents: 1234,
      extended_cents: 3702,
      watts: null,
      panels: null,
      pallets: null,
      price_per_watt_microcents: null,
    },
  ],
};

/** Route-shaped harness: the hub's `tab` prop is App-owned state in production. */
function Harness({ initialTab = "orders" as PoTab }) {
  const [tab, setTab] = useState<PoTab>(initialTab);
  return <PurchaseOrdersPage tab={tab} onTabChange={setTab} onBack={() => {}} />;
}

const panel = (container: HTMLElement, label: string) =>
  container.querySelector(`[role="tabpanel"][aria-label="${label}"]`) as HTMLElement | null;

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(useAuth).mockReturnValue(authWith(["cap.po.manage"]));
  vi.mocked(api.fetchPos).mockResolvedValue([]);
  vi.mocked(api.fetchVendors).mockResolvedValue(VENDORS);
  // 0069 — the disposition screen now resolves the job's Site/phase through this call when the
  // estimate carries a job_id. Default it so these fold tests, which are about the estimate→PO
  // hand-off rather than the site, exercise the same path without asserting on it.
  vi.mocked(api.fetchJobShipTo).mockResolvedValue({
    job_id: "JOB-000001", job_no: "2023.126", site_phase: 0,
    ship_to_name: "", ship_to_address: "", ship_to_city: "", ship_to_state: "", ship_to_zip: "",
    delivery_contact_name: "", delivery_contact_phone: "", delivery_contact_email: "",
  });
  vi.mocked(api.fetchTerms).mockResolvedValue([]);
  vi.mocked(api.fetchPoConfig).mockResolvedValue(null as never);
  vi.mocked(api.fetchPoMaterials).mockResolvedValue([]);
  vi.mocked(fetchJobs).mockResolvedValue([]);
  vi.mocked(rfq.fetchRfqs).mockResolvedValue([]);
  vi.mocked(est.fetchEstimates).mockResolvedValue([]);
});

describe("PurchaseOrdersPage — tab strip + keep-alive panels", () => {
  it("renders the three tabs; panels mount on first visit and STAY mounted across flips", async () => {
    const { container, getByRole } = render(<Harness />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalledTimes(1));

    // Orders is the active tab; the other panels haven't mounted (no fetch yet).
    expect(getByRole("tab", { name: "Purchase Orders" }).getAttribute("aria-selected")).toBe("true");
    expect(panel(container, "Purchase Orders")!.hasAttribute("hidden")).toBe(false);
    expect(panel(container, "RFQs")).toBeNull();
    expect(rfq.fetchRfqs).not.toHaveBeenCalled();

    // Flip to RFQs: its panel mounts (single fetch); Orders hides but STAYS mounted.
    fireEvent.click(getByRole("tab", { name: "RFQs" }));
    await waitFor(() => expect(rfq.fetchRfqs).toHaveBeenCalledTimes(1));
    expect(panel(container, "Purchase Orders")!.hasAttribute("hidden")).toBe(true);
    expect(panel(container, "RFQs")!.hasAttribute("hidden")).toBe(false);

    // Flip back: no re-mount, no re-fetch (keep-alive is the wizard-state guarantee).
    fireEvent.click(getByRole("tab", { name: "Purchase Orders" }));
    expect(panel(container, "Purchase Orders")!.hasAttribute("hidden")).toBe(false);
    expect(api.fetchPos).toHaveBeenCalledTimes(1);
    expect(rfq.fetchRfqs).toHaveBeenCalledTimes(1);
  });

  it("cold-loading the estimates tab (deep link) renders it active with the upload form", async () => {
    const { container, getByRole } = render(<Harness initialTab="estimates" />);
    await waitFor(() => expect(est.fetchEstimates).toHaveBeenCalled());
    expect(getByRole("tab", { name: "Vendor Estimates" }).getAttribute("aria-selected")).toBe("true");
    expect(within(panel(container, "Vendor Estimates")!).getByText("Upload an estimate")).toBeTruthy();
    expect(panel(container, "Purchase Orders")).toBeNull(); // unvisited ⇒ unmounted
  });
});

describe("PurchaseOrdersPage — the estimate→PO fold", () => {
  it("0069: the disposition seeds Site / phase from the estimate's JOB, not 0", async () => {
    // THE DEFECT THIS CLOSES. site_phase was hard-seeded to "0" and nothing ever resolved it,
    // so a PO drafted from an accepted estimate for MH405 (2026.384 site 1) composed
    // 2026.384.0.x — a contractual number for a site that does not exist. job_no alone cannot
    // fix it: 2026.384 is ALSO OG593 at site 2. The estimate must carry job_id (0069).
    vi.mocked(est.fetchEstimates).mockResolvedValue([ESTIMATE_ROW]);
    vi.mocked(est.fetchEstimate).mockResolvedValue({
      estimate: { ...ESTIMATE_ROW, job_no: "2026.384", job_id: "JOB-000034" },
      extraction: null, lines: [], preview_count: 0,
    });
    vi.mocked(api.fetchJobShipTo).mockResolvedValue({
      job_id: "JOB-000034", job_no: "2026.384", site_phase: 1,
      ship_to_name: "MH405", ship_to_address: "9208 Ridgefield Rd", ship_to_city: "Crystal Lake",
      ship_to_state: "IL", ship_to_zip: "60012",
      delivery_contact_name: "", delivery_contact_phone: "", delivery_contact_email: "",
    });

    const { getByText, getByLabelText } = render(<Harness />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
    fireEvent.click(getByText("New PO from a vendor estimate"));
    await waitFor(() => expect(getByText("apex-quote.pdf")).toBeTruthy());
    fireEvent.click(getByText("Review & import"));
    await waitFor(() => expect(est.fetchEstimate).toHaveBeenCalledWith(5));
    await waitFor(() => expect(api.fetchJobShipTo).toHaveBeenCalledWith("JOB-000034"));

    // Seeded from the job — the PO will now compose 2026.384.1.x, not 2026.384.0.x.
    await waitFor(() =>
      expect((getByLabelText("Site / phase") as HTMLInputElement).value).toBe("1"),
    );

    // ...and the seeded value must actually REACH the draft. Asserting only the input would
    // prove the box shows "1", not that the PO numbers .1 — and the number is contractual.
    vi.mocked(api.createDraft).mockResolvedValue({
      id: 91,
      totals: { subtotal_cents: 1234, tax_rate_bp: 900, tax_cents: 111, total_cents: 1345 },
    });
    vi.mocked(est.disposeEstimate).mockResolvedValue({ ok: true, status: "imported" });
    fireEvent.change(getByLabelText("Manual line 1 description"), { target: { value: "Rail 208in" } });
    fireEvent.change(getByLabelText("Manual line 1 quantity"), { target: { value: "3" } });
    fireEvent.change(getByLabelText("Manual line 1 unit cost"), { target: { value: "12.34" } });
    fireEvent.change(getByLabelText("Vendor"), { target: { value: "VEN-000001" } });
    fireEvent.change(getByLabelText("Ship-to state (2 letters — drives tax)"), { target: { value: "IL" } });
    fireEvent.click(getByText("Create draft PO"));

    await waitFor(() =>
      expect(api.createDraft).toHaveBeenCalledWith(
        expect.objectContaining({ job_no: "2026.384", site_phase: 1 }),
      ),
    );
  });

  it("0069: an estimate with NO job_id leaves Site / phase alone (no lookup, no crash)", async () => {
    // Every estimate uploaded before 0067 has job_id '' and never will have one. The screen
    // must degrade to the operator-editable default rather than guessing from job_no.
    vi.mocked(est.fetchEstimates).mockResolvedValue([ESTIMATE_ROW]);
    vi.mocked(est.fetchEstimate).mockResolvedValue({
      estimate: { ...ESTIMATE_ROW, job_id: "" },
      extraction: null, lines: [], preview_count: 0,
    });
    vi.mocked(api.fetchJobShipTo).mockClear();

    const { getByText, getByLabelText } = render(<Harness />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
    fireEvent.click(getByText("New PO from a vendor estimate"));
    await waitFor(() => expect(getByText("apex-quote.pdf")).toBeTruthy());
    fireEvent.click(getByText("Review & import"));
    await waitFor(() => expect(est.fetchEstimate).toHaveBeenCalledWith(5));
    await waitFor(() => expect(getByText("Confirm & import")).toBeTruthy());

    expect(api.fetchJobShipTo).not.toHaveBeenCalled();
    expect((getByLabelText("Site / phase") as HTMLInputElement).value).toBe("0");
  });

  it("walks the whole lane: pick on Orders → disposition on Estimates → import → draft OPEN in the builder", async () => {
    vi.mocked(est.fetchEstimates).mockResolvedValue([ESTIMATE_ROW]);
    vi.mocked(est.fetchEstimate).mockResolvedValue({
      estimate: ESTIMATE_ROW,
      extraction: null,
      lines: [],
      preview_count: 0,
    });
    vi.mocked(api.createDraft).mockResolvedValue({
      id: 77,
      totals: { subtotal_cents: 3702, tax_rate_bp: 900, tax_cents: 333, total_cents: 4035 },
    });
    vi.mocked(est.disposeEstimate).mockResolvedValue({ ok: true, status: "imported" });
    vi.mocked(api.fetchPo).mockResolvedValue(PO_DETAIL);
    vi.mocked(api.fetchPoAttachments).mockResolvedValue([]);

    const { getByRole, getByText, getByLabelText, queryByText } = render(<Harness />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());

    // 1 — the Orders tracker offers "New PO from a vendor estimate"; opening it lists the
    //     reviewable rows (fetched on demand).
    fireEvent.click(getByText("New PO from a vendor estimate"));
    await waitFor(() => expect(getByText("apex-quote.pdf")).toBeTruthy());

    // 2 — picking one flips to the Estimates tab and opens the DISPOSITION screen (the
    //     fidelity gate — never a direct import).
    fireEvent.click(getByText("Review & import"));
    await waitFor(() => expect(est.fetchEstimate).toHaveBeenCalledWith(5));
    expect(getByRole("tab", { name: "Vendor Estimates" }).getAttribute("aria-selected")).toBe("true");
    await waitFor(() => expect(getByText("Confirm & import")).toBeTruthy());

    // 3 — Tier-3 manual entry (this doc has no extraction lines, so the preview gate is
    //     legitimately inapplicable): one line + vendor + state; job_no pre-filled.
    fireEvent.change(getByLabelText("Manual line 1 description"), { target: { value: "Rail 208in" } });
    fireEvent.change(getByLabelText("Manual line 1 quantity"), { target: { value: "3" } });
    fireEvent.change(getByLabelText("Manual line 1 unit cost"), { target: { value: "12.34" } });
    fireEvent.change(getByLabelText("Vendor"), { target: { value: "VEN-000001" } });
    fireEvent.change(getByLabelText("Ship-to state (2 letters — drives tax)"), { target: { value: "IL" } });

    // 4 — import: the draft is created through the EXISTING createDraft route with the
    //     estimate_id provenance, then the estimate is disposed.
    fireEvent.click(getByText("Create draft PO"));
    await waitFor(() => expect(api.createDraft).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.createDraft).mock.calls[0][0]).toMatchObject({
      vendor_key: "VEN-000001",
      job_no: "2023.126",
      estimate_id: 5,
    });
    await waitFor(() => expect(est.disposeEstimate).toHaveBeenCalledWith(5, expect.objectContaining({ action: "imported", po_id: 77 })));

    // 5 — the hub hands the minted draft back to the Orders tab, OPEN in the builder and
    //     fully editable (the user can add/modify lines before Generate).
    await waitFor(() =>
      expect(getByRole("tab", { name: "Purchase Orders" }).getAttribute("aria-selected")).toBe("true"),
    );
    await waitFor(() => expect(api.fetchPo).toHaveBeenCalledWith(77));
    await waitFor(() => expect(getByText(/Estimate imported into draft #77/)).toBeTruthy());
    expect(getByText("Editing draft #77")).toBeTruthy();
    // The imported line is sitting in the editable grid, not a read-only view.
    expect((getByLabelText("Line 1 description") as HTMLInputElement).value).toBe("Rail 208in");

    // 6 — the round-trip closed the from-estimate picker (its list went stale the moment the
    //     import landed) — back on the tracker it offers a fresh open, not a stale list.
    fireEvent.click(getByText("← Back to the list"));
    expect(getByText("New PO from a vendor estimate")).toBeTruthy();
    expect(queryByText("Hide vendor estimates")).toBeNull();
  });

  it("retargeting the disposition to another estimate RESETS the fidelity gate and manual lines (key={openId})", async () => {
    // Both estimates are reviewable and carry ADVISORY extraction lines + 1 preview page.
    vi.mocked(est.fetchEstimates).mockResolvedValue([ESTIMATE_ROW, ESTIMATE_ROW_B]);
    vi.mocked(est.fetchEstimate).mockImplementation(async (id: number) =>
      EXTRACTION_DETAIL(id === 5 ? ESTIMATE_ROW : ESTIMATE_ROW_B),
    );

    const { getByRole, getByText, getByLabelText, getByAltText } = render(
      <Harness initialTab="estimates" />,
    );
    await waitFor(() => expect(getByText("apex-quote.pdf")).toBeTruthy());

    // Open estimate A's disposition, LOAD its preview page (the gate's evidence), and type a
    // manual Tier-3 line — the state the retarget must NOT carry over.
    fireEvent.click(within(getByText("apex-quote.pdf").closest("section")!).getByText("Review & disposition"));
    await waitFor(() => expect(est.fetchEstimate).toHaveBeenCalledWith(5));
    await waitFor(() => expect(getByText(/Extracted line of apex-quote\.pdf/)).toBeTruthy());
    fireEvent.load(getByAltText("Estimate page 1"));
    fireEvent.change(getByLabelText("Manual line 1 description"), { target: { value: "Carried line" } });

    // Cross-tab retarget: Orders picker → "Review & import" on estimate B.
    fireEvent.click(getByRole("tab", { name: "Purchase Orders" }));
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
    fireEvent.click(getByText("New PO from a vendor estimate"));
    await waitFor(() => expect(getByText("beta-quote.pdf")).toBeTruthy());
    fireEvent.click(within(getByText("beta-quote.pdf").closest("div")!).getByText("Review & import"));
    await waitFor(() => expect(est.fetchEstimate).toHaveBeenCalledWith(6));
    await waitFor(() => expect(getByText(/Extracted line of beta-quote\.pdf/)).toBeTruthy());

    // The FRESH mount (key={openId}) means estimate B starts with ZERO preview evidence: the
    // ADR-0004 decision-3 gate blocks import until B's own preview loads, and A's manual line
    // did not ride along. Without the key, A's loadedPages/manualLines would leak in — the
    // exact fidelity-gate bypass the 2026-07-20 adversarial review confirmed.
    expect(getByText(/Load a source preview page/)).toBeTruthy();
    expect((getByLabelText("Manual line 1 description") as HTMLInputElement).value).toBe("");
  });

  it("an import while the builder is mid-edit asks before clobbering; declining keeps the work", async () => {
    vi.mocked(est.fetchEstimates).mockResolvedValue([ESTIMATE_ROW]);
    vi.mocked(est.fetchEstimate).mockResolvedValue({
      estimate: ESTIMATE_ROW,
      extraction: null,
      lines: [],
      preview_count: 0,
    });
    vi.mocked(api.createDraft).mockResolvedValue({
      id: 88,
      totals: { subtotal_cents: 1234, tax_rate_bp: 900, tax_cents: 111, total_cents: 1345 },
    });
    vi.mocked(est.disposeEstimate).mockResolvedValue({ ok: true, status: "imported" });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    const { getByRole, getByText, getByLabelText } = render(<Harness />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());

    // Start a blank PO and type into it — the unsaved work at stake.
    fireEvent.click(getByText("+ New purchase order"));
    fireEvent.change(getByLabelText("Line 1 description"), { target: { value: "Half-built line" } });

    // Import an estimate over on the Estimates tab (manual-entry doc, gate inapplicable).
    fireEvent.click(getByRole("tab", { name: "Vendor Estimates" }));
    await waitFor(() => expect(getByText("apex-quote.pdf")).toBeTruthy());
    fireEvent.click(getByText("Review & disposition"));
    await waitFor(() => expect(getByText("Confirm & import")).toBeTruthy());
    fireEvent.change(getByLabelText("Manual line 1 description"), { target: { value: "Imported" } });
    fireEvent.change(getByLabelText("Manual line 1 quantity"), { target: { value: "1" } });
    fireEvent.change(getByLabelText("Manual line 1 unit cost"), { target: { value: "9.99" } });
    fireEvent.change(getByLabelText("Vendor"), { target: { value: "VEN-000001" } });
    fireEvent.change(getByLabelText("Ship-to state (2 letters — drives tax)"), { target: { value: "IL" } });
    fireEvent.click(getByText("Create draft PO"));
    await waitFor(() => expect(est.disposeEstimate).toHaveBeenCalled());

    // Declined: the import is announced + findable in the tracker, the half-built PO survives,
    // and the imported draft was NOT force-opened over it.
    await waitFor(() => expect(getByText(/open it from the list when you're ready/)).toBeTruthy());
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(api.fetchPo).not.toHaveBeenCalled();
    expect((getByLabelText("Line 1 description") as HTMLInputElement).value).toBe("Half-built line");
    confirmSpy.mockRestore();
  });

  it("with nothing reviewable, the picker points at the Vendor Estimates tab", async () => {
    vi.mocked(est.fetchEstimates).mockResolvedValue([{ ...ESTIMATE_ROW, status: "imported", po_id: 42 }]);
    const { getByRole, getByText } = render(<Harness />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());

    fireEvent.click(getByText("New PO from a vendor estimate"));
    await waitFor(() => expect(getByText(/No estimates are ready to import/)).toBeTruthy());
    fireEvent.click(getByText("Open Vendor Estimates"));
    await waitFor(() =>
      expect(getByRole("tab", { name: "Vendor Estimates" }).getAttribute("aria-selected")).toBe("true"),
    );
  });
});
