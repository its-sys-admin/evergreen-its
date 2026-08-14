import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { provision, login, g, seedJob, seedPersonnel } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// GET /api/fieldops/tasks?job_id= (Track A4) — ONE job's assigned tasks for the Site Tasks page.
// Read tier cap.jobtracker.read (the job-detail tasks leg's existing exposure — no data class
// widens); WHO resolves through personnel.name (display-name-only, House Reflex §5);
// viewer_personnel_id / viewer_privileged are display hints (the write route re-enforces
// ownership in its own WHERE). Ordering mirrors /tasks/mine: open-first, dated-first due ASC.
// ─────────────────────────────────────────────────────────────────────────────

async function seedTask(
  jobId: string,
  personnelId: number | null,
  description: string,
  over: { status?: string; dueDate?: string | null; createdAt?: number } = {},
): Promise<number> {
  const r = await env.DB
    .prepare(
      "INSERT INTO task_assignments (job_id, personnel_id, description, status, assigned_by, created_at, due_date) " +
        "VALUES (?1, ?2, ?3, ?4, 'adm.a', ?5, ?6) RETURNING id",
    )
    .bind(jobId, personnelId, description, over.status ?? "open", over.createdAt ?? 1_700_000_000, over.dueDate ?? null)
    .first<{ id: number }>();
  return r!.id;
}

let admin: string, submitter: string;
let samId: number;

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM task_assignments"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
  await provision("adm.a", "password123", "admin");
  await provision("sub.sam", "password123", "submitter");
  admin = await login("adm.a", "password123");
  submitter = await login("sub.sam", "password123");
  await seedJob("JOB-A");
  await seedPersonnel("Sam Sub", "sub.sam", "JOB-A");
  samId = (await env.DB.prepare("SELECT id FROM personnel WHERE username='sub.sam'").first<{ id: number }>())!.id;
});

describe("GET /api/fieldops/tasks", () => {
  it("rejects a missing/oversized job_id and 404s an unknown job", async () => {
    expect((await g(admin, "/api/fieldops/tasks")).status).toBe(400);
    expect((await g(admin, `/api/fieldops/tasks?job_id=${"x".repeat(65)}`)).status).toBe(400);
    expect((await g(admin, "/api/fieldops/tasks?job_id=JOB-NOPE")).status).toBe(404);
  });

  it("requires a session (401 anonymous)", async () => {
    const res = await fetch("http://example.com/api/fieldops/tasks?job_id=JOB-A");
    // helpers' g() carries the cookie; a bare fetch through the worker under test needs the
    // harness — use g with an empty cookie instead.
    expect([401, 403]).toContain((await g("", "/api/fieldops/tasks?job_id=JOB-A")).status);
    void res;
  });

  it("returns the job's tasks ordered open-first then due-date urgency, with display names", async () => {
    await seedTask("JOB-A", samId, "Done long ago", { status: "done", createdAt: 1 });
    await seedTask("JOB-A", samId, "Open undated", { createdAt: 3 });
    await seedTask("JOB-A", samId, "Open due soon", { dueDate: "2026-08-20", createdAt: 2 });
    await seedTask("JOB-A", null, "Unassigned open", { dueDate: "2026-09-01", createdAt: 4 });
    await seedTask("JOB-B-ELSEWHERE", samId, "Foreign job", {}); // absent job row is fine — filtered by job_id
    const res = await g(submitter, "/api/fieldops/tasks?job_id=JOB-A");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as {
      tasks: { description: string; assignee_name: string | null; personnel_id: number | null }[];
      project_name: string;
      viewer_personnel_id: number | null;
      viewer_privileged: boolean;
    };
    expect(body.project_name).toBeTruthy();
    expect(body.tasks.map((t) => t.description)).toEqual([
      "Open due soon", "Unassigned open", "Open undated", "Done long ago",
    ]);
    expect(body.tasks[0].assignee_name).toBe("Sam Sub"); // personnel.name, never username
    expect(body.tasks[1].assignee_name).toBeNull();
    // Viewer hints: the submitter's own personnel link, unprivileged.
    expect(body.viewer_personnel_id).toBe(samId);
    expect(body.viewer_privileged).toBe(false);
  });

  it("flags a privileged viewer (admin holds cap.jobtracker.manage) without a personnel link", async () => {
    const res = await g(admin, "/api/fieldops/tasks?job_id=JOB-A");
    const body = (await res.json()) as { viewer_personnel_id: number | null; viewer_privileged: boolean };
    expect(body.viewer_personnel_id).toBeNull();
    expect(body.viewer_privileged).toBe(true);
  });
});
