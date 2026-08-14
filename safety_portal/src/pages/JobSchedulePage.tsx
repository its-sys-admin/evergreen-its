import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../lib/fieldops_schedules";
import type { ScheduleListRow, ScheduleTaskRow } from "../lib/fieldops_schedules";
import { ScheduleValidatePage } from "./ScheduleValidatePage";
import { ScheduleReconcilePage } from "./ScheduleReconcilePage";
import { PaymentsSection } from "./JobSchedulePaymentsSection";
import { ScheduleTimeline } from "../components/ScheduleTimeline";
import {
  groupTasks,
  matchesFilter,
  summarize,
  todayPacific,
  type ScheduleFilter,
} from "../lib/schedule_view";
import { useAuth } from "../lib/auth";
import { errorText } from "../lib/errorCopy";
import { PageShell } from "../components/PageShell";
import { ConfirmDelete } from "../components/ChecklistItemForm";
import { TaskRow } from "../components/ScheduleTaskRow";
import { EMPTY_TASK_FORM, ScheduleTaskEditor } from "../components/ScheduleTaskEditor";
import { useScheduleMarks } from "../lib/useScheduleMarks";
import { ExpectedMaterialsSection } from "../components/ExpectedMaterialsSection";

// Per-job SCHEDULE page (ADR-0006) — /jobs/:jobId/schedule, the deep-link target from the
// Job Tracker's "Open schedule →" card.
//
// It is the home of the LIVING TASK LIST: what the committed project schedule says this job
// is doing, section by section, with dates, durations and progress.
//   • Everyone with cap.jobtracker.read sees the task list (operator decision 4 — schedule
//     visibility is all-roles).
//   • cap.jobtracker.manage (admin/office) additionally gets the IMPORT surface: upload a
//     Smartsheet Gantt PDF export, watch it get read on the office Mac, then check the
//     OCR-proposed grid on the validate SUB-FACE (ScheduleValidatePage — the
//     ManifestValidatePage pattern, remount-keyed, NOT a router entry) and commit it.
//   • cap.schedule.mark (PR-5 — submitter/manager/admin, per-job scoped Worker-side) gets
//     the mark-off controls (operator decision 8): quick-% chips + an exact-% input on
//     ordinary tasks, a done-mark on milestones, a delivered-date control on Deliveries
//     tasks. Optimistic row update, then a reload — the server's row is the record.
// The Worker re-gates every call; capability checks here drive affordances only
// (Invariant 2 — SPA gating is convenience, never the boundary).
//
// TWO sub-faces, routed by whether the job already HAS a task list (the plan's degenerate
// flag made local): no tasks → the first-import VALIDATE face (ScheduleValidatePage); tasks
// exist → the revision RECONCILE face (ScheduleReconcilePage — the three-way diff review).
// Both are remount-keyed sub-faces, never router entries; the Worker's own plan re-derives
// the truth either way, so a mis-route degrades to an honest banner, never a wrong commit.
//
// ── LAYOUT (2026-08 design pass) ──────────────────────────────────────────────────────
// The page used to be a 5-column table inside the 920px reading well, with five percent
// chips, a number input and a Set button crammed into one cell of every row. It did not fit
// a laptop and could not be worked on a phone. What replaced it:
//
//   • A HERO answering the schedule's first question — overall progress, and how much is
//     late — before any control.
//   • SECTION GROUPS that collapse, each carrying its own rollup, because that is how a
//     superintendent reads a schedule.
//   • A SEARCH + FILTER row, so a 300-task import is navigable at all.
//   • A TIMELINE view (ScheduleTimeline) — the signature. See that file for why.
//   • MARK-OFF as a per-row disclosure with 48px targets, so the list stays scannable and
//     the controls stay thumb-sized. One row open at a time.
//   • The office surfaces (import, payments) as drawers BELOW the task list, so the
//     job-site face is what the page opens on.
//
// Three facts the old page threw away are now derived in `lib/schedule_view.ts` and shown:
// LATE (past finish, under 100%), SLIP against the immutable baseline anchor, and the
// server's `truncated` flag — which meant the page was silently showing a partial schedule.

/** Upload-list status → chip copy. `superseded` reads as revision history on purpose —
 *  those rows are the job's prior governing schedules, kept, never a failure state. */
function statusChip(s: ScheduleListRow): { label: string; className: string } {
  switch (s.status) {
    case "pending":
    case "claimed":
      return { label: "Being read…", className: "dash-pill" };
    case "parsed":
      return { label: "Ready to check", className: "dash-pill dash-pill--warn" };
    case "committing":
      return { label: "Import stopped part-way", className: "dash-pill dash-pill--warn" };
    case "committed":
      return { label: "Governing schedule", className: "dash-pill dash-pill--ok" };
    case "superseded":
      return { label: "Revision history", className: "dash-pill" };
    case "refused":
      return { label: `Refused${s.detail ? ` — ${s.detail}` : ""}`, className: "dash-pill dash-pill--danger" };
    default:
      return { label: s.status, className: "dash-pill" };
  }
}

function errText(e: unknown, fallback: string): string {
  if (e && typeof e === "object" && "code" in e) {
    const code = (e as { code: string | null }).code;
    if (code) return errorText(code);
  }
  return fallback;
}

/** The quick-% chips (operator decision 8). */

const FILTERS: { key: ScheduleFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "open", label: "In progress" },
  { key: "late", label: "Late" },
  { key: "milestones", label: "Milestones" },
  { key: "deliveries", label: "Deliveries" },
];

export function JobSchedulePage({
  jobId,
  onHome,
  onOpenJob,
  onOpenMaterials,
}: {
  jobId: string;
  onHome: () => void;
  onOpenJob: (jobId: string) => void;
  /** Passed only when the session holds cap.materials.receive. The schedule page shows
   *  delivery CONTEXT but hands receiving off to the materials page — see the note on the
   *  delivery controls below for why the two are deliberately not conflated. */
  onOpenMaterials?: (jobId: string) => void;
}) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.jobtracker.manage");
  const canMark = caps.includes("cap.schedule.mark");
  // Payments (PR-7): admin-only office surface (operator decision 4). The check gates the
  // AFFORDANCE — the Worker re-gates every /api/fieldops/payments call (Invariant 2).
  const canPayments = caps.includes("cap.payments.manage");
  // Materials: the same two caps ExpectedMaterialsSection itself checks. Gating the DRAWER on
  // them too means a session without either never renders the word "Materials" and never
  // triggers the section's fetch.
  const canMaterials =
    caps.includes("cap.materials.receive") || caps.includes("cap.materials.manage");

  const [tasks, setTasks] = useState<ScheduleTaskRow[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [projectName, setProjectName] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Import surface state (canManage only). Own single-flight, separate from the task read.
  const [schedules, setSchedules] = useState<ScheduleListRow[] | null>(null);
  const [openSchedule, setOpenSchedule] = useState<number | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);

  // Mark-off state (canMark only): one in-flight mark at a time keeps double-taps out
  // (the materials-page busy posture); the drafts hold each row's exact-% text and
  // delivered-date pick until its button fires.

  // View state. Mark strips open per row and STAY open — end-of-day marking walks several
  // tasks at once, so closing the last one on every tap would fight the actual job. The list
  // is still tight because nothing is open until asked for.
  const [view, setView] = useState<"list" | "timeline">("list");
  const [filter, setFilter] = useState<ScheduleFilter>("all");
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [openRows, setOpenRows] = useState<Set<number>>(() => new Set());
  // Hand-editing (canManage). One form at a time: `editingTask` is a task id, `addingIn`
  // a section-group key. Both null means nothing is open.
  const [editingTask, setEditingTask] = useState<number | null>(null);
  const [addingIn, setAddingIn] = useState<string | null>(null);
  const [taskBusy, setTaskBusy] = useState(false);

  // Today is resolved ONCE per mount and threaded down, so every "is this late" answer on
  // the page agrees with every other one.
  const today = useMemo(() => todayPacific(), []);

  const loadTasks = useCallback(() => {
    setLoadError(null);
    api
      .fetchScheduleTasks(jobId)
      .then((d) => {
        setTasks(d.tasks);
        setProjectName(d.project_name);
        setTruncated(Boolean(d.truncated));
      })
      .catch(() => setLoadError("Could not load this job's schedule."));
  }, [jobId]);

  // The uploads list is a SEPARATE read: it must survive a task-load failure (an upload
  // still needs discarding even if the list read is down), and it refreshes on its own
  // after an upload or a commit.
  const loadSchedules = useCallback(() => {
    if (!canManage) return;
    api
      .fetchSchedules(jobId)
      .then((d) => setSchedules(d.schedules))
      .catch(() => setSchedules([]));
  }, [jobId, canManage]);

  useEffect(() => {
    loadTasks();
  }, [loadTasks]);
  useEffect(() => {
    loadSchedules();
  }, [loadSchedules]);

  const stats = useMemo(() => summarize(tasks ?? [], today), [tasks, today]);

  // Filter FIRST, then group: a section whose every task is filtered out should disappear
  // rather than sit there as an empty heading.
  const groups = useMemo(() => {
    const visible = (tasks ?? []).filter((t) => matchesFilter(t, filter, query, today));
    return groupTasks(visible, today);
  }, [tasks, filter, query, today]);

  const hiddenCount = (tasks?.length ?? 0) - groups.reduce((n, g) => n + g.tasks.length, 0);

  // Mark-off semantics shared with the Site Tasks page (src/lib/useScheduleMarks — §14).
  const {
    markBusy, exactPct, setExactPct, deliveredDraft, setDeliveredDraft,
    markPercent, markExact, markMilestone, markDelivered,
  } = useScheduleMarks({ setTasks, reload: loadTasks, setMsg, today });


  async function uploadScheduleFile(file: File) {
    if (uploadBusy) return;
    if (file.size > api.SCHEDULE_MAX_BYTES) {
      setMsg({ ok: false, text: "That PDF is too large to import — re-export the schedule and try again." });
      return;
    }
    setUploadBusy(true);
    setMsg(null);
    try {
      await api.uploadSchedule(jobId, file);
      loadSchedules();
      setMsg({
        ok: true,
        text: "Uploaded. It will be read in a minute or two — refresh to see it ready to check.",
      });
    } catch (e) {
      setMsg({ ok: false, text: errText(e, "Could not upload that file.") });
    } finally {
      setUploadBusy(false);
    }
  }

  function toggleSection(key: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleRow(id: number) {
    setOpenRows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  /** One in-flight task write at a time; reload after — the server's row is the record.
   *  Returns true so callers can close their form only on success. */
  async function runTaskWrite(fn: () => Promise<unknown>, failText: string): Promise<boolean> {
    if (taskBusy) return false;
    setTaskBusy(true);
    setMsg(null);
    try {
      await fn();
      return true;
    } catch (e) {
      setMsg({ ok: false, text: errText(e, failText) });
      return false;
    } finally {
      setTaskBusy(false);
      loadTasks();
    }
  }

  /** A draft arrives already validated, or as the Worker's own error code — translate it
   *  through the one vocabulary rather than inventing a second. */
  function refuseDraft(draft: api.ScheduleTaskDraft | string): draft is string {
    if (typeof draft === "string") {
      setMsg({ ok: false, text: errorText(draft) });
      return true;
    }
    return false;
  }

  function saveTaskEdit(id: number, draft: api.ScheduleTaskDraft | string) {
    if (refuseDraft(draft)) return;
    void runTaskWrite(
      () => api.editScheduleTask(id, draft),
      "Could not save that task.",
    ).then((ok) => {
      if (ok) setEditingTask(null);
    });
  }

  function saveNewTask(draft: api.ScheduleTaskDraft | string) {
    if (refuseDraft(draft)) return;
    // Append: one past the highest ordinal in the job. The list groups by section in
    // first-seen order, so the new row lands at the end of its own section rather than
    // in an arbitrary place — and no existing task's position moves.
    const sortOrder = (tasks ?? []).reduce((m, t) => Math.max(m, t.sort_order), 0) + 1;
    void runTaskWrite(
      () => api.addScheduleTask(jobId, { ...draft, sort_order: sortOrder }),
      "Could not add that task.",
    ).then((ok) => {
      if (ok) setAddingIn(null);
    });
  }

  function removeTask(t: ScheduleTaskRow) {
    return runTaskWrite(
      () => api.deactivateScheduleTask(t.id),
      "Could not remove that task.",
    );
  }

  // ── The validate / reconcile SUB-FACES (remount-keyed; not router entries) ─────────
  // Routed by the task list: no tasks → first import (validate); tasks → revision
  // reconcile. While the task read is still in flight the route is unknown — hold,
  // rather than guessing a face the Worker's plan would immediately contradict.
  if (openSchedule !== null) {
    const onCloseFace = (notice?: { ok: boolean; text: string }, committed?: boolean) => {
      setOpenSchedule(null);
      if (notice) setMsg(notice);
      loadSchedules();
      // A commit authors/updates the living task list, so the list behind this screen
      // is stale the moment we finish.
      if (committed) loadTasks();
    };
    return (
      <PageShell onHome={onHome} wide>
        {tasks === null ? (
          <section className="card dash-section" aria-label="Loading schedule">
            <p className="dash__intro">Loading the task list…</p>
          </section>
        ) : tasks.length > 0 ? (
          <ScheduleReconcilePage key={openSchedule} scheduleId={openSchedule} onClose={onCloseFace} />
        ) : (
          <ScheduleValidatePage key={openSchedule} scheduleId={openSchedule} onClose={onCloseFace} />
        )}
      </PageShell>
    );
  }

  const hasUploads = (schedules?.length ?? 0) > 0;
  const empty = tasks !== null && tasks.length === 0;
  const hasTasks = tasks !== null && tasks.length > 0;

  return (
    <PageShell onHome={onHome} wide>
      <div className="dash-back-btn">
        <button type="button" className="btn btn--secondary" onClick={() => onOpenJob(jobId)}>
          ← Back to job
        </button>
      </div>

      {/* The job NAME, not the JOB-###### key (the Materials-page precedent) — the key is a
          system identifier the field never speaks; the id falls back only while loading. */}
      <p className="page__heading--eyebrow">Project schedule</p>
      <h1 className="page__heading">Schedule — {projectName ?? jobId}</h1>

      {msg && (
        <p className={msg.ok ? "dash__msg" : "login__error"} role={msg.ok ? "status" : "alert"}>
          {msg.text}
        </p>
      )}

      {loadError && (
        <p className="login__error" role="alert">
          {loadError}{" "}
          <button type="button" className="btn btn--secondary" onClick={loadTasks}>
            Retry
          </button>
        </p>
      )}

      {/* Never-silent: the read caps at 600 tasks but a commit can author up to 2000, so a
          truncated read means this page is showing a PARTIAL schedule. It used to say
          nothing at all. */}
      {truncated && (
        <p className="wpr-banner wpr-banner--warn" role="status">
          This job has more tasks than the page can load at once, so the list below is not the
          whole schedule. Use search to find a specific task.
        </p>
      )}

      {tasks === null && !loadError && (
        <div aria-label="Loading schedule" aria-busy="true">
          <div className="sched-skel" />
          <div className="sched-skel" />
          <div className="sched-skel" />
        </div>
      )}

      {/* ── Hero: the schedule's first question, answered before any control ─────────── */}
      {hasTasks && (
        <div className="sched-hero">
          <div className="sched-hero__figure">
            <span className="sched-hero__pct">{stats.percent}</span>
            <span className="sched-hero__pct-unit">% complete</span>
          </div>
          <div>
            <div
              className="sched-hero__meter"
              role="img"
              aria-label={`Overall progress ${stats.percent}%`}
            >
              <div className="sched-hero__meter-fill" style={{ width: `${stats.percent}%` }} />
            </div>
            <ul className="sched-hero__stats">
              <li className="sched-hero__stat">
                <b>{stats.done}</b> of <b>{stats.total}</b> tasks complete
              </li>
              {stats.late > 0 && (
                <li className="sched-hero__stat sched-hero__stat--late">
                  <b>{stats.late}</b> past its finish date
                </li>
              )}
              {stats.nextMilestone && (
                <li className="sched-hero__stat">
                  Next milestone <b>{stats.nextMilestone.name}</b> on{" "}
                  <b>{stats.nextMilestone.date}</b>
                </li>
              )}
            </ul>
          </div>
        </div>
      )}

      {/* ── Toolbar ──────────────────────────────────────────────────────────────────── */}
      {hasTasks && (
        <div className="sched-toolbar">
          <input
            type="search"
            className="sched-toolbar__search"
            placeholder="Search tasks and sections"
            aria-label="Search tasks"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="sched-filters" role="group" aria-label="Filter tasks">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                className="sched-chip"
                aria-pressed={filter === f.key}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
                {f.key === "late" && stats.late > 0 ? (
                  <span className="sched-chip__count">{stats.late}</span>
                ) : null}
              </button>
            ))}
          </div>
          <div className="sched-views">
            <button
              type="button"
              className={`admin-tabs__tab${view === "list" ? " admin-tabs__tab--active" : ""}`}
              aria-pressed={view === "list"}
              onClick={() => setView("list")}
            >
              List
            </button>
            <button
              type="button"
              className={`admin-tabs__tab${view === "timeline" ? " admin-tabs__tab--active" : ""}`}
              aria-pressed={view === "timeline"}
              onClick={() => setView("timeline")}
            >
              Timeline
            </button>
          </div>
        </div>
      )}

      {empty && (
        <div className="sched-empty">
          <p className="sched-empty__title">No tasks yet</p>
          <p>
            {canManage
              ? hasUploads
                ? "No tasks yet — check a finished upload below and import it."
                : "No schedule yet. Upload the project-schedule PDF export below to start the task list."
              : "No schedule has been imported for this job yet."}
          </p>
        </div>
      )}

      {hasTasks && groups.length === 0 && (
        <div className="sched-empty">
          <p className="sched-empty__title">Nothing matches</p>
          <p>No task matches that search or filter. Clear them to see all {tasks.length} tasks.</p>
        </div>
      )}

      {/* ── TIMELINE view ────────────────────────────────────────────────────────────── */}
      {view === "timeline" && groups.length > 0 && (
        <ScheduleTimeline groups={groups} today={today} />
      )}

      {/* ── LIST view ────────────────────────────────────────────────────────────────── */}
      {view === "list" &&
        groups.map((group) => {
          const key = group.name ?? "__none";
          const isCollapsed = collapsed.has(key);
          return (
            <section key={key} className="sched-group" aria-label={group.name ?? "Tasks"}>
              <button
                type="button"
                className="sched-group__head"
                aria-expanded={!isCollapsed}
                onClick={() => toggleSection(key)}
              >
                <span className="sched-group__caret" aria-hidden="true">
                  ▸
                </span>
                <span className="sched-group__title">
                  {group.name ?? "Tasks"}{" "}
                  <span className="sched-group__count">
                    {group.tasks.length} {group.tasks.length === 1 ? "task" : "tasks"}
                    {group.late > 0 ? ` · ${group.late} late` : ""}
                  </span>
                </span>
                <span className="sched-group__roll">
                  <span className="sched-group__roll-meter" aria-hidden="true">
                    <span
                      className="sched-group__roll-fill"
                      style={{ width: `${group.percent}%` }}
                    />
                  </span>
                  <span className="sched-group__roll-pct">{group.percent}%</span>
                </span>
              </button>

              {!isCollapsed && (
                <ul className="sched-list">
                  {group.tasks.map((t) => (
                    <TaskRow
                      key={t.id}
                      t={t}
                      today={today}
                      canMark={canMark}
                      canManage={canManage}
                      markBusy={markBusy}
                      taskBusy={taskBusy}
                      open={openRows.has(t.id)}
                      onToggle={() => toggleRow(t.id)}
                      editing={editingTask === t.id}
                      onEdit={() => {
                        setEditingTask(t.id);
                        setOpenRows((prev) => new Set(prev).add(t.id));
                      }}
                      onCancelEdit={() => setEditingTask(null)}
                      onSaveEdit={(d) => saveTaskEdit(t.id, d)}
                      onRemove={() => removeTask(t)}
                      exactPct={exactPct}
                      setExactPct={setExactPct}
                      deliveredDraft={deliveredDraft}
                      setDeliveredDraft={setDeliveredDraft}
                      markPercent={markPercent}
                      markExact={markExact}
                      markMilestone={markMilestone}
                      markDelivered={markDelivered}
                      onOpenMaterials={onOpenMaterials ? () => onOpenMaterials(jobId) : undefined}
                    />
                  ))}
                </ul>
              )}

              {/* Add sits at the FOOT of a section because a task belongs to a section, and
                  that is where a reader is standing when they notice one missing. */}
              {canManage && !isCollapsed ? (
                addingIn === key ? (
                  <div className="sched-group__add">
                    <ScheduleTaskEditor
                      mode="add"
                      ariaScope="new task"
                      initial={{ ...EMPTY_TASK_FORM, section: group.name ?? "" }}
                      busy={taskBusy}
                      onSave={saveNewTask}
                      onCancel={() => setAddingIn(null)}
                    />
                  </div>
                ) : (
                  <div className="sched-group__add">
                    <button
                      type="button"
                      className="btn btn--secondary btn--sm"
                      aria-label={`Add a task to ${group.name ?? "Tasks"}`}
                      onClick={() => setAddingIn(key)}
                    >
                      + Add a task
                    </button>
                  </div>
                )
              ) : null}
            </section>
          );
        })}

      {hiddenCount > 0 && (
        <p className="dash__intro">
          {hiddenCount} {hiddenCount === 1 ? "task is" : "tasks are"} hidden by the current search
          or filter.
        </p>
      )}

      {/* ── Office drawers ───────────────────────────────────────────────────────────────
          Below the task list on purpose: the job-site face is what the page opens on. Both
          are <details> so they are keyboard- and screen-reader-native with no dependency,
          and the import drawer opens itself on a job that has no schedule yet — the one
          case where importing IS the page's job. */}
      {canManage ? (
        <details className="sched-drawer" open={empty}>
          <summary>
            Import a schedule
            {hasUploads ? (
              <span className="sched-drawer__note">
                {schedules?.length} {schedules?.length === 1 ? "upload" : "uploads"}
              </span>
            ) : null}
          </summary>
          <div className="sched-drawer__body">
            <p className="dash__intro">
              Upload the project schedule as a PDF (the Smartsheet export). It is read on the
              office Mac and comes back as a task grid you check side-by-side with the source
              pages — nothing reaches the task list until you import it. Schedule revisions are
              supported: a new export uploaded onto a job with a task list opens a reconcile
              review — date changes, new tasks, removals and progress conflicts — before
              anything applies.
            </p>
            <div className="dash-row">
              <input
                type="file"
                accept={api.SCHEDULE_ACCEPT}
                aria-label="Schedule PDF"
                disabled={uploadBusy}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  // Clear the picker so re-choosing the SAME file after a failure still fires.
                  e.target.value = "";
                  if (f) void uploadScheduleFile(f);
                }}
              />
              {uploadBusy ? <span>Uploading…</span> : null}
            </div>

            {hasUploads ? (
              <div style={{ overflowX: "auto" }}>
                <table className="dash-table dash-table--stack">
                  <thead>
                    <tr>
                      <th scope="col">File</th>
                      <th scope="col">Status</th>
                      <th scope="col">Rows</th>
                      <th scope="col" />
                    </tr>
                  </thead>
                  <tbody>
                    {(schedules ?? []).map((s) => {
                      const chip = statusChip(s);
                      const validatable = s.status === "parsed" || s.status === "committing";
                      // The button says what OPENING it does: a job with a task list gets
                      // the reconcile review, an empty one the first-import check.
                      const openVerb = (tasks?.length ?? 0) > 0 ? "Reconcile" : "Validate";
                      const discardable =
                        s.status !== "committed" && s.status !== "superseded";
                      return (
                        <tr key={s.id}>
                          <td data-cell="File">{s.filename}</td>
                          <td data-cell="Status">
                            <span className={chip.className}>{chip.label}</span>
                          </td>
                          <td data-cell="Rows">{s.row_count ?? "—"}</td>
                          <td>
                            {validatable ? (
                              <button
                                type="button"
                                className="btn btn--secondary"
                                aria-label={`${openVerb} ${s.filename}`}
                                onClick={() => setOpenSchedule(s.id)}
                              >
                                {openVerb} →
                              </button>
                            ) : null}
                            {discardable ? (
                              <>
                                {" "}
                                <ConfirmDelete
                                  actionLabel="Remove"
                                  ariaLabel={`Remove ${s.filename}`}
                                  copy={
                                    s.status === "committing"
                                      ? "Stop this import and remove it? Tasks already imported stay on the list."
                                      : "Remove this upload from the list?"
                                  }
                                  busy={uploadBusy}
                                  onConfirm={() =>
                                    void (async () => {
                                      try {
                                        await api.discardSchedule(s.id);
                                      } catch {
                                        /* surfaced by the list not changing; refresh below */
                                      }
                                      loadSchedules();
                                    })()
                                  }
                                />
                              </>
                            ) : null}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        </details>
      ) : null}

      {/* Materials — the SAME ExpectedMaterialsSection the per-job Materials page renders,
          mounted here rather than reimplemented (its own docstring says it is mountable
          anywhere that has somewhere to navigate to). That matters: the delivery ledger is
          APPEND-ONLY with no delete path, so a second, subtly-different receiving UI is a
          liability. One component, one behaviour, two places it can be reached from.

          Why it belongs on the schedule page at all: the schedule's Deliveries tasks are the
          only place the plan says material is due, and marking one only records that the
          SCHEDULE line is done — the receipt against the ledger is a separate act. Putting
          the real tracking one drawer away closes that gap without pretending the two records
          are joined. They are not: nothing in the schema links a task to a receipt (the only
          shared key is job_id), so this page will not claim otherwise. */}
      {canMaterials ? (
        <details className="sched-drawer">
          <summary>
            Materials
            <span className="sched-drawer__note">Expected lines, loads and delivery marks</span>
          </summary>
          <div className="sched-drawer__body">
            <ExpectedMaterialsSection
              jobId={jobId}
              onOpenMaterials={onOpenMaterials ? () => onOpenMaterials(jobId) : undefined}
            />
          </div>
        </details>
      ) : null}

      {/* Payments — SAME PAGE, office-only (operator decisions: payments live where the
          job's schedule lives; admin-only visibility). Below the schedule so the job-site
          face stays first. */}
      {canPayments ? <PaymentsSection jobId={jobId} /> : null}
    </PageShell>
  );
}

