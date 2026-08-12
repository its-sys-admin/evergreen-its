import { useState } from "react";
import type { ScheduleTaskDraft, ScheduleTaskRow } from "../lib/fieldops_schedules";

// The office's hand-editor for ONE schedule task — the manual floor beside the import.
//
// Until now the schedule lane could only author tasks by importing a Smartsheet Gantt PDF.
// The Worker has always exposed add / edit / deactivate (cap.jobtracker.manage, the same
// gate as the import), and the validate screen could add a row DURING a first import — but
// nothing in the SPA called those routes against a committed task list, so a one-line
// correction meant re-exporting from Smartsheet and running a whole revision reconcile.
//
// TWO THINGS THIS FORM MUST BE HONEST ABOUT, because both are invisible in the data:
//
//  1. A HAND-AUTHORED TASK IS NOT IN THE NEXT EXPORT. Reconcile matches the incoming
//     document against the living list; a task Smartsheet has never heard of appears there
//     as a REMOVAL. If it has been marked, delivered, or flagged a contract milestone the
//     reconcile blocks and demands an explicit keep — but an untouched one defaults to
//     remove. So a hand-added task can quietly disappear at the next revision.
//  2. RENAMING RE-KEYS THE TASK. The Worker recomputes `match_key` from section + name on
//     every edit, and that key is what the next revision matches on. Rename a task and the
//     next import sees the old name as removed and the new name as fresh, losing the link
//     unless a human relinks it on the reconcile screen.
//
// Both warnings are rendered, not just commented — the office cannot be expected to hold
// the matching model in their head.
//
// BOUNDS ARE MIRRORED FROM THE WORKER (`readScheduleTaskFields`) so a typo fails instantly
// instead of after a round trip, and the codes returned here are the Worker's own so
// `errorText` renders one vocabulary either way.

const MAX_NAME = 300;
const MAX_SECTION = 120;
const MAX_PREDECESSORS = 200;
const MAX_DURATION_DAYS = 5000;

export interface TaskFormValues {
  name: string;
  section: string;
  start_date: string;
  finish_date: string;
  duration_days: string;
  percent_done: string;
  is_milestone: boolean;
  is_contract_milestone: boolean;
  is_delivery: boolean;
  predecessors_raw: string;
}

export const EMPTY_TASK_FORM: TaskFormValues = {
  name: "", section: "", start_date: "", finish_date: "", duration_days: "",
  percent_done: "", is_milestone: false, is_contract_milestone: false,
  is_delivery: false, predecessors_raw: "",
};

/** Seed the editor from a live row. Numbers become strings so an empty field stays
 *  distinguishable from a real zero — the labor-table rule, for the same reason. */
export function formFromTask(t: ScheduleTaskRow): TaskFormValues {
  return {
    name: t.name,
    section: t.section ?? "",
    start_date: t.start_date ?? "",
    finish_date: t.finish_date ?? "",
    duration_days: t.duration_days === null ? "" : String(t.duration_days),
    percent_done: String(t.percent_done),
    is_milestone: Boolean(t.is_milestone),
    is_contract_milestone: Boolean(t.is_contract_milestone),
    is_delivery: Boolean(t.is_delivery),
    predecessors_raw: t.predecessors_raw ?? "",
  };
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Validate + shape one form into the wire draft, or return the Worker's own error CODE.
 * Deliberately the same bounds and the same vocabulary as `readScheduleTaskFields`; a
 * divergence here would refuse something the server accepts, or vice versa.
 */
export function toTaskDraft(v: TaskFormValues): ScheduleTaskDraft | string {
  const name = v.name.trim();
  if (name.length < 1 || name.length > MAX_NAME) return "invalid_task_name";

  const section = v.section.trim();
  if (section.length > MAX_SECTION) return "invalid_task_section";

  let duration_days: number | null = null;
  if (v.duration_days.trim().length) {
    const n = Number(v.duration_days);
    if (!Number.isSafeInteger(n) || n < 0 || n > MAX_DURATION_DAYS) return "invalid_task_duration";
    duration_days = n;
  }

  for (const d of [v.start_date, v.finish_date]) {
    if (d.trim().length && !ISO_DATE.test(d.trim())) return "invalid_task_date";
  }

  let percent_done = 0;
  if (v.percent_done.trim().length) {
    const n = Number(v.percent_done);
    if (!Number.isSafeInteger(n) || n < 0 || n > 100) return "invalid_task_percent";
    percent_done = n;
  }

  const predecessors_raw = v.predecessors_raw.trim();
  if (predecessors_raw.length > MAX_PREDECESSORS) return "invalid_task_predecessors";

  return {
    name,
    section: section || null,
    duration_days,
    start_date: v.start_date.trim() || null,
    finish_date: v.finish_date.trim() || null,
    percent_done,
    is_milestone: v.is_milestone,
    is_contract_milestone: v.is_contract_milestone,
    is_delivery: v.is_delivery,
    predecessors_raw: predecessors_raw || null,
  };
}

export function ScheduleTaskEditor({
  mode,
  initial,
  ariaScope,
  busy,
  onSave,
  onCancel,
}: {
  mode: "add" | "edit";
  initial: TaskFormValues;
  /** Disambiguates this form's aria-labels from every other row's on the page. */
  ariaScope: string;
  busy: boolean;
  /** Receives the validated draft, or the Worker's error code for the page to translate. */
  onSave: (draft: ScheduleTaskDraft | string) => void;
  onCancel: () => void;
}) {
  const [v, setV] = useState<TaskFormValues>(initial);
  const set = <K extends keyof TaskFormValues>(k: K, val: TaskFormValues[K]) =>
    setV((prev) => ({ ...prev, [k]: val }));
  const label = (field: string) => `${field} — ${ariaScope}`;

  return (
    <div className="sched-edit">
      <p className="sched-edit__title">
        {mode === "add" ? "Add a task by hand" : "Edit this task"}
      </p>

      <div className="sched-edit__grid">
        <label className="sched-edit__field sched-edit__field--wide">
          <span className="sched-edit__label">Task name</span>
          <input
            className="field__input" type="text" maxLength={MAX_NAME}
            aria-label={label("Task name")} value={v.name}
            disabled={busy} onChange={(e) => set("name", e.target.value)}
          />
        </label>

        <label className="sched-edit__field">
          <span className="sched-edit__label">Section</span>
          <input
            className="field__input" type="text" maxLength={MAX_SECTION}
            aria-label={label("Section")} value={v.section}
            disabled={busy} onChange={(e) => set("section", e.target.value)}
          />
        </label>

        <label className="sched-edit__field">
          <span className="sched-edit__label">Predecessors</span>
          <input
            className="field__input" type="text" maxLength={MAX_PREDECESSORS}
            aria-label={label("Predecessors")} value={v.predecessors_raw}
            disabled={busy} onChange={(e) => set("predecessors_raw", e.target.value)}
          />
        </label>

        <label className="sched-edit__field">
          <span className="sched-edit__label">Start</span>
          <input
            className="field__input" type="date"
            aria-label={label("Start date")} value={v.start_date}
            disabled={busy} onChange={(e) => set("start_date", e.target.value)}
          />
        </label>

        <label className="sched-edit__field">
          <span className="sched-edit__label">Finish</span>
          <input
            className="field__input" type="date"
            aria-label={label("Finish date")} value={v.finish_date}
            disabled={busy} onChange={(e) => set("finish_date", e.target.value)}
          />
        </label>

        <label className="sched-edit__field">
          <span className="sched-edit__label">Duration (days)</span>
          <input
            className="field__input" type="number" min={0} max={MAX_DURATION_DAYS} inputMode="numeric"
            aria-label={label("Duration days")} value={v.duration_days}
            disabled={busy} onChange={(e) => set("duration_days", e.target.value)}
          />
        </label>

        <label className="sched-edit__field">
          <span className="sched-edit__label">Percent done</span>
          <input
            className="field__input" type="number" min={0} max={100} inputMode="numeric"
            aria-label={label("Percent done")} value={v.percent_done}
            disabled={busy} onChange={(e) => set("percent_done", e.target.value)}
          />
        </label>
      </div>

      <div className="sched-edit__flags">
        {([
          ["is_milestone", "Milestone"],
          ["is_contract_milestone", "Contract milestone"],
          ["is_delivery", "Delivery"],
        ] as const).map(([key, text]) => (
          <label key={key} className="sched-edit__flag">
            <input
              type="checkbox" aria-label={label(text)} checked={v[key]}
              disabled={busy} onChange={(e) => set(key, e.target.checked)}
            />
            {text}
          </label>
        ))}
      </div>

      {/* The two things the data cannot tell them. See the module header. */}
      <p className="sched-edit__warn">
        {mode === "add"
          ? "A task added here is not in the project schedule export. The next revision you import will offer to remove it — it blocks and asks first once it has been marked off, delivered, or flagged a contract milestone."
          : "Changing the name or section re-keys this task, so the next revision you import will not recognise it and will read it as one task removed and another added. Dates you change here are measured against the original baseline, so an edit shows as slip."}
      </p>

      <div className="sched-edit__actions">
        <button
          type="button" className="btn btn--primary"
          aria-label={mode === "add" ? "Save new task" : label("Save task")}
          disabled={busy || !v.name.trim().length}
          onClick={() => onSave(toTaskDraft(v))}
        >
          {mode === "add" ? "Add task" : "Save changes"}
        </button>
        <button
          type="button" className="btn btn--secondary"
          aria-label={mode === "add" ? "Cancel new task" : label("Cancel editing")}
          disabled={busy} onClick={onCancel}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
