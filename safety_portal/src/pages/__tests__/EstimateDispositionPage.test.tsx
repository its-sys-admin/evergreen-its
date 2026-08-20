/**
 * Estimate disposition screen (ADR-0004 E3) — first page-level coverage, added with the
 * GridViewport wiring. What this pins:
 *   • the EXTRACTED-lines accept/reject grid rides the resizable sticky-header viewport
 *     (it is the 200-line quote case), while the manual Tier-3 entry table does NOT —
 *     manual rows are a handful and a resize bar there would be noise;
 *   • the viewport persists under its OWN key (estimate-lines), never bleeding into the
 *     manifest screen's manifest-rows key.
 *
 * Mocks the lib modules (the page-test convention — pages never stub global.fetch);
 * formatCents/parseDollarsToCents stay REAL so rendered money is the real formatting.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/estimates", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/estimates")>();
  return {
    ...actual,
    fetchEstimate: vi.fn(),
    disposeEstimate: vi.fn(),
  };
});
vi.mock("../../lib/po", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/po")>();
  return {
    ...actual,
    fetchVendors: vi.fn(),
    fetchJobShipTo: vi.fn(),
    createDraft: vi.fn(),
    deletePoDraft: vi.fn(),
  };
});
vi.mock("../../lib/rfq", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/rfq")>();
  return { ...actual, fetchRfq: vi.fn() };
});

import * as est from "../../lib/estimates";
import * as po from "../../lib/po";
import { EstimateDispositionPage } from "../EstimateDispositionPage";

const ESTIMATE: est.EstimateRow = {
  job_id: "JOB-1",
  id: 42,
  est_uuid: "u-est-42",
  job_no: "2026.384",
  job_name: "MH405",
  vendor_key: "acme",
  filename: "acme-quote.pdf",
  declared_mime: "application/pdf",
  size_bytes: 4096,
  sha256: "deadbeef",
  status: "extracted",
  doc_type: "quote",
  detail: null,
  uploaded_by: "office.admin",
  box_file_id: "box-9",
  family_key: null,
  supersedes_estimate_id: null,
  po_id: null,
  rfq_id: null,
  rfq_vendor_key: null,
  created_at: 1,
  screened_at: 2,
  extracted_at: 3,
  disposed_at: null,
};

const EXTRACTION: est.EstimateExtraction = {
  id: 7,
  estimate_id: 42,
  tier: 1,
  schema_version: "1",
  doc_type: "quote",
  vendor_name: "Acme Supply",
  quote_number: "Q-100",
  revision_label: null,
  quote_date: null,
  valid_until: null,
  subtotal_cents: 30000,
  tax_cents: null,
  freight_cents: null,
  misc_cents: null,
  grand_total_cents: 30000,
  math_ok: 1,
  confidence: 0.9,
  anomalies: null,
  created_at: 4,
};

const LINES: est.ExtractionLine[] = [
  {
    id: 1, position: 1, section: null, part_number: "7006955", description: "Pile cap",
    qty: 4, unit: "EA", unit_cost_cents: 2500, extended_cents: 10000, math_ok: 1,
    line_note: null, disposition: "pending", edited_json: null,
  },
  {
    id: 2, position: 2, section: null, part_number: "7000153", description: "Bracket",
    qty: 8, unit: "EA", unit_cost_cents: 2500, extended_cents: 20000, math_ok: 1,
    line_note: null, disposition: "pending", edited_json: null,
  },
];

function mount() {
  vi.mocked(est.fetchEstimate).mockResolvedValue({
    estimate: ESTIMATE,
    extraction: EXTRACTION,
    lines: LINES,
    preview_count: 0,
  });
  vi.mocked(po.fetchVendors).mockResolvedValue([]);
  vi.mocked(po.fetchJobShipTo).mockResolvedValue({
    job_id: "JOB-1", job_no: "2026.384", site_phase: 1,
    ship_to_name: "", ship_to_address: "", ship_to_city: "", ship_to_state: "CA",
    ship_to_zip: "", delivery_contact_name: "", delivery_contact_phone: "",
    delivery_contact_email: "",
  });
  return render(<EstimateDispositionPage estimateId={42} onClose={vi.fn()} />);
}

// The SPA jsdom env doesn't reliably provide localStorage (Node's experimental global
// shadows jsdom's — the draftCache.test.ts note). GridViewport persists its height there,
// so install a fresh in-memory Storage per case to keep them isolated.
function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => m.clear(),
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    key: (i: number) => Array.from(m.keys())[i] ?? null,
    removeItem: (k: string) => void m.delete(k),
    setItem: (k: string, v: string) => void m.set(k, String(v)),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("localStorage", memoryStorage());
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("EstimateDispositionPage", () => {
  it("puts the extracted-lines grid — and ONLY it — inside a GridViewport", async () => {
    const { container, getByLabelText } = mount();
    await waitFor(() => getByLabelText("Accept line 1"));
    const extracted = container.querySelector('section[aria-label="Extracted lines"]');
    const manual = container.querySelector('section[aria-label="Manual lines"]');
    expect(extracted?.querySelector(".gridvp .dash-table"),
      "the accept/reject grid is the 200-line case — it gets the viewport").toBeTruthy();
    expect(manual?.querySelector(".gridvp"),
      "manual entry is a handful of rows — no viewport").toBeNull();
  });

  it("persists its height under estimate-lines, never the manifest screen's key", async () => {
    const { getByRole, getByLabelText } = mount();
    await waitFor(() => getByLabelText("Accept line 1"));
    fireEvent.click(getByRole("button", { name: "Tall" }));
    expect(localStorage.getItem("its-portal-gridvp:v1:estimate-lines")).toBe("tall");
    expect(localStorage.getItem("its-portal-gridvp:v1:manifest-rows")).toBeNull();
  });
});
