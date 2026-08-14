import { useEffect, useState } from "react";
import type { JobProcurementResponse } from "../../worker/wire-types";

// Per-job Procurement section for the job detail (Track A8) — READ-ONLY by doctrine: the POs,
// RFQs and Subcontracts generated for this job with their lifecycle state. No send / approve /
// advance affordance exists here (Invariant 1 — approval lives on the workspace share lists,
// F22 Mac-side); the deep links open the lanes' own pages. Statuses at approved-or-beyond are a
// CACHE of the Mac/Smartsheet-side state (the lanes' status-sync passes) — the hint says so.
//
// Self-contained fetch (the payments-summary pattern): the lane data rides its own cap-gated
// route, never the job-detail response, so commercial figures reach only sessions that could
// open the lane pages themselves.

const STAGE_LABEL: Record<string, string> = {
  draft: "Draft",
  queued: "Queued",
  pending_review: "Pending review",
  approved: "Approved",
  sent: "Sent",
  executed: "Executed",
  superseded: "Superseded",
  canceled: "Canceled",
  generated: "Generated",
  partially_sent: "Partially sent",
};

function stagePill(status: string): string {
  if (status === "sent" || status === "executed" || status === "approved") return "dash-pill dash-pill--ok";
  if (status === "superseded" || status === "canceled") return "dash-pill";
  return "dash-pill dash-pill--warn";
}

function money(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

export function JobProcurementSection(props: {
  jobId: string;
  onOpenPurchaseOrders?: () => void;
  onOpenSubcontracts?: () => void;
  /** Track D: the per-job lifecycle screen — where documents are clicked into and marked. */
  onOpenProcurement?: () => void;
}) {
  const { jobId, onOpenPurchaseOrders, onOpenSubcontracts, onOpenProcurement } = props;
  const [data, setData] = useState<JobProcurementResponse | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let alive = true;
    setData(null);
    setErr(false);
    void fetch(`/api/fieldops/jobs/${encodeURIComponent(jobId)}/procurement`, { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error(String(r.status));
        const d = (await r.json()) as JobProcurementResponse;
        if (alive) setData(d);
      })
      .catch(() => {
        if (alive) setErr(true);
      });
    return () => {
      alive = false;
    };
  }, [jobId]);

  return (
    <section className="card dash-section" id="jd-procurement">
      <header className="job-sec__head">
        <h2 className="job-sec__title">Procurement</h2>
        {onOpenProcurement && (
          <button type="button" className="btn btn--secondary" onClick={onOpenProcurement}>
            Track lifecycle →
          </button>
        )}
      </header>
      <p className="dash-hint">
        This job&apos;s purchase orders, RFQ rounds and subcontracts, with where each sits in its
        lifecycle — as of the last sync from the review sheets. Approvals and sends happen in
        their own lanes, never here.
      </p>
      {err && <p className="dash-hint">Could not load the procurement summary — reload to retry.</p>}
      {data && (
        <>
          {data.purchase_orders !== null && (
            <>
              <h3 className="job-sec__title">Purchase orders</h3>
              {data.purchase_orders.length === 0 ? (
                <p className="dash-hint">No purchase orders for this job yet.</p>
              ) : (
                <ul className="dash-tasklist">
                  {data.purchase_orders.map((p) => (
                    <li key={p.id}>
                      <span className={stagePill(p.status)}>{STAGE_LABEL[p.status] ?? p.status}</span>{" "}
                      <span>
                        {p.po_number ?? "PO (unnumbered draft)"}
                        {p.revision > 0 ? ` rev ${p.revision}` : ""}
                        <span className="dash-card__sub">
                          {" "}· {p.vendor_name ?? "Vendor unset"} · {money(p.total_cents)}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <h3 className="job-sec__title">RFQ rounds</h3>
              {data.rfqs !== null && data.rfqs.length === 0 ? (
                <p className="dash-hint">
                  No RFQs recorded for this job. (RFQs drafted before the job link existed do not
                  appear here — find them on the RFQs page.)
                </p>
              ) : (
                <ul className="dash-tasklist">
                  {(data.rfqs ?? []).map((r) => (
                    <li key={r.id}>
                      <span className={stagePill(r.status)}>{STAGE_LABEL[r.status] ?? r.status}</span>{" "}
                      <span>
                        {r.rfq_number ?? "RFQ (unnumbered draft)"}
                        <span className="dash-card__sub">
                          {" "}· {r.sent_count}/{r.vendor_count} sent · {r.responded_count} responded
                          {r.due_date ? ` · quotes due ${r.due_date}` : ""}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {onOpenPurchaseOrders && (
                <button type="button" className="btn btn--secondary" onClick={onOpenPurchaseOrders}>
                  Open Purchase Orders →
                </button>
              )}
            </>
          )}
          {data.subcontracts !== null && (
            <>
              <h3 className="job-sec__title">Subcontracts</h3>
              {data.subcontracts.length === 0 ? (
                <p className="dash-hint">No subcontracts for this job yet.</p>
              ) : (
                <ul className="dash-tasklist">
                  {data.subcontracts.map((s) => (
                    <li key={s.id}>
                      <span className={stagePill(s.status)}>{STAGE_LABEL[s.status] ?? s.status}</span>{" "}
                      <span>
                        {s.sc_number ?? "Subcontract (unnumbered draft)"}
                        {s.revision > 0 ? ` rev ${s.revision}` : ""}
                        <span className="dash-card__sub">
                          {" "}· {s.sub_name ?? "Subcontractor unset"}
                          {s.trade ? ` · ${s.trade}` : ""} · {money(s.contract_price_cents)}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {onOpenSubcontracts && (
                <button type="button" className="btn btn--secondary" onClick={onOpenSubcontracts}>
                  Open Subcontracts →
                </button>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
