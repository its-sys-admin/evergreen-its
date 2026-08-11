import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../lib/fieldops_jobtracker";
import type { JobArchiveStatus, JobDetail } from "../lib/fieldops_jobtracker";
import { ConfirmTypedModal } from "./ConfirmTypedModal";

/**
 * The Archive / Un-archive control (ROADMAP Track 6).
 *
 * Pressing Archive does NOT move anything — it records intent. The Mac-side pass relocates the
 * job's seven containers (four Smartsheet folders, three Box) on its next cycle and reports back,
 * which is why this polls `job.archive.state` instead of treating the 200 as "done". Claiming a job
 * is archived the instant a flag flipped is precisely the lie the old dropdown told.
 *
 * Sited in its own danger-zone section at the BOTTOM of the manage area, deliberately away from the
 * routine active/inactive selector — those are day-to-day; this is a cross-system relocation.
 */

const IN_FLIGHT = new Set(["requested", "in_progress"]);
/** Poll fast while the daemon is working, slow when idle. Recursive setTimeout, not setInterval —
 *  the house pattern (PublishMonitor / PoConfigPage / FormFillPage): an interval can stack requests
 *  when one is slow, a self-rescheduling timeout cannot. */
const POLL_ACTIVE_MS = 4000;
/** Stop polling after this long regardless — a stuck relocation is an ITS_Errors question, not
 *  something to spin the browser on forever. */
const POLL_CEILING_MS = 10 * 60 * 1000;

const CONTAINER_COUNT = 7;

function label(direction: string): string {
  return direction === "unarchive" ? "Un-archiving" : "Archiving";
}

export function JobArchivePanel({
  job,
  canArchive,
  onChanged,
}: {
  job: JobDetail;
  canArchive: boolean;
  /** Ask the parent to re-fetch the detail (the archive block lives on it). */
  onChanged: () => void | Promise<void>;
}) {
  const [modal, setModal] = useState<null | "archive" | "unarchive">(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startedAt = useRef<number>(0);

  const archive: JobArchiveStatus | null = job.archive;
  const state = archive?.state ?? "none";
  const direction = archive?.direction ?? "";
  const inFlight = IN_FLIGHT.has(state);

  // Poll only while the daemon owns the row. Cleared on terminal state, job switch, or unmount.
  useEffect(() => {
    if (!inFlight) {
      if (timer.current) clearTimeout(timer.current);
      startedAt.current = 0;
      return;
    }
    if (!startedAt.current) startedAt.current = Date.now();
    let live = true;
    const tick = () => {
      if (!live) return;
      if (Date.now() - startedAt.current > POLL_CEILING_MS) return; // give up quietly; banner says so
      void Promise.resolve(onChanged()).finally(() => {
        if (live) timer.current = setTimeout(tick, POLL_ACTIVE_MS);
      });
    };
    timer.current = setTimeout(tick, POLL_ACTIVE_MS);
    return () => {
      live = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [inFlight, job.job_id, onChanged]);

  const run = useCallback(
    async (kind: "archive" | "unarchive") => {
      setBusy(true);
      setErr(null);
      try {
        if (kind === "archive") await api.archiveJob(job.job_id, job.project_name);
        else await api.unarchiveJob(job.job_id, job.project_name);
        setModal(null);
        await onChanged();
      } catch (e) {
        // errorCopy gives err.message human copy; never surface a raw "Request failed (409)".
        setErr(e instanceof Error ? e.message : "That didn't work — please try again.");
      } finally {
        setBusy(false);
      }
    },
    [job.job_id, job.project_name, onChanged],
  );

  // null = the viewer lacks cap.job.archive (the worker withholds the block entirely).
  if (!canArchive || !archive) return null;

  const moved = archive.containers.filter((c) => c.moved).length;
  const stuck = archive.containers.filter((c) => !c.moved);
  const total = archive.containers.length || CONTAINER_COUNT;

  const whatMoves = (
    <>
      <p>
        This <strong>moves</strong> the job&apos;s folders into the <strong>ITS — Archive</strong>{" "}
        area, filed under <code>{job.project_name}</code>:
      </p>
      <ul className="modal__list">
        <li>Safety folder <span className="muted">(Smartsheet)</span></li>
        <li>Progress folder <span className="muted">(Smartsheet)</span></li>
        <li>Purchase Orders folder <span className="muted">(Smartsheet)</span></li>
        <li>Subcontracts folder <span className="muted">(Smartsheet)</span></li>
        <li>Safety Reports folder <span className="muted">(Box)</span></li>
        <li>Progress Reports folder <span className="muted">(Box)</span></li>
      </ul>
      <p>
        <strong>Nothing is deleted, and these do not move:</strong> the PO, RFQ, Estimate and
        Subcontract logs · the WSR and WPR review rows · the job&apos;s row in{" "}
        <code>ITS_Active_Jobs</code> (it stays, marked Archived) · the customer&apos;s{" "}
        <code>ITS DATA</code> project folder · <code>ITS Photos</code> · every record in the portal.
      </p>
      <p className="muted">
        The move runs on the office Mac and usually finishes within a few minutes. You can reverse
        it with Un-archive.
      </p>
    </>
  );

  return (
    <section className="card dash-section" aria-label="Archive this job">
      <h3 className="dash-card__title">Archive</h3>

      {err && <div className="banner banner--err" role="alert">{err}</div>}

      {state === "none" && (
        <>
          <p className="dash-card__sub muted">
            Moves this job&apos;s seven folders into the ITS — Archive area. Reversible.
          </p>
          <div className="dash-row">
            <button type="button" className="btn btn--retire" onClick={() => setModal("archive")}>
              Archive job…
            </button>
          </div>
        </>
      )}

      {inFlight && (
        <div className="banner banner--ok" role="status">
          <strong>{label(direction)}…</strong>{" "}
          {archive.containers.length > 0
            ? `${moved} of ${total} folders moved.`
            : "Waiting for the office Mac to pick this up."}
          <div className="muted">This page updates itself — no need to refresh.</div>
        </div>
      )}

      {state === "complete" && direction === "archive" && (
        <>
          <div className="banner banner--ok" role="status">
            <strong>Archived.</strong> All {total} folders are filed under{" "}
            <code>ITS — Archive / {job.project_name}</code>.
          </div>
          <div className="dash-row">
            <button type="button" className="btn btn--secondary" onClick={() => setModal("unarchive")}>
              Un-archive…
            </button>
          </div>
        </>
      )}

      {(state === "partial" || state === "failed") && (
        <>
          <div className="banner banner--err" role="alert">
            <strong>
              {state === "partial"
                ? `${direction === "unarchive" ? "Partly un-archived" : "Partly archived"} — ` +
                  `${moved} of ${total} folders moved.`
                : "Nothing moved."}
            </strong>
            {stuck.length > 0 && (
              <>
                <div>These did not move yet:</div>
                <ul className="modal__list">
                  {stuck.map((c) => (
                    <li key={c.key}>
                      {c.label}
                      {c.note ? <span className="muted"> — {c.note}</span> : null}
                    </li>
                  ))}
                </ul>
              </>
            )}
            <div className="muted">
              <strong>This will not retry on its own</strong> — press “Try again” to resume it.
              The office Mac only picks up archives that are still in progress, so a stopped one
              waits for you. If it keeps failing, check <code>ITS_Errors</code> and see the
              project-closure runbook — do not drag the folders by hand.
            </div>
          </div>
          <div className="dash-row">
            {/* RESUME THE DIRECTION THAT STALLED. This used to hard-code "archive", so pressing
                "Try again" on a half-finished UN-archive offered to archive — moving the
                containers that had just come back into the archive again, the exact opposite of
                resuming. It is also the ONLY retry control rendered on a partial, so getting the
                direction wrong left a stranded job with no correct move. (issue #54) */}
            <button
              type="button"
              className="btn btn--retire"
              onClick={() => setModal(direction === "unarchive" ? "unarchive" : "archive")}
            >
              Try again
            </button>
          </div>
        </>
      )}

      <ConfirmTypedModal
        open={modal === "archive"}
        title={`Archive "${job.project_name}" (${job.job_id})?`}
        expected={job.project_name}
        body={whatMoves}
        confirmLabel="Archive job"
        busy={busy}
        onConfirm={() => void run("archive")}
        onCancel={() => setModal(null)}
      />
      <ConfirmTypedModal
        open={modal === "unarchive"}
        title={`Un-archive "${job.project_name}" (${job.job_id})?`}
        expected={job.project_name}
        confirmLabel="Un-archive job"
        confirmClass="btn btn--primary"
        busy={busy}
        onConfirm={() => void run("unarchive")}
        onCancel={() => setModal(null)}
        body={
          <>
            <p>
              This moves the job&apos;s folders back out of <strong>ITS — Archive</strong> to where
              they were.
            </p>
            <p>
              The job comes back <strong>Inactive</strong>. If work has restarted, set it Active
              separately — bringing folders back is not the same as re-opening a job.
            </p>
          </>
        }
      />
    </section>
  );
}
