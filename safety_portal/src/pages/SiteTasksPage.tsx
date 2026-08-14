import { useCallback, useEffect, useMemo, useState } from "react";
import * as schedApi from "../lib/fieldops_schedules";
import type { ScheduleTaskRow } from "../lib/fieldops_schedules";
import { fetchJobList } from "../lib/fieldops_jobtracker";
import type { JobRow } from "../../worker/wire-types";
import { fetchJobTasks, setTaskStatus, type JobTasksResponse } from "../lib/fieldops_tasks";
import { groupTasks, summarize, todayPacific } from "../lib/schedule_view";
import { useScheduleMarks } from "../lib/useScheduleMarks";
import { TaskRow } from "../components/ScheduleTaskRow";
import { PageShell } from "../components/PageShell";
import { useAuth } from "../lib/auth";
import { statusLabel } from "../lib/labels";
import type { MarkMsg } from "../lib/useScheduleMarks";

// Site Tasks (Track A4) — ONE page that answers "what needs doing on this job": the
// schedule-generated living task list (deterministically authored at every schedule commit,
// %-markable here exactly as on the Job Schedule page — same TaskRow, same useScheduleMarks,
// nothing forked) PLUS the person-assigned one-off tasks. Job-selectable via the drop-down
// (the FormFillPage select pattern), defaulting to the routed job, else the viewer's own
// placement. Read tier is cap.jobtracker.read (all roles — ADR-0006 decision 4); the mark
// strip appears only with cap.schedule.mark, and assigned-task status buttons follow the
// write route's own-only rule (privileged viewers excepted) — the Worker re-enforces both.

const EMPTY_SCHED_MSG =
  "No schedule has been imported for this job yet — schedule tasks appear here after the office commits one.";

function statusPill(status: string): string {
  return status === "done"
    ? "dash-pill dash-pill--ok"
    : status === "in_progress"
      ? "dash-pill dash-pill--warn"
      : "dash-pill";
}

export function SiteTasksPage(props: {
  jobId?: string;
  onBack: () => void;
  onOpenSchedule?: (jobId: string) => void;
  /** Keep the URL in step with the drop-down (replace, not push — a flip is within-page state). */
  onJobChange?: (jobId: string | null) => void;
}) {
  const { jobId: routedJobId, onBack, onOpenSchedule, onJobChange } = props;
  const { user } = useAuth();
  const caps = useMemo(() => new Set(user?.capabilities ?? []), [user]);
  const canMark = caps.has("cap.schedule.mark");

  const [jobs, setJobs] = useState<JobRow[] | null>(null);
  const [viewerJob, setViewerJob] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(routedJobId ?? null);
  const [tasks, setTasks] = useState<ScheduleTaskRow[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [assigned, setAssigned] = useState<JobTasksResponse | null>(null);
  const [msg, setMsg] = useState<MarkMsg>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [busyIds, setBusyIds] = useState<Set<number>>(new Set());

  const today = todayPacific();

  // Job list once; the default job is the route's, else the viewer's placement.
  useEffect(() => {
    let alive = true;
    void fetchJobList("active").then(
      (r) => {
        if (!alive) return;
        setJobs(r.jobs);
        setViewerJob(r.viewer_current_job ?? null);
      },
      () => {
        if (alive) setJobs([]);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (jobId === null && viewerJob !== null) {
      setJobId(viewerJob);
      onJobChange?.(viewerJob);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps — the placement default fires once
  }, [viewerJob]);

  const loadTasks = useCallback(() => {
    if (!jobId) return;
    void schedApi.fetchScheduleTasks(jobId).then(
      (r) => {
        setTasks(r.tasks);
        setTruncated(r.truncated);
      },
      () => setMsg({ ok: false, text: "Could not load the schedule tasks." }),
    );
  }, [jobId]);

  const loadAssigned = useCallback(() => {
    if (!jobId) return;
    void fetchJobTasks(jobId).then(
      (r) => setAssigned(r),
      () => setMsg({ ok: false, text: "Could not load the assigned tasks." }),
    );
  }, [jobId]);

  useEffect(() => {
    setTasks(null);
    setAssigned(null);
    setMsg(null);
    setOpenId(null);
    loadTasks();
    loadAssigned();
  }, [loadTasks, loadAssigned]);

  const marks = useScheduleMarks({ setTasks, reload: loadTasks, setMsg, today });
  const groups = useMemo(() => groupTasks(tasks ?? [], today), [tasks, today]);
  const stats = useMemo(() => summarize(tasks ?? [], today), [tasks, today]);

  async function changeStatus(taskId: number, status: "in_progress" | "done") {
    setBusyIds((prev) => new Set(prev).add(taskId));
    try {
      await setTaskStatus(taskId, status);
      loadAssigned();
    } catch {
      setMsg({ ok: false, text: "Could not update that task." });
    } finally {
      setBusyIds((prev) => {
        const n = new Set(prev);
        n.delete(taskId);
        return n;
      });
    }
  }

  const canTouch = (t: { personnel_id: number | null }): boolean =>
    assigned !== null &&
    (assigned.viewer_privileged ||
      (assigned.viewer_personnel_id !== null && t.personnel_id === assigned.viewer_personnel_id));

  return (
    <PageShell onHome={onBack}>
      <h1 className="page__heading">Site tasks</h1>
      <p className="dash-hint">
        Everything on this job&apos;s plate — the schedule&apos;s tasks with progress mark-off,
        plus assigned one-off tasks.
      </p>

      <label className="wpr-field" style={{ maxWidth: "26rem" }}>
        <span className="wpr-field__label">Job</span>
        <select
          className="wpr-field__input"
          aria-label="Job"
          value={jobId ?? ""}
          onChange={(e) => {
            const v = e.target.value || null;
            setJobId(v);
            onJobChange?.(v);
          }}
        >
          <option value="">Select a job…</option>
          {(jobs ?? []).map((j) => (
            <option key={j.job_id} value={j.job_id}>{j.project_name}</option>
          ))}
        </select>
      </label>

      {msg && (
        <p className={`wpr-banner ${msg.ok ? "wpr-banner--ok" : "wpr-banner--warn"}`} role="status">
          {msg.text}
        </p>
      )}

      {jobId === null ? (
        <p className="dash-hint">Pick a job to see its tasks.</p>
      ) : (
        <>
          <section className="card dash-section" id="st-schedule">
            <header className="job-sec__head">
              <h2 className="job-sec__title">Schedule tasks</h2>
              {tasks !== null && tasks.length > 0 && (
                <span className="dash-card__sub">
                  {stats.percent}% overall · {stats.late} late · {tasks.length} task(s)
                </span>
              )}
            </header>
            {truncated && (
              <p className="wpr-banner wpr-banner--warn">
                Showing the first {tasks?.length} tasks — open the schedule page for the full list.
              </p>
            )}
            {tasks !== null && tasks.length === 0 && (
              <p className="dash-hint">
                {EMPTY_SCHED_MSG}
                {onOpenSchedule && (
                  <>
                    {" "}
                    <button type="button" className="btn btn--secondary" onClick={() => onOpenSchedule(jobId)}>
                      Open the schedule page →
                    </button>
                  </>
                )}
              </p>
            )}
            {groups.map((group) => (
              <div key={group.name ?? "(no section)"} className="sched-group">
                {group.name !== null && (
                  <h3 className="sched-group__head">
                    {group.name}
                    <span className="dash-card__sub"> {group.percent}%</span>
                  </h3>
                )}
                <ul className="sched-list">
                  {group.tasks.map((t) => (
                    <TaskRow
                      key={t.id}
                      t={t}
                      today={today}
                      canMark={canMark}
                      canManage={false}
                      markBusy={marks.markBusy}
                      taskBusy={false}
                      open={openId === t.id}
                      onToggle={() => setOpenId(openId === t.id ? null : t.id)}
                      editing={false}
                      onEdit={() => {}}
                      onCancelEdit={() => {}}
                      onSaveEdit={() => {}}
                      onRemove={async () => false}
                      exactPct={marks.exactPct}
                      setExactPct={marks.setExactPct}
                      deliveredDraft={marks.deliveredDraft}
                      setDeliveredDraft={marks.setDeliveredDraft}
                      markPercent={marks.markPercent}
                      markExact={marks.markExact}
                      markMilestone={marks.markMilestone}
                      markDelivered={marks.markDelivered}
                    />
                  ))}
                </ul>
              </div>
            ))}
          </section>

          <section className="card dash-section" id="st-assigned">
            <header className="job-sec__head">
              <h2 className="job-sec__title">Assigned tasks</h2>
            </header>
            {assigned !== null && assigned.tasks.length === 0 && (
              <p className="dash-hint">No one-off tasks are assigned on this job.</p>
            )}
            <ul className="dash-tasklist">
              {(assigned?.tasks ?? []).map((t) => (
                <li key={t.id}>
                  <span className={statusPill(t.status)}>{statusLabel(t.status)}</span>{" "}
                  <span>
                    {t.description}
                    <span className="dash-card__sub">
                      {" "}
                      {t.assignee_name ? `· ${t.assignee_name}` : "· Unassigned"}
                      {t.due_date ? ` · due ${t.due_date}` : ""}
                    </span>
                  </span>
                  {canTouch(t) && (
                    <>
                      {t.status === "open" && (
                        <>
                          {" "}
                          <button type="button" className="btn btn--secondary"
                                  aria-label={`Start task ${t.id}`}
                                  disabled={busyIds.has(t.id)}
                                  onClick={() => void changeStatus(t.id, "in_progress")}>
                            Start
                          </button>
                        </>
                      )}
                      {t.status !== "done" && (
                        <>
                          {" "}
                          <button type="button" className="btn btn--primary"
                                  aria-label={`Mark task ${t.id} done`}
                                  disabled={busyIds.has(t.id)}
                                  onClick={() => void changeStatus(t.id, "done")}>
                            Done
                          </button>
                        </>
                      )}
                    </>
                  )}
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </PageShell>
  );
}
