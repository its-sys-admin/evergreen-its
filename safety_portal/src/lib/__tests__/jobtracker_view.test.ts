// The job-tracker view derivations, extracted from FieldOpsJobTracker.tsx so the rules
// that carry arithmetic (voided → 0, null hours → 0) have a direct test.

import { describe, expect, it } from "vitest";
import {
  fmtHours,
  jobPillClass,
  loggedHours,
  openTaskCount,
  taskPillClass,
} from "../jobtracker_view";

describe("fmtHours", () => {
  it("renders two decimals", () => {
    expect(fmtHours(7.5)).toBe("7.50");
    expect(fmtHours(0)).toBe("0.00");
  });

  it("renders an em-dash for absent hours (open clock-in) and NaN", () => {
    expect(fmtHours(null)).toBe("—");
    expect(fmtHours(NaN)).toBe("—");
  });
});

describe("openTaskCount", () => {
  it("counts open and in_progress, excludes done", () => {
    expect(
      openTaskCount([
        { status: "open" },
        { status: "in_progress" },
        { status: "done" },
      ]),
    ).toBe(2);
  });

  it("is zero on an empty list", () => {
    expect(openTaskCount([])).toBe(0);
  });
});

describe("loggedHours", () => {
  it("sums hours across entries", () => {
    expect(
      loggedHours([
        { voided: false, hours: 4 },
        { voided: false, hours: 3.5 },
      ]),
    ).toBe(7.5);
  });

  it("treats a VOIDED entry as a correction to zero", () => {
    expect(
      loggedHours([
        { voided: false, hours: 8 },
        { voided: true, hours: 8 },
      ]),
    ).toBe(8);
  });

  it("treats null hours (an open clock-in) as zero, never NaN", () => {
    expect(
      loggedHours([
        { voided: false, hours: null },
        { voided: false, hours: 2 },
      ]),
    ).toBe(2);
  });

  it("is zero on an empty list", () => {
    expect(loggedHours([])).toBe(0);
  });
});

describe("pill classes", () => {
  it("maps job statuses, falling through to the bare pill for closed/unknown", () => {
    expect(jobPillClass("active")).toBe("dash-pill dash-pill--ok");
    expect(jobPillClass("on_hold")).toBe("dash-pill dash-pill--warn");
    expect(jobPillClass("closed")).toBe("dash-pill");
    expect(jobPillClass("anything-else")).toBe("dash-pill");
  });

  it("maps task statuses, falling through to the bare pill for open/unknown", () => {
    expect(taskPillClass("in_progress")).toBe("dash-pill dash-pill--warn");
    expect(taskPillClass("done")).toBe("dash-pill dash-pill--ok");
    expect(taskPillClass("open")).toBe("dash-pill");
  });
});
