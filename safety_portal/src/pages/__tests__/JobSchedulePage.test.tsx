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

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchScheduleTasks).mockResolvedValue({
    tasks: [TASK, DELIVERY_TASK, CM_TASK],
    project_name: "Deep Lake",
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
    vi.mocked(api.fetchScheduleTasks).mockResolvedValue({ tasks: [], project_name: "Deep Lake" });
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
    expect(getByLabelText("Validate Project Schedule - Kestrel 8.5.26.pdf")).toBeTruthy();
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

  it("Validate opens the sub-face (the validate screen replaces the page body)", async () => {
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
    // The sub-face took over: the uploads card is gone, the grid is on screen.
    await waitFor(() =>
      expect(container.textContent ?? "").not.toContain("Import a schedule"),
    );
    await waitFor(() => expect(getByLabelText("Row 1 task name")).toBeTruthy());
  });
});
