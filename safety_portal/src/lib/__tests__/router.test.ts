/**
 * G2.5 — router parse/format law tests.
 *
 * The load-bearing invariant: parseRoute(formatRoute(r)) deep-equals r for every canonical
 * route (round-trip law), and every unrecognized/malformed URL parses to null (the caller
 * renders home + normalizes — never a blank page). Param hygiene (length caps, date shape,
 * percent-decoding) is convenience only; the server re-gates everything (Invariant 2).
 */
import { describe, expect, it } from "vitest";
import { HOME_ROUTE, VIEW_CAPS, formatRoute, parseRoute, type AppRoute } from "../router";

const loc = (url: string) => {
  const u = new URL(url, "http://portal.test");
  return { pathname: u.pathname, search: u.search };
};

describe("router — round-trip law", () => {
  const canonical: AppRoute[] = [
    { view: "home" },
    { view: "login" },
    { view: "request" },
    { view: "accounts" },
    { view: "forms" },
    { view: "materials-catalog" },
    { view: "po-builder" },
    { view: "po-vendors" },
    { view: "po-config" },
    { view: "po-estimates" },
    { view: "po-rfqs" },
    { view: "subcontractors" },
    { view: "subcontract-builder" },
    { view: "fieldops-inspections" },
    { view: "fieldops-equipment" },
    { view: "fieldops-personnel" },
    { view: "fieldops-job-materials", jobId: "JOB-000018" },
    // A job id needing percent-encoding — the /jobs/:id/materials matcher must decode it back.
    { view: "fieldops-job-materials", jobId: "JOB 42/A" },
    { view: "fieldops-tasks" },
    { view: "fieldops-tasks", tab: "assigned" },
    { view: "fieldops-tasks", tab: "daily" },
    { view: "fieldops-jobs" },
    { view: "fieldops-jobs", jobId: "JOB-000018" },
    { view: "fill" },
    { view: "fill", prefill: { jobId: "JOB-000018" } },
    {
      view: "fill",
      prefill: {
        jobId: "JOB-000018",
        parentCode: "daily-report",
        variantCode: "v2",
        workDate: "2026-07-01",
      },
    },
  ];

  it.each(canonical.map((r) => [formatRoute(r), r] as const))(
    "parse(format(r)) === r  —  %s",
    (_url, r) => {
      expect(parseRoute(loc(formatRoute(r)))).toEqual(r);
    },
  );

  it("a job id needing percent-encoding survives the round trip", () => {
    const r: AppRoute = { view: "fieldops-jobs", jobId: "JOB A/1" };
    const url = formatRoute(r);
    expect(url).toBe("/jobs/JOB%20A%2F1"); // the encoded slash stays inside one segment
    expect(parseRoute(loc(url))).toEqual(r);
  });

  it("the shared-link URL for a job detail is the documented shape", () => {
    expect(formatRoute({ view: "fieldops-jobs", jobId: "JOB-000018" })).toBe("/jobs/JOB-000018");
  });

  it("the PO-hub tab routes are the documented nested shape (2026-07 fold)", () => {
    expect(formatRoute({ view: "po-builder" })).toBe("/purchase-orders");
    expect(formatRoute({ view: "po-rfqs" })).toBe("/purchase-orders/rfqs");
    expect(formatRoute({ view: "po-estimates" })).toBe("/purchase-orders/estimates");
  });
});

describe("router — parse tolerance + canonicalization", () => {
  it("root and trailing-slash variants parse", () => {
    expect(parseRoute(loc("/"))).toEqual(HOME_ROUTE);
    expect(parseRoute(loc("/tasks/"))).toEqual({ view: "fieldops-tasks" });
    expect(parseRoute(loc("/jobs/JOB-1/"))).toEqual({ view: "fieldops-jobs", jobId: "JOB-1" });
  });

  it("legacy pre-fold /estimates and /rfqs still parse to the folded tabs (parse-only aliases)", () => {
    expect(parseRoute(loc("/estimates"))).toEqual({ view: "po-estimates" });
    expect(parseRoute(loc("/rfqs"))).toEqual({ view: "po-rfqs" });
    // formatRoute never emits the legacy shape — the round-trip law stays on the canonical
    // nested paths; App's entry normalization rewrites a cold-loaded legacy URL.
    expect(formatRoute({ view: "po-estimates" })).toBe("/purchase-orders/estimates");
    expect(formatRoute({ view: "po-rfqs" })).toBe("/purchase-orders/rfqs");
  });

  it("unknown query params on /submit are dropped; known ones survive", () => {
    expect(parseRoute(loc("/submit?job=J1&bogus=x"))).toEqual({
      view: "fill",
      prefill: { jobId: "J1" },
    });
  });

  it("a malformed date is dropped (never seeds the form with garbage)", () => {
    expect(parseRoute(loc("/submit?job=J1&date=yesterday"))).toEqual({
      view: "fill",
      prefill: { jobId: "J1" },
    });
  });

  it("/submit with no (usable) params is the blank fill", () => {
    expect(parseRoute(loc("/submit"))).toEqual({ view: "fill" });
    expect(parseRoute(loc("/submit?date=garbage"))).toEqual({ view: "fill" });
  });

  it("unrecognized paths parse to null (caller homes + normalizes)", () => {
    expect(parseRoute(loc("/bogus"))).toBeNull();
    expect(parseRoute(loc("/jobs/a/b"))).toBeNull();
    expect(parseRoute(loc("/tasks/bogus"))).toBeNull();
    expect(parseRoute(loc("/api/session"))).toBeNull(); // the Worker's fenced namespace
  });

  it("malformed percent-encoding in a job id parses to null, not a crash", () => {
    expect(parseRoute(loc("/jobs/%E0%A4%A"))).toBeNull();
  });

  it("an oversized URL param is rejected (hygiene cap)", () => {
    expect(parseRoute(loc(`/jobs/${"x".repeat(300)}`))).toBeNull();
  });
});

describe("router — VIEW_CAPS gate map", () => {
  it("mirrors the pre-router App switch exactly (fill/request intentionally ungated)", () => {
    expect(VIEW_CAPS).toEqual({
      home: null,
      login: null,
      fill: null,
      request: null,
      accounts: "cap.admin.accounts",
      forms: "cap.admin.formbuilder",
      "materials-catalog": "cap.materials.manage",
      "po-builder": "cap.po.manage",
      "po-vendors": "cap.po.manage",
      "po-config": "cap.po.manage",
      "po-estimates": "cap.po.manage",
      "po-rfqs": "cap.po.manage",
      subcontractors: "cap.subcontracts.manage",
      "subcontract-builder": "cap.subcontracts.manage",
      "fieldops-jobs": "cap.jobtracker.read",
      "fieldops-job-materials": "cap.materials.receive",
      "fieldops-tasks": "cap.tasks.own",
      "fieldops-inspections": "cap.checklist.manage",
      "fieldops-equipment": "cap.equipment.field",
      "fieldops-personnel": "cap.personnel.read",
    });
  });
});
