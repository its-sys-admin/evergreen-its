/**
 * Schedule reconcile screen (ADR-0006 decision 9, PR-6) — the human review of a revision
 * against a living task list.
 *
 * What this pins:
 *   • The three-column review renders FROM the plan: CHANGES (old→new date chips +
 *     percent-conflict radios defaulting to portal), NEW (fresh rows + the link picker
 *     over removed candidates — suggestion-sorted, nothing preselected), REMOVED
 *     (blocking reason badges; blocking removals REQUIRE an explicit keep/remove while
 *     non-blocking ones default to remove).
 *   • The commit gate: apply stays disabled while a blocking removal is undecided or the
 *     plan shows ambiguities; the preview-evidence gate carries over from the validate
 *     face.
 *   • Commit sends the derived rows PLUS the resolutions through the paged loop and
 *     closes committed=true.
 *   • A degenerate plan (task list vanished) says so instead of pretending to reconcile.
 *
 * Mocks the lib module (the ScheduleValidatePage convention).
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
import type { ScheduleGridRow, SchedulePlanResponse } from "../../lib/fieldops_schedules";
import { ScheduleReconcilePage } from "../ScheduleReconcilePage";

const COLUMN_MAP = JSON.stringify({
  mapping: {
    row_number: 0, task_name: 1, duration: 2, start_date: 3,
    finish_date: 4, percent_done: 5, predecessors: 6, phase: 7,
  },
  labels: {},
});

const SCHEDULE = {
  id: 7, schedule_uuid: "su-7", job_id: "JOB-000018",
  filename: "Project Schedule - Kestrel 9.1.26.pdf", declared_mime: "application/pdf",
  size_bytes: 724954, status: "parsed" as const, detail: null, profile: "gantt_export",
  row_count: 3, committed_through_row: 0, uploaded_by: "office.admin", box_file_id: null,
  created_at: 1, parsed_at: 2, committed_at: null, superseded_at: null,
  column_map_json: COLUMN_MAP, header_meta_json: null,
  parse_notes: null, resolutions_json: null,
};

const cells = (c: string[]) => JSON.stringify(c);
const ROWS: ScheduleGridRow[] = [
  { row_index: 1, source_page: "pdf:p1", kind: "section", cells_json: cells(["", "Civil", "", "", "", "", "", ""]), flags: "" },
  { row_index: 2, source_page: "pdf:p1", kind: "data", cells_json: cells(["1", "Fencing", "5d", "2026-09-01", "2026-09-08", "60", "", ""]), flags: "" },
  { row_index: 3, source_page: "pdf:p1", kind: "data", cells_json: cells(["2", "Mobilize 2", "3d", "2026-09-10", "2026-09-12", "0", "", ""]), flags: "" },
];

const PLAN: SchedulePlanResponse = {
  ok: true, degenerate: false, revision_reconcile_available: true,
  counts: {
    incoming: 2, new: 1, existing: 3,
    matched: 1, ambiguous: 0, removed: 2, blocking_removals: 1, percent_conflicts: 1,
  },
  matched: [{
    source_row_index: 2, task_id: 11, task_uuid: "tu-11", name: "Fencing", section: "Civil",
    date_change: {
      start_from: "2026-09-01", start_to: "2026-09-01",
      finish_from: "2026-09-05", finish_to: "2026-09-08",
    },
    percent: { rule: "conflict", portal: 50, revision: 60 },
    info_changes: [],
  }],
  ambiguous: [],
  fresh: [{
    source_row_index: 3, name: "Mobilize 2", section: "Civil",
    start_date: "2026-09-10", finish_date: "2026-09-12",
  }],
  removed: [
    {
      task_id: 12, task_uuid: "tu-12", name: "Old Fence", section: "Civil",
      percent_done: 40, start_date: "2026-08-01", finish_date: "2026-08-05",
      blocking: true, reasons: ["marked"],
    },
    {
      task_id: 13, task_uuid: "tu-13", name: "Temp task", section: "Civil",
      percent_done: 0, start_date: "2026-09-10", finish_date: "2026-09-12",
      blocking: false, reasons: [],
    },
  ],
};

function mount(onClose = vi.fn()) {
  return { onClose, ...render(<ScheduleReconcilePage scheduleId={7} onClose={onClose} />) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchSchedule).mockResolvedValue({ schedule: SCHEDULE, preview_pages: [] });
  vi.mocked(api.fetchAllScheduleRows).mockResolvedValue(ROWS);
  vi.mocked(api.planSchedule).mockResolvedValue(PLAN);
  vi.mocked(api.commitAllSchedule).mockResolvedValue({
    inserted: 1, updated: 1, linked: 0, removed: 1, pages: 1,
  });
});
afterEach(cleanup);

describe("ScheduleReconcilePage — the three-column review", () => {
  it("runs the plan on load and renders CHANGES / NEW / REMOVED with dates, badges and counts", async () => {
    const { container, getByLabelText } = mount();
    await waitFor(() => expect(api.planSchedule).toHaveBeenCalledTimes(1));
    // The plan was posted with the rows DERIVED from the grid (section context resolved).
    const [id, rows] = vi.mocked(api.planSchedule).mock.calls[0];
    expect(id).toBe(7);
    expect(rows).toEqual([
      expect.objectContaining({ source_row_index: 2, name: "Fencing", section: "Civil", percent_done: 60 }),
      expect.objectContaining({ source_row_index: 3, name: "Mobilize 2", finish_date: "2026-09-12" }),
    ]);
    await waitFor(() => expect(getByLabelText("Plan summary")).toBeTruthy());
    expect(getByLabelText("Plan summary").textContent ?? "").toContain("1 matched · 1 new · 2 removed");
    const text = container.textContent ?? "";
    expect(getByLabelText("Changes")).toBeTruthy();
    expect(getByLabelText("New tasks")).toBeTruthy();
    expect(getByLabelText("Removed tasks")).toBeTruthy();
    expect(text).toContain("2026-09-05 → 2026-09-08"); // the finish-date chip
    expect(text).toContain("field-marked 40%"); // the blocking reason badge
  });

  it("percent-conflict radios default to PORTAL; blocking removal starts UNDECIDED, non-blocking pre-checks remove", async () => {
    const { getByLabelText } = mount();
    await waitFor(() => expect(getByLabelText("Plan summary")).toBeTruthy());
    expect((getByLabelText("Keep portal 50% for Fencing") as HTMLInputElement).checked).toBe(true);
    expect((getByLabelText("Take revision 60% for Fencing") as HTMLInputElement).checked).toBe(false);
    // Blocking removal: NEITHER radio checked — the human must decide (decision 9).
    expect((getByLabelText("Remove Old Fence") as HTMLInputElement).checked).toBe(false);
    expect((getByLabelText("Keep Old Fence") as HTMLInputElement).checked).toBe(false);
    // Non-blocking: default remove, toggleable.
    expect((getByLabelText("Remove Temp task") as HTMLInputElement).checked).toBe(true);
  });

  it("Apply stays disabled until every BLOCKING removal is decided (evidence acknowledged first)", async () => {
    const { getByLabelText, getByText, container } = mount();
    await waitFor(() => expect(getByLabelText("Plan summary")).toBeTruthy());
    fireEvent.click(getByLabelText("I reviewed the source PDF elsewhere"));
    const applyBtn = () => getByText("Apply revision") as HTMLButtonElement;
    expect(applyBtn().disabled).toBe(true);
    expect(container.textContent ?? "").toContain("choose keep or remove");
    fireEvent.click(getByLabelText("Keep Old Fence"));
    expect(applyBtn().disabled).toBe(false);
  });

  it("commit posts the derived rows + the FULL resolutions and closes committed", async () => {
    const onClose = vi.fn();
    const { getByLabelText, getByText } = mount(onClose);
    await waitFor(() => expect(getByLabelText("Plan summary")).toBeTruthy());
    fireEvent.click(getByLabelText("I reviewed the source PDF elsewhere"));
    fireEvent.click(getByLabelText("Take revision 60% for Fencing"));
    fireEvent.click(getByLabelText("Keep Old Fence"));
    // Link the fresh row onto the non-blocking removed task (a rename)…
    fireEvent.change(getByLabelText("Link Mobilize 2 to a removed task"), { target: { value: "13" } });
    // …which removes it from the REMOVED column (it is claimed now).
    await waitFor(() =>
      expect(getByLabelText("Removed tasks").textContent ?? "").not.toContain("Temp task"),
    );
    fireEvent.click(getByText("Apply revision"));
    await waitFor(() => expect(api.commitAllSchedule).toHaveBeenCalledTimes(1));
    const [id, rows, opts] = vi.mocked(api.commitAllSchedule).mock.calls[0];
    expect(id).toBe(7);
    expect((rows as unknown[]).length).toBe(2);
    expect(opts?.resolutions).toEqual({
      links: { 3: 13 },
      removals: { 12: "keep" }, // 13 left the removed set by being linked
      percents: { 2: "revision" },
    });
    await waitFor(() =>
      expect(onClose).toHaveBeenCalledWith(
        expect.objectContaining({ ok: true, text: expect.stringContaining("Revision applied") }),
        true,
      ),
    );
  });

  it("ambiguous rows render as a blocking alert and Apply never enables", async () => {
    vi.mocked(api.planSchedule).mockResolvedValue({
      ...PLAN,
      counts: { ...PLAN.counts, ambiguous: 1 },
      ambiguous: [{
        source_row_index: 2, name: "Fencing", section: "Civil",
        candidates: [11, 14], reason: "duplicate_living",
      }],
      matched: [], removed: [],
    });
    const { getByLabelText, getByText, findByRole } = mount();
    await waitFor(() => expect(getByLabelText("Plan summary")).toBeTruthy());
    fireEvent.click(getByLabelText("I reviewed the source PDF elsewhere"));
    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("match more than one task");
    expect((getByText("Apply revision") as HTMLButtonElement).disabled).toBe(true);
  });

  it("a DEGENERATE plan (the task list vanished) says so instead of reconciling", async () => {
    vi.mocked(api.planSchedule).mockResolvedValue({
      ok: true, degenerate: true, revision_reconcile_available: true,
      counts: {
        incoming: 2, new: 2, existing: 0,
        matched: 0, ambiguous: 0, removed: 0, blocking_removals: 0, percent_conflicts: 0,
      },
      matched: [], ambiguous: [], fresh: [], removed: [],
    });
    const { container } = mount();
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("nothing to reconcile"),
    );
  });

  it("discard closes without claiming a commit", async () => {
    vi.mocked(api.discardSchedule).mockResolvedValue({ ok: true, id: 7 });
    const onClose = vi.fn();
    const { getByLabelText, getByText } = mount(onClose);
    await waitFor(() => expect(getByLabelText("Plan summary")).toBeTruthy());
    fireEvent.click(getByText("Discard this upload"));
    await waitFor(() => expect(api.discardSchedule).toHaveBeenCalledWith(7));
    expect(onClose).toHaveBeenCalledWith(
      expect.objectContaining({ ok: true, text: expect.stringContaining("task list is unchanged") }),
      false,
    );
  });
});
