import { useCallback, useEffect, useState } from "react";
import type {
  ChangeOrdersResponse,
  JobProcurementPo,
  JobProcurementResponse,
  JobProcurementRfq,
  JobProcurementSub,
  ProcurementChangeOrder,
} from "../../worker/wire-types";
import { PageShell } from "../components/PageShell";

// Per-job Procurement screen (Track D, operator directive 2026-08-14) — each job's own page:
// the lanes' documents as clickable rows, an item panel tracking the lifecycle
// (Generated → Submitted → Accepted) with the office's manual marks, and change orders
// recorded against the document. STILL SEND-FREE: every action here RECORDS a fact about
// paper; approvals and actual sends live in their own lanes (Invariant 1 / F22).
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

type Selected =
  | { kind: "po"; row: JobProcurementPo }
  | { kind: "subcontract"; row: JobProcurementSub }
  | { kind: "rfq"; row: JobProcurementRfq };

export function JobProcurementPage(props: {
  jobId: string;
  onOpenJob: (jobId: string) => void;
  onOpenPurchaseOrders?: () => void;
  onOpenSubcontracts?: () => void;
}) {
  const { jobId, onOpenJob, onOpenPurchaseOrders, onOpenSubcontracts } = props;
  const [data, setData] = useState<JobProcurementResponse | null>(null);
  const [selected, setSelected] = useState<Selected | null>(null);
  const [cos, setCos] = useState<ProcurementChangeOrder[] | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [coDesc, setCoDesc] = useState("");
  const [coAmount, setCoAmount] = useState("");

  const load = useCallback(async () => {
    const res = await fetch(`/api/fieldops/jobs/${encodeURIComponent(jobId)}/procurement`, {
      credentials: "same-origin",
    });
    if (!res.ok) {
      setMsg({ ok: false, text: "Could not load this job's procurement." });
      return;
    }
    setData((await res.json()) as JobProcurementResponse);
  }, [jobId]);

  useEffect(() => {
    setSelected(null);
    setMsg(null);
    void load();
  }, [load]);

  // The item panel's change orders (POs + subcontracts only).
  useEffect(() => {
    setCos(null);
    setCoDesc("");
    setCoAmount("");
    if (!selected || selected.kind === "rfq") return;
    let alive = true;
    void fetch(
      `/api/fieldops/procurement/${selected.kind}/${selected.row.id}/change-orders`,
      { credentials: "same-origin" },
    ).then(async (r) => {
      if (!alive || !r.ok) return;
      const body = (await r.json()) as ChangeOrdersResponse;
      if (alive) setCos(body.change_orders);
    });
    return () => {
      alive = false;
    };
  }, [selected]);

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

  async function lifecycle(kind: Selected["kind"], id: number, action: string) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await postJson(`/api/fieldops/procurement/${kind}/${id}/lifecycle`, { action });
      if (res.ok) {
        await refreshKeeping(kind, id);
      } else {
        const body = (await res.json()) as { error?: string; current_status?: string };
        setMsg({
          ok: false,
          text: body.error === "wrong_state"
            ? `That doesn't fit the record — this document is currently "${STAGE_LABEL[body.current_status ?? ""] ?? body.current_status}".`
            : "Could not record that. Reload and try again.",
        });
      }
    } finally {
      setBusy(false);
    }
  }

  async function addChangeOrder() {
    if (!selected || selected.kind === "rfq" || busy) return;
    const amount = Math.round(Number(coAmount) * 100);
    if (!coDesc.trim() || !Number.isFinite(amount)) {
      setMsg({ ok: false, text: "A change order needs a description and a dollar amount (negative for deductive)." });
      return;
    }
    setBusy(true);
    try {
      const res = await postJson("/api/fieldops/procurement/change-orders", {
        doc_type: selected.kind, doc_id: selected.row.id,
        description: coDesc.trim(), amount_cents: amount,
      });
      if (res.ok) {
        setCoDesc("");
        setCoAmount("");
        await refreshKeeping(selected.kind, selected.row.id);
      } else {
        setMsg({ ok: false, text: "Could not record the change order." });
      }
    } finally {
      setBusy(false);
    }
  }

  async function decideCo(id: number, status: "approved" | "rejected") {
    if (!selected || selected.kind === "rfq" || busy) return;
    setBusy(true);
    try {
      const res = await postJson(`/api/fieldops/procurement/change-orders/${id}/decide`, { status });
      if (!res.ok) setMsg({ ok: false, text: "Could not record that decision." });
      await refreshKeeping(selected.kind, selected.row.id);
    } finally {
      setBusy(false);
    }
  }

  const poRows = data?.purchase_orders ?? null;
  const rfqRows = data?.rfqs ?? null;
  const subRows = data?.subcontracts ?? null;

  return (
    <PageShell onHome={() => onOpenJob(jobId)} wide>
      <div className="dash-back-btn">
        <button type="button" className="btn btn--secondary" onClick={() => onOpenJob(jobId)}>
          ← Back to job
        </button>
      </div>
      <h1 className="page__heading">Procurement</h1>
      <p className="dash-hint">
        Every purchase order, RFQ round and subcontract on this job, tracked through its
        lifecycle. Click a document to record where it stands — marks here are the office&apos;s
        record; approvals and sends still happen in their own lanes.
      </p>
      {msg && (
        <p className={`wpr-banner ${msg.ok ? "wpr-banner--ok" : "wpr-banner--warn"}`} role="status">
          {msg.text}
        </p>
      )}

      <div className="proc-shell">
        <div className="proc-lists">
          {poRows !== null && (
            <section className="card dash-section" id="pl-pos">
              <header className="job-sec__head"><h2 className="job-sec__title">Purchase orders</h2></header>
              {poRows.length === 0 && <p className="dash-hint">No purchase orders for this job yet.</p>}
              <ul className="dash-tasklist">
                {poRows.map((p) => (
                  <li key={p.id}>
                    <button type="button" className="proc-item"
                            aria-label={`Open ${p.po_number ?? `PO draft ${p.id}`}`}
                            onClick={() => setSelected({ kind: "po", row: p })}>
                      <span className={stagePill(p.status, p.accepted_at !== null)}>
                        {p.accepted_at ? "Accepted" : STAGE_LABEL[p.status] ?? p.status}
                      </span>{" "}
                      {p.po_number ?? "PO (unnumbered draft)"}
                      <span className="dash-card__sub">
                        {" "}· {p.vendor_name ?? "Vendor unset"} · {money(p.total_cents)}
                        {p.change_order_count > 0 ? ` · ${p.change_order_count} CO` : ""}
                      </span>
                    </button>
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
            </section>
          )}
          {subRows !== null && (
            <section className="card dash-section" id="pl-subs">
              <header className="job-sec__head"><h2 className="job-sec__title">Subcontracts</h2></header>
              {subRows.length === 0 && <p className="dash-hint">No subcontracts for this job yet.</p>}
              <ul className="dash-tasklist">
                {subRows.map((sc) => (
                  <li key={sc.id}>
                    <button type="button" className="proc-item"
                            aria-label={`Open ${sc.sc_number ?? `Subcontract draft ${sc.id}`}`}
                            onClick={() => setSelected({ kind: "subcontract", row: sc })}>
                      <span className={stagePill(sc.status, sc.status === "executed")}>
                        {STAGE_LABEL[sc.status] ?? sc.status}
                      </span>{" "}
                      {sc.sc_number ?? "Subcontract (unnumbered draft)"}
                      <span className="dash-card__sub">
                        {" "}· {sc.sub_name ?? "Subcontractor unset"} · {money(sc.contract_price_cents)}
                        {sc.change_order_count > 0 ? ` · ${sc.change_order_count} CO` : ""}
                      </span>
                    </button>
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
              return (
                <>
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
                    {!submitted && generated && (
                      <button type="button" className="btn btn--primary" disabled={busy}
                              onClick={() => void lifecycle(selected.kind, row.id, "mark_submitted")}>
                        Mark submitted
                      </button>
                    )}
                    {submitted && !accepted && row.status === "sent" && (
                      <button type="button" className="btn btn--primary" disabled={busy}
                              onClick={() => void lifecycle(selected.kind, row.id, "mark_accepted")}>
                        Mark accepted
                      </button>
                    )}
                    {selected.kind === "po" && (row as JobProcurementPo).accepted_at !== null && (
                      <button type="button" className="btn btn--secondary" disabled={busy}
                              onClick={() => void lifecycle("po", row.id, "clear_accepted")}>
                        Undo accepted
                      </button>
                    )}
                  </div>

                  <h3 className="job-sec__title">Change orders</h3>
                  {cos !== null && cos.length === 0 && (
                    <p className="dash-hint">No change orders recorded against this document.</p>
                  )}
                  <ul className="dash-tasklist">
                    {(cos ?? []).map((co) => (
                      <li key={co.id}>
                        <span className={co.status === "approved" ? "dash-pill dash-pill--ok" : co.status === "rejected" ? "dash-pill" : "dash-pill dash-pill--warn"}>
                          {co.status}
                        </span>{" "}
                        CO #{co.seq} · {money(co.amount_cents)}
                        <span className="dash-card__sub"> — {co.description}</span>
                        {co.status === "pending" && (
                          <>
                            {" "}
                            <button type="button" className="btn btn--secondary" disabled={busy}
                                    aria-label={`Approve change order ${co.seq}`}
                                    onClick={() => void decideCo(co.id, "approved")}>
                              Approve
                            </button>{" "}
                            <button type="button" className="btn btn--secondary" disabled={busy}
                                    aria-label={`Reject change order ${co.seq}`}
                                    onClick={() => void decideCo(co.id, "rejected")}>
                              Reject
                            </button>
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                  <div className="proc-co-form">
                    <input className="wpr-field__input" placeholder="Change order description"
                           aria-label="Change order description"
                           value={coDesc} onChange={(e) => setCoDesc(e.target.value)} />
                    <input className="wpr-num" inputMode="decimal" placeholder="$ (− deductive)"
                           aria-label="Change order amount in dollars"
                           value={coAmount} onChange={(e) => setCoAmount(e.target.value)} />
                    <button type="button" className="btn btn--primary" disabled={busy}
                            onClick={() => void addChangeOrder()}>
                      Add change order
                    </button>
                  </div>
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
                            onClick={() => void lifecycle("rfq", row.id, "close")}>
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
