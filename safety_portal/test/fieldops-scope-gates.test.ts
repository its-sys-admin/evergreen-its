import { env, SELF } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { provision, login } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// fieldops_scope.ts (optimization slice 3, finding #2) — the extracted per-job ownership-scope
// machinery (requireJob / resolveActorPersonnel / requireJobScope) exercised through the three
// surfaces that share it:
//   • GET /api/fieldops/daily-form/status          (bypass: jobtracker.manage / checklist.manage)
//   • GET /api/fieldops/daily-form/requirements    (bypass: jobtracker.manage / checklist.manage)
//   • GET /api/fieldops/expected-materials         (bypass: jobtracker.manage / MATERIALS.manage)
//
// The point under test: the bypass-cap sets are INTENTIONALLY DIVERGENT and must stay exactly as
// they were before the extraction — cap.materials.manage opens ONLY the expected-materials read,
// cap.checklist.manage ONLY the two daily-form reads, cap.jobtracker.manage all three. Custom D1
// roles isolate single capabilities (the built-in submitter/manager/admin tiers hold the caps only
// in bundles). Error shapes must be byte-identical to the pre-extraction inline gates.
//
// Directive 2026-07-03 (daily-report role gating): the two daily-form reads now ALSO require
// role ∈ {manager, admin} BEFORE the scope check (fieldops_scope.requireDailyReportRole), so the
// shared-scope actors here are MANAGERS (they exercise the scope machinery on all three paths;
// a submitter would 403 forbidden_role on the daily-form reads before ever reaching the scope).
// The role gate itself — incl. the submitter matrix — is covered in daily-report-role-gate.test.ts.
// Runs against the REAL worker with Miniflare D1 (migrations auto-apply); per-test isolation.
// ─────────────────────────────────────────────────────────────────────────────

const BASE = "https://portal.test";
type Init = RequestInit & { cookie?: string; bearer?: string };

function call(path: string, init: Init = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.cookie) headers.set("Cookie", init.cookie);
  if (init.bearer) headers.set("Authorization", `Bearer ${init.bearer}`);
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  return SELF.fetch(BASE + path, { ...init, headers });
}
const g = (cookie: string, path: string) => call(path, { cookie });

async function seedJob(jobId: string): Promise<void> {
  await env.DB.prepare("INSERT INTO jobs (job_id, project_name, active, status, created_at) VALUES (?,?,1,'active',?)")
    .bind(jobId, `Project ${jobId}`, 1_700_000_000).run();
}
async function seedPersonnel(name: string, username: string | null, currentJob: string | null): Promise<void> {
  await env.DB.prepare("INSERT INTO personnel (name, username, current_job, active) VALUES (?,?,?,1)")
    .bind(name, username, currentJob).run();
}

// The three bypass caps under test are cap.materials.manage / cap.checklist.manage /
// cap.jobtracker.manage. None is part of the built-in MANAGER grant matrix (0023/0025), so
// granting exactly one to 'manager' isolates it. A CUSTOM role can't do this: requireSession
// coerces any unknown role key to 'submitter' (coerceRole fails safe), so only a built-in tier's
// matrix can be varied. resolveCapabilities reads role_capabilities fresh per request — a grant is
// effective on the next call, no re-login needed.

/** Grant exactly `caps` (bypass caps above) to the built-in manager role — on top of its stock
 *  matrix, which already carries the cap.tasks.own + cap.materials.receive the routes' capability
 *  gates require. beforeEach strips all three, so each test starts from the stock matrix. */
async function grantManagerCaps(caps: readonly string[]): Promise<void> {
  for (const cap of caps) {
    await env.DB.prepare("INSERT OR IGNORE INTO role_capabilities (role_key, capability_key) VALUES ('manager', ?)")
      .bind(cap).run();
  }
}

const DATE = "2026-07-02";
const statusPath = (job: string) => `/api/fieldops/daily-form/status?job_id=${encodeURIComponent(job)}&date=${DATE}`;
const requirementsPath = (job: string) => `/api/fieldops/daily-form/requirements?job_id=${encodeURIComponent(job)}`;
const materialsPath = (job: string) => `/api/fieldops/expected-materials?job_id=${encodeURIComponent(job)}`;
const ALL_THREE = [statusPath, requirementsPath, materialsPath];

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
    // Strip any per-test bypass grants so each test starts from the stock manager matrix.
    env.DB.prepare(
      "DELETE FROM role_capabilities WHERE role_key='manager' AND capability_key IN ('cap.materials.manage','cap.checklist.manage','cap.jobtracker.manage')",
    ),
  ]);
  await seedJob("JOB-A");
  await seedJob("JOB-B");
});

describe("shared ownership scope — placed vs foreign vs unlinked (all three surfaces)", () => {
  it("a PLACED non-admin reads their own job (200) and is refused a foreign job with the byte-identical forbidden_job shape", async () => {
    await provision("mgr.sam", "password123", "manager"); // manager: passes the daily-report role gate; holds NO bypass caps
    await seedPersonnel("Sam Manager", "mgr.sam", "JOB-A");
    const sam = await login("mgr.sam", "password123");
    for (const path of ALL_THREE) {
      expect((await g(sam, path("JOB-A"))).status, path("JOB-A")).toBe(200);
      const res = await g(sam, path("JOB-B"));
      expect(res.status, path("JOB-B")).toBe(403);
      // Byte-identical error body — the proof the extraction preserved the inline gates' shape.
      expect(await res.text()).toBe('{"error":"forbidden_job"}');
    }
  });

  it("an account with NO linked active personnel row is forbidden_job on all three (even its own former job)", async () => {
    await provision("mgr.solo", "password123", "manager");
    const solo = await login("mgr.solo", "password123"); // no personnel row at all
    for (const path of ALL_THREE) {
      const res = await g(solo, path("JOB-A"));
      expect(res.status, path("JOB-A")).toBe(403);
      expect(((await res.json()) as { error: string }).error).toBe("forbidden_job");
    }
  });

  it("shared requireJob: oversize job_id → 400 invalid_job_id; unknown job → 404 not_found (all three)", async () => {
    await provision("mgr.sam", "password123", "manager");
    await seedPersonnel("Sam Manager", "mgr.sam", "JOB-A");
    const sam = await login("mgr.sam", "password123");
    const oversize = "J".repeat(65);
    for (const path of ALL_THREE) {
      const bad = await g(sam, path(oversize));
      expect(bad.status, path(oversize)).toBe(400);
      expect(((await bad.json()) as { error: string }).error).toBe("invalid_job_id");
      const unknown = await g(sam, path("JOB-NOPE"));
      expect(unknown.status).toBe(404);
      expect(((await unknown.json()) as { error: string }).error).toBe("not_found");
    }
  });
});

describe("divergent bypass-cap sets — preserved exactly through the extraction", () => {
  it("cap.materials.manage bypasses NOTHING — it says WHAT you may edit, not WHOSE job", async () => {
    // Until 2026-08-24 this cap was a member of expected-materials' SCOPE_BYPASS_CAPS, on the
    // rationale that the office editor of the list may naturally see any job's list. Migration
    // 0079 granted the cap to the whole `manager` role, at which point that rationale stopped
    // holding: leaving it in the bypass set would have silently opened EVERY job's materials to
    // six crew leads. It was removed instead — a no-op for the office, who reach other jobs via
    // cap.jobtracker.manage (asserted below).
    await provision("mgr.mat", "password123", "manager");
    const matOffice = await login("mgr.mat", "password123"); // unplaced — no personnel row, so ONLY a bypass cap can open a job
    await grantManagerCaps(["cap.materials.manage"]);
    for (const path of [materialsPath, statusPath, requirementsPath]) {
      const res = await g(matOffice, path("JOB-B"));
      expect(res.status).toBe(403);
      expect(await res.text()).toBe('{"error":"forbidden_job"}');
    }
  });

  it("cap.jobtracker.manage is what opens another job's expected-materials", async () => {
    await provision("mgr.jt", "password123", "manager");
    const jtOffice = await login("mgr.jt", "password123"); // unplaced
    await grantManagerCaps(["cap.jobtracker.manage"]);
    expect((await g(jtOffice, materialsPath("JOB-B"))).status).toBe(200);
  });

  it("cap.checklist.manage bypasses ONLY the daily-form reads (403 forbidden_job on expected-materials)", async () => {
    await provision("mgr.chk", "password123", "manager");
    const chkOffice = await login("mgr.chk", "password123"); // unplaced
    await grantManagerCaps(["cap.checklist.manage"]);
    expect((await g(chkOffice, statusPath("JOB-B"))).status).toBe(200);
    expect((await g(chkOffice, requirementsPath("JOB-B"))).status).toBe(200);
    const mats = await g(chkOffice, materialsPath("JOB-B"));
    expect(mats.status).toBe(403);
    expect(await mats.text()).toBe('{"error":"forbidden_job"}');
  });

  it("cap.jobtracker.manage is a member of BOTH bypass sets (200 on all three, unplaced)", async () => {
    await provision("mgr.jt", "password123", "manager");
    const jtOffice = await login("mgr.jt", "password123"); // unplaced
    await grantManagerCaps(["cap.jobtracker.manage"]);
    for (const path of ALL_THREE) {
      expect((await g(jtOffice, path("JOB-B"))).status, path("JOB-B")).toBe(200);
    }
  });

  it("a stock manager holds NONE of the bypass caps — foreign job stays forbidden on all three", async () => {
    await provision("mgr.plain", "password123", "manager");
    await seedPersonnel("Plain Manager", "mgr.plain", "JOB-A");
    const plain = await login("mgr.plain", "password123");
    for (const path of ALL_THREE) {
      expect((await g(plain, path("JOB-A"))).status, path("JOB-A")).toBe(200); // own placement
      const res = await g(plain, path("JOB-B"));
      expect(res.status, path("JOB-B")).toBe(403);
      expect(((await res.json()) as { error: string }).error).toBe("forbidden_job");
    }
  });

  it("admin (all caps) reads any job on all three surfaces", async () => {
    await provision("admin.one", "password123", "admin");
    const admin = await login("admin.one", "password123"); // unplaced
    for (const path of ALL_THREE) {
      expect((await g(admin, path("JOB-A"))).status, path("JOB-A")).toBe(200);
      expect((await g(admin, path("JOB-B"))).status, path("JOB-B")).toBe(200);
    }
  });
});
