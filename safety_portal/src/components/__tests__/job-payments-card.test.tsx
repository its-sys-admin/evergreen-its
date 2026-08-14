/**
 * JobPaymentsCard (Track A7) — the tracker's cap-gated payments signpost.
 * Pins: the overdue pill leads with the worst state; the honest no-terms line; next-due copy;
 * and READ-ONLY (the only button is the schedule-page deep link).
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_payments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_payments")>();
  return { ...actual, fetchPaymentsSummary: vi.fn() };
});
import { fetchPaymentsSummary } from "../../lib/fieldops_payments";
import { JobPaymentsCard } from "../JobPaymentsCard";

afterEach(cleanup);

describe("JobPaymentsCard", () => {
  it("leads with the worst overdue state and offers only the deep link", async () => {
    vi.mocked(fetchPaymentsSummary).mockResolvedValue({
      job_id: "JOB-A", has_terms: true, cycle_count: 3, overdue_count: 2,
      worst_state: "nonpayment_notice_due", next_due: null, today: "2026-08-13",
    });
    render(<JobPaymentsCard jobId="JOB-A" onOpenSchedule={() => {}} />);
    expect(await screen.findByText(/2 overdue — nonpayment notice due/)).toBeTruthy();
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["Open payments →"]);
  });

  it("states the honest no-terms case", async () => {
    vi.mocked(fetchPaymentsSummary).mockResolvedValue({
      job_id: "JOB-A", has_terms: false, cycle_count: 0, overdue_count: 0,
      worst_state: null, next_due: null, today: "2026-08-13",
    });
    render(<JobPaymentsCard jobId="JOB-A" />);
    expect(await screen.findByText(/No payment terms are set/)).toBeTruthy();
  });

  it("shows the next due cycle with its open balance", async () => {
    vi.mocked(fetchPaymentsSummary).mockResolvedValue({
      job_id: "JOB-A", has_terms: true, cycle_count: 2, overdue_count: 0,
      worst_state: null,
      next_due: { label: "PP #2", due_date: "2026-09-01", balance_cents: 50000 },
      today: "2026-08-13",
    });
    render(<JobPaymentsCard jobId="JOB-A" />);
    expect(await screen.findByText(/Next due: PP #2 · 2026-09-01 · \$500\.00 open/)).toBeTruthy();
  });
});
