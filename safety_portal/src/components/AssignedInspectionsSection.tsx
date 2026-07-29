import { useEffect, useState } from "react";
import * as checklist from "../lib/fieldops_checklist";
import { ChecklistItemRow } from "./ChecklistItemRow";
import { SignaturePad } from "./SignaturePad";
import { useAuth } from "../lib/auth";
import { statusLabel } from "../lib/labels";
import { resolveFormTarget } from "../forms/registry";
import type { FormPrefill } from "../pages/FormFillPage";
import {
  InlineRowMsg,
  SectionError,
  SectionLoading,
  SectionRefreshWarn,
  errMsg,
  fmtDate,
  pacificToday,
  type RowFeedback,
} from "./myTasksShared";

/**
 * S6 — "Assigned inspections" for ANYONE with an assigned inspection (manager OR subcontractor;
 * extracted from FieldOpsMyTasks in R2 — lives on the Assigned-tasks tab). Fetches
 * GET /checklist/assigned.
 *
 * R8 — DRILL-IN: the assignee no longer sees every inspection's items dumped flat. Each assigned
 * inspection is a clickable CARD (title + who/where/due + status + a progress bar); clicking one
 * opens a FOCUSED view of just that inspection's items (the SAME per-item completion controls,
 * ChecklistItemRow — Mark done / Record / open a linked form), with a "← Back" and a "Done" that
 * returns to the list. Completion stays per-item + immediate (the item-state routes are unchanged);
 * an inspection auto-reaches "complete" server-side when its last item is done. The R2 never-silent
 * load/error/empty states and the mutation/refetch try-split are preserved verbatim.
 */
export function AssignedInspectionsSection({
  onOpenForm,
  refreshToken = 0,
}: {
  onOpenForm?: (p: FormPrefill) => void;
  /** Bump to refetch (page Refresh / focus / visibilitychange — the parent owns the trigger). */
  refreshToken?: number;
}) {
  const [resp, setResp] = useState<checklist.AssignedInspectionsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<ReadonlySet<number>>(new Set());
  const [rowMsgs, setRowMsgs] = useState<Record<number, RowFeedback>>({});
  const [softWarn, setSoftWarn] = useState<string | null>(null);
  // R8 — which inspection is opened for completion (null = the card list).
  const [openId, setOpenId] = useState<number | null>(null);
  // #17 (Seam A) — the feature is DARK unless the server var is on (display hint only; the emit
  // route is the real gate). When dark, the "Sign & log to progress report" action never renders.
  const { user } = useAuth();
  const progressLoggingLive = !!user?.checklist_progress_logging_enabled;
  // The sign-off panel state for the OPENED inspection (one open at a time in the drill-in).
  const [signing, setSigning] = useState(false);
  const [sig, setSig] = useState("");
  const [progressBusy, setProgressBusy] = useState(false);
  const [progressErr, setProgressErr] = useState<string | null>(null);

  // Reset the sign-off panel whenever the opened inspection changes (open a card / Back / Done).
  useEffect(() => {
    setSigning(false);
    setSig("");
    setProgressErr(null);
  }, [openId]);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setResp(await checklist.fetchAssignedInspections());
      setSoftWarn(null);
    } catch (err) {
      // Never silent (Mandatory B): previously a fetch error rendered NOTHING (silent-swallow site 2).
      setError(errMsg(err, "Could not load your assigned inspections."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshToken]);

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

  /** Apply a CompleteResult locally: the item's row + ONLY its containing inspection's status. */
  function applyResult(
    r: checklist.AssignedInspectionsResponse | null,
    res: checklist.CompleteResult,
  ): checklist.AssignedInspectionsResponse | null {
    if (!r) return r;
    return {
      ...r,
      inspections: r.inspections.map((insp) => {
        if (!insp.items.some((it) => it.id === res.id)) return insp;
        return {
          instance: { ...insp.instance, status: res.instance_status },
          items: insp.items.map((it) =>
            it.id === res.id
              ? { ...it, status: res.status, value_num: res.value_num !== undefined ? res.value_num : it.value_num }
              : it,
          ),
        };
      }),
    };
  }

  /** Mutation/refetch try-split — the R2 Mandatory-B contract (the mutation and the follow-up
   *  refetch fail independently; a landed write is never reported as failed). */
  async function runItemAction(
    item: checklist.ChecklistItemState,
    call: () => Promise<checklist.CompleteResult>,
    okText: (res: checklist.CompleteResult) => string,
  ) {
    if (busyIds.has(item.id)) return;
    markBusy(item.id, true);
    setRowMsg(item.id, null);
    let res: checklist.CompleteResult;
    try {
      res = await call();
    } catch (err) {
      // R1: user copy comes from errorCopy.ts via err.message — never duplicated in pages.
      setRowMsg(item.id, { ok: false, text: errMsg(err, "Update failed.") });
      markBusy(item.id, false);
      return;
    }
    setResp((r) => applyResult(r, res));
    setRowMsg(item.id, { ok: true, text: okText(res) });
    try {
      setResp(await checklist.fetchAssignedInspections());
      setSoftWarn(null);
    } catch {
      setSoftWarn("Saved — but the list couldn't refresh; what you see may be slightly stale.");
    } finally {
      markBusy(item.id, false);
    }
  }

  /** Mark ONE inspection progress_logged locally (mirror of applyResult): the "Sign & log" action
   *  hides + the "Logged" pill shows without waiting for a refetch. */
  function markLogged(
    r: checklist.AssignedInspectionsResponse | null,
    instanceId: number,
  ): checklist.AssignedInspectionsResponse | null {
    if (!r) return r;
    return {
      ...r,
      inspections: r.inspections.map((insp) =>
        insp.instance.id === instanceId
          ? { ...insp, instance: { ...insp.instance, progress_logged: true } }
          : insp,
      ),
    };
  }

  // #17 (Seam A) — sign off the opened COMPLETE inspection and log it to the weekly progress report.
  async function logProgress(instanceId: number) {
    if (progressBusy || !sig) return;
    setProgressBusy(true);
    setProgressErr(null);
    try {
      await checklist.submitChecklistCompletion(instanceId, sig);
    } catch (err) {
      // Never silent — err.message is the human copy from errorCopy.ts (e.g. already-logged, dark).
      setProgressErr(errMsg(err, "Could not log to the progress report."));
      setProgressBusy(false);
      return;
    }
    setResp((r) => markLogged(r, instanceId));
    setSigning(false);
    setSig("");
    setProgressBusy(false);
  }

  // photoRef threaded through (R3 3-arg contract) — a note-edit never NULLs photo evidence.
  function complete(item: checklist.ChecklistItemState, note?: string, photoRef?: string) {
    void runItemAction(
      item,
      () => checklist.completeChecklistItem(item.id, note || photoRef ? { note, photo_ref: photoRef } : undefined),
      (res) => (res.instance_status === "complete" ? "Inspection complete." : "Item updated."),
    );
  }

  function uncomplete(item: checklist.ChecklistItemState) {
    void runItemAction(
      item,
      () => checklist.uncompleteChecklistItem(item.id),
      () => "Item updated.",
    );
  }

  function recordCount(item: checklist.ChecklistItemState, value: number) {
    if (!Number.isFinite(value)) {
      setRowMsg(item.id, { ok: false, text: "Enter a number." });
      return;
    }
    void runItemAction(
      item,
      () => checklist.recordCountItem(item.id, value),
      (res) => (res.instance_status === "complete" ? "Inspection complete." : "Count recorded."),
    );
  }

  // A form_linked/inspection item in an assignment only auto-closes when the instance carries a
  // concrete (job, date) — otherwise there's no submission to match. Build the deep-link from those;
  // the row disables the button when canOpenForm is false.
  function openLinkedForm(inst: checklist.AssignedInstance, item: checklist.ChecklistItemState) {
    if (!onOpenForm || !item.form_code || !inst.job_id || !inst.instance_date) return;
    const { parentCode, variantCode } = resolveFormTarget(item.form_code);
    onOpenForm({ jobId: inst.job_id, parentCode, variantCode: variantCode || undefined, workDate: inst.instance_date });
  }

  const renderItem = (insp: checklist.AssignedInspection) => (it: checklist.ChecklistItemState) => (
    <li key={it.id}>
      <ChecklistItemRow
        item={it}
        busy={busyIds.has(it.id)}
        canOpenForm={!!onOpenForm && !!insp.instance.job_id && !!insp.instance.instance_date}
        onComplete={complete}
        onUncomplete={uncomplete}
        onRecordCount={recordCount}
        onCountRecorded={() => void load()}
        onPhotoUploaded={() => void load()}
        onOpenForm={(item) => openLinkedForm(insp.instance, item)}
      />
      {it.filed_by ? <span className="dash-card__sub"> · filed by {it.filed_by}</span> : null}
      {rowMsgs[it.id] ? <InlineRowMsg msg={rowMsgs[it.id]} /> : null}
    </li>
  );

  // ── Render states ───────────────────────────────────────────────────────────────────────────────
  if (loading && !resp) return <SectionLoading label="Loading assigned inspections…" />;
  if (error && !resp) {
    return <SectionError message={error} onRetry={() => void load()} what="loading assigned inspections" />;
  }
  if (!resp) return null;
  // Confirmed-empty (no error): invisible for users with no assignments. The tasks list on this tab
  // explains linked:false, so no duplicate roster-link copy here.
  if (resp.inspections.length === 0 && !error) return null;

  const today = pacificToday();
  const openInsp = openId !== null ? resp.inspections.find((i) => i.instance.id === openId) ?? null : null;

  function statusPill(status: string) {
    return (
      <span className={status === "complete" ? "dash-pill dash-pill--ok" : "dash-pill dash-pill--warn"}>
        {statusLabel(status)}
      </span>
    );
  }

  return (
    <section className="card dash-section" aria-label="Assigned inspections">
      <h3 className="dash-detail__h2">Assigned inspections</h3>
      {error && <SectionRefreshWarn message={error} onRetry={() => void load()} what="loading assigned inspections" />}
      {softWarn && <SectionRefreshWarn message={softWarn} onRetry={() => void load()} what="refreshing assigned inspections" />}

      {openInsp === null ? (
        // ── The CARD LIST — click a card to open its items ──────────────────────────────────────
        <>
          <p className="dash-card__sub muted">
            Tap an inspection to open its items, complete them, then tap Done.
          </p>
          <ul className="dash-grid checklist-tasklist" aria-label="Assigned inspection list">
            {resp.inspections.map((insp) => {
              const total = insp.items.length;
              const done = insp.items.filter((it) => it.status === "done").length;
              const overdue =
                insp.instance.status === "open" && !!insp.instance.instance_date && insp.instance.instance_date < today;
              const title = insp.instance.template_title ?? "Inspection";
              const where = insp.instance.project_name ?? insp.instance.job_id;
              return (
                <li key={insp.instance.id}>
                  <button
                    type="button"
                    className="card dash-card--click checklist-task-card"
                    aria-label={`Open ${title} inspection`}
                    onClick={() => {
                      setOpenId(insp.instance.id);
                      setRowMsgs({});
                    }}
                  >
                    <div className="checklist-task-card__head">
                      <h4 className="dash-card__title">{title}</h4>
                      {overdue && <span className="dash-pill dash-pill--warn">Overdue</span>}
                      {statusPill(insp.instance.status)}
                      {progressLoggingLive && insp.instance.progress_logged && (
                        <span className="dash-pill dash-pill--ok">Logged ✓</span>
                      )}
                    </div>
                    <div className="dash-card__sub">
                      #{insp.instance.id}
                      {where ? <> · {where}</> : null}
                      {insp.instance.instance_date ? <> · due {fmtDate(insp.instance.instance_date)}</> : null}
                    </div>
                    <div className="checklist-task-card__progress">
                      <span className="dash-progress" aria-hidden="true">
                        <span className="dash-progress__fill" style={{ width: total ? `${(done / total) * 100}%` : "0%" }} />
                      </span>
                      <span className="dash-card__sub">
                        {done}/{total} item{total === 1 ? "" : "s"} done
                      </span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        // ── ONE opened inspection — its items + completion + Done ────────────────────────────────
        <div className="checklist-task-open">
          <button
            type="button"
            className="btn btn--secondary checklist-task-open__back"
            aria-label="Back to my inspections"
            onClick={() => setOpenId(null)}
          >
            ← Back to my inspections
          </button>
          {(() => {
            const insp = openInsp;
            const overdue =
              insp.instance.status === "open" && !!insp.instance.instance_date && insp.instance.instance_date < today;
            const doneItems = insp.items.filter((it) => it.status === "done");
            const where = insp.instance.project_name ?? insp.instance.job_id;
            const ri = renderItem(insp);
            return (
              <>
                <h4 className="dash-detail__h2">
                  {insp.instance.template_title ?? "Inspection"}
                  <span className="dash-card__sub"> · #{insp.instance.id}</span>
                  {where ? <span className="dash-card__sub"> · {where}</span> : null}
                  {insp.instance.instance_date ? (
                    <span className="dash-card__sub"> · due {fmtDate(insp.instance.instance_date)}</span>
                  ) : null}{" "}
                  {overdue && <span className="dash-pill dash-pill--warn">Overdue</span>} {statusPill(insp.instance.status)}
                </h4>
                {insp.items.length === 0 ? (
                  <div className="muted">No items on this inspection.</div>
                ) : (
                  <>
                    <div className="checklist-open__progress">
                      <span className="dash-progress" aria-hidden="true">
                        <span
                          className="dash-progress__fill"
                          style={{ width: `${(doneItems.length / insp.items.length) * 100}%` }}
                        />
                      </span>
                      <span className="dash-card__sub">
                        {doneItems.length}/{insp.items.length} done
                      </span>
                    </div>
                    {/* Every item inline in seq order — a done item keeps its Undo visible (toggle to
                        uncheck), so nothing hides behind a disclosure while the person works the list. */}
                    <ul className="dash-tasklist checklist-open__items">{insp.items.map(ri)}</ul>
                    {doneItems.length === insp.items.length ? (
                      <p className="banner banner--ok">All items are done — tap Done to finish.</p>
                    ) : null}
                  </>
                )}
                {/* #17 (Seam A) — sign off a COMPLETE inspection to the weekly progress report.
                    DARK unless the server flag is live; once logged, the action is replaced by a pill. */}
                {progressLoggingLive &&
                  (insp.instance.progress_logged ? (
                    <p className="banner banner--ok">Logged to progress report ✓</p>
                  ) : insp.instance.status === "complete" ? (
                    <div className="checklist-progress-log">
                      {!signing ? (
                        <button
                          type="button"
                          className="btn btn--primary"
                          aria-label="Sign and log this inspection to the progress report"
                          onClick={() => {
                            setSigning(true);
                            setSig("");
                            setProgressErr(null);
                          }}
                        >
                          Sign &amp; log to progress report
                        </button>
                      ) : (
                        <div className="checklist-progress-log__panel">
                          <p className="dash-card__sub">
                            Sign to log this completed inspection to the weekly progress report.
                          </p>
                          {/* CONTROLLED — `sig` is what the Log button gates on, so the
                              preview and the gate can never disagree. */}
                          <SignaturePad value={sig} onChange={(svg, empty) => setSig(empty ? "" : svg)} />
                          {progressErr ? <InlineRowMsg msg={{ ok: false, text: progressErr }} /> : null}
                          <div className="checklist-progress-log__actions">
                            <button
                              type="button"
                              className="btn btn--secondary"
                              onClick={() => {
                                setSigning(false);
                                setSig("");
                                setProgressErr(null);
                              }}
                              disabled={progressBusy}
                            >
                              Cancel
                            </button>
                            <button
                              type="button"
                              className="btn btn--primary"
                              aria-label="Log to progress report"
                              onClick={() => void logProgress(insp.instance.id)}
                              disabled={progressBusy || !sig}
                            >
                              {progressBusy ? "Logging…" : "Log to progress report"}
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  ) : null)}
                <div className="checklist-task-open__foot">
                  <button type="button" className="btn btn--primary" onClick={() => setOpenId(null)}>
                    Done
                  </button>
                </div>
              </>
            );
          })()}
        </div>
      )}
    </section>
  );
}
