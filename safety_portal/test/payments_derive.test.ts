import { describe, it, expect } from "vitest";
import {
  DUE_SOON_DAYS,
  computeDueDate,
  derivePaymentState,
  type PaymentCycleDeriveInput,
  type PaymentTermsDeriveInput,
} from "../worker/payments_derive";

// ─────────────────────────────────────────────────────────────────────────────
// payments_derive — the pure state-derivation boundary table (ADR-0006 decision 10,
// PR-7). The function is PURE (fixed `todayPacific` string in, state out), so every
// threshold edge is pinned here at day precision; the worker suite
// (fieldops-payments.test.ts) covers what THIS file structurally cannot — the D1
// read/write plumbing and the live-`today` derivation on real rows.
//
// The invariant under test everywhere: the notice clocks key off RECORDED dates only.
// No recorded nonpayment notice ⇒ the suspend clock cannot exist, no matter how
// overdue the cycle is — the machine must not pretend a notice went out.
// ─────────────────────────────────────────────────────────────────────────────

const TODAY = "2026-06-15";

const TERMS: PaymentTermsDeriveInput = {
  nonpayment_notice_days: 10,
  intent_to_suspend_days: 14,
};

/** A submitted cycle with sensible defaults; override per case. */
function cycle(over: Partial<PaymentCycleDeriveInput> = {}): PaymentCycleDeriveInput {
  return {
    invoice_submitted_date: "2026-06-01",
    invoice_amount_cents: 100_000,
    due_date: "2026-07-01",
    nonpayment_notice_date: null,
    suspend_notice_date: null,
    ...over,
  };
}

function state(
  over: Partial<PaymentCycleDeriveInput>,
  receipts = 0,
  lastReceipt: string | null = null,
  terms: PaymentTermsDeriveInput | null = TERMS,
  today = TODAY,
) {
  return derivePaymentState(cycle(over), receipts, lastReceipt, terms, today);
}

describe("derivePaymentState — the precedence boundary table", () => {
  it("no submitted date → draft, beating everything else (even full receipts and notices)", () => {
    expect(state({ invoice_submitted_date: null }).state).toBe("draft");
    // Draft wins over receipts ≥ amount and over recorded notices — an un-invoiced
    // cycle has no clock to be anywhere on.
    const d = state(
      { invoice_submitted_date: null, nonpayment_notice_date: "2026-06-01", suspend_notice_date: "2026-06-10" },
      100_000,
      "2026-06-10",
    );
    expect(d.state).toBe("draft");
    expect(d.days_overdue).toBeNull();
    expect(d.days_until_due).toBeNull();
    // balance still reports (the office sees the draft's planned amount).
    expect(d.balance_cents).toBe(0);
  });

  it("due-date walk: due in 8d → awaiting; 7d (the DUE_SOON edge) → due_soon; due today → due_soon; 1d past → overdue", () => {
    expect(DUE_SOON_DAYS).toBe(7);
    const at = (due: string) => state({ due_date: due });
    expect(at("2026-06-23")).toMatchObject({ state: "awaiting", days_until_due: 8 });
    expect(at("2026-06-22")).toMatchObject({ state: "due_soon", days_until_due: 7 });
    expect(at("2026-06-15")).toMatchObject({ state: "due_soon", days_until_due: 0 });
    expect(at("2026-06-14")).toMatchObject({ state: "overdue", days_overdue: 1, days_until_due: null });
  });

  it("overdue → nonpayment_notice_due exactly at the terms threshold (9d under, 10d at)", () => {
    // nonpayment_notice_days = 10: overdue 9 → still overdue; overdue 10 → the notice falls due.
    expect(state({ due_date: "2026-06-06" })).toMatchObject({ state: "overdue", days_overdue: 9 });
    expect(state({ due_date: "2026-06-05" })).toMatchObject({
      state: "nonpayment_notice_due",
      days_overdue: 10,
    });
  });

  it("without terms there is NO notice threshold — deep overdue stays plain overdue", () => {
    expect(state({ due_date: "2026-01-01" }, 0, null, null).state).toBe("overdue");
  });

  it("a RECORDED nonpayment notice starts the suspend clock: 13d old → sent, 14d (the edge) → suspension_notice_due", () => {
    // intent_to_suspend_days = 14, keyed off the RECORDED date — not the due date.
    expect(state({ due_date: "2026-05-01", nonpayment_notice_date: "2026-06-02" }).state).toBe(
      "nonpayment_notice_sent",
    );
    expect(state({ due_date: "2026-05-01", nonpayment_notice_date: "2026-06-01" }).state).toBe(
      "suspension_notice_due",
    );
    // Recorded today → the clock just started.
    expect(state({ due_date: "2026-05-01", nonpayment_notice_date: TODAY }).state).toBe(
      "nonpayment_notice_sent",
    );
  });

  it("a recorded nonpayment notice WITHOUT terms can never ring the suspend clock (no threshold)", () => {
    expect(state({ due_date: "2026-01-01", nonpayment_notice_date: "2026-01-15" }, 0, null, null).state).toBe(
      "nonpayment_notice_sent",
    );
  });

  it("a RECORDED suspend notice is the top rung — suspension_notice_sent regardless of every clock", () => {
    expect(
      state({ due_date: "2026-01-01", nonpayment_notice_date: "2026-01-15", suspend_notice_date: "2026-02-01" })
        .state,
    ).toBe("suspension_notice_sent");
  });

  it("UNRECORDED clocks never fire: deep overdue with NO recorded nonpayment notice skips every *_sent/suspend state", () => {
    // A year overdue — but nobody recorded a notice, so the machine reports only that
    // the NEXT step (the nonpayment notice) has fallen due. It never pretends further.
    const d = state({ due_date: "2025-06-15" });
    expect(d.state).toBe("nonpayment_notice_due");
    expect(d.days_overdue).toBe(365);
  });

  it("paid: receipts ≥ amount settles everything (notices included); paid_late keys on the LAST receipt vs due", () => {
    // On time (last receipt on the due date is NOT late).
    expect(state({}, 100_000, "2026-07-01")).toMatchObject({
      state: "paid", paid_late: false, balance_cents: 0,
    });
    // Late (last receipt one day past due).
    expect(state({ due_date: "2026-06-01" }, 100_000, "2026-06-02")).toMatchObject({
      state: "paid", paid_late: true,
    });
    // Paid beats a recorded suspend notice — money in full ends the ladder.
    expect(
      state(
        { due_date: "2026-05-01", nonpayment_notice_date: "2026-05-10", suspend_notice_date: "2026-06-01" },
        100_000,
        "2026-06-10",
      ),
    ).toMatchObject({ state: "paid", paid_late: true });
    // Overpayment: still paid, balance goes negative (the office sees the credit).
    expect(state({}, 120_000, "2026-06-10").balance_cents).toBe(-20_000);
    // No due date at all → paid can't be late.
    expect(state({ due_date: null }, 100_000, "2026-06-10")).toMatchObject({
      state: "paid", paid_late: false,
    });
  });

  it("partial receipts change the BALANCE, never the state (the escalation clock keeps running)", () => {
    const d = state({ due_date: "2026-06-05" }, 40_000, "2026-06-10");
    expect(d.state).toBe("nonpayment_notice_due"); // same as unpaid at 10d overdue
    expect(d.balance_cents).toBe(60_000);
  });

  it("an amount-less cycle can never read paid — receipts against an unknown amount prove nothing", () => {
    const d = state({ invoice_amount_cents: null }, 500_000, "2026-06-10");
    expect(d.state).not.toBe("paid");
    expect(d.balance_cents).toBeNull();
  });

  it("submitted without a due date (no terms at submit time) rests at awaiting — nothing is knowably due", () => {
    const d = state({ due_date: null });
    expect(d.state).toBe("awaiting");
    expect(d.days_overdue).toBeNull();
    expect(d.days_until_due).toBeNull();
  });
});

describe("computeDueDate — the stored snapshot's math", () => {
  it("adds calendar days across month/year boundaries (UTC day-index math — no DST drift)", () => {
    expect(computeDueDate("2026-06-01", 30)).toBe("2026-07-01");
    expect(computeDueDate("2026-01-31", 30)).toBe("2026-03-02"); // through February
    expect(computeDueDate("2026-12-31", 1)).toBe("2027-01-01"); // year rollover
    expect(computeDueDate("2026-06-01", 0)).toBe("2026-06-01"); // net 0 = due on submit
    // Spans the March DST change — a naive local-Date implementation can land 23:00
    // the previous day; the day-index math cannot.
    expect(computeDueDate("2026-03-01", 15)).toBe("2026-03-16");
  });
});
