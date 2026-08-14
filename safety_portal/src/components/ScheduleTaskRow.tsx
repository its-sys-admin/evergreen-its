import type { Dispatch, SetStateAction } from "react";
import * as api from "../lib/fieldops_schedules";
import type { ScheduleTaskRow } from "../lib/fieldops_schedules";
import { isLate, progressBar, slipDays } from "../lib/schedule_view";
import { ConfirmDelete } from "./ChecklistItemForm";
import { ScheduleTaskEditor, formFromTask } from "./ScheduleTaskEditor";

const PERCENT_CHIPS = [0, 25, 50, 75, 100] as const;

// Extracted VERBATIM from JobSchedulePage (Track A4, 2026-08-13) so the Site Tasks page can
// render the same rows without forking the mark-strip semantics (§14 parameterize-not-clone:
// milestone_binary, delivered-date default, optimistic % updates — none of it may drift
// between the two surfaces). Props and behavior unchanged; the page passes canManage=false
// on the Site Tasks surface, which hides the office edit/remove affordances exactly as it
// does for a non-manage viewer on the schedule page.

// ── One task row ────────────────────────────────────────────────────────────────────────
// A grid row on a laptop, a stacked card on a phone. Deliberately NOT a <table>: the
// mark-off strip spans the full row width when open, which a table cell cannot do without
// colspan gymnastics — and cramming those controls into a cell is precisely what made the
// old page unusable on a phone.

export function TaskRow({
  t,
  today,
  canMark,
  canManage,
  markBusy,
  taskBusy,
  open,
  onToggle,
  editing,
  onEdit,
  onCancelEdit,
  onSaveEdit,
  onRemove,
  exactPct,
  setExactPct,
  deliveredDraft,
  setDeliveredDraft,
  markPercent,
  markExact,
  markMilestone,
  markDelivered,
  onOpenMaterials,
}: {
  t: ScheduleTaskRow;
  today: string;
  canMark: boolean;
  canManage: boolean;
  markBusy: number | null;
  taskBusy: boolean;
  open: boolean;
  onToggle: () => void;
  editing: boolean;
  onEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdit: (draft: api.ScheduleTaskDraft | string) => void;
  onRemove: () => Promise<boolean>;
  exactPct: Record<number, string>;
  setExactPct: Dispatch<SetStateAction<Record<number, string>>>;
  deliveredDraft: Record<number, string>;
  setDeliveredDraft: Dispatch<SetStateAction<Record<number, string>>>;
  markPercent: (t: ScheduleTaskRow, p: number) => Promise<void>;
  markExact: (t: ScheduleTaskRow) => void;
  markMilestone: (t: ScheduleTaskRow, done: boolean) => Promise<void>;
  markDelivered: (t: ScheduleTaskRow) => Promise<void>;
  onOpenMaterials?: () => void;
}) {
  const late = isLate(t, today);
  const slip = slipDays(t);
  const doneRow = t.percent_done >= 100;

  const cls = ["sched-task", late ? "sched-task--late" : "", doneRow ? "sched-task--done" : ""]
    .filter(Boolean)
    .join(" ");

  const provenance = [
    t.schedule_percent !== null && t.schedule_percent !== t.percent_done
      ? `The schedule document says ${t.schedule_percent}%.`
      : "",
    t.last_marked_by_name ? `Last marked by ${t.last_marked_by_name}.` : "",
    t.predecessors_raw ? `Follows ${t.predecessors_raw}.` : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <li className={cls}>
      <div className="sched-task__main">
        {t.is_milestone ? (
          <span className="sched-task__glyph" aria-hidden="true">
            ◆
          </span>
        ) : null}
        <span className="sched-task__name">{t.name}</span>
        {t.is_contract_milestone ? (
          <span className="dash-pill dash-pill--warn">Contract milestone</span>
        ) : t.is_milestone ? (
          <span className="dash-pill">Milestone</span>
        ) : null}
        {t.is_delivery ? <span className="dash-pill">Delivery</span> : null}
        {late ? <span className="dash-pill dash-pill--danger">Late</span> : null}
        {/* Slip is measured against the baseline anchor stamped at the task's first commit —
            the one field that records that a revision moved this date. Never shown before. */}
        {slip !== null ? (
          <span className="dash-pill">
            {slip > 0 ? `Slipped ${slip}d` : `Pulled in ${Math.abs(slip)}d`}
          </span>
        ) : null}
        {t.delivered_date ? (
          <span className="dash-pill dash-pill--ok">
            Delivered {t.delivered_date}
            {t.delivered_by_name ? ` · ${t.delivered_by_name}` : ""}
          </span>
        ) : null}
      </div>

      <div className="sched-task__dates">
        {t.start_date ?? "—"} → {t.finish_date ?? "—"}
      </div>
      <div className="sched-task__dur">{t.duration_days != null ? `${t.duration_days}d` : "—"}</div>

      {/* One disclosure per row, whichever capability the session holds: the field's
          mark-off and the office's edit both live behind it, so a row expands in exactly
          one place. The label names what THIS session can actually do with it. */}
      {canMark || canManage ? (
        <button
          type="button"
          className="sched-task__prog"
          aria-label={canMark ? `Update progress for ${t.name}` : `Open task ${t.name}`}
          aria-expanded={open}
          onClick={onToggle}
        >
          <ProgressReadout percent={t.percent_done} />
          <span className="sched-task__prog-hint" aria-hidden="true">
            {open ? "▾" : "▸"}
          </span>
        </button>
      ) : (
        <div className="sched-task__prog">
          <ProgressReadout percent={t.percent_done} />
        </div>
      )}

      {canMark && open && !editing ? (
        <div className="sched-mark">
          <span className="sched-mark__label">Mark progress — {t.name}</span>

          {!t.is_milestone ? (
            <>
              {PERCENT_CHIPS.map((p) => (
                <button
                  key={p}
                  type="button"
                  className="sched-mark__chip"
                  aria-label={`Mark ${t.name} ${p}%`}
                  aria-pressed={t.percent_done === p}
                  disabled={markBusy !== null || t.percent_done === p}
                  onClick={() => void markPercent(t, p)}
                >
                  {p}%
                </button>
              ))}
              <input
                type="number"
                min={0}
                max={100}
                inputMode="numeric"
                className="sched-mark__exact"
                aria-label={`Exact percent for ${t.name}`}
                placeholder="%"
                value={exactPct[t.id] ?? ""}
                disabled={markBusy !== null}
                onChange={(e) => setExactPct((prev) => ({ ...prev, [t.id]: e.target.value }))}
              />
              <button
                type="button"
                className="btn btn--secondary"
                aria-label={`Set exact percent for ${t.name}`}
                disabled={markBusy !== null || !(exactPct[t.id] ?? "").trim().length}
                onClick={() => markExact(t)}
              >
                Set
              </button>
            </>
          ) : null}

          {t.is_milestone ? (
            <label className="sched-mark__done">
              <input
                type="checkbox"
                aria-label={`Done ${t.name}`}
                checked={t.percent_done === 100}
                disabled={markBusy !== null}
                onChange={(e) => void markMilestone(t, e.target.checked)}
              />{" "}
              Done
            </label>
          ) : null}

          {t.is_delivery ? (
            <>
              <input
                type="date"
                className="sched-mark__date"
                aria-label={`Delivered date for ${t.name}`}
                value={deliveredDraft[t.id] ?? t.delivered_date ?? today}
                disabled={markBusy !== null}
                onChange={(e) =>
                  setDeliveredDraft((prev) => ({ ...prev, [t.id]: e.target.value }))
                }
              />
              <button
                type="button"
                className="btn btn--secondary"
                aria-label={`Mark ${t.name} delivered`}
                disabled={markBusy !== null}
                onClick={() => void markDelivered(t)}
              >
                {t.delivered_date ? "Update date" : "Delivered"}
              </button>
              {/* Marking this task delivered records that the SCHEDULE line is done. It does
                  not record WHAT arrived — that is a receipt against the material ledger, on
                  the materials page, and the two are separate acts with separate records.
                  Saying so here, with the way to go and do it, is more honest than implying a
                  link the data model does not have. */}
              {onOpenMaterials ? (
                <button type="button" className="btn btn--secondary" onClick={onOpenMaterials}>
                  Receive materials →
                </button>
              ) : null}
            </>
          ) : null}

          {provenance ? <span className="sched-mark__label">{provenance}</span> : null}
        </div>
      ) : null}

      {/* The OFFICE half of the same disclosure — a different capability from the field's
          mark-off, so it is gated separately rather than folded into the strip above. */}
      {canManage && open && !editing ? (
        <div className="sched-rowops">
          <span className="sched-rowops__label">Office</span>
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            aria-label={`Edit ${t.name}`}
            disabled={taskBusy}
            onClick={onEdit}
          >
            Edit task
          </button>
          <ConfirmDelete
            actionLabel="Remove task"
            ariaLabel={`Remove task ${t.name}`}
            copy="Remove this task from the schedule? Its history is kept, and re-importing the schedule can bring it back."
            busy={taskBusy}
            onConfirm={() => void onRemove()}
          />
        </div>
      ) : null}

      {canManage && editing ? (
        <ScheduleTaskEditor
          mode="edit"
          ariaScope={t.name}
          initial={formFromTask(t)}
          busy={taskBusy}
          onSave={onSaveEdit}
          onCancel={onCancelEdit}
        />
      ) : null}
    </li>
  );
}

/** The progress readout: a CSS meter for the eye, the block-character bar for a screen
 *  reader and for print. The text is not decoration — it is the page's actual accessible
 *  rendering of progress, and it is what survives a stylesheet failing to load. */
function ProgressReadout({ percent }: { percent: number }) {
  return (
    <>
      <span className="sched-task__prog-meter" aria-hidden="true">
        <span className="sched-task__prog-fill" style={{ width: `${percent}%` }} />
      </span>
      <span className="sched-task__prog-pct" aria-hidden="true">
        {percent}%
      </span>
      <span className="u-sr-only">{progressBar(percent)}</span>
    </>
  );
}
