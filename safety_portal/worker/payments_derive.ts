import type { PaymentState } from "./wire-types";

// ─────────────────────────────────────────────────────────────────────────────
// payments_derive.ts — the payment-cycle state derivation (ADR-0006 decision 10, PR-7).
//
// PURE on purpose: no Hono, no env, no D1, no Date.now() — `derivePaymentState(...)` is a
// deterministic function from (cycle dates, receipt totals, terms, todayPacific) to a
// state + modifiers, so the whole boundary table lives under plain vitest
// (test/payments_derive.test.ts) and the read route CANNOT disagree with the tests: both
// call this one function. Importable from BOTH tsconfig scopes (the wire-types posture) —
// its only import is a type.
//
// NO STORED STATE anywhere (decision 10): this function runs at read time with the
// server's Pacific `today`, so a cycle's chip is always the truth of its dates — a stored
// state column would go stale at midnight without a write.
//
// THE PRECEDENCE (exact, top wins):
//   1. no invoice_submitted_date               → draft
//   2. receipts total ≥ amount (amount known)  → paid   (+ paid_late when the LAST active
//                                                receipt landed after the due date)
//   3. suspend_notice_date recorded            → suspension_notice_sent
//   4. nonpayment_notice_date recorded         → suspension_notice_due when the recorded
//                                                notice is intent_to_suspend_days old,
//                                                else nonpayment_notice_sent
//   5. today past due_date                     → nonpayment_notice_due when overdue by
//                                                nonpayment_notice_days, else overdue
//   6. due within DUE_SOON_DAYS                → due_soon
//   7. otherwise                               → awaiting
//
// NOTICE CLOCKS KEY OFF RECORDED DATES ONLY (steps 3–4): the machine must not pretend a
// clock started — nonpayment_notice_date / suspend_notice_date are what a human RECORDED
// as actually sent (outside ITS; notices are never auto-sent, Invariant 1), so
// "suspension_notice_due" can only ever count from a notice that really went out.
//
// A cycle submitted WITHOUT terms has due_date NULL: steps 5–6 need a due date, so it
// rests at `awaiting` (honest — nothing is knowably due) until terms exist and the
// submitted date is re-entered.
// ─────────────────────────────────────────────────────────────────────────────

/** Days before the due date at which `awaiting` becomes `due_soon`. */
export const DUE_SOON_DAYS = 7;

/** The cycle fields the derivation reads — a row subset, so the route can pass its D1 row. */
export interface PaymentCycleDeriveInput {
  invoice_submitted_date: string | null;
  invoice_amount_cents: number | null;
  due_date: string | null;
  nonpayment_notice_date: string | null;
  suspend_notice_date: string | null;
}

/** The terms fields the derivation reads. NULL terms = no notice thresholds exist, so the
 *  notice-DUE states (which need a threshold) can never fire — only the recorded-SENT ones. */
export interface PaymentTermsDeriveInput {
  nonpayment_notice_days: number;
  intent_to_suspend_days: number;
}

export interface PaymentDerivation {
  state: PaymentState;
  /** amount − active receipts (may be negative on overpayment); null while amount unknown. */
  balance_cents: number | null;
  /** Whole days past due (≥1). Null when not past due or no due date. */
  days_overdue: number | null;
  /** Whole days until due (≥0; 0 = due today). Null when past due or no due date. */
  days_until_due: number | null;
  /** Paid, but the last receipt landed after the due date. */
  paid_late: boolean;
}

/** YYYY-MM-DD → integer day index. Parsed at UTC midnight ON PURPOSE: both operands get
 *  the identical fixed offset, so the difference is exact calendar days regardless of the
 *  host timezone or DST — these are civil DATES (business days on paper), not instants,
 *  and `todayPacific` is already the Pacific calendar date rendered as a string
 *  (pacificDateString), so no further zone math belongs here. */
function dayNum(ymd: string): number {
  const [y, m, d] = ymd.split("-").map(Number);
  return Math.floor(Date.UTC(y, m - 1, d) / 86_400_000);
}

/**
 * Derive one cycle's display state + modifiers.
 *
 * @param cycle              the cycle's date/amount fields (see PaymentCycleDeriveInput)
 * @param receiptsTotalCents sum of the cycle's ACTIVE receipts' amount_cents
 * @param lastReceiptDate    the LATEST received_date among active receipts (null when none)
 * @param terms              the job's terms row, or null when none exists
 * @param todayPacific       the server's Pacific calendar date, YYYY-MM-DD (pacificDateString)
 */
export function derivePaymentState(
  cycle: PaymentCycleDeriveInput,
  receiptsTotalCents: number,
  lastReceiptDate: string | null,
  terms: PaymentTermsDeriveInput | null,
  todayPacific: string,
): PaymentDerivation {
  const balance_cents =
    cycle.invoice_amount_cents === null ? null : cycle.invoice_amount_cents - receiptsTotalCents;

  const today = dayNum(todayPacific);
  const due = cycle.due_date === null ? null : dayNum(cycle.due_date);
  let days_overdue: number | null = null;
  let days_until_due: number | null = null;
  if (due !== null) {
    const delta = today - due;
    if (delta > 0) days_overdue = delta;
    else days_until_due = delta === 0 ? 0 : -delta; // never -0 (Object.is(-0,0) is false — it leaks into JSON/tests)
  }

  // 1 — never invoiced: a planned row. Dates/notices are irrelevant until it goes out.
  if (cycle.invoice_submitted_date === null) {
    return { state: "draft", balance_cents, days_overdue: null, days_until_due: null, paid_late: false };
  }

  // 2 — paid beats everything below it: money in full settles the escalation ladder.
  // Requires a KNOWN amount — receipts against an amountless cycle can't prove settlement.
  if (cycle.invoice_amount_cents !== null && receiptsTotalCents >= cycle.invoice_amount_cents) {
    const paid_late =
      cycle.due_date !== null && lastReceiptDate !== null && dayNum(lastReceiptDate) > dayNum(cycle.due_date);
    return { state: "paid", balance_cents, days_overdue, days_until_due, paid_late };
  }

  // 3 — a RECORDED intent-to-suspend is the ladder's top rung; nothing left to fall due.
  if (cycle.suspend_notice_date !== null) {
    return { state: "suspension_notice_sent", balance_cents, days_overdue, days_until_due, paid_late: false };
  }

  // 4 — a RECORDED nonpayment notice starts the suspend clock (and ONLY a recorded one —
  // see the module header). Without terms there is no threshold, so the clock can't ring.
  if (cycle.nonpayment_notice_date !== null) {
    const noticeAge = today - dayNum(cycle.nonpayment_notice_date);
    const state: PaymentState =
      terms !== null && noticeAge >= terms.intent_to_suspend_days
        ? "suspension_notice_due"
        : "nonpayment_notice_sent";
    return { state, balance_cents, days_overdue, days_until_due, paid_late: false };
  }

  // 5 — past due. The nonpayment notice falls due once the overdue age crosses the terms'
  // threshold; without terms it stays plain `overdue` (no threshold to cross).
  if (days_overdue !== null) {
    const state: PaymentState =
      terms !== null && days_overdue >= terms.nonpayment_notice_days ? "nonpayment_notice_due" : "overdue";
    return { state, balance_cents, days_overdue, days_until_due, paid_late: false };
  }

  // 6 — inside the due-soon window (due today counts).
  if (days_until_due !== null && days_until_due <= DUE_SOON_DAYS) {
    return { state: "due_soon", balance_cents, days_overdue, days_until_due, paid_late: false };
  }

  // 7 — invoiced, not yet near due (or due date unknown — no terms at submit time).
  return { state: "awaiting", balance_cents, days_overdue, days_until_due, paid_late: false };
}

/** Due-date snapshot: submitted + net_days, as calendar-day arithmetic on the day index
 *  (see dayNum — UTC parsing makes this exact). The ROUTE decides when to (re)compute;
 *  this is just the math, shared by create and edit so they cannot drift. */
export function computeDueDate(submittedYmd: string, netDays: number): string {
  const n = dayNum(submittedYmd) + netDays;
  const dt = new Date(n * 86_400_000);
  const y = dt.getUTCFullYear();
  const m = String(dt.getUTCMonth() + 1).padStart(2, "0");
  const d = String(dt.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}
