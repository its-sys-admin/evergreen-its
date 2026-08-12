/**
 * Payments section of the per-job Schedule page (ADR-0006 decision 10, PR-7).
 *
 * What this pins:
 *   • The affordance wall: WITHOUT cap.payments.manage the section does not exist and
 *     NO payments fetch ever fires (the Worker re-gates — this is the SPA half of
 *     operator decision 4's admin-only visibility).
 *   • State chips render HUMANIZED copy per derived state (paid/late, overdue-with-days,
 *     notice-due), with the outstanding balance on a partial.
 *   • The same-client terms prefill flow (operator decision 6): the offer button names
 *     the client, copies values into the editor, and Save submits them.
 *   • Recording a payment posts integer cents parsed from the dollars text.
 *
 * Mocks the payments lib + the schedule lib + useAuth (the JobSchedulePage convention).
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_schedules", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_schedules")>();
  return {
    ...actual,
    fetchScheduleTasks: vi.fn(),
    fetchSchedules: vi.fn(),
  };
});
vi.mock("../../lib/fieldops_payments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_payments")>();
  return {
    ...actual,
    fetchPayments: vi.fn(),
    fetchTermsDefault: vi.fn(),
    saveTerms: vi.fn(),
    createCycle: vi.fn(),
    editCycle: vi.fn(),
    recordNotice: vi.fn(),
    addReceipt: vi.fn(),
    deactivateCycle: vi.fn(),
    deactivateReceipt: vi.fn(),
  };
});
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as scheduleApi from "../../lib/fieldops_schedules";
import * as api from "../../lib/fieldops_payments";
import type { PaymentCycleView, PaymentTermsRow } from "../../lib/fieldops_payments";
import { useAuth } from "../../lib/auth";
import { JobSchedulePage } from "../JobSchedulePage";

function authWith(capabilities: string[]) {
  return {
    user: { username: "u", role: "admin" as const, capabilities },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  };
}

const TERMS: PaymentTermsRow = {
  job_id: "JOB-000018", billing_cadence: "monthly", billing_cadence_detail: null,
  net_days: 30, nonpayment_notice_days: 10, intent_to_suspend_days: 14, notes: null,
  created_at: 1, updated_at: 1,
};

const BASE_CYCLE: PaymentCycleView = {
  id: 1, cycle_uuid: "cu-1", job_id: "JOB-000018", seq: 10, label: "PP #1",
  invoice_submitted_date: "2026-06-01", invoice_amount_cents: 100_000,
  due_date: "2026-07-01", nonpayment_notice_date: null, suspend_notice_date: null,
  note: null, created_at: 1, updated_at: 1, receipts: [],
  state: "awaiting", balance_cents: 100_000, days_overdue: null, days_until_due: 16,
  paid_late: false,
};

function mountWith(caps: string[]) {
  vi.mocked(useAuth).mockReturnValue(authWith(caps));
  return render(<JobSchedulePage jobId="JOB-000018" onHome={vi.fn()} onOpenJob={vi.fn()} />);
}

const READ_ONLY = ["cap.jobtracker.read"];
const PAY = ["cap.jobtracker.read", "cap.payments.manage"];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(scheduleApi.fetchScheduleTasks).mockResolvedValue({
    tasks: [], project_name: "Deep Lake", truncated: false,
  });
  vi.mocked(scheduleApi.fetchSchedules).mockResolvedValue({ schedules: [] });
  vi.mocked(api.fetchPayments).mockResolvedValue({
    terms: TERMS, cycles: [BASE_CYCLE], project_name: "Deep Lake",
  });
  vi.mocked(api.fetchTermsDefault).mockResolvedValue({
    terms: null, client_name: null, source_project_name: null,
  });
});
afterEach(cleanup);

describe("PaymentsSection — the capability wall (SPA half of operator decision 4)", () => {
  it("without cap.payments.manage the section does not render and NO payments fetch fires", async () => {
    const { container } = mountWith(READ_ONLY);
    await waitFor(() => expect(scheduleApi.fetchScheduleTasks).toHaveBeenCalled());
    expect(container.textContent ?? "").not.toContain("Payments");
    expect(api.fetchPayments).not.toHaveBeenCalled();
    expect(api.fetchTermsDefault).not.toHaveBeenCalled();
  });

  it("with the cap the section renders (collapsible card) and loads the job's payments", async () => {
    const { container } = mountWith(PAY);
    await waitFor(() => expect(api.fetchPayments).toHaveBeenCalledWith("JOB-000018"));
    await waitFor(() => expect(container.textContent ?? "").toContain("PP #1"));
    expect(container.textContent ?? "").toContain("Payments");
    // Terms summary renders the recorded values.
    expect(container.textContent ?? "").toContain("Net 30");
  });

  it("a failed payments load says so with a working Retry (never silent)", async () => {
    vi.mocked(api.fetchPayments).mockRejectedValueOnce(new Error("boom"));
    const { findAllByRole, getByText } = mountWith(PAY);
    const alerts = await findAllByRole("alert");
    expect(alerts.some((a) => (a.textContent ?? "").includes("Could not load this job's payments"))).toBe(true);
    fireEvent.click(getByText("Retry"));
    await waitFor(() => expect(api.fetchPayments).toHaveBeenCalledTimes(2));
  });
});

describe("PaymentsSection — state chips (humanized derived states)", () => {
  it("renders per-state copy: draft, due-in-days, overdue-with-days, notice DUE, paid late — and the balance on a partial", async () => {
    vi.mocked(api.fetchPayments).mockResolvedValue({
      terms: TERMS, project_name: "Deep Lake",
      cycles: [
        { ...BASE_CYCLE, id: 1, label: "Draft one", invoice_submitted_date: null, due_date: null, state: "draft", balance_cents: null, days_until_due: null },
        { ...BASE_CYCLE, id: 2, label: "Soon", state: "due_soon", days_until_due: 3 },
        {
          ...BASE_CYCLE, id: 3, label: "Late partial", state: "overdue", days_overdue: 12,
          balance_cents: 60_000,
          receipts: [{ id: 9, received_date: "2026-06-20", amount_cents: 40_000, note: null, recorded_at: 2 }],
        },
        { ...BASE_CYCLE, id: 4, label: "Escalate", state: "nonpayment_notice_due", days_overdue: 30 },
        { ...BASE_CYCLE, id: 5, label: "Done", state: "paid", paid_late: true, balance_cents: 0 },
      ],
    });
    const { container } = mountWith(PAY);
    await waitFor(() => expect(container.textContent ?? "").toContain("Draft one"));
    const text = container.textContent ?? "";
    expect(text).toContain("Draft — not invoiced");
    expect(text).toContain("Due in 3d");
    expect(text).toContain("Overdue 12d");
    expect(text).toContain("$600.00 outstanding"); // the partial's balance
    expect(text).toContain("$400.00 on 2026-06-20"); // the receipt line
    expect(text).toContain("Nonpayment notice DUE");
    expect(text).toContain("Paid (late)");
  });
});

describe("PaymentsSection — the same-client terms prefill flow (operator decision 6)", () => {
  it("no terms + a prefill available → the button names the client, copies values into the editor, and Save submits them", async () => {
    vi.mocked(api.fetchPayments).mockResolvedValue({ terms: null, cycles: [], project_name: "Deep Lake" });
    vi.mocked(api.fetchTermsDefault).mockResolvedValue({
      terms: {
        billing_cadence: "milestone", billing_cadence_detail: "per PP schedule",
        net_days: 45, nonpayment_notice_days: 15, intent_to_suspend_days: 21, notes: null,
        created_at: 1, updated_at: 1,
      },
      client_name: "Acme Solar",
      source_project_name: "Kestrel",
    });
    vi.mocked(api.saveTerms).mockResolvedValue({ ok: true, terms: TERMS });

    const { container, getByText, getByLabelText } = mountWith(PAY);
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("No payment terms recorded"),
    );
    fireEvent.click(getByText("Copy from Acme Solar's latest job"));
    // The editor opened with the COPIED values (a value copy, not a link — decision 6).
    expect((getByLabelText("Net days") as HTMLInputElement).value).toBe("45");
    expect((getByLabelText("Nonpayment notice days") as HTMLInputElement).value).toBe("15");
    expect((getByLabelText("Intent to suspend days") as HTMLInputElement).value).toBe("21");
    expect((getByLabelText("Billing cadence") as HTMLSelectElement).value).toBe("milestone");

    fireEvent.click(getByLabelText("Save terms"));
    await waitFor(() =>
      expect(api.saveTerms).toHaveBeenCalledWith("JOB-000018", {
        billing_cadence: "milestone",
        billing_cadence_detail: "per PP schedule",
        net_days: 45,
        nonpayment_notice_days: 15,
        intent_to_suspend_days: 21,
        notes: null,
      }),
    );
    await waitFor(() => expect(api.fetchPayments).toHaveBeenCalledTimes(2)); // mount + reload
  });

  it("out-of-bounds days refuse LOCALLY with the worker's copy — no call goes out", async () => {
    vi.mocked(api.fetchPayments).mockResolvedValue({ terms: null, cycles: [], project_name: null });
    const { container, getByText, getByLabelText, findAllByRole } = mountWith(PAY);
    await waitFor(() => expect(container.textContent ?? "").toContain("Set terms"));
    fireEvent.click(getByText("Set terms"));
    fireEvent.change(getByLabelText("Net days"), { target: { value: "366" } });
    fireEvent.click(getByLabelText("Save terms"));
    const alerts = await findAllByRole("alert");
    expect(alerts.some((a) => (a.textContent ?? "").includes("0 to 365"))).toBe(true);
    expect(api.saveTerms).not.toHaveBeenCalled();
  });
});

describe("PaymentsSection — receipts and notices", () => {
  it("recording a payment parses dollars text to integer cents and posts it", async () => {
    vi.mocked(api.addReceipt).mockResolvedValue({ ok: true, id: 9, cycle_id: 1 });
    const { container, getByLabelText } = mountWith(PAY);
    await waitFor(() => expect(container.textContent ?? "").toContain("PP #1"));
    fireEvent.change(getByLabelText("Payment amount for PP #1"), { target: { value: "1,234.56" } });
    fireEvent.click(getByLabelText("Record payment for PP #1"));
    await waitFor(() =>
      expect(api.addReceipt).toHaveBeenCalledWith(1, { amount_cents: 123_456 }),
    );
  });

  it("a DRAFT cycle gets the Mark-submitted control — the edit route carries the date so the Worker stamps the due-date snapshot", async () => {
    vi.mocked(api.fetchPayments).mockResolvedValue({
      terms: TERMS, project_name: "Deep Lake",
      cycles: [{
        ...BASE_CYCLE, id: 7, label: "Draft PP", invoice_submitted_date: null, due_date: null,
        state: "draft", days_until_due: null,
      }],
    });
    vi.mocked(api.editCycle).mockResolvedValue({ ok: true, id: 7, due_date: "2026-07-01" });
    const { container, getByLabelText } = mountWith(PAY);
    await waitFor(() => expect(container.textContent ?? "").toContain("Draft PP"));
    const btn = getByLabelText("Mark Draft PP submitted") as HTMLButtonElement;
    expect(btn.disabled).toBe(true); // no date picked yet
    fireEvent.change(getByLabelText("Submitted date for Draft PP"), { target: { value: "2026-06-01" } });
    fireEvent.click(btn);
    await waitFor(() =>
      expect(api.editCycle).toHaveBeenCalledWith(7, {
        label: "Draft PP",
        invoice_submitted_date: "2026-06-01",
        invoice_amount_cents: 100_000,
        note: null,
      }),
    );
    // A SUBMITTED cycle never shows the control (the date corrects through the Worker's
    // edit route, not this quick affordance).
    cleanup();
    vi.mocked(api.fetchPayments).mockResolvedValue({
      terms: TERMS, project_name: "Deep Lake", cycles: [BASE_CYCLE],
    });
    const submitted = mountWith(PAY);
    await waitFor(() => expect(submitted.container.textContent ?? "").toContain("PP #1"));
    expect(submitted.queryByLabelText("Mark PP #1 submitted")).toBeNull();
  });

  it("recording a nonpayment notice runs the confirm gate and says it only records a date", async () => {
    vi.mocked(api.recordNotice).mockResolvedValue({ ok: true, id: 1, kind: "nonpayment", date: "2026-06-15" });
    const { container, getByLabelText } = mountWith(PAY);
    await waitFor(() => expect(container.textContent ?? "").toContain("PP #1"));
    fireEvent.click(getByLabelText("Record nonpayment notice for PP #1"));
    // The confirm copy is explicit that NOTHING is sent (Invariant 1's display-only lane).
    expect(container.textContent ?? "").toContain("nothing is sent");
    fireEvent.click(getByLabelText("Confirm Record nonpayment notice for PP #1"));
    await waitFor(() => expect(api.recordNotice).toHaveBeenCalledWith(1, "nonpayment"));
  });
});
