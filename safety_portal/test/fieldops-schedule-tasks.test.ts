import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, g, p, seedJob, seedPersonnel } from "./helpers";
import { scheduleMatchKey } from "../worker/schedule_normalize";
import { pacificDateString } from "../worker/fieldops_recurrence";

// ─────────────────────────────────────────────────────────────────────────────
// Living schedule task list + degenerate plan/commit (ADR-0006 PR-4) —
// worker/fieldops_schedule_tasks.ts + the plan/commit additions in
// worker/fieldops_schedules.ts, over migration 0071.
//
// Real workerd + D1 (migrations auto-applied), no route mocks. The properties under
// test are the ones unit tests structurally cannot reach: the read/manage role split
// (decision 4), W4 mutation+audit atomicity, the 0066 watermark commit protocol
// (replay idempotence, paging, the two-uploads serialization, the honest 409 on a
// guard-tripped statement 0), baseline immutability at first commit, and the PR-4
// degenerate boundary (revision_reconcile_not_available).
// ─────────────────────────────────────────────────────────────────────────────

const SCHEDULE_BEARER = "test-schedule-token";
const PDF_MIME = "application/pdf";

function b64(head: number[], tail = 0x41, tailLen = 16): string {
  const bytes = new Uint8Array(head.length + tailLen);
  bytes.set(head, 0);
  for (let i = head.length; i < bytes.length; i++) bytes[i] = tail;
  let bin = "";
  for (const byte of bytes) bin += String.fromCharCode(byte);
  return btoa(bin);
}
const PDF_HEAD = [0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37]; // "%PDF-1.7"
const PDF_B64 = b64(PDF_HEAD);
const PDF_B64_OTHER = b64(PDF_HEAD, 0x42);

let admin = "";
let manager = "";
let sub = "";

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_schedule_tasks"),
    env.DB.prepare("DELETE FROM job_schedule_chunks"),
    env.DB.prepare("DELETE FROM job_schedule_rows"),
    env.DB.prepare("DELETE FROM job_schedule_previews"),
    env.DB.prepare("DELETE FROM job_schedules"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
  await provision("admin.one", "pw-admin-1234", "admin");
  await provision("mgr.mike", "pw-manager-12", "manager");
  await provision("sub.sam", "pw-subby-1234", "submitter");
  admin = await login("admin.one", "pw-admin-1234");
  manager = await login("mgr.mike", "pw-manager-12");
  sub = await login("sub.sam", "pw-subby-1234");
  await seedJob("JOB-A", { projectName: "Deep Lake" });
  await seedJob("JOB-B");
});

/** An uploaded pool row moved straight to `parsed` (the daemon's servicing is exercised by
 *  fieldops-schedules.test.ts — here the pool row is just the commit's substrate). */
async function parsedSchedule(jobId: string, data = PDF_B64, filename = "Project Schedule.pdf"): Promise<number> {
  const res = await p(admin, "/api/fieldops/schedules", {
    job_id: jobId, filename, mime: PDF_MIME, data_b64: data,
  });
  expect(res.status, await res.clone().text()).toBe(201);
  const id = ((await res.json()) as { id: number }).id;
  await env.DB.prepare("UPDATE job_schedules SET status='parsed' WHERE id = ?1").bind(id).run();
  return id;
}

function taskRow(i: number, over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    source_row_index: i,
    kind: "data",
    name: `Task ${i}`,
    section: "Civil",
    duration_days: 5,
    start_date: "2026-09-01",
    finish_date: "2026-09-05",
    percent_done: 25,
    ...over,
  };
}

async function addTask(body: Record<string, unknown>, cookie = admin): Promise<Response> {
  return p(cookie, "/api/fieldops/schedule-tasks", { job_id: "JOB-A", name: "Fencing", ...body });
}

describe("task read (cap.jobtracker.read — ALL roles view, decision 4)", () => {
  it("submitter CAN read; rows arrive in sort order with the job's display name; W9 names only", async () => {
    await seedPersonnel("Mo Manager", "mgr.mike");
    await env.DB.batch([
      env.DB
        .prepare(
          "INSERT INTO job_schedule_tasks (task_uuid, job_id, section, name, match_key, sort_order, percent_done, last_marked_by, last_marked_at) " +
            "VALUES ('tu-2','JOB-A','Civil','Fencing','civil\nfencing',20,50,'mgr.mike',1780000100)",
        ),
      env.DB
        .prepare(
          "INSERT INTO job_schedule_tasks (task_uuid, job_id, section, name, match_key, sort_order) " +
            "VALUES ('tu-1','JOB-A','Deliveries','Pile Delivery','deliveries\npile delivery',10)",
        ),
      // Inactive rows never serve; other jobs never leak.
      env.DB
        .prepare(
          "INSERT INTO job_schedule_tasks (task_uuid, job_id, name, match_key, sort_order, active) " +
            "VALUES ('tu-gone','JOB-A','Removed','\nremoved',5,0)",
        ),
      env.DB
        .prepare(
          "INSERT INTO job_schedule_tasks (task_uuid, job_id, name, match_key, sort_order) " +
            "VALUES ('tu-b','JOB-B','Other job','\nother job',10)",
        ),
    ]);
    const res = await g(sub, "/api/fieldops/schedule-tasks?job_id=JOB-A");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as {
      tasks: Record<string, unknown>[];
      project_name: string | null;
    };
    expect(body.project_name).toBe("Deep Lake");
    expect(body.tasks.map((t) => t.task_uuid)).toEqual(["tu-1", "tu-2"]);
    // W9: the display NAME, never the account username; match_key never serves.
    expect(body.tasks[1].last_marked_by_name).toBe("Mo Manager");
    expect(body.tasks[1]).not.toHaveProperty("last_marked_by");
    expect(body.tasks[1]).not.toHaveProperty("match_key");
  });

  it("unknown job → 404; blank job_id → 400", async () => {
    expect((await g(admin, "/api/fieldops/schedule-tasks?job_id=JOB-NOPE")).status).toBe(404);
    expect((await g(admin, "/api/fieldops/schedule-tasks?job_id=")).status).toBe(400);
  });
});

describe("manual add / edit / deactivate (cap.jobtracker.manage — admin only)", () => {
  it("403 for submitter AND manager on add, edit and deactivate (0023 withholds the cap)", async () => {
    const addRes = await addTask({});
    const id = ((await addRes.json()) as { id: number }).id;
    expect((await addTask({}, sub)).status).toBe(403);
    expect((await addTask({}, manager)).status).toBe(403);
    expect((await p(sub, `/api/fieldops/schedule-tasks/${id}/edit`, { name: "X" })).status).toBe(403);
    expect((await p(manager, `/api/fieldops/schedule-tasks/${id}/edit`, { name: "X" })).status).toBe(403);
    expect((await p(manager, `/api/fieldops/schedule-tasks/${id}/deactivate`)).status).toBe(403);
  });

  it("add mints task_uuid + computes match_key server-side + audits; sort_order defaults to end-of-list", async () => {
    await addTask({ section: "  Site Work ", name: "Fencing 2" });
    const row = await env.DB
      .prepare("SELECT task_uuid, match_key, section, name, sort_order, percent_done FROM job_schedule_tasks")
      .first<Record<string, unknown>>();
    expect(String(row!.task_uuid)).toMatch(/^[0-9a-f-]{36}$/);
    expect(row!.match_key).toBe(scheduleMatchKey("Site Work", "Fencing 2"));
    expect(row!.match_key).toBe("site work\nfencing 2"); // trailing numeral PRESERVED
    expect(row!.sort_order).toBe(10);
    expect(row!.percent_done).toBe(0);
    const second = await addTask({ name: "Gate" });
    expect(second.status).toBe(201);
    const row2 = await env.DB
      .prepare("SELECT sort_order FROM job_schedule_tasks WHERE name='Gate'")
      .first<{ sort_order: number }>();
    expect(row2!.sort_order).toBe(20);
    const audit = await env.DB
      .prepare("SELECT actor_username FROM audit_log WHERE action='schedule_task_create'")
      .first<{ actor_username: string }>();
    expect(audit).toMatchObject({ actor_username: "admin.one" });
  });

  it("edit full-replaces + RECOMPUTES match_key + audits; a PERCENT edit does NOT stamp last_marked_by (PR-5 semantics)", async () => {
    const res = await addTask({ section: "Civil", name: "Fencing", percent_done: 10 });
    const id = ((await res.json()) as { id: number }).id;
    const edit = await p(admin, `/api/fieldops/schedule-tasks/${id}/edit`, {
      name: "Fence Install", section: "Civil", percent_done: 60, is_milestone: true,
    });
    expect(edit.status, await edit.clone().text()).toBe(200);
    const row = await env.DB
      .prepare(
        "SELECT name, match_key, percent_done, is_milestone, last_marked_by, last_marked_at FROM job_schedule_tasks WHERE id = ?1",
      )
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ name: "Fence Install", percent_done: 60, is_milestone: 1 });
    expect(row!.match_key).toBe(scheduleMatchKey("Civil", "Fence Install"));
    // The reconcile %-conflict predicate: last_marked_by IS NOT NULL ⇔ a FIELD mark.
    // An office percent edit is curation, so both stay NULL.
    expect(row!.last_marked_by).toBeNull();
    expect(row!.last_marked_at).toBeNull();
    const audit = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='schedule_task_update'")
      .first<{ n: number }>();
    expect(audit!.n).toBe(1);
  });

  it("deactivate is soft + idempotent: second call 200 already_inactive, ONE audit row, read excludes it", async () => {
    const res = await addTask({});
    const id = ((await res.json()) as { id: number }).id;
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/deactivate`)).status).toBe(200);
    const again = await p(admin, `/api/fieldops/schedule-tasks/${id}/deactivate`);
    expect(await again.json()).toMatchObject({ ok: true, already_inactive: true });
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='schedule_task_deactivate'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(1);
    const read = (await (await g(admin, "/api/fieldops/schedule-tasks?job_id=JOB-A")).json()) as {
      tasks: unknown[];
    };
    expect(read.tasks).toHaveLength(0);
    expect((await p(admin, "/api/fieldops/schedule-tasks/999999/deactivate")).status).toBe(404);
  });

  it("bounds: bad name / date / percent / duration / predecessors each refuse with their own code", async () => {
    for (const [body, code] of [
      [{ name: "" }, "invalid_task_name"],
      [{ name: "x".repeat(301) }, "invalid_task_name"],
      [{ section: "x".repeat(121) }, "invalid_task_section"],
      [{ start_date: "09/01/2026" }, "invalid_task_date"],
      [{ percent_done: 101 }, "invalid_task_percent"],
      [{ percent_done: 12.5 }, "invalid_task_percent"],
      [{ duration_days: 5001 }, "invalid_task_duration"],
      [{ predecessors_raw: "x".repeat(201) }, "invalid_task_predecessors"],
      [{ is_milestone: "yes" }, "invalid_task_flag"],
    ] as const) {
      const res = await addTask(body as Record<string, unknown>);
      expect(res.status, JSON.stringify(body)).toBe(400);
      expect(await res.json()).toMatchObject({ error: code });
    }
  });
});

describe("degenerate plan (POST /api/fieldops/schedules/:id/plan)", () => {
  it("no existing tasks → degenerate:true, every data row classifies new; section rows are context, not counted", async () => {
    const id = await parsedSchedule("JOB-A");
    const res = await p(admin, `/api/fieldops/schedules/${id}/plan`, {
      rows: [
        { source_row_index: 1, kind: "section", name: "Civil" },
        taskRow(2), taskRow(3),
      ],
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await res.json()).toMatchObject({
      ok: true, degenerate: true, revision_reconcile_available: false,
      counts: { incoming: 2, new: 2, existing: 0 },
    });
  });

  it("an existing task list → 200 degenerate:false (a read must NOT 409); manager/submitter 403", async () => {
    const id = await parsedSchedule("JOB-A");
    await addTask({ name: "Pre-existing" });
    const res = await p(admin, `/api/fieldops/schedules/${id}/plan`, { rows: [taskRow(1)] });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({
      ok: true, degenerate: false, revision_reconcile_available: false,
      counts: { incoming: 1, new: 0, existing: 1 },
    });
    expect((await p(manager, `/api/fieldops/schedules/${id}/plan`, { rows: [taskRow(1)] })).status).toBe(403);
    expect((await p(sub, `/api/fieldops/schedules/${id}/plan`, { rows: [taskRow(1)] })).status).toBe(403);
  });
});

describe("degenerate commit (POST /api/fieldops/schedules/:id/commit)", () => {
  it("happy path: tasks created with baselines == dates, schedule_percent == percent_done, minted uuids; pool row parsed→committed; audited", async () => {
    const id = await parsedSchedule("JOB-A");
    const res = await p(admin, `/api/fieldops/schedules/${id}/commit`, {
      rows: [
        { source_row_index: 1, kind: "section", name: "Civil" },
        taskRow(2, { predecessors_raw: "1FS +10d" }),
        taskRow(3, { name: "Pile Delivery", section: "Deliveries", is_delivery: true, percent_done: 0 }),
      ],
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await res.json()).toMatchObject({ ok: true, done: true, inserted: 2, committed_through_row: 3 });

    const tasks = await env.DB
      .prepare(
        "SELECT task_uuid, name, section, match_key, start_date, finish_date, baseline_start_date, " +
          "baseline_finish_date, percent_done, schedule_percent, is_delivery, predecessors_raw, " +
          "sort_order, source_row_index, source_schedule_id, last_marked_by " +
          "FROM job_schedule_tasks WHERE job_id='JOB-A' ORDER BY sort_order",
      )
      .all<Record<string, unknown>>();
    expect(tasks.results).toHaveLength(2); // the section row was context, never a task
    const [t1, t2] = tasks.results!;
    expect(t1).toMatchObject({
      name: "Task 2", section: "Civil",
      start_date: "2026-09-01", finish_date: "2026-09-05",
      baseline_start_date: "2026-09-01", baseline_finish_date: "2026-09-05",
      percent_done: 25, schedule_percent: 25,
      predecessors_raw: "1FS +10d", sort_order: 10, source_row_index: 2, source_schedule_id: id,
    });
    expect(t1.last_marked_by).toBeNull(); // commits never field-mark
    expect(String(t1.task_uuid)).toMatch(/^[0-9a-f-]{36}$/);
    expect(t1.match_key).toBe(scheduleMatchKey("Civil", "Task 2"));
    expect(t2).toMatchObject({
      name: "Pile Delivery", section: "Deliveries", is_delivery: 1,
      percent_done: 0, schedule_percent: 0, sort_order: 20, source_row_index: 3,
    });

    const pool = await env.DB
      .prepare("SELECT status, committed_at, committed_through_row FROM job_schedules WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(pool).toMatchObject({ status: "committed", committed_through_row: 3 });
    expect(pool!.committed_at).not.toBeNull();

    const imports = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='job_schedule_task_import'")
      .first<{ n: number }>();
    expect(imports!.n).toBe(2);
    const committed = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='job_schedule_commit'")
      .first<{ n: number }>();
    expect(committed!.n).toBe(1);
  });

  it("REFUSES 409 revision_reconcile_not_available when the job holds any active task this schedule didn't author", async () => {
    const id = await parsedSchedule("JOB-A");
    await addTask({ name: "Pre-existing" });
    const res = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows: [taskRow(1)] });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "revision_reconcile_not_available" });
    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_schedule_tasks WHERE job_id='JOB-A'")
      .first<{ n: number }>();
    expect(n!.n).toBe(1); // nothing landed
    // …and a DEACTIVATED pre-existing task no longer blocks (the guard is active=1).
    const pre = await env.DB
      .prepare("SELECT id FROM job_schedule_tasks WHERE name='Pre-existing'")
      .first<{ id: number }>();
    await p(admin, `/api/fieldops/schedule-tasks/${pre!.id}/deactivate`);
    expect((await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows: [taskRow(1)] })).status).toBe(200);
  });

  it("a replayed final page is an idempotent no-op — done:true, inserted:0, no duplicate tasks (the lost-response retry)", async () => {
    const id = await parsedSchedule("JOB-A");
    const rows = [taskRow(1), taskRow(2, { name: "Task 2b" })];
    expect((await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows })).status).toBe(200);
    const replay = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows });
    expect(replay.status, await replay.clone().text()).toBe(200);
    expect(await replay.json()).toMatchObject({ ok: true, done: true, inserted: 0, committed_through_row: 2 });
    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_schedule_tasks WHERE job_id='JOB-A'")
      .first<{ n: number }>();
    expect(n!.n).toBe(2);
  });

  it("two-uploads serialization: a SECOND schedule of the job mid-commit → 409 another_commit_in_progress, nothing lands", async () => {
    const id = await parsedSchedule("JOB-A");
    await env.DB
      .prepare(
        "INSERT INTO job_schedules (schedule_uuid, job_id, filename, declared_mime, size_bytes, sha256, hmac, uploaded_by, status) " +
          "VALUES ('other-uuid','JOB-A','rev0.pdf','application/pdf',100,'other-sha','mac','pm','committing')",
      )
      .run();
    const res = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows: [taskRow(1)] });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "another_commit_in_progress" });
    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_schedule_tasks")
      .first<{ n: number }>();
    expect(n!.n).toBe(0);
    const still = await env.DB
      .prepare("SELECT status, committed_through_row FROM job_schedules WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(still).toMatchObject({ status: "parsed", committed_through_row: 0 });
  });

  it("not committable from refused / discarded / pending — 409 schedule_not_committable naming the status", async () => {
    for (const status of ["refused", "discarded", "pending"]) {
      const id = await parsedSchedule("JOB-B", status === "refused" ? PDF_B64 : status === "discarded" ? PDF_B64_OTHER : b64(PDF_HEAD, 0x43), `${status}.pdf`);
      await env.DB.prepare("UPDATE job_schedules SET status = ?2 WHERE id = ?1").bind(id, status).run();
      const res = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows: [taskRow(1)] });
      expect(res.status, status).toBe(409);
      expect(await res.json()).toMatchObject({ error: "schedule_not_committable", status });
    }
  });

  it("watermark paging >100 rows: page 1 lands 100 + done:false, the SAME full payload re-post lands the rest + done:true", async () => {
    const id = await parsedSchedule("JOB-A");
    const rows = Array.from({ length: 150 }, (_, i) => taskRow(i + 1, { name: `Task ${i + 1}` }));
    const first = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows });
    expect(first.status, await first.clone().text()).toBe(200);
    expect(await first.json()).toMatchObject({ ok: true, done: false, inserted: 100, committed_through_row: 100 });
    // Mid-commit the pool row reads 'committing' at the page-1 watermark.
    const mid = await env.DB
      .prepare("SELECT status, committed_through_row FROM job_schedules WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(mid).toMatchObject({ status: "committing", committed_through_row: 100 });

    const second = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows });
    expect(await second.json()).toMatchObject({ ok: true, done: true, inserted: 50, committed_through_row: 150 });
    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_schedule_tasks WHERE job_id='JOB-A'")
      .first<{ n: number }>();
    expect(n!.n).toBe(150);
    // sort_order tracks the FULL payload's ordinal — page 2's first task is 101*10, so the
    // document order survives the paging.
    const t101 = await env.DB
      .prepare("SELECT sort_order FROM job_schedule_tasks WHERE source_row_index = 101")
      .first<{ sort_order: number }>();
    expect(t101!.sort_order).toBe(1010);
  });

  it("final page flips a PRIOR committed row → superseded FIRST (the one-committed UNIQUE ordering), stamped superseded_at", async () => {
    // The displaced governor: a bare committed row of the SAME job with NO tasks (its own
    // task list is out of scope here — PR-6 owns real supersede-with-tasks reconciles).
    await env.DB
      .prepare(
        "INSERT INTO job_schedules (schedule_uuid, job_id, filename, declared_mime, size_bytes, sha256, hmac, uploaded_by, status) " +
          "VALUES ('old-gov','JOB-A','rev0.pdf','application/pdf',100,'old-sha','mac','pm','committed')",
      )
      .run();
    const id = await parsedSchedule("JOB-A", PDF_B64_OTHER, "rev1.pdf");
    const res = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows: [taskRow(1)] });
    expect(res.status, await res.clone().text()).toBe(200);
    const old = await env.DB
      .prepare("SELECT status, superseded_at FROM job_schedules WHERE schedule_uuid='old-gov'")
      .first<Record<string, unknown>>();
    expect(old!.status).toBe("superseded");
    expect(old!.superseded_at).not.toBeNull();
    const now = await env.DB
      .prepare("SELECT status FROM job_schedules WHERE id = ?1")
      .bind(id)
      .first<{ status: string }>();
    expect(now!.status).toBe("committed");
    const audit = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='job_schedule_supersede'")
      .first<{ n: number }>();
    expect(audit!.n).toBe(1);
  });

  it("row validation refuses with the offending row's index; imported rows run the SAME bounds as manual add", async () => {
    const id = await parsedSchedule("JOB-A");
    const res = await p(admin, `/api/fieldops/schedules/${id}/commit`, {
      rows: [taskRow(1), taskRow(2, { start_date: "09/01/26" })],
    });
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({ error: "invalid_task_date", row: 1 });
    const bad = await p(admin, `/api/fieldops/schedules/${id}/plan`, {
      rows: [{ source_row_index: 1, kind: "gantt-bar", name: "X" }],
    });
    expect(bad.status).toBe(400);
    expect(await bad.json()).toMatchObject({ error: "invalid_task_kind" });
  });

  it("commit/plan are manage-gated and 404 an unknown schedule", async () => {
    expect((await p(manager, "/api/fieldops/schedules/999/commit", { rows: [taskRow(1)] })).status).toBe(403);
    expect((await p(admin, "/api/fieldops/schedules/999999/commit", { rows: [taskRow(1)] })).status).toBe(404);
    expect((await p(admin, "/api/fieldops/schedules/999999/plan", { rows: [taskRow(1)] })).status).toBe(404);
  });
});

describe("schedule bearer cannot reach the task surface (privilege separation)", () => {
  it("the internal bearer is not a session — task read/add refuse it", async () => {
    expect(
      (await call("/api/fieldops/schedule-tasks?job_id=JOB-A", { bearer: SCHEDULE_BEARER })).status,
    ).toBe(401);
    expect(
      (
        await call("/api/fieldops/schedule-tasks", {
          method: "POST", bearer: SCHEDULE_BEARER,
          body: JSON.stringify({ job_id: "JOB-A", name: "X" }),
        })
      ).status,
    ).toBe(401);
  });

  it("…and the PR-5 mark routes refuse it (and anon) too", async () => {
    const res = await addTask({});
    const id = ((await res.json()) as { id: number }).id;
    for (const route of ["progress", "milestone-done", "delivered"]) {
      expect(
        (
          await call(`/api/fieldops/schedule-tasks/${id}/${route}`, {
            method: "POST", bearer: SCHEDULE_BEARER, body: JSON.stringify({ percent: 50 }),
          })
        ).status,
        route,
      ).toBe(401);
      expect(
        (await call(`/api/fieldops/schedule-tasks/${id}/${route}`, { method: "POST" })).status,
        route,
      ).toBe(401);
    }
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// PR-5 — field mark-off (cap.schedule.mark, migration 0072). The properties under
// test: the role/cap matrix + the per-job ownership scope (requireJobScope with the
// cap.jobtracker.manage bypass — the task's OWN job_id anchors it), the from/to audit
// history, milestone binariness, milestone-done idempotency (the deactivate
// precedent), the delivered mark + date correction, and the last_marked_by
// exclusivity contract (ONLY these routes stamp it — decision 9's %-conflict
// predicate rides on that).
// ─────────────────────────────────────────────────────────────────────────────

describe("field mark-off — scope + capability matrix (cap.schedule.mark)", () => {
  it("a submitter placed on the task's job marks it; cross-job and unplaced are 403 forbidden_job; admin bypasses with no roster row", async () => {
    await seedPersonnel("Sam Submitter", "sub.sam", "JOB-A");
    const aId = ((await (await addTask({ name: "Fence A", percent_done: 10 })).json()) as { id: number }).id;
    const bId = ((await (await p(admin, "/api/fieldops/schedule-tasks", { job_id: "JOB-B", name: "Fence B" })).json()) as { id: number }).id;

    // Own placement → 200, and the FIELD-MARK stamps land (the submitter's account).
    const own = await p(sub, `/api/fieldops/schedule-tasks/${aId}/progress`, { percent: 50 });
    expect(own.status, await own.clone().text()).toBe(200);
    expect(await own.json()).toMatchObject({ ok: true, id: aId, percent_done: 50 });
    const marked = await env.DB
      .prepare("SELECT percent_done, last_marked_by, last_marked_at FROM job_schedule_tasks WHERE id = ?1")
      .bind(aId)
      .first<Record<string, unknown>>();
    expect(marked).toMatchObject({ percent_done: 50, last_marked_by: "sub.sam" });
    expect(marked!.last_marked_at).not.toBeNull();

    // A DIFFERENT job's task → 403 forbidden_job, row untouched.
    const cross = await p(sub, `/api/fieldops/schedule-tasks/${bId}/progress`, { percent: 50 });
    expect(cross.status).toBe(403);
    expect(((await cross.json()) as { error: string }).error).toBe("forbidden_job");
    const untouched = await env.DB
      .prepare("SELECT percent_done, last_marked_by FROM job_schedule_tasks WHERE id = ?1")
      .bind(bId)
      .first<Record<string, unknown>>();
    expect(untouched).toMatchObject({ percent_done: 0, last_marked_by: null });

    // An UNPLACED manager (no personnel link) holds the cap but fails the scope.
    const unplaced = await p(manager, `/api/fieldops/schedule-tasks/${aId}/progress`, { percent: 75 });
    expect(unplaced.status).toBe(403);
    expect(((await unplaced.json()) as { error: string }).error).toBe("forbidden_job");

    // Admin holds cap.jobtracker.manage — the bypass set — so any job, no roster row needed.
    expect((await p(admin, `/api/fieldops/schedule-tasks/${bId}/progress`, { percent: 25 })).status).toBe(200);
    // …and a PLACED manager marks their own job (0072 grants manager the cap).
    await seedPersonnel("Mo Manager", "mgr.mike", "JOB-A");
    expect((await p(manager, `/api/fieldops/schedule-tasks/${aId}/progress`, { percent: 75 })).status).toBe(200);
  });

  it("the cap gate is REAL — revoking cap.schedule.mark from submitter → 403 forbidden even on their own job", async () => {
    await seedPersonnel("Sam Submitter", "sub.sam", "JOB-A");
    const id = ((await (await addTask({})).json()) as { id: number }).id;
    await env.DB
      .prepare("DELETE FROM role_capabilities WHERE role_key = 'submitter' AND capability_key = 'cap.schedule.mark'")
      .run();
    try {
      expect((await p(sub, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 50 })).status).toBe(403);
    } finally {
      // Restore the seeded grant — role_capabilities persists across this file's tests.
      await env.DB
        .prepare("INSERT OR IGNORE INTO role_capabilities (role_key, capability_key) VALUES ('submitter', 'cap.schedule.mark')")
        .run();
    }
    expect((await p(sub, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 50 })).status).toBe(200);
  });
});

describe("field mark-off — POST /:id/progress", () => {
  it("happy path audits from/to (audit_log IS the progress history); a % regression is allowed; W9 name serves on read", async () => {
    await seedPersonnel("Sam Submitter", "sub.sam", "JOB-A");
    const res = await addTask({ name: "Fencing", percent_done: 25 });
    const id = ((await res.json()) as { id: number }).id;

    expect((await p(sub, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 75 })).status).toBe(200);
    const first = await env.DB
      .prepare("SELECT target_username, detail FROM audit_log WHERE action='schedule_task_progress' ORDER BY id ASC LIMIT 1")
      .first<{ target_username: string; detail: string }>();
    expect(first!.target_username).toBe("JOB-A");
    const detail = JSON.parse(first!.detail) as Record<string, unknown>;
    expect(detail).toMatchObject({ job_id: "JOB-A", from: 25, to: 75 });
    expect(String(detail.task_uuid)).toMatch(/^[0-9a-f-]{36}$/);

    // Regression (a correction) is real — allowed, and its own audit row records it.
    expect((await p(sub, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 50 })).status).toBe(200);
    const audits = await env.DB
      .prepare("SELECT detail FROM audit_log WHERE action='schedule_task_progress' ORDER BY id ASC")
      .all<{ detail: string }>();
    expect(audits.results).toHaveLength(2);
    expect(JSON.parse(audits.results![1].detail)).toMatchObject({ from: 75, to: 50 });

    // The read serves the DISPLAY name (W9), never the account username.
    const read = (await (await g(sub, "/api/fieldops/schedule-tasks?job_id=JOB-A")).json()) as {
      tasks: Record<string, unknown>[];
    };
    expect(read.tasks[0].last_marked_by_name).toBe("Sam Submitter");
    expect(read.tasks[0].percent_done).toBe(50);
  });

  it("bounds: 101 / -1 / 12.5 / '50' / missing → 400 invalid_percent; garbage body → 400 bad_request; unknown id → 404", async () => {
    const id = ((await (await addTask({})).json()) as { id: number }).id;
    for (const percent of [101, -1, 12.5, "50", undefined]) {
      const res = await p(admin, `/api/fieldops/schedule-tasks/${id}/progress`, { percent });
      expect(res.status, JSON.stringify(percent ?? null)).toBe(400);
      expect(await res.json()).toMatchObject({ error: "invalid_percent" });
    }
    expect(
      (
        await call(`/api/fieldops/schedule-tasks/${id}/progress`, {
          method: "POST", cookie: admin, body: "not json",
        })
      ).status,
    ).toBe(400);
    expect((await p(admin, "/api/fieldops/schedule-tasks/999999/progress", { percent: 10 })).status).toBe(404);
  });

  it("a milestone is BINARY — 50 refuses 400 milestone_binary and stamps nothing; 100 and 0 both land", async () => {
    const res = await addTask({ name: "Substantial Completion", is_milestone: true });
    const id = ((await res.json()) as { id: number }).id;
    const half = await p(admin, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 50 });
    expect(half.status).toBe(400);
    expect(await half.json()).toMatchObject({ error: "milestone_binary" });
    const row = await env.DB
      .prepare("SELECT percent_done, last_marked_by FROM job_schedule_tasks WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ percent_done: 0, last_marked_by: null });
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 100 })).status).toBe(200);
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 0 })).status).toBe(200);
  });

  it("milestone_binary is enforced IN-WHERE, not just at load time — the mutation itself refuses the race arm (2026-08-11 review)", async () => {
    // The TOCTOU window (an office /:id/edit flips is_milestone AFTER the route's pre-load
    // check passed) can't be interleaved through the single-threaded test worker, so this
    // proves the guard at its own level: the route's exact mutation WHERE, run against a
    // milestone row with a non-binary percent, must change ZERO rows. Mirrors the route's
    // SQL in fieldops_schedule_tasks.ts — if that WHERE changes, change this with it.
    const res = await addTask({ name: "Substantial Completion", is_milestone: true });
    const id = ((await res.json()) as { id: number }).id;
    const raced = await env.DB
      .prepare(
        `UPDATE job_schedule_tasks
            SET percent_done = ?2, last_marked_by = ?3, last_marked_at = unixepoch(),
                updated_at = unixepoch()
          WHERE id = ?1 AND active = 1
            AND (is_milestone = 0 OR ?2 IN (0, 100))`,
      )
      .bind(id, 42, "racer")
      .run();
    expect(raced.meta.changes).toBe(0); // the invariant held atomically
    const row = await env.DB
      .prepare("SELECT percent_done, last_marked_by FROM job_schedule_tasks WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ percent_done: 0, last_marked_by: null });
    // …and the same statement with a BINARY percent still lands (the guard isn't a lockout).
    const ok = await env.DB
      .prepare(
        `UPDATE job_schedule_tasks
            SET percent_done = ?2, last_marked_by = ?3, last_marked_at = unixepoch(),
                updated_at = unixepoch()
          WHERE id = ?1 AND active = 1
            AND (is_milestone = 0 OR ?2 IN (0, 100))`,
      )
      .bind(id, 100, "racer")
      .run();
    expect(ok.meta.changes).toBe(1);
  });
});

describe("field mark-off — POST /:id/milestone-done", () => {
  it("sets 100 + the field-mark stamps + ONE audit; the second call is already:true with NO re-stamp and NO second audit (the deactivate precedent)", async () => {
    const res = await addTask({ name: "Substantial Completion", is_milestone: true });
    const id = ((await res.json()) as { id: number }).id;

    const first = await p(admin, `/api/fieldops/schedule-tasks/${id}/milestone-done`);
    expect(first.status, await first.clone().text()).toBe(200);
    expect(await first.json()).toMatchObject({ ok: true, id });
    const row = await env.DB
      .prepare("SELECT percent_done, last_marked_by, last_marked_at FROM job_schedule_tasks WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ percent_done: 100, last_marked_by: "admin.one" });
    const audit = await env.DB
      .prepare("SELECT detail FROM audit_log WHERE action='schedule_task_milestone_done'")
      .all<{ detail: string }>();
    expect(audit.results).toHaveLength(1);
    expect(JSON.parse(audit.results![0].detail)).toMatchObject({ from: 0, to: 100 });

    // Pin a sentinel stamp, then repeat: already:true, stamp UNTOUCHED, still one audit row.
    await env.DB.prepare("UPDATE job_schedule_tasks SET last_marked_at = 123 WHERE id = ?1").bind(id).run();
    const again = await p(admin, `/api/fieldops/schedule-tasks/${id}/milestone-done`);
    expect(again.status).toBe(200);
    expect(await again.json()).toMatchObject({ ok: true, id, already: true });
    const after = await env.DB
      .prepare("SELECT last_marked_at FROM job_schedule_tasks WHERE id = ?1")
      .bind(id)
      .first<{ last_marked_at: number }>();
    expect(after!.last_marked_at).toBe(123);
    const audits = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='schedule_task_milestone_done'")
      .first<{ n: number }>();
    expect(audits!.n).toBe(1);
  });

  it("a non-milestone task → 400 not_a_milestone; unknown id → 404", async () => {
    const id = ((await (await addTask({})).json()) as { id: number }).id;
    const res = await p(admin, `/api/fieldops/schedule-tasks/${id}/milestone-done`);
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({ error: "not_a_milestone" });
    expect((await p(admin, "/api/fieldops/schedule-tasks/999999/milestone-done")).status).toBe(404);
  });
});

describe("field mark-off — POST /:id/delivered", () => {
  async function deliveryTask(name = "Pile Delivery"): Promise<number> {
    const res = await addTask({ name, section: "Deliveries", is_delivery: true });
    return ((await res.json()) as { id: number }).id;
  }

  it("explicit date lands delivered_date/by/at + the field-mark stamps + audit; the read serves the W9 display name", async () => {
    await seedPersonnel("Mo Manager", "mgr.mike", "JOB-A");
    const id = await deliveryTask();
    const res = await p(manager, `/api/fieldops/schedule-tasks/${id}/delivered`, { delivered_date: "2026-09-10" });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await res.json()).toMatchObject({ ok: true, id, delivered_date: "2026-09-10" });
    const row = await env.DB
      .prepare(
        "SELECT delivered_date, delivered_by, delivered_at, last_marked_by, last_marked_at FROM job_schedule_tasks WHERE id = ?1",
      )
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ delivered_date: "2026-09-10", delivered_by: "mgr.mike", last_marked_by: "mgr.mike" });
    expect(row!.delivered_at).not.toBeNull();
    expect(row!.last_marked_at).not.toBeNull();
    const read = (await (await g(manager, "/api/fieldops/schedule-tasks?job_id=JOB-A")).json()) as {
      tasks: Record<string, unknown>[];
    };
    expect(read.tasks[0].delivered_by_name).toBe("Mo Manager");
    expect(read.tasks[0]).not.toHaveProperty("delivered_by");
  });

  it("an empty body defaults the business date to today PACIFIC (the Worker's clock, not the browser's)", async () => {
    const id = await deliveryTask("Module Delivery");
    const res = await p(admin, `/api/fieldops/schedule-tasks/${id}/delivered`);
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as { delivered_date: string };
    expect(body.delivered_date).toBe(pacificDateString(Date.now()));
  });

  it("a second call UPDATES the date (a correction is real) — audited with from/to", async () => {
    const id = await deliveryTask();
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/delivered`, { delivered_date: "2026-09-10" })).status).toBe(200);
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/delivered`, { delivered_date: "2026-09-11" })).status).toBe(200);
    const row = await env.DB
      .prepare("SELECT delivered_date FROM job_schedule_tasks WHERE id = ?1")
      .bind(id)
      .first<{ delivered_date: string }>();
    expect(row!.delivered_date).toBe("2026-09-11");
    const audits = await env.DB
      .prepare("SELECT detail FROM audit_log WHERE action='schedule_task_delivered' ORDER BY id ASC")
      .all<{ detail: string }>();
    expect(audits.results).toHaveLength(2);
    expect(JSON.parse(audits.results![1].detail)).toMatchObject({ from: "2026-09-10", to: "2026-09-11" });
  });

  it("bad date → 400 invalid_delivered_date; a non-delivery task → 400 not_a_delivery; unknown id → 404", async () => {
    const id = await deliveryTask();
    const bad = await p(admin, `/api/fieldops/schedule-tasks/${id}/delivered`, { delivered_date: "09/10/2026" });
    expect(bad.status).toBe(400);
    expect(await bad.json()).toMatchObject({ error: "invalid_delivered_date" });
    const plainId = ((await (await addTask({ name: "Not a delivery" })).json()) as { id: number }).id;
    const wrong = await p(admin, `/api/fieldops/schedule-tasks/${plainId}/delivered`, { delivered_date: "2026-09-10" });
    expect(wrong.status).toBe(400);
    expect(await wrong.json()).toMatchObject({ error: "not_a_delivery" });
    expect((await p(admin, "/api/fieldops/schedule-tasks/999999/delivered")).status).toBe(404);
  });
});

describe("field mark-off — a removed task and the last_marked exclusivity contract", () => {
  it("a deactivated task 404s on all three mark routes", async () => {
    const res = await addTask({ name: "Gone", is_milestone: true, is_delivery: true });
    const id = ((await res.json()) as { id: number }).id;
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/deactivate`)).status).toBe(200);
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 100 })).status).toBe(404);
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/milestone-done`)).status).toBe(404);
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/delivered`)).status).toBe(404);
  });

  it("ONLY the mark routes stamp last_marked_by: an office percent edit leaves it NULL, and PRESERVES an existing field mark", async () => {
    const res = await addTask({ name: "Fencing", percent_done: 10 });
    const id = ((await res.json()) as { id: number }).id;

    // Regression-assert (decision 9): the office edit route changes percent WITHOUT stamping.
    expect(
      (await p(admin, `/api/fieldops/schedule-tasks/${id}/edit`, { name: "Fencing", percent_done: 40 })).status,
    ).toBe(200);
    let row = await env.DB
      .prepare("SELECT percent_done, last_marked_by, last_marked_at FROM job_schedule_tasks WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ percent_done: 40, last_marked_by: null, last_marked_at: null });

    // A field mark stamps; a later office edit must not erase that the field marked it.
    expect((await p(admin, `/api/fieldops/schedule-tasks/${id}/progress`, { percent: 60 })).status).toBe(200);
    expect(
      (await p(admin, `/api/fieldops/schedule-tasks/${id}/edit`, { name: "Fencing", percent_done: 65 })).status,
    ).toBe(200);
    row = await env.DB
      .prepare("SELECT percent_done, last_marked_by FROM job_schedule_tasks WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ percent_done: 65, last_marked_by: "admin.one" });
  });
});

describe("2026-08-11 security-review BLOCK fixes (stuck-committing repair + discard cascade)", () => {
  it("a replay against a schedule STUCK in 'committing' (finalize batch lost) repairs it to 'committed'", async () => {
    // Simulate the Worker-eviction window: page batch landed (tasks + maxed watermark),
    // finalize batch never ran. The client's documented recovery is a plain re-post —
    // before the fix it returned done:true while the row stayed 'committing' forever.
    const id = await parsedSchedule("JOB-A");
    const rows = [taskRow(1), taskRow(2)];
    expect((await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows })).status).toBe(200);
    // Force the stuck state: back to 'committing' with the watermark already at max.
    await env.DB.prepare("UPDATE job_schedules SET status='committing', committed_at=NULL WHERE id = ?1").bind(id).run();

    const replay = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows });
    expect(replay.status).toBe(200);
    expect(await replay.json()).toMatchObject({ ok: true, done: true, inserted: 0 });
    const after = await env.DB
      .prepare("SELECT status, committed_at FROM job_schedules WHERE id = ?1")
      .bind(id)
      .first<{ status: string; committed_at: number | null }>();
    expect(after!.status).toBe("committed");
    expect(after!.committed_at).not.toBeNull();
    // And no duplicate tasks from the replay.
    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_schedule_tasks WHERE source_schedule_id = ?1 AND active = 1")
      .bind(id)
      .first<{ n: number }>();
    expect(n!.n).toBe(2);
  });

  it("discarding a mid-commit schedule deactivates its orphan tasks — and the job can import again", async () => {
    const id = await parsedSchedule("JOB-A");
    // Land page 1 of 2 (watermark below max) — tasks exist, status 'committing'.
    const allRows = Array.from({ length: 120 }, (_, i) => taskRow(i + 1));
    const first = await p(admin, `/api/fieldops/schedules/${id}/commit`, { rows: allRows });
    expect(await first.clone().json()).toMatchObject({ ok: true, done: false });

    const res = await p(admin, `/api/fieldops/schedules/${id}/discard`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { orphaned_tasks_deactivated: number };
    expect(body.orphaned_tasks_deactivated).toBe(100); // exactly one committed page

    const live = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_schedule_tasks WHERE job_id = 'JOB-A' AND active = 1")
      .bind()
      .first<{ n: number }>();
    expect(live!.n).toBe(0); // no orphaned remnants pretend to be a task list

    // THE LOCKOUT IS GONE: a fresh upload plans degenerate and commits cleanly.
    const id2 = await parsedSchedule("JOB-A", PDF_B64_OTHER, "Project Schedule rev2.pdf");
    const plan = await p(admin, `/api/fieldops/schedules/${id2}/plan`, { rows: [taskRow(1)] });
    expect(await plan.json()).toMatchObject({ ok: true, degenerate: true });
    expect((await p(admin, `/api/fieldops/schedules/${id2}/commit`, { rows: [taskRow(1)] })).status).toBe(200);
    const audit = await env.DB
      .prepare("SELECT detail FROM audit_log WHERE action = 'job_schedule_discard_orphan_tasks'")
      .first<{ detail: string }>();
    expect(audit).not.toBeNull();
  });
});
