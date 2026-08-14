/**
 * Site Tasks page (Track A4) — one job-selectable page for BOTH task models.
 *
 * Under test:
 *   1. the job drop-down defaults to the viewer's placement when no job is routed;
 *   2. both sections render — schedule tasks (via the SHARED TaskRow, mark strip gated on
 *      cap.schedule.mark) and assigned tasks (status buttons gated own-only/privileged);
 *   3. a schedule-less job states the honest empty case with the schedule-page link;
 *   4. a %-mark round-trips through the shared marks hook to the API.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_jobtracker", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_jobtracker")>();
  return { ...actual, fetchJobList: vi.fn() };
});
vi.mock("../../lib/fieldops_schedules", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_schedules")>();
  return { ...actual, fetchScheduleTasks: vi.fn(), markScheduleTaskProgress: vi.fn() };
});
vi.mock("../../lib/fieldops_tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_tasks")>();
  return { ...actual, fetchJobTasks: vi.fn(), setTaskStatus: vi.fn() };
});
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import { fetchJobList } from "../../lib/fieldops_jobtracker";
import { fetchScheduleTasks, markScheduleTaskProgress } from "../../lib/fieldops_schedules";
import { fetchJobTasks, setTaskStatus } from "../../lib/fieldops_tasks";
import { useAuth } from "../../lib/auth";
import { SiteTasksPage } from "../SiteTasksPage";

function schedTask(over: Record<string, unknown> = {}) {
  return {
    id: 11, task_uuid: "u-11", section: "Civil", name: "Trenching east run",
    duration_days: 5, start_date: "2026-08-03", finish_date: "2026-08-08",
    baseline_start_date: "2026-08-03", baseline_finish_date: "2026-08-08",
    percent_done: 25, schedule_percent: 25, is_milestone: 0, is_contract_milestone: 0,
    is_delivery: 0, delivered_date: null, delivered_by: null,
    predecessors_raw: "", sort_order: 10, last_marked_by: null, last_marked_at: null,
    ...over,
  };
}

const JOBS = {
  jobs: [
    { job_id: "JOB-1", project_name: "Deep Lake", status: "active", lifecycle: "active", client_name: null, crew: [], open_tasks: [] },
    { job_id: "JOB-2", project_name: "Steger", status: "active", lifecycle: "active", client_name: null, crew: [], open_tasks: [] },
  ],
  next_cursor: null,
  viewer_current_job: "JOB-2",
};

function mockAuth(capabilities: string[]): void {
  vi.mocked(useAuth).mockReturnValue({
    user: { username: "sam", role: "submitter", capabilities },
    loading: false, login: vi.fn(), logout: vi.fn(),
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  mockAuth(["cap.jobtracker.read", "cap.schedule.mark", "cap.tasks.own"]);
  vi.mocked(fetchJobList).mockResolvedValue(JOBS as never);
  vi.mocked(fetchScheduleTasks).mockResolvedValue({ tasks: [schedTask()], truncated: false } as never);
  vi.mocked(fetchJobTasks).mockResolvedValue({
    tasks: [
      { id: 1, description: "Stage the conduit", status: "open", due_date: "2026-08-20", created_at: 1, personnel_id: 5, assignee_name: "Sam Sub" },
      { id: 2, description: "Someone else's task", status: "open", due_date: null, created_at: 2, personnel_id: 9, assignee_name: "Other Person" },
    ],
    project_name: "Steger",
    viewer_personnel_id: 5,
    viewer_privileged: false,
  } as never);
  vi.mocked(setTaskStatus).mockResolvedValue(undefined as never);
  vi.mocked(markScheduleTaskProgress).mockResolvedValue(undefined as never);
});
afterEach(cleanup);

describe("SiteTasksPage", () => {
  it("defaults the drop-down to the viewer's placement and loads both sections", async () => {
    render(<SiteTasksPage onBack={() => {}} />);
    await waitFor(() => expect((screen.getByLabelText("Job") as HTMLSelectElement).value).toBe("JOB-2"));
    expect(await screen.findByText("Trenching east run")).toBeTruthy();
    expect(await screen.findByText("Stage the conduit")).toBeTruthy();
  });

  it("honors a routed job over the placement default", async () => {
    render(<SiteTasksPage jobId="JOB-1" onBack={() => {}} />);
    await waitFor(() => expect((screen.getByLabelText("Job") as HTMLSelectElement).value).toBe("JOB-1"));
    expect(vi.mocked(fetchScheduleTasks)).toHaveBeenCalledWith("JOB-1");
  });

  it("gates assigned-task status buttons own-only: mine actionable, a foreign task not", async () => {
    render(<SiteTasksPage jobId="JOB-1" onBack={() => {}} />);
    await screen.findByText("Stage the conduit");
    expect(screen.getByLabelText("Start task 1")).toBeTruthy();
    expect(screen.queryByLabelText("Start task 2")).toBeNull();
    fireEvent.click(screen.getByLabelText("Mark task 1 done"));
    await waitFor(() => expect(setTaskStatus).toHaveBeenCalledWith(1, "done"));
  });

  it("states the honest empty case for a schedule-less job, with the schedule-page link", async () => {
    vi.mocked(fetchScheduleTasks).mockResolvedValue({ tasks: [], truncated: false } as never);
    const onOpenSchedule = vi.fn();
    render(<SiteTasksPage jobId="JOB-1" onBack={() => {}} onOpenSchedule={onOpenSchedule} />);
    expect(await screen.findByText(/No schedule has been imported/)).toBeTruthy();
    fireEvent.click(screen.getByText(/Open the schedule page/));
    expect(onOpenSchedule).toHaveBeenCalledWith("JOB-1");
  });

  it("round-trips a quick-% mark through the shared hook", async () => {
    render(<SiteTasksPage jobId="JOB-1" onBack={() => {}} />);
    await screen.findByText("Trenching east run");
    fireEvent.click(screen.getByRole("button", { expanded: false })); // the row's disclosure header
    const chip = await screen.findByLabelText("Mark Trenching east run 50%");
    fireEvent.click(chip);
    await waitFor(() => expect(markScheduleTaskProgress).toHaveBeenCalledWith(11, 50));
  });

  it("hides the mark strip without cap.schedule.mark", async () => {
    mockAuth(["cap.jobtracker.read"]);
    render(<SiteTasksPage jobId="JOB-1" onBack={() => {}} />);
    await screen.findByText("Trenching east run");
    // Read-only viewers get no disclosure button at all — the row is a plain readout, so the
    // mark strip is structurally unreachable, not merely hidden.
    expect(screen.queryByRole("button", { expanded: false })).toBeNull();
    expect(screen.queryByLabelText("Mark Trenching east run 50%")).toBeNull();
  });
});
