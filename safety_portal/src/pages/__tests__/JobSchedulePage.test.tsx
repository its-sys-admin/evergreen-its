/**
 * Per-job Schedule page (ADR-0006 PR-4) — the deep-link target from the Job Tracker.
 *
 * What this pins:
 *   • The affordance split: cap.jobtracker.manage gets the import surface (upload +
 *     validate + discard); everyone with the page's read cap sees the task list only.
 *   • The task table renders what the page exists for — sections in document order,
 *     dates, duration, the progress text, milestone/delivery badges, the delivered chip.
 *   • The validate SUB-FACE opens on a parsed upload (remount-keyed, not a router entry).
 *   • Never-silent: a failed load says so with a working Retry; empty states say so.
 *
 * Mocks the lib module + useAuth (the JobMaterialsPage convention).
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_schedules", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_schedules")>();
  return {
    ...actual,
    fetchScheduleTasks: vi.fn(),
    fetchSchedules: vi.fn(),
    fetchSchedule: vi.fn(),
    fetchAllScheduleRows: vi.fn(),
    fetchSchedulePreview: vi.fn(),
    uploadSchedule: vi.fn(),
    discardSchedule: vi.fn(),
    planSchedule: vi.fn(),
    commitAllSchedule: vi.fn(),
    markScheduleTaskProgress: vi.fn(),
    markScheduleTaskMilestoneDone: vi.fn(),
    markScheduleTaskDelivered: vi.fn(),
  };
});
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as api from "../../lib/fieldops_schedules";
import type { ScheduleListRow, ScheduleTaskRow } from "../../lib/fieldops_schedules";
import { useAuth } from "../../lib/auth";
import { JobSchedulePage } from "../JobSchedulePage";

type Role = "admin" | "manager" | "submitter";
function authWith(role: Role, capabilities: string[]) {
  return {
    user: { username: "u", role, capabilities },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

const TASK: ScheduleTaskRow = {
  id: 1, task_uuid: "tu-1", job_id: "JOB-000018", section: "Civil", name: "Fencing",
  duration_days: 5, start_date: "2026-09-01", finish_date: "2026-09-05",
  baseline_start_date: "2026-09-01", baseline_finish_date: "2026-09-05",
  percent_done: 50, schedule_percent: 25, is_milestone: 0, is_contract_milestone: 0,
  is_delivery: 0, delivered_date: null, delivered_by_name: null, delivered_at: null,
  predecessors_raw: "1FS", sort_order: 10, last_marked_by_name: null, last_marked_at: null,
  created_at: 1, updated_at: 1,
};
const DELIVERY_TASK: ScheduleTaskRow = {
  ...TASK, id: 2, task_uuid: "tu-2", section: "Deliveries", name: "Pile Delivery",
  is_delivery: 1, is_milestone: 1, percent_done: 100, delivered_date: "2026-09-10",
  delivered_by_name: "Mo Manager", delivered_at: 2, sort_order: 20,
};
const CM_TASK: ScheduleTaskRow = {
  ...TASK, id: 3, task_uuid: "tu-3", section: "Civil", name: "Substantial Completion",
  is_contract_milestone: 1, is_milestone: 1, percent_done: 0, sort_order: 30,
};

const PARSED: ScheduleListRow = {
  id: 7, schedule_uuid: "su-7", job_id: "JOB-000018",
  filename: "Project Schedule - Kestrel 8.5.26.pdf", declared_mime: "application/pdf",
  size_bytes: 724954, status: "parsed", detail: null, profile: "gantt_export",
  row_count: 46, committed_through_row: 0, uploaded_by: "office.admin", box_file_id: null,
  created_at: 1, parsed_at: 2, committed_at: null, superseded_at: null,
};
const SUPERSEDED: ScheduleListRow = {
  ...PARSED, id: 6, schedule_uuid: "su-6", filename: "Project Schedule - Kestrel 7.1.26.pdf",
  status: "superseded", superseded_at: 3,
};
const REFUSED: ScheduleListRow = {
  ...PARSED, id: 5, schedule_uuid: "su-5", filename: "Bad Schedule.pdf",
  status: "refused", detail: "screen:malicious:L3", row_count: null,
};

function mountAs(role: Role, caps: string[]) {
  vi.mocked(useAuth).mockReturnValue(authWith(role, caps));
  return render(<JobSchedulePage jobId="JOB-000018" onHome={vi.fn()} onOpenJob={vi.fn()} />);
}

const MANAGE = ["cap.jobtracker.read", "cap.jobtracker.manage"];
const READ_ONLY = ["cap.jobtracker.read"];
const MARK = ["cap.jobtracker.read", "cap.schedule.mark"];

/**
 * Open a task's mark strip (2026-08 design pass). The five percent chips, the exact-% input
 * and the delivered-date control used to sit inside every row's progress CELL — five small
 * targets in a table column that also had to fit a phone. They now live in a per-row
 * disclosure with 48px targets, so the list stays scannable and the controls stay
 * thumb-sized.
 *
 * Strips STAY open once opened (end-of-day marking walks several tasks at a time), which is
 * why a test can open three rows and then assert across all of them.
 */
function openMark(getByLabelText: (t: string) => HTMLElement, taskName: string) {
  fireEvent.click(getByLabelText(`Update progress for ${taskName}`));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchScheduleTasks).mockResolvedValue({
    tasks: [TASK, DELIVERY_TASK, CM_TASK],
    project_name: "Deep Lake",
    truncated: false,
  });
  vi.mocked(api.fetchSchedules).mockResolvedValue({
    schedules: [PARSED, SUPERSEDED, REFUSED],
  });
});
afterEach(cleanup);

describe("JobSchedulePage — the task list", () => {
  it("renders the project name, sections, dates, duration, progress text and badges", async () => {
    const { container, getByLabelText } = mountAs("submitter", READ_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fencing"));
    const text = container.textContent ?? "";
    // The heading says the job's NAME, never the JOB-###### system key.
    expect(text).toContain("Schedule — Deep Lake");
    expect(text).not.toContain("Schedule — JOB-");
    expect(getByLabelText("Civil")).toBeTruthy(); // section groups
    expect(getByLabelText("Deliveries")).toBeTruthy();
    expect(text).toContain("2026-09-01");
    expect(text).toContain("5d");
    expect(text).toContain("█████░░░░░ 50%"); // the progress-bar-ish text
    expect(text).toContain("Milestone");
    expect(text).toContain("Contract milestone");
    expect(text).toContain("Delivery");
    expect(text).toContain("Delivered 2026-09-10 · Mo Manager");
  });

  it("a failed load says so and offers a working Retry", async () => {
    vi.mocked(api.fetchScheduleTasks).mockRejectedValueOnce(new Error("boom"));
    const { findByRole, getByText } = mountAs("submitter", READ_ONLY);
    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("Could not load");
    fireEvent.click(getByText("Retry"));
    await waitFor(() => expect(api.fetchScheduleTasks).toHaveBeenCalledTimes(2));
  });

  it("an empty list says so — read-only copy for the field, an upload CTA for the office", async () => {
    vi.mocked(api.fetchScheduleTasks).mockResolvedValue({ tasks: [], project_name: "Deep Lake", truncated: false });
    vi.mocked(api.fetchSchedules).mockResolvedValue({ schedules: [] });
    const readOnly = mountAs("submitter", READ_ONLY);
    await waitFor(() =>
      expect(readOnly.container.textContent ?? "").toContain("No schedule has been imported"),
    );
    cleanup();
    const office = mountAs("admin", MANAGE);
    await waitFor(() =>
      expect(office.container.textContent ?? "").toContain("Upload the project-schedule PDF export"),
    );
  });
});

describe("JobSchedulePage — the import affordance split", () => {
  it("admin sees the uploads card with status chips; superseded reads as revision history", async () => {
    const { container, getByLabelText, queryByLabelText } = mountAs("admin", MANAGE);
    await waitFor(() => expect(container.textContent ?? "").toContain("Import a schedule"));
    const text = container.textContent ?? "";
    expect(text).toContain("Ready to check");
    expect(text).toContain("Revision history");
    expect(text).toContain("Refused — screen:malicious:L3"); // refused shows its detail
    // The job HAS tasks, so the open button says what opening does: Reconcile.
    expect(getByLabelText("Reconcile Project Schedule - Kestrel 8.5.26.pdf")).toBeTruthy();
    // A superseded revision is history — no Remove for it; the refused one gets one.
    expect(getByLabelText("Remove Bad Schedule.pdf")).toBeTruthy();
    expect(queryByLabelText("Remove Project Schedule - Kestrel 7.1.26.pdf")).toBeNull();
  });

  it("a read-only role gets NO uploads card and no schedule fetch at all", async () => {
    const { container } = mountAs("manager", READ_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fencing"));
    expect(container.textContent ?? "").not.toContain("Import a schedule");
    expect(api.fetchSchedules).not.toHaveBeenCalled();
  });

  it("uploading calls the lib and refreshes the list; an oversize file refuses locally", async () => {
    vi.mocked(api.uploadSchedule).mockResolvedValue({ ok: true, id: 9, filename: "s.pdf", size_bytes: 3 });
    const { getByLabelText, findByRole } = mountAs("admin", MANAGE);
    await waitFor(() => expect(getByLabelText("Schedule PDF")).toBeTruthy());
    const input = getByLabelText("Schedule PDF") as HTMLInputElement;
    const small = new File(["%PDF-1.7 x"], "s.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [small] } });
    await waitFor(() => expect(api.uploadSchedule).toHaveBeenCalledWith("JOB-000018", small));
    await waitFor(() => expect(api.fetchSchedules).toHaveBeenCalledTimes(2));

    const big = new File(["x"], "big.pdf", { type: "application/pdf" });
    Object.defineProperty(big, "size", { value: api.SCHEDULE_MAX_BYTES + 1 });
    fireEvent.change(input, { target: { files: [big] } });
    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("too large");
    expect(api.uploadSchedule).toHaveBeenCalledTimes(1); // never sent
  });

  it("mark affordances are HIDDEN without cap.schedule.mark (the office import cap alone gets none either)", async () => {
    const { container, queryByLabelText } = mountAs("submitter", READ_ONLY);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fencing"));
    expect(queryByLabelText("Mark Fencing 75%")).toBeNull();
    expect(queryByLabelText("Exact percent for Fencing")).toBeNull();
    expect(queryByLabelText("Done Substantial Completion")).toBeNull();
    expect(queryByLabelText("Mark Pile Delivery delivered")).toBeNull();
    expect(queryByLabelText("Delivered date for Pile Delivery")).toBeNull();
  });

  it("a job WITHOUT tasks routes the open button to the VALIDATE face (first import)", async () => {
    vi.mocked(api.fetchScheduleTasks).mockResolvedValue({
      tasks: [], project_name: "Deep Lake", truncated: false,
    });
    vi.mocked(api.fetchSchedule).mockResolvedValue({
      schedule: {
        ...PARSED,
        column_map_json: null, header_meta_json: null,
        parse_notes: null, resolutions_json: null,
      },
      preview_pages: [],
    });
    vi.mocked(api.fetchAllScheduleRows).mockResolvedValue([
      { row_index: 1, source_page: "pdf:p1", kind: "data", cells_json: '["1","Fencing","5d","2026-09-01","2026-09-05","25","",""]', flags: "" },
    ]);
    const { container, getByLabelText } = mountAs("admin", MANAGE);
    await waitFor(() => expect(container.textContent ?? "").toContain("Ready to check"));
    fireEvent.click(getByLabelText("Validate Project Schedule - Kestrel 8.5.26.pdf"));
    // The sub-face took over: the uploads card is gone, the editable grid is on screen.
    await waitFor(() =>
      expect(container.textContent ?? "").not.toContain("Import a schedule"),
    );
    await waitFor(() => expect(getByLabelText("Row 1 task name")).toBeTruthy());
  });

  it("a job WITH tasks routes the open button to the RECONCILE face (PR-6)", async () => {
    vi.mocked(api.fetchSchedule).mockResolvedValue({
      schedule: {
        ...PARSED,
        column_map_json: null, header_meta_json: null,
        parse_notes: null, resolutions_json: null,
      },
      preview_pages: [],
    });
    vi.mocked(api.fetchAllScheduleRows).mockResolvedValue([
      { row_index: 1, source_page: "pdf:p1", kind: "data", cells_json: '["1","Fencing","5d","2026-09-01","2026-09-08","25","",""]', flags: "" },
    ]);
    vi.mocked(api.planSchedule).mockResolvedValue({
      ok: true, degenerate: false, revision_reconcile_available: true,
      counts: {
        incoming: 1, new: 0, existing: 3,
        matched: 1, ambiguous: 0, removed: 2, blocking_removals: 0, percent_conflicts: 0,
      },
      matched: [{
        source_row_index: 1, task_id: 1, task_uuid: "tu-1", name: "Fencing", section: "Civil",
        date_change: null,
        percent: { rule: "keep_portal", portal: 50, revision: 25 }, info_changes: [],
      }],
      ambiguous: [], fresh: [], removed: [],
    });
    const { container, getByLabelText, getByText } = mountAs("admin", MANAGE);
    await waitFor(() => expect(container.textContent ?? "").toContain("Ready to check"));
    fireEvent.click(getByLabelText("Reconcile Project Schedule - Kestrel 8.5.26.pdf"));
    // The RECONCILE sub-face took over — its revision banner + plan summary render.
    await waitFor(() => expect(getByText("Revision reconcile")).toBeTruthy());
    await waitFor(() => expect(api.planSchedule).toHaveBeenCalled());
    await waitFor(() => expect(getByLabelText("Plan summary")).toBeTruthy());
  });
});

describe("JobSchedulePage — field mark-off (cap.schedule.mark, PR-5)", () => {
  beforeEach(() => {
    vi.mocked(api.markScheduleTaskProgress).mockResolvedValue({ ok: true, id: 1, percent_done: 75 });
    vi.mocked(api.markScheduleTaskMilestoneDone).mockResolvedValue({ ok: true, id: 3 });
    vi.mocked(api.markScheduleTaskDelivered).mockResolvedValue({
      ok: true, id: 2, delivered_date: "2026-09-10",
    });
  });

  it("renders the row controls per kind: quick-% chips + exact input on ordinary tasks, a Done checkbox on milestones, a date control on deliveries", async () => {
    const { container, getByLabelText } = mountAs("submitter", MARK);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fencing"));
    openMark(getByLabelText, "Fencing");
    openMark(getByLabelText, "Substantial Completion");
    openMark(getByLabelText, "Pile Delivery");
    // Ordinary task (Fencing, 50%): all five chips + the exact input; the CURRENT % chip
    // is disabled (nothing to re-mark), the others live.
    for (const pct of [0, 25, 75, 100]) {
      expect((getByLabelText(`Mark Fencing ${pct}%`) as HTMLButtonElement).disabled).toBe(false);
    }
    expect((getByLabelText("Mark Fencing 50%") as HTMLButtonElement).disabled).toBe(true);
    expect(getByLabelText("Exact percent for Fencing")).toBeTruthy();
    // Milestone rows get the done-checkbox, NOT chips (a milestone is binary).
    const done = getByLabelText("Done Substantial Completion") as HTMLInputElement;
    expect(done.type).toBe("checkbox");
    expect(done.checked).toBe(false); // percent_done 0
    expect((getByLabelText("Done Pile Delivery") as HTMLInputElement).checked).toBe(true); // 100
    // The delivery row gets the date control, seeded from its stored date.
    const date = getByLabelText("Delivered date for Pile Delivery") as HTMLInputElement;
    expect(date.value).toBe("2026-09-10");
    expect((getByLabelText("Mark Pile Delivery delivered") as HTMLButtonElement).textContent).toBe("Update date");
  });

  it("a quick-% chip calls the progress helper and reloads the list (optimistic then confirmed)", async () => {
    const { container, getByLabelText } = mountAs("submitter", MARK);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fencing"));
    openMark(getByLabelText, "Fencing");
    fireEvent.click(getByLabelText("Mark Fencing 75%"));
    await waitFor(() => expect(api.markScheduleTaskProgress).toHaveBeenCalledWith(1, 75));
    await waitFor(() => expect(api.fetchScheduleTasks).toHaveBeenCalledTimes(2)); // mount + reload
  });

  it("the exact-% input marks an off-chip value; a non-integer refuses locally without a call", async () => {
    const { container, getByLabelText } = mountAs("submitter", MARK);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fencing"));
    openMark(getByLabelText, "Fencing");
    const input = getByLabelText("Exact percent for Fencing") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "62" } });
    fireEvent.click(getByLabelText("Set exact percent for Fencing"));
    await waitFor(() => expect(api.markScheduleTaskProgress).toHaveBeenCalledWith(1, 62));

    fireEvent.change(input, { target: { value: "12.5" } });
    fireEvent.click(getByLabelText("Set exact percent for Fencing"));
    await waitFor(() =>
      expect(container.querySelector('[role="alert"]')?.textContent ?? "").toContain("whole number"),
    );
    expect(api.markScheduleTaskProgress).toHaveBeenCalledTimes(1); // never sent
  });

  it("the milestone checkbox calls milestone-done when checking, progress 0 when un-checking (a correction)", async () => {
    const { container, getByLabelText } = mountAs("submitter", MARK);
    await waitFor(() => expect(container.textContent ?? "").toContain("Substantial Completion"));
    openMark(getByLabelText, "Substantial Completion");
    openMark(getByLabelText, "Pile Delivery");
    fireEvent.click(getByLabelText("Done Substantial Completion")); // 0 → done
    await waitFor(() => expect(api.markScheduleTaskMilestoneDone).toHaveBeenCalledWith(3));
    fireEvent.click(getByLabelText("Done Pile Delivery")); // 100 → un-done
    await waitFor(() => expect(api.markScheduleTaskProgress).toHaveBeenCalledWith(2, 0));
  });

  it("the Delivered button sends the picked date; a Worker refusal surfaces its plain-language copy", async () => {
    const { container, getByLabelText, findByRole } = mountAs("submitter", MARK);
    await waitFor(() => expect(container.textContent ?? "").toContain("Pile Delivery"));
    openMark(getByLabelText, "Pile Delivery");
    openMark(getByLabelText, "Fencing");
    fireEvent.change(getByLabelText("Delivered date for Pile Delivery"), {
      target: { value: "2026-09-12" },
    });
    fireEvent.click(getByLabelText("Mark Pile Delivery delivered"));
    await waitFor(() => expect(api.markScheduleTaskDelivered).toHaveBeenCalledWith(2, "2026-09-12"));

    // Error path: the wire code translates to human copy (errorCopy), never a raw code.
    vi.mocked(api.markScheduleTaskProgress).mockRejectedValueOnce(
      Object.assign(new Error("x"), { code: "milestone_binary" }),
    );
    fireEvent.click(getByLabelText("Mark Fencing 75%"));
    const alert = await findByRole("alert");
    expect(alert.textContent ?? "").toContain("either done or not");
  });
});
