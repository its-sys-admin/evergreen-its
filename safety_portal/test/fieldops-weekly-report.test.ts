import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, json, seedJob, login, g, ADMIN_BEARER } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// Weekly Production Report (0067) — the aggregation + office-input record behind the
// client-facing 5-page report. Runs against the REAL worker with Miniflare D1.
//
// TWO gates on ONE derivation: bearer /api/internal/production-report (the Mac compile) and
// session+cap.jobtracker.manage /api/fieldops/weekly-report (the office screen). The suite
// proves both gates fail closed, that a malformed window returns NOTHING rather than a
// zero-looking week, that every derived leg reads what the field actually filed, and that the
// three-state photo contract survives a save.
// ─────────────────────────────────────────────────────────────────────────────

const INTERNAL_BEARER = "test-internal-token";
const JOB = "JOB-WPR-1";

// A Sat→Fri week. week_start/week_end bound the work_date legs; from/to bound the epoch ones.
const WEEK_START = "2026-08-08";
const WEEK_END = "2026-08-14";
const FROM = 1_786_000_000;
const TO = FROM + 7 * 86400;

const qs = (over: Record<string, string> = {}): string => {
  const base: Record<string, string> = {
    job_id: JOB, week_start: WEEK_START, week_end: WEEK_END,
    from: String(FROM), to: String(TO),
  };
  return new URLSearchParams({ ...base, ...over }).toString();
};

type PhotoPick = { pool_id: number; box_file_id: string; caption: string; work_date: string };
type ReportBody = {
  job_id: string;
  week: { start: string; end: string; from: number; to: number };
  job: { project_name: string } | null;
  daily_report_count: number;
  weather: {
    days: { work_date: string; conditions: string; avg_temp: string; inclement: boolean }[];
    weather_days_week: number;
    weather_days_to_date: number;
  };
  labor: { total_hours: number; crews: { company: string; workers: number; days: number }[] };
  crew_progress: { work_date: string; crew: string; manpower: string; progress: string }[];
  daily_notes: { work_date: string; tomorrows_goals: string; comments: string; prepared_by: string }[];
  hazard_form_codes: string[];
  deliveries: { event_date: string; item: string; vendor: string; qty: string }[];
  material_incidents: { material: string; issue: string }[];
  photos: { available: PhotoPick[]; selected: PhotoPick[]; auto_selected: boolean };
  schedule: null;
  office: {
    header: { ess_management: string; subcontractors: string[]; mobilization_date: string };
    safety: Record<string, { month: number; to_date: number }>;
    weather: { inclement_dates: string[]; weather_days_to_date: number };
    labor: { rows: { company: string; workers: string; man_hours: string }[] };
    narrative: { critical_items: string; upcoming_activities: string; hazard_topics: string[] };
    pending: { rfis: string; submittals: string; ifc_review: string; change_orders: string };
    photos: PhotoPick[] | null;
    saved: boolean;
    carried_from: string | null;
  };
};

// `bearer: null` means SEND NO Authorization header. A `string | undefined` parameter with a
// default would silently substitute the real token for `undefined` — which is exactly how the
// original "rejects a missing bearer" test passed while authenticating. null cannot be defaulted.
const internal = (query = qs(), bearer: string | null = INTERNAL_BEARER): Promise<Response> =>
  call(`/api/internal/production-report?${query}`, bearer === null ? {} : { bearer });

const body = (res: Response) => json<ReportBody>(res);
const err = (res: Response) => json<{ error: string }>(res);

// ── seeds ───────────────────────────────────────────────────────────────────
async function seedDaily(
  workDate: string,
  values: Record<string, unknown>,
  opts: { uuid?: string; actor?: string; amendsUuid?: string | null } = {},
): Promise<string> {
  const uuid = opts.uuid ?? `sub-${workDate}-${Math.abs(hash(JSON.stringify(values)))}`;
  await env.DB.prepare(
    "INSERT INTO submissions (submission_uuid, job_id, form_code, work_date, payload_json, created_at, actor_username, amends_uuid, box_verified) VALUES (?,?,?,?,?,?,?,?,1)",
  ).bind(
    uuid, JOB, "daily-report-v7", workDate, JSON.stringify(values),
    FROM + 3600, opts.actor ?? "pm.one", opts.amendsUuid ?? null,
  ).run();
  return uuid;
}
// Tiny deterministic string hash so seeded uuids are stable without Math.random.
function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) | 0;
  return h;
}

// Accounts persist across tests in a file (beforeEach clears job data, not the user table), so
// provisioning the same username twice 409s. Provision-once-then-login keeps each test able to
// ask for a cookie without caring whether an earlier test already created the account.
async function accountCookie(username: string, role: "submitter" | "manager" | "admin"): Promise<string> {
  const password = `pw-${username.replace(/\W/g, "")}-000001`;
  const existing = await call("/api/internal/admin/users", {
    method: "POST", bearer: ADMIN_BEARER, body: JSON.stringify({ username, password, role }),
  });
  if (existing.status !== 201 && existing.status !== 409) {
    throw new Error(`provision ${username} failed: ${existing.status} ${await existing.text()}`);
  }
  return login(username, password);
}

async function seedOtherForm(formCode: string, workDate: string, values: Record<string, unknown> = {}): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO submissions (submission_uuid, job_id, form_code, work_date, payload_json, created_at, actor_username, box_verified) VALUES (?,?,?,?,?,?,?,1)",
  ).bind(`o-${formCode}-${workDate}`, JOB, formCode, workDate, JSON.stringify(values), FROM + 100, "pm.one").run();
}

async function seedPhoto(
  workDate: string, status: string, boxFileId: string | null, claimedBy: string | null = null,
): Promise<number> {
  const r = await env.DB.prepare(
    "INSERT INTO daily_photo_pool (job_id, work_date, uploaded_by, status, hmac, box_file_id, created_at, claimed_by_submission) VALUES (?,?,?,?,?,?,?,?)",
  ).bind(JOB, workDate, "pm.one", status, "deadbeef", boxFileId, FROM + 10, claimedBy).run();
  return Number(r.meta.last_row_id);
}

async function seedTime(uuid: string, hours: number, amends: string | null = null): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO time_entries (uuid, job_id, work_started_at, hours, created_at, actor_username, amends_uuid) VALUES (?,?,?,?,?,?,?)",
  ).bind(uuid, JOB, FROM + 7200, hours, FROM + 7200, "pm.one", amends).run();
}

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_weekly_report_inputs"),
    env.DB.prepare("DELETE FROM daily_photo_pool"),
    env.DB.prepare("DELETE FROM material_receipt_events"),
    env.DB.prepare("DELETE FROM material_shipments"),
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM material_catalog"),
    env.DB.prepare("DELETE FROM time_entries"),
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
  await seedJob(JOB, { projectName: "Bonacci 2" });
});

// ── auth: both gates fail closed ────────────────────────────────────────────
describe("weekly report — auth", () => {
  it("internal route rejects a missing bearer", async () => {
    expect((await internal(qs(), null)).status).toBe(401);
  });

  it("internal route rejects a wrong bearer", async () => {
    expect((await internal(qs(), "not-the-token")).status).toBe(401);
  });

  // CROSS-LANE RED-PROOF: every other lane's bearer is a DIFFERENT privilege class and must not
  // open this route. A shared-secret slip here would hand a document-decoding lane read access to
  // a client's safety statistics.
  it.each([
    ["admin", "test-admin-token"],
    ["fieldops", "test-fieldops-token"],
    ["manifest", "test-manifest-token"],
    ["po", "test-po-token"],
  ])("internal route rejects the %s bearer", async (_name, token) => {
    expect((await internal(qs(), token)).status).toBe(401);
  });

  it("session route rejects an anonymous caller", async () => {
    expect((await call(`/api/fieldops/weekly-report?${qs()}`)).status).toBe(401);
  });

  it("session route rejects a submitter (no cap.jobtracker.manage)", async () => {
    const cookie = await accountCookie("sub.one", "submitter");
    expect((await g(cookie, `/api/fieldops/weekly-report?${qs()}`)).status).toBe(403);
  });

  it("session route admits an admin and returns the same shape as the internal route", async () => {
    const cookie = await accountCookie("adm.one", "admin");
    const viaSession = await g(cookie, `/api/fieldops/weekly-report?${qs()}`);
    expect(viaSession.status).toBe(200);
    const a = await body(viaSession);
    const b = await body(await internal());
    // The office must see exactly what the report will render — a second derivation would be a
    // second truth. generated_at is the only field allowed to differ.
    expect({ ...a, generated_at: 0 }).toEqual({ ...(b as object), generated_at: 0 });
  });
});

// ── bounds: reject the WHOLE request, never a zero-looking week ──────────────
describe("weekly report — window validation", () => {
  it.each([
    ["missing job_id", { job_id: "" }, "invalid_job_id"],
    ["oversized job_id", { job_id: "x".repeat(65) }, "invalid_job_id"],
    ["malformed week_start", { week_start: "08/08/2026" }, "invalid_week"],
    ["malformed week_end", { week_end: "2026-8-14" }, "invalid_week"],
    ["inverted week", { week_start: "2026-08-14", week_end: "2026-08-08" }, "invalid_week"],
    ["non-numeric from", { from: "abc" }, "invalid_window"],
    ["negative to", { to: "-5" }, "invalid_window"],
    ["inverted epoch window", { from: String(TO), to: String(FROM) }, "invalid_window"],
    ["equal epoch window", { from: String(FROM), to: String(FROM) }, "invalid_window"],
  ])("400s on %s and returns no data", async (_name, over, code) => {
    const res = await internal(qs(over as Record<string, string>));
    expect(res.status).toBe(400);
    const payload = await err(res);
    expect(payload.error).toBe(code);
    expect(payload).not.toHaveProperty("weather");
    expect(payload).not.toHaveProperty("daily_report_count");
  });
});

// ── derived legs ────────────────────────────────────────────────────────────
describe("weekly report — derived data", () => {
  it("returns weather one row per daily report, in work-date order", async () => {
    await seedDaily("2026-08-10", { weather: "Overcast, light rain", average_temp: 68 });
    await seedDaily("2026-08-08", { weather: "Clear", average_temp: 74 });
    const r = await body(await internal());
    expect(r.weather.days.map((d) => d.work_date)).toEqual(["2026-08-08", "2026-08-10"]);
    expect(r.weather.days[0]).toMatchObject({ conditions: "Clear", avg_temp: "74", inclement: false });
    expect(r.daily_report_count).toBe(2);
  });

  it("excludes a daily report that a later one amends", async () => {
    const first = await seedDaily("2026-08-08", { weather: "Wrong" }, { uuid: "d1" });
    await seedDaily("2026-08-08", { weather: "Corrected" }, { uuid: "d2", amendsUuid: first });
    const r = await body(await internal());
    expect(r.daily_report_count).toBe(1);
    expect(r.weather.days[0].conditions).toBe("Corrected");
  });

  it("excludes daily reports outside the work_date window", async () => {
    await seedDaily("2026-08-07", { weather: "Before" });
    await seedDaily("2026-08-15", { weather: "After" });
    await seedDaily("2026-08-08", { weather: "Inside" });
    const r = await body(await internal());
    expect(r.weather.days.map((d) => d.conditions)).toEqual(["Inside"]);
  });

  it("aggregates crews by PEAK headcount, not the sum across days", async () => {
    await seedDaily("2026-08-08", {
      crew_progress: [
        { crew_subcontractor: "Pro Panel", manpower: 9, todays_progress: "Racking rows 1-4" },
        { crew_subcontractor: "ESS Supervisor", manpower: 2, todays_progress: "Oversight" },
      ],
    });
    await seedDaily("2026-08-09", {
      crew_progress: [{ crew_subcontractor: "pro panel", manpower: 11, todays_progress: "Racking rows 5-9" }],
    });
    const r = await body(await internal());
    const proPanel = r.labor.crews.find((c) => c.company.toLowerCase() === "pro panel");
    // Same crew on two days is ONE crew of 11 at peak — not 20 people.
    expect(proPanel).toEqual({ company: "Pro Panel", workers: 11, days: 2 });
    expect(r.labor.crews).toHaveLength(2);
    expect(r.crew_progress).toHaveLength(3);
  });

  it("tolerates a daily report with no crew_progress array", async () => {
    await seedDaily("2026-08-08", { weather: "Clear" });
    await seedDaily("2026-08-09", { crew_progress: "not-an-array" });
    const r = await body(await internal());
    expect(r.labor.crews).toEqual([]);
    expect(r.daily_report_count).toBe(2);
  });

  it("sums labor hours amend-collapsed over the epoch window", async () => {
    await seedTime("t1", 8);
    await seedTime("t2", 10);
    await seedTime("t3", 12, "t2"); // t3 amends t2 → t2 drops out, t3 counts
    const r = await body(await internal());
    expect(r.labor.total_hours).toBe(8 + 12);
  });

  it("returns distinct safety-meeting form codes, not display names", async () => {
    await seedOtherForm("toolbox-talk-ppe-v1", "2026-08-08");
    await seedOtherForm("toolbox-talk-ppe-v1", "2026-08-09");
    await seedOtherForm("jha-v3", "2026-08-09");
    await seedOtherForm("incident-report-v3", "2026-08-10"); // not a meeting topic
    const r = await body(await internal());
    expect(r.hazard_form_codes).toEqual(["jha-v3", "toolbox-talk-ppe-v1"]);
  });

  it("returns deliveries with the catalog manufacturer as vendor", async () => {
    await env.DB.prepare(
      "INSERT INTO material_catalog (id, model_id, manufacturer, category) VALUES (1,'TT-1P','TerraSmart','tracker')",
    ).run();
    await env.DB.prepare(
      "INSERT INTO job_expected_materials (id, job_id, material_id, description, qty, unit, status, seq) VALUES (1,?,1,'Torque tube',100,'ea','received',1)",
    ).bind(JOB).run();
    await env.DB.prepare(
      "INSERT INTO material_receipt_events (event_uuid, line_id, job_id, kind, qty, event_date, actor) VALUES ('e1',1,?,'delivered',40,'2026-08-11','pm.one')",
    ).bind(JOB).run();
    // A 'not_delivered' mark is not a delivery and must not appear on the client's log.
    await env.DB.prepare(
      "INSERT INTO material_receipt_events (event_uuid, line_id, job_id, kind, qty, event_date, actor) VALUES ('e2',1,?,'not_delivered',0,'2026-08-12','pm.one')",
    ).bind(JOB).run();
    const r = await body(await internal());
    expect(r.deliveries).toHaveLength(1);
    expect(r.deliveries[0]).toMatchObject({
      event_date: "2026-08-11", item: "Torque tube", vendor: "TerraSmart", qty: "40",
    });
  });
});

// ── photos: the screening control, and the three-state contract ─────────────
describe("weekly report — photos", () => {
  it("offers ONLY clean, Box-filed photos — the screening control", async () => {
    const clean = await seedPhoto("2026-08-08", "clean", "box-111");
    await seedPhoto("2026-08-09", "pending", null);          // not screened yet
    await seedPhoto("2026-08-10", "refused", null);           // screened MALICIOUS
    await seedPhoto("2026-08-11", "clean", null);             // clean but not yet filed to Box
    const r = await body(await internal());
    expect(r.photos.available.map((p) => p.pool_id)).toEqual([clean]);
  });

  it("auto-selects a spread across days rather than the first N of one day", async () => {
    // Monday is busy; Tue/Wed have one each. A naive "first 8" would show only Monday.
    for (let i = 0; i < 10; i += 1) await seedPhoto("2026-08-08", "clean", `mon-${i}`);
    await seedPhoto("2026-08-09", "clean", "tue-0");
    await seedPhoto("2026-08-10", "clean", "wed-0");
    const r = await body(await internal());
    expect(r.photos.auto_selected).toBe(true);
    expect(r.photos.selected).toHaveLength(8);
    const days = new Set(r.photos.selected.map((p) => p.work_date));
    expect(days).toEqual(new Set(["2026-08-08", "2026-08-09", "2026-08-10"]));
    // Round-robin: each day's first photo precedes any day's second.
    expect(r.photos.selected.slice(0, 3).map((p) => p.box_file_id)).toEqual(["mon-0", "tue-0", "wed-0"]);
  });

  it("tolerates a claiming submission whose additional_photos is not an array", async () => {
    const uuid = await seedDaily("2026-08-08", { additional_photos: "not-an-array" }, { uuid: "bad-claimer" });
    await seedPhoto("2026-08-08", "clean", "box-bad", uuid);
    const res = await internal();
    expect(res.status).toBe(200);
    const r = await body(res);
    expect(r.photos.available).toHaveLength(1);
    expect(r.photos.available[0].caption).toBe("");
  });

  it("carries the caption from the claiming submission's additional_photos ref", async () => {
    const uuid = await seedDaily("2026-08-08", {
      additional_photos: [{ pool_id: 0, caption: "placeholder" }],
    }, { uuid: "claimer" });
    const poolId = await seedPhoto("2026-08-08", "clean", "box-cap", uuid);
    await env.DB.prepare("UPDATE submissions SET payload_json = ? WHERE submission_uuid = ?")
      .bind(JSON.stringify({ additional_photos: [{ pool_id: poolId, caption: "Pile driving, rows 12-18" }] }), uuid)
      .run();
    const r = await body(await internal());
    expect(r.photos.available[0].caption).toBe("Pile driving, rows 12-18");
  });
});

// ── office record: carry-forward and the three-state save ───────────────────
describe("weekly report — office inputs", () => {
  const save = async (cookie: string, payload: Record<string, unknown>): Promise<Response> =>
    call("/api/fieldops/weekly-report", {
      method: "PUT", cookie, body: JSON.stringify({ job_id: JOB, week_start: WEEK_START, ...payload }),
    });

  const adminCookie = (): Promise<string> => accountCookie("adm.two", "admin");

  it("returns empty, unsaved office values when nothing exists", async () => {
    const r = await body(await internal());
    expect(r.office.saved).toBe(false);
    expect(r.office.carried_from).toBeNull();
    expect(r.office.header.ess_management).toBe("");
    expect(r.office.safety.lost_time).toEqual({ month: 0, to_date: 0 });
    expect(r.office.photos).toBeNull();
  });

  it("saves and reads back, marked saved", async () => {
    const cookie = await adminCookie();
    const res = await save(cookie, {
      header: { ess_management: "Ben Finkhousen", subcontractors: ["Pro Panel"], mobilization_date: "2026-05-01" },
      safety: { near_miss: { month: 1, to_date: 3 } },
      pending: { rfis: "RFI-014 tracker embed depth" },
    });
    expect(res.status).toBe(200);
    const r = await body(await internal());
    expect(r.office.saved).toBe(true);
    expect(r.office.carried_from).toBeNull();
    expect(r.office.header).toMatchObject({
      ess_management: "Ben Finkhousen", subcontractors: ["Pro Panel"], mobilization_date: "2026-05-01",
    });
    expect(r.office.safety.near_miss).toEqual({ month: 1, to_date: 3 });
    expect(r.office.pending.rfis).toBe("RFI-014 tracker embed depth");
  });

  it("writes ONE audit row per save, atomic with the upsert", async () => {
    const cookie = await adminCookie();
    await save(cookie, { header: { ess_management: "A" } });
    await save(cookie, { header: { ess_management: "B" } });
    const rows = await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'weekly_report_inputs_save'",
    ).first<{ n: number }>();
    expect(rows!.n).toBe(2);
    // The upsert converged on one row rather than forking the week.
    const inputs = await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM job_weekly_report_inputs WHERE job_id = ?",
    ).bind(JOB).first<{ n: number }>();
    expect(inputs!.n).toBe(1);
  });

  it("carries the most recent EARLIER week forward, flagged, without writing", async () => {
    const cookie = await adminCookie();
    // Two prior weeks — the nearer one must win.
    await call("/api/fieldops/weekly-report", {
      method: "PUT", cookie,
      body: JSON.stringify({ job_id: JOB, week_start: "2026-07-25", header: { ess_management: "OLDER" } }),
    });
    await call("/api/fieldops/weekly-report", {
      method: "PUT", cookie,
      body: JSON.stringify({ job_id: JOB, week_start: "2026-08-01", header: { ess_management: "NEARER" }, safety: { first_aid: { month: 0, to_date: 1 } } }),
    });
    const r = await body(await internal());
    expect(r.office.carried_from).toBe("2026-08-01");
    expect(r.office.header.ess_management).toBe("NEARER");
    expect(r.office.safety.first_aid).toEqual({ month: 0, to_date: 1 });
    // Carried, NOT saved — the office has not reviewed this week yet.
    expect(r.office.saved).toBe(false);
    // And nothing was written on read.
    const n = await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM job_weekly_report_inputs WHERE job_id = ? AND week_start = ?",
    ).bind(JOB, WEEK_START).first<{ n: number }>();
    expect(n!.n).toBe(0);
  });

  it("never carries a LATER week backwards", async () => {
    const cookie = await adminCookie();
    await call("/api/fieldops/weekly-report", {
      method: "PUT", cookie,
      body: JSON.stringify({ job_id: JOB, week_start: "2026-08-15", header: { ess_management: "FUTURE" } }),
    });
    const r = await body(await internal());
    expect(r.office.carried_from).toBeNull();
    expect(r.office.header.ess_management).toBe("");
  });

  it("marks a day inclement only from the office's marked dates", async () => {
    await seedDaily("2026-08-08", { weather: "Clear" });
    await seedDaily("2026-08-09", { weather: "T-storms PM" });
    const before = await body(await internal());
    // Rain in the conditions text does NOT make a weather day — it is a contractual claim.
    expect(before.weather.days.every((d) => !d.inclement)).toBe(true);
    expect(before.weather.weather_days_week).toBe(0);

    const cookie = await adminCookie();
    await save(cookie, { weather: { inclement_dates: ["2026-08-09"], weather_days_to_date: 9 } });
    const after = await body(await internal());
    expect(after.weather.days.find((d) => d.work_date === "2026-08-09")!.inclement).toBe(true);
    expect(after.weather.weather_days_week).toBe(1);
    expect(after.weather.weather_days_to_date).toBe(9);
  });

  it("keeps the three-state photo contract across a save", async () => {
    await seedPhoto("2026-08-08", "clean", "box-a");
    await seedPhoto("2026-08-09", "clean", "box-b");
    const cookie = await adminCookie();

    // 1. absent key → auto-select stays in force
    await save(cookie, { header: { ess_management: "X" } });
    let r = await body(await internal());
    expect(r.photos.auto_selected).toBe(true);
    expect(r.photos.selected).toHaveLength(2);

    // 2. explicit empty list → NO photos, and it must not re-populate
    await save(cookie, { photos: [] });
    r = await body(await internal());
    expect(r.photos.auto_selected).toBe(false);
    expect(r.photos.selected).toEqual([]);

    // 3. explicit picks → exactly those, in order
    await save(cookie, {
      photos: [{ pool_id: 2, box_file_id: "box-b", caption: "Torque tube splice", work_date: "2026-08-09" }],
    });
    r = await body(await internal());
    expect(r.photos.auto_selected).toBe(false);
    expect(r.photos.selected).toHaveLength(1);
    expect(r.photos.selected[0]).toMatchObject({ box_file_id: "box-b", caption: "Torque tube splice" });
  });

  it("rejects a malformed photos value rather than silently reverting to auto-select", async () => {
    await seedPhoto("2026-08-08", "clean", "box-a");
    const cookie = await adminCookie();
    // First clear the page explicitly...
    expect((await save(cookie, { photos: [] })).status).toBe(200);
    // ...then send junk. Degrading that to null would mean "auto-select" and would silently
    // re-populate the photo page the office had just cleared.
    expect((await save(cookie, { photos: "junk" })).status).toBe(400);
    const r = await body(await internal());
    expect(r.photos.auto_selected).toBe(false);
    expect(r.photos.selected).toEqual([]);
  });

  it("rejects a save for an unknown job, and a malformed body", async () => {
    const cookie = await adminCookie();
    const unknown = await call("/api/fieldops/weekly-report", {
      method: "PUT", cookie, body: JSON.stringify({ job_id: "NOPE", week_start: WEEK_START }),
    });
    expect(unknown.status).toBe(404);
    const badWeek = await save(cookie, { week_start: "nope" });
    expect(badWeek.status).toBe(400);
    const notObject = await call("/api/fieldops/weekly-report", {
      method: "PUT", cookie, body: JSON.stringify([1, 2, 3]),
    });
    expect(notObject.status).toBe(400);
  });

  it("rejects a save from a caller without cap.jobtracker.manage", async () => {
    const cookie = await accountCookie("sub.two", "submitter");
    expect((await save(cookie, { header: { ess_management: "nope" } })).status).toBe(403);
  });

  it("clamps hostile office input to the documented bounds instead of storing it raw", async () => {
    const cookie = await adminCookie();
    await save(cookie, {
      header: { ess_management: "x".repeat(9000), subcontractors: Array.from({ length: 200 }, (_, i) => `S${i}`), mobilization_date: "not-a-date" },
      safety: { lost_time: { month: -5, to_date: "12" } },
      weather: { inclement_dates: ["2026-08-09", "garbage", "2026-08-09"] },
    });
    const r = await body(await internal());
    expect(r.office.header.ess_management).toHaveLength(4000);
    expect(r.office.header.subcontractors).toHaveLength(60);
    // A malformed mobilization date is dropped, never rendered as a fake one.
    expect(r.office.header.mobilization_date).toBe("");
    // A negative OSHA count is not a count.
    expect(r.office.safety.lost_time).toEqual({ month: 0, to_date: 12 });
    // Non-dates dropped, duplicates collapsed.
    expect(r.office.weather.inclement_dates).toEqual(["2026-08-09"]);
  });
});

// ── the schedule seam stays honestly empty until the ADR-0006 lane lands ────
describe("weekly report — schedule seam", () => {
  it("returns schedule: null so the renderer prints an honest empty state", async () => {
    const r = await body(await internal());
    expect(r.schedule).toBeNull();
    // And no fabricated percentage rides along under another name.
    expect(JSON.stringify(r)).not.toMatch(/percent_done|progress_pct/);
  });
});
