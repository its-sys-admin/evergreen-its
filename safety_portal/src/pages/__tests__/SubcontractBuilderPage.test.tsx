/**
 * Subcontract Builder page (SC-S5) — SPA render-smoke for the wizard + tracker. Covers: cap-gated
 * affordances, the live integer-cents SOV-sums-to-price gate (a 2-line fixture whose extendeds sum
 * to the contract price → "balances" badge; a mismatch → warn badge), the job_no suggestion parse,
 * the generate flow (sends {contract_price_cents}; a sov_mismatch 409 re-renders the gate from the
 * Worker's `recomputed`; a success returns to the tracker with the sc_number), and the tracker state
 * machine (sent AND executed both offer Supersede — the dual-source delta from PO; queued offers
 * Cancel). Lib network fns + auth are mocked (importOriginal keeps the REAL money math — formatCents
 * / sovExtendedCents / computeSubtotal / US_STATES / stateName are the code under test here).
 */
import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/subcontracts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/subcontracts")>();
  return {
    ...actual,
    fetchSubcontractors: vi.fn(),
    fetchSubConfig: vi.fn(),
    fetchSubTerms: vi.fn(),
    fetchSubDrafts: vi.fn(),
    fetchSubDraft: vi.fn(),
    createSubDraft: vi.fn(),
    updateSubDraft: vi.fn(),
    generateSubcontract: vi.fn(),
    supersedeSubcontract: vi.fn(),
    cancelSubcontract: vi.fn(),
    deleteSubDraft: vi.fn(),
    fetchExhibitTemplate: vi.fn(),
    fetchJobSiteAddress: vi.fn(),
    fetchTrades: vi.fn(),
  };
});
vi.mock("../../lib/api", () => ({ fetchJobs: vi.fn() }));
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as api from "../../lib/subcontracts";
import { fetchJobs } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { SubcontractBuilderPage } from "../SubcontractBuilderPage";

function authWith(capabilities: string[]) {
  return {
    user: { username: "office", role: "admin" as const, capabilities },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

const CONFIG: api.SubcontractConfig = {
  contractor: {
    entity: "Evergreen Renewables LLC",
    address_lines: ["100 Spectrum Center Dr. STE 570", "Irvine, CA. 92618"],
    phone: "888-303-6424",
    signature_entity: "Evergreen Renewables LLC",
    prime_contractor_default: "Evergreen Renewables LLC",
  },
  payment_terms: { retainage_bp: 1000, retainage_reduced_bp: 500, retainage_reduction_at_pct: 50 },
  // Small subset for the picker; must include CA (the fixture's governing-law state).
  governing_law_states: ["CA", "IL", "TX"],
};

const TERMS: api.TermsProfile[] = [
  {
    id: "standard_subcontract",
    kind: "library",
    label: "Standard Subcontract",
    description: "Evergreen's standard subcontract terms.",
    current_version: "1",
    tokens: [],
    render_line: null,
  },
];

const SUBS: api.Subcontractor[] = [
  {
    sub_key: "SUB-000001",
    sub_name: "Apex Electrical",
    address: "1 Volt Way",
    contact_name: "Sam Sparks",
    contact_email: "sam@apex.example",
    contact_phone: "555-0101",
    state: "CA",
    trades: ["Electrical"],
    default_terms_profile: "standard_subcontract",
    msa_reference: "",
    coi_reference: "",
    license_number: "C-10 12345",
    active: 1,
    notes: "",
    origin: "portal",
    sync_state: "synced",
    mirror_version: 1,
  },
];

const JOBS = [{ job_id: "JOB-000001", project_name: "2023.126 Kendall Solar", job_no: "", site_phase: 0 }];

function scRow(overrides: Partial<api.SubcontractListRow>): api.SubcontractListRow {
  return {
    id: 1,
    sc_number: null,
    job_no: "2023.126",
    site_phase: 0,
    supersede_seq: 0,
    revision: null,
    sub_key: "SUB-000001",
    job_id: "JOB-000001",
    job_name: "2023.126 Kendall Solar",
    project_name: "2023.126 Kendall Solar",
    owner_entity: "Kendall LLC",
    status: "draft",
    contract_price_cents: 3712,
    supersedes_sc_id: null,
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
  vi.mocked(useAuth).mockReturnValue(authWith(["cap.subcontracts.manage"]));
  vi.mocked(api.fetchSubDrafts).mockResolvedValue([]);
  vi.mocked(api.fetchSubcontractors).mockResolvedValue(SUBS);
  vi.mocked(api.fetchSubTerms).mockResolvedValue(TERMS);
  vi.mocked(api.fetchSubConfig).mockResolvedValue(CONFIG);
  vi.mocked(fetchJobs).mockResolvedValue(JOBS);
  vi.mocked(api.fetchJobSiteAddress).mockResolvedValue({ job_id: "JOB-000001", site_address: "500 Solar Way, Kendall CA" });
  vi.mocked(api.fetchTrades).mockResolvedValue([...api.TRADES]); // default = the static baseline
});

/** Open the builder and fill a valid 2-line fixture: 3 × $12.34 + 2 × $0.05 = $37.12, job CA,
 *  subcontractor Apex Electrical, contract price $37.12 (balances). */
async function openBuilderWithFixture(r: ReturnType<typeof render>) {
  const { getByText, getByLabelText, getByRole } = r;
  await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
  fireEvent.click(getByText("+ New subcontract"));

  fireEvent.change(getByLabelText("Job"), { target: { value: "JOB-000001" } }); // auto-fills Project name
  // Subcontractor pick (the picker row button carries the subcontractor name).
  fireEvent.click(getByRole("button", { name: /Apex Electrical/ }));
  fireEvent.change(getByLabelText("Governing-law state"), { target: { value: "CA" } });
  // Render-required party/scope fields (now flagged in validate()).
  fireEvent.change(getByLabelText("Trade"), { target: { value: "AC Electrical" } });
  fireEvent.change(getByLabelText("Owner entity"), { target: { value: "Bonacci 1, LLC" } });

  // Two lines: 3 × $12.34 = $37.02 and 2 × $0.05 = $0.10 → subtotal $37.12.
  fireEvent.change(getByLabelText("Line 1 description"), { target: { value: "Mobilization" } });
  fireEvent.change(getByLabelText("Line 1 quantity"), { target: { value: "3" } });
  fireEvent.change(getByLabelText("Line 1 unit price"), { target: { value: "12.34" } });
  fireEvent.click(getByText("+ Add a line"));
  fireEvent.change(getByLabelText("Line 2 description"), { target: { value: "Closeout" } });
  fireEvent.change(getByLabelText("Line 2 quantity"), { target: { value: "2" } });
  fireEvent.change(getByLabelText("Line 2 unit price"), { target: { value: "0.05" } });

  fireEvent.change(getByLabelText("Contract price dollars"), { target: { value: "37.12" } });
}

describe("SubcontractBuilderPage", () => {
  it("renders the tracker under cap.subcontracts.manage; write affordances hidden without the cap", async () => {
    vi.mocked(api.fetchSubDrafts).mockResolvedValue([scRow({})]);
    const withCap = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(withCap.getByText("Draft #1")).toBeTruthy());
    expect(withCap.getByText("+ New subcontract")).toBeTruthy();
    expect(withCap.getByText("Open")).toBeTruthy();
    withCap.unmount();

    vi.mocked(useAuth).mockReturnValue(authWith([]));
    const withoutCap = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(withoutCap.getByText("Draft #1")).toBeTruthy());
    expect(withoutCap.queryByText("+ New subcontract")).toBeNull();
    expect(withoutCap.queryByText("Open")).toBeNull();
    expect(withoutCap.queryByText("Cancel SC")).toBeNull();
  });

  it("mirrors the Worker's integer-cents SOV math and shows the sums-to-price gate", async () => {
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await openBuilderWithFixture(r);

    // Per-row extended: round(3 × 1234) = 3702, round(2 × 5) = 10.
    expect(r.getByText("$37.02")).toBeTruthy();
    expect(r.getByText("$0.10")).toBeTruthy();

    // The gate panel: subtotal $37.12 == contract price $37.12 → balances.
    const panel = r.getByLabelText("Contract price panel") as HTMLElement;
    expect(within(panel).getByText("SOV balances to the contract price")).toBeTruthy();
  });

  it("warns (and blocks generate) when the SOV subtotal ≠ the contract price", async () => {
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await openBuilderWithFixture(r);

    // Break the balance: contract price $50.00 ≠ SOV subtotal $37.12.
    fireEvent.change(r.getByLabelText("Contract price dollars"), { target: { value: "50.00" } });
    const panel = r.getByLabelText("Contract price panel") as HTMLElement;
    expect(within(panel).getByText(/adjust a line\s+or the price/)).toBeTruthy();

    // Generate is blocked client-side — the draft is never saved on a mismatch.
    fireEvent.click(r.getByText("Generate subcontract"));
    await waitFor(() => expect(r.getByText(/must add up to the contract price/)).toBeTruthy());
    expect(api.createSubDraft).not.toHaveBeenCalled();
    expect(api.generateSubcontract).not.toHaveBeenCalled();
  });

  it("blocks generate (client-side) when a render-required field is blank — Owner entity", async () => {
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await openBuilderWithFixture(r);
    // Clear the Owner entity the fixture filled — the render would otherwise permanently fence.
    fireEvent.change(r.getByLabelText("Owner entity"), { target: { value: "" } });
    fireEvent.click(r.getByText("Generate subcontract"));
    await waitFor(() => expect(r.getByText(/Enter the Owner entity/)).toBeTruthy());
    expect(api.createSubDraft).not.toHaveBeenCalled();
    expect(api.generateSubcontract).not.toHaveBeenCalled();
  });

  it("suggests the job number from the YYYY.NNN project-name prefix (editable)", async () => {
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    const jobNo = r.getByLabelText("Job number (YYYY.NNN)") as HTMLInputElement;
    expect(jobNo.value).toBe("2023.126");
    fireEvent.change(jobNo, { target: { value: "bogus" } });
    expect(r.getByText(/must look like 2023\.126/)).toBeTruthy();
  });

  it("generate sends {contract_price_cents}; a sov_mismatch 409 re-renders the gate from `recomputed`", async () => {
    vi.mocked(api.createSubDraft).mockResolvedValue({ id: 7, subtotal_cents: 3712 });
    vi.mocked(api.generateSubcontract).mockResolvedValue({
      ok: false,
      error: "sov_mismatch",
      recomputed: { subtotal_cents: 9999, contract_price_cents: 3712 },
    });
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await openBuilderWithFixture(r);

    fireEvent.click(r.getByText("Generate subcontract"));

    // Save-then-generate: the draft persists first, then generate carries the DISPLAYED contract price.
    await waitFor(() => expect(api.createSubDraft).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(api.generateSubcontract).toHaveBeenCalledWith(7, { contract_price_cents: 3712 }),
    );

    // The refusal banner + the gate re-rendered from the server's recomputed subtotal.
    await waitFor(() => expect(r.getByText(/doesn't add up to the contract price/)).toBeTruthy());
    const panel = r.getByLabelText("Contract price panel") as HTMLElement;
    expect(within(panel).getByText("$99.99")).toBeTruthy(); // recomputed subtotal
  });

  it("a successful generate returns to the tracker with the subcontract number", async () => {
    vi.mocked(api.createSubDraft).mockResolvedValue({ id: 7, subtotal_cents: 3712 });
    vi.mocked(api.generateSubcontract).mockResolvedValue({
      ok: true,
      id: 7,
      sc_number: "2023.126.0.0",
      revision: 0,
      subtotal_cents: 3712,
    });
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await openBuilderWithFixture(r);

    fireEvent.click(r.getByText("Generate subcontract"));
    await waitFor(() => expect(r.getByText(/Subcontract 2023\.126\.0\.0 generated/)).toBeTruthy());
    expect(r.getByText("+ New subcontract")).toBeTruthy(); // back on the tracker
  });

  it("selecting a trade with a blank Exhibit A fills Article II from the trade template", async () => {
    vi.mocked(api.fetchExhibitTemplate).mockResolvedValue({
      trade: "AC Electrical",
      template_key: "electrical",
      article_ii: "ARTICLE II — THE WORK (electrical scope).",
    });
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));

    // Exhibit A starts blank — the pre-fill precondition.
    expect((r.getByLabelText("Exhibit A work text") as HTMLTextAreaElement).value).toBe("");

    fireEvent.change(r.getByLabelText("Trade"), { target: { value: "AC Electrical" } });

    // The trade resolves to its Article II body; the textarea fills and the source hint shows.
    await waitFor(() => expect(api.fetchExhibitTemplate).toHaveBeenCalledWith("AC Electrical"));
    await waitFor(() =>
      expect((r.getByLabelText("Exhibit A work text") as HTMLTextAreaElement).value).toBe(
        "ARTICLE II — THE WORK (electrical scope).",
      ),
    );
    expect(r.getByText(/set from the AC Electrical template/)).toBeTruthy();
  });

  it("the Trade dropdown is fed by the served (manifest-derived) trade list, not a hardcoded one", async () => {
    // A trade the static baseline does NOT contain — proves the dropdown reads fetchTrades.
    vi.mocked(api.fetchTrades).mockResolvedValue([...api.TRADES, "Battery Storage"]);
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchTrades).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));
    await waitFor(() => expect(r.getByText("Battery Storage")).toBeTruthy()); // the new option renders
  });

  it("falls back to the static TRADES when the trades fetch degrades (dropdown never empties)", async () => {
    vi.mocked(api.fetchTrades).mockRejectedValue(new Error("network"));
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));
    await waitFor(() => expect(r.getByText(api.TRADES[0])).toBeTruthy()); // static baseline still populates
  });

  it("selecting a trade OVERWRITES existing Exhibit A text with the trade template (operator directive)", async () => {
    vi.mocked(api.fetchExhibitTemplate).mockResolvedValue({
      trade: "Civil",
      template_key: "civil",
      article_ii: "TEMPLATE BODY — replaces whatever was there.",
    });
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));

    // Operator authors Exhibit A first, THEN switches trade — the trade must REPLACE their text
    // (2026-07-12 directive: changing the dropdown overwrites whatever is in the Work box).
    fireEvent.change(r.getByLabelText("Exhibit A work text"), {
      target: { value: "Operator's bespoke scope." },
    });
    fireEvent.change(r.getByLabelText("Trade"), { target: { value: "Civil" } });

    // The template IS fetched and overwrites the prior text; the source hint shows.
    await waitFor(() => expect(api.fetchExhibitTemplate).toHaveBeenCalledWith("Civil"));
    await waitFor(() =>
      expect((r.getByLabelText("Exhibit A work text") as HTMLTextAreaElement).value).toBe(
        "TEMPLATE BODY — replaces whatever was there.",
      ),
    );
    expect(r.getByText(/set from the Civil template/)).toBeTruthy();
  });

  it("a failed trade-template fetch does NOT clobber existing Exhibit A text (no destructive empty overwrite)", async () => {
    vi.mocked(api.fetchExhibitTemplate).mockRejectedValue(new Error("unknown trade"));
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));

    fireEvent.change(r.getByLabelText("Exhibit A work text"), {
      target: { value: "Operator's bespoke scope." },
    });
    fireEvent.change(r.getByLabelText("Trade"), { target: { value: "Civil" } });

    // The fetch failed → no template to write → the operator's text stands, and no stale hint.
    await waitFor(() => expect(api.fetchExhibitTemplate).toHaveBeenCalledWith("Civil"));
    expect((r.getByLabelText("Exhibit A work text") as HTMLTextAreaElement).value).toBe(
      "Operator's bespoke scope.",
    );
    expect(r.queryByText(/set from the/)).toBeNull();
  });

  it("start and completion dates are native date pickers (type=date), not free text (C3)", async () => {
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));
    expect((r.getByLabelText("Start date") as HTMLInputElement).type).toBe("date");
    expect((r.getByLabelText("Completion date") as HTMLInputElement).type).toBe("date");
  });

  it("selecting a job auto-fills the Site address from the Smartsheet SoR (C1)", async () => {
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));
    expect((r.getByLabelText("Site address") as HTMLInputElement).value).toBe(""); // blank pre-select
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() => expect(api.fetchJobSiteAddress).toHaveBeenCalledWith("JOB-000001"));
    await waitFor(() =>
      expect((r.getByLabelText("Site address") as HTMLInputElement).value).toBe("500 Solar Way, Kendall CA"),
    );
  });

  it("a blank SoR site address does NOT clobber an operator-typed Site address (C1 degrade-to-manual)", async () => {
    vi.mocked(api.fetchJobSiteAddress).mockResolvedValue({ job_id: "JOB-000001", site_address: "" });
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalled());
    fireEvent.click(r.getByText("+ New subcontract"));
    fireEvent.change(r.getByLabelText("Site address"), { target: { value: "operator typed" } });
    fireEvent.change(r.getByLabelText("Job"), { target: { value: "JOB-000001" } });
    await waitFor(() => expect(api.fetchJobSiteAddress).toHaveBeenCalledWith("JOB-000001"));
    // Blank SoR → the operator's text stands (no empty overwrite).
    expect((r.getByLabelText("Site address") as HTMLInputElement).value).toBe("operator typed");
  });

  it("tracker state machine: sent AND executed both offer Supersede; queued offers Cancel", async () => {
    vi.mocked(api.fetchSubDrafts).mockResolvedValue([
      scRow({ id: 3, status: "sent", sc_number: "2023.126.0.0" }),
      scRow({ id: 5, status: "executed", sc_number: "2023.126.0.1" }),
      scRow({ id: 4, status: "queued" }),
    ]);
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    await waitFor(() => expect(r.getByText("2023.126.0.0")).toBeTruthy());

    const sentCard = r.getByText("2023.126.0.0").closest(".card") as HTMLElement;
    expect(within(sentCard).getByText("Supersede")).toBeTruthy();
    expect(within(sentCard).queryByText("Cancel SC")).toBeNull();

    // The dual-source delta from PO: an EXECUTED subcontract is also a supersede source.
    const executedCard = r.getByText("2023.126.0.1").closest(".card") as HTMLElement;
    expect(within(executedCard).getByText("Supersede")).toBeTruthy();

    const queuedCard = r.getByText("Draft #4").closest(".card") as HTMLElement;
    expect(within(queuedCard).getByText("Cancel SC")).toBeTruthy();
    expect(within(queuedCard).queryByText("Supersede")).toBeNull();
  });

  it("a DRAFT card offers a two-step Delete (not Cancel); confirming calls deleteSubDraft + reloads", async () => {
    vi.mocked(api.fetchSubDrafts).mockResolvedValue([scRow({ id: 9, status: "draft" })]);
    vi.mocked(api.deleteSubDraft).mockResolvedValue(undefined);
    const r = render(<SubcontractBuilderPage onBack={() => {}} />);
    const card = (await waitFor(() => r.getByText("Draft #9"))).closest(".card") as HTMLElement;
    // A draft shows Delete (hard delete), NOT the soft Cancel SC.
    expect(within(card).getByText("Delete")).toBeTruthy();
    expect(within(card).queryByText("Cancel SC")).toBeNull();
    // Two-step armed: Delete → Confirm delete.
    fireEvent.click(within(card).getByText("Delete"));
    fireEvent.click(within(card).getByText("Confirm delete"));
    await waitFor(() => expect(api.deleteSubDraft).toHaveBeenCalledWith(9));
    await waitFor(() => expect(api.fetchSubDrafts).toHaveBeenCalledTimes(2)); // reload-after-delete
  });
});
