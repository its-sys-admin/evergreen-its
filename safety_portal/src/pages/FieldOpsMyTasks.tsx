import { useEffect, useRef, useState } from "react";
import * as api from "../lib/fieldops_tasks";
import { statusLabel } from "../lib/labels";
import { PageShell } from "../components/PageShell";
import { DailyReportTab, type DailyPlacement } from "../components/DailyReportTab";
import { AssignedInspectionsSection } from "../components/AssignedInspectionsSection";
import { AddCrewSection } from "../components/AddCrewSection";
import {
  CompletedDisclosure,
  InlineRowMsg,
  ROSTER_LINK_COPY,
  SectionError,
  SectionLoading,
  SectionRefreshWarn,
  TaskDue,
  errMsg,
  fmtEpochDate,
  type RowFeedback,
} from "../components/myTasksShared";
import { useAuth } from "../lib/auth";
import type { FormPrefill } from "./FormFillPage";

// Status → pill class (mirrors the Job Tracker task-list styling): done = ok, in_progress = warn.
function statusPill(status: string): string {
  if (status === "done") return "dash-pill dash-pill--ok";
  if (status === "in_progress") return "dash-pill dash-pill--warn";
  return "dash-pill";
}

interface JobGroup {
  job_id: string;
  project_name: string | null;
  tasks: api.MyTask[];
}

// Group a flat task list by job, preserving the server order within each group and the order in
// which each job first appears.
function groupByJob(tasks: api.MyTask[]): JobGroup[] {
  const order: string[] = [];
  const byJob = new Map<string, JobGroup>();
  for (const t of tasks) {
    let g = byJob.get(t.job_id);
    if (!g) {
      g = { job_id: t.job_id, project_name: t.project_name, tasks: [] };
      byJob.set(t.job_id, g);
      order.push(t.job_id);
    }
    g.tasks.push(t);
  }
  return order.map((id) => byJob.get(id)!);
}

type Tab = "assigned" | "daily";

/**
 * "My Tasks" (P4 S1 + R2 restructure; Daily tab rebuilt in D2) — TWO TABS:
 *   • "Assigned tasks" — the one-off tasks assigned to the actor (grouped by job, open-first per
 *     the R1 server ordering), the Assigned-inspections section, and the Add-crew disclosure
 *     (cap.crew.create).
 *   • "Daily report" (D2, SOP daily form) — the placed manager's/admin's daily SOP FORM rendered
 *     inline (DailyReportTab: date selector + the daily-report definition + form_link deep-links),
 *     replacing the retired R2 checkbox checklist. Directive 2026-07-03: the tab EXISTS only for
 *     manager/admin accounts with a placement — a subcontractor (submitter) never sees it (their
 *     My Tasks is tasks/inspections-only, no tab strip), and an unplaced manager/admin gets no
 *     tab either. The Worker re-gates every daily surface on the same role set.
 *
 * Both tab panels stay MOUNTED (the inactive one is `hidden`) so each section's single fetch runs
 * once and the daily tab can report its placement up for the auto-switch: on first load, a placed
 * manager with no open one-off tasks lands on the Daily tab.
 *
 * R2 never-silent hardening (Mandatory B): the tasks fetch has distinct loading / error+Retry /
 * empty states (error and empty mutually exclusive); `linked:false` (R1) explains the roster-link
 * gap instead of a lying "no tasks"; status changes are optimistic per-row (busy + inline feedback
 * scoped to the row) with contextual Start / Done / Reopen buttons per the portal button standard.
 *
 * Refresh: a header Refresh control + visibilitychange/focus refetch bump `refreshToken`, which
 * re-runs the page's own tasks fetch and every section's fetch (the Daily tab refetches its
 * placement + filed status on the same token).
 *
 * "Assigned to me" is resolved server-side via the personnel↔account link. The cap gates here are
 * a CONVENIENCE — the Worker re-gates every read + status write (Invariant 2).
 */
export function FieldOpsMyTasks({
  onBack,
  onOpenForm,
  onOpenJob,
  onOpenMaterials,
  initialTab,
  onTabChange,
}: {
  onBack: () => void;
  onOpenForm?: (p: FormPrefill) => void;
  /** G2.5 — the URL-routed tab (/tasks/assigned | /tasks/daily). An EXPLICIT initial tab pins
   *  the choice (the one-time auto-switch may not yank a deep-linked tab); absent (/tasks)
   *  keeps the pre-router default + auto-switch behavior. */
  initialTab?: Tab;
  /** G2.5 — reports every tab change up (user pick AND the one-time auto-switch) so App can
   *  keep the address bar shareable. Never triggers a re-render here (App syncs the URL only). */
  onTabChange?: (t: Tab) => void;
  /** R7 — open the Job Tracker via App's navigation; a jobId preselects that job's detail
   *  (the "Log time" quick action + job-group links). Absent when the actor can't read the
   *  tracker (App gates on cap.jobtracker.read) — the affordances then don't render. */
  onOpenJob?: (jobId?: string) => void;
  /** PR2 — deep link from the Daily tab's material-receipt region into the per-job Materials
   *  page. Absent when the actor lacks cap.materials.receive — the button then doesn't render. */
  onOpenMaterials?: (jobId: string) => void;
}) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canOwn = caps.includes("cap.tasks.own");
  const canCreateCrew = caps.includes("cap.crew.create");
  const canLogTime = caps.includes("cap.time.log");
  // Directive 2026-07-03 (daily-report role gating): the Daily tab is a MANAGER/ADMIN surface —
  // a placed admin files the daily report exactly like a placed manager; a subcontractor
  // (submitter) never sees the tab, even when placed (their My Tasks stays tasks/inspections-
  // only). Role from the session (closed vocabulary) — a convenience gate; the Worker re-gates
  // every daily surface on the same role set (Invariant 2).
  const canDaily = user?.role === "manager" || user?.role === "admin";

  const [tab, setTab] = useState<Tab>(initialTab ?? "assigned");
  const [refreshToken, setRefreshToken] = useState(0);
  const [resp, setResp] = useState<api.MyTasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<ReadonlySet<number>>(new Set());
  const [rowMsgs, setRowMsgs] = useState<Record<number, RowFeedback>>({});
  // What the Daily tab last resolved (D2: the actor's PLACEMENT, not a checklist instance) —
  // drives the one-time auto-tab-switch + the Add-crew placement hint + the Log-time deep-link.
  // Reported up via DailyReportTab's onLoaded; null = not landed yet, { placement: null } = landed
  // with no placement (non-manager / unlinked / unplaced).
  const [dailyInfo, setDailyInfo] = useState<{ placement: DailyPlacement | null } | null>(null);
  // G2.5: an explicit deep-linked tab (/tasks/daily) counts as a user choice — pre-pinned.
  const autoSwitched = useRef(initialTab !== undefined);
  const lastWakeRef = useRef(0);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setResp(await api.fetchMyTasks());
    } catch (err) {
      // Never silent (Mandatory B): with no data yet → error+Retry block; with data on screen →
      // refresh warn, previous list stays up.
      setError(errMsg(err, "Failed to load your tasks."));
    } finally {
      setLoading(false);
    }
  }

  // Refresh EVERYTHING: the page's own tasks fetch + (via refreshToken) every section's fetch.
  function refreshAll() {
    setRefreshToken((t) => t + 1);
    void load();
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Day-rollover / stale-tab recovery: refetch when the tab regains visibility or focus (debounced —
  // focus + visibilitychange typically fire together).
  useEffect(() => {
    const onWake = () => {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();
      if (now - lastWakeRef.current < 1500) return;
      lastWakeRef.current = now;
      refreshAll();
    };
    window.addEventListener("focus", onWake);
    document.addEventListener("visibilitychange", onWake);
    return () => {
      window.removeEventListener("focus", onWake);
      document.removeEventListener("visibilitychange", onWake);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // One-time auto-switch (judgment call, kept simple): once BOTH first loads land, a PLACED
  // manager/admin with no open one-off tasks starts on the Daily tab (D2: placement replaces the
  // old checklist-instance signal). Never fires again (no tab yanking after the user interacts).
  // A daily-ineligible role (submitter) never mounts the Daily panel, so dailyInfo never lands
  // for it — don't wait on it (there is nothing to switch to).
  useEffect(() => {
    if (autoSwitched.current || !resp || (canDaily && !dailyInfo)) return;
    autoSwitched.current = true;
    // (An explicit user tab click also sets autoSwitched — see pickTab — so a slow first load can
    // never yank the user off a tab they chose; review WARN.)
    const hasOpenTasks = resp.tasks.some((t) => t.status !== "done");
    if (canDaily && dailyInfo?.placement && !hasOpenTasks) {
      setTab("daily");
      onTabChange?.("daily"); // G2.5: keep the address bar on the tab actually shown
    }
    // onTabChange is an every-render App closure; the autoSwitched guard makes re-runs no-ops.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resp, dailyInfo]);

  // Explicit tab choice pins the tab: the one-time auto-switch may never override it.
  function pickTab(t: "assigned" | "daily") {
    autoSwitched.current = true;
    setTab(t);
    onTabChange?.(t); // G2.5: URL sync (App replaces — tab flips never spam history)
  }

  function markBusy(id: number, on: boolean) {
    setBusyIds((s) => {
      const next = new Set(s);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function setRowMsg(id: number, msg: RowFeedback | null) {
    setRowMsgs((m) => {
      const next = { ...m };
      if (msg) next[id] = msg;
      else delete next[id];
      return next;
    });
  }

  async function changeStatus(taskId: number, status: api.TaskStatus) {
    if (busyIds.has(taskId)) return;
    markBusy(taskId, true);
    setRowMsg(taskId, null);
    // Optimistic update (the local application of the mutation); revert ONLY this row on failure —
    // a whole-snapshot revert would clobber a sibling row's concurrently-landed update (review WARN).
    const prevStatus = resp?.tasks.find((t) => t.id === taskId)?.status;
    setResp((r) => (r ? { ...r, tasks: r.tasks.map((t) => (t.id === taskId ? { ...t, status } : t)) } : r));
    try {
      await api.setTaskStatus(taskId, status);
      setRowMsg(taskId, { ok: true, text: "Updated." });
    } catch (err) {
      if (prevStatus !== undefined) {
        setResp((r) => (r ? { ...r, tasks: r.tasks.map((t) => (t.id === taskId ? { ...t, status: prevStatus } : t)) } : r));
      }
      setRowMsg(taskId, { ok: false, text: errMsg(err, "Update failed.") });
    } finally {
      markBusy(taskId, false);
    }
  }

  const groups = groupByJob(resp?.tasks ?? []);

  // Directive 2026-07-03: the Daily tab renders only for manager/admin accounts WITH a placement.
  // While /tasks/mine is in flight (resp null) the tab renders optimistically for eligible roles
  // (no pop-in for the common placed case); a LANDED response without a placement removes it —
  // an unplaced admin/manager gets no Daily tab (matrix: admin unplaced ✗). Submitters never get
  // it regardless of placement. The Worker independently 403s every daily surface for
  // ineligible roles — this is display gating only.
  const showDailyTab = canDaily && (resp === null || resp.viewer_placement !== null);
  // A deep-linked /tasks/daily (or a landed placement removal) for an account without the tab
  // coerces the DISPLAYED tab to Assigned — `tab` keeps the user's pick, the coercion is
  // presentation-only and self-heals if the placement lands.
  const effectiveTab: Tab = showDailyTab ? tab : "assigned";

  const renderTaskRow = (t: api.MyTask) => {
    const rowBusy = busyIds.has(t.id);
    return (
      <li key={t.id}>
        <span className={statusPill(t.status)}>{statusLabel(t.status)}</span>{" "}
        <span>
          {t.description}
          <span className="dash-card__sub">
            {" "}
            · {t.assigned_by ? `Assigned by ${t.assigned_by} · ` : "Assigned "}
            {fmtEpochDate(t.created_at)}
          </span>
          {/* (G2.6) deadline + the shared Overdue pill (not-done AND due < Pacific-today). */}
          <TaskDue dueDate={t.due_date} status={t.status} />
        </span>
        {canOwn && (
          <>
            {t.status === "open" && (
              <>
                {" "}
                <button
                  type="button"
                  className="btn btn--secondary"
                  aria-label={`Start task ${t.id}`}
                  disabled={rowBusy}
                  onClick={() => void changeStatus(t.id, "in_progress")}
                >
                  Start
                </button>
              </>
            )}
            {t.status !== "done" && (
              <>
                {" "}
                <button
                  type="button"
                  className="btn btn--primary"
                  aria-label={`Mark task ${t.id} done`}
                  disabled={rowBusy}
                  onClick={() => void changeStatus(t.id, "done")}
                >
                  Done
                </button>
              </>
            )}
            {t.status === "done" && (
              <>
                {" "}
                <button
                  type="button"
                  className="btn btn--secondary"
                  aria-label={`Reopen task ${t.id}`}
                  disabled={rowBusy}
                  onClick={() => void changeStatus(t.id, "open")}
                >
                  Reopen
                </button>
              </>
            )}
          </>
        )}
        {rowMsgs[t.id] ? <InlineRowMsg msg={rowMsgs[t.id]} /> : null}
      </li>
    );
  };

  const tasksBlock =
    loading && !resp ? (
      <SectionLoading label="Loading your tasks…" />
    ) : error && !resp ? (
      <SectionError message={error} onRetry={() => void load()} what="loading your tasks" />
    ) : !resp ? null : (
      <>
        {error && <SectionRefreshWarn message={error} onRetry={() => void load()} what="loading your tasks" />}
        {!resp.linked ? (
          <div className="dash-unavail">{ROSTER_LINK_COPY}</div>
        ) : groups.length === 0 ? (
          <div className="dash-unavail">
            No tasks are assigned to you. Tasks your crew lead or the office assigns to you will appear here.
          </div>
        ) : (
          <div className="dash-grid">
            {groups.map((g) => {
              const openTasks = g.tasks.filter((t) => t.status !== "done");
              const doneTasks = g.tasks.filter((t) => t.status === "done");
              return (
                <section key={g.job_id} className="card dash-section">
                  {/* R7 (the R2 deferral): App now passes onOpenJob — the group header links to the
                      job's detail in the Job Tracker. Without the prop (no cap.jobtracker.read) it
                      renders as plain text exactly as before. */}
                  <h3 className="dash-detail__h2">
                    {onOpenJob ? (
                      <button
                        type="button"
                        className="btn btn--secondary"
                        aria-label={`Open ${g.project_name ?? g.job_id} in the Job Tracker`}
                        onClick={() => onOpenJob(g.job_id)}
                      >
                        {g.project_name ?? g.job_id}
                      </button>
                    ) : (
                      g.project_name ?? g.job_id
                    )}
                    <span className="dash-card__sub"> · {g.job_id}</span>
                  </h3>
                  {openTasks.length > 0 && <ul className="dash-tasklist">{openTasks.map(renderTaskRow)}</ul>}
                  <CompletedDisclosure count={doneTasks.length}>
                    <ul className="dash-tasklist">{doneTasks.map(renderTaskRow)}</ul>
                  </CompletedDisclosure>
                </section>
              );
            })}
          </div>
        )}
      </>
    );

  return (
    <PageShell onHome={onBack}>
      <div className="dash-detail__head">
        <h2 className="page__heading">My Tasks</h2>
        <button type="button" className="btn btn--secondary" aria-label="Refresh" onClick={refreshAll}>
          Refresh
        </button>
      </div>
      <p className="dash__intro">
        Your assigned work — one-off tasks and inspections under Assigned tasks; placed crew leads also file
        their Daily report here.
      </p>

      {/* Tab strip — the admin-nav banner-extension pattern (.admin-tabs), same look site-wide.
          Rendered only when the Daily tab exists (directive 2026-07-03): a submitter — or an
          unplaced manager/admin — gets the Assigned content with no tab chrome at all. */}
      {showDailyTab && (
        <nav className="admin-tabs" role="tablist" aria-label="My Tasks sections">
          <button
            type="button"
            role="tab"
            aria-selected={effectiveTab === "assigned"}
            className={`admin-tabs__tab${effectiveTab === "assigned" ? " admin-tabs__tab--active" : ""}`}
            onClick={() => pickTab("assigned")}
          >
            Assigned tasks
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={effectiveTab === "daily"}
            className={`admin-tabs__tab${effectiveTab === "daily" ? " admin-tabs__tab--active" : ""}`}
            onClick={() => pickTab("daily")}
          >
            Daily report
          </button>
        </nav>
      )}

      {/* Both panels stay mounted (inactive hidden) — single fetch per section + the daily section
          reports its instance up for the auto-switch even while its tab is inactive. The daily
          panel mounts ONLY for manager/admin (directive 2026-07-03): a submitter's My Tasks is
          tasks/inspections-only. */}
      <div role="tabpanel" aria-label="Assigned tasks" hidden={effectiveTab !== "assigned"}>
        {/* R7 — "Log time" quick action (the subcontractor's direct path to logging hours, A3):
            deep-links to the Job Tracker detail of the actor's current placement when known (the
            Daily tab's placement resolve names it, D2); otherwise opens the tracker plainly. The
            log-time form itself lives on the job detail. */}
        {canLogTime && onOpenJob && (
          <div className="dash-row">
            <button
              type="button"
              className="btn btn--secondary"
              aria-label="Log time in the Job Tracker"
              onClick={() => onOpenJob(dailyInfo?.placement?.job_id ?? undefined)}
            >
              Log time
            </button>{" "}
            <span className="dash-card__sub muted">
              {dailyInfo?.placement
                ? `Opens ${dailyInfo.placement.project_name ?? dailyInfo.placement.job_id} to log hours.`
                : "Opens the Job Tracker — pick your job to log hours."}
            </span>
          </div>
        )}
        {tasksBlock}

        {/* S6 — admin-assigned inspection checklists (renders nothing when confirmed-empty). */}
        <AssignedInspectionsSection onOpenForm={onOpenForm} refreshToken={refreshToken} />

        {/* Slice T — a subcontractor adds field-only crew, auto-placed on their current job. */}
        {canCreateCrew && (
          <AddCrewSection
            placementJob={dailyInfo?.placement?.job_id ?? null}
            placementProject={dailyInfo?.placement?.project_name ?? null}
          />
        )}
      </div>

      {canDaily && (
        <div role="tabpanel" aria-label="Daily report" hidden={effectiveTab !== "daily"}>
          {/* D2 (SOP daily form) — the placed manager's/admin's daily SOP form rendered inline;
              the R2 reason-coded explanatory empty states carry over for everyone else. CS4 #12:
              the placement rides THIS page's /tasks/mine response (viewer_placement) — the tab no
              longer fetches a Job Tracker list page of its own; its error+Retry surface reuses
              this page's load(). */}
          <DailyReportTab
            linked={resp?.linked ?? null}
            placement={resp?.viewer_placement ?? null}
            placementError={error}
            onRetryPlacement={() => void load()}
            onOpenForm={onOpenForm}
            onOpenMaterials={onOpenMaterials}
            refreshToken={refreshToken}
            onLoaded={setDailyInfo}
          />
        </div>
      )}
    </PageShell>
  );
}
