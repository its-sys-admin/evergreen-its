import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, get, post, provision, login, seedJob } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// CS4 Slice 4 Part B — vestigial-cap enforcement (cap.form.submit / cap.form.request).
//
// Migration 0013 seeded both capabilities and granted them to every role, but NO route ever
// called requireCapability on them — the portal's core submit + form-request surfaces gated on
// bare requireSession. This suite locks the enforcement added at worker/index.ts:
//   • POST /api/submit                          → cap.form.submit
//   • GET  /api/recent                          → cap.form.submit   (added 2026-08-13 — the
//     one member of the family this pass MISSED; it gated on bare requireSession alone)
//   • POST /api/submissions/:uuid/request-pdf   → cap.form.request
//   • GET  /api/submissions/:uuid/status        → cap.form.request
//   • GET  /api/submissions/:uuid/pdf           → cap.form.request
//   • GET  /api/filed                           → cap.form.request
//   • GET  /api/filed/months                    → cap.form.request
//   • POST /api/request-pdfs                    → cap.form.request
//
// TWO proofs:
//   1. NO-LOCKOUT REGRESSION — every existing role (submitter 0013, manager 0023, admin 0013
//      catch-all) holds both caps, so all three roles still pass every gated route.
//   2. THE GATE IS REAL — revoking a grant row in role_capabilities (simulating a future scoped
//      role) turns the same call into a 403; capabilities resolve per-request, so the flip is
//      effective immediately (fail-closed resolveCapabilities posture).
// Runs against the REAL worker with Miniflare D1 (migrations auto-apply).
// ─────────────────────────────────────────────────────────────────────────────

const JOB = "JOB-CAPS";

function submitBody(): Record<string, unknown> {
  return {
    job_id: JOB,
    form_code: "jha",
    work_date: "2026-07-01",
    submission_uuid: crypto.randomUUID(),
    values: { hazards: "none" },
  };
}

async function revoke(role: string, cap: string): Promise<void> {
  await env.DB.prepare("DELETE FROM role_capabilities WHERE role_key = ?1 AND capability_key = ?2")
    .bind(role, cap)
    .run();
}

async function regrant(role: string, cap: string): Promise<void> {
  await env.DB.prepare("INSERT OR IGNORE INTO role_capabilities (role_key, capability_key) VALUES (?1, ?2)")
    .bind(role, cap)
    .run();
}

let admin: string, manager: string, submitter: string;

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
  // The suite mutates role_capabilities to prove the gate — restore the seeded grants first so
  // test order can never leak a revocation.
  await regrant("submitter", "cap.form.submit");
  await regrant("submitter", "cap.form.request");
  await provision("admin.one", "password123", "admin");
  await provision("manager.mo", "password123", "manager");
  await provision("sub.sam", "password123", "submitter");
  admin = await login("admin.one", "password123");
  manager = await login("manager.mo", "password123");
  submitter = await login("sub.sam", "password123");
  await seedJob(JOB, { projectName: "Caps Test" });
});

describe("no-lockout regression — every seeded role still passes (the 0013/0023 grant matrix)", () => {
  it("POST /api/submit (cap.form.submit): submitter, manager, and admin all still file forms", async () => {
    for (const cookie of [submitter, manager, admin]) {
      const res = await post(cookie, "/api/submit", submitBody());
      expect(res.status, await res.clone().text()).toBe(200);
    }
  });

  it("the form-request surfaces (cap.form.request): all three roles still browse/request/poll", async () => {
    // Each role files its OWN submission — the per-uuid routes are requester-bound by design
    // (a different account 404s; that ownership model is orthogonal to this capability gate).
    for (const cookie of [submitter, manager, admin]) {
      const uuid = crypto.randomUUID();
      expect((await post(cookie, "/api/submit", { ...submitBody(), submission_uuid: uuid })).status).toBe(200);
      expect((await get(cookie, `/api/filed?job_id=${JOB}`)).status, "filed").toBe(200);
      expect((await get(cookie, `/api/filed/months?job_id=${JOB}`)).status, "months").toBe(200);
      expect((await get(cookie, `/api/submissions/${uuid}/status`)).status, "status").toBe(200);
      expect((await post(cookie, `/api/submissions/${uuid}/request-pdf`)).status, "request-pdf").toBe(200);
      expect((await post(cookie, "/api/request-pdfs", { uuids: [uuid] })).status, "request-pdfs").toBe(200);
    }
  });

  it("GET /api/recent (cap.form.submit): all three roles still get Amend prefill", async () => {
    const uuid = crypto.randomUUID();
    expect((await post(submitter, "/api/submit", { ...submitBody(), submission_uuid: uuid })).status).toBe(200);
    for (const cookie of [submitter, manager, admin]) {
      const res = await get(cookie, `/api/recent?job=${JOB}&form=jha&date=2026-07-01`);
      expect(res.status, await res.clone().text()).toBe(200);
      expect((await res.json<{ submission: { submission_uuid: string } | null }>()).submission?.submission_uuid).toBe(uuid);
    }
  });

  it("CROSS-ACTOR prefill is INTENDED — a colleague loads your submission, and that is the workflow", async () => {
    // Operator decision 2026-08-13, pinned here so nobody "hardens" this into an ownership
    // scope later: anyone who can submit a form may load and amend a PEER's prior submission
    // on the same job. The capability gate narrows WHO may prefill; it deliberately does not
    // narrow prefill to your own rows.
    const uuid = crypto.randomUUID();
    expect((await post(submitter, "/api/submit", { ...submitBody(), submission_uuid: uuid })).status).toBe(200);
    const res = await get(manager, `/api/recent?job=${JOB}&form=jha&date=2026-07-01`);
    expect(res.status).toBe(200);
    const body = await res.json<{ submission: { submission_uuid: string; values: unknown } | null }>();
    expect(body.submission?.submission_uuid, "a peer's submission must be prefillable").toBe(uuid);
    expect(body.submission?.values).toEqual({ hazards: "none" });
  });

  it("prefill is NOT age-bounded — a submission near the 90-day retention edge still prefills", async () => {
    // Operator decision 2026-08-13: no age window. The row is retained 90 days regardless, so
    // reaching back costs nothing; a shorter window would remove a working capability to buy
    // nothing. This fails the moment someone adds a created_at floor to /api/recent.
    const uuid = crypto.randomUUID();
    expect((await post(submitter, "/api/submit", { ...submitBody(), submission_uuid: uuid })).status).toBe(200);
    const old = Math.floor(Date.now() / 1000) - 89 * 86_400;
    await env.DB.prepare("UPDATE submissions SET created_at = ?1 WHERE submission_uuid = ?2").bind(old, uuid).run();
    const res = await get(submitter, `/api/recent?job=${JOB}&form=jha&date=2026-07-01`);
    expect(res.status).toBe(200);
    expect((await res.json<{ submission: { submission_uuid: string } | null }>()).submission?.submission_uuid).toBe(uuid);
  });

  it("anon is still 401 on every gated route (the session gate stays FIRST)", async () => {
    expect((await call("/api/submit", { method: "POST", body: JSON.stringify(submitBody()) })).status).toBe(401);
    expect((await call(`/api/recent?job=${JOB}&form=jha&date=2026-07-01`)).status).toBe(401);
    expect((await call(`/api/filed?job_id=${JOB}`)).status).toBe(401);
    expect((await call("/api/filed/months?job_id=x")).status).toBe(401);
    expect((await call("/api/submissions/some-uuid/status")).status).toBe(401);
    expect((await call("/api/submissions/some-uuid/pdf")).status).toBe(401);
    expect((await call("/api/submissions/some-uuid/request-pdf", { method: "POST" })).status).toBe(401);
    expect((await call("/api/request-pdfs", { method: "POST", body: "{}" })).status).toBe(401);
  });
});

describe("the gate is REAL — a revoked grant 403s immediately (per-request resolve, fail-closed)", () => {
  it("revoking cap.form.submit from submitter → /api/submit 403 forbidden; nothing filed", async () => {
    await revoke("submitter", "cap.form.submit");
    const res = await post(submitter, "/api/submit", submitBody());
    expect(res.status).toBe(403);
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM submissions").first<{ n: number }>();
    expect(n!.n).toBe(0);
    // …and prefill rides the SAME capability, so it 403s off the same revocation.
    expect((await get(submitter, `/api/recent?job=${JOB}&form=jha&date=2026-07-01`)).status).toBe(403);
    // …and the OTHER capability's surfaces are untouched by the revocation (independent gates).
    expect((await get(submitter, `/api/filed?job_id=${JOB}`)).status).toBe(200);
    await regrant("submitter", "cap.form.submit");
    expect((await post(submitter, "/api/submit", submitBody())).status).toBe(200); // effective next request
  });

  it("revoking cap.form.request from submitter → all six request/download routes 403; submit unaffected", async () => {
    const uuid = crypto.randomUUID();
    expect((await post(submitter, "/api/submit", { ...submitBody(), submission_uuid: uuid })).status).toBe(200);
    await revoke("submitter", "cap.form.request");
    expect((await get(submitter, `/api/filed?job_id=${JOB}`)).status).toBe(403);
    expect((await get(submitter, `/api/filed/months?job_id=${JOB}`)).status).toBe(403);
    expect((await get(submitter, `/api/submissions/${uuid}/status`)).status).toBe(403);
    expect((await get(submitter, `/api/submissions/${uuid}/pdf`)).status).toBe(403);
    expect((await post(submitter, `/api/submissions/${uuid}/request-pdf`)).status).toBe(403);
    expect((await post(submitter, "/api/request-pdfs", { uuids: [uuid] })).status).toBe(403);
    // The submit path (its own capability) still works.
    expect((await post(submitter, "/api/submit", submitBody())).status).toBe(200);
    await regrant("submitter", "cap.form.request");
    expect((await get(submitter, `/api/filed?job_id=${JOB}`)).status).toBe(200);
  });
});
