/**
 * The office's hand-editor for one schedule task.
 *
 * `toTaskDraft` mirrors the Worker's `readScheduleTaskFields` — same bounds, same error
 * CODES — so a typo fails instantly instead of after a round trip and both sides speak one
 * vocabulary. These pin that mirroring: if the Worker's bounds move and these do not, the
 * form starts refusing what the server accepts (or worse, the reverse).
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ScheduleTaskRow } from "../../lib/fieldops_schedules";
import {
  EMPTY_TASK_FORM,
  ScheduleTaskEditor,
  formFromTask,
  toTaskDraft,
  type TaskFormValues,
} from "../ScheduleTaskEditor";

afterEach(cleanup);

const form = (over: Partial<TaskFormValues> = {}): TaskFormValues => ({
  ...EMPTY_TASK_FORM,
  name: "Trench main run",
  ...over,
});

describe("toTaskDraft — bounds mirrored from the Worker", () => {
  it("shapes a complete form into the wire draft", () => {
    const d = toTaskDraft(
      form({
        section: "Civil", start_date: "2026-09-01", finish_date: "2026-09-05",
        duration_days: "5", percent_done: "40", is_milestone: true,
        is_contract_milestone: true, is_delivery: false, predecessors_raw: "3FS",
      }),
    );
    expect(d).toEqual({
      name: "Trench main run", section: "Civil", duration_days: 5,
      start_date: "2026-09-01", finish_date: "2026-09-05", percent_done: 40,
      is_milestone: true, is_contract_milestone: true, is_delivery: false,
      predecessors_raw: "3FS",
    });
  });

  it("sends null — not empty string — for every omitted optional field", () => {
    const d = toTaskDraft(form());
    expect(d).toMatchObject({
      section: null, duration_days: null, start_date: null, finish_date: null,
      predecessors_raw: null, percent_done: 0,
    });
  });

  it("trims the name and refuses one that is blank or over 300", () => {
    expect(toTaskDraft(form({ name: "  Piles  " }))).toMatchObject({ name: "Piles" });
    expect(toTaskDraft(form({ name: "   " }))).toBe("invalid_task_name");
    expect(toTaskDraft(form({ name: "x".repeat(301) }))).toBe("invalid_task_name");
    expect(toTaskDraft(form({ name: "x".repeat(300) }))).toMatchObject({ name: "x".repeat(300) });
  });

  it("refuses a section over 120", () => {
    expect(toTaskDraft(form({ section: "s".repeat(121) }))).toBe("invalid_task_section");
    expect(toTaskDraft(form({ section: "s".repeat(120) }))).toMatchObject({ section: "s".repeat(120) });
  });

  it("refuses a non-integer, negative, or over-5000 duration", () => {
    expect(toTaskDraft(form({ duration_days: "2.5" }))).toBe("invalid_task_duration");
    expect(toTaskDraft(form({ duration_days: "-1" }))).toBe("invalid_task_duration");
    expect(toTaskDraft(form({ duration_days: "5001" }))).toBe("invalid_task_duration");
    expect(toTaskDraft(form({ duration_days: "5000" }))).toMatchObject({ duration_days: 5000 });
    expect(toTaskDraft(form({ duration_days: "0" }))).toMatchObject({ duration_days: 0 });
  });

  it("refuses a percent outside 0-100 or with a fraction", () => {
    expect(toTaskDraft(form({ percent_done: "101" }))).toBe("invalid_task_percent");
    expect(toTaskDraft(form({ percent_done: "-1" }))).toBe("invalid_task_percent");
    expect(toTaskDraft(form({ percent_done: "12.5" }))).toBe("invalid_task_percent");
    expect(toTaskDraft(form({ percent_done: "100" }))).toMatchObject({ percent_done: 100 });
  });

  it("refuses a date that is not YYYY-MM-DD", () => {
    expect(toTaskDraft(form({ start_date: "09/01/2026" }))).toBe("invalid_task_date");
    expect(toTaskDraft(form({ finish_date: "2026-9-1" }))).toBe("invalid_task_date");
  });

  it("refuses predecessors over 200", () => {
    expect(toTaskDraft(form({ predecessors_raw: "p".repeat(201) }))).toBe("invalid_task_predecessors");
  });
});

const TASK: ScheduleTaskRow = {
  id: 1, task_uuid: "tu-1", job_id: "JOB-1", section: "Civil", name: "Fencing",
  duration_days: 5, start_date: "2026-09-01", finish_date: "2026-09-05",
  baseline_start_date: "2026-09-01", baseline_finish_date: "2026-09-05",
  percent_done: 50, schedule_percent: 25, is_milestone: 0, is_contract_milestone: 0,
  is_delivery: 1, delivered_date: null, delivered_by_name: null, delivered_at: null,
  predecessors_raw: "1FS", sort_order: 10, last_marked_by_name: null, last_marked_at: null,
  created_at: 1, updated_at: 1,
};

describe("formFromTask", () => {
  it("seeds every field from the live row, numbers as strings", () => {
    expect(formFromTask(TASK)).toEqual({
      name: "Fencing", section: "Civil", start_date: "2026-09-01", finish_date: "2026-09-05",
      duration_days: "5", percent_done: "50", is_milestone: false,
      is_contract_milestone: false, is_delivery: true, predecessors_raw: "1FS",
    });
  });

  it("renders a null duration as empty, distinguishable from a real 0", () => {
    expect(formFromTask({ ...TASK, duration_days: null }).duration_days).toBe("");
    expect(formFromTask({ ...TASK, duration_days: 0 }).duration_days).toBe("0");
  });
});

describe("ScheduleTaskEditor", () => {
  it("scopes its aria-labels so two open rows never collide", () => {
    render(
      <ScheduleTaskEditor mode="edit" ariaScope="Fencing" initial={formFromTask(TASK)}
        busy={false} onSave={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(screen.getByLabelText("Task name — Fencing")).toBeTruthy();
    expect(screen.getByLabelText("Finish date — Fencing")).toBeTruthy();
    expect(screen.getByLabelText("Contract milestone — Fencing")).toBeTruthy();
  });

  it("hands the caller a validated draft", () => {
    const onSave = vi.fn();
    render(
      <ScheduleTaskEditor mode="edit" ariaScope="Fencing" initial={formFromTask(TASK)}
        busy={false} onSave={onSave} onCancel={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText("Task name — Fencing"), { target: { value: "Fencing north" } });
    fireEvent.click(screen.getByLabelText("Save task — Fencing"));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ name: "Fencing north" }));
  });

  it("hands the caller the Worker's error CODE rather than silently refusing", () => {
    const onSave = vi.fn();
    render(
      <ScheduleTaskEditor mode="edit" ariaScope="Fencing" initial={formFromTask(TASK)}
        busy={false} onSave={onSave} onCancel={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText("Percent done — Fencing"), { target: { value: "150" } });
    fireEvent.click(screen.getByLabelText("Save task — Fencing"));
    expect(onSave).toHaveBeenCalledWith("invalid_task_percent");
  });

  it("cannot be saved with an empty name", () => {
    render(
      <ScheduleTaskEditor mode="add" ariaScope="new task" initial={EMPTY_TASK_FORM}
        busy={false} onSave={vi.fn()} onCancel={vi.fn()} />,
    );
    expect((screen.getByLabelText("Save new task") as HTMLButtonElement).disabled).toBe(true);
  });

  it("warns that a hand-added task is not in the next export, and that a rename re-keys", () => {
    const { container, rerender } = render(
      <ScheduleTaskEditor mode="add" ariaScope="new task" initial={EMPTY_TASK_FORM}
        busy={false} onSave={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(container.textContent ?? "").toContain("not in the project schedule export");
    rerender(
      <ScheduleTaskEditor mode="edit" ariaScope="Fencing" initial={formFromTask(TASK)}
        busy={false} onSave={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(container.textContent ?? "").toContain("re-keys this task");
  });

  it("disables every control while a write is in flight", () => {
    render(
      <ScheduleTaskEditor mode="edit" ariaScope="Fencing" initial={formFromTask(TASK)}
        busy onSave={vi.fn()} onCancel={vi.fn()} />,
    );
    expect((screen.getByLabelText("Task name — Fencing") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Save task — Fencing") as HTMLButtonElement).disabled).toBe(true);
  });
});
