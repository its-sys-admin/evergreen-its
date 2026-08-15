import { useCallback, useEffect, useState } from "react";
import type {
  JobProcurementPo,
  JobProcurementResponse,
  JobProcurementRfq,
  JobProcurementSub,
} from "../../worker/wire-types";
import { PageShell } from "../components/PageShell";
import { createPoChangeOrder } from "../lib/po";
import { createSubChangeOrder } from "../lib/subcontracts";

// Per-job Procurement screen (Track D + D2) — each job's own page: the lanes' documents as
// clickable rows, an item panel tracking the lifecycle (Generated → Submitted → Accepted)
// with the office's manual marks, and CHANGE ORDERS AS DOCUMENTS (operator directive
// 2026-08-14): a change order is a full lane document cloned from its parent's configuration,
// edited in the lane builder, and generated/reviewed/sent through the lane's own pipeline.
// This screen nests CO documents under their parent and offers "Create change order" on
// in-force documents — the actual drafting happens in the lane builder it opens into.
// STILL SEND-FREE: every action here RECORDS a fact or drafts paper; approvals and actual
// sends live in their own lanes (Invariant 1 / F22).
//
// Stage semantics on the panel:
//   Generated  — the document exists (draft → pending_review → approved on the machine).
//   Submitted  — machine 'sent': stamped automatically when ITS sends, or marked manually
//                here for paper that went out OUTSIDE ITS (audited; the daemon's own later
//                stamp no-ops against it).
//   Accepted   — POs: the portal-owned acceptance mark (status stays 'sent');
//                subcontracts: the machine's countersign state ('executed').
//   RFQs close instead of accepting — a finished round is retired; an accepted quote becomes
//   a PO through the estimate-import flow, not here.
//
// Totals on a CO chain are shown per document, never summed: whether the office drafts a CO
// as a delta or a restated contract is their drafting choice, so a computed "current value"
// would assert math the system can't know.

const STAGE_LABEL: Record<string, string> = {
  draft: "Draft",
  queued: "Queued",
  pending_review: "Pending review",
  approved: "Approved",
  sent: "Submitted",
  executed: "Accepted",
  superseded: "Superseded",
  canceled: "Canceled",
  generated: "Generated",
  partially_sent: "Partially sent",
  closed: "Closed",
};

function money(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function stagePill(status: string, accepted: boolean): string {
  if (accepted || status === "executed") return "dash-pill dash-pill--ok";
  if (status === "sent" || status === "approved") return "dash-pill dash-pill--ok";
  if (status === "superseded" || status === "canceled" || status === "closed") return "dash-pill";
  return "dash-pill dash-pill--warn";
}

async function postJson(path: string, body: unknown): Promise<Response> {
  return fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** Group a lane's rows for display: base documents in server (newest-first) order, each
 *  carrying its CO documents sorted by co_seq. A CO whose parent fell off the lane cap
 *  degrades to a top-level row (never hidden). */
function groupWithCos<T extends { id: number; change_order_of: number | null; co_seq: number | null }>(
  rows: T[],
): { base: T; cos: T[] }[] {
  const byParent = new Map<number, T[]>();
  for (const r of rows) {
    if (r.change_order_of !== null) {
      const list = byParent.get(r.change_order_of) ?? [];
      list.push(r);
      byParent.set(r.change_order_of, list);
    }
  }
  const out: { base: T; cos: T[] }[] = [];
  const seen = new Set<number>();
  for (const r of rows) {
    if (r.change_order_of === null) {
      const cos = (byParent.get(r.id) ?? []).sort((a, b) => (a.co_seq ?? 0) - (b.co_seq ?? 0));
      cos.forEach((c) => seen.add(c.id));
      out.push({ base: r, cos });
    }
  }
  for (const r of rows) {
    if (r.change_order_of !== null && !seen.has(r.id)) out.push({ base: r, cos: [] });
  }
  return out;
}

type Selected =
  | { kind: "po"; row: JobProcurementPo }
  | { kind: "subcontract"; row: JobProcurementSub }
  | { kind: "rfq"; row: JobProcurementRfq };

export function JobProcurementPage(props: {
  jobId: string;
  onOpenJob: (jobId: string) => void;
  /** The shell's '← Home' control — a true Home, per the sibling job-scoped pages. Falls back
   *  to the job detail so the control never dead-ends when unwired. */
  onHome?: () => void;
  onOpenPurchaseOrders?: () => void;
  onOpenSubcontracts?: () => void;
  onOpenRfqs?: () => void;
  /** Track D2: open the lane builder ON a specific draft (a change-order or other draft). */
  onOpenPoDraft?: (id: number) => void;
  onOpenSubDraft?: (id: number) => void;
}) {
  const { jobId, onOpenJob, onHome, onOpenPurchaseOrders, onOpenSubcontracts, onOpenRfqs, onOpenPoDraft, onOpenSubDraft } = props;
  const [data, setData] = useState<JobProcurementResponse | null>(null);
  const [selected, setSelected] = useState<Selected | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);

  const load = useCallback(async () => {
    // Failure here must never be silent (offline/flaky-cell is a real office condition):
    // both the network throw and a non-OK body land on the banner + the Retry affordance.
    try {
      const res = await fetch(`/api/fieldops/jobs/${encodeURIComponent(jobId)}/procurement`, {
        credentials: "same-origin",
      });
      if (!res.ok) {
        setLoadFailed(true);
        setMsg({ ok: false, text: "Could not load this job's procurement." });
        return;
      }
      setData((await res.json()) as JobProcurementResponse);
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
      setMsg({ ok: false, text: "Could not load this job's procurement — check the connection and retry." });
    }
  }, [jobId]);

  useEffect(() => {
    setSelected(null);
    setMsg(null);
    void load();
  }, [load]);

  // Re-select the refreshed row after a reload so the panel shows the new state.
  const refreshKeeping = useCallback(async (kind: Selected["kind"], id: number) => {
    await load();
    setData((d) => {
      if (d) {
        const list = kind === "po" ? d.purchase_orders : kind === "subcontract" ? d.subcontracts : d.rfqs;
        const row = (list as { id: number }[] | null)?.find((r) => r.id === id);
        if (row) setSelected({ kind, row } as Selected);
      }
      return d;
    });
  }, [load]);

  async function lifecycle(kind: Selected["kind"], id: number, action: string, confirmText?: string) {
    if (busy) return;
    // Lifecycle marks record commercial facts and most have no undo — a mis-tap must not be
    // one click. The confirm names exactly what will be recorded.
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await postJson(`/api/fieldops/procurement/${kind}/${id}/lifecycle`, { action });
      if (res.ok) {
        await refreshKeeping(kind, id);
      } else {
        const body = (await res.json().catch(() => ({}))) as { error?: string; current_status?: string };
        setMsg({
          ok: false,
          text: body.error === "wrong_state"
            ? `That doesn't fit the record — this document is currently "${STAGE_LABEL[body.current_status ?? ""] ?? body.current_status}".`
            : "Could not record that. Reload and try again.",
        });
      }
    } catch {
      setMsg({ ok: false, text: "Could not record that — check the connection and try again." });
    } finally {
      setBusy(false);
    }
  }

  // Create a CO draft from the selected in-force document, then hand off to the lane
  // builder so the office edits WHAT CHANGED and generates the new contract document.
  async function createChangeOrder(kind: "po" | "subcontract", id: number) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      if (kind === "po") {
        const r = await createPoChangeOrder(id);
        if (onOpenPoDraft) {
          onOpenPoDraft(r.id);
        } else {
          setMsg({ ok: true, text: `Change order draft created — open it in the Purchase Orders lane.` });
          await refreshKeeping(kind, id);
        }
      } else {
        const r = await createSubChangeOrder(id);
        if (onOpenSubDraft) {
          onOpenSubDraft(r.id);
        } else {
          setMsg({ ok: true, text: `Change order draft created — open it in the Subcontracts lane.` });
          await refreshKeeping(kind, id);
        }
      }
    } catch (err) {
      // ApiError.message is already the office-facing copy (errorText in its constructor) —
      // surface it rather than a generic line that hides the actual refusal.
      setMsg({
        ok: false,
        text: err instanceof Error && err.message
          ? err.message
          : "Could not create the change order draft. Reload and try again.",
      });
    } finally {
      setBusy(false);
    }
  }

  const poRows = data?.purchase_orders ?? null;
  const rfqRows = data?.rfqs ?? null;
  const subRows = data?.subcontracts ?? null;

  function renderDocButton(
    kind: "po" | "subcontract",
    row: JobProcurementPo | JobProcurementSub,
    isCo: boolean,
  ) {
    const num = kind === "po"
      ? (row as JobProcurementPo).po_number
      : (row as JobProcurementSub).sc_number;
    const fallback = isCo
      ? `Change order draft CO${row.co_seq ?? "?"}`
      : kind === "po" ? "PO (unnumbered draft)" : "Subcontract (unnumbered draft)";
    const accepted = kind === "po"
      ? (row as JobProcurementPo).accepted_at !== null
      : row.status === "executed";
    const total = kind === "po"
      ? (row as JobProcurementPo).total_cents
      : (row as JobProcurementSub).contract_price_cents;
    const party = kind === "po"
      ? (row as JobProcurementPo).vendor_name ?? "Vendor unset"
      : (row as JobProcurementSub).sub_name ?? "Subcontractor unset";
    return (
      <button type="button" className={isCo ? "proc-item proc-item--co" : "proc-item"}
              aria-label={`Open ${num ?? fallback}`}
              onClick={() => setSelected({ kind, row } as Selected)}>
        <span className={stagePill(row.status, accepted)}>
          {kind === "po" && accepted ? "Accepted" : STAGE_LABEL[row.status] ?? row.status}
        </span>{" "}
        {isCo && <span className="dash-pill">CO</span>}{" "}
        {num ?? fallback}
        <span className="dash-card__sub"> · {party} · {money(total)}</span>
      </button>
    );
  }

  const truncated = data?.truncated_lanes ?? [];

  return (
    <PageShell onHome={onHome ?? (() => onOpenJob(jobId))} wide>
      <div className="dash-back-btn">
        <button type="button" className="btn btn--secondary" onClick={() => onOpenJob(jobId)}>
          ← Back to job
        </button>
      </div>
      <h1 className="page__heading">Procurement</h1>
      <p className="dash-hint">
        Every purchase order, RFQ round and subcontract on this job, tracked through its
        lifecycle. Click a document to record where it stands — marks here are the office&apos;s
        record; approvals and sends still happen in their own lanes. Change orders are full
        documents: created from the original, edited in the lane, and sent like any contract.
      </p>
      {msg && (
        <p className={`wpr-banner ${msg.ok ? "wpr-banner--ok" : "wpr-banner--warn"}`} role="status">
          {msg.text}
        </p>
      )}
      {loadFailed && (
        <button type="button" className="btn btn--secondary" disabled={busy} onClick={() => void load()}>
          Retry loading
        </button>
      )}

      <div className="proc-shell">
        <div className="proc-lists">
          {poRows !== null && (
            <section className="card dash-section" id="pl-pos">
              <header className="job-sec__head"><h2 className="job-sec__title">Purchase orders</h2></header>
              {poRows.length === 0 && <p className="dash-hint">No purchase orders for this job yet.</p>}
              {truncated.includes("purchase_orders") && (
                <p className="dash-hint">Showing the newest {poRows.length} — older purchase orders live on the lane page.</p>
              )}
              <ul className="dash-tasklist">
                {groupWithCos(poRows).map((g) => (
                  <li key={g.base.id}>
                    {renderDocButton("po", g.base, g.base.change_order_of !== null)}
                    {g.cos.length > 0 && (
                      <ul className="dash-tasklist proc-colist">
                        {g.cos.map((co) => (
                          <li key={co.id}>{renderDocButton("po", co, true)}</li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
              {onOpenPurchaseOrders && (
                <button type="button" className="btn btn--secondary" onClick={onOpenPurchaseOrders}>
                  Open Purchase Orders lane →
                </button>
              )}
            </section>
          )}
          {rfqRows !== null && (
            <section className="card dash-section" id="pl-rfqs">
              <header className="job-sec__head"><h2 className="job-sec__title">RFQ rounds</h2></header>
              {rfqRows.length === 0 && (
                <p className="dash-hint">
                  No RFQs recorded for this job. (RFQs drafted before the job link existed do not
                  appear here — find them on the RFQs page.)
                </p>
              )}
              {truncated.includes("rfqs") && (
                <p className="dash-hint">Showing the newest {rfqRows.length} — older RFQ rounds live on the RFQs page.</p>
              )}
              <ul className="dash-tasklist">
                {rfqRows.map((r) => (
                  <li key={r.id}>
                    <button type="button" className="proc-item"
                            aria-label={`Open ${r.rfq_number ?? `RFQ draft ${r.id}`}`}
                            onClick={() => setSelected({ kind: "rfq", row: r })}>
                      <span className={stagePill(r.status, false)}>{STAGE_LABEL[r.status] ?? r.status}</span>{" "}
                      {r.rfq_number ?? "RFQ (unnumbered draft)"}
                      <span className="dash-card__sub">
                        {" "}· {r.sent_count}/{r.vendor_count} sent · {r.responded_count} responded
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
              {onOpenRfqs && (
                <button type="button" className="btn btn--secondary" onClick={onOpenRfqs}>
                  Open RFQs lane →
                </button>
              )}
            </section>
          )}
          {subRows !== null && (
            <section className="card dash-section" id="pl-subs">
              <header className="job-sec__head"><h2 className="job-sec__title">Subcontracts</h2></header>
              {subRows.length === 0 && <p className="dash-hint">No subcontracts for this job yet.</p>}
              {truncated.includes("subcontracts") && (
                <p className="dash-hint">Showing the newest {subRows.length} — older subcontracts live on the lane page.</p>
              )}
              <ul className="dash-tasklist">
                {groupWithCos(subRows).map((g) => (
                  <li key={g.base.id}>
                    {renderDocButton("subcontract", g.base, g.base.change_order_of !== null)}
                    {g.cos.length > 0 && (
                      <ul className="dash-tasklist proc-colist">
                        {g.cos.map((co) => (
                          <li key={co.id}>{renderDocButton("subcontract", co, true)}</li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
              {onOpenSubcontracts && (
                <button type="button" className="btn btn--secondary" onClick={onOpenSubcontracts}>
                  Open Subcontracts lane →
                </button>
              )}
            </section>
          )}
        </div>

        {selected && (
          <aside className="card dash-section proc-panel" aria-label="Document detail">
            <header className="job-sec__head">
              <h2 className="job-sec__title">
                {selected.kind === "po"
                  ? (selected.row as JobProcurementPo).po_number ?? "PO (unnumbered draft)"
                  : selected.kind === "subcontract"
                    ? (selected.row as JobProcurementSub).sc_number ?? "Subcontract (unnumbered draft)"
                    : (selected.row as JobProcurementRfq).rfq_number ?? "RFQ (unnumbered draft)"}
              </h2>
              <button type="button" className="btn btn--secondary" onClick={() => setSelected(null)}>
                Close
              </button>
            </header>

            {selected.kind !== "rfq" && (() => {
              const row = selected.row as JobProcurementPo | JobProcurementSub;
              const accepted = selected.kind === "po"
                ? (row as JobProcurementPo).accepted_at !== null
                : row.status === "executed";
              const submitted = accepted || row.status === "sent" || row.status === "superseded";
              const generated = !["draft", "queued"].includes(row.status);
              const isCo = row.change_order_of !== null;
              const laneRows = (selected.kind === "po" ? poRows : subRows) as
                | (JobProcurementPo | JobProcurementSub)[]
                | null;
              const cos = (laneRows ?? []).filter((r) => r.change_order_of === row.id);
              const canChangeOrder = !isCo &&
                (selected.kind === "po" ? row.status === "sent" : row.status === "sent" || row.status === "executed");
              return (
                <>
                  {isCo && (
                    <p className="dash-hint">
                      Change order CO{row.co_seq ?? "?"} — the original{" "}
                      {selected.kind === "po" ? "purchase order" : "subcontract"} stays in force;
                      this document carries the change and ships like any contract.
                    </p>
                  )}
                  <ol className="proc-timeline">
                    <li className={generated ? "is-done" : ""}>
                      Generated
                      <span className="dash-card__sub"> — {STAGE_LABEL[row.status] ?? row.status}</span>
                    </li>
                    <li className={submitted ? "is-done" : ""}>
                      Submitted to {selected.kind === "po" ? "vendor" : "subcontractor"}
                    </li>
                    <li className={accepted ? "is-done" : ""}>
                      Accepted
                      {row.accepted_at && (
                        <span className="dash-card__sub"> — {row.accepted_at} by {row.accepted_by}</span>
                      )}
                    </li>
                  </ol>
                  <div className="proc-actions">
                    {/* mark_submitted only from 'approved' (the worker's own guard) — offering
                        it on pending_review/canceled was a doomed button that always 409'd. */}
                    {row.status === "approved" && (
                      <button type="button" className="btn btn--primary" disabled={busy}
                              onClick={() => void lifecycle(selected.kind, row.id, "mark_submitted",
                                "Record this document as submitted to the " +
                                (selected.kind === "po" ? "vendor" : "subcontractor") +
                                "? This marks paper that went out outside ITS.")}>
                        Mark submitted
                      </button>
                    )}
                    {["queued", "pending_review"].includes(row.status) && (
                      <p className="dash-hint">
                        Awaiting approval — once approved, it can be sent by ITS or marked
                        submitted here.
                      </p>
                    )}
                    {submitted && !accepted && row.status === "sent" && (
                      <button type="button" className="btn btn--primary" disabled={busy}
                              onClick={() => void lifecycle(selected.kind, row.id, "mark_accepted",
                                selected.kind === "subcontract"
                                  ? "Record the subcontractor's countersign? This marks the subcontract executed and cannot be undone here."
                                  : undefined)}>
                        Mark accepted
                      </button>
                    )}
                    {selected.kind === "po" && (row as JobProcurementPo).accepted_at !== null && (
                      <button type="button" className="btn btn--secondary" disabled={busy}
                              onClick={() => void lifecycle("po", row.id, "clear_accepted")}>
                        Undo accepted
                      </button>
                    )}
                    {selected.kind === "subcontract" && row.status === "executed" && (
                      <button type="button" className="btn btn--secondary" disabled={busy}
                              onClick={() => void lifecycle("subcontract", row.id, "clear_accepted",
                                "Undo the countersign record? The subcontract returns to Submitted; if the ledger truly shows it countersigned, the sync will re-mark it.")}>
                        Undo accepted
                      </button>
                    )}
                    {canChangeOrder && (
                      <button type="button" className="btn btn--secondary" disabled={busy}
                              onClick={() => void createChangeOrder(selected.kind as "po" | "subcontract", row.id)}>
                        Create change order
                      </button>
                    )}
                    {row.status === "draft" && (selected.kind === "po" ? onOpenPoDraft : onOpenSubDraft) && (
                      <button type="button" className="btn btn--secondary" disabled={busy}
                              onClick={() => (selected.kind === "po" ? onOpenPoDraft! : onOpenSubDraft!)(row.id)}>
                        Open in the lane builder →
                      </button>
                    )}
                  </div>

                  {!isCo && (
                    <>
                      <h3 className="job-sec__title">Change orders</h3>
                      {cos.length === 0 && (
                        <p className="dash-hint">
                          No change orders against this document.
                          {canChangeOrder
                            ? " “Create change order” drafts one from this document's own configuration."
                            : ""}
                        </p>
                      )}
                      <ul className="dash-tasklist">
                        {cos
                          .slice()
                          .sort((a, b) => (a.co_seq ?? 0) - (b.co_seq ?? 0))
                          .map((co) => (
                            <li key={co.id}>{renderDocButton(selected.kind as "po" | "subcontract", co, true)}</li>
                          ))}
                      </ul>
                    </>
                  )}
                </>
              );
            })()}

            {selected.kind === "rfq" && (() => {
              const row = selected.row as JobProcurementRfq;
              return (
                <>
                  <p className="dash-hint">
                    {row.sent_count}/{row.vendor_count} vendors sent · {row.responded_count} responded
                    {row.due_date ? ` · quotes due ${row.due_date}` : ""}. An accepted quote becomes a
                    purchase order through the estimate import — closing here retires the round.
                  </p>
                  {["generated", "partially_sent", "sent"].includes(row.status) && (
                    <button type="button" className="btn btn--primary" disabled={busy}
                            onClick={() => void lifecycle("rfq", row.id, "close",
                              "Close this RFQ round? A closed round cannot be reopened from here.")}>
                      Close this RFQ round
                    </button>
                  )}
                </>
              );
            })()}
          </aside>
        )}
      </div>
    </PageShell>
  );
}
