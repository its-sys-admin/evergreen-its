/**
 * PO Builder page (S6) — SPA render-smoke for the wizard + tracker. Covers: cap-gated
 * affordances, the live integer-cents math mirror (2-line fixture → exact $ display), the
 * line-column variant toggle, the job_no suggestion parse, and the totals_mismatch error
 * path (the panel re-renders from the Worker's `recomputed` — the server is authoritative).
 * Lib fns + auth are mocked (importOriginal keeps the REAL money math — formatCents /
 * computeDisplayTotals / lineExtendedCents are the code under test here).
 */
import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/po", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/po")>();
  return {
    ...actual,
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
vi.mock("../../lib/api", () => ({ fetchJobs: vi.fn() }));
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as api from "../../lib/po";
import { fetchJobs } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { PoBuilderPage } from "../PoBuilderPage";

function authWith(capabilities: string[]) {
  return {
    user: { username: "office", role: "admin" as const, capabilities },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

const CONFIG: api.PoConfig = {
  purchaser: {
    entity: "Evergreen Renewables LLC",
    address_lines: ["100 Spectrum Center Dr. STE 570", "Irvine, CA. 92618"],
    phone: "888-303-6424",
    invoice_routing: { to: "invoices@example.com", cc: ["ap-lead@example.com"] },
  },
  tax: {
    rates_bp: { IL: 900, OR: 0 },
    state_names: { IL: "Illinois", OR: "Oregon" },
  },
  // MOCKED fixture (HOUSE REFLEXES §5 — SPA tests never importActual the live config).
  delivery_contacts: [
    { name: "Pat Yardman", phone: "555-0177", email: "pat@yard.example" },
    { name: "Dana Dockside", phone: "555-0188", email: "" }, // no email configured
  ],
};

const TERMS: api.TermsProfile[] = [
  {
    id: "standard_17",
    kind: "library",
    label: "Standard 17-clause (Purchase Order 2019)",
    description: "Evergreen's standard domestic terms.",
    current_version: "1",
    tokens: [],
    render_line: null,
  },
];

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

const JOBS = [{ job_id: "JOB-000001", project_name: "2023.126 Kendall Solar", job_no: "", site_phase: 0 }];

const CATALOG: api.CatalogMaterial[] = [
  { id: 11, model_id: "Q.PEAK_DUO_XL-G11.3_BFG", manufacturer: "Qcells", category: "module", key_specs: "570-585Wp bifacial" },
  { id: 12, model_id: "Generic-Crane", manufacturer: null, category: "other", key_specs: null },
];

/** A ship-to auto-fill block with all fields empty — the default so a job select in the
 *  existing fixtures auto-fills NOTHING (the fixtures set ship-to state manually). */
const EMPTY_SHIPTO: api.JobShipTo = {
  job_id: "JOB-000001",
  job_no: "",
  site_phase: 0,
  ship_to_name: "",
  ship_to_address: "",
  ship_to_city: "",
  ship_to_state: "",
  ship_to_zip: "",
  delivery_contact_name: "",
  delivery_contact_phone: "",
  delivery_contact_email: "",
};

function poRow(overrides: Partial<api.PoListRow>): api.PoListRow {
  return {
    id: 1,
    po_number: null,
    job_no: "2023.126",
    site_phase: 0,
    supersede_seq: 0,
    revision: null,
    vendor_key: "VEN-000001",
    change_order_of: null,
    co_seq: null,
    job_id: "JOB-000001",
    job_name: "2023.126 Kendall Solar",
    status: "draft",
    total_cents: 4046,
    supersedes_po_id: null,
    box_file_id: null,
    created_by: "office",
    created_at: 1_780_000_000,
    updated_at: 1_780_000_000,
    ...overrides,
  };
}

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(useAuth).mockReturnValue(authWith(["cap.po.manage"]));
  vi.mocked(api.fetchPos).mockResolvedValue([]);
  vi.mocked(api.fetchVendors).mockResolvedValue(VENDORS);
  vi.mocked(api.fetchTerms).mockResolvedValue(TERMS);
  vi.mocked(api.fetchPoConfig).mockResolvedValue(CONFIG);
  vi.mocked(api.fetchPoMaterials).mockResolvedValue(CATALOG);
  vi.mocked(api.fetchJobShipTo).mockResolvedValue(EMPTY_SHIPTO);
  vi.mocked(api.fetchPoAttachments).mockResolvedValue([]);
  vi.mocked(fetchJobs).mockResolvedValue(JOBS);
});

/** Open the builder and fill the 2-line fixture: 3 × $12.34 + 2 × $0.05, ship-to IL (auto tax). */
async function openBuilderWithFixture(r: ReturnType<typeof render>) {
  const { getByText, getByLabelText } = r;
  await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
  fireEvent.click(getByText("+ New purchase order"));

  fireEvent.change(getByLabelText("Job"), { target: { value: "JOB-000001" } });
  fireEvent.change(getByLabelText("Ship-to state"), { target: { value: "IL" } });
  await waitFor(() => expect(getByText(/Illinois — 9\.00% sales tax/)).toBeTruthy());

  // Vendor select (the picker row button carries the vendor name).
  fireEvent.click(r.getByRole("button", { name: /Apex Racking/ }));

  // Two lines: 3 × $12.34 = $37.02 and 2 × $0.05 = $0.10.
  fireEvent.change(getByLabelText("Line 1 description"), { target: { value: "Rail 208in" } });
  fireEvent.change(getByLabelText("Line 1 quantity"), { target: { value: "3" } });
  fireEvent.change(getByLabelText("Line 1 unit cost"), { target: { value: "12.34" } });
  fireEvent.click(getByText("+ Add a line"));
  fireEvent.change(getByLabelText("Line 2 description"), { target: { value: "Hardware kit" } });
  fireEvent.change(getByLabelText("Line 2 quantity"), { target: { value: "2" } });
  fireEvent.change(getByLabelText("Line 2 unit cost"), { target: { value: "0.05" } });
}

describe("PoBuilderPage", () => {
  it("renders the tracker under cap.po.manage; write affordances hidden without the cap", async () => {
    vi.mocked(api.fetchPos).mockResolvedValue([poRow({})]);
    const withCap = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await waitFor(() => expect(withCap.getByText("Draft #1")).toBeTruthy());
    expect(withCap.getByText("+ New purchase order")).toBeTruthy();
    expect(withCap.getByText("Open")).toBeTruthy();
    withCap.unmount();

    vi.mocked(useAuth).mockReturnValue(authWith([]));
    const withoutCap = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await waitFor(() => expect(withoutCap.getByText("Draft #1")).toBeTruthy());
    expect(withoutCap.queryByText("+ New purchase order")).toBeNull();
    expect(withoutCap.queryByText("Open")).toBeNull();
    expect(withoutCap.queryByText("Cancel PO")).toBeNull();
  });

  it("mirrors the Worker's integer-cents math for display: 2-line fixture totals exactly", async () => {
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilderWithFixture(r);

    // Per-row extended: round(3 × 1234) = 3702, round(2 × 5) = 10.
    expect(r.getByText("$37.02")).toBeTruthy();
    expect(r.getByText("$0.10")).toBeTruthy();

    // Panel: subtotal 3712 · tax round(3712×900/10000)=334 · total 4046.
    const panel = r.getByLabelText("Totals panel") as HTMLElement;
    expect(within(panel).getByText("$37.12")).toBeTruthy();
    expect(within(panel).getByText("Tax (9.00%)")).toBeTruthy();
    expect(within(panel).getByText("$3.34")).toBeTruthy();
    expect(within(panel).getByText("$40.46")).toBeTruthy();
  });

  it("suggests the job number from the YYYY.NNN project-name prefix (editable)", async () => {
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New purchase order"));
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    const jobNo = r.getByLabelText("Job number (YYYY.NNN)") as HTMLInputElement;
    expect(jobNo.value).toBe("2023.126");
    fireEvent.change(jobNo, { target: { value: "bogus" } });
    expect(r.getByText(/must look like 2023\.126/)).toBeTruthy();
  });

  it("the variant toggle swaps the line-grid column set (default ↔ per-watt)", async () => {
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New purchase order"));

    expect(r.getByRole("columnheader", { name: "Unit cost" })).toBeTruthy();
    expect(r.queryByRole("columnheader", { name: "$/W" })).toBeNull();

    fireEvent.click(r.getByRole("button", { name: "Per watt" }));
    expect(r.getByRole("columnheader", { name: "$/W" })).toBeTruthy();
    expect(r.getByRole("columnheader", { name: "Watts" })).toBeTruthy();
    expect(r.getByRole("columnheader", { name: "Pallets" })).toBeTruthy();
    expect(r.queryByRole("columnheader", { name: "Unit cost" })).toBeNull();

    // Per-watt extended: round(1000 W × 350000 µ¢/W ÷ 1e6) = 350 cents.
    fireEvent.change(r.getByLabelText("Line 1 description"), { target: { value: "Modules" } });
    fireEvent.change(r.getByLabelText("Line 1 watts"), { target: { value: "1000" } });
    fireEvent.change(r.getByLabelText("Line 1 price per watt"), { target: { value: "0.35" } });
    expect(r.getByText("$350.00")).toBeTruthy();
  });

  it("pick from catalog populates a line's part number + description; qty/unit-cost stay operator-entered", async () => {
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New purchase order"));
    await waitFor(() => expect(api.fetchPoMaterials).toHaveBeenCalled());

    // The per-row picker is present (the catalog loaded); pick a TYPE for line 1.
    const pick = await waitFor(() => r.getByLabelText("Line 1 pick from catalog") as HTMLSelectElement);
    fireEvent.change(pick, { target: { value: "11" } });

    // Identity fields populate from the catalog TYPE (catalogLineFields — the REAL fn via
    // importOriginal): part_number ← model_id, description ← manufacturer + model + key_specs.
    expect((r.getByLabelText("Line 1 part number") as HTMLInputElement).value).toBe("Q.PEAK_DUO_XL-G11.3_BFG");
    expect((r.getByLabelText("Line 1 description") as HTMLInputElement).value).toBe(
      "Qcells Q.PEAK_DUO_XL-G11.3_BFG — 570-585Wp bifacial",
    );
    // The catalog carries no price — qty/unit cost are untouched (free-form entry is the fallback).
    expect((r.getByLabelText("Line 1 quantity") as HTMLInputElement).value).toBe("");
    expect((r.getByLabelText("Line 1 unit cost") as HTMLInputElement).value).toBe("");
  });

  it("generate sends the displayed totals; a totals_mismatch 409 re-renders from `recomputed`", async () => {
    const savedTotals: api.PoTotals = { subtotal_cents: 3712, tax_rate_bp: 900, tax_cents: 334, total_cents: 4046 };
    vi.mocked(api.createDraft).mockResolvedValue({ id: 7, totals: savedTotals });
    vi.mocked(api.generateDraft).mockResolvedValue({
      ok: false,
      error: "totals_mismatch",
      recomputed: { subtotal_cents: 9999, tax_rate_bp: 900, tax_cents: 900, total_cents: 10899 },
    });
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilderWithFixture(r);

    fireEvent.click(r.getByText("Generate PO"));

    // Save-then-generate: the draft persists first, then generate carries the DISPLAYED cents
    // (the Worker's own totals for that snapshot).
    await waitFor(() => expect(api.createDraft).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(api.generateDraft).toHaveBeenCalledWith(
        7,
        expect.objectContaining({ subtotal_cents: 3712, tax_cents: 334, total_cents: 4046 }),
      ),
    );

    // The refusal banner + the panel re-rendered from the server's recomputed money.
    await waitFor(() => expect(r.getByText(/recomputed totals differ/)).toBeTruthy());
    const panel = r.getByLabelText("Totals panel") as HTMLElement;
    expect(within(panel).getByText("$99.99")).toBeTruthy(); // recomputed subtotal
    expect(within(panel).getByText("$108.99")).toBeTruthy(); // recomputed total
  });

  it("a successful generate returns to the tracker with the PO number", async () => {
    const savedTotals: api.PoTotals = { subtotal_cents: 3712, tax_rate_bp: 900, tax_cents: 334, total_cents: 4046 };
    vi.mocked(api.createDraft).mockResolvedValue({ id: 7, totals: savedTotals });
    vi.mocked(api.generateDraft).mockResolvedValue({
      ok: true,
      id: 7,
      po_number: "2023.126.0.0",
      revision: 0,
      totals: savedTotals,
    });
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilderWithFixture(r);

    fireEvent.click(r.getByText("Generate PO"));
    await waitFor(() => expect(r.getByText(/PO 2023\.126\.0\.0 generated/)).toBeTruthy());
    expect(r.getByText("+ New purchase order")).toBeTruthy(); // back on the tracker
  });

  it("tracker actions follow the state machine: sent → Supersede (opens the clone draft)", async () => {
    vi.mocked(api.fetchPos).mockResolvedValue([
      poRow({ id: 3, status: "sent", po_number: "2023.126.0.0" }),
      poRow({ id: 4, status: "queued" }),
    ]);
    vi.mocked(api.supersedePo).mockResolvedValue({ ok: true, id: 9 });
    vi.mocked(api.fetchPo).mockResolvedValue({
      po: {
        ...poRow({ id: 9, status: "draft", supersedes_po_id: 3 }),
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
        terms_profile_id: "standard_17",
        terms_version: "1",
        subtotal_cents: 3712,
        tax_mode: "auto",
        tax_rate_bp: 900,
        tax_cents: 334,
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
    });
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await waitFor(() => expect(r.getByText("2023.126.0.0")).toBeTruthy());

    // The queued row offers Cancel (two-step), never Supersede; the sent row the reverse.
    const queuedCard = r.getByText("Draft #4").closest(".card") as HTMLElement;
    expect(within(queuedCard).getByText("Cancel PO")).toBeTruthy();
    expect(within(queuedCard).queryByText("Supersede")).toBeNull();
    const sentCard = r.getByText("2023.126.0.0").closest(".card") as HTMLElement;
    expect(within(sentCard).queryByText("Cancel PO")).toBeNull();

    fireEvent.click(within(sentCard).getByText("Supersede"));
    await waitFor(() => expect(api.supersedePo).toHaveBeenCalledWith(3));
    // The clone draft opened in the builder, carrying the supersession marker.
    await waitFor(() => expect(r.getByText("Supersedes PO #3")).toBeTruthy());
    expect(r.getByText("Editing draft #9")).toBeTruthy();
  });

  it("a DRAFT card offers a two-step Delete (not Cancel); confirming calls deletePoDraft + reloads", async () => {
    vi.mocked(api.fetchPos).mockResolvedValue([poRow({ id: 9, status: "draft" })]);
    vi.mocked(api.deletePoDraft).mockResolvedValue(undefined);
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    const card = (await waitFor(() => r.getByText("Draft #9"))).closest(".card") as HTMLElement;
    // A draft shows Delete (hard delete), NOT the soft Cancel PO.
    expect(within(card).getByText("Delete")).toBeTruthy();
    expect(within(card).queryByText("Cancel PO")).toBeNull();
    fireEvent.click(within(card).getByText("Delete"));
    fireEvent.click(within(card).getByText("Confirm delete"));
    await waitFor(() => expect(api.deletePoDraft).toHaveBeenCalledWith(9));
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalledTimes(2)); // reload-after-delete
  });
});

describe("PoBuilderPage — ship-to auto-fill (S6 follow-up)", () => {
  // The routing SoR carries a single `address` line (no structured city/state/zip), so those
  // three come back empty from the Worker — matching the real GET /api/po/jobs/:id/ship-to shape.
  const FILLED: api.JobShipTo = {
    job_id: "JOB-000001",
    job_no: "2023.126",
    site_phase: 0,
    ship_to_name: "2023.126 Kendall Solar",
    ship_to_address: "742 Panel Way",
    ship_to_city: "",
    ship_to_state: "",
    ship_to_zip: "",
    delivery_contact_name: "Riley Receiver",
    delivery_contact_phone: "555-0142",
    delivery_contact_email: "riley@site.example",
  };

  async function openBuilder(r: ReturnType<typeof render>): Promise<void> {
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New purchase order"));
  }

  it("populates the ship-to address + delivery contact from the routing SoR on job select", async () => {
    vi.mocked(api.fetchJobShipTo).mockResolvedValue(FILLED);
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() => expect(api.fetchJobShipTo).toHaveBeenCalledWith("JOB-000001"));
    await waitFor(() => expect((r.getByLabelText("Address") as HTMLInputElement).value).toBe("742 Panel Way"));
    expect((r.getByLabelText("Site / receiving name") as HTMLInputElement).value).toBe("2023.126 Kendall Solar");
    expect((r.getByLabelText("Name") as HTMLInputElement).value).toBe("Riley Receiver");
    expect((r.getByLabelText("Phone") as HTMLInputElement).value).toBe("555-0142");
    expect((r.getByLabelText("Email") as HTMLInputElement).value).toBe("riley@site.example");
    // City/State/ZIP aren't in the SoR → left blank for the operator to fill.
    expect((r.getByLabelText("City") as HTMLInputElement).value).toBe("");
    expect((r.getByLabelText("Ship-to state") as HTMLInputElement).value).toBe("");
    expect((r.getByLabelText("ZIP") as HTMLInputElement).value).toBe("");
  });

  it("keeps auto-filled fields editable (auto-fill is a convenience, not a lock)", async () => {
    vi.mocked(api.fetchJobShipTo).mockResolvedValue(FILLED);
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() => expect((r.getByLabelText("Address") as HTMLInputElement).value).toBe("742 Panel Way"));
    // Override the auto-filled address; the input accepts the edit.
    fireEvent.change(r.getByLabelText("Address"), { target: { value: "1 Override Rd" } });
    expect((r.getByLabelText("Address") as HTMLInputElement).value).toBe("1 Override Rd");
    // And type the city/state the SoR didn't supply (state upper-cases, drives auto tax).
    fireEvent.change(r.getByLabelText("City"), { target: { value: "Rockford" } });
    fireEvent.change(r.getByLabelText("Ship-to state"), { target: { value: "il" } });
    expect((r.getByLabelText("City") as HTMLInputElement).value).toBe("Rockford");
    expect((r.getByLabelText("Ship-to state") as HTMLInputElement).value).toBe("IL");
  });

  it("degrades silently when the ship-to read fails (list-derived fills stand, no crash)", async () => {
    vi.mocked(api.fetchJobShipTo).mockRejectedValue(new Error("403"));
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() => expect(api.fetchJobShipTo).toHaveBeenCalled());
    // The /api/jobs-derived fills still applied; the SoR-only fields stay blank; nothing threw.
    expect((r.getByLabelText("Site / receiving name") as HTMLInputElement).value).toBe("2023.126 Kendall Solar");
    expect((r.getByLabelText("Job number (YYYY.NNN)") as HTMLInputElement).value).toBe("2023.126");
    expect((r.getByLabelText("Address") as HTMLInputElement).value).toBe("");
    expect((r.getByLabelText("Name") as HTMLInputElement).value).toBe("");
  });

  // ── 0064: the Site/phase auto-fill ───────────────────────────────────────────────────────────
  // The whole user-visible payload of the split. Without it the operator re-types the site by
  // hand on every PO and a slip composes a document number for the WRONG site of the project.
  it("0064: picking a job auto-fills Site / phase from the job record", async () => {
    vi.mocked(fetchJobs).mockResolvedValue([
      { job_id: "JOB-000001", project_name: "MH405", job_no: "2026.384", site_phase: 1 },
    ]);
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() =>
      expect((r.getByLabelText("Job number (YYYY.NNN)") as HTMLInputElement).value).toBe("2026.384"),
    );
    // Together these compose 2026.384.1.<supersede>.<revision> — MH405's real PO number.
    expect((r.getByLabelText("Site / phase") as HTMLInputElement).value).toBe("1");
  });

  it("0064: a job with no site resets Site / phase to 0 rather than keeping the previous job's", async () => {
    vi.mocked(fetchJobs).mockResolvedValue([
      { job_id: "JOB-000001", project_name: "MH405", job_no: "2026.384", site_phase: 2 },
      { job_id: "JOB-000002", project_name: "Legacy", job_no: "2026.123", site_phase: 0 },
    ]);
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() => expect((r.getByLabelText("Site / phase") as HTMLInputElement).value).toBe("2"));
    // Switching jobs must not carry the old site across — that would silently mis-number the PO.
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000002" } });
    await waitFor(() => expect((r.getByLabelText("Site / phase") as HTMLInputElement).value).toBe("0"));
  });

  it("0064: with no stored job_no, the site comes from the NAME prefix and is never truncated away", async () => {
    vi.mocked(fetchJobs).mockResolvedValue([
      { job_id: "JOB-000001", project_name: "2026.384.1 Coker Solar", job_no: "", site_phase: 0 },
    ]);
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() =>
      expect((r.getByLabelText("Job number (YYYY.NNN)") as HTMLInputElement).value).toBe("2026.384"),
    );
    expect((r.getByLabelText("Site / phase") as HTMLInputElement).value).toBe("1");
  });
});

describe("PoBuilderPage — configured delivery-contact suggestions (Feature C)", () => {
  async function openBuilder(r: ReturnType<typeof render>): Promise<void> {
    await waitFor(() => expect(api.fetchPos).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New purchase order"));
  }

  it("attaches a <datalist> option per configured contact to the delivery-contact name input", async () => {
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    await waitFor(() => expect(api.fetchPoConfig).toHaveBeenCalled());
    const name = r.getByLabelText("Name") as HTMLInputElement;
    await waitFor(() => expect(name.getAttribute("list")).toBe("po-delivery-contact-options"));
    const options = r.container.querySelectorAll("#po-delivery-contact-options option");
    expect(Array.from(options).map((o) => (o as HTMLOptionElement).value)).toEqual([
      "Pat Yardman",
      "Dana Dockside",
    ]);
  });

  it("a configured-name entry auto-fills phone + email; free text never blocked, never filled", async () => {
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    await waitFor(() => expect(api.fetchPoConfig).toHaveBeenCalled());
    // Free text first: no fill happens, the value stands (suggestion-only, never a gate).
    fireEvent.change(r.getByLabelText("Name"), { target: { value: "Somebody Unlisted" } });
    expect((r.getByLabelText("Name") as HTMLInputElement).value).toBe("Somebody Unlisted");
    expect((r.getByLabelText("Phone") as HTMLInputElement).value).toBe("");
    expect((r.getByLabelText("Email") as HTMLInputElement).value).toBe("");
    // A match (as a datalist pick produces) fills phone + email from the entry.
    fireEvent.change(r.getByLabelText("Name"), { target: { value: "Pat Yardman" } });
    expect((r.getByLabelText("Phone") as HTMLInputElement).value).toBe("555-0177");
    expect((r.getByLabelText("Email") as HTMLInputElement).value).toBe("pat@yard.example");
    // The filled values stay operator-editable (convenience, not a lock).
    fireEvent.change(r.getByLabelText("Phone"), { target: { value: "555-9999" } });
    expect((r.getByLabelText("Phone") as HTMLInputElement).value).toBe("555-9999");
  });

  it("the match is case-INSENSITIVE (aligns with the config editor's case-insensitive dedupe)", async () => {
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    await waitFor(() => expect(api.fetchPoConfig).toHaveBeenCalled());
    // A saved "Pat Yardman" must still autofill on a differing-case typed "pat yardman" —
    // the list can't hold two entries differing only by case, so this is unambiguous.
    fireEvent.change(r.getByLabelText("Name"), { target: { value: "  pat YARDMAN  " } });
    expect((r.getByLabelText("Phone") as HTMLInputElement).value).toBe("555-0177");
    expect((r.getByLabelText("Email") as HTMLInputElement).value).toBe("pat@yard.example");
  });

  it("a configured contact with an empty field never wipes an already-entered value", async () => {
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    await waitFor(() => expect(api.fetchPoConfig).toHaveBeenCalled());
    // Operator (or the job-stakeholder auto-fill) already entered an email.
    fireEvent.change(r.getByLabelText("Email"), { target: { value: "kept@site.example" } });
    // "Dana Dockside" is configured with a phone but NO email — the empty field must not wipe.
    fireEvent.change(r.getByLabelText("Name"), { target: { value: "Dana Dockside" } });
    expect((r.getByLabelText("Phone") as HTMLInputElement).value).toBe("555-0188");
    expect((r.getByLabelText("Email") as HTMLInputElement).value).toBe("kept@site.example");
  });

  it("renders no datalist when no contacts are configured", async () => {
    vi.mocked(api.fetchPoConfig).mockResolvedValue({ ...CONFIG, delivery_contacts: [] });
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    await waitFor(() => expect(api.fetchPoConfig).toHaveBeenCalled());
    expect(r.container.querySelector("#po-delivery-contact-options")).toBeNull();
    expect((r.getByLabelText("Name") as HTMLInputElement).getAttribute("list")).toBeNull();
    // Free text still works with an empty list.
    fireEvent.change(r.getByLabelText("Name"), { target: { value: "Anyone At All" } });
    expect((r.getByLabelText("Name") as HTMLInputElement).value).toBe("Anyone At All");
  });

  it("the job-stakeholder auto-fill stays as-is alongside the configured suggestions (additive)", async () => {
    vi.mocked(api.fetchJobShipTo).mockResolvedValue({
      job_id: "JOB-000001",
      job_no: "2023.126",
      site_phase: 0,
      ship_to_name: "2023.126 Kendall Solar",
      ship_to_address: "742 Panel Way",
      ship_to_city: "",
      ship_to_state: "",
      ship_to_zip: "",
      delivery_contact_name: "Stakeholder Sam", // NOT a configured contact
      delivery_contact_phone: "555-0100",
      delivery_contact_email: "sam@stakeholder.example",
    });
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilder(r);
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() => expect((r.getByLabelText("Name") as HTMLInputElement).value).toBe("Stakeholder Sam"));
    // The stakeholder fill landed untouched by the config list…
    expect((r.getByLabelText("Phone") as HTMLInputElement).value).toBe("555-0100");
    expect((r.getByLabelText("Email") as HTMLInputElement).value).toBe("sam@stakeholder.example");
    // …and the configured suggestions remain available on top (additive, not a replacement).
    expect(r.container.querySelectorAll("#po-delivery-contact-options option").length).toBe(2);
    fireEvent.change(r.getByLabelText("Name"), { target: { value: "Pat Yardman" } });
    expect((r.getByLabelText("Phone") as HTMLInputElement).value).toBe("555-0177");
  });
});

describe("PoBuilderPage — attachments (Feature B)", () => {
  const savedTotals: api.PoTotals = { subtotal_cents: 3712, tax_rate_bp: 900, tax_cents: 334, total_cents: 4046 };

  it("gates the upload on a SAVED draft: save-first hint before, file input + empty list after", async () => {
    vi.mocked(api.createDraft).mockResolvedValue({ id: 7, totals: savedTotals });
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilderWithFixture(r);

    expect(r.getByText(/Save the draft first/)).toBeTruthy();
    expect(r.queryByLabelText("Attach files")).toBeNull();

    fireEvent.click(r.getByText("Save draft"));
    await waitFor(() => expect(r.getByLabelText("Attach files")).toBeTruthy());
    expect(r.getByText("No attachments yet.")).toBeTruthy();
  });

  it("uploads a picked file over the base64 wire and renders the refreshed list", async () => {
    vi.mocked(api.createDraft).mockResolvedValue({ id: 7, totals: savedTotals });
    vi.mocked(api.uploadPoAttachment).mockResolvedValue({ id: 1 });
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilderWithFixture(r);
    fireEvent.click(r.getByText("Save draft"));
    await waitFor(() => expect(r.getByLabelText("Attach files")).toBeTruthy());

    vi.mocked(api.fetchPoAttachments).mockResolvedValue([
      { id: 1, filename: "spec.pdf", declared_mime: "application/pdf", size_bytes: 4, status: "pending", created_at: 1_780_000_000 },
    ]);
    // %PDF header bytes → base64 "JVBERg==" (the no-data:-prefix wire contract).
    const file = new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], "spec.pdf", { type: "application/pdf" });
    fireEvent.change(r.getByLabelText("Attach files"), { target: { files: [file] } });
    await waitFor(() =>
      expect(api.uploadPoAttachment).toHaveBeenCalledWith(7, "spec.pdf", "application/pdf", "JVBERg=="),
    );
    await waitFor(() => expect(r.getByText("spec.pdf")).toBeTruthy());
    expect(r.getByText("Awaiting screening")).toBeTruthy();
  });

  it("refuses a disallowed extension client-side (hint only — the Worker is the real gate)", async () => {
    vi.mocked(api.createDraft).mockResolvedValue({ id: 7, totals: savedTotals });
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilderWithFixture(r);
    fireEvent.click(r.getByText("Save draft"));
    await waitFor(() => expect(r.getByLabelText("Attach files")).toBeTruthy());

    const bad = new File([new Uint8Array([0x4d, 0x5a])], "evil.exe", { type: "application/octet-stream" });
    fireEvent.change(r.getByLabelText("Attach files"), { target: { files: [bad] } });
    await waitFor(() => expect(r.getByText(/isn't an allowed type/)).toBeTruthy());
    expect(api.uploadPoAttachment).not.toHaveBeenCalled();
  });

  it("removes an attachment from the draft via its remove button", async () => {
    vi.mocked(api.createDraft).mockResolvedValue({ id: 7, totals: savedTotals });
    vi.mocked(api.deletePoAttachment).mockResolvedValue(undefined);
    const r = render(<PoBuilderPage onReviewEstimate={() => {}} onOpenEstimatesTab={() => {}} />);
    await openBuilderWithFixture(r);
    vi.mocked(api.fetchPoAttachments).mockResolvedValue([
      { id: 5, filename: "drawing.png", declared_mime: "image/png", size_bytes: 2048, status: "pending", created_at: 1_780_000_000 },
    ]);
    fireEvent.click(r.getByText("Save draft"));
    await waitFor(() => expect(r.getByLabelText("Attach files")).toBeTruthy());
    // Seed the list through an upload-free path: re-render via remove flow needs rows —
    // simulate by uploading nothing and refreshing through the remove handler's fetch.
    // (The list populated on the next fetch; drive it via a file pick.)
    vi.mocked(api.uploadPoAttachment).mockResolvedValue({ id: 5 });
    const file = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "drawing.png", { type: "image/png" });
    fireEvent.change(r.getByLabelText("Attach files"), { target: { files: [file] } });
    await waitFor(() => expect(r.getByText("drawing.png")).toBeTruthy());

    vi.mocked(api.fetchPoAttachments).mockResolvedValue([]);
    fireEvent.click(r.getByLabelText("Remove attachment drawing.png"));
    await waitFor(() => expect(api.deletePoAttachment).toHaveBeenCalledWith(7, 5));
    await waitFor(() => expect(r.getByText("No attachments yet.")).toBeTruthy());
  });
});
