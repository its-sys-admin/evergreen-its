/**
 * Schedule validate screen (ADR-0006 PR-4) — the human half of "the parser proposes,
 * the human disposes".
 *
 * What this pins:
 *   • The grid seeds FROM the proposal: section rows render as dividers feeding the
 *     following rows' section, data rows are editable, flags render as warning badges
 *     with human copy, milestone/delivery toggles pre-check from the parse heuristics.
 *   • The PREVIEW-EVIDENCE GATE: commit stays disabled until a source page rendered or
 *     the office explicitly acknowledges reviewing the PDF elsewhere (UX-only in PR-4;
 *     the Worker-side gate arrives with the reconcile hardening).
 *   • The degenerate boundary: a non-degenerate plan shows the "later update" copy and
 *     never enables commit.
 *   • Commit drives the paged loop and reports committed=true up to the parent.
 *
 * Mocks the lib module (the JobMaterialsPage convention).
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_schedules", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_schedules")>();
  return {
    ...actual,
    fetchSchedule: vi.fn(),
    fetchAllScheduleRows: vi.fn(),
    fetchSchedulePreview: vi.fn(),
    planSchedule: vi.fn(),
    commitAllSchedule: vi.fn(),
    discardSchedule: vi.fn(),
  };
});

import * as api from "../../lib/fieldops_schedules";
import type { ScheduleGridRow } from "../../lib/fieldops_schedules";
import { ScheduleValidatePage } from "../ScheduleValidatePage";

const COLUMN_MAP = JSON.stringify({
  mapping: {
    row_number: 0, task_name: 1, duration: 2, start_date: 3,
    finish_date: 4, percent_done: 5, predecessors: 6, phase: 7,
  },
  labels: {},
});

const SCHEDULE = {
  id: 7, schedule_uuid: "su-7", job_id: "JOB-000018",
  filename: "Project Schedule - Kestrel 8.5.26.pdf", declared_mime: "application/pdf",
  size_bytes: 724954, status: "parsed" as const, detail: null, profile: "gantt_export",
  row_count: 3, committed_through_row: 0, uploaded_by: "office.admin", box_file_id: null,
  created_at: 1, parsed_at: 2, committed_at: null, superseded_at: null,
  column_map_json: COLUMN_MAP, header_meta_json: null,
  parse_notes: "p1:rotation: 90cw", resolutions_json: null,
};

const cells = (c: string[]) => JSON.stringify(c);
const ROWS: ScheduleGridRow[] = [
  { row_index: 1, source_page: "pdf:p1", kind: "section", cells_json: cells(["", "Civil", "", "", "", "", "", ""]), flags: "" },
  { row_index: 2, source_page: "pdf:p1", kind: "data", cells_json: cells(["1", "Fencing", "5d", "2026-09-01", "2026-09-05", "25", "", ""]), flags: "" },
  // Same-day + Deliveries phase → milestone AND delivery pre-check; a real corpus flag.
  { row_index: 3, source_page: "pdf:p1", kind: "data", cells_json: cells(["2", "Pile Delivery", "1d", "2026-09-10", "2026-09-10", "", "", "Deliveries"]), flags: "implausible_year" },
];

function mount(onClose = vi.fn()) {
  return { onClose, ...render(<ScheduleValidatePage scheduleId={7} onClose={onClose} />) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchSchedule).mockResolvedValue({ schedule: SCHEDULE, preview_pages: [] });
  vi.mocked(api.fetchAllScheduleRows).mockResolvedValue(ROWS);
  vi.mocked(api.planSchedule).mockResolvedValue({
    ok: true, degenerate: true, revision_reconcile_available: false,
    counts: { incoming: 2, new: 2, existing: 0 },
  });
  vi.mocked(api.commitAllSchedule).mockResolvedValue({ inserted: 2, pages: 1 });
});
afterEach(cleanup);

describe("ScheduleValidatePage — the seeded proposal", () => {
  it("section rows render as dividers; data rows edit; the section context feeds following rows", async () => {
    const { getByLabelText } = mount();
    await waitFor(() => expect(getByLabelText("Section Civil")).toBeTruthy());
    expect((getByLabelText("Row 2 task name") as HTMLInputElement).value).toBe("Fencing");
    // Row 2's own phase cell is empty → the running 'Civil' divider fed its section.
    expect((getByLabelText("Row 2 section") as HTMLInputElement).value).toBe("Civil");
    // Row 3 carries its own phase cell — it wins over the running section.
    expect((getByLabelText("Row 3 section") as HTMLInputElement).value).toBe("Deliveries");
  });

  it("flags render as a warning badge carrying the HUMAN copy, and toggles pre-check from the parse heuristics", async () => {
    const { getByLabelText } = mount();
    await waitFor(() => expect(getByLabelText("Row 3 task name")).toBeTruthy());
    const warn = getByLabelText(/Row 3 warnings:/);
    expect(warn.getAttribute("aria-label") ?? "").toContain("a date's year looks wrong");
    // Same-day span + 1d duration → milestone; Deliveries phase → delivery. Proposals only.
    expect((getByLabelText("Row 3 milestone") as HTMLInputElement).checked).toBe(true);
    expect((getByLabelText("Row 3 delivery") as HTMLInputElement).checked).toBe(true);
    expect((getByLabelText("Row 2 milestone") as HTMLInputElement).checked).toBe(false);
    expect((getByLabelText("Row 3 contract milestone") as HTMLInputElement).checked).toBe(false);
  });

  it("a blocking problem (emptied name) is named per row and disables commit", async () => {
    const { getByLabelText, getByText, findByRole } = mount();
    await waitFor(() => expect(getByLabelText("Row 2 task name")).toBeTruthy());
    fireEvent.click(getByLabelText("I reviewed the source PDF elsewhere"));
    fireEvent.click(getByText("Preview import"));
    await waitFor(() => expect(api.planSchedule).toHaveBeenCalled());
    fireEvent.change(getByLabelText("Row 2 task name"), { target: { value: "  " } });
    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("no task name");
    expect((getByText(/^Import \d+ tasks$/) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("ScheduleValidatePage — plan, evidence gate, commit", () => {
  it("commit is disabled before a plan; a plan alone is NOT enough without preview evidence", async () => {
    const { getByLabelText, getByText, container } = mount();
    await waitFor(() => expect(getByLabelText("Row 2 task name")).toBeTruthy());
    const commitBtn = () => getByText(/^Import \d+ tasks$/) as HTMLButtonElement;
    expect(commitBtn().disabled).toBe(true);
    fireEvent.click(getByText("Preview import"));
    await waitFor(() => expect(api.planSchedule).toHaveBeenCalledTimes(1));
    // Plan landed (degenerate) but NO source page ever rendered and no acknowledgment —
    // the misread-catching side-by-side hasn't happened, so commit stays off and says why.
    expect(commitBtn().disabled).toBe(true);
    expect(container.textContent ?? "").toContain("Import stays disabled until a source page");
    fireEvent.click(getByLabelText("I reviewed the source PDF elsewhere"));
    expect(commitBtn().disabled).toBe(false);
  });

  it("a NON-degenerate plan says reconcile arrives later and never enables commit", async () => {
    vi.mocked(api.planSchedule).mockResolvedValue({
      ok: true, degenerate: false, revision_reconcile_available: false,
      counts: { incoming: 2, new: 0, existing: 40 },
    });
    const { getByLabelText, getByText, container } = mount();
    await waitFor(() => expect(getByLabelText("Row 2 task name")).toBeTruthy());
    fireEvent.click(getByLabelText("I reviewed the source PDF elsewhere"));
    fireEvent.click(getByText("Preview import"));
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("arrives in a later update"),
    );
    expect((getByText(/^Import \d+ tasks$/) as HTMLButtonElement).disabled).toBe(true);
  });

  it("commit sends the RESOLVED rows through the paged loop and closes committed", async () => {
    const onClose = vi.fn();
    const { getByLabelText, getByText } = mount(onClose);
    await waitFor(() => expect(getByLabelText("Row 2 task name")).toBeTruthy());
    fireEvent.click(getByLabelText("I reviewed the source PDF elsewhere"));
    fireEvent.click(getByText("Preview import"));
    await waitFor(() => expect(api.planSchedule).toHaveBeenCalled());
    fireEvent.click(getByText("Import 2 tasks"));
    await waitFor(() => expect(api.commitAllSchedule).toHaveBeenCalledTimes(1));
    const [id, rows] = vi.mocked(api.commitAllSchedule).mock.calls[0];
    expect(id).toBe(7);
    expect(rows).toEqual([
      {
        source_row_index: 2, kind: "data", name: "Fencing", section: "Civil",
        duration_days: 5, start_date: "2026-09-01", finish_date: "2026-09-05",
        percent_done: 25, is_milestone: false, is_contract_milestone: false,
        is_delivery: false, predecessors_raw: null,
      },
      {
        source_row_index: 3, kind: "data", name: "Pile Delivery", section: "Deliveries",
        duration_days: 1, start_date: "2026-09-10", finish_date: "2026-09-10",
        percent_done: 0, is_milestone: true, is_contract_milestone: false,
        is_delivery: true, predecessors_raw: null,
      },
    ]);
    await waitFor(() =>
      expect(onClose).toHaveBeenCalledWith(
        expect.objectContaining({ ok: true, text: expect.stringContaining("Imported 2 tasks") }),
        true,
      ),
    );
  });

  it("an added task (the manual floor) rides after the document's rows", async () => {
    const onClose = vi.fn();
    const { getByLabelText, getByText } = mount(onClose);
    await waitFor(() => expect(getByLabelText("Row 2 task name")).toBeTruthy());
    fireEvent.click(getByText("+ Add a task"));
    fireEvent.change(getByLabelText("Added task 1 name"), { target: { value: "Punch list" } });
    fireEvent.click(getByLabelText("I reviewed the source PDF elsewhere"));
    fireEvent.click(getByText("Preview import"));
    await waitFor(() => expect(api.planSchedule).toHaveBeenCalled());
    fireEvent.click(getByText("Import 3 tasks"));
    await waitFor(() => expect(api.commitAllSchedule).toHaveBeenCalled());
    const rows = vi.mocked(api.commitAllSchedule).mock.calls[0][1];
    expect(rows[2]).toMatchObject({ source_row_index: 4, name: "Punch list" });
  });

  it("a rendered source page satisfies the evidence gate via onLoad (no checkbox needed)", async () => {
    vi.mocked(api.fetchSchedule).mockResolvedValue({ schedule: SCHEDULE, preview_pages: [1] });
    vi.mocked(api.fetchSchedulePreview).mockResolvedValue({ page: 1, png_b64: "QUJD" });
    const { getByLabelText, getByText, getByAltText } = mount();
    await waitFor(() => expect(getByLabelText("Row 2 task name")).toBeTruthy());
    const img = await waitFor(() => getByAltText("Project Schedule - Kestrel 8.5.26.pdf page 1"));
    fireEvent.load(img);
    fireEvent.click(getByText("Preview import"));
    await waitFor(() => expect(api.planSchedule).toHaveBeenCalled());
    expect((getByText(/^Import \d+ tasks$/) as HTMLButtonElement).disabled).toBe(false);
  });

  it("discard closes with a notice and does not claim a commit", async () => {
    vi.mocked(api.discardSchedule).mockResolvedValue({ ok: true, id: 7 });
    const onClose = vi.fn();
    const { getByText, getByLabelText } = mount(onClose);
    await waitFor(() => expect(getByLabelText("Row 2 task name")).toBeTruthy());
    fireEvent.click(getByText("Discard this upload"));
    await waitFor(() => expect(api.discardSchedule).toHaveBeenCalledWith(7));
    expect(onClose).toHaveBeenCalledWith(
      expect.objectContaining({ ok: true, text: "Schedule discarded." }),
      false,
    );
  });
});
