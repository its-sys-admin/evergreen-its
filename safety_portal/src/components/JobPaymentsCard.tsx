import { useEffect, useState } from "react";
import { fetchPaymentsSummary, type PaymentsSummaryResponse } from "../lib/fieldops_payments";

// The job detail's payments CARD (Track A7) — a cap.payments.manage-only reduction of the
// payments read, self-contained fetch (the ADR-0006 decision-7 posture: payment data rides
// ONLY payments-family routes; the jobs detail response carries zero payment fields and this
// component mounts only when the capability is held). Read-only — the PaymentsSection on the
// schedule page is where cycles/receipts are managed; this card is the tracker's signpost.

const STATE_COPY: Record<string, string> = {
  overdue: "overdue",
  nonpayment_notice_due: "nonpayment notice due",
  nonpayment_notice_sent: "nonpayment notice sent",
  suspension_notice_due: "suspension notice due",
  suspension_notice_sent: "suspension notice sent",
};

function money(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

export function JobPaymentsCard(props: { jobId: string; onOpenSchedule?: () => void }) {
  const { jobId, onOpenSchedule } = props;
  const [data, setData] = useState<PaymentsSummaryResponse | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let alive = true;
    setData(null);
    setErr(false);
    void fetchPaymentsSummary(jobId).then(
      (d) => { if (alive) setData(d); },
      () => { if (alive) setErr(true); },
    );
    return () => { alive = false; };
  }, [jobId]);

  return (
    <section className="card dash-section job-link" id="jd-payments">
      <header className="job-sec__head">
        <h2 className="job-sec__title">Payments</h2>
        {data && data.overdue_count > 0 && (
          <span className="dash-pill dash-pill--warn">
            {data.overdue_count} overdue{data.worst_state && STATE_COPY[data.worst_state] ? ` — ${STATE_COPY[data.worst_state]}` : ""}
          </span>
        )}
      </header>
      {err && <p className="dash-hint">Could not load the payments summary — reload to retry.</p>}
      {data && (
        <p className="dash-hint">
          {!data.has_terms
            ? "No payment terms are set for this job yet — set them on the schedule page's payments drawer."
            : data.overdue_count > 0
              ? "A cycle needs attention — open the payments drawer to record receipts or issue the notice."
              : data.next_due
                ? `Next due: ${data.next_due.label} · ${data.next_due.due_date}` +
                  (data.next_due.balance_cents !== null ? ` · ${money(data.next_due.balance_cents)} open` : "")
                : `${data.cycle_count} cycle(s) — nothing due or overdue right now.`}
        </p>
      )}
      {onOpenSchedule && (
        <button type="button" className="btn btn--secondary" onClick={onOpenSchedule}>
          Open payments →
        </button>
      )}
    </section>
  );
}
