import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, get, provision, login, seedJob as seedJobRow, seedPersonnel as seedPersonnelRow } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// BRIEF C — Job Tracker tab (cap.jobtracker.read, SUBMITTER + ADMIN).
// Runs against the REAL worker with Miniflare D1; SELF.fetch cookie-forwarding.
// F5: the LIST filters by a validated `status` param (not a hard active=1 gate); the per-job
// DETAIL serves any status and 404s only a truly unknown job_id.
// ─────────────────────────────────────────────────────────────────────────────

// ── seed helpers ──────────────────────────────────────────────────────────────
async function seedClient(name: string): Promise<number> {
  await env.DB.prepare("INSERT INTO clients (name, contact, phone, email) VALUES (?,?,?,?)")
    .bind(name, "Pat Contact", "555-0100", "pat@example.com").run();
  return (await env.DB.prepare("SELECT id FROM clients WHERE name=?").bind(name).first<{ id: number }>())!.id;
}
const seedJob = (jobId: string, projectName: string, status: string, progress = 0, clientId: number | null = null): Promise<void> =>
  seedJobRow(jobId, { projectName, status, progress, client_id: clientId });
const seedPersonnel = (name: string, trade: string): Promise<number> =>
  seedPersonnelRow(name, name.toLowerCase().replace(/\s+/g, "."), null, { trade });
// Crew is the people PLACED on a job (personnel.current_job, migration 0023) — set placement here.
// This is what the crew legs now read (converged onto placement); NULL = unplaced (not on any crew).
async function placePersonnel(personnelId: number, jobId: string): Promise<void> {
  await env.DB.prepare("UPDATE personnel SET current_job = ? WHERE id = ?").bind(jobId, personnelId).run();
}
async function seedTask(jobId: string, personnelId: number | null, description: string, status: string, createdAt: number, dueDate: string | null = null): Promise<number> {
  await env.DB.prepare(
    "INSERT INTO task_assignments (job_id, personnel_id, description, status, created_at, due_date) VALUES (?,?,?,?,?,?)",
  ).bind(jobId, personnelId, description, status, createdAt, dueDate).run();
  return (await env.DB.prepare("SELECT id FROM task_assignments WHERE job_id=? AND description=?")
    .bind(jobId, description).first<{ id: number }>())!.id;
}
// R7: optional task_id (the attribution join source) + actor (the recorded-by stamp).
async function seedTimeEntry(
  jobId: string,
  personnelId: number | null,
  uuid: string,
  createdAt: number,
  opts: { taskId?: number | null; actor?: string } = {},
): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO time_entries (uuid, job_id, personnel_id, task_id, work_started_at, work_ended_at, hours, notes, created_at, actor_username) VALUES (?,?,?,?,?,?,?,?,?,?)",
  ).bind(uuid, jobId, personnelId, opts.taskId ?? null, createdAt - 3600, createdAt, 8, "note", createdAt, opts.actor ?? "admin.one").run();
}
async function seedEquipment(name: string): Promise<number> {
  await env.DB.prepare("INSERT INTO equipment (name, kind, identifier, active) VALUES (?,?,?,1)")
    .bind(name, "skid-steer", name.toUpperCase()).run();
  return (await env.DB.prepare("SELECT id FROM equipment WHERE name=?").bind(name).first<{ id: number }>())!.id;
}
async function seedLocation(equipmentId: number, jobId: string, recordedAt: number): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO equipment_location (equipment_id, job_id, label, lat, lon, read_at, recorded_at) VALUES (?,?,?,?,?,?,?)",
  ).bind(equipmentId, jobId, "Site", 1.0, 2.0, recordedAt, recordedAt).run();
}
async function seedInspection(jobId: string, equipmentId: number, uuid: string, createdAt: number): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO inspections (uuid, job_id, equipment_id, form_code, version, payload_json, performed_at, created_at, actor_username) VALUES (?,?,?,?,?,?,?,?,?)",
  ).bind(uuid, jobId, equipmentId, "skid-daily", 1, "{}", createdAt, createdAt, "admin.one").run();
}

beforeEach(async () => {
  // 0004 dev-seeds jobs; clear everything for deterministic status-filter assertions.
  await env.DB.batch([
    env.DB.prepare("DELETE FROM users"),
    // time_entries.task_id REFERENCES task_assignments(id) → children first (R7 seeds task-linked
    // time entries).
    env.DB.prepare("DELETE FROM time_entries"),
    env.DB.prepare("DELETE FROM task_assignments"),
    env.DB.prepare("DELETE FROM inspections"),
    env.DB.prepare("DELETE FROM equipment_location"),
    env.DB.prepare("DELETE FROM equipment"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM clients"),
  ]);
});

// ── GET /api/fieldops/jobs (list) ───────────────────────────────────────────────
describe("GET /api/fieldops/jobs", () => {
  beforeEach(async () => {
    await provision("admin.one", "password123", "admin");
    await provision("submitter.jim", "password123", "submitter");
  });

  it("no session → 401", async () => {
    expect((await call("/api/fieldops/jobs")).status).toBe(401);
  });

  it("submitter is allowed (cap.jobtracker.read is submitter + admin) → 200", async () => {
    const c = await login("submitter.jim", "password123");
    expect((await call("/api/fieldops/jobs", { cookie: c })).status).toBe(200);
  });

  it("empty list when no jobs", async () => {
    const c = await login("admin.one", "password123");
    const res = await call("/api/fieldops/jobs", { cookie: c });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { jobs: any[]; next_cursor: string | null };
    expect(body.jobs).toEqual([]);
    expect(body.next_cursor).toBeNull();
  });

  it("F5: ?status filters; active excludes closed, closed returns closed, all returns both", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    await seedJob("JOB-Z", "Zulu", "closed");
    const c = await login("admin.one", "password123");

    const active = (await (await call("/api/fieldops/jobs?status=active", { cookie: c })).json()) as { jobs: any[] };
    expect(active.jobs.map((j) => j.job_id)).toEqual(["JOB-A"]);

    const closed = (await (await call("/api/fieldops/jobs?status=closed", { cookie: c })).json()) as { jobs: any[] };
    expect(closed.jobs.map((j) => j.job_id)).toEqual(["JOB-Z"]);

    const all = (await (await call("/api/fieldops/jobs?status=all", { cookie: c })).json()) as { jobs: any[] };
    expect(all.jobs.map((j) => j.job_id).sort()).toEqual(["JOB-A", "JOB-Z"]);
  });

  it("invalid status falls back to active (not 400)", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    await seedJob("JOB-Z", "Zulu", "closed");
    const c = await login("admin.one", "password123");
    const res = await call("/api/fieldops/jobs?status=bogus", { cookie: c });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { jobs: any[] };
    expect(body.jobs.map((j) => j.job_id)).toEqual(["JOB-A"]);
  });

  it("returns client_name + crew + open_tasks per job (open excludes done)", async () => {
    const clientId = await seedClient("Acme Co");
    await seedJob("JOB-A", "Alpha", "active", 40, clientId);
    const pid = await seedPersonnel("Alice Chen", "operator");
    await placePersonnel(pid, "JOB-A"); // crew = placed personnel
    await seedTask("JOB-A", pid, "Dig footings", "open", 100, "2026-07-10"); // (G2.6) dated
    await seedTask("JOB-A", pid, "Finished item", "done", 90);
    const c = await login("admin.one", "password123");
    const body = (await (await call("/api/fieldops/jobs?status=active", { cookie: c })).json()) as { jobs: any[] };
    const job = body.jobs.find((j) => j.job_id === "JOB-A");
    expect(job.client_name).toBe("Acme Co");
    expect(job.crew.map((p: any) => p.name)).toContain("Alice Chen");
    expect(job.open_tasks).toHaveLength(1); // 'done' excluded
    expect(job.open_tasks[0].description).toBe("Dig footings");
    expect(job.open_tasks[0].due_date).toBe("2026-07-10"); // (G2.6) due_date rides the card preview
  });

  it("crew = PLACED personnel: a task-assigned-but-unplaced person is NOT crew (convergence)", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    const placed = await seedPersonnel("Placed Pat", "operator");
    await placePersonnel(placed, "JOB-A"); // on the crew
    const assignedOnly = await seedPersonnel("Task Tom", "laborer");
    await seedTask("JOB-A", assignedOnly, "Dig footings", "open", 100); // task, but NOT placed
    const c = await login("admin.one", "password123");
    const body = (await (await call("/api/fieldops/jobs?status=active", { cookie: c })).json()) as { jobs: any[] };
    const job = body.jobs.find((j) => j.job_id === "JOB-A");
    const crewNames = job.crew.map((p: any) => p.name);
    expect(crewNames).toContain("Placed Pat");
    expect(crewNames).not.toContain("Task Tom"); // assigned a task but not placed → not crew
    // The task assignment is unaffected: it still surfaces as an open task with its assignee.
    expect(job.open_tasks.map((t: any) => t.personnel_name)).toContain("Task Tom");
  });

  it("crew excludes a retired (inactive) placement and scopes to the right job", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    await seedJob("JOB-B", "Bravo", "active");
    const onA = await seedPersonnel("Anna A", "operator");
    await placePersonnel(onA, "JOB-A");
    const onB = await seedPersonnel("Bob B", "operator");
    await placePersonnel(onB, "JOB-B");
    const retired = await seedPersonnel("Gone Gwen", "operator");
    await placePersonnel(retired, "JOB-A");
    await env.DB.prepare("UPDATE personnel SET active = 0 WHERE id = ?").bind(retired).run();
    const c = await login("admin.one", "password123");
    const body = (await (await call("/api/fieldops/jobs?status=all", { cookie: c })).json()) as { jobs: any[] };
    const jobA = body.jobs.find((j) => j.job_id === "JOB-A");
    const jobB = body.jobs.find((j) => j.job_id === "JOB-B");
    expect(jobA.crew.map((p: any) => p.name)).toEqual(["Anna A"]); // Bob is JOB-B, Gwen is inactive
    expect(jobB.crew.map((p: any) => p.name)).toEqual(["Bob B"]);
  });

  it("keyset walks page 2 with no overlap", async () => {
    for (let i = 0; i < 75; i++) {
      await seedJob(`JOB-${String(i).padStart(3, "0")}`, `Project ${String(i).padStart(3, "0")}`, "active");
    }
    const c = await login("admin.one", "password123");
    let body = (await (await call("/api/fieldops/jobs?status=active&limit=50", { cookie: c })).json()) as { jobs: any[]; next_cursor: string };
    expect(body.jobs).toHaveLength(50);
    expect(body.next_cursor).not.toBeNull();
    const page1 = new Set(body.jobs.map((j) => j.job_id));
    const body2 = (await (await call(`/api/fieldops/jobs?status=active&limit=50&cursor=${body.next_cursor}`, { cookie: c })).json()) as { jobs: any[]; next_cursor: string | null };
    expect(body2.jobs).toHaveLength(25);
    for (const j of body2.jobs) expect(page1.has(j.job_id)).toBe(false);
    expect(body2.next_cursor).toBeNull();
  });

  it("hostile non-primitive cursor → first page (200), never 500", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    const c = await login("admin.one", "password123");
    const hostile = btoa(JSON.stringify({ p: {}, j: [] })).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const res = await call(`/api/fieldops/jobs?cursor=${hostile}`, { cookie: c });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(((await res.json()) as { jobs: any[] }).jobs.length).toBeGreaterThan(0);
  });
});

// ── GET /api/fieldops/jobs/:job_id (detail) ─────────────────────────────────────
describe("GET /api/fieldops/jobs/:job_id", () => {
  beforeEach(async () => {
    await provision("admin.one", "password123", "admin");
    await provision("submitter.jim", "password123", "submitter");
  });

  it("no session → 401", async () => {
    expect((await call("/api/fieldops/jobs/JOB-A")).status).toBe(401);
  });

  it("submitter is allowed → 200", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    const c = await login("submitter.jim", "password123");
    expect((await call("/api/fieldops/jobs/JOB-A", { cookie: c })).status).toBe(200);
  });

  it("unknown job_id → 404", async () => {
    const c = await login("admin.one", "password123");
    expect((await call("/api/fieldops/jobs/NOPE", { cookie: c })).status).toBe(404);
  });

  it("F5: detail of a CLOSED job → 200 (not 404)", async () => {
    await seedJob("JOB-Z", "Zulu", "closed");
    const c = await login("admin.one", "password123");
    const res = await call("/api/fieldops/jobs/JOB-Z", { cookie: c });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { job: any };
    expect(body.job.job_id).toBe("JOB-Z");
    expect(body.job.status).toBe("closed");
  });

  it("returns header + client + crew + tasks + time + inspections", async () => {
    const clientId = await seedClient("Acme Co");
    await seedJob("JOB-A", "Alpha", "active", 60, clientId);
    const pid = await seedPersonnel("Alice Chen", "operator");
    await placePersonnel(pid, "JOB-A"); // crew = placed personnel
    await seedTask("JOB-A", pid, "Dig", "open", 100, "2026-07-10"); // (G2.6) dated
    await seedTimeEntry("JOB-A", pid, "te-1", 200);
    const eq = await seedEquipment("unit-a");
    await seedInspection("JOB-A", eq, "in-1", 150);
    const c = await login("admin.one", "password123");
    const body = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: c })).json()) as { job: any; cursors: any };
    expect(body.job.client.name).toBe("Acme Co");
    expect(body.job.crew.map((p: any) => p.name)).toContain("Alice Chen");
    expect(body.job.tasks).toHaveLength(1);
    expect(body.job.tasks[0].due_date).toBe("2026-07-10"); // (G2.6) due_date rides the detail leg
    expect(body.job.time_entries).toHaveLength(1);
    expect(body.job.time_entries[0].recorded_at).toBe(200); // created_at AS recorded_at
    expect(body.job.inspections).toHaveLength(1);
    expect(body.cursors).toHaveProperty("tasks");
  });

  it("detail crew = PLACED personnel: task-assigned-but-unplaced excluded (convergence)", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    const placed = await seedPersonnel("Placed Pat", "operator");
    await placePersonnel(placed, "JOB-A");
    const assignedOnly = await seedPersonnel("Task Tom", "laborer");
    await seedTask("JOB-A", assignedOnly, "Dig", "open", 100); // task, not placed
    const c = await login("admin.one", "password123");
    const body = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: c })).json()) as { job: any };
    const crewNames = body.job.crew.map((p: any) => p.name);
    expect(crewNames).toContain("Placed Pat");
    expect(crewNames).not.toContain("Task Tom");
    // Tom's task still shows in the tasks leg with his name.
    expect(body.job.tasks.map((t: any) => t.personnel_name)).toContain("Task Tom");
  });

  it("equipment-on-site: includes a unit whose LATEST location is this job, excludes one moved away", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    await seedJob("JOB-B", "Bravo", "active");
    const here = await seedEquipment("here-unit");
    await seedLocation(here, "JOB-A", 100);
    await seedLocation(here, "JOB-A", 200); // latest on JOB-A
    const moved = await seedEquipment("moved-unit");
    await seedLocation(moved, "JOB-A", 50); // was on JOB-A
    await seedLocation(moved, "JOB-B", 300); // latest on JOB-B → excluded from JOB-A
    const c = await login("admin.one", "password123");
    const body = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: c })).json()) as { job: any };
    const names = body.job.equipment_on_site.map((e: any) => e.name);
    expect(names).toContain("here-unit");
    expect(names).not.toContain("moved-unit");
  });

  it("time-entries leg keyset paginates without overlap", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    const pid = await seedPersonnel("Alice Chen", "operator");
    for (let i = 0; i < 75; i++) await seedTimeEntry("JOB-A", pid, `te-${String(i).padStart(3, "0")}`, 1000 - i);
    const c = await login("admin.one", "password123");
    let body = (await (await call("/api/fieldops/jobs/JOB-A?limit=50", { cookie: c })).json()) as { job: any; cursors: any };
    expect(body.job.time_entries).toHaveLength(50);
    expect(body.cursors.time).not.toBeNull();
    const page1 = new Set(body.job.time_entries.map((t: any) => t.uuid));
    const body2 = (await (await call(`/api/fieldops/jobs/JOB-A?limit=50&time_cursor=${body.cursors.time}`, { cookie: c })).json()) as { job: any };
    expect(body2.job.time_entries).toHaveLength(25);
    for (const t of body2.job.time_entries) expect(page1.has(t.uuid)).toBe(false);
  });
});

// ── R7 — detail attribution contract (time-leg joins, crew assignability, viewer_personnel) ────
describe("GET /api/fieldops/jobs/:job_id — R7 attribution contract", () => {
  beforeEach(async () => {
    await provision("admin.one", "password123", "admin");
    await provision("submitter.jim", "password123", "submitter");
  });

  it("time entries carry task_description (task_id join) and a display-name-only recorded_by", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    const pid = await seedPersonnel("Alice Chen", "operator"); // username alice.chen
    const taskId = await seedTask("JOB-A", pid, "Dig footings", "open", 100);
    // "Admin One" links to the admin.one account → recorded_by_name resolves through the roster.
    await seedPersonnel("Admin One", "office");
    await seedTimeEntry("JOB-A", pid, "te-task", 200, { taskId, actor: "admin.one" });
    // Job-level entry (no task) recorded by an account with NO roster row → honest nulls:
    // task_description null, recorded_by_name null; the raw username is never exposed.
    await seedTimeEntry("JOB-A", null, "te-plain", 190, { actor: "ghost.user" });

    const c = await login("admin.one", "password123");
    const body = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: c })).json()) as { job: any };
    const byUuid = new Map(body.job.time_entries.map((t: any) => [t.uuid, t]));

    const withTask = byUuid.get("te-task") as any;
    expect(withTask.task_id).toBe(taskId);
    expect(withTask.task_description).toBe("Dig footings");
    expect(withTask.recorded_by_username).toBeUndefined(); // display-name-only (R7 review BLOCK fix)
    expect(withTask.recorded_by_name).toBe("Admin One");

    const plain = byUuid.get("te-plain") as any;
    expect(plain.task_id).toBeNull();
    expect(plain.task_description).toBeNull();
    // (R7 review BLOCK fix) display-name-only: the raw username is NEVER exposed — an unresolved
    // recorder yields NULL name and no username field at all.
    expect(plain.recorded_by_username).toBeUndefined();
    expect(plain.recorded_by_name).toBeNull(); // creator genuinely has no roster row
    expect(plain.personnel_name).toBeNull(); // job-level subject
  });

  it("detail crew rows carry account_role: submitter / manager / null for a no-login person", async () => {
    await provision("mo.manager", "password123", "manager");
    await seedJob("JOB-A", "Alpha", "active");
    // seedPersonnel derives username from the name → these link to the accounts above.
    const sub = await seedPersonnel("Submitter Jim", "laborer"); // username submitter.jim
    const mgr = await seedPersonnel("Mo Manager", "foreman"); // username mo.manager
    const noLogin = await seedPersonnel("No Login Ned", "laborer");
    await env.DB.prepare("UPDATE personnel SET username = NULL WHERE id = ?").bind(noLogin).run();
    for (const id of [sub, mgr, noLogin]) await placePersonnel(id, "JOB-A");

    const c = await login("admin.one", "password123");
    const body = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: c })).json()) as { job: any };
    const roleByName = new Map(body.job.crew.map((p: any) => [p.name, p.account_role]));
    expect(roleByName.get("Submitter Jim")).toBe("submitter");
    expect(roleByName.get("Mo Manager")).toBe("manager");
    expect(roleByName.get("No Login Ned")).toBeNull();

    // (R7 review WARN fix) account_role is org-hierarchy metadata — a plain reader (submitter,
    // cap.jobtracker.read only) gets NULL for every crew row; only assign-capable viewers see roles.
    const cSub = await login("submitter.jim", "password123");
    const subBody = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: cSub })).json()) as { job: any };
    for (const row of subBody.job.crew) expect(row.account_role).toBeNull();
  });

  it("LIST returns viewer_current_job (the viewer's own placement) — null when unlinked", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    const mine = await seedPersonnel("Admin One", "office"); // username admin.one
    await placePersonnel(mine, "JOB-A");

    const cAdmin = await login("admin.one", "password123");
    const placed = (await (await call("/api/fieldops/jobs", { cookie: cAdmin })).json()) as any;
    expect(placed.viewer_current_job).toBe("JOB-A");

    const cSub = await login("submitter.jim", "password123"); // no roster row
    const unlinked = (await (await call("/api/fieldops/jobs", { cookie: cSub })).json()) as any;
    expect(unlinked.viewer_current_job).toBeNull();
  });

  it("viewer_personnel resolves the session user's linked ACTIVE roster row; null when unlinked", async () => {
    await seedJob("JOB-A", "Alpha", "active");
    const mine = await seedPersonnel("Admin One", "office"); // username admin.one → the viewer's row

    const cAdmin = await login("admin.one", "password123");
    const withLink = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: cAdmin })).json()) as any;
    expect(withLink.viewer_personnel).toEqual({ id: mine, name: "Admin One" });

    // submitter.jim has no personnel row → null (the SPA says so instead of a phantom "Me").
    const cSub = await login("submitter.jim", "password123");
    const noLink = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: cSub })).json()) as any;
    expect(noLink.viewer_personnel).toBeNull();

    // A RETIRED (active=0) link also resolves to null — retired personnel can't take new time.
    await env.DB.prepare("UPDATE personnel SET active = 0 WHERE id = ?").bind(mine).run();
    const retired = (await (await call("/api/fieldops/jobs/JOB-A", { cookie: cAdmin })).json()) as any;
    expect(retired.viewer_personnel).toBeNull();
  });
  // ── Track 6 PR-0 — `lifecycle` on the wire ────────────────────────────────────────────────
  //
  // The list and detail payloads carried only the LEGACY `status`, which maps active→'active'
  // but BOTH inactive and archived→'closed'. With no `lifecycle` field the SPA re-derived state
  // from `status`, so every ARCHIVED job re-displayed as "Inactive" after a reload — the runbook
  // had to tell operators to "validate by effects, not the dropdown". These pin the wire contract
  // that makes the display truthful.
  it("serves lifecycle on BOTH the list row and the detail, distinguishing archived from inactive", async () => {
    await seedJob("JOB-ACT", "Active One", "active");
    await seedJob("JOB-INA", "Inactive One", "closed");
    await seedJob("JOB-ARC", "Archived One", "closed");
    // seedJob writes the legacy status; set the canonical field the two closed jobs differ on.
    await env.DB.prepare("UPDATE jobs SET lifecycle='inactive' WHERE job_id='JOB-INA'").run();
    await env.DB.prepare("UPDATE jobs SET lifecycle='archived' WHERE job_id='JOB-ARC'").run();

    const c = await login("admin.one", "password123");
    const list = (await (await call("/api/fieldops/jobs?status=all", { cookie: c })).json()) as any;
    const byId = Object.fromEntries(list.jobs.map((r: any) => [r.job_id, r]));

    expect(byId["JOB-ACT"].lifecycle).toBe("active");
    expect(byId["JOB-INA"].lifecycle).toBe("inactive");
    expect(byId["JOB-ARC"].lifecycle).toBe("archived");
    // The legacy column genuinely cannot tell the last two apart — which is the whole point.
    expect(byId["JOB-INA"].status).toBe(byId["JOB-ARC"].status);

    const detail = (await (await call("/api/fieldops/jobs/JOB-ARC", { cookie: c })).json()) as any;
    expect(detail.job.lifecycle).toBe("archived");
  });

  it("coerces an UNKNOWN stored lifecycle to 'active' (fail-SAFE, never 'archived')", async () => {
    await seedJob("JOB-ODD", "Odd One", "active");
    // The column is NOT NULL (0021), so NULL is unreachable — but nothing constrains the VALUE,
    // so a future migration or a hand-edit can leave a token this build doesn't know.
    await env.DB.prepare("UPDATE jobs SET lifecycle='mothballed' WHERE job_id='JOB-ODD'").run();

    const c = await login("admin.one", "password123");
    const detail = (await (await call("/api/fieldops/jobs/JOB-ODD", { cookie: c })).json()) as any;
    // Defaulting the other way would let an unknown value hide a live job and — once the archive
    // path exists — mark it relocatable.
    expect(detail.job.lifecycle).toBe("active");
  });
});

// ── A2: the live schedule signal (worker/schedule_rollup.ts) ─────────────────────
async function seedSchedTask(
  jobId: string,
  name: string,
  over: {
    duration?: number | null; percent?: number; finish?: string | null; start?: string | null;
    milestone?: number; active?: number;
  } = {},
): Promise<void> {
  await env.DB
    .prepare(
      "INSERT INTO job_schedule_tasks (task_uuid, job_id, name, match_key, duration_days, start_date, finish_date, percent_done, is_milestone, sort_order, active) " +
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 10, ?10)",
    )
    .bind(
      crypto.randomUUID(), jobId, name, name.toLowerCase(),
      over.duration === undefined ? null : over.duration,
      over.start ?? null, over.finish ?? null,
      over.percent ?? 0, over.milestone ?? 0, over.active ?? 1,
    )
    .run();
}

describe("GET /api/fieldops/jobs — the schedule signal (A2)", () => {
  beforeEach(async () => {
    await provision("adm.sched", "password123", "admin");
  });

  it("serves the duration-weighted percent, the late count, and the next unreached milestone", async () => {
    await seedJobRow("JOB-S");
    // Weighted mix: a 100-day task at 50% dominates four one-day punch items at 100%.
    await seedSchedTask("JOB-S", "Mechanical install", { duration: 100, percent: 50 });
    for (let i = 0; i < 4; i += 1) await seedSchedTask("JOB-S", `Punch ${i}`, { duration: 1, percent: 100 });
    // A LATE task (finish < today, <100%) and an inactive row that must not count.
    await seedSchedTask("JOB-S", "Trench east", { duration: 5, percent: 80, finish: "2020-01-01" });
    await seedSchedTask("JOB-S", "Ghost row", { percent: 0, active: 0 });
    // Milestones: one unreached in the future (the NEXT), one already done, one in the past.
    await seedSchedTask("JOB-S", "M1 energize", { milestone: 1, percent: 0, finish: "2099-01-02" });
    await seedSchedTask("JOB-S", "M0 done", { milestone: 1, percent: 100, finish: "2099-01-01" });
    await seedSchedTask("JOB-S", "M-past", { milestone: 1, percent: 0, finish: "2020-06-01" });

    const cookie = await login("adm.sched", "password123");
    const res = await get(cookie, "/api/fieldops/jobs?status=all");
    const body = (await res.json()) as { jobs: { job_id: string; schedule: { task_count: number; percent: number; late_count: number; next_milestone: { name: string; date: string } | null } | null }[] };
    const job = body.jobs.find((j) => j.job_id === "JOB-S")!;
    expect(job.schedule).not.toBeNull();
    const sched = job.schedule!;
    expect(sched.task_count).toBe(9); // all ACTIVE rows; the inactive ghost never counts
    // weightedPercent parity: floor((100*50 + 4*1*100 + 5*80 + 1*0 + 1*100 + 1*0) / (100+4+5+1+1+1))
    const expected = Math.floor((100 * 50 + 4 * 100 + 5 * 80 + 0 + 100 + 0) / 112);
    expect(sched.percent).toBe(expected);
    // Late: "Trench east" (2020 finish, 80%) and "M-past" (2020 finish, 0%).
    expect(sched.late_count).toBe(2);
    // Next milestone = the earliest UNREACHED at/after today — never the done one, never the past one.
    expect(sched.next_milestone).toEqual({ name: "M1 energize", date: "2099-01-02" });
  });

  it("serves schedule: null for a job with no active schedule tasks — the honest no-schedule state", async () => {
    await seedJobRow("JOB-N");
    await seedSchedTask("JOB-N", "Only ghost", { active: 0 });
    const cookie = await login("adm.sched", "password123");
    const res = await get(cookie, "/api/fieldops/jobs?status=all");
    const body = (await res.json()) as { jobs: { job_id: string; schedule: unknown }[] };
    expect(body.jobs.find((j) => j.job_id === "JOB-N")!.schedule).toBeNull();
  });

  it("rides the detail response too, from the same shared derivation", async () => {
    await seedJobRow("JOB-D");
    await seedSchedTask("JOB-D", "Solo task", { duration: 10, percent: 30 });
    const cookie = await login("adm.sched", "password123");
    const res = await get(cookie, "/api/fieldops/jobs/JOB-D");
    const body = (await res.json()) as { job: { schedule: { percent: number; task_count: number } | null } };
    expect(body.job.schedule).toEqual(
      expect.objectContaining({ task_count: 1, percent: 30, late_count: 0 }),
    );
  });
});
