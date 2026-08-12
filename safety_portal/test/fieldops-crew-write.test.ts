import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, p, seedJob as seedJobRow, seedPersonnel } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// Assigned-Tasks Slice T — SUBCONTRACTOR tier (migration 0027). Auth-boundary proofs:
//   • POST /api/fieldops/crew (cap.crew.create) — a subcontractor creates a NON-LOGIN person
//     auto-placed on THEIR current job, created_by stamped; unplaced → 422; account payload → rejected;
//     a subcontractor CANNOT edit/retire others (those stay cap.personnel.manage → 403).
//   • Time-route scoping — a subcontractor logs time for self ✓ / a person they created ✓ / a person
//     they did NOT create → 403; a manager/admin is UNRESTRICTED.
//   • The 'submitter' role KEY + coerceRole fail-safe default are UNCHANGED (only the display label moved).
// Real worker via cloudflare:test SELF.fetch + Miniflare D1 (migrations incl. 0027 auto-apply).
// ─────────────────────────────────────────────────────────────────────────────

const seedJob = (jobId: string, active: 0 | 1): Promise<void> => seedJobRow(jobId, { active });
// Insert an ACTIVE roster person linked to `username`, optionally placed on a job.
const seedPerson = (name: string, opts: { username?: string | null; current_job?: string | null; created_by?: string | null } = {}): Promise<number> =>
  seedPersonnel(name, opts.username ?? null, opts.current_job ?? null, { created_by: opts.created_by ?? null });
async function personRow(id: number) {
  return await env.DB.prepare("SELECT * FROM personnel WHERE id=?").bind(id).first<any>();
}

let admin: string, manager: string, sub: string;
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
  await provision("sub.sam", "password123", "submitter");
  admin = await login("admin.one", "password123");
  manager = await login("manager.mo", "password123");
  sub = await login("sub.sam", "password123");
  await seedJob("JOB-A", 1);
  await seedJob("JOB-B", 1);
});

// ── Migration 0027 grant + role-key invariance ─────────────────────────────────────────
describe("migration 0027 — cap.crew.create grant + submitter key unchanged", () => {
  it("cap.crew.create is granted to submitter + admin, NOT manager", async () => {
    const holders = (await env.DB.prepare("SELECT role_key FROM role_capabilities WHERE capability_key='cap.crew.create'").all()).results as { role_key: string }[];
    const set = new Set(holders.map((r) => r.role_key));
    expect(set.has("submitter")).toBe(true);
    expect(set.has("admin")).toBe(true);
    expect(set.has("manager")).toBe(false);
  });

  it("the subcontractor keeps all 8 base submitter caps + gains cap.crew.create + cap.schedule.mark (10 total)", async () => {
    const rows = (await env.DB.prepare("SELECT capability_key FROM role_capabilities WHERE role_key='submitter'").all()).results as { capability_key: string }[];
    const caps = new Set(rows.map((r) => r.capability_key));
    for (const c of ["cap.form.submit", "cap.form.request", "cap.time.log", "cap.jobtracker.read", "cap.equipment.field", "cap.materials.receive", "cap.tasks.own", "cap.inspection.job"]) {
      expect(caps.has(c), `submitter must keep ${c}`).toBe(true);
    }
    expect(caps.has("cap.crew.create")).toBe(true);
    // Schedule lane PR-5 (migration 0072): the field PM marks schedule progress on their own
    // job — the cap.materials.receive mirror (per-job scoped in-route).
    expect(caps.has("cap.schedule.mark")).toBe(true);
    expect(caps.size).toBe(10);
  });

  it("the role KEY stays 'submitter' + coerceRole fail-safe default is intact (garbage role → submitter, never upward)", async () => {
    // A provisioned subcontractor session reports role 'submitter' (the key is unchanged).
    const s = await call("/api/session", { cookie: sub });
    expect(((await s.json()) as { user: { role: string } }).user.role).toBe("submitter");
    // Corrupt the DB role to an UNRECOGNIZED value → requireSession's coerceRole must FAIL-SAFE to
    // 'submitter' (never upward). Seed the bogus key into `roles` first so the users.role FK holds —
    // coerceRole only recognizes 'admin'/'manager' explicitly, so this unlisted key falls through.
    await env.DB.prepare("INSERT OR IGNORE INTO roles (key, label, is_system) VALUES ('wizard','Wizard',0)").run();
    await env.DB.prepare("UPDATE users SET role='wizard' WHERE username='sub.sam'").run();
    const s2 = await call("/api/session", { cookie: sub });
    expect(s2.status).toBe(200);
    expect(((await s2.json()) as { user: { role: string } }).user.role).toBe("submitter");
  });
});

// ── POST /api/fieldops/crew — scoped crew-create ───────────────────────────────────────
describe("POST /api/fieldops/crew — subcontractor scoped crew-create", () => {
  it("gate: anon → 401, manager (no cap.crew.create) → 403", async () => {
    expect((await call("/api/fieldops/crew", { method: "POST", body: JSON.stringify({ name: "X" }) })).status).toBe(401);
    // manager holds cap.personnel.manage (the fuller route) but NOT the scoped cap.crew.create.
    expect((await p(manager, "/api/fieldops/crew", { name: "X" })).status).toBe(403);
  });

  it("a PLACED subcontractor creates a NON-LOGIN person auto-placed on THEIR job + created_by stamped", async () => {
    await seedPerson("Sam", { username: "sub.sam", current_job: "JOB-A" });
    const res = await p(sub, "/api/fieldops/crew", { name: "Helper Hank", trade: "laborer" });
    expect(res.status, await res.clone().text()).toBe(201);
    const bodyJson = (await res.json()) as { id: number; current_job: string };
    expect(bodyJson.current_job).toBe("JOB-A");
    const row = await personRow(bodyJson.id);
    expect(row.username).toBeNull(); // NON-login
    expect(row.current_job).toBe("JOB-A"); // auto-placed on the actor's job
    expect(row.created_by).toBe("sub.sam"); // provenance stamped
    // audit row landed.
    const a = (await env.DB.prepare("SELECT COUNT(*) n FROM audit_log WHERE action='crew_create'").first<{ n: number }>())!.n;
    expect(a).toBe(1);
  });

  it("a subcontractor with NO linked personnel → 422 not_placed", async () => {
    const res = await p(sub, "/api/fieldops/crew", { name: "Nope" });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { error: string }).error).toBe("not_placed");
  });

  it("a subcontractor linked but UNPLACED (current_job NULL) → 422 not_placed", async () => {
    await seedPerson("Sam", { username: "sub.sam", current_job: null });
    const res = await p(sub, "/api/fieldops/crew", { name: "Nope" });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { error: string }).error).toBe("not_placed");
  });

  it("a subcontractor CANNOT mint a login via this route — any account/username/password/role → 400, no user row", async () => {
    await seedPerson("Sam", { username: "sub.sam", current_job: "JOB-A" });
    for (const payload of [
      { name: "A", account: { username: "acct.new", password: "password123" } },
      { name: "A", username: "acct.new" },
      { name: "A", password: "password123" },
      { name: "A", role: "admin" },
    ]) {
      const res = await p(sub, "/api/fieldops/crew", payload);
      expect(res.status, JSON.stringify(payload)).toBe(400);
      expect(((await res.json()) as { error: string }).error).toBe("login_not_allowed");
    }
    expect(await env.DB.prepare("SELECT COUNT(*) n FROM users WHERE username='acct.new'").first<{ n: number }>()).toEqual({ n: 0 });
  });

  it("a subcontractor CANNOT edit / retire OTHERS — those stay cap.personnel.manage → 403", async () => {
    await seedPerson("Sam", { username: "sub.sam", current_job: "JOB-A" });
    const other = await seedPerson("Other Person", { created_by: "someone.else" });
    expect((await p(sub, `/api/fieldops/personnel/${other}/update`, { name: "Renamed" })).status).toBe(403);
    expect((await p(sub, `/api/fieldops/personnel/${other}/retire`, {})).status).toBe(403);
    expect((await p(sub, `/api/fieldops/personnel/${other}/assign`, { job_id: "JOB-B" })).status).toBe(403);
    // The person is untouched.
    expect((await personRow(other)).name).toBe("Other Person");
  });

  it("admin holds cap.crew.create too, but is likewise 422 not_placed when unplaced (route is job-scoped by the ACTOR)", async () => {
    // admin has the cap (explicit 0027 grant) but no linked/placed personnel → not_placed. (Admins use
    // the fuller personnel-create route in practice; this just proves the gate accepts admin.)
    const res = await p(admin, "/api/fieldops/crew", { name: "X" });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { error: string }).error).toBe("not_placed");
  });
});

// ── GET /api/fieldops/crew/mine — the subcontractor's loggable crew ────────────────────
describe("GET /api/fieldops/crew/mine", () => {
  it("returns the actor's own linked personnel + anyone they created (active only)", async () => {
    await seedPerson("Sam", { username: "sub.sam", current_job: "JOB-A" });
    await seedPerson("Created By Sam", { created_by: "sub.sam", current_job: "JOB-A" });
    await seedPerson("Someone Else", { created_by: "other.person" });
    const res = await call("/api/fieldops/crew/mine", { cookie: sub });
    expect(res.status).toBe(200);
    const names = ((await res.json()) as { personnel: { name: string }[] }).personnel.map((x) => x.name).sort();
    expect(names).toEqual(["Created By Sam", "Sam"]);
  });
});

// ── Time-route scoping (fieldops_time_write.ts) ────────────────────────────────────────
describe("POST /api/fieldops/time-entry — subcontractor {self, created-by} scoping", () => {
  it("subcontractor logs time for SELF (own linked personnel) → 201", async () => {
    const selfId = await seedPerson("Sam", { username: "sub.sam", current_job: "JOB-A" });
    const res = await p(sub, "/api/fieldops/time-entry", { uuid: "s1", job_id: "JOB-A", personnel_id: selfId, hours: 8 });
    expect(res.status, await res.clone().text()).toBe(201);
  });

  it("subcontractor logs time for a person THEY created → 201", async () => {
    await seedPerson("Sam", { username: "sub.sam", current_job: "JOB-A" });
    const helper = await seedPerson("Helper", { created_by: "sub.sam", current_job: "JOB-A" });
    const res = await p(sub, "/api/fieldops/time-entry", { uuid: "s2", job_id: "JOB-A", personnel_id: helper, hours: 6 });
    expect(res.status, await res.clone().text()).toBe(201);
  });

  it("subcontractor logs time for a person they did NOT create → 403 forbidden_personnel", async () => {
    await seedPerson("Sam", { username: "sub.sam", current_job: "JOB-A" });
    const stranger = await seedPerson("Stranger", { created_by: "manager.mo", current_job: "JOB-A" });
    const res = await p(sub, "/api/fieldops/time-entry", { uuid: "s3", job_id: "JOB-A", personnel_id: stranger, hours: 8 });
    expect(res.status).toBe(403);
    expect(((await res.json()) as { error: string }).error).toBe("forbidden_personnel");
    // Nothing was written.
    expect(await env.DB.prepare("SELECT COUNT(*) n FROM time_entries WHERE uuid='s3'").first<{ n: number }>()).toEqual({ n: 0 });
  });

  it("subcontractor with personnel_id NULL (job-level / self) is always allowed → 201", async () => {
    const res = await p(sub, "/api/fieldops/time-entry", { uuid: "s4", job_id: "JOB-A", hours: 8 });
    expect(res.status, await res.clone().text()).toBe(201);
  });

  it("a bogus personnel_id still 422 (existence precedes the scope check)", async () => {
    const res = await p(sub, "/api/fieldops/time-entry", { uuid: "s5", job_id: "JOB-A", personnel_id: 999999, hours: 8 });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { error: string }).error).toBe("unknown_personnel");
  });

  it("a MANAGER is UNRESTRICTED — logs time for a stranger they did not create → 201", async () => {
    const stranger = await seedPerson("Stranger", { created_by: "someone.else", current_job: "JOB-A" });
    const res = await p(manager, "/api/fieldops/time-entry", { uuid: "m1", job_id: "JOB-A", personnel_id: stranger, hours: 8 });
    expect(res.status, await res.clone().text()).toBe(201);
  });

  it("an ADMIN is UNRESTRICTED → 201", async () => {
    const stranger = await seedPerson("Stranger2", { created_by: "someone.else", current_job: "JOB-A" });
    const res = await p(admin, "/api/fieldops/time-entry", { uuid: "a1", job_id: "JOB-A", personnel_id: stranger, hours: 8 });
    expect(res.status, await res.clone().text()).toBe(201);
  });
});
