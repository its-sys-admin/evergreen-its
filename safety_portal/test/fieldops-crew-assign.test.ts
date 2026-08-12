import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, p, seedJob as seedJobRow } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// P2.6 — MANAGER tier + crew→job ASSIGNMENT (migration 0023). Two proofs:
//   1. The manager capability matrix — a mid-tier role that runs crews (roster CRUD,
//      time log, jobtracker READ, crew assign) but CANNOT mint logins, create jobs/tasks,
//      or reach the admin surface.
//   2. POST /api/fieldops/personnel/:id/assign (cap.crew.assign; Manager + admin) — sets
//      the STANDING placement (personnel.current_job), ORTHOGONAL to time logging.
// Runs against the REAL worker with Miniflare D1 (migrations incl. 0023 auto-apply).
// ─────────────────────────────────────────────────────────────────────────────

async function createPerson(cookie: string, name: string): Promise<number> {
  const res = await p(cookie, "/api/fieldops/personnel", { name });
  expect(res.status, await res.clone().text()).toBe(201);
  return ((await res.json()) as { id: number }).id;
}
async function personRow(id: number) {
  return await env.DB.prepare("SELECT * FROM personnel WHERE id=?").bind(id).first<any>();
}
async function userRow(username: string) {
  return await env.DB.prepare("SELECT * FROM users WHERE username=?").bind(username).first<any>();
}
async function assignAudits(): Promise<number> {
  return (await env.DB.prepare("SELECT COUNT(*) n FROM audit_log WHERE action='personnel_assign'").first<{ n: number }>())!.n;
}
const seedJob = (jobId: string, active: 0 | 1): Promise<void> => seedJobRow(jobId, { active });

let admin: string, manager: string, submitter: string;
beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM time_entries"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
  await provision("admin.one", "password123", "admin");
  await provision("manager.mo", "password123", "manager");
  await provision("submitter.jim", "password123", "submitter");
  admin = await login("admin.one", "password123");
  manager = await login("manager.mo", "password123");
  submitter = await login("submitter.jim", "password123");
  await seedJob("JOB-A", 1);
  await seedJob("JOB-B", 1);
});

// ── A. Manager capability matrix — the core P2.6 proof ─────────────────────────────────
describe("P2.6 — manager capability matrix", () => {
  it("manager CAN create a roster-only person (cap.personnel.manage) → 201", async () => {
    const res = await p(manager, "/api/fieldops/personnel", { name: "Roster By Manager" });
    expect(res.status, await res.clone().text()).toBe(201);
  });

  it("manager CANNOT mint a login account (role≠admin) → 403, nothing created", async () => {
    const res = await p(manager, "/api/fieldops/personnel", { name: "Acct By Manager", account: { username: "acct.bymgr", password: "password123" } });
    expect(res.status).toBe(403);
    expect(await userRow("acct.bymgr")).toBeFalsy();
  });

  it("manager CANNOT create a job (no cap.jobtracker.manage) → 403", async () => {
    expect((await p(manager, "/api/fieldops/job", { project_name: "Nope" })).status).toBe(403);
  });

  it("manager CAN create an unassigned task (cap.tasks.assign, Assigned-Tasks S1) → 201", async () => {
    // Reversal of the P2.6 "no task create" invariant (migration 0025). An unassigned task has no
    // personnel target, so the subcontractor-target guard doesn't apply — the manager just creates it.
    expect((await p(manager, "/api/fieldops/job/JOB-A/task", { description: "Dig" })).status).toBe(201);
  });

  it("manager CAN log time (cap.time.log) → 201", async () => {
    const res = await p(manager, "/api/fieldops/time-entry", { uuid: "mgr-t1", job_id: "JOB-A", hours: 8 });
    expect(res.status, await res.clone().text()).toBe(201);
  });

  it("manager CAN read the Job Tracker (cap.jobtracker.read) → 200", async () => {
    expect((await call("/api/fieldops/jobs", { cookie: manager })).status).toBe(200);
  });

  it("manager CAN assign crew (cap.crew.assign) → 200", async () => {
    const id = await createPerson(admin, "Assignable");
    expect((await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-A" })).status).toBe(200);
  });
});

// ── B. The assign route itself ─────────────────────────────────────────────────────────
describe("POST /api/fieldops/personnel/:id/assign", () => {
  it("gate: anon → 401, submitter (no cap.crew.assign) → 403, manager → 200", async () => {
    const id = await createPerson(admin, "Gated");
    expect((await call(`/api/fieldops/personnel/${id}/assign`, { method: "POST", body: JSON.stringify({ job_id: "JOB-A" }) })).status).toBe(401);
    expect((await p(submitter, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-A" })).status).toBe(403);
    expect((await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-A" })).status).toBe(200);
  });

  it("assign to an ACTIVE job → 200, current_job set, one personnel_assign audit", async () => {
    const id = await createPerson(admin, "Placeable");
    const res = await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-A" });
    expect(res.status).toBe(200);
    expect((await personRow(id)).current_job).toBe("JOB-A");
    expect(await assignAudits()).toBe(1);
  });

  it("assign to a NON-EXISTENT job → 422 unknown_job, current_job unchanged", async () => {
    const id = await createPerson(admin, "NoSuchJob");
    const res = await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-NONE" });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { error: string }).error).toBe("unknown_job");
    expect((await personRow(id)).current_job).toBeNull(); // unchanged
  });

  it("assign to an INACTIVE job → 422 unknown_job", async () => {
    await seedJob("JOB-DEAD", 0);
    const id = await createPerson(admin, "InactiveTarget");
    const res = await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-DEAD" });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { error: string }).error).toBe("unknown_job");
    expect((await personRow(id)).current_job).toBeNull();
  });

  it("unknown personnel id → 404", async () => {
    expect((await p(manager, "/api/fieldops/personnel/999999/assign", { job_id: "JOB-A" })).status).toBe(404);
  });

  it("unassign ({job_id:null}) on a placed person → 200, current_job NULL, audit incremented", async () => {
    const id = await createPerson(admin, "ToUnassign");
    expect((await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-A" })).status).toBe(200);
    expect((await personRow(id)).current_job).toBe("JOB-A");
    expect(await assignAudits()).toBe(1);

    const res = await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: null });
    expect(res.status).toBe(200);
    expect((await personRow(id)).current_job).toBeNull();
    expect(await assignAudits()).toBe(2); // a second placement-change audit row
  });

  it("invalid body → 400", async () => {
    const id = await createPerson(admin, "BadBody");
    expect((await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: 5 })).status).toBe(400); // number
    expect((await p(manager, `/api/fieldops/personnel/${id}/assign`, {})).status).toBe(400); // missing key
    const over = await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: "x".repeat(65) });
    expect(over.status).toBe(400);
    expect(((await over.json()) as { error: string }).error).toBe("invalid_job_id");
  });

  it("admin can also assign (explicit 0023 grant) → 200", async () => {
    const id = await createPerson(admin, "AdminAssigns");
    expect((await p(admin, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-B" })).status).toBe(200);
    expect((await personRow(id)).current_job).toBe("JOB-B");
  });
});

// ── C. Orthogonality — placement is independent of time logging (operator-locked) ────────
describe("P2.6 — placement is orthogonal to time logging", () => {
  it("a person placed on JOB-A may log time against JOB-B; placement stays JOB-A", async () => {
    const id = await createPerson(admin, "Orthogonal");
    expect((await p(manager, `/api/fieldops/personnel/${id}/assign`, { job_id: "JOB-A" })).status).toBe(200);
    expect((await personRow(id)).current_job).toBe("JOB-A");

    // Log time against a DIFFERENT active job for that person → 201 (unchanged time-write path).
    const t = await p(manager, "/api/fieldops/time-entry", { uuid: "orth-1", job_id: "JOB-B", personnel_id: id, hours: 6 });
    expect(t.status, await t.clone().text()).toBe(201);

    // Placement is untouched by the time entry.
    expect((await personRow(id)).current_job).toBe("JOB-A");
  });
});

// ── D. Migration 0023 grant correctness (query D1 directly) ─────────────────────────────
describe("migration 0023 — manager grant matrix", () => {
  const EXPECTED_MANAGER_CAPS = [
    "cap.form.submit",
    "cap.form.request",
    "cap.time.log",
    "cap.jobtracker.read",
    "cap.equipment.field",
    "cap.materials.receive",
    "cap.tasks.own",
    "cap.inspection.job",
    "cap.personnel.read",
    "cap.personnel.manage",
    "cap.crew.assign",
    // Assigned-Tasks S1 (migration 0025): manager gains task authority — create / assign / reassign
    // tasks (subcontractor-target guarded). Deliberate, operator-approved reversal of 0023's
    // "no task create" (see 0025 header). cap.jobtracker.manage stays withheld (job create/lifecycle).
    "cap.tasks.assign",
    // Schedule lane PR-5 (migration 0072): field mark-off of schedule tasks (%, milestone done,
    // delivered) — the crew lead marks progress on their own job (per-job scoped in-route).
    "cap.schedule.mark",
  ];
  const WITHHELD = [
    "cap.jobtracker.manage",
    "cap.admin.accounts",
    "cap.admin.formbuilder",
    "cap.submit_as",
    "cap.equipment.manage",
    "cap.materials.manage",
    "cap.checklist.manage",
  ];

  async function managerCaps(): Promise<Set<string>> {
    const rows = (await env.DB.prepare("SELECT capability_key FROM role_capabilities WHERE role_key='manager'").all()).results as { capability_key: string }[];
    return new Set(rows.map((r) => r.capability_key));
  }

  it("manager's grant is EXACTLY the 13 expected capabilities", async () => {
    const caps = await managerCaps();
    expect([...caps].sort()).toEqual([...EXPECTED_MANAGER_CAPS].sort());
  });

  it("cap.crew.assign is granted to BOTH manager and admin", async () => {
    const holders = (await env.DB.prepare("SELECT role_key FROM role_capabilities WHERE capability_key='cap.crew.assign'").all()).results as { role_key: string }[];
    const set = new Set(holders.map((r) => r.role_key));
    expect(set.has("manager")).toBe(true);
    expect(set.has("admin")).toBe(true);
  });

  it("manager is WITHHELD every privileged capability", async () => {
    const caps = await managerCaps();
    for (const w of WITHHELD) expect(caps.has(w), `manager must NOT have ${w}`).toBe(false);
  });

  it("a provisioned manager's session reports role 'manager' (coerceRole handles it)", async () => {
    const res = await call("/api/session", { cookie: manager });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { user: { role: string } };
    expect(body.user.role).toBe("manager");
  });
});
