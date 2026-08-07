import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, p as j, seedJob as seedJobRow } from "./helpers";

// ROADMAP Track 6 — the daemon's queue + commit point.
//
//   GET  /api/internal/fieldops/archive-pending
//   POST /api/internal/fieldops/job-archive-progress
//
// The property under test throughout is FORWARD-ONLY. The daemon runs on a separate host on its
// own cycle, so a post can arrive late, twice, or after the operator has changed their mind — and
// none of those may resurrect a finished archive or apply a stale result to a reversed one.

const TOKEN = "test-fieldops-token";
const NAME = "Bradley Solar - Block C";
let admin: string;

async function createOk(cookie: string, projectName: string): Promise<string> {
  const res = await j(cookie, "/api/fieldops/job", { project_name: projectName, safety_cc: ["cc@x.com"] });
  expect(res.status, await res.clone().text()).toBe(201);
  return ((await res.json()) as { job_id: string }).job_id;
}
async function jobRow(jobId: string) {
  return await env.DB.prepare("SELECT * FROM jobs WHERE job_id=?").bind(jobId).first<any>();
}
/** Raise a real archive request through the browser route, as the daemon would find it. */
async function request(jobId: string, name: string) {
  expect((await j(admin, `/api/fieldops/job/${jobId}/archive`, { confirm: name })).status).toBe(200);
}
function post(body: unknown, token = TOKEN) {
  return call("/api/internal/fieldops/job-archive-progress", {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}
function pending(token = TOKEN) {
  return call("/api/internal/fieldops/archive-pending", { headers: { authorization: `Bearer ${token}` } });
}

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS job_counter (id INTEGER PRIMARY KEY CHECK (id = 1), last_value INTEGER NOT NULL)"),
    env.DB.prepare("INSERT OR REPLACE INTO job_counter (id, last_value) VALUES (1, 16)"),
  ]);
  await provision("admin.one", "password123", "admin");
  admin = await login("admin.one", "password123");
});

describe("archive-pending — the queue", () => {
  it("is bearer-gated: absent and wrong tokens both 401", async () => {
    expect((await call("/api/internal/fieldops/archive-pending")).status).toBe(401);
    expect((await pending("nope")).status).toBe(401);
    expect((await pending()).status).toBe(200);
  });

  it("serves ONLY jobs awaiting relocation, oldest request first", async () => {
    const a = await createOk(admin, "Alpha");
    const b = await createOk(admin, "Bravo");
    const idle = await createOk(admin, "Idle");
    await request(a, "Alpha");
    await request(b, "Bravo");
    // Force a deterministic order — both requests land in the same epoch second otherwise.
    await env.DB.prepare("UPDATE jobs SET archive_requested_at=100 WHERE job_id=?").bind(a).run();
    await env.DB.prepare("UPDATE jobs SET archive_requested_at=200 WHERE job_id=?").bind(b).run();

    const out = (await (await pending()).json()) as { jobs: { job_id: string }[] };
    expect(out.jobs.map((r) => r.job_id)).toEqual([a, b]);
    expect(out.jobs.map((r) => r.job_id)).not.toContain(idle);
  });

  it("keeps re-serving a job until it reaches a TERMINAL state", async () => {
    // The whole reason this is a dedicated queue: the pre-Track-6 move rode the job-dirty list,
    // which an unrelated mirror success cleared — so a failed move never retried.
    const id = await createOk(admin, NAME);
    await request(id, NAME);

    await post({ updates: [{ job_id: id, direction: "archive", state: "in_progress" }] });
    let out = (await (await pending()).json()) as { jobs: { job_id: string }[] };
    expect(out.jobs.map((r) => r.job_id)).toContain(id);

    await post({ updates: [{ job_id: id, direction: "archive", state: "partial" }] });
    out = (await (await pending()).json()) as { jobs: { job_id: string }[] };
    expect(out.jobs.map((r) => r.job_id)).not.toContain(id); // partial is terminal for the pass

    // ...and the operator's retry puts it straight back on the queue.
    await j(admin, `/api/fieldops/job/${id}/archive`, { confirm: NAME });
    out = (await (await pending()).json()) as { jobs: { job_id: string }[] };
    expect(out.jobs.map((r) => r.job_id)).toContain(id);
  });

  it("never serves a smartsheet-origin job", async () => {
    await seedJobRow("SS-9", { status: "active", projectName: "Legacy" });
    await env.DB
      .prepare("UPDATE jobs SET archive_state='requested', archive_direction='archive' WHERE job_id='SS-9'")
      .run();
    const out = (await (await pending()).json()) as { jobs: { job_id: string }[] };
    expect(out.jobs.map((r) => r.job_id)).not.toContain("SS-9");
  });
});

describe("job-archive-progress — validation", () => {
  it("is bearer-gated", async () => {
    expect((await post({ updates: [] }, "nope")).status).toBe(401);
  });

  it("rejects malformed batches before executing ANY of them", async () => {
    const id = await createOk(admin, NAME);
    await request(id, NAME);

    expect((await post({})).status).toBe(400);
    expect((await post({ updates: "x" })).status).toBe(400);
    expect((await post({ updates: [] })).status).toBe(400);
    expect((await post({ updates: Array(26).fill({ job_id: id, direction: "archive", state: "complete" }) })).status).toBe(413);

    // A malformed member AFTER a valid one must discard the whole batch — validate-all-then-execute.
    const res = await post({
      updates: [
        { job_id: id, direction: "archive", state: "complete" },
        { job_id: id, direction: "archive", state: "nonsense" },
      ],
    });
    expect(res.status).toBe(400);
    expect((await jobRow(id)).archive_state).toBe("requested"); // the VALID member did not land
  });

  it("refuses 'requested' — the daemon may advance a request, never raise one", async () => {
    const id = await createOk(admin, NAME);
    await request(id, NAME);
    const res = await post({ updates: [{ job_id: id, direction: "archive", state: "requested" }] });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("invalid_archive_state");
  });

  it("bounds the container report", async () => {
    const id = await createOk(admin, NAME);
    await request(id, NAME);
    const huge = Array.from({ length: 500 }, (_, i) => ({ key: `k${i}`, note: "x".repeat(50) }));
    expect((await post({ updates: [{ job_id: id, direction: "archive", state: "partial", containers: huge }] })).status).toBe(400);
  });
});

describe("job-archive-progress — forward-only", () => {
  it("advances requested to in_progress to complete, stamping completed_at once", async () => {
    const id = await createOk(admin, NAME);
    await request(id, NAME);

    await post({ updates: [{ job_id: id, direction: "archive", state: "in_progress" }] });
    expect((await jobRow(id)).archive_state).toBe("in_progress");

    const containers = [{ key: "smartsheet:safety", label: "Safety folder", moved: true, note: "" }];
    await post({ updates: [{ job_id: id, direction: "archive", state: "complete", containers }] });

    const row = await jobRow(id);
    expect(row.archive_state).toBe("complete");
    expect(row.archive_completed_at).toBeGreaterThan(0);
    expect(JSON.parse(row.archive_detail)).toEqual(containers);
  });

  it("counts attempts on partial/failed only, so the pass can stop retrying a wedged job", async () => {
    const id = await createOk(admin, NAME);
    await request(id, NAME);

    await post({ updates: [{ job_id: id, direction: "archive", state: "in_progress" }] });
    expect((await jobRow(id)).archive_attempts).toBe(0); // progress is not a failure

    await post({ updates: [{ job_id: id, direction: "archive", state: "partial" }] });
    expect((await jobRow(id)).archive_attempts).toBe(1);
  });

  it("a REPLAYED post cannot resurrect a completed archive", async () => {
    const id = await createOk(admin, NAME);
    await request(id, NAME);
    await post({ updates: [{ job_id: id, direction: "archive", state: "complete" }] });

    // The same message arrives again a cycle later (retry, duplicate delivery, slow host).
    const res = await post({ updates: [{ job_id: id, direction: "archive", state: "in_progress" }] });
    expect(res.status).toBe(200);
    const out = (await res.json()) as { updated: number; skipped: string[] };
    expect(out.updated).toBe(0);
    expect(out.skipped).toEqual([id]);
    expect((await jobRow(id)).archive_state).toBe("complete"); // unmoved
  });

  it("an ARCHIVE result cannot be applied to a job the operator has flipped to UN-archive", async () => {
    // The sharpest ordering hazard: the daemon posts a stale archive result while the operator has
    // already reversed course. The direction predicate is what refuses it.
    const id = await createOk(admin, NAME);
    await request(id, NAME);
    await post({ updates: [{ job_id: id, direction: "archive", state: "complete" }] });
    expect((await j(admin, `/api/fieldops/job/${id}/unarchive`, { confirm: NAME })).status).toBe(200);

    const res = await post({ updates: [{ job_id: id, direction: "archive", state: "failed" }] });
    const out = (await res.json()) as { skipped: string[] };
    expect(out.skipped).toEqual([id]);
    const row = await jobRow(id);
    expect(row.archive_direction).toBe("unarchive"); // the operator's intent stands
    expect(row.archive_state).toBe("requested");
  });

  it("one stale member never discards the genuine results beside it", async () => {
    const good = await createOk(admin, "Good Job");
    const stale = await createOk(admin, "Stale Job");
    await request(good, "Good Job");
    await request(stale, "Stale Job");
    await post({ updates: [{ job_id: stale, direction: "archive", state: "complete" }] }); // now terminal

    const res = await post({
      updates: [
        { job_id: good, direction: "archive", state: "complete" },
        { job_id: stale, direction: "archive", state: "in_progress" }, // stale — will not match
      ],
    });
    const out = (await res.json()) as { updated: number; skipped: string[] };
    expect(out.updated).toBe(1);
    expect(out.skipped).toEqual([stale]);
    expect((await jobRow(good)).archive_state).toBe("complete");
  });

  it("a COMPLETED un-archive resets the record to neutral, making the job ordinary again", async () => {
    const id = await createOk(admin, NAME);
    await request(id, NAME);
    await post({ updates: [{ job_id: id, direction: "archive", state: "complete" }] });
    await j(admin, `/api/fieldops/job/${id}/unarchive`, { confirm: NAME });

    await post({ updates: [{ job_id: id, direction: "unarchive", state: "complete" }] });

    const row = await jobRow(id);
    // Back to an ordinary job: the audit_log keeps the history, and prune.ts's archive fence
    // (archive_state = 'none') will let it be swept again once it is genuinely empty.
    expect(row.archive_state).toBe("none");
    expect(row.archive_direction).toBe("");
    expect(row.archive_requested_at).toBeNull();
    expect(row.archive_completed_at).toBeNull();
    expect(row.archive_attempts).toBe(0);
    expect(row.archive_detail).toBe("");
    expect(row.archive_folder_key).toBe("");
    expect(row.lifecycle).toBe("inactive"); // un-archive never re-opens a job
  });

  it("writes exactly ONE summary audit row per batch", async () => {
    const a = await createOk(admin, "Alpha");
    const b = await createOk(admin, "Bravo");
    await request(a, "Alpha");
    await request(b, "Bravo");

    await post({
      updates: [
        { job_id: a, direction: "archive", state: "in_progress" },
        { job_id: b, direction: "archive", state: "in_progress" },
      ],
    });

    const rows = (await env.DB.prepare("SELECT * FROM audit_log WHERE action='job_archive_progress'").all()).results as any[];
    expect(rows.length).toBe(1);
    expect(JSON.parse(rows[0].detail).count).toBe(2);
    expect(rows[0].actor_username).toBe("system:fieldops_sync");
  });
});
