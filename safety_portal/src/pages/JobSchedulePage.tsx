import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../lib/fieldops_schedules";
import type { ScheduleListRow, ScheduleTaskRow } from "../lib/fieldops_schedules";
import { ScheduleValidatePage } from "./ScheduleValidatePage";
import { useAuth } from "../lib/auth";
import { errorText } from "../lib/errorCopy";
import { PageShell } from "../components/PageShell";
import { ConfirmDelete } from "../components/ChecklistItemForm";

// Per-job SCHEDULE page (ADR-0006 PR-4) — /jobs/:jobId/schedule, the deep-link target from
// the Job Tracker's "Open schedule →" card.
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
//     the IN-ROW mark-off controls (operator decision 8): quick-% chips + an exact-% input
//     on ordinary tasks, a done-mark on milestones, a delivered-date control on Deliveries
//     tasks. Optimistic row update, then a reload — the server's row is the record.
// The Worker re-gates every call; capability checks here drive affordances only
// (Invariant 2 — SPA gating is convenience, never the boundary).
//
// Revision reconcile arrives in PR-6.

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

/** A text progress bar — deliberately characters, not CSS: it survives every theme, prints,
 *  and reads out loud sanely. 10 cells, floor-rounded so 99% still shows one open cell. */
function progressBar(percent: number): string {
  const filled = Math.max(0, Math.min(10, Math.floor(percent / 10)));
  return `${"█".repeat(filled)}${"░".repeat(10 - filled)} ${percent}%`;
}

function errText(e: unknown, fallback: string): string {
  if (e && typeof e === "object" && "code" in e) {
    const code = (e as { code: string | null }).code;
    if (code) return errorText(code);
  }
  return fallback;
}

/** Today as the Pacific calendar date (YYYY-MM-DD, en-CA yields ISO order) — the crews' day,
 *  matching the Worker's own default for an omitted delivered_date. */
function todayPacific(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

/** The quick-% chips (operator decision 8). */
const PERCENT_CHIPS = [0, 25, 50, 75, 100] as const;

export function JobSchedulePage({
  jobId,
  onHome,
  onOpenJob,
}: {
  jobId: string;
  onHome: () => void;
  onOpenJob: (jobId: string) => void;
}) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.jobtracker.manage");
  const canMark = caps.includes("cap.schedule.mark");

  const [tasks, setTasks] = useState<ScheduleTaskRow[] | null>(null);
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
  const [markBusy, setMarkBusy] = useState<number | null>(null);
  const [exactPct, setExactPct] = useState<Record<number, string>>({});
  const [deliveredDraft, setDeliveredDraft] = useState<Record<number, string>>({});

  const loadTasks = useCallback(() => {
    setLoadError(null);
    api
      .fetchScheduleTasks(jobId)
      .then((d) => {
        setTasks(d.tasks);
        setProjectName(d.project_name);
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

  // Group by section in DOCUMENT order (sort_order), preserving first-seen section order —
  // a schedule is read top to bottom, so alphabetizing sections would scramble the phases.
  const groups = useMemo(() => {
    const list = tasks ?? [];
    const out: { name: string | null; tasks: ScheduleTaskRow[] }[] = [];
    const byName = new Map<string, ScheduleTaskRow[]>();
    for (const t of list) {
      const key = t.section ?? "";
      let bucket = byName.get(key);
      if (!bucket) {
        bucket = [];
        byName.set(key, bucket);
        out.push({ name: t.section, tasks: bucket });
      }
      bucket.push(t);
    }
    return out;
  }, [tasks]);

  /** Optimistically patch one task row, run the mark call, then reload — the reload
   *  confirms the server's row on success and honestly reverts the optimism on failure. */
  async function runMark(
    taskId: number,
    patch: Partial<ScheduleTaskRow>,
    callFn: () => Promise<unknown>,
    failText: string,
  ) {
    if (markBusy !== null) return;
    setMarkBusy(taskId);
    setMsg(null);
    setTasks((prev) => (prev ? prev.map((t) => (t.id === taskId ? { ...t, ...patch } : t)) : prev));
    try {
      await callFn();
    } catch (e) {
      setMsg({ ok: false, text: errText(e, failText) });
    } finally {
      setMarkBusy(null);
      loadTasks();
    }
  }

  function markPercent(t: ScheduleTaskRow, percent: number) {
    return runMark(
      t.id,
      { percent_done: percent },
      () => api.markScheduleTaskProgress(t.id, percent),
      "Could not save that progress mark.",
    );
  }

  function markExact(t: ScheduleTaskRow) {
    const raw = (exactPct[t.id] ?? "").trim();
    const val = Number(raw);
    // Mirror the Worker's bound locally so a typo fails instantly, not after a round trip.
    if (!raw.length || !Number.isInteger(val) || val < 0 || val > 100) {
      setMsg({ ok: false, text: "Progress must be a whole number from 0 to 100." });
      return;
    }
    setExactPct((prev) => ({ ...prev, [t.id]: "" }));
    void markPercent(t, val);
  }

  function markMilestone(t: ScheduleTaskRow, done: boolean) {
    if (done) {
      return runMark(
        t.id,
        { percent_done: 100 },
        () => api.markScheduleTaskMilestoneDone(t.id),
        "Could not save that done mark.",
      );
    }
    // Un-checking is a correction — a milestone is binary, so "not done" is 0%.
    return markPercent(t, 0);
  }

  function markDelivered(t: ScheduleTaskRow) {
    const date = deliveredDraft[t.id] ?? t.delivered_date ?? todayPacific();
    return runMark(
      t.id,
      { delivered_date: date },
      () => api.markScheduleTaskDelivered(t.id, date),
      "Could not save the delivered mark.",
    );
  }

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

  // ── The validate SUB-FACE (remount-keyed; not a router entry) ──────────────────────
  if (openSchedule !== null) {
    return (
      <PageShell onHome={onHome}>
        <ScheduleValidatePage
          key={openSchedule}
          scheduleId={openSchedule}
          onClose={(notice, committed) => {
            setOpenSchedule(null);
            if (notice) setMsg(notice);
            loadSchedules();
            // A commit authors the living task list, so the list behind this screen is
            // stale the moment we finish.
            if (committed) loadTasks();
          }}
        />
      </PageShell>
    );
  }

  const hasUploads = (schedules?.length ?? 0) > 0;
  const empty = tasks !== null && tasks.length === 0;

  return (
    <PageShell onHome={onHome}>
      <div className="dash-back-btn">
        <button type="button" className="btn btn--secondary" onClick={() => onOpenJob(jobId)}>
          ← Back to job
        </button>
      </div>
      {/* The job NAME, not the JOB-###### key (the Materials-page precedent) — the key is a
          system identifier the field never speaks; the id falls back only while loading. */}
      <h1 className="page__heading">Schedule — {projectName ?? jobId}</h1>
      <p className="dash__intro">
        The job&apos;s living task list — what the project schedule says is happening, section by
        section.
        {canMark
          ? " Tap a percent to mark progress; milestones get a done-mark, deliveries a delivered date."
          : ""}
      </p>

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

      {tasks === null && !loadError && <p className="dash__intro">Loading…</p>}

      {empty && (
        <p className="dash__intro">
          {canManage
            ? hasUploads
              ? "No tasks yet — check a finished upload below and import it."
              : "No schedule yet. Upload the project-schedule PDF export below to start the task list."
            : "No schedule has been imported for this job yet."}
        </p>
      )}

      {canManage ? (
        <section className="card dash-section" aria-label="Schedule uploads">
          <h3 className="dash-detail__h2">Import a schedule</h3>
          <p className="dash__intro">
            Upload the project schedule as a PDF (the Smartsheet export). It is read on the
            office Mac and comes back as a task grid you check side-by-side with the source
            pages — nothing reaches the task list until you import it.
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
              <table className="dash-table">
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
                    const discardable =
                      s.status !== "committed" && s.status !== "superseded";
                    return (
                      <tr key={s.id}>
                        <td>{s.filename}</td>
                        <td>
                          <span className={chip.className}>{chip.label}</span>
                        </td>
                        <td>{s.row_count ?? "—"}</td>
                        <td>
                          {validatable ? (
                            <button
                              type="button"
                              className="btn btn--secondary"
                              aria-label={`Validate ${s.filename}`}
                              onClick={() => setOpenSchedule(s.id)}
                            >
                              Validate →
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
        </section>
      ) : null}

      {groups.map((group) => (
        <section
          key={group.name ?? "__none"}
          className="card dash-section"
          aria-label={group.name ?? "Tasks"}
        >
          {group.name && <h3 className="dash-detail__h2">{group.name}</h3>}
          <div style={{ overflowX: "auto" }}>
            <table className="dash-table">
              <thead>
                <tr>
                  <th scope="col">Task</th>
                  <th scope="col">Start</th>
                  <th scope="col">Finish</th>
                  <th scope="col">Duration</th>
                  <th scope="col">Progress</th>
                </tr>
              </thead>
              <tbody>
                {group.tasks.map((t) => (
                  <tr key={t.id}>
                    <td>
                      {t.name}
                      {t.is_contract_milestone ? (
                        <>
                          {" "}
                          <span className="dash-pill dash-pill--warn">Contract milestone</span>
                        </>
                      ) : t.is_milestone ? (
                        <>
                          {" "}
                          <span className="dash-pill">Milestone</span>
                        </>
                      ) : null}
                      {t.is_delivery ? (
                        <>
                          {" "}
                          <span className="dash-pill">Delivery</span>
                        </>
                      ) : null}
                      {t.delivered_date ? (
                        <>
                          {" "}
                          <span className="dash-pill dash-pill--ok">
                            Delivered {t.delivered_date}
                            {t.delivered_by_name ? ` · ${t.delivered_by_name}` : ""}
                          </span>
                        </>
                      ) : null}
                      {canMark && t.is_delivery ? (
                        <span style={{ whiteSpace: "nowrap" }}>
                          {" "}
                          <input
                            type="date"
                            aria-label={`Delivered date for ${t.name}`}
                            value={deliveredDraft[t.id] ?? t.delivered_date ?? todayPacific()}
                            disabled={markBusy !== null}
                            onChange={(e) =>
                              setDeliveredDraft((prev) => ({ ...prev, [t.id]: e.target.value }))
                            }
                          />{" "}
                          <button
                            type="button"
                            className="btn btn--secondary btn--sm"
                            aria-label={`Mark ${t.name} delivered`}
                            disabled={markBusy !== null}
                            onClick={() => void markDelivered(t)}
                          >
                            {t.delivered_date ? "Update date" : "Delivered"}
                          </button>
                        </span>
                      ) : null}
                    </td>
                    <td>{t.start_date ?? "—"}</td>
                    <td>{t.finish_date ?? "—"}</td>
                    <td>{t.duration_days != null ? `${t.duration_days}d` : "—"}</td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <span style={{ fontFamily: "monospace" }}>{progressBar(t.percent_done)}</span>
                      {canMark && !t.is_milestone ? (
                        <div className="dash-row" style={{ marginTop: "0.25rem" }}>
                          {PERCENT_CHIPS.map((p) => (
                            <button
                              key={p}
                              type="button"
                              className="btn btn--secondary btn--sm"
                              aria-label={`Mark ${t.name} ${p}%`}
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
                            aria-label={`Exact percent for ${t.name}`}
                            style={{ width: "4.2rem" }}
                            value={exactPct[t.id] ?? ""}
                            disabled={markBusy !== null}
                            onChange={(e) =>
                              setExactPct((prev) => ({ ...prev, [t.id]: e.target.value }))
                            }
                          />
                          <button
                            type="button"
                            className="btn btn--secondary btn--sm"
                            aria-label={`Set exact percent for ${t.name}`}
                            disabled={markBusy !== null || !(exactPct[t.id] ?? "").trim().length}
                            onClick={() => markExact(t)}
                          >
                            Set
                          </button>
                        </div>
                      ) : null}
                      {canMark && t.is_milestone ? (
                        <label style={{ marginLeft: "0.5rem", whiteSpace: "nowrap" }}>
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
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </PageShell>
  );
}
