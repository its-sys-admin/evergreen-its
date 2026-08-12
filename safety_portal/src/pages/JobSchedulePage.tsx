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
// The Worker re-gates every call; capability checks here drive affordances only
// (Invariant 2 — SPA gating is convenience, never the boundary).
//
// Progress mark-off chips arrive in PR-5 (cap.schedule.mark); revision reconcile in PR-6.

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

  const [tasks, setTasks] = useState<ScheduleTaskRow[] | null>(null);
  const [projectName, setProjectName] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Import surface state (canManage only). Own single-flight, separate from the task read.
  const [schedules, setSchedules] = useState<ScheduleListRow[] | null>(null);
  const [openSchedule, setOpenSchedule] = useState<number | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);

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
        section. Progress mark-off from the field arrives in a later update.
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
                    </td>
                    <td>{t.start_date ?? "—"}</td>
                    <td>{t.finish_date ?? "—"}</td>
                    <td>{t.duration_days != null ? `${t.duration_days}d` : "—"}</td>
                    <td style={{ fontFamily: "monospace", whiteSpace: "nowrap" }}>
                      {progressBar(t.percent_done)}
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
