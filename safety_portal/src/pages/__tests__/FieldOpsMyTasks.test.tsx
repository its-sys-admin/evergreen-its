/**
 * "My Tasks" page (P4 S1 + R2 two-tab restructure; Daily tab rebuilt in D2) + its HomePage card
 * gate. Mirrors FieldOpsPersonnel.test.tsx: vi.mock the lib + useAuth, render, query.
 *
 * R2: the page is two tabs (Assigned tasks / Daily report), never-silent (Mandatory B — every
 * fetch has loading / error+Retry / empty, mutually exclusive), per-row busy + inline feedback,
 * contextual Start/Done/Reopen buttons, completed collapse, Refresh + focus refetch, and the
 * mutation/refetch try-split. D2: the Daily tab is the inline SOP daily FORM (DailyReportTab) —
 * placement-driven auto-switch + quick actions; section-level detail tests live beside the
 * component (src/components/__tests__/DailyReportTab.test.tsx).
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_tasks")>();
  return { ...actual, fetchMyTasks: vi.fn(), setTaskStatus: vi.fn() };
});
vi.mock("../../lib/fieldops_checklist", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_checklist")>();
  return { ...actual, completeChecklistItem: vi.fn(), uncompleteChecklistItem: vi.fn(), recordCountItem: vi.fn(), fetchAssignedInspections: vi.fn() };
});
vi.mock("../../lib/fieldops_personnel", () => ({ createCrew: vi.fn(), fetchMyCrew: vi.fn() }));
// D2/CS4: the Daily tab (DailyReportTab) takes its placement from THIS page's /tasks/mine response
// (viewer_placement — no jobs-list fetch of its own), reads the daily-form status endpoint, fetches
// the job detail for the best-effort prefill, and submits through the standard api path.
vi.mock("../../lib/fieldops_jobtracker", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_jobtracker")>();
  return { ...actual, fetchJobList: vi.fn(), fetchJobDetail: vi.fn() };
});
vi.mock("../../lib/fieldops_daily_form", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_daily_form")>();
  return { ...actual, fetchDailyFormStatus: vi.fn() };
});
vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, fetchRecent: vi.fn(), submitForm: vi.fn() };
});
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as api from "../../lib/fieldops_tasks";
import * as personnel from "../../lib/fieldops_personnel";
import * as checklist from "../../lib/fieldops_checklist";
import * as jobtracker from "../../lib/fieldops_jobtracker";
import * as portalApi from "../../lib/api";
import { fetchDailyFormStatus } from "../../lib/fieldops_daily_form";
import { ApiError } from "../../lib/errorCopy";
import { fmtDate, pacificToday } from "../../components/myTasksShared";
import { FieldOpsMyTasks } from "../FieldOpsMyTasks";
import { HomePage } from "../HomePage";
import { useAuth } from "../../lib/auth";

function authWith(capabilities: string[], role: "submitter" | "manager" | "admin" = "submitter") {
  return {
    user: { username: "sam", role, capabilities },
    loading: false,
    login: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
  };
}

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  // Default: a submitter → the Daily tab renders the not-a-manager explanatory copy, no fetches.
  vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own"]));
  // Default: no assigned inspections → the S6 section renders nothing.
  vi.mocked(checklist.fetchAssignedInspections).mockResolvedValue({ inspections: [], linked: true });
  // Default: empty crew list (AddCrewSection's auxiliary fetch).
  vi.mocked(personnel.fetchMyCrew).mockResolvedValue([]);
  // D2 Daily-tab defaults: an empty filed map + a minimal detail (placed tests supply the
  // placement through the /tasks/mine fixture — CS4 #12).
  vi.mocked(jobtracker.fetchJobDetail).mockResolvedValue(JOB_DETAIL);
  vi.mocked(fetchDailyFormStatus).mockResolvedValue({ filed: {}, daily_filed: null });
  vi.mocked(portalApi.fetchRecent).mockResolvedValue(null);
});

const TODAY = pacificToday();

// D2/CS4 Daily-tab fixtures: the placement rides the /tasks/mine response (viewer_placement) +
// a minimal job detail for the best-effort prefill leg.
const PLACED_VIEWER: api.ViewerTaskPlacement = { job_id: "JOB-A", project_name: "Alpha", personnel_id: 1, name: "Mo Manager" };
const JOB_DETAIL: jobtracker.JobDetailResponse = {
  job: {
    job_id: "JOB-A", project_name: "Alpha", status: "active", lifecycle: "active", archive: null, client: null,
    crew: [], tasks: [], time_entries: [], equipment_on_site: [], inspections: [],
    job_no: "",
    site_phase: 0,
    routing: {
      address: "", address_city: "", address_state: "", address_zip: "",
      stakeholder_name: "", stakeholder_email: "", stakeholder_phone: "",
      safety_contact_name: "", safety_contact_email: "", safety_cc: [],
      progress_contact_name: "", progress_contact_email: "", progress_cc: [],
    },
  },
  cursors: { tasks: null, time: null, insp: null },
  viewer_personnel: { id: 1, name: "Mo Manager" },
};
// A PLACED MANAGER session (extra caps as the test needs them). The placement itself rides the
// tasksOk fixture: pass PLACED_VIEWER as its second argument.
function placedManager(caps: string[] = ["cap.tasks.own", "cap.jobtracker.read"]) {
  vi.mocked(useAuth).mockReturnValue(authWith(caps, "manager"));
}

const TASKS: api.MyTask[] = [
  { id: 1, job_id: "JOB-A", project_name: "Alpha", description: "Dig footings", status: "open", created_at: 100, assigned_by: "boss.bob", due_date: null },
  { id: 2, job_id: "JOB-A", project_name: "Alpha", description: "Pour slab", status: "in_progress", created_at: 90, assigned_by: null, due_date: null },
  { id: 3, job_id: "JOB-B", project_name: "Bravo", description: "Frame wall", status: "open", created_at: 80, assigned_by: null, due_date: null },
];

function tasksOk(tasks: api.MyTask[] = TASKS, viewer: api.ViewerTaskPlacement | null = null) {
  vi.mocked(api.fetchMyTasks).mockResolvedValue({ tasks, linked: true, viewer_placement: viewer });
}

describe("FieldOpsMyTasks — tabs", () => {
  // Directive 2026-07-03: the Daily tab exists only for manager/admin accounts WITH a placement —
  // the tab-strip tests therefore run as a PLACED MANAGER (open tasks pin the assigned tab).
  it("renders both tabs for a placed manager, Assigned tasks selected by default", async () => {
    placedManager();
    tasksOk(TASKS, PLACED_VIEWER);
    const { getByRole, queryByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(getByRole("tab", { name: "Assigned tasks" })).not.toBeNull());
    expect(getByRole("tab", { name: "Assigned tasks" }).getAttribute("aria-selected")).toBe("true");
    expect(getByRole("tab", { name: "Daily report" }).getAttribute("aria-selected")).toBe("false");
    // Only the assigned panel is visible (the daily panel is mounted but hidden).
    expect(getByRole("tabpanel", { name: "Assigned tasks" })).not.toBeNull();
    expect(queryByRole("tabpanel", { name: "Daily report" })).toBeNull();
  });

  it("switches to the Daily report tab on click", async () => {
    placedManager();
    tasksOk(TASKS, PLACED_VIEWER);
    const { getByRole, queryByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(getByRole("tab", { name: "Daily report" })).not.toBeNull());
    fireEvent.click(getByRole("tab", { name: "Daily report" }));
    expect(getByRole("tab", { name: "Daily report" }).getAttribute("aria-selected")).toBe("true");
    expect(getByRole("tabpanel", { name: "Daily report" })).not.toBeNull();
    expect(queryByRole("tabpanel", { name: "Assigned tasks" })).toBeNull();
  });

  it("a PLACED submitter gets NO Daily tab (directive 2026-07-03): no tab strip, no daily panel", async () => {
    tasksOk(TASKS, PLACED_VIEWER); // default submitter session — PLACED, yet no daily surface
    const { container, getByRole, queryByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Dig footings"));
    expect(queryByRole("tab", { name: "Daily report" })).toBeNull();
    expect(queryByRole("tablist")).toBeNull(); // tasks/inspections-only — no tab chrome at all
    expect(queryByRole("tabpanel", { name: "Daily report" })).toBeNull(); // panel not even mounted
    expect(getByRole("tabpanel", { name: "Assigned tasks" })).not.toBeNull(); // content intact
  });

  it("an unplaced manager and an unplaced admin get NO Daily tab once the placement lands (✗-tab)", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own", "cap.jobtracker.read"], "manager"));
    tasksOk([]); // linked, viewer_placement null
    const a = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(a.container.textContent ?? "").toContain("No tasks are assigned to you"));
    expect(a.queryByRole("tab", { name: "Daily report" })).toBeNull();
    a.unmount();
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own", "cap.jobtracker.read"], "admin"));
    tasksOk([]);
    const b = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(b.container.textContent ?? "").toContain("No tasks are assigned to you"));
    expect(b.queryByRole("tab", { name: "Daily report" })).toBeNull();
  });

  it("a PLACED admin gets the Daily tab + the inline form, exactly like a placed manager", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own", "cap.jobtracker.read"], "admin"));
    tasksOk([], PLACED_VIEWER); // no open tasks → the auto-switch lands the admin on the Daily tab
    const { getByRole } = render(<FieldOpsMyTasks onBack={() => {}} onOpenForm={vi.fn()} />);
    const panel = await waitFor(() => getByRole("tabpanel", { name: "Daily report" }));
    await waitFor(() => expect(panel.textContent ?? "").toContain("SITE SUPERVISOR"));
    expect(panel.querySelector('input[type="date"]')).not.toBeNull();
  });

  it("auto-switches to Daily report when the actor is a PLACED MANAGER with no open tasks (D2)", async () => {
    placedManager();
    tasksOk([], PLACED_VIEWER); // no open one-off tasks, placed
    const { getByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(getByRole("tab", { name: "Daily report" }).getAttribute("aria-selected")).toBe("true"));
    expect(getByRole("tabpanel", { name: "Daily report" })).not.toBeNull();
  });

  it("does NOT auto-switch while open tasks exist", async () => {
    placedManager();
    tasksOk(TASKS, PLACED_VIEWER); // has open tasks, placed
    const { getByRole, container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Dig footings"));
    expect(getByRole("tab", { name: "Assigned tasks" }).getAttribute("aria-selected")).toBe("true");
  });
});

describe("FieldOpsMyTasks — tasks list (Assigned tasks tab)", () => {
  it("renders my tasks grouped by job (project name), open-first with humanized status pills", async () => {
    tasksOk();
    const { container, getByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const panel = await waitFor(() => getByRole("tabpanel", { name: "Assigned tasks" }));
    // Two job groups (Alpha with 2 tasks, Bravo with 1) inside the tasks grid.
    await waitFor(() => expect(panel.querySelectorAll(".dash-grid .dash-section")).toHaveLength(2));
    const headings = Array.from(panel.querySelectorAll(".dash-detail__h2")).map((h) => h.textContent ?? "");
    expect(headings.some((h) => h.includes("Alpha") && h.includes("JOB-A"))).toBe(true);
    expect(headings.some((h) => h.includes("Bravo") && h.includes("JOB-B"))).toBe(true);
    const txt = panel.textContent ?? "";
    expect(txt).toContain("Dig footings");
    expect(txt).toContain("Pour slab");
    expect(txt).toContain("Frame wall");
    // Humanized status vocabulary (labels.ts) — raw snake_case never reaches the UI.
    expect(txt).toContain("In progress");
    expect(txt).not.toContain("in_progress");
    // Context line: assigner + created date.
    expect(txt).toContain("Assigned by boss.bob");
    // The raw status dropdown is gone (contextual buttons instead).
    expect(container.querySelector("select")).toBeNull();
    // The Alpha group holds two task rows.
    const alphaSection = Array.from(panel.querySelectorAll(".dash-grid .dash-section")).find((s) => (s.textContent ?? "").includes("Alpha"))!;
    expect(alphaSection.querySelectorAll(".dash-tasklist li")).toHaveLength(2);
  });

  it("contextual actions per status: open → Start + Done; in_progress → Done; done → Reopen", async () => {
    tasksOk([
      { ...TASKS[0], id: 1, status: "open" },
      { ...TASKS[1], id: 2, status: "in_progress" },
      { ...TASKS[2], id: 3, status: "done" },
    ]);
    const { getByLabelText, queryByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(getByLabelText("Start task 1")).not.toBeNull());
    expect(getByLabelText("Mark task 1 done")).not.toBeNull();
    expect(getByLabelText("Mark task 2 done")).not.toBeNull();
    expect(queryByLabelText("Start task 2")).toBeNull();
    expect(getByLabelText("Reopen task 3")).not.toBeNull();
    expect(queryByLabelText("Mark task 3 done")).toBeNull();
  });

  it("a status change fires setTaskStatus(taskId, status) and shows inline row feedback", async () => {
    tasksOk();
    vi.mocked(api.setTaskStatus).mockResolvedValue(undefined);
    const { getByLabelText, container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const btn = await waitFor(() => getByLabelText("Mark task 1 done"));
    fireEvent.click(btn);
    await waitFor(() => expect(api.setTaskStatus).toHaveBeenCalledWith(1, "done"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Updated."));
  });

  it("a failed status change reverts the optimistic update and shows the human error inline", async () => {
    tasksOk();
    vi.mocked(api.setTaskStatus).mockRejectedValue(new ApiError("forbidden_task", 403));
    const { getByLabelText, container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const btn = await waitFor(() => getByLabelText("Mark task 1 done"));
    fireEvent.click(btn);
    // R1 human copy from errorCopy.ts surfaces; the row reverts to open (Done button back).
    await waitFor(() => expect(container.textContent ?? "").toContain("You can only update tasks assigned to you."));
    expect((getByLabelText("Mark task 1 done") as HTMLButtonElement).disabled).toBe(false);
  });

  it("per-row busy: an in-flight change disables only that row's controls", async () => {
    tasksOk();
    vi.mocked(api.setTaskStatus).mockReturnValue(new Promise(() => {})); // never settles
    const { getByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const btn = await waitFor(() => getByLabelText("Mark task 1 done"));
    fireEvent.click(btn);
    // Task 1 optimistically flips to done → its Reopen control renders, busy-disabled.
    await waitFor(() => expect((getByLabelText("Reopen task 1") as HTMLButtonElement).disabled).toBe(true));
    // Task 2's controls stay live.
    expect((getByLabelText("Mark task 2 done") as HTMLButtonElement).disabled).toBe(false);
  });

  it("completed tasks collapse under a 'Completed (N)' disclosure per job group", async () => {
    tasksOk([
      { ...TASKS[0], id: 1, status: "open" },
      { ...TASKS[1], id: 2, status: "done", description: "Pour slab" },
    ]);
    const { getByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const panel = await waitFor(() => getByRole("tabpanel", { name: "Assigned tasks" }));
    await waitFor(() => expect(panel.textContent ?? "").toContain("Completed (1)"));
    const details = panel.querySelector("details.dash-completed")!;
    expect(details).not.toBeNull();
    expect(details.hasAttribute("open")).toBe(false); // collapsed by default
    expect(details.textContent ?? "").toContain("Pour slab");
    // The open task renders OUTSIDE the disclosure.
    const openList = panel.querySelector(".dash-grid .dash-section > .dash-tasklist")!;
    expect(openList.textContent ?? "").toContain("Dig footings");
  });

  it("shows an empty state for a linked user with no assigned tasks", async () => {
    tasksOk([]);
    const { getByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const panel = await waitFor(() => getByRole("tabpanel", { name: "Assigned tasks" }));
    await waitFor(() => expect(panel.querySelector(".dash-unavail")).not.toBeNull());
    expect(panel.textContent ?? "").toContain("No tasks are assigned to you");
    expect(panel.querySelector(".dash-tasklist")).toBeNull();
  });

  it("linked:false explains the roster-link gap instead of a bare 'no tasks'", async () => {
    vi.mocked(api.fetchMyTasks).mockResolvedValue({ tasks: [], linked: false, viewer_placement: null });
    const { getByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const panel = await waitFor(() => getByRole("tabpanel", { name: "Assigned tasks" }));
    await waitFor(() => expect(panel.textContent ?? "").toContain("isn't linked to a roster person"));
    expect(panel.textContent ?? "").not.toContain("No tasks are assigned to you");
  });

  it("shows a loading state distinct from empty while tasks are in flight", async () => {
    vi.mocked(api.fetchMyTasks).mockReturnValue(new Promise(() => {})); // never settles
    const { getByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const panel = await waitFor(() => getByRole("tabpanel", { name: "Assigned tasks" }));
    expect(panel.textContent ?? "").toContain("Loading your tasks…");
    expect(panel.textContent ?? "").not.toContain("No tasks are assigned to you");
  });

  it("a tasks fetch failure shows the human error + a working Retry (never a false empty)", async () => {
    vi.mocked(api.fetchMyTasks)
      .mockRejectedValueOnce(new ApiError(null, 500))
      .mockResolvedValueOnce({ tasks: TASKS, linked: true, viewer_placement: null });
    const { getByRole, getByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const panel = await waitFor(() => getByRole("tabpanel", { name: "Assigned tasks" }));
    await waitFor(() => expect(panel.textContent ?? "").toContain("Something went wrong on the server"));
    // Error and empty are mutually exclusive.
    expect(panel.textContent ?? "").not.toContain("No tasks are assigned to you");
    fireEvent.click(getByLabelText("Retry loading your tasks"));
    await waitFor(() => expect(panel.textContent ?? "").toContain("Dig footings"));
    expect(api.fetchMyTasks).toHaveBeenCalledTimes(2);
  });
});

describe("FieldOpsMyTasks — Refresh + wake refetch", () => {
  it("the header Refresh control refetches the tasks AND every section (incl. the Daily tab)", async () => {
    placedManager();
    tasksOk(TASKS, PLACED_VIEWER);
    const { getByLabelText, container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Dig footings"));
    // CS4 #12: fetchMyTasks IS the placement fetch now; the Daily tab's own reads (detail prefill
    // + filed status) ride the refreshToken. fetchJobList never fires from this page.
    expect(api.fetchMyTasks).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(jobtracker.fetchJobDetail).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchDailyFormStatus).toHaveBeenCalledTimes(1));
    expect(checklist.fetchAssignedInspections).toHaveBeenCalledTimes(1);
    fireEvent.click(getByLabelText("Refresh"));
    await waitFor(() => expect(api.fetchMyTasks).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(jobtracker.fetchJobDetail).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(fetchDailyFormStatus).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(checklist.fetchAssignedInspections).toHaveBeenCalledTimes(2));
    expect(jobtracker.fetchJobList).not.toHaveBeenCalled();
  });

  it("refetches when the window regains focus (overnight-tab recovery)", async () => {
    tasksOk();
    const { container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Dig footings"));
    expect(api.fetchMyTasks).toHaveBeenCalledTimes(1);
    fireEvent(window, new Event("focus"));
    await waitFor(() => expect(api.fetchMyTasks).toHaveBeenCalledTimes(2));
  });
});

describe("FieldOpsMyTasks — D2 Daily report tab (page integration)", () => {
  // Section-level detail (prefill, filed banner, amend, submit, past dates) lives in
  // components/__tests__/DailyReportTab.test.tsx — these prove the PAGE wiring.

  it("renders the inline SOP form (date selector + v2 definition) for a placed manager", async () => {
    placedManager();
    tasksOk([], PLACED_VIEWER);
    const { getByRole, getByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} onOpenForm={vi.fn()} />);
    const panel = await waitFor(() => getByRole("tabpanel", { name: "Daily report" })); // auto-switched
    await waitFor(() => expect(panel.textContent ?? "").toContain("SITE SUPERVISOR"));
    expect(panel.querySelector('input[type="date"]')).not.toBeNull();
    expect((panel.querySelector('input[type="date"]') as HTMLInputElement).value).toBe(TODAY);
    expect(panel.textContent ?? "").toContain("Alpha");
    expect(getByLabelText("Submit daily report")).not.toBeNull();
    // The retired R2 checkbox checklist is GONE from the tab.
    expect(panel.textContent ?? "").not.toContain("Today's checklist");
  });

  it("a placed submitter never reaches any Daily copy — the tab and panel are simply absent", async () => {
    // Directive 2026-07-03 REPLACES the old explanatory-copy path for submitters: instead of a
    // tab that opens a "crew-lead managers" explanation, the tab does not exist at all.
    tasksOk([], PLACED_VIEWER); // default submitter session, PLACED
    const { container, queryByRole } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("No tasks are assigned to you"));
    expect(queryByRole("tab", { name: "Daily report" })).toBeNull();
    expect(container.textContent ?? "").not.toContain("crew-lead managers");
    expect(container.querySelector('input[type="date"]')).toBeNull();
  });

  it("an unplaced/unlinked manager gets no Daily tab once landed (the tab needs a placement)", async () => {
    // Unplaced (linked:true from /tasks/mine, no viewer placement) → tab absent after land.
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own", "cap.jobtracker.read"], "manager"));
    tasksOk([]);
    const a = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(a.container.textContent ?? "").toContain("No tasks are assigned to you"));
    expect(a.queryByRole("tab", { name: "Daily report" })).toBeNull();
    a.unmount();
    // Unlinked (linked:false) → same: no placement, no tab.
    vi.mocked(api.fetchMyTasks).mockResolvedValue({ tasks: [], linked: false, viewer_placement: null });
    const b = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(b.container.textContent ?? "").toContain("isn't linked to a roster person"));
    expect(b.queryByRole("tab", { name: "Daily report" })).toBeNull();
  });

  it("a form_link deep-links through the page's onOpenForm with the placement job + selected date", async () => {
    placedManager();
    tasksOk([], PLACED_VIEWER);
    const onOpenForm = vi.fn();
    const { getByRole } = render(<FieldOpsMyTasks onBack={() => {}} onOpenForm={onOpenForm} />);
    const btn = await waitFor(() => getByRole("button", { name: /Create Job Hazard Analysis/ }));
    fireEvent.click(btn);
    expect(onOpenForm).toHaveBeenCalledWith(
      expect.objectContaining({ jobId: "JOB-A", parentCode: "jha", workDate: TODAY }),
    );
  });
});

describe("FieldOpsMyTasks — S6 assigned inspections", () => {
  const INSPECTION: checklist.AssignedInspection = {
    instance: { id: 30, job_id: "JOB-A", project_name: "Alpha", instance_date: "2099-07-10", status: "open", template_title: "Fall protection", created_at: 100, progress_logged: false },
    items: [
      { id: 40, source_item_id: 1, item_type: "manual_attest", label: "Harness checked", form_code: null, target_count: null, status: "open", note: null, photo_ref: null, completed_by: null, completed_at: null, value_num: null, filed_by: null, photo_status: null, requires_photo: false },
      { id: 41, source_item_id: 2, item_type: "form_linked", label: "File JHA", form_code: "jha", target_count: null, status: "open", note: null, photo_ref: null, completed_by: null, completed_at: null, value_num: null, filed_by: null, photo_status: null, requires_photo: false },
    ],
  };

  it("renders the Assigned inspections section titled by template_title (#id demoted)", async () => {
    tasksOk([]);
    vi.mocked(checklist.fetchAssignedInspections).mockResolvedValue({ inspections: [INSPECTION], linked: true });
    const { container, getByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('[aria-label="Assigned inspections"]')).not.toBeNull());
    // The card list titles by template_title; the raw id survives only as demoted small text.
    const card = container.querySelector(".checklist-task-card")!;
    expect(card.querySelector(".dash-card__title")?.textContent).toBe("Fall protection");
    expect(card.textContent ?? "").toContain("#30");
    // Open the inspection (R8 drill-in) → its items appear with the completion controls.
    fireEvent.click(getByLabelText("Open Fall protection inspection"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Harness checked"));
    expect(container.textContent ?? "").toContain("File JHA");
    // manual_attest gets a complete control; form_linked gets a deep-link (no manual-check).
    expect(getByLabelText("Complete item 40")).not.toBeNull();
    expect(() => getByLabelText("Complete item 41")).toThrow();
  });

  it("renders nothing when there are no assigned inspections (confirmed empty)", async () => {
    tasksOk([]);
    vi.mocked(checklist.fetchAssignedInspections).mockResolvedValue({ inspections: [], linked: true });
    const { container } = render(<FieldOpsMyTasks onBack={() => {}} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.querySelector(".dash-unavail")).not.toBeNull());
    expect(container.querySelector('[aria-label="Assigned inspections"]')).toBeNull();
  });

  it("completing an assigned-inspection manual_attest item fires completeChecklistItem", async () => {
    tasksOk([]);
    vi.mocked(checklist.fetchAssignedInspections).mockResolvedValue({ inspections: [INSPECTION], linked: true });
    vi.mocked(checklist.completeChecklistItem).mockResolvedValue({ ok: true, id: 40, status: "done", instance_status: "open" });
    const { getByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} onOpenForm={vi.fn()} />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    const btn = await waitFor(() => getByLabelText("Complete item 40"));
    fireEvent.click(btn);
    await waitFor(() => expect(checklist.completeChecklistItem).toHaveBeenCalledWith(40, undefined));
  });

  it("a form_linked inspection item deep-links pre-filled from the instance's job + date", async () => {
    tasksOk([]);
    vi.mocked(checklist.fetchAssignedInspections).mockResolvedValue({ inspections: [INSPECTION], linked: true });
    const onOpenForm = vi.fn();
    const { getByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} onOpenForm={onOpenForm} />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    const link = await waitFor(() => getByLabelText("Complete File JHA"));
    fireEvent.click(link);
    // 'jha' resolves to its versioned variant; job + date come from the inspection instance.
    await waitFor(() => expect(onOpenForm).toHaveBeenCalledWith(expect.objectContaining({ jobId: "JOB-A", parentCode: "jha", workDate: "2099-07-10" })));
  });
});

describe("FieldOpsMyTasks — Slice T subcontractor Add crew", () => {
  it("hides the Add crew control without cap.crew.create", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own"]));
    tasksOk([]);
    const { container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.querySelector(".dash-unavail")).not.toBeNull());
    expect(container.querySelector('[aria-label="Add crew"]')).toBeNull();
  });

  it("shows the Add crew control as a collapsed disclosure and posts to createCrew", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own", "cap.crew.create"]));
    tasksOk([]);
    vi.mocked(personnel.createCrew).mockResolvedValue({ id: 5, current_job: "JOB-A" });
    const { container, getByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.querySelector('[aria-label="Add crew"]')).not.toBeNull());
    // Collapsed disclosure by default.
    const details = container.querySelector('details[aria-label="Add crew"]')!;
    expect(details).not.toBeNull();
    expect(details.hasAttribute("open")).toBe(false);
    const form = getByLabelText("Add crew form") as HTMLFormElement;
    fireEvent.change(form.querySelector('input[placeholder="Name"]')!, { target: { value: "Helper Hank" } });
    fireEvent.change(form.querySelector('input[placeholder="Trade (optional)"]')!, { target: { value: "laborer" } });
    fireEvent.submit(form);
    await waitFor(() => expect(personnel.createCrew).toHaveBeenCalledWith({ name: "Helper Hank", trade: "laborer" }));
    await waitFor(() => expect(container.textContent ?? "").toContain("Added Helper Hank to your crew on JOB-A"));
    // The crew list refreshes so the new person shows up.
    await waitFor(() => expect(personnel.fetchMyCrew).toHaveBeenCalledTimes(2));
  });

  it("surfaces a clear 'must be placed on a job' message on a 422 not_placed", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own", "cap.crew.create"]));
    tasksOk([]);
    // R1: the libs throw ApiError (err.code = wire code, err.message = human copy) — the page
    // branches on err.code.
    vi.mocked(personnel.createCrew).mockRejectedValue(new ApiError("not_placed", 422));
    const { getByLabelText, container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    const form = await waitFor(() => getByLabelText("Add crew form") as HTMLFormElement);
    fireEvent.change(form.querySelector('input[placeholder="Name"]')!, { target: { value: "Nope" } });
    fireEvent.submit(form);
    await waitFor(() => expect(container.textContent ?? "").toContain("must be placed on a job"));
  });
});

describe("HomePage — My Tasks card gate", () => {
  it("renders the My Tasks card for a holder of cap.tasks.own", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own"]));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    expect(container.textContent ?? "").toContain("My Tasks");
  });

  it("hides the My Tasks card without cap.tasks.own", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.form.submit"]));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    expect(container.textContent ?? "").not.toContain("My Tasks");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// R7 — "Log time" quick action + job-group links (via App's onOpenJob callback).
// ─────────────────────────────────────────────────────────────────────────────
describe("FieldOpsMyTasks — R7 Log time quick action + job links", () => {
  it("with cap.time.log + onOpenJob, 'Log time' deep-links to the placed manager's job", async () => {
    placedManager(["cap.tasks.own", "cap.jobtracker.read", "cap.time.log"]);
    tasksOk(TASKS, PLACED_VIEWER);
    const onOpenJob = vi.fn();
    const { getByLabelText, container } = render(<FieldOpsMyTasks onBack={() => {}} onOpenJob={onOpenJob} />);
    await waitFor(() => expect(getByLabelText("Log time in the Job Tracker")).not.toBeNull());
    // The Daily tab's placement resolve (D2) names the job → the caption + the click carry it.
    await waitFor(() => expect(container.textContent ?? "").toContain("Opens Alpha to log hours."));
    fireEvent.click(getByLabelText("Log time in the Job Tracker"));
    expect(onOpenJob).toHaveBeenCalledWith("JOB-A");
  });

  it("without a known placement the quick action opens the Job Tracker plainly (undefined)", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own", "cap.time.log"]));
    tasksOk();
    const onOpenJob = vi.fn();
    const { getByLabelText, container } = render(<FieldOpsMyTasks onBack={() => {}} onOpenJob={onOpenJob} />);
    await waitFor(() => expect(getByLabelText("Log time in the Job Tracker")).not.toBeNull());
    expect(container.textContent ?? "").toContain("pick your job to log hours");
    fireEvent.click(getByLabelText("Log time in the Job Tracker"));
    expect(onOpenJob).toHaveBeenCalledWith(undefined);
  });

  it("hides the quick action without cap.time.log, and without onOpenJob", async () => {
    // No cap.time.log → no button even with the callback.
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own"]));
    tasksOk();
    const a = render(<FieldOpsMyTasks onBack={() => {}} onOpenJob={vi.fn()} />);
    await waitFor(() => expect(a.container.textContent ?? "").toContain("Dig footings"));
    expect(a.queryByLabelText("Log time in the Job Tracker")).toBeNull();
    a.unmount();
    // cap.time.log but NO callback (actor can't read the tracker) → no button either.
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own", "cap.time.log"]));
    tasksOk();
    const b = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(b.container.textContent ?? "").toContain("Dig footings"));
    expect(b.queryByLabelText("Log time in the Job Tracker")).toBeNull();
  });

  it("job-group headers link to the Job Tracker detail when onOpenJob is present, plain text when absent", async () => {
    tasksOk();
    const onOpenJob = vi.fn();
    const { getByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} onOpenJob={onOpenJob} />);
    const link = await waitFor(() => getByLabelText("Open Alpha in the Job Tracker"));
    fireEvent.click(link);
    expect(onOpenJob).toHaveBeenCalledWith("JOB-A");
  });

  it("group headers render unlinked without the callback (no dead buttons)", async () => {
    tasksOk();
    const { container, queryByLabelText } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Alpha"));
    expect(queryByLabelText("Open Alpha in the Job Tracker")).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// G2.6 — task due dates on My Tasks rows: the shared myTasksShared.TaskDue chrome — `· due <date>`
// + the SAME Overdue warn pill the inspections section uses. Overdue = NOT done AND
// due_date < pacificToday(); a done task never warns; no date → no due chrome at all.
// ─────────────────────────────────────────────────────────────────────────────
describe("FieldOpsMyTasks — G2.6 due dates + overdue pills", () => {
  const DATED: api.MyTask[] = [
    { id: 1, job_id: "JOB-A", project_name: "Alpha", description: "Late one", status: "open", created_at: 100, assigned_by: null, due_date: "2001-01-01" },
    { id: 2, job_id: "JOB-A", project_name: "Alpha", description: "Late started one", status: "in_progress", created_at: 95, assigned_by: null, due_date: "2001-02-02" },
    { id: 3, job_id: "JOB-A", project_name: "Alpha", description: "Future one", status: "open", created_at: 90, assigned_by: null, due_date: "2099-12-31" },
    { id: 4, job_id: "JOB-A", project_name: "Alpha", description: "Undated one", status: "open", created_at: 80, assigned_by: null, due_date: null },
    { id: 5, job_id: "JOB-A", project_name: "Alpha", description: "Done late one", status: "done", created_at: 70, assigned_by: null, due_date: "2001-01-01" },
  ];

  function rowOf(container: HTMLElement, label: string): Element {
    return Array.from(container.querySelectorAll(".dash-tasklist li")).find((r) => r.textContent?.includes(label))!;
  }

  it("overdue pill on past-due OPEN and IN-PROGRESS tasks; never on future / done / undated", async () => {
    tasksOk(DATED);
    const { container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Late one"));
    expect(rowOf(container, "Late one").textContent).toContain("Overdue");
    expect(rowOf(container, "Late started one").textContent).toContain("Overdue"); // in_progress still warns
    expect(rowOf(container, "Future one").textContent).toContain("due");
    expect(rowOf(container, "Future one").textContent).not.toContain("Overdue");
    expect(rowOf(container, "Done late one").textContent).not.toContain("Overdue"); // finished work never warns
    expect(rowOf(container, "Undated one").textContent).not.toContain("due"); // no date → no chrome
  });

  it("the due date renders through the shared fmtDate (local-parts, not UTC-shifted)", async () => {
    tasksOk([DATED[2]]);
    const { container } = render(<FieldOpsMyTasks onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Future one"));
    expect(rowOf(container, "Future one").textContent).toContain(`due ${fmtDate("2099-12-31")}`);
  });
});
