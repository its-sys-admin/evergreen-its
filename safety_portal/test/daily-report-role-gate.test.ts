import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, g, p, seedJob, seedPersonnel } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// Daily-report ROLE gate (operator directive 2026-07-03) — the SOP daily field report is a
// MANAGER/ADMIN surface: a subcontractor (role key 'submitter') must not reach it even when
// placed on a job, and a placed ADMIN files it exactly like a placed manager. The capability
// grants can't express this (cap.tasks.own / cap.form.submit / cap.materials.receive are held
// by ALL THREE roles per 0013/0023), so the gate is the per-request SESSION ROLE — the closed
// three-value vocabulary requireSession resolves from D1 (fieldops_scope.requireDailyReportRole).
//
// The five server choke points under test:
//   • POST /api/submit for a launch:"daily-tab" family form_code (parent OR '-v%' variant —
//     the S4 family-match convention);
//   • GET  /api/fieldops/daily-form/status
//   • GET  /api/fieldops/daily-form/requirements
//   • POST /api/fieldops/expected-material/:id/receive
//   • POST /api/fieldops/expected-material/:id/flag-incident
//
// Deliberately NOT gated: GET /api/fieldops/expected-materials — the Job Tracker job-detail
// "Expected materials" section is a LIVE read-only consumer for cap.materials.receive holders
// (incl. placed submitters); only the daily-form receipt ACTIONS are daily surfaces. And
// /api/submit for a NON-daily form stays open to submitters — that is their core job.
// Runs against the REAL worker with Miniflare D1 (migrations auto-apply); per-test isolation.
// ─────────────────────────────────────────────────────────────────────────────

const DATE = "2026-07-02";

function submitBody(formCode: string): Record<string, unknown> {
  return {
    job_id: "JOB-A",
    form_code: formCode,
    work_date: DATE,
    submission_uuid: crypto.randomUUID(),
    values: { weather: "sunny" },
  };
}

async function submissionCount(): Promise<number> {
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM submissions").first<{ n: number }>();
  return row?.n ?? 0;
}

async function createExpectation(admin: string, jobId: string): Promise<number> {
  const res = await p(admin, "/api/fieldops/expected-material", { job_id: jobId, description: "Panels" });
  expect(res.status, await res.clone().text()).toBe(201);
  return ((await res.json()) as { id: number }).id;
}

let admin: string, manager: string, submitter: string;

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
  await provision("adm.a", "password123", "admin");
  await provision("mgr.mo", "password123", "manager");
  await provision("sub.sam", "password123", "submitter");
  admin = await login("adm.a", "password123");
  manager = await login("mgr.mo", "password123");
  submitter = await login("sub.sam", "password123");
  await seedJob("JOB-A");
  // EVERY role is PLACED on JOB-A — the point of the directive: placement alone must not open
  // the daily surfaces to a subcontractor, and an admin's placement is a real, reachable state
  // (a personnel row linked to the admin account + current_job).
  await seedPersonnel("Mo Manager", "mgr.mo", "JOB-A");
  await seedPersonnel("Sam Sub", "sub.sam", "JOB-A");
  await seedPersonnel("Ada Admin", "adm.a", "JOB-A");
});

describe("POST /api/submit — the daily-tab family is manager/admin only", () => {
  it("a PLACED submitter posting the daily form (current version) → 403 forbidden_role, nothing filed", async () => {
    const res = await p(submitter, "/api/submit", submitBody("daily-report-v5"));
    expect(res.status).toBe(403);
    expect(await res.text()).toBe('{"error":"forbidden_role"}');
    expect(await submissionCount()).toBe(0);
  });

  it("the family match covers the bare parent code AND any -v variant (S4 convention), without overmatching", async () => {
    for (const code of ["daily-report", "daily-report-v1", "daily-report-v99"]) {
      const res = await p(submitter, "/api/submit", submitBody(code));
      expect(res.status, code).toBe(403);
      expect(((await res.json()) as { error: string }).error, code).toBe("forbidden_role");
    }
    expect(await submissionCount()).toBe(0);
    // A LOOKALIKE prefix outside the '-v' convention is NOT the daily family — the gate must not
    // overmatch (that would silently break an unrelated future form for submitters). /api/submit
    // accepts any bounded form_code string, so the lookalike files normally.
    const lookalike = await p(submitter, "/api/submit", submitBody("daily-reporting-v1"));
    expect(lookalike.status, await lookalike.clone().text()).toBe(200);
    expect(await submissionCount()).toBe(1);
  });

  it("a placed MANAGER and a placed ADMIN both file the daily form (200 + row)", async () => {
    for (const [cookie, who] of [[manager, "manager"], [admin, "admin"]] as const) {
      const res = await p(cookie, "/api/submit", submitBody("daily-report-v5"));
      expect(res.status, who).toBe(200);
    }
    expect(await submissionCount()).toBe(2);
  });

  it("an UNPLACED admin may still file the daily form directly (sane: office files for any job)", async () => {
    await env.DB.prepare("UPDATE personnel SET current_job = NULL WHERE username = 'adm.a'").run();
    const res = await p(admin, "/api/submit", submitBody("daily-report-v5"));
    expect(res.status).toBe(200);
  });

  it("a submitter still files NON-daily forms (jha) — the gate touches only the daily family", async () => {
    const res = await p(submitter, "/api/submit", submitBody("jha-v3"));
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await submissionCount()).toBe(1);
  });

  it("the role gate runs BEFORE the job lookup — an ineligible role learns nothing about job existence", async () => {
    const res = await p(submitter, "/api/submit", { ...submitBody("daily-report-v5"), job_id: "JOB-NOPE" });
    expect(res.status).toBe(403); // not the 422 unknown_job a manager would see
    expect(((await res.json()) as { error: string }).error).toBe("forbidden_role");
  });
});

describe("the two daily-form reads — role ∈ {manager, admin} before the ownership scope", () => {
  const statusPath = `/api/fieldops/daily-form/status?job_id=JOB-A&date=${DATE}`;
  const requirementsPath = "/api/fieldops/daily-form/requirements?job_id=JOB-A";

  it("a PLACED submitter is 403 forbidden_role on both reads (placement alone no longer opens them)", async () => {
    for (const path of [statusPath, requirementsPath]) {
      const res = await g(submitter, path);
      expect(res.status, path).toBe(403);
      expect(await res.text(), path).toBe('{"error":"forbidden_role"}');
    }
  });

  it("a placed manager and a placed admin read both (200)", async () => {
    for (const cookie of [manager, admin]) {
      for (const path of [statusPath, requirementsPath]) {
        expect((await g(cookie, path)).status, path).toBe(200);
      }
    }
  });

  it("an UNPLACED admin still reads ANY job (the bypass-cap admin set is unchanged — sane direct read)", async () => {
    await env.DB.prepare("UPDATE personnel SET current_job = NULL WHERE username = 'adm.a'").run();
    for (const path of [statusPath, requirementsPath]) {
      expect((await g(admin, path)).status, path).toBe(200);
    }
  });

  it("the role gate answers before job resolution — a submitter probing an unknown job gets 403, not 404", async () => {
    const res = await g(submitter, `/api/fieldops/daily-form/requirements?job_id=JOB-NOPE`);
    expect(res.status).toBe(403);
    expect(((await res.json()) as { error: string }).error).toBe("forbidden_role");
  });
});

describe("expected-material receipt actions — daily-form surfaces; the LIST read stays open", () => {
  it("a PLACED submitter is 403 forbidden_role on receive AND flag-incident; the row is untouched", async () => {
    const id = await createExpectation(admin, "JOB-A");
    const rec = await p(submitter, `/api/fieldops/expected-material/${id}/receive`);
    expect(rec.status).toBe(403);
    expect(((await rec.json()) as { error: string }).error).toBe("forbidden_role");
    const flag = await p(submitter, `/api/fieldops/expected-material/${id}/flag-incident`, { note: "crushed" });
    expect(flag.status).toBe(403);
    const row = await env.DB.prepare("SELECT status, received_at FROM job_expected_materials WHERE id = ?1")
      .bind(id)
      .first<{ status: string; received_at: number | null }>();
    expect(row).toEqual({ status: "expected", received_at: null }); // no stamp, no flip
  });

  it("a placed manager still receives (200) — the daily form's receipt flow is intact", async () => {
    const id = await createExpectation(admin, "JOB-A");
    expect((await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 5 })).status).toBe(200);
  });

  it("the expected-materials LIST read stays open to a placed submitter (the Job Tracker section — a live consumer)", async () => {
    await createExpectation(admin, "JOB-A");
    const res = await g(submitter, "/api/fieldops/expected-materials?job_id=JOB-A");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as { expected_materials: unknown[] };
    expect(body.expected_materials).toHaveLength(1);
  });

  it("anon is 401 everywhere (the session gate stays first)", async () => {
    expect((await call("/api/submit", { method: "POST", body: JSON.stringify(submitBody("daily-report-v5")) })).status).toBe(401);
    expect((await call(`/api/fieldops/daily-form/status?job_id=JOB-A&date=${DATE}`)).status).toBe(401);
    expect((await call("/api/fieldops/daily-form/requirements?job_id=JOB-A")).status).toBe(401);
    expect((await call("/api/fieldops/expected-material/1/receive", { method: "POST" })).status).toBe(401);
    expect((await call("/api/fieldops/expected-material/1/flag-incident", { method: "POST" })).status).toBe(401);
  });
});
