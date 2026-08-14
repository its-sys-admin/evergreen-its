import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { provision, login, p, seedJob } from "./helpers";
import { MAX_VALUES_DEPTH, jsonDepthExceeds } from "../worker/json_depth";

// ─────────────────────────────────────────────────────────────────────────────
// Submit-time JSON depth bound (portal-worker-security-reviewer finding (b), 2026-08-13).
//
// SQLite's JSON1 parser raises `malformed JSON` on json_type(payload_json, …) when the STORED
// document nests deeply enough (~500+ levels, a ~4KB body) — before any per-element WHERE guard
// can run — so one stored hostile submission poisons every json_extract-bearing report query for
// that job/week, persistently, for both the Mac compile and the office screen. The only durable
// fix is refusing the payload at the write boundary; these tests pin that gate.
// Runs against the REAL worker with Miniflare D1 (migrations auto-apply); per-test isolation.
// ─────────────────────────────────────────────────────────────────────────────

/** A values object whose nesting is `depth` levels (root object = level 1). */
function nested(depth: number): Record<string, unknown> {
  let v: unknown = "leaf";
  for (let i = 0; i < depth - 1; i += 1) v = [v];
  return { field: v };
}

function submitBody(values: Record<string, unknown>): Record<string, unknown> {
  return {
    job_id: "JOB-A",
    form_code: "jha-v3",
    work_date: "2026-08-08",
    submission_uuid: crypto.randomUUID(),
    values,
  };
}

let submitter: string;

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
  await provision("sub.deep", "password123", "submitter");
  submitter = await login("sub.deep", "password123");
  await seedJob("JOB-A");
});

describe("POST /api/submit — values depth bound", () => {
  it("rejects a deeply-nested values payload before anything is stored", async () => {
    const res = await p(submitter, "/api/submit", submitBody(nested(600)));
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "invalid_submission", detail: "values_too_deep" });
    const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM submissions").first<{ n: number }>();
    expect(row?.n ?? 0).toBe(0);
  });

  it("rejects right at the bound — a container AT MAX_VALUES_DEPTH is already too deep", async () => {
    const res = await p(submitter, "/api/submit", submitBody(nested(MAX_VALUES_DEPTH + 1)));
    expect(res.status).toBe(400);
    expect(((await res.json()) as { detail?: string }).detail).toBe("values_too_deep");
  });

  it("accepts a realistic payload and the stored row still parses under json_type", async () => {
    // Realistic worst case: section → repeating-table rows → cell objects (~4 levels).
    const values = {
      worker_acknowledgement: [
        { worker_name: "Devin Jones", company: "Evergreen", signature: "M 0 0" },
      ],
      hazard_analysis: [{ task: "Trenching", hazards: "Cave-in", mitigation: "Shoring" }],
    };
    const res = await p(submitter, "/api/submit", submitBody(values));
    expect(res.status, await res.clone().text()).toBe(200);
    // The poisoning predicate itself: json_type over the stored document must not raise.
    const probe = await env.DB
      .prepare("SELECT json_type(payload_json, '$.worker_acknowledgement') AS t FROM submissions")
      .first<{ t: string }>();
    expect(probe?.t).toBe("array");
  });

  it("jsonDepthExceeds: scalar leaves AT the bound are fine, containers are not; hostile depth cannot blow the checker's own stack", () => {
    expect(jsonDepthExceeds(nested(MAX_VALUES_DEPTH), MAX_VALUES_DEPTH)).toBe(true); // container at 24
    expect(jsonDepthExceeds(nested(MAX_VALUES_DEPTH - 1), MAX_VALUES_DEPTH)).toBe(false);
    expect(jsonDepthExceeds(nested(50_000), MAX_VALUES_DEPTH)).toBe(true); // iterative, no RangeError
  });
});
