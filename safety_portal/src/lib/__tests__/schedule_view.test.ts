/**
 * Pure view helpers for the Schedule page (2026-08 design pass).
 *
 * These derive three facts the page previously threw away — LATE, SLIP against the immutable
 * baseline anchor, and section/overall ROLLUPS — plus the timeline's geometry. They are pure
 * and `today` is always injected, so every rule here is pinned rather than inferred from a
 * screenshot.
 *
 * What this pins:
 *   • Late means past its finish AND under 100 — never "no finish date", which is absent data
 *     rather than evidence of a problem.
 *   • Percent aggregation is DURATION-WEIGHTED, so thirty one-day punch items cannot outvote a
 *     hundred-day install. A plain mean would read fine and lie.
 *   • Slip is null when nothing moved — the caller renders nothing rather than a soothing "0d".
 *   • A bar's extent treats the finish date as INCLUSIVE (a Mon→Fri task occupies five days).
 *   • Sections group in DOCUMENT order, never alphabetically.
 */
import { describe, expect, it } from "vitest";
import type { ScheduleTaskRow } from "../fieldops_schedules";
import {
  barGeometry,
  dayNumber,
  groupTasks,
  isLate,
  matchesFilter,
  progressBar,
  slipDays,
  summarize,
  timelineDomain,
  weightedPercent,
} from "../schedule_view";

const BASE: ScheduleTaskRow = {
  id: 1, task_uuid: "tu-1", job_id: "JOB-1", section: "Civil", name: "Fencing",
  duration_days: 5, start_date: "2026-09-01", finish_date: "2026-09-05",
  baseline_start_date: "2026-09-01", baseline_finish_date: "2026-09-05",
  percent_done: 50, schedule_percent: 25, is_milestone: 0, is_contract_milestone: 0,
  is_delivery: 0, delivered_date: null, delivered_by_name: null, delivered_at: null,
  predecessors_raw: null, sort_order: 10, last_marked_by_name: null, last_marked_at: null,
  created_at: 1, updated_at: 1,
};
const task = (over: Partial<ScheduleTaskRow>): ScheduleTaskRow => ({ ...BASE, ...over });

const TODAY = "2026-09-10";

describe("isLate", () => {
  it("is late when the finish date has passed and the work is not finished", () => {
    expect(isLate(task({ finish_date: "2026-09-05", percent_done: 50 }), TODAY)).toBe(true);
  });

  it("is NOT late once the work is finished, however late the date", () => {
    expect(isLate(task({ finish_date: "2020-01-01", percent_done: 100 }), TODAY)).toBe(false);
  });

  it("is NOT late on its finish date — the day is not over", () => {
    expect(isLate(task({ finish_date: TODAY, percent_done: 0 }), TODAY)).toBe(false);
  });

  it("is NOT late without a finish date — absent data is not evidence of a problem", () => {
    expect(isLate(task({ finish_date: null, percent_done: 0 }), TODAY)).toBe(false);
  });
});

describe("slipDays", () => {
  it("counts days the finish moved LATER than the baseline anchor", () => {
    expect(slipDays(task({ baseline_finish_date: "2026-09-05", finish_date: "2026-09-12" }))).toBe(7);
  });

  it("counts a pull-in as a negative", () => {
    expect(slipDays(task({ baseline_finish_date: "2026-09-12", finish_date: "2026-09-05" }))).toBe(-7);
  });

  it("is null when nothing moved, so the row shows nothing rather than a soothing 0d", () => {
    expect(slipDays(task({ baseline_finish_date: "2026-09-05", finish_date: "2026-09-05" }))).toBeNull();
  });

  it("is null when either anchor is missing", () => {
    expect(slipDays(task({ baseline_finish_date: null }))).toBeNull();
    expect(slipDays(task({ finish_date: null }))).toBeNull();
  });
});

describe("weightedPercent", () => {
  it("weights by duration, so many tiny finished tasks cannot drown one long unfinished one", () => {
    const tasks = [
      ...Array.from({ length: 30 }, (_, i) => task({ id: i + 10, duration_days: 1, percent_done: 100 })),
      task({ id: 99, duration_days: 100, percent_done: 0 }),
    ];
    // A plain mean would read 97%. Weighted, the 100-day task dominates: 30/130 ≈ 23%.
    expect(weightedPercent(tasks)).toBe(23);
  });

  it("treats a null or zero duration as one day rather than as no weight", () => {
    expect(weightedPercent([task({ duration_days: null, percent_done: 100 }), task({ id: 2, duration_days: null, percent_done: 0 })])).toBe(50);
  });

  it("is 0 for an empty set, never NaN", () => {
    expect(weightedPercent([])).toBe(0);
  });

  it("clamps an out-of-range percent instead of propagating it", () => {
    expect(weightedPercent([task({ percent_done: 150 })])).toBe(100);
  });
});

describe("summarize", () => {
  it("counts done, late, milestones and deliveries, and names the next unreached milestone", () => {
    const s = summarize(
      [
        task({ id: 1, percent_done: 100 }),
        task({ id: 2, percent_done: 20, finish_date: "2026-09-01" }), // late
        task({ id: 3, is_milestone: 1, percent_done: 0, finish_date: "2026-10-01" }),
        task({ id: 4, is_milestone: 1, percent_done: 0, finish_date: "2026-09-20" }), // sooner
        task({ id: 5, is_delivery: 1, percent_done: 0, finish_date: "2026-12-01" }),
      ],
      TODAY,
    );
    expect(s.total).toBe(5);
    expect(s.done).toBe(1);
    expect(s.late).toBe(1);
    expect(s.milestones).toBe(2);
    expect(s.deliveries).toBe(1);
    // The SOONER of the two unreached milestones, not the first in the list.
    expect(s.nextMilestone?.date).toBe("2026-09-20");
  });

  it("does not offer a milestone that is already past as the NEXT one", () => {
    const s = summarize([task({ is_milestone: 1, percent_done: 0, finish_date: "2026-01-01" })], TODAY);
    expect(s.nextMilestone).toBeNull();
    expect(s.late).toBe(1); // it is counted as late instead
  });
});

describe("groupTasks", () => {
  it("groups in DOCUMENT order, never alphabetically — a schedule is read top to bottom", () => {
    const groups = groupTasks(
      [
        task({ id: 1, section: "Civil" }),
        task({ id: 2, section: "Anchors" }),
        task({ id: 3, section: "Civil" }),
      ],
      TODAY,
    );
    expect(groups.map((g) => g.name)).toEqual(["Civil", "Anchors"]);
    expect(groups[0].tasks).toHaveLength(2);
  });

  it("carries a per-section rollup and late count", () => {
    const groups = groupTasks(
      [
        task({ id: 1, section: "Civil", duration_days: 1, percent_done: 100 }),
        task({ id: 2, section: "Civil", duration_days: 1, percent_done: 0, finish_date: "2026-09-01" }),
      ],
      TODAY,
    );
    expect(groups[0].percent).toBe(50);
    expect(groups[0].late).toBe(1);
  });

  it("keeps a null section as its own group rather than dropping the tasks", () => {
    const groups = groupTasks([task({ section: null })], TODAY);
    expect(groups).toHaveLength(1);
    expect(groups[0].name).toBeNull();
  });
});

describe("matchesFilter", () => {
  const t = task({ name: "Trench dig", section: "Civil", percent_done: 40, finish_date: "2026-09-01" });

  it("searches the name AND the section, case-insensitively", () => {
    expect(matchesFilter(t, "all", "TRENCH", TODAY)).toBe(true);
    expect(matchesFilter(t, "all", "civil", TODAY)).toBe(true);
    expect(matchesFilter(t, "all", "racking", TODAY)).toBe(false);
  });

  it("applies the status filters", () => {
    expect(matchesFilter(t, "open", "", TODAY)).toBe(true);
    expect(matchesFilter(task({ percent_done: 100 }), "open", "", TODAY)).toBe(false);
    expect(matchesFilter(t, "late", "", TODAY)).toBe(true);
    expect(matchesFilter(t, "milestones", "", TODAY)).toBe(false);
    expect(matchesFilter(task({ is_delivery: 1 }), "deliveries", "", TODAY)).toBe(true);
  });

  it("combines search AND filter rather than either-or", () => {
    expect(matchesFilter(t, "late", "racking", TODAY)).toBe(false);
  });
});

describe("timelineDomain", () => {
  it("is null when no task carries a date, so the caller shows an honest empty state", () => {
    expect(timelineDomain([task({ start_date: null, finish_date: null })], TODAY)).toBeNull();
  });

  it("spans the job's dates with padding, and places today inside it", () => {
    const d = timelineDomain(
      [task({ start_date: "2026-09-01", finish_date: "2026-09-30" })],
      "2026-09-15",
    )!;
    expect(d.start).toBeLessThan(dayNumber("2026-09-01")!);
    expect(d.end).toBeGreaterThan(dayNumber("2026-09-30")!);
    expect(d.todayPct).not.toBeNull();
    expect(d.todayPct!).toBeGreaterThan(0);
    expect(d.todayPct!).toBeLessThan(100);
  });

  it("reports today as null when it falls outside the job, rather than clamping it to an edge", () => {
    const d = timelineDomain([task({ start_date: "2026-09-01", finish_date: "2026-09-30" })], "2030-01-01")!;
    expect(d.todayPct).toBeNull();
  });

  it("gives a single-day job a usable span instead of dividing by zero", () => {
    const d = timelineDomain([task({ start_date: "2026-09-01", finish_date: "2026-09-01" })], TODAY)!;
    expect(d.span).toBeGreaterThan(0);
    expect(Number.isFinite(d.span)).toBe(true);
  });

  it("emits month ticks across the domain", () => {
    const d = timelineDomain([task({ start_date: "2026-09-01", finish_date: "2026-12-15" })], TODAY)!;
    expect(d.ticks.length).toBeGreaterThanOrEqual(3);
    expect(d.ticks[0].label).toMatch(/^[A-Z][a-z]{2} \d{2}$/);
  });
});

describe("barGeometry", () => {
  const domain = timelineDomain(
    [task({ start_date: "2026-09-01", finish_date: "2026-09-30" })],
    TODAY,
  )!;

  it("treats the finish date as INCLUSIVE — a one-day task still has width", () => {
    const g = barGeometry(task({ start_date: "2026-09-10", finish_date: "2026-09-10", is_milestone: 0 }), domain)!;
    expect(g.widthPct).toBeGreaterThan(0);
  });

  it("gives a longer task a proportionally wider bar", () => {
    const short = barGeometry(task({ start_date: "2026-09-02", finish_date: "2026-09-04" }), domain)!;
    const long = barGeometry(task({ start_date: "2026-09-02", finish_date: "2026-09-20" }), domain)!;
    expect(long.widthPct).toBeGreaterThan(short.widthPct * 2);
  });

  it("marks a milestone as a POINT so it draws as a diamond, not a hairline bar", () => {
    expect(barGeometry(task({ is_milestone: 1 }), domain)!.point).toBe(true);
  });

  it("is null when the task carries no usable date", () => {
    expect(barGeometry(task({ start_date: null, finish_date: null }), domain)).toBeNull();
  });

  it("survives a task whose dates fall outside the domain by clamping into it", () => {
    const g = barGeometry(task({ start_date: "2020-01-01", finish_date: "2020-02-01" }), domain)!;
    expect(g.leftPct).toBeGreaterThanOrEqual(0);
    expect(g.leftPct + g.widthPct).toBeLessThanOrEqual(100.001);
  });
});

describe("progressBar", () => {
  it("renders ten cells, floor-rounded, so 99% still shows one open cell", () => {
    expect(progressBar(50)).toBe("█████░░░░░ 50%");
    expect(progressBar(99)).toBe("█████████░ 99%");
    expect(progressBar(100)).toBe("██████████ 100%");
    expect(progressBar(0)).toBe("░░░░░░░░░░ 0%");
  });
});
