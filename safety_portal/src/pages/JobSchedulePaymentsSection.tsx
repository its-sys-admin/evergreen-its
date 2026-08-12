import { useCallback, useEffect, useState } from "react";
import * as api from "../lib/fieldops_payments";
import type {
  PaymentCycleView,
  PaymentState,
  PaymentTermsDefaultResponse,
  PaymentTermsRow,
} from "../lib/fieldops_payments";
import { formatCents, parseDollarsToCents } from "../lib/po";
import { errorText } from "../lib/errorCopy";
import { ConfirmDelete } from "../components/ChecklistItemForm";

// Payments section of the per-job Schedule page (ADR-0006 decision 10, PR-7) — SAME PAGE
// as the schedule (operator decision: office works job money where they work the job),
// rendered by JobSchedulePage ONLY when the session holds cap.payments.manage (admin —
// operator decision 4; the Worker re-gates every call, so the SPA check is an
// affordance, never the boundary — Invariant 2).
//
// DISPLAY-ONLY (operator decision 3): every state chip is DERIVED server-side at read
// (payments_derive.ts) — this section records what happened (invoice out, money in, a
// notice a human sent OUTSIDE ITS) and shows where each cycle stands. Nothing here
// sends, reminds, or generates a document; the "Record notice" buttons stamp a date,
// and the confirm copy says exactly that.

/** Derived state → chip copy + tone. The *_due states are the section's whole point —
 *  the office's next required action, so they read as instructions, not jargon. */
function stateChip(c: PaymentCycleView): { label: string; className: string } {
  const chips: Record<PaymentState, { label: string; className: string }> = {
    draft: { label: "Draft — not invoiced", className: "dash-pill" },
    awaiting: { label: "Awaiting payment", className: "dash-pill" },
    due_soon: {
      label: c.days_until_due !== null ? `Due in ${c.days_until_due}d` : "Due soon",
      className: "dash-pill dash-pill--warn",
    },
    overdue: {
      label: c.days_overdue !== null ? `Overdue ${c.days_overdue}d` : "Overdue",
      className: "dash-pill dash-pill--warn",
    },
    nonpayment_notice_due: { label: "Nonpayment notice DUE", className: "dash-pill dash-pill--danger" },
    nonpayment_notice_sent: { label: "Nonpayment notice sent", className: "dash-pill dash-pill--warn" },
    suspension_notice_due: { label: "Intent to suspend DUE", className: "dash-pill dash-pill--danger" },
    suspension_notice_sent: { label: "Intent to suspend sent", className: "dash-pill dash-pill--danger" },
    paid: { label: c.paid_late ? "Paid (late)" : "Paid", className: "dash-pill dash-pill--ok" },
  };
  return chips[c.state];
}

function errText(e: unknown, fallback: string): string {
  if (e && typeof e === "object" && "code" in e) {
    const code = (e as { code: string | null }).code;
    if (code) return errorText(code);
  }
  return fallback;
}

const CADENCES: { value: PaymentTermsRow["billing_cadence"]; label: string }[] = [
  { value: "monthly", label: "Monthly" },
  { value: "semimonthly", label: "Semimonthly" },
  { value: "milestone", label: "By milestone" },
  { value: "other", label: "Other" },
];

type TermsDraft = {
  billing_cadence: PaymentTermsRow["billing_cadence"];
  billing_cadence_detail: string;
  net_days: string;
  nonpayment_notice_days: string;
  intent_to_suspend_days: string;
  notes: string;
};

const EMPTY_TERMS: TermsDraft = {
  billing_cadence: "monthly", billing_cadence_detail: "",
  net_days: "30", nonpayment_notice_days: "10", intent_to_suspend_days: "10", notes: "",
};

function termsToDraft(t: Omit<PaymentTermsRow, "job_id">): TermsDraft {
  return {
    billing_cadence: t.billing_cadence,
    billing_cadence_detail: t.billing_cadence_detail ?? "",
    net_days: String(t.net_days),
    nonpayment_notice_days: String(t.nonpayment_notice_days),
    intent_to_suspend_days: String(t.intent_to_suspend_days),
    notes: t.notes ?? "",
  };
}

export function PaymentsSection({ jobId }: { jobId: string }) {
  const [data, setData] = useState<api.PaymentsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // The same-client prefill offer (operator decision 6) — fetched once, only shown
  // while the job has no terms of its own.
  const [prefill, setPrefill] = useState<PaymentTermsDefaultResponse | null>(null);

  const [editingTerms, setEditingTerms] = useState(false);
  const [terms, setTerms] = useState<TermsDraft>(EMPTY_TERMS);

  // Draft-cycle add form.
  const [newCycle, setNewCycle] = useState({ label: "", submitted: "", amount: "" });
  // Per-cycle inline receipt drafts (amount dollars text + optional date).
  const [receiptDraft, setReceiptDraft] = useState<Record<number, { amount: string; date: string }>>({});
  // Per-cycle submitted-date pick for marking a DRAFT cycle invoiced (the edit route —
  // the Worker computes + stores the due-date snapshot from the terms of that moment).
  const [submittedDraft, setSubmittedDraft] = useState<Record<number, string>>({});

  const load = useCallback(() => {
    setLoadError(null);
    api
      .fetchPayments(jobId)
      .then((d) => setData(d))
      .catch(() => setLoadError("Could not load this job's payments."));
  }, [jobId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api
      .fetchTermsDefault(jobId)
      .then(setPrefill)
      .catch(() => setPrefill(null)); // no prefill is a shrug, never an error banner
  }, [jobId]);

  /** One in-flight mutation at a time (the schedule page's busy posture); reload after —
   *  the derived states live server-side, so the reload IS the truth. */
  async function run(fn: () => Promise<unknown>, failText: string): Promise<boolean> {
    if (busy) return false;
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      return true;
    } catch (e) {
      setMsg({ ok: false, text: errText(e, failText) });
      return false;
    } finally {
      setBusy(false);
      load();
    }
  }

  function saveTermsForm() {
    const net = Number(terms.net_days);
    const notice = Number(terms.nonpayment_notice_days);
    const suspend = Number(terms.intent_to_suspend_days);
    // Mirror the Worker's bounds locally so a typo fails instantly, not after a round trip.
    if (!Number.isInteger(net) || net < 0 || net > 365) {
      setMsg({ ok: false, text: errorText("invalid_net_days") });
      return;
    }
    if (!Number.isInteger(notice) || notice < 1 || notice > 365 || !Number.isInteger(suspend) || suspend < 1 || suspend > 365) {
      setMsg({ ok: false, text: errorText("invalid_notice_days") });
      return;
    }
    void run(
      () =>
        api.saveTerms(jobId, {
          billing_cadence: terms.billing_cadence,
          billing_cadence_detail: terms.billing_cadence_detail.trim() || null,
          net_days: net,
          nonpayment_notice_days: notice,
          intent_to_suspend_days: suspend,
          notes: terms.notes.trim() || null,
        }),
      "Could not save the payment terms.",
    ).then((ok) => {
      if (ok) setEditingTerms(false);
    });
  }

  function addCycle() {
    const label = newCycle.label.trim();
    if (!label) {
      setMsg({ ok: false, text: errorText("invalid_cycle_label") });
      return;
    }
    let amount: number | null = null;
    if (newCycle.amount.trim()) {
      amount = parseDollarsToCents(newCycle.amount);
      if (amount === null) {
        setMsg({ ok: false, text: errorText("invalid_amount_cents") });
        return;
      }
    }
    void run(
      () =>
        api.createCycle(jobId, {
          label,
          invoice_submitted_date: newCycle.submitted || null,
          invoice_amount_cents: amount,
        }),
      "Could not add that payment cycle.",
    ).then((ok) => {
      if (ok) setNewCycle({ label: "", submitted: "", amount: "" });
    });
  }

  function addReceipt(c: PaymentCycleView) {
    const d = receiptDraft[c.id] ?? { amount: "", date: "" };
    const cents = parseDollarsToCents(d.amount);
    if (cents === null || cents <= 0) {
      setMsg({ ok: false, text: errorText("invalid_amount_cents") });
      return;
    }
    void run(
      () => api.addReceipt(c.id, { amount_cents: cents, ...(d.date ? { received_date: d.date } : {}) }),
      "Could not record that payment.",
    ).then((ok) => {
      if (ok) setReceiptDraft((prev) => ({ ...prev, [c.id]: { amount: "", date: "" } }));
    });
  }

  const t = data?.terms ?? null;
  const cycles = data?.cycles ?? [];
  const showPrefill = t === null && prefill?.terms != null;

  return (
    /* Collapsible on purpose: the schedule is the page's job-site face; money opens on
       demand. <details> keeps it dependency-free and keyboard/screen-reader native.
       Wears .sched-drawer so it matches the import and materials drawers beside it —
       three office surfaces, one control. */
    <details className="sched-drawer" aria-label="Payments">
      <summary>
        Payments
        <span className="sched-drawer__note">Invoice cycles against the contract terms</span>
      </summary>
      <div className="sched-drawer__body">
      <p className="dash__intro">
        Where each invoice cycle stands against the contract&apos;s payment terms. Status is
        computed from the recorded dates — notices are recorded here after you send them,
        never sent by the system.
      </p>

      {msg && (
        <p className={msg.ok ? "dash__msg" : "login__error"} role={msg.ok ? "status" : "alert"}>
          {msg.text}
        </p>
      )}
      {loadError && (
        <p className="login__error" role="alert">
          {loadError}{" "}
          <button type="button" className="btn btn--secondary" onClick={load}>
            Retry
          </button>
        </p>
      )}
      {data === null && !loadError && <p className="dash__intro">Loading…</p>}

      {data !== null && (
        <>
          {/* ── Terms ─────────────────────────────────────────────────────────── */}
          <h4 className="dash-detail__h2">Payment terms</h4>
          {t !== null && !editingTerms ? (
            <p className="dash__intro">
              {CADENCES.find((c) => c.value === t.billing_cadence)?.label ?? t.billing_cadence}
              {t.billing_cadence_detail ? ` (${t.billing_cadence_detail})` : ""} · Net {t.net_days} ·
              nonpayment notice at {t.nonpayment_notice_days}d overdue · intent to suspend{" "}
              {t.intent_to_suspend_days}d after notice
              {t.notes ? ` — ${t.notes}` : ""}{" "}
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                onClick={() => {
                  setTerms(termsToDraft(t));
                  setEditingTerms(true);
                }}
              >
                Edit terms
              </button>
            </p>
          ) : null}
          {t === null && !editingTerms ? (
            <p className="dash__intro">
              No payment terms recorded for this job yet.{" "}
              {showPrefill ? (
                <button
                  type="button"
                  className="btn btn--secondary btn--sm"
                  onClick={() => {
                    // Prefill COPIES values into the editor — the human still reviews and
                    // saves; nothing links the two jobs' terms (decision 6).
                    setTerms(termsToDraft(prefill!.terms!));
                    setEditingTerms(true);
                  }}
                >
                  Copy from {prefill?.client_name ?? "the client"}&apos;s latest job
                </button>
              ) : null}{" "}
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                onClick={() => {
                  setTerms(EMPTY_TERMS);
                  setEditingTerms(true);
                }}
              >
                Set terms
              </button>
            </p>
          ) : null}
          {editingTerms ? (
            <div className="dash-row" style={{ flexWrap: "wrap", gap: "0.5rem" }}>
              <label>
                Cadence{" "}
                <select
                  aria-label="Billing cadence"
                  value={terms.billing_cadence}
                  onChange={(e) =>
                    setTerms((p) => ({
                      ...p,
                      billing_cadence: e.target.value as PaymentTermsRow["billing_cadence"],
                    }))
                  }
                >
                  {CADENCES.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Detail{" "}
                <input
                  type="text"
                  aria-label="Cadence detail"
                  value={terms.billing_cadence_detail}
                  onChange={(e) => setTerms((p) => ({ ...p, billing_cadence_detail: e.target.value }))}
                />
              </label>
              <label>
                Net days{" "}
                <input
                  type="number" min={0} max={365} style={{ width: "4.5rem" }}
                  aria-label="Net days"
                  value={terms.net_days}
                  onChange={(e) => setTerms((p) => ({ ...p, net_days: e.target.value }))}
                />
              </label>
              <label>
                Nonpayment notice after (days overdue){" "}
                <input
                  type="number" min={1} max={365} style={{ width: "4.5rem" }}
                  aria-label="Nonpayment notice days"
                  value={terms.nonpayment_notice_days}
                  onChange={(e) => setTerms((p) => ({ ...p, nonpayment_notice_days: e.target.value }))}
                />
              </label>
              <label>
                Intent to suspend after (days past notice){" "}
                <input
                  type="number" min={1} max={365} style={{ width: "4.5rem" }}
                  aria-label="Intent to suspend days"
                  value={terms.intent_to_suspend_days}
                  onChange={(e) => setTerms((p) => ({ ...p, intent_to_suspend_days: e.target.value }))}
                />
              </label>
              <label>
                Notes{" "}
                <input
                  type="text"
                  aria-label="Terms notes"
                  value={terms.notes}
                  onChange={(e) => setTerms((p) => ({ ...p, notes: e.target.value }))}
                />
              </label>
              <button type="button" className="btn" disabled={busy} aria-label="Save terms" onClick={saveTermsForm}>
                Save terms
              </button>
              <button
                type="button"
                className="btn btn--secondary"
                disabled={busy}
                onClick={() => setEditingTerms(false)}
              >
                Cancel
              </button>
            </div>
          ) : null}

          {/* ── Cycles ────────────────────────────────────────────────────────── */}
          <h4 className="dash-detail__h2">Payment cycles</h4>
          {cycles.length === 0 ? (
            <p className="dash__intro">No payment cycles yet — add the first one below.</p>
          ) : (
            /* --stack (the kit's phone treatment) drops the header row below 640px and
               re-flows each cycle into a labelled block, so an eight-column money table
               stops being 6 window-widths of sideways scroll on a phone. Each cell carries
               its own `data-cell` label for that mode. */
            <div style={{ overflowX: "auto" }}>
              <table className="dash-table dash-table--stack">
                <thead>
                  <tr>
                    <th scope="col">Cycle</th>
                    <th scope="col">Submitted</th>
                    <th scope="col">Amount</th>
                    <th scope="col">Due</th>
                    <th scope="col">Status</th>
                    <th scope="col">Notices</th>
                    <th scope="col">Payments</th>
                    <th scope="col" />
                  </tr>
                </thead>
                <tbody>
                  {cycles.map((c) => {
                    const chip = stateChip(c);
                    const received = c.receipts.reduce((s, r) => s + r.amount_cents, 0);
                    const partial = received > 0 && c.state !== "paid";
                    const rd = receiptDraft[c.id] ?? { amount: "", date: "" };
                    return (
                      <tr key={c.id}>
                        <td data-cell="Cycle">
                          {c.label}
                          {c.note ? <div className="dash__intro">{c.note}</div> : null}
                        </td>
                        <td data-cell="Submitted" style={{ whiteSpace: "nowrap" }}>
                          {c.invoice_submitted_date ?? "—"}
                          {c.invoice_submitted_date === null ? (
                            <div className="dash-row">
                              <input
                                type="date"
                                aria-label={`Submitted date for ${c.label}`}
                                value={submittedDraft[c.id] ?? ""}
                                disabled={busy}
                                onChange={(e) =>
                                  setSubmittedDraft((p) => ({ ...p, [c.id]: e.target.value }))
                                }
                              />
                              <button
                                type="button"
                                className="btn btn--secondary btn--sm"
                                aria-label={`Mark ${c.label} submitted`}
                                disabled={busy || !(submittedDraft[c.id] ?? "")}
                                onClick={() =>
                                  void run(
                                    // The edit route recomputes the due-date snapshot because
                                    // the submitted date — its OWN input — is what changes here.
                                    () =>
                                      api.editCycle(c.id, {
                                        label: c.label,
                                        invoice_submitted_date: submittedDraft[c.id],
                                        invoice_amount_cents: c.invoice_amount_cents,
                                        note: c.note,
                                      }),
                                    "Could not mark that cycle submitted.",
                                  )
                                }
                              >
                                Mark submitted
                              </button>
                            </div>
                          ) : null}
                        </td>
                        <td data-cell="Amount">{c.invoice_amount_cents !== null ? formatCents(c.invoice_amount_cents) : "—"}</td>
                        <td data-cell="Due">{c.due_date ?? "—"}</td>
                        <td data-cell="Status" style={{ whiteSpace: "nowrap" }}>
                          <span className={chip.className}>{chip.label}</span>
                          {partial && c.balance_cents !== null ? (
                            <div className="dash__intro">{formatCents(c.balance_cents)} outstanding</div>
                          ) : null}
                        </td>
                        <td data-cell="Notices" style={{ whiteSpace: "nowrap" }}>
                          {c.nonpayment_notice_date ? (
                            <div>Nonpayment: {c.nonpayment_notice_date}</div>
                          ) : (
                            <ConfirmDelete
                              actionLabel="Record nonpayment notice"
                              ariaLabel={`Record nonpayment notice for ${c.label}`}
                              copy="Record that the Notice of Nonpayment was sent today? This only records the date — nothing is sent."
                              busy={busy}
                              onConfirm={() =>
                                void run(
                                  () => api.recordNotice(c.id, "nonpayment"),
                                  "Could not record the notice.",
                                )
                              }
                            />
                          )}
                          {c.suspend_notice_date ? (
                            <div>Suspend: {c.suspend_notice_date}</div>
                          ) : c.nonpayment_notice_date ? (
                            <ConfirmDelete
                              actionLabel="Record intent to suspend"
                              ariaLabel={`Record intent to suspend for ${c.label}`}
                              copy="Record that the Intent to Suspend was sent today? This only records the date — nothing is sent."
                              busy={busy}
                              onConfirm={() =>
                                void run(
                                  () => api.recordNotice(c.id, "suspend"),
                                  "Could not record the notice.",
                                )
                              }
                            />
                          ) : null}
                        </td>
                        <td data-cell="Payments" style={{ whiteSpace: "nowrap" }}>
                          {c.receipts.map((r) => (
                            <div key={r.id}>
                              {formatCents(r.amount_cents)} on {r.received_date}{" "}
                              <ConfirmDelete
                                actionLabel="Undo"
                                ariaLabel={`Remove receipt of ${formatCents(r.amount_cents)} on ${r.received_date}`}
                                copy="Remove this payment record? Add the corrected one after."
                                busy={busy}
                                onConfirm={() =>
                                  void run(
                                    () => api.deactivateReceipt(r.id),
                                    "Could not remove that payment record.",
                                  )
                                }
                              />
                            </div>
                          ))}
                          {c.invoice_submitted_date !== null ? (
                            <div className="dash-row">
                              <input
                                type="text" inputMode="decimal" placeholder="$"
                                style={{ width: "6rem" }}
                                aria-label={`Payment amount for ${c.label}`}
                                value={rd.amount}
                                disabled={busy}
                                onChange={(e) =>
                                  setReceiptDraft((p) => ({ ...p, [c.id]: { ...rd, amount: e.target.value } }))
                                }
                              />
                              <input
                                type="date"
                                aria-label={`Payment date for ${c.label}`}
                                value={rd.date}
                                disabled={busy}
                                onChange={(e) =>
                                  setReceiptDraft((p) => ({ ...p, [c.id]: { ...rd, date: e.target.value } }))
                                }
                              />
                              <button
                                type="button"
                                className="btn btn--secondary btn--sm"
                                aria-label={`Record payment for ${c.label}`}
                                disabled={busy || !rd.amount.trim()}
                                onClick={() => addReceipt(c)}
                              >
                                Record payment
                              </button>
                            </div>
                          ) : null}
                        </td>
                        <td>
                          <ConfirmDelete
                            actionLabel="Remove"
                            ariaLabel={`Remove cycle ${c.label}`}
                            copy="Remove this payment cycle from the list? Its history is kept."
                            busy={busy}
                            onConfirm={() =>
                              void run(() => api.deactivateCycle(c.id), "Could not remove that cycle.")
                            }
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* ── Add a cycle ───────────────────────────────────────────────────── */}
          <div className="dash-row" style={{ flexWrap: "wrap", gap: "0.5rem" }}>
            <input
              type="text"
              placeholder="Cycle label (e.g. PP #3)"
              aria-label="New cycle label"
              value={newCycle.label}
              disabled={busy}
              onChange={(e) => setNewCycle((p) => ({ ...p, label: e.target.value }))}
            />
            <label>
              Invoice submitted{" "}
              <input
                type="date"
                aria-label="New cycle submitted date"
                value={newCycle.submitted}
                disabled={busy}
                onChange={(e) => setNewCycle((p) => ({ ...p, submitted: e.target.value }))}
              />
            </label>
            <input
              type="text" inputMode="decimal" placeholder="Amount $" style={{ width: "7rem" }}
              aria-label="New cycle amount"
              value={newCycle.amount}
              disabled={busy}
              onChange={(e) => setNewCycle((p) => ({ ...p, amount: e.target.value }))}
            />
            <button
              type="button"
              className="btn"
              aria-label="Add payment cycle"
              disabled={busy || !newCycle.label.trim()}
              onClick={addCycle}
            >
              Add cycle
            </button>
          </div>
        </>
      )}
      </div>
    </details>
  );
}
