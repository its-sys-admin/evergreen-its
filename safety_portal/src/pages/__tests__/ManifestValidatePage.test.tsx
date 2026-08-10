/**
 * Manifest validate screen (PR3b) — the human half of "the parser proposes, the human disposes".
 *
 * What this pins, and why each one is here rather than being obvious:
 *   • AMBIGUOUS part numbers BLOCK the commit until every one is decided. Duplicate part numbers
 *     are universal in the real BOMs, so a screen that let you commit through them would silently
 *     pick winners — that is the exact failure this screen exists to prevent, and it is the single
 *     most important assertion in this file.
 *   • Preview is a real dry run and writes nothing; Commit is disabled until you have previewed,
 *     so nobody imports a set they never checked against live data.
 *   • What you previewed IS what commits — both read the same resolved-lines derivation, so a
 *     column remap or a dropped row invalidates the stale plan rather than committing behind it.
 *   • A row with no description is refused BEFORE the round trip, with a count, rather than
 *     letting the Worker's `description_required` come back per row. The importer must never
 *     invent a description (§4), so the screen has to make the gap visible instead.
 *   • The commit drives the PAGED loop to completion and reports honest progress.
 *   • A non-parsed manifest cannot be committed from here at all.
 *
 * Mocks the lib module (the page-test convention — pages never stub global.fetch).
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_manifests", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_manifests")>();
  return {
    ...actual,
    fetchManifest: vi.fn(),
    fetchAllManifestRows: vi.fn(),
    fetchManifestPreview: vi.fn(),
    planManifest: vi.fn(),
    commitAll: vi.fn(),
    discardManifest: vi.fn(),
  };
});

import * as api from "../../lib/fieldops_manifests";
import { ManifestValidatePage } from "../ManifestValidatePage";

const MANIFEST = {
  id: 7,
  manifest_uuid: "u-man-7",
  job_id: "JOB-A",
  filename: "Bradley 1 Customer BOM.pdf",
  declared_mime: "application/pdf",
  size_bytes: 4096,
  status: "parsed" as const,
  detail: null,
  profile: "customer_bom",
  row_count: 3,
  mode: null,
  committed_through_row: 0,
  uploaded_by: "office.admin",
  box_file_id: "box-1",
  created_at: 1,
  parsed_at: 2,
  committed_at: null,
  column_map_json: JSON.stringify({
    mapping: { part_number: 0, description: 1, qty: 2 },
    labels: { "0": "PART NUMBER", "1": "DESCRIPTION", "2": "QTY" },
    qty_candidates: [2],
    qty_default: 2,
  }),
  header_meta_json: JSON.stringify({ CLIENT: "Evergreen" }),
  parse_notes: "revision cross-check: sum(quantities)+overage equals the chosen revision on 47/47 rows",
  merge_options_json: null,
};

const ROWS: api.ManifestGridRow[] = [
  { row_index: 1, source_page: "pdf:p1:t0", kind: "header", cells_json: '["PART NUMBER","DESCRIPTION","QTY"]', flags: null },
  { row_index: 2, source_page: "pdf:p1:t0", kind: "data", cells_json: '["7006955","Pile cap","4"]', flags: null },
  { row_index: 3, source_page: "pdf:p1:t0", kind: "data", cells_json: '["7000153","Bracket","2"]', flags: null },
];

const PLAN_CLEAN: api.ManifestPlanResponse = {
  ok: true, job_id: "JOB-A", committed_through_row: 0,
  counts: { incoming: 2, matched: 0, ambiguous: 0, new: 2, absent: 0, existing: 0 },
  matched: [], ambiguous: [], absent: [], projected_total: 2, would_exceed_line_cap: false,
};

const PLAN_AMBIGUOUS: api.ManifestPlanResponse = {
  ...PLAN_CLEAN,
  counts: { incoming: 2, matched: 0, ambiguous: 1, new: 1, absent: 0, existing: 2 },
  ambiguous: [{ source_row_index: 3, part_number: "7000153", line_ids: [11, 12] }],
};

type ManifestOverride = Partial<Omit<typeof MANIFEST, "status" | "detail">> & {
  status?: api.ManifestStatus;
  detail?: string | null;
};

function mount(overrides: ManifestOverride = {}, onClose = vi.fn()) {
  vi.mocked(api.fetchManifest).mockResolvedValue({
    manifest: { ...MANIFEST, ...overrides },
    preview_pages: [1],
  } as unknown as api.ManifestDetailResponse);
  vi.mocked(api.fetchAllManifestRows).mockResolvedValue(ROWS);
  vi.mocked(api.fetchManifestPreview).mockResolvedValue({ page: 1, png_b64: "QUJD" });
  const r = render(<ManifestValidatePage manifestId={7} onClose={onClose} />);
  return { ...r, onClose };
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(cleanup);

describe("ManifestValidatePage", () => {
  it("shows the parser's evidence for its proposed quantity column", async () => {
    const { container } = mount();
    await waitFor(() => expect(container.textContent ?? "").toContain("Bradley 1 Customer BOM.pdf"));
    // The DELTA arithmetic cross-check is what makes the pre-selection a reasoned proposal rather
    // than a guess to rubber-stamp — it must be on screen, not buried in a log.
    expect(container.textContent ?? "").toContain("47/47 rows");
    expect(container.textContent ?? "").toContain("Evergreen");
  });

  it("seeds the column mapping FROM the parser's proposal", async () => {
    const { getByLabelText } = mount();
    await waitFor(() => getByLabelText("Import column PART NUMBER as"));
    expect((getByLabelText("Import column PART NUMBER as") as HTMLSelectElement).value).toBe("part_number");
    expect((getByLabelText("Import column DESCRIPTION as") as HTMLSelectElement).value).toBe("description");
    expect((getByLabelText("Import column QTY as") as HTMLSelectElement).value).toBe("qty");
  });

  it("requires a preview before the import button enables", async () => {
    const { getByText, getByRole } = mount();
    await waitFor(() => getByText("Preview changes"));
    const importBtn = getByRole("button", { name: /^Import 2 lines$/ }) as HTMLButtonElement;
    expect(importBtn.disabled).toBe(true);

    vi.mocked(api.planManifest).mockResolvedValue(PLAN_CLEAN);
    fireEvent.click(getByText("Preview changes"));
    await waitFor(() => expect(importBtn.disabled).toBe(false));
    expect(api.commitAll).not.toHaveBeenCalled(); // a dry run writes nothing
  });

  it("BLOCKS the import while an ambiguous part number is undecided", async () => {
    // THE assertion. A part number matching more than one existing line has no single correct
    // target; committing through it would silently pick a winner.
    const { getByText, getByRole, getByLabelText, container } = mount();
    await waitFor(() => getByText("Preview changes"));
    vi.mocked(api.planManifest).mockResolvedValue(PLAN_AMBIGUOUS);
    fireEvent.click(getByText("Preview changes"));

    await waitFor(() =>
      expect(container.textContent ?? "").toContain("match more than one existing line"),
    );
    const importBtn = getByRole("button", { name: /^Import 2 lines$/ }) as HTMLButtonElement;
    expect(importBtn.disabled, "commit must stay blocked until every ambiguity is decided").toBe(true);
    expect(container.textContent ?? "").toContain("1 ambiguous part number still to decide");

    fireEvent.change(getByLabelText("Which existing line for row 3"), { target: { value: "11" } });
    await waitFor(() => expect(importBtn.disabled).toBe(false));
  });

  it("invalidates a stale preview when the mapping changes", async () => {
    // What you previewed IS what commits. Remapping a column changes the resolved lines, so the
    // old plan must stop authorising a commit.
    const { getByText, getByRole, getByLabelText } = mount();
    await waitFor(() => getByText("Preview changes"));
    vi.mocked(api.planManifest).mockResolvedValue(PLAN_CLEAN);
    fireEvent.click(getByText("Preview changes"));
    const importBtn = getByRole("button", { name: /^Import 2 lines$/ }) as HTMLButtonElement;
    await waitFor(() => expect(importBtn.disabled).toBe(false));

    fireEvent.change(getByLabelText("Import column DESCRIPTION as"), { target: { value: "ignore" } });
    await waitFor(() => expect(importBtn.disabled).toBe(true));
  });

  it("refuses description-less lines up front rather than per row from the Worker", async () => {
    const { getByLabelText, container } = mount();
    await waitFor(() => getByLabelText("Import column DESCRIPTION as"));
    fireEvent.change(getByLabelText("Import column DESCRIPTION as"), { target: { value: "ignore" } });
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("no description"),
    );
    // The importer must never synthesize one (§4) — the screen makes the gap visible instead.
    expect(container.textContent ?? "").toContain("2 selected lines have no description");
  });

  it("drops a row from the import when it is unticked", async () => {
    const { getByLabelText, getByText, container } = mount();
    await waitFor(() => getByLabelText("Import source row 2"));
    expect(container.textContent ?? "").toContain("Rows (2 of 2 selected)");
    fireEvent.click(getByLabelText("Import source row 2"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Rows (1 of 2 selected)"));
    expect(getByText("Preview changes")).toBeTruthy();
  });

  it("commits the resolved lines and hands the count back to the parent", async () => {
    const onClose = vi.fn();
    const { getByText, getByRole } = mount({}, onClose);
    await waitFor(() => getByText("Preview changes"));
    vi.mocked(api.planManifest).mockResolvedValue(PLAN_CLEAN);
    fireEvent.click(getByText("Preview changes"));
    const importBtn = getByRole("button", { name: /^Import 2 lines$/ });
    await waitFor(() => expect((importBtn as HTMLButtonElement).disabled).toBe(false));

    vi.mocked(api.commitAll).mockResolvedValue({ inserted: 2, pages: 1 });
    fireEvent.click(importBtn);

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const [, mode, lines] = vi.mocked(api.commitAll).mock.calls[0];
    expect(mode).toBe("add_new");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toMatchObject({
      source_row_index: 2, part_number: "7006955", description: "Pile cap", qty: 4,
    });
    // `committed` true tells the parent its materials list is now stale.
    expect(onClose.mock.calls[0][1]).toBe(true);
    expect(onClose.mock.calls[0][0]).toMatchObject({ ok: true });
  });

  it("sends an edited cell rather than the source value", async () => {
    const onClose = vi.fn();
    const { getByText, getByRole, getByLabelText } = mount({}, onClose);
    await waitFor(() => getByLabelText("Row 2 column 2"));
    fireEvent.change(getByLabelText("Row 2 column 2"), { target: { value: "Pile cap (revised)" } });

    vi.mocked(api.planManifest).mockResolvedValue(PLAN_CLEAN);
    fireEvent.click(getByText("Preview changes"));
    const importBtn = getByRole("button", { name: /^Import 2 lines$/ });
    await waitFor(() => expect((importBtn as HTMLButtonElement).disabled).toBe(false));
    vi.mocked(api.commitAll).mockResolvedValue({ inserted: 2, pages: 1 });
    fireEvent.click(importBtn);

    await waitFor(() => expect(api.commitAll).toHaveBeenCalled());
    const lines = vi.mocked(api.commitAll).mock.calls[0][2];
    expect(lines[0].description).toBe("Pile cap (revised)");
  });

  it("warns when the import would exceed the job's line cap", async () => {
    const { getByText, container } = mount();
    await waitFor(() => getByText("Preview changes"));
    vi.mocked(api.planManifest).mockResolvedValue({
      ...PLAN_CLEAN, projected_total: 640, would_exceed_line_cap: true,
    });
    fireEvent.click(getByText("Preview changes"));
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("over the 500 the materials page can show"),
    );
  });

  it("reports on-list-but-absent without implying a deletion", async () => {
    const { getByText, container } = mount();
    await waitFor(() => getByText("Preview changes"));
    vi.mocked(api.planManifest).mockResolvedValue({
      ...PLAN_CLEAN,
      counts: { ...PLAN_CLEAN.counts, absent: 3 },
      absent: [
        { line_id: 1, part_number: "A" },
        { line_id: 2, part_number: "B" },
        { line_id: 3, part_number: "C" },
      ],
    });
    fireEvent.click(getByText("Preview changes"));
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("a partial BOM is not a deletion"),
    );
  });

  it("cannot import a manifest that is not parsed", async () => {
    const { container, queryByText } = mount({ status: "refused", detail: "screen:malicious:L3:eicar" });
    await waitFor(() => expect(container.textContent ?? "").toContain("was refused"));
    expect(container.textContent ?? "").toContain("screen:malicious");
    expect(queryByText("Preview changes")).toBeNull();
  });

  it("surfaces a failed commit as actionable copy and stays on screen", async () => {
    const onClose = vi.fn();
    const { getByText, getByRole, container } = mount({}, onClose);
    await waitFor(() => getByText("Preview changes"));
    vi.mocked(api.planManifest).mockResolvedValue(PLAN_CLEAN);
    fireEvent.click(getByText("Preview changes"));
    const importBtn = getByRole("button", { name: /^Import 2 lines$/ });
    await waitFor(() => expect((importBtn as HTMLButtonElement).disabled).toBe(false));

    vi.mocked(api.commitAll).mockRejectedValue(
      Object.assign(new Error("x"), { code: "line_cap_exceeded" }),
    );
    fireEvent.click(importBtn);
    await waitFor(() => expect(container.textContent ?? "").toContain("500 material lines"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("discards without committing", async () => {
    const onClose = vi.fn();
    const { getByText } = mount({}, onClose);
    await waitFor(() => getByText("Discard this manifest"));
    vi.mocked(api.discardManifest).mockResolvedValue({ ok: true, id: 7 });
    fireEvent.click(getByText("Discard this manifest"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onClose.mock.calls[0][1], "a discard must not tell the parent to reload lines").toBe(false);
    expect(api.commitAll).not.toHaveBeenCalled();
  });
});
