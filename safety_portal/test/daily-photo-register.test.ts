import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, json } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// POST /api/internal/daily-photos/register — the SITE-PHOTOS BRIDGE (0074, Track B).
//
// The Mac's intake screens a daily report's INLINE site_photos (§34), files them to Box, then
// registers each clean one here so the WPR picker (which reads ONLY daily_photo_pool) can offer
// them. The contract under test:
//   • bearer-gated (requireInternalToken — the portal_poll privilege class);
//   • job/date/actor derived SERVER-SIDE from the submissions row (the daemon body cannot
//     place a photo on a foreign job/day); unknown submission → 404, nothing stored;
//   • rows born clean + CLAIMED + origin='site_photos' + byte-free (photo_json NULL) with the
//     'registered:v1' hmac sentinel — and therefore INVISIBLE to the pending screening queue;
//   • idempotent on (submission_uuid, box_file_id) — replays register 0 and audit nothing;
//   • bounds: 1..8 photos, box_file_id ≤200, caption clamped 300, thumb ≤ THUMB_MAX decoded.
// Runs against the REAL worker with Miniflare D1 (migrations auto-apply).
// ─────────────────────────────────────────────────────────────────────────────

const INTERNAL_BEARER = "test-internal-token"; // == PORTAL_INTERNAL_API_TOKEN in test env

const register = (body: unknown, bearer: string | null = INTERNAL_BEARER): Promise<Response> =>
  call("/api/internal/daily-photos/register", {
    method: "POST",
    body: JSON.stringify(body),
    ...(bearer === null ? {} : { bearer }),
  });

async function seedSubmission(uuid: string, jobId = "JOB-R", workDate = "2026-08-11", actor = "mgr.mo"): Promise<void> {
  await env.DB
    .prepare(
      "INSERT INTO submissions (submission_uuid, job_id, form_code, work_date, payload_json, actor_username, box_verified) " +
        "VALUES (?1, ?2, 'daily-report-v6', ?3, '{}', ?4, 1)",
    )
    .bind(uuid, jobId, workDate, actor)
    .run();
}

interface PoolRow {
  id: number; job_id: string; work_date: string; uploaded_by: string; status: string;
  photo_json: string | null; hmac: string; box_file_id: string | null;
  claimed_by_submission: string | null; origin: string; caption: string | null; thumb_b64: string | null;
}
async function poolRows(): Promise<PoolRow[]> {
  return (await env.DB.prepare("SELECT * FROM daily_photo_pool ORDER BY id").all<PoolRow>()).results;
}

const SMALL_THUMB = btoa(String.fromCharCode(0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 4));

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM daily_photo_pool"),
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
  await env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('JOB-R','Register Test',1)").run();
  await seedSubmission("sub-r1");
});

describe("register — auth + submission authority", () => {
  it("rejects a missing/wrong bearer", async () => {
    expect((await register({ submission_uuid: "sub-r1", photos: [{ box_file_id: "b1" }] }, null)).status).toBe(401);
    expect((await register({ submission_uuid: "sub-r1", photos: [{ box_file_id: "b1" }] }, "wrong")).status).toBe(401);
    expect(await poolRows()).toEqual([]);
  });

  it("404s an unknown submission and stores nothing — the row is the placement authority", async () => {
    const res = await register({ submission_uuid: "sub-nope", photos: [{ box_file_id: "b1" }] });
    expect(res.status).toBe(404);
    expect(await json<{ error: string }>(res)).toEqual({ error: "unknown_submission" });
    expect(await poolRows()).toEqual([]);
  });

  it("derives job/date/actor from the submission row, never the body", async () => {
    const res = await register({
      submission_uuid: "sub-r1",
      // Hostile extras must be ignored — there is no body field that can place the row.
      job_id: "JOB-EVIL", work_date: "1999-01-01",
      photos: [{ box_file_id: "box-1", caption: "Piles driven", thumb_b64: SMALL_THUMB }],
    });
    expect(res.status).toBe(200);
    expect(await json(res)).toEqual({ ok: true, registered: 1, skipped: 0 });
    const rows = await poolRows();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      job_id: "JOB-R", work_date: "2026-08-11", uploaded_by: "mgr.mo",
      status: "clean", photo_json: null, hmac: "registered:v1", box_file_id: "box-1",
      claimed_by_submission: "sub-r1", origin: "site_photos", caption: "Piles driven",
      thumb_b64: SMALL_THUMB,
    });
  });

  it("a bridge row never appears on the pending screening queue", async () => {
    await register({ submission_uuid: "sub-r1", photos: [{ box_file_id: "box-1" }] });
    const res = await call("/api/internal/daily-photos/pending", { bearer: INTERNAL_BEARER });
    const body = await json<{ daily_photos: unknown[] }>(res);
    expect(body.daily_photos).toEqual([]);
  });
});

describe("register — idempotency + audit gating", () => {
  it("replays register 0, skip N, and add no audit rows", async () => {
    const first = await register({
      submission_uuid: "sub-r1",
      photos: [{ box_file_id: "box-1" }, { box_file_id: "box-2", caption: "Trench" }],
    });
    expect(await json(first)).toEqual({ ok: true, registered: 2, skipped: 0 });
    const audits1 = (await env.DB.prepare("SELECT COUNT(*) n FROM audit_log WHERE action='daily_photo_register'").first<{ n: number }>())!.n;
    expect(audits1).toBe(2);

    const replay = await register({
      submission_uuid: "sub-r1",
      photos: [{ box_file_id: "box-1" }, { box_file_id: "box-2" }],
    });
    expect(await json(replay)).toEqual({ ok: true, registered: 0, skipped: 2 });
    expect(await poolRows()).toHaveLength(2);
    const audits2 = (await env.DB.prepare("SELECT COUNT(*) n FROM audit_log WHERE action='daily_photo_register'").first<{ n: number }>())!.n;
    expect(audits2).toBe(2); // changes()-gated — a no-op INSERT audits nothing
  });

  it("a partial replay registers only the new photo", async () => {
    await register({ submission_uuid: "sub-r1", photos: [{ box_file_id: "box-1" }] });
    const res = await register({
      submission_uuid: "sub-r1",
      photos: [{ box_file_id: "box-1" }, { box_file_id: "box-3" }],
    });
    expect(await json(res)).toEqual({ ok: true, registered: 1, skipped: 1 });
    expect((await poolRows()).map((r) => r.box_file_id)).toEqual(["box-1", "box-3"]);
  });
});

describe("register — bounds (Invariant 2)", () => {
  it("rejects an empty or >8 photo list", async () => {
    expect((await register({ submission_uuid: "sub-r1", photos: [] })).status).toBe(400);
    const nine = Array.from({ length: 9 }, (_, i) => ({ box_file_id: `b${i}` }));
    expect((await register({ submission_uuid: "sub-r1", photos: nine })).status).toBe(400);
    expect(await poolRows()).toEqual([]);
  });

  it("rejects a missing box_file_id, a non-object photo entry, and an oversized thumb", async () => {
    expect((await register({ submission_uuid: "sub-r1", photos: [{ caption: "no id" }] })).status).toBe(400);
    expect((await register({ submission_uuid: "sub-r1", photos: ["bare-string"] })).status).toBe(400);
    const bigThumb = btoa("x".repeat(41_000));
    const res = await register({ submission_uuid: "sub-r1", photos: [{ box_file_id: "b1", thumb_b64: bigThumb }] });
    expect(res.status).toBe(400);
    expect((await json<{ detail?: string }>(res)).detail).toBe("thumb_too_large");
    expect(await poolRows()).toEqual([]);
  });

  it("clamps an over-long caption to 300 characters", async () => {
    await register({ submission_uuid: "sub-r1", photos: [{ box_file_id: "b1", caption: "c".repeat(500) }] });
    expect((await poolRows())[0].caption).toHaveLength(300);
  });
});

describe("register — thumb validity (write-time end-to-end check)", () => {
  it("rejects a base64 string atob would throw on — the permanent-500 wedge (review 2026-08-13)", async () => {
    // "AAAAA" passes the charset regex and the length ESTIMATE, but len % 4 === 1 → atob throws.
    const res = await register({ submission_uuid: "sub-r1", photos: [{ box_file_id: "b1", thumb_b64: "AAAAA" }] });
    expect(res.status).toBe(400);
    expect((await json<{ detail?: string }>(res)).detail).toBe("thumb_invalid");
    expect(await poolRows()).toEqual([]);
  });

  it("rejects a thumb that is not a JPEG", async () => {
    const gif = btoa("GIF89a\x01\x00\x01\x00");
    const res = await register({ submission_uuid: "sub-r1", photos: [{ box_file_id: "b1", thumb_b64: gif }] });
    expect(res.status).toBe(400);
    expect((await json<{ detail?: string }>(res)).detail).toBe("thumb_not_jpeg");
  });
});
