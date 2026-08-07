/**
 * R4 (inspections-only since D2) — the admin "Checklists" page (FieldOpsInspections, view key
 * unchanged). Gated cap.checklist.manage. Mock the libs the page imports, render, drive. Covers:
 * the D2 retirement (the "Default daily checklist" editor is GONE — daily content lives in the
 * daily-report-v2 form definition); loading distinct from empty; library rename / deactivate /
 * confirm-gated delete; the per-template item editor (add/edit/reorder/remove) + assignee preview
 * (the catalog-driven form_code select is covered at the component level,
 * ChecklistItemForm.test.tsx); assign; and the HomePage card gate.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_checklist", () => ({
  fetchInspectionTemplates: vi.fn(),
  fetchInspectionTemplate: vi.fn(),
  createInspectionTemplate: vi.fn(),
  editInspectionTemplate: vi.fn(),
  deleteInspectionTemplate: vi.fn(),
  addInspectionItem: vi.fn(),
  editInspectionItem: vi.fn(),
  deleteInspectionItem: vi.fn(),
  assignInspection: vi.fn(),
  // R5 — assignment lifecycle
  fetchChecklistInstances: vi.fn(),
  cancelChecklistInstance: vi.fn(),
  fetchFullRoster: vi.fn(),
  // #16 — recurring assignments
  fetchChecklistRecurrences: vi.fn(),
  deactivateChecklistRecurrence: vi.fn(),
}));
vi.mock("../../lib/fieldops_jobtracker", () => ({ fetchJobList: vi.fn() }));
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as checklist from "../../lib/fieldops_checklist";
import { fetchJobList } from "../../lib/fieldops_jobtracker";
import { FieldOpsInspections } from "../FieldOpsInspections";
import { HomePage } from "../HomePage";
import { useAuth } from "../../lib/auth";

function authWith(capabilities: string[], recurringEnabled = false) {
  return {
    user: { username: "admin", role: "admin" as const, capabilities, recurring_checklists_enabled: recurringEnabled },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

// Crane pre-lift is INACTIVE — drives the badge + assign-picker-exclusion assertions.
// Empty active (0 items, active) — drives the R5 disabled "(no items yet)" picker option.
const TEMPLATES: checklist.InspectionTemplate[] = [
  { id: 1, title: "Fall protection", active: 1, created_at: 100, item_count: 2 },
  { id: 2, title: "Crane pre-lift", active: 0, created_at: 90, item_count: 0 },
  { id: 3, title: "Empty active", active: 1, created_at: 80, item_count: 0 },
];

// R5 — the outstanding-assignments admin list (GET /checklist/instances). Row 41 is past-due (drives
// the overdue pill); row 42 has no job/date.
const INSTANCES: checklist.AdminInstanceRow[] = [
  { id: 41, template_title: "Fall protection", assignee_personnel_id: 5, assignee_name: "Sam Sub", job_id: "JOB-A", project_name: "Alpha", instance_date: "2000-01-02", status: "open", created_at: 200, items_total: 2, items_done: 1 },
  { id: 42, template_title: "Crane pre-lift", assignee_personnel_id: 6, assignee_name: "No Login", job_id: null, project_name: null, instance_date: null, status: "open", created_at: 190, items_total: 1, items_done: 0 },
];

const TEMPLATE_ITEMS: checklist.DefaultItem[] = [
  { id: 11, seq: 10, item_type: "manual_attest", label: "Harness checked", form_code: null, target_count: null, config_json: null },
  { id: 12, seq: 20, item_type: "count", label: "Anchor points", form_code: null, target_count: 4, config_json: null },
];

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(useAuth).mockReturnValue(authWith(["cap.checklist.manage"]));
  vi.mocked(checklist.fetchInspectionTemplates).mockResolvedValue({ templates: TEMPLATES });
  vi.mocked(checklist.fetchInspectionTemplate).mockResolvedValue({
    template: { id: 1, title: "Fall protection", active: 1 },
    items: TEMPLATE_ITEMS,
  });
  vi.mocked(checklist.editInspectionItem).mockResolvedValue({ ok: true, id: 11 });
  // R5: the assign picker pages the FULL roster (login-linked AND non-login people both offered —
  // /assign requires active personnel only), annotated with current placement.
  vi.mocked(checklist.fetchFullRoster).mockResolvedValue([
    { id: 5, name: "Sam Sub", trade: "Laborer", username: "sub.sam", current_job: "JOB-A", current_job_name: "Alpha" },
    { id: 6, name: "No Login", trade: "", username: null, current_job: null, current_job_name: null },
  ]);
  vi.mocked(checklist.fetchChecklistInstances).mockResolvedValue({ instances: INSTANCES, status_filter: "open" });
  vi.mocked(checklist.cancelChecklistInstance).mockResolvedValue({ ok: true, id: 41 });
  vi.mocked(checklist.fetchChecklistRecurrences).mockResolvedValue({ recurrences: [] });
  vi.mocked(checklist.deactivateChecklistRecurrence).mockResolvedValue({ ok: true, id: 51 });
  vi.mocked(fetchJobList).mockResolvedValue({ jobs: [{ job_id: "JOB-A", project_name: "Alpha", status: "active", lifecycle: "active", progress: 0, client_name: null, crew: [], open_tasks: [] }], next_cursor: null });
});

describe("FieldOpsInspections — inspections-only Checklists page (D2 retirement)", () => {
  it("renders the inspection library and the intro pointing daily content at the form definition", async () => {
    const { container } = render(<FieldOpsInspections onBack={() => {}} />);
    expect(container.textContent ?? "").toContain("Checklists");
    await waitFor(() => expect(container.textContent ?? "").toContain("Fall protection"));
    expect(container.querySelector('[aria-label="Inspection library"]')).not.toBeNull();
    expect(container.textContent ?? "").toContain("Crane pre-lift");
    // The intro names the new home of the daily content: the form definition (form builder).
    expect(container.textContent ?? "").toContain("form definition");
  });

  it("the retired 'Default daily checklist' editor is GONE (D2): no section, no CRUD", async () => {
    // (The fetchDefaultChecklist not-called assertion retired with the fn itself — the retired-flow
    // client fns were REMOVED from the lib in the optimization pass, so no call site can exist.)
    const { container, queryByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fall protection"));
    expect(container.textContent ?? "").not.toContain("Default daily checklist");
    expect(container.querySelector('[aria-label="Default daily checklist"]')).toBeNull();
    expect(queryByLabelText("Add default item label")).toBeNull();
  });

  it("renders loading states distinct from empty (no 'No … yet' flash while fetches are pending)", () => {
    vi.mocked(checklist.fetchInspectionTemplates).mockReturnValue(new Promise(() => {}));
    const { container } = render(<FieldOpsInspections onBack={() => {}} />);
    expect(container.textContent ?? "").toContain("Loading inspection checklists");
    expect(container.textContent ?? "").not.toContain("No inspection checklists yet");
  });
});

describe("FieldOpsInspections — inspection library lifecycle", () => {
  it("creating a template fires createInspectionTemplate(title)", async () => {
    vi.mocked(checklist.createInspectionTemplate).mockResolvedValue({ ok: true, id: 3 });
    const { getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    const input = await waitFor(() => getByLabelText("New checklist title") as HTMLInputElement);
    fireEvent.change(input, { target: { value: "Excavation" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(checklist.createInspectionTemplate).toHaveBeenCalledWith("Excavation"));
  });

  it("rename is inline: prefilled input, Save fires editInspectionTemplate(id, { title })", async () => {
    vi.mocked(checklist.editInspectionTemplate).mockResolvedValue({ ok: true, id: 1 });
    const { getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    // Lifecycle actions live in the DETAIL now (form-builder pattern) — select the checklist first.
    fireEvent.click(await waitFor(() => getByLabelText("Edit Fall protection")));
    fireEvent.click(await waitFor(() => getByLabelText("Rename Fall protection")));
    const input = getByLabelText("Rename Fall protection title") as HTMLInputElement;
    expect(input.value).toBe("Fall protection");
    fireEvent.change(input, { target: { value: "Fall safety" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(checklist.editInspectionTemplate).toHaveBeenCalledWith(1, { title: "Fall safety" }));
  });

  it("Deactivate/Reactivate use the edit route's active flag; inactive shows the not-assignable badge", async () => {
    vi.mocked(checklist.editInspectionTemplate).mockResolvedValue({ ok: true, id: 1 });
    const { container, getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    // The inactive template carries the not-assignable badge in the library list.
    await waitFor(() => expect(container.textContent ?? "").toContain("inactive — not assignable"));
    // Lifecycle actions live in the detail — select each checklist first (form-builder pattern).
    fireEvent.click(getByLabelText("Edit Fall protection"));
    fireEvent.click(await waitFor(() => getByLabelText("Deactivate Fall protection")));
    await waitFor(() =>
      expect(checklist.editInspectionTemplate).toHaveBeenCalledWith(1, { title: "Fall protection", active: false }),
    );
    fireEvent.click(getByLabelText("Edit Crane pre-lift"));
    const reactivate = await waitFor(() => {
      const btn = getByLabelText("Reactivate Crane pre-lift") as HTMLButtonElement;
      expect(btn.disabled).toBe(false); // wait out the first update's busy-gate
      return btn;
    });
    fireEvent.click(reactivate);
    await waitFor(() =>
      expect(checklist.editInspectionTemplate).toHaveBeenCalledWith(2, { title: "Crane pre-lift", active: true }),
    );
  });

  it("the assign picker excludes inactive templates", async () => {
    const { getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    const sel = await waitFor(() => getByLabelText("Checklist") as HTMLSelectElement);
    const texts = Array.from(sel.options).map((o) => o.textContent ?? "");
    expect(texts.some((t) => t.includes("Fall protection"))).toBe(true);
    expect(texts.some((t) => t.includes("Crane pre-lift"))).toBe(false);
  });

  it("template delete is confirm-gated with item-count + snapshot copy, offering Deactivate first; cancel is a no-op", async () => {
    vi.mocked(checklist.deleteInspectionTemplate).mockResolvedValue({ ok: true, id: 1 });
    const { container, getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    // Delete lives in the detail — select the checklist first (form-builder pattern).
    fireEvent.click(await waitFor(() => getByLabelText("Edit Fall protection")));
    fireEvent.click(await waitFor(() => getByLabelText("Delete Fall protection")));
    const text = container.textContent ?? "";
    expect(text).toContain("2 items");
    expect(text).toContain("keep their snapshot");
    expect(text).toContain("Deactivate"); // the reversible choice is named first
    fireEvent.click(getByLabelText("Cancel Delete Fall protection"));
    expect(checklist.deleteInspectionTemplate).not.toHaveBeenCalled();
    fireEvent.click(getByLabelText("Delete Fall protection"));
    fireEvent.click(getByLabelText("Confirm Delete Fall protection"));
    await waitFor(() => expect(checklist.deleteInspectionTemplate).toHaveBeenCalledWith(1));
  });
});

describe("FieldOpsInspections — template item editor", () => {
  async function openEditor() {
    const utils = render(<FieldOpsInspections onBack={() => {}} />);
    fireEvent.click(await waitFor(() => utils.getByLabelText("Edit Fall protection")));
    await waitFor(() => expect(utils.container.textContent ?? "").toContain("Harness checked"));
    return utils;
  }

  it("add fires addInspectionItem with auto-suggested seq", async () => {
    vi.mocked(checklist.addInspectionItem).mockResolvedValue({ ok: true, id: 13 });
    const { getByLabelText } = await openEditor();
    fireEvent.change(getByLabelText("Add item label"), { target: { value: "Guardrails present" } });
    fireEvent.submit(getByLabelText("Add item"));
    await waitFor(() =>
      expect(checklist.addInspectionItem).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ label: "Guardrails present", item_type: "manual_attest", seq: 30 }),
      ),
    );
  });

  it("Edit opens a prefilled form and Save fires editInspectionItem (count target carried over)", async () => {
    const { getByLabelText } = await openEditor();
    fireEvent.click(getByLabelText("Edit Anchor points"));
    const labelInput = getByLabelText("Edit item label") as HTMLInputElement;
    expect(labelInput.value).toBe("Anchor points");
    expect((getByLabelText("Edit item target count") as HTMLInputElement).value).toBe("4");
    fireEvent.change(labelInput, { target: { value: "Anchor points verified" } });
    fireEvent.submit(labelInput.closest("form")!);
    await waitFor(() =>
      expect(checklist.editInspectionItem).toHaveBeenCalledWith(
        1,
        12,
        expect.objectContaining({ label: "Anchor points verified", item_type: "count", target_count: 4, seq: 20 }),
      ),
    );
  });

  it("Move down swaps seq via editInspectionItem", async () => {
    const { getByLabelText } = await openEditor();
    fireEvent.click(getByLabelText("Move Harness checked down"));
    await waitFor(() => expect(checklist.editInspectionItem).toHaveBeenCalledTimes(2));
    expect(checklist.editInspectionItem).toHaveBeenCalledWith(1, 11, expect.objectContaining({ seq: 20 }));
    expect(checklist.editInspectionItem).toHaveBeenCalledWith(1, 12, expect.objectContaining({ seq: 10 }));
  });

  it("item removal is confirm-gated; cancel leaves it", async () => {
    vi.mocked(checklist.deleteInspectionItem).mockResolvedValue({ ok: true, id: 11 });
    const { getByLabelText } = await openEditor();
    fireEvent.click(getByLabelText("Remove Harness checked"));
    fireEvent.click(getByLabelText("Cancel Remove Harness checked"));
    expect(checklist.deleteInspectionItem).not.toHaveBeenCalled();
    fireEvent.click(getByLabelText("Remove Harness checked"));
    fireEvent.click(getByLabelText("Confirm Remove Harness checked"));
    await waitFor(() => expect(checklist.deleteInspectionItem).toHaveBeenCalledWith(1, 11));
  });

  it("an empty template shows the 'now add its items below' nudge", async () => {
    vi.mocked(checklist.fetchInspectionTemplate).mockResolvedValue({
      template: { id: 2, title: "Crane pre-lift", active: 0 },
      items: [],
    });
    const { container, getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    fireEvent.click(await waitFor(() => getByLabelText("Edit Crane pre-lift")));
    await waitFor(() => expect(container.textContent ?? "").toContain("now add its items below"));
  });

  it("the assignee preview renders beside the editor, always-on, read-only", async () => {
    const { container, getByLabelText } = await openEditor();
    // The preview pane renders ALONGSIDE the editor now (no toggle) — both are visible at once.
    await waitFor(() => expect(container.textContent ?? "").toContain("Read-only preview"));
    // Faithful: both items render through the assignee row component, every control disabled.
    // (Deliberately loose about ChecklistItemRow internals — R3 owns that component.)
    const preview = container.querySelector('[aria-label="Assignee preview"]')!;
    expect(preview.textContent).toContain("Harness checked");
    expect(preview.textContent).toContain("Anchor points");
    const buttons = Array.from(preview.querySelectorAll("button")) as HTMLButtonElement[];
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons.every((b) => b.disabled)).toBe(true);
    // The editor's Add-item form is available AT THE SAME TIME — the preview no longer hides it.
    expect(getByLabelText("Add item label")).not.toBeNull();
  });
});

describe("FieldOpsInspections — guarded assign (R5)", () => {
  it("offers the FULL active roster (login not required by /assign) with placement context and visible labels", async () => {
    vi.mocked(checklist.assignInspection).mockResolvedValue({ ok: true, instance_id: 7, item_count: 2 });
    const { getByLabelText, container } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(container.querySelector('[aria-label="Assign an inspection"]')).not.toBeNull());
    // Stacked visible labels on the assign fields (job/date read "(optional)" until a form-bearing
    // template is selected).
    const labels = Array.from(container.querySelectorAll(".field__label")).map((el) => el.textContent);
    expect(labels).toEqual(expect.arrayContaining(["Checklist", "Assign to", "Job (optional)", "Due date (optional)"]));
    // VERIFIED rule: POST /assign requires an ACTIVE personnel row only — no login link — so BOTH
    // people are offered (the old client-side username filter dropped "No Login" for no server reason).
    const assignee = await waitFor(() => {
      const sel = getByLabelText("Assignee") as HTMLSelectElement;
      expect(sel.options.length).toBeGreaterThan(2);
      return sel;
    });
    const optTexts = Array.from(assignee.options).map((o) => o.textContent ?? "");
    // Placement context on each option: "Sam Sub (Laborer) — on Alpha".
    expect(optTexts.some((t) => t.includes("Sam Sub") && t.includes("— on Alpha"))).toBe(true);
    expect(optTexts.some((t) => t.includes("No Login"))).toBe(true);

    fireEvent.change(getByLabelText("Checklist"), { target: { value: "1" } });
    fireEvent.change(assignee, { target: { value: "5" } });
    fireEvent.submit(getByLabelText("Assign form"));
    await waitFor(() =>
      expect(checklist.assignInspection).toHaveBeenCalledWith({ template_id: 1, assignee_personnel_id: 5 }),
    );
  });

  it("includes job_id + due_date when chosen", async () => {
    vi.mocked(checklist.assignInspection).mockResolvedValue({ ok: true, instance_id: 8, item_count: 1 });
    const { getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => getByLabelText("Assign form"));
    fireEvent.change(getByLabelText("Checklist"), { target: { value: "1" } });
    fireEvent.change(getByLabelText("Assignee"), { target: { value: "5" } });
    fireEvent.change(getByLabelText("Job"), { target: { value: "JOB-A" } });
    fireEvent.change(getByLabelText("Due date"), { target: { value: "2026-07-10" } });
    fireEvent.submit(getByLabelText("Assign form"));
    await waitFor(() =>
      expect(checklist.assignInspection).toHaveBeenCalledWith({ template_id: 1, assignee_personnel_id: 5, job_id: "JOB-A", due_date: "2026-07-10" }),
    );
  });

  it("a 0-item template is offered DISABLED '(no items yet)' — the empty-template dead-end is unpickable", async () => {
    const { getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    const sel = await waitFor(() => getByLabelText("Checklist") as HTMLSelectElement);
    const opts = Array.from(sel.options);
    const empty = opts.find((o) => (o.textContent ?? "").includes("Empty active"))!;
    expect(empty.disabled).toBe(true);
    expect(empty.textContent).toContain("(no items yet)");
    const full = opts.find((o) => (o.textContent ?? "").includes("Fall protection"))!;
    expect(full.disabled).toBe(false);
    // Inactive templates still excluded entirely.
    expect(opts.some((o) => (o.textContent ?? "").includes("Crane pre-lift"))).toBe(false);
  });

  it("a form-bearing template flips job+date to REQUIRED with inline copy and blocks submit BEFORE the server call", async () => {
    vi.mocked(checklist.fetchInspectionTemplate).mockResolvedValue({
      template: { id: 1, title: "Fall protection", active: 1 },
      items: [
        { id: 31, seq: 10, item_type: "form_linked", label: "File JHA", form_code: "jha", target_count: null, config_json: null },
      ],
    });
    vi.mocked(checklist.assignInspection).mockResolvedValue({ ok: true, instance_id: 9, item_count: 1 });
    const { getByLabelText, container } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => getByLabelText("Assign form"));
    fireEvent.change(getByLabelText("Checklist"), { target: { value: "1" } });
    // The detail fetch resolves → inline copy + required labels.
    await waitFor(() => expect(container.textContent ?? "").toContain("auto-checks from filed forms"));
    expect(container.textContent ?? "").toContain("Job (required)");
    expect(container.textContent ?? "").toContain("Due date (required)");
    // Submit without job+date → blocked client-side (the R1 422's client half); no request fires.
    fireEvent.change(getByLabelText("Assignee"), { target: { value: "5" } });
    fireEvent.submit(getByLabelText("Assign form"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Pick a job and a due date"));
    expect(checklist.assignInspection).not.toHaveBeenCalled();
    // Supplying both lets it through.
    fireEvent.change(getByLabelText("Job"), { target: { value: "JOB-A" } });
    fireEvent.change(getByLabelText("Due date"), { target: { value: "2026-07-10" } });
    fireEvent.submit(getByLabelText("Assign form"));
    await waitFor(() =>
      expect(checklist.assignInspection).toHaveBeenCalledWith({
        template_id: 1,
        assignee_personnel_id: 5,
        job_id: "JOB-A",
        due_date: "2026-07-10",
      }),
    );
  });

  it("success shows the persistent 'Assigned to <name> ✓' card (title/job/due) and FULLY resets the form", async () => {
    vi.mocked(checklist.assignInspection).mockResolvedValue({ ok: true, instance_id: 9, item_count: 2 });
    const { getByLabelText, container } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => getByLabelText("Assign form"));
    fireEvent.change(getByLabelText("Checklist"), { target: { value: "1" } });
    fireEvent.change(getByLabelText("Assignee"), { target: { value: "5" } });
    fireEvent.change(getByLabelText("Job"), { target: { value: "JOB-A" } });
    fireEvent.change(getByLabelText("Due date"), { target: { value: "2026-07-10" } });
    fireEvent.submit(getByLabelText("Assign form"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Assigned to Sam Sub ✓"));
    const card = container.querySelector('[aria-label="Assignment confirmation"]')!;
    expect(card.textContent).toContain("Fall protection");
    expect(card.textContent).toContain("Alpha");
    expect(card.textContent).toContain("due 2026-07-10");
    expect(card.textContent).toContain("2 items");
    // FULL reset — a double-tap can't re-fire with stale selections (duplicate-assign guard).
    expect((getByLabelText("Checklist") as HTMLSelectElement).value).toBe("");
    expect((getByLabelText("Assignee") as HTMLSelectElement).value).toBe("");
    expect((getByLabelText("Job") as HTMLSelectElement).value).toBe("");
    expect((getByLabelText("Due date") as HTMLInputElement).value).toBe("");
  });

  it("picker load failure is NEVER silent: error with Retry, and Retry refetches", async () => {
    vi.mocked(checklist.fetchFullRoster).mockRejectedValueOnce(new Error("boom"));
    const { getByLabelText, container } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Couldn't load the people and jobs"));
    fireEvent.click(getByLabelText("Retry loading assign pickers"));
    await waitFor(() => {
      const assignee = getByLabelText("Assignee") as HTMLSelectElement;
      expect(Array.from(assignee.options).some((o) => (o.textContent ?? "").includes("Sam Sub"))).toBe(true);
    });
  });
});

describe("FieldOpsInspections — outstanding assignments (R5)", () => {
  it("renders each assignment with title, assignee, job, due date, progress aggregate, and status pill", async () => {
    const { container } = render(<FieldOpsInspections onBack={() => {}} />);
    const list = await waitFor(() => {
      const el = container.querySelector('[aria-label="Assignment rows"]');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(checklist.fetchChecklistInstances).toHaveBeenCalledWith("open");
    const text = list.textContent ?? "";
    expect(text).toContain("Fall protection");
    expect(text).toContain("Sam Sub");
    expect(text).toContain("Alpha"); // project_name, not the raw job id
    expect(text).toContain("due 2000-01-02");
    expect(text).toContain("1/2 items done");
    expect(text).toContain("Open"); // humanized status pill
    // The no-job/no-date row renders without fabricated context.
    expect(text).toContain("Crane pre-lift");
    expect(text).toContain("No Login");
  });

  it("an OPEN past-due row gets the overdue pill; undated, future, and complete rows do not", async () => {
    vi.mocked(checklist.fetchChecklistInstances).mockResolvedValue({
      instances: [
        { ...INSTANCES[0], id: 41, instance_date: "2000-01-02", status: "open" },
        { ...INSTANCES[0], id: 43, instance_date: "2099-01-01", status: "open" },
        { ...INSTANCES[0], id: 44, instance_date: null, status: "open" },
        { ...INSTANCES[0], id: 45, instance_date: "2000-01-02", status: "complete" },
      ],
      status_filter: "all",
    });
    const { container } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(container.querySelector('[aria-label="Assignment rows"]')).not.toBeNull());
    const pills = Array.from(container.querySelectorAll(".dash-pill--warn")).filter((el) =>
      (el.textContent ?? "").includes("overdue"),
    );
    expect(pills).toHaveLength(1);
  });

  it("loading is distinct from empty; a confirmed-empty response shows the empty copy", async () => {
    vi.mocked(checklist.fetchChecklistInstances).mockReturnValue(new Promise(() => {}));
    const { container, unmount } = render(<FieldOpsInspections onBack={() => {}} />);
    expect(container.textContent ?? "").toContain("Loading assignments…");
    expect(container.textContent ?? "").not.toContain("No open assignments");
    unmount();

    vi.mocked(checklist.fetchChecklistInstances).mockResolvedValue({ instances: [], status_filter: "open" });
    const second = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(second.container.textContent ?? "").toContain("No open assignments"));
  });

  it("a fetch failure renders an error with Retry (never a silent blank); Retry reloads the rows", async () => {
    vi.mocked(checklist.fetchChecklistInstances).mockRejectedValueOnce(new Error("boom"));
    const { container, getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Couldn't load the assignments."));
    // Error and empty are mutually exclusive.
    expect(container.textContent ?? "").not.toContain("No open assignments");
    fireEvent.click(getByLabelText("Retry loading assignments"));
    await waitFor(() => expect(container.querySelector('[aria-label="Assignment rows"]')).not.toBeNull());
  });

  it("the Open/All toggle refetches with the chosen filter", async () => {
    const { container, getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(checklist.fetchChecklistInstances).toHaveBeenCalledWith("open"));
    fireEvent.click(getByLabelText("Show all assignments"));
    await waitFor(() => expect(checklist.fetchChecklistInstances).toHaveBeenCalledWith("all"));
    await waitFor(() => expect(container.querySelector('[aria-label="Assignment rows"]')).not.toBeNull());
  });

  it("Cancel is confirm-gated with the discard blast-radius copy; cancel is a no-op, confirm fires + refetches", async () => {
    const { container, getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(container.querySelector('[aria-label="Assignment rows"]')).not.toBeNull());
    fireEvent.click(getByLabelText("Cancel assignment Fall protection for Sam Sub"));
    expect(container.textContent ?? "").toContain("removes it from Sam Sub's Assigned inspections");
    expect(container.textContent ?? "").toContain("completed items are discarded");
    // The confirm's cancel path leaves everything untouched.
    fireEvent.click(getByLabelText("Cancel Cancel assignment Fall protection for Sam Sub"));
    expect(checklist.cancelChecklistInstance).not.toHaveBeenCalled();
    // Confirm path: the cancel fires and the list refetches.
    const callsBefore = vi.mocked(checklist.fetchChecklistInstances).mock.calls.length;
    fireEvent.click(getByLabelText("Cancel assignment Fall protection for Sam Sub"));
    fireEvent.click(getByLabelText("Confirm Cancel assignment Fall protection for Sam Sub"));
    await waitFor(() => expect(checklist.cancelChecklistInstance).toHaveBeenCalledWith(41));
    await waitFor(() =>
      expect(vi.mocked(checklist.fetchChecklistInstances).mock.calls.length).toBeGreaterThan(callsBefore),
    );
    await waitFor(() => expect(container.textContent ?? "").toContain("Cancelled “Fall protection” for Sam Sub."));
  });

  it("a successful assign refreshes the assignments section (refreshKey wiring)", async () => {
    vi.mocked(checklist.assignInspection).mockResolvedValue({ ok: true, instance_id: 9, item_count: 2 });
    const { getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => getByLabelText("Assign form"));
    const callsBefore = vi.mocked(checklist.fetchChecklistInstances).mock.calls.length;
    fireEvent.change(getByLabelText("Checklist"), { target: { value: "1" } });
    fireEvent.change(getByLabelText("Assignee"), { target: { value: "5" } });
    fireEvent.submit(getByLabelText("Assign form"));
    await waitFor(() =>
      expect(vi.mocked(checklist.fetchChecklistInstances).mock.calls.length).toBeGreaterThan(callsBefore),
    );
  });
});

describe("FieldOpsInspections — recurring checklists (#16)", () => {
  it("flag OFF (default): no recurring toggle, no Recurring-assignments band, and no recurrences fetch", async () => {
    const { container, queryByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => expect(container.querySelector('[aria-label="Assign an inspection"]')).not.toBeNull());
    expect(queryByLabelText("Recurring checklist")).toBeNull();
    expect(container.querySelector('[aria-label="Recurring assignments"]')).toBeNull();
    expect(checklist.fetchChecklistRecurrences).not.toHaveBeenCalled();
  });

  it("flag ON: checking Recurring reveals cadence + start date and posts a recurrence payload (no due_date)", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.checklist.manage"], true));
    vi.mocked(checklist.assignInspection).mockResolvedValue({ ok: true, recurrence_id: 51, instances_created: 1 });
    const { getByLabelText, container } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => getByLabelText("Assign form"));
    // The toggle is offered; checking it reveals Cadence + Start date (and hides Due date).
    fireEvent.click(getByLabelText("Recurring checklist"));
    const cadence = getByLabelText("Cadence") as HTMLSelectElement;
    fireEvent.change(getByLabelText("Checklist"), { target: { value: "1" } });
    fireEvent.change(getByLabelText("Assignee"), { target: { value: "5" } });
    fireEvent.change(getByLabelText("Job"), { target: { value: "JOB-A" } });
    fireEvent.change(cadence, { target: { value: "weekly" } });
    fireEvent.change(getByLabelText("Start date"), { target: { value: "2026-07-11" } });
    fireEvent.submit(getByLabelText("Assign form"));
    await waitFor(() =>
      expect(checklist.assignInspection).toHaveBeenCalledWith({
        template_id: 1,
        assignee_personnel_id: 5,
        job_id: "JOB-A",
        recurrence: { cadence: "weekly", anchor_date: "2026-07-11" },
      }),
    );
    await waitFor(() => expect(container.textContent ?? "").toContain("Recurring checklist set"));
  });

  it("flag ON: recurring requires a job + start date before the request (client-half of the server guard)", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.checklist.manage"], true));
    const { getByLabelText, container } = render(<FieldOpsInspections onBack={() => {}} />);
    await waitFor(() => getByLabelText("Assign form"));
    fireEvent.click(getByLabelText("Recurring checklist"));
    fireEvent.change(getByLabelText("Checklist"), { target: { value: "1" } });
    fireEvent.change(getByLabelText("Assignee"), { target: { value: "5" } });
    // No job / no start date → blocked client-side, no request fires.
    fireEvent.submit(getByLabelText("Assign form"));
    await waitFor(() => expect(container.textContent ?? "").toContain("needs a job and a start"));
    expect(checklist.assignInspection).not.toHaveBeenCalled();
  });

  it("flag ON: the Recurring-assignments band lists an active recurrence and Stop deactivates it", async () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.checklist.manage"], true));
    vi.mocked(checklist.fetchChecklistRecurrences).mockResolvedValue({
      recurrences: [
        {
          id: 51,
          template_id: 1,
          template_title: "Fall protection",
          assignee_personnel_id: 5,
          assignee_name: "Sam Sub",
          job_id: "JOB-A",
          project_name: "Alpha",
          cadence: "daily",
          anchor_date: "2026-07-01",
          last_generated_date: "2026-07-05",
          created_at: 200,
        },
      ],
    });
    const { container, getByLabelText } = render(<FieldOpsInspections onBack={() => {}} />);
    const band = await waitFor(() => {
      const el = container.querySelector('[aria-label="Recurring assignments"]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    await waitFor(() => expect(band.textContent ?? "").toContain("Fall protection"));
    expect(band.textContent ?? "").toContain("Sam Sub");
    expect(band.textContent ?? "").toContain("Daily");
    // Two-step confirm (ConfirmDelete): Stop → Confirm → deactivate fires.
    fireEvent.click(getByLabelText("Stop recurring Fall protection for Sam Sub"));
    fireEvent.click(getByLabelText("Confirm Stop recurring Fall protection for Sam Sub"));
    await waitFor(() => expect(checklist.deactivateChecklistRecurrence).toHaveBeenCalledWith(51));
  });
});

describe("HomePage — Checklists card gate (R7 rename: was 'Inspection checklists')", () => {
  it("renders the card for a holder of cap.checklist.manage", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.checklist.manage"]));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    const titles = Array.from(container.querySelectorAll(".form-card__title")).map((el) => el.textContent);
    expect(titles).toContain("Checklists");
    expect(titles).not.toContain("Inspection checklists"); // renamed per Open Q4
  });

  it("hides the card without cap.checklist.manage", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(["cap.tasks.own"]));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    const titles = Array.from(container.querySelectorAll(".form-card__title")).map((el) => el.textContent);
    expect(titles).not.toContain("Checklists");
  });
});
