import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, get, provision, login, seedJob } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// GET /api/fieldops/portfolio (A6) — the tracker list's cross-job strip. Under test:
// cap tier (any signed-in read role); ACTIVE jobs only; the delivery window INCLUDING
// overdue-undelivered; the 14-day milestone-risk rule; the materials due leg; signal-less
// jobs absent; and the LATE-predicate parity with the weekly report's behind list (the
// one-predicate discipline in worker/schedule_rollup.ts).
// ─────────────────────────────────────────────────────────────────────────────

const TODAY = new Date().toISOString().slice(0, 10);
function daysFromToday(n: number): string {
  const d = new Date(`${TODAY}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

async function seedTask(
  jobId: string, name: string,
  over: { percent?: number; finish?: string | null; milestone?: number; delivery?: number; delivered?: string | null; active?: number } = {},
): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO job_schedule_tasks (task_uuid, job_id, name, match_key, finish_date, percent_done, is_milestone, is_delivery, delivered_date, sort_order, active) " +
      "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 10, ?10)",
  ).bind(
    crypto.randomUUID(), jobId, name, name.toLowerCase(),
    over.finish ?? null, over.percent ?? 0, over.milestone ?? 0,
    over.delivery ?? 0, over.delivered ?? null, over.active ?? 1,
  ).run();
}

async function seedMaterial(jobId: string, expectedDate: string, status = "expected"): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO job_expected_materials (job_id, description, qty, unit, status, seq, expected_date, active) VALUES (?1, 'Torque tube', 10, 'ea', ?3, 1, ?2, 1)",
  ).bind(jobId, expectedDate, status).run();
}

let sub: string;

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_schedule_tasks"),
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
  await provision("sub.port", "password123", "submitter");
  sub = await login("sub.port", "password123");
});

describe("GET /api/fieldops/portfolio", () => {
  it("rides the read tier (submitter OK; anonymous 401)", async () => {
    expect((await call("/api/fieldops/portfolio")).status).toBe(401);
    expect((await get(sub, "/api/fieldops/portfolio")).status).toBe(200);
  });

  it("rolls active jobs only, with the delivery + milestone windows and the materials leg", async () => {
    await seedJob("JOB-ACT");
    await seedJob("JOB-CLOSED", { status: "closed" });
    // Late task (parity case) + a delivery OVERDUE-undelivered + one due in-window + one delivered.
    await seedTask("JOB-ACT", "Late trench", { finish: daysFromToday(-3), percent: 50 });
    await seedTask("JOB-ACT", "Overdue delivery", { delivery: 1, finish: daysFromToday(-2) });
    await seedTask("JOB-ACT", "Inweek delivery", { delivery: 1, finish: daysFromToday(3) });
    await seedTask("JOB-ACT", "Done delivery", { delivery: 1, finish: daysFromToday(2), delivered: daysFromToday(-1) });
    // Milestones: at-risk inside 14d, one PAST (also at risk), one far future (not), one done.
    await seedTask("JOB-ACT", "M soon", { milestone: 1, finish: daysFromToday(10) });
    await seedTask("JOB-ACT", "M past", { milestone: 1, finish: daysFromToday(-30) });
    await seedTask("JOB-ACT", "M far", { milestone: 1, finish: daysFromToday(60) });
    await seedTask("JOB-ACT", "M done", { milestone: 1, percent: 100, finish: daysFromToday(5) });
    // The closed job's task must not appear at all.
    await seedTask("JOB-CLOSED", "Ghost", { finish: daysFromToday(-5) });
    // Materials: one due in-window, one already received, one undated.
    await seedMaterial("JOB-ACT", daysFromToday(4));
    await seedMaterial("JOB-ACT", daysFromToday(2), "received");

    const res = await get(sub, "/api/fieldops/portfolio");
    const body = (await res.json()) as { jobs: Record<string, unknown>[] };
    expect(body.jobs).toHaveLength(1);
    expect(body.jobs[0]).toMatchObject({
      job_id: "JOB-ACT",
      late_count: 3, // Late trench + Overdue delivery + M past (all finish<today, <100%)
      deliveries_due: 2, // overdue-undelivered + in-week; the delivered one excluded
      milestones_at_risk: 2, // M soon + M past; far/done excluded
      materials_due: 1,
    });
  });

  it("late-predicate PARITY: the strip's late count equals the weekly report's behind list", async () => {
    await seedJob("JOB-PAR");
    await seedTask("JOB-PAR", "Behind A", { finish: daysFromToday(-1), percent: 10 });
    await seedTask("JOB-PAR", "Behind B", { finish: daysFromToday(-9), percent: 99 });
    await seedTask("JOB-PAR", "Fine", { finish: daysFromToday(9), percent: 0 });
    const strip = (await (await get(sub, "/api/fieldops/portfolio")).json()) as { jobs: { late_count: number }[] };

    await provision("adm.par", "password123", "admin");
    const admin = await login("adm.par", "password123");
    const from = Math.floor(Date.now() / 1000) - 7 * 86400;
    const to = Math.floor(Date.now() / 1000) + 86400;
    const rep = await get(
      admin,
      `/api/fieldops/weekly-report?job_id=JOB-PAR&week_start=${daysFromToday(-6)}&week_end=${TODAY}&from=${from}&to=${to}`,
    );
    const repBody = (await rep.json()) as { schedule: { behind: unknown[] } | null };
    expect(strip.jobs[0].late_count).toBe(repBody.schedule!.behind.length);
  });

  it("omits signal-less jobs entirely", async () => {
    await seedJob("JOB-QUIET");
    const res = await get(sub, "/api/fieldops/portfolio");
    expect(((await res.json()) as { jobs: unknown[] }).jobs).toEqual([]);
  });
});
