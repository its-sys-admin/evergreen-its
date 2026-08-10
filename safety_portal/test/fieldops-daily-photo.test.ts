import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, get, post, seedJob, seedPersonnel, json } from "./helpers";
import {
  POOL_CAP_PER_DAY,
  POOL_PENDING_GLOBAL_MAX,
  parseAdditionalPhotoRefs,
} from "../worker/fieldops_daily_photos";

// ─────────────────────────────────────────────────────────────────────────────
// DR-photo-pool Slice 1 — the daily-report additional-photo POOL + the /api/submit claim.
//
//   POST /api/fieldops/daily-photo               — upload ONE photo into the (job, date) pool
//     • session + the closed-vocabulary role gate (manager/admin; submitter 403)
//     • the per-job placement scope (requireJobScope; admin caps bypass)
//     • the VERBATIM per-photo /api/submit bounds behind ONE derived body bound (413)
//     • per-(job, date, uploader) cap POOL_CAP_PER_DAY (refused rows vacate their slot)
//       + the pool-wide pending backstop POOL_PENDING_GLOBAL_MAX (503) — BOTH folded into the
//       INSERT's own WHERE (atomic; a refused insert leaves zero rows AND zero audit)
//     • HMAC-signed ("daily_photo:v1"\n<job_id>\n<work_date>\n<photo_json>); W4 one batch
//   GET  /api/fieldops/daily-photos              — the actor's OWN unclaimed rows, STATUS ONLY;
//     `amends=<uuid>` (verified same-actor/job/date) also serves THAT submission's claimed rows
//   POST /api/fieldops/daily-photo/:id/delete    — pre-submit removal (own, pending/clean,
//     unclaimed; refused = forensic marker 409; claimed = filed linkage 409)
//   /api/submit values.additional_photos         — [{pool_id, caption?}] REFERENCES: shape 400s,
//     reference validation (unknown/foreign/refused/claimed), and the ATOMIC claim
//     (claim-first; double-claim 409 leaves no submission row; same-uuid retry passes;
//     amendment transfers the filed report's claims ONLY through a server-VERIFIED amends_uuid;
//     a drop-all-refs same-uuid replace releases every prior claim).
//
// Runs against the REAL worker with Miniflare D1 (migrations auto-apply) — the same harness as
// fieldops-item-photo.test.ts.
// ─────────────────────────────────────────────────────────────────────────────

function b64(bytes: number[]): string {
  return btoa(String.fromCharCode(...bytes));
}
const JPEG_B64 = b64([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49]);
const BAD_MAGIC_B64 = b64([0x00, 0x01, 0x02, 0x03, 0x04, 0x05]);

function photo(over: Record<string, unknown> = {}): Record<string, unknown> {
  return { data: JPEG_B64, name: "extra.jpg", taken_at: "", gps: "", ...over };
}

const DATE = "2026-07-03";

const upload = (
  cookie: string,
  body: Record<string, unknown> = {},
): Promise<Response> =>
  post(cookie, "/api/fieldops/daily-photo", { job_id: "JOB-A", work_date: DATE, photo: photo(), ...body });

async function uploadOk(cookie: string, body: Record<string, unknown> = {}): Promise<number> {
  const res = await upload(cookie, body);
  expect(res.status, await res.clone().text()).toBe(201);
  const parsed = await json<{ ok: boolean; pool_id: number; status: string }>(res);
  expect(parsed.ok).toBe(true);
  expect(parsed.status).toBe("pending");
  return parsed.pool_id;
}

async function hmacHexTest(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

interface PoolRow {
  id: number;
  job_id: string;
  work_date: string;
  uploaded_by: string;
  status: string;
  photo_json: string | null;
  hmac: string;
  box_file_id: string | null;
  created_at: number;
  screened_at: number | null;
  claimed_by_submission: string | null;
}
async function poolRows(): Promise<PoolRow[]> {
  const r = await env.DB.prepare("SELECT * FROM daily_photo_pool ORDER BY id").all<PoolRow>();
  return r.results;
}
async function poolRow(id: number): Promise<PoolRow> {
  return (await env.DB.prepare("SELECT * FROM daily_photo_pool WHERE id=?1").bind(id).first<PoolRow>())!;
}
async function auditRows(action: string): Promise<{ detail: string | null }[]> {
  const r = await env.DB.prepare("SELECT detail FROM audit_log WHERE action=?1 ORDER BY id").bind(action).all<{ detail: string | null }>();
  return r.results;
}
async function submissionCount(): Promise<number> {
  return (await env.DB.prepare("SELECT COUNT(*) n FROM submissions").first<{ n: number }>())!.n;
}

/** Seed a pool row directly (cap / backstop / foreign-row fixtures). */
async function seedPoolRow(over: Partial<PoolRow> = {}): Promise<number> {
  const r = await env.DB.prepare(
    `INSERT INTO daily_photo_pool (job_id, work_date, uploaded_by, status, photo_json, hmac, claimed_by_submission)
     VALUES (?1, ?2, ?3, ?4, ?5, 'h', ?6) RETURNING id`,
  )
    .bind(
      over.job_id ?? "JOB-A",
      over.work_date ?? DATE,
      over.uploaded_by ?? "mgr.mo",
      over.status ?? "pending",
      over.photo_json === undefined ? "{}" : over.photo_json,
      over.claimed_by_submission ?? null,
    )
    .first<{ id: number }>();
  return r!.id;
}

const submit = (
  cookie: string,
  uuid: string,
  over: Record<string, unknown> = {},
): Promise<Response> =>
  post(cookie, "/api/submit", {
    job_id: "JOB-A",
    form_code: "daily-report-v6",
    work_date: DATE,
    submission_uuid: uuid,
    values: { prepared_by: "Mo Manager" },
    ...over,
  });

let admin: string, manager: string, sub: string, mgr2: string;

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM daily_photo_pool"),
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
  await provision("admin.one", "password123", "admin");
  await provision("mgr.mo", "password123", "manager");
  await provision("mgr.max", "password123", "manager");
  await provision("sub.sam", "password123", "submitter");
  admin = await login("admin.one", "password123");
  manager = await login("mgr.mo", "password123");
  mgr2 = await login("mgr.max", "password123");
  sub = await login("sub.sam", "password123");
  await seedJob("JOB-A");
  await seedJob("JOB-B");
  await seedPersonnel("Mo Manager", "mgr.mo", "JOB-A");
  await seedPersonnel("Max Manager", "mgr.max", "JOB-A");
  await seedPersonnel("Sam Sub", "sub.sam", "JOB-A");
});

describe("pool upload — role + placement matrix (the closed Role vocabulary)", () => {
  it("401 with no session; 403 for a submitter (even placed on the job)", async () => {
    expect((await call("/api/fieldops/daily-photo", { method: "POST", body: "{}" })).status).toBe(401);
    const res = await upload(sub);
    expect(res.status).toBe(403);
    expect(await poolRows()).toHaveLength(0);
  });

  it("201 for a manager placed on the job; 403 forbidden_job outside their placement", async () => {
    await uploadOk(manager);
    const other = await upload(manager, { job_id: "JOB-B" });
    expect(other.status).toBe(403);
    expect((await json<{ error: string }>(other)).error).toBe("forbidden_job");
  });

  it("201 for an admin on ANY job (the daily family's scope-bypass caps)", async () => {
    await uploadOk(admin, { job_id: "JOB-B" });
  });

  it("404 unknown job; 400 bad work_date shapes", async () => {
    expect((await upload(manager, { job_id: "NOPE" })).status).toBe(404);
    for (const bad of ["2026-7-3", "20260703", "", "2026-07-03T10:00"]) {
      const res = await upload(manager, { work_date: bad });
      expect(res.status, bad).toBe(400);
      expect((await json<{ error: string }>(res)).error).toBe("invalid_work_date");
    }
  });
});

describe("pool upload — bounds matrix (the verbatim per-photo gate behind one body bound)", () => {
  it("rejects bad magic / oversize / bad shape with the /api/submit machine reasons", async () => {
    const magic = await upload(manager, { photo: photo({ data: BAD_MAGIC_B64 }) });
    expect(magic.status).toBe(400);
    expect((await json<{ detail: string }>(magic)).detail).toBe("photo_bad_magic");

    const big = await upload(manager, { photo: photo({ data: "A".repeat(533_340) }) });
    expect(big.status).toBe(400);
    expect((await json<{ detail: string }>(big)).detail).toBe("photo_too_large");

    const shape = await upload(manager, { photo: { data: JPEG_B64, name: "x.jpg" } });
    expect(shape.status).toBe(400);
    expect((await json<{ detail: string }>(shape)).detail).toBe("invalid_photo_shape");
  });

  it("rejects a body over the single derived bound → 413 BEFORE JSON.parse (nothing queued)", async () => {
    const res = await upload(manager, { photo: photo({ data: "A".repeat(560_004) }) });
    expect(res.status).toBe(413);
    expect((await json<{ error: string }>(res)).error).toBe("photo_upload_too_large");
    expect(await poolRows()).toHaveLength(0);
  });
});

describe("pool upload — caps (FOLDED into the INSERT's own WHERE — atomic, never check-then-act)", () => {
  it("the per-(job, date, uploader) cap → 409 pool_cap_reached; refused rows vacate their slots", async () => {
    for (let i = 0; i < POOL_CAP_PER_DAY; i++) await seedPoolRow();
    const res = await upload(manager);
    expect(res.status).toBe(409);
    expect((await json<{ error: string; cap: number }>(res))).toEqual({ error: "pool_cap_reached", cap: POOL_CAP_PER_DAY });
    // THE FOLD BITES: the cap lives in the INSERT … SELECT … WHERE itself, so a seeded-at-cap
    // insert changes ZERO rows — and the changes()-gated audit (auditStmtIfChanged) lands ZERO
    // audit rows (no lying "daily_photo_add" for a refused upload).
    expect(await poolRows()).toHaveLength(POOL_CAP_PER_DAY);
    expect(await auditRows("daily_photo_add")).toHaveLength(0);

    // Another uploader, another date, another job: all outside the cap key.
    await uploadOk(mgr2);
    await uploadOk(manager, { work_date: "2026-07-02" });

    // Refused rows don't count — refusing one frees a slot for the retry.
    await env.DB.prepare("UPDATE daily_photo_pool SET status='refused', photo_json=NULL WHERE id=(SELECT MIN(id) FROM daily_photo_pool)").run();
    await uploadOk(manager);
  });

  it("the pool-wide pending backstop → 503 pool_backlogged (counts every job/uploader); zero rows + zero audit", async () => {
    for (let i = 0; i < POOL_PENDING_GLOBAL_MAX; i++) {
      await seedPoolRow({ job_id: i % 2 ? "JOB-A" : "JOB-B", uploaded_by: `ghost.${i % 7}`, work_date: "2026-06-01" });
    }
    const res = await upload(manager);
    expect(res.status).toBe(503);
    expect((await json<{ error: string }>(res)).error).toBe("pool_backlogged");
    // Same fold proof for the global-pending arm: refused insert → no row, no audit.
    expect(await poolRows()).toHaveLength(POOL_PENDING_GLOBAL_MAX);
    expect(await auditRows("daily_photo_add")).toHaveLength(0);
  });
});

describe("pool upload — W4 atomicity + HMAC + the stored record", () => {
  it("one batch: queue row + audit — and the HMAC verifies against the documented canonical string", async () => {
    const poolId = await uploadOk(manager, { photo: photo({ taken_at: "2026-07-03T10:00", gps: "33.5,-117.7" }) });
    const rows = await poolRows();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: poolId, job_id: "JOB-A", work_date: DATE, uploaded_by: "mgr.mo",
      status: "pending", box_file_id: null, screened_at: null, claimed_by_submission: null,
    });
    const stored = JSON.parse(rows[0].photo_json!) as Record<string, string>;
    expect(stored).toEqual({
      data: JPEG_B64, name: "extra.jpg", taken_at: "2026-07-03T10:00", gps: "33.5,-117.7",
      uploaded_by: "mgr.mo", // from the SESSION, not the client body
    });
    // "daily_photo:v1" \n <job_id> \n <work_date> \n <photo_json> — HMAC-SHA256 hex, the same
    // secret as submissions (vitest binding HMAC_PAYLOAD_SECRET).
    const expected = await hmacHexTest(
      "test-hmac-payload-secret",
      `daily_photo:v1\nJOB-A\n${DATE}\n${rows[0].photo_json}`,
    );
    expect(rows[0].hmac).toBe(expected);

    const audits = await auditRows("daily_photo_add");
    expect(audits).toHaveLength(1);
    const detail = JSON.parse(audits[0].detail!) as Record<string, unknown>;
    expect(detail).toMatchObject({ job_id: "JOB-A", work_date: DATE, photo_name: "extra.jpg" });
    expect(audits[0].detail).not.toContain(JPEG_B64); // never the bytes
  });
});

describe("GET /api/fieldops/daily-photos — the actor's own unclaimed rows, STATUS ONLY", () => {
  it("serves own unclaimed rows for the (job, date); excludes claimed, foreign, other-date rows; never bytes", async () => {
    const mine = await uploadOk(manager);
    await seedPoolRow({ uploaded_by: "mgr.max" }); // foreign uploader
    await seedPoolRow({ claimed_by_submission: "uuid-elsewhere" }); // claimed
    await seedPoolRow({ work_date: "2026-07-01" }); // other date
    const res = await get(manager, `/api/fieldops/daily-photos?job_id=JOB-A&work_date=${DATE}`);
    expect(res.status).toBe(200);
    const { photos } = await json<{ photos: Record<string, unknown>[] }>(res);
    expect(photos.map((p) => p.id)).toEqual([mine]);
    expect(photos[0]).toEqual({
      id: mine, status: "pending", created_at: expect.any(Number), screened_at: null, claimed: 0,
    }); // exact shape — no photo_json / hmac / bytes ever served (Option D)
  });

  it("role + scope gates: submitter 403; manager off their placement 403", async () => {
    expect((await get(sub, `/api/fieldops/daily-photos?job_id=JOB-A&work_date=${DATE}`)).status).toBe(403);
    expect((await get(manager, `/api/fieldops/daily-photos?job_id=JOB-B&work_date=${DATE}`)).status).toBe(403);
  });

  it("amends=<own filed uuid> ALSO serves rows claimed by THAT submission (claimed:1); other claims stay hidden", async () => {
    const mine = await uploadOk(manager);
    const filed = await uploadOk(manager, { photo: photo({ name: "b.jpg" }) });
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: filed }] } })).status).toBe(200);
    await seedPoolRow({ claimed_by_submission: "uuid-elsewhere" }); // NOT the amends target — hidden
    const res = await get(manager, `/api/fieldops/daily-photos?job_id=JOB-A&work_date=${DATE}&amends=uuid-1`);
    expect(res.status).toBe(200);
    const { photos } = await json<{ photos: { id: number; claimed: number }[] }>(res);
    expect(photos.map((p) => [p.id, p.claimed])).toEqual([[mine, 0], [filed, 1]]);
  });

  it("an UNVERIFIABLE amends param (unknown / foreign-actor / other-family uuid) degrades to unclaimed-only", async () => {
    const filed = await uploadOk(manager);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: filed }] } })).status).toBe(200);
    expect((await submit(mgr2, "uuid-max")).status).toBe(200); // same job/date, FOREIGN actor
    expect((await submit(manager, "uuid-old", { work_date: "2026-07-01" })).status).toBe(200); // own, other date
    for (const amends of ["uuid-ghost", "uuid-max", "uuid-old"]) {
      const res = await get(manager, `/api/fieldops/daily-photos?job_id=JOB-A&work_date=${DATE}&amends=${amends}`);
      expect(res.status, amends).toBe(200);
      const { photos } = await json<{ photos: { id: number }[] }>(res);
      // The claimed row never leaks through an unverified uuid — indistinguishable from unknown.
      expect(photos.map((p) => p.id), amends).not.toContain(filed);
    }
  });
});

describe("POST /api/fieldops/daily-photo/:id/delete — pre-submit removal", () => {
  it("deletes an OWN pending row (audited); a second delete is 404", async () => {
    const id = await uploadOk(manager);
    const res = await post(manager, `/api/fieldops/daily-photo/${id}/delete`);
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await poolRows()).toHaveLength(0);
    expect(await auditRows("daily_photo_delete")).toHaveLength(1);
    expect((await post(manager, `/api/fieldops/daily-photo/${id}/delete`)).status).toBe(404);
  });

  it("404 for a foreign uploader's row (enumeration-safe: indistinguishable from missing)", async () => {
    const id = await uploadOk(manager);
    expect((await post(mgr2, `/api/fieldops/daily-photo/${id}/delete`)).status).toBe(404);
    expect(await poolRows()).toHaveLength(1); // untouched
  });

  it("409 photo_claimed for a claimed row; 409 not_deletable for a refused forensic marker", async () => {
    const claimed = await seedPoolRow({ claimed_by_submission: "uuid-1" });
    const refused = await seedPoolRow({ status: "refused", photo_json: null });
    const c = await post(manager, `/api/fieldops/daily-photo/${claimed}/delete`);
    expect(c.status).toBe(409);
    expect((await json<{ error: string }>(c)).error).toBe("photo_claimed");
    const r = await post(manager, `/api/fieldops/daily-photo/${refused}/delete`);
    expect(r.status).toBe(409);
    expect((await json<{ error: string }>(r)).error).toBe("not_deletable");
    expect(await poolRows()).toHaveLength(2); // both survive
    expect(await auditRows("daily_photo_delete")).toHaveLength(0); // no lying audit rows
  });

  it("role gate: a submitter gets 403 even for a row id that exists", async () => {
    const id = await uploadOk(manager);
    expect((await post(sub, `/api/fieldops/daily-photo/${id}/delete`)).status).toBe(403);
  });
});

describe("/api/submit — additional_photos reference validation (shape 400s)", () => {
  it("absent and empty lists are no-ops (200, nothing claimed)", async () => {
    expect((await submit(manager, "uuid-a")).status).toBe(200);
    expect((await submit(manager, "uuid-b", { values: { additional_photos: [] } })).status).toBe(200);
  });

  it("malformed lists → 400 invalid_additional_photos with the machine reason", async () => {
    const id = await uploadOk(manager);
    const cases: [unknown, string][] = [
      ["not-a-list", "refs_not_array"],
      [[{ pool_id: id }, { pool_id: id }], "ref_duplicate_pool_id"],
      [[{ pool_id: "1" }], "ref_bad_pool_id"],
      [[{ pool_id: 0 }], "ref_bad_pool_id"],
      [[{ pool_id: id, caption: "x".repeat(301) }], "ref_bad_caption"],
      [[{ pool_id: id, extra: "key" }], "ref_unknown_key"],
      [["flat"], "ref_not_object"],
      [Array.from({ length: POOL_CAP_PER_DAY + 1 }, (_, i) => ({ pool_id: i + 1 })), "too_many_refs"],
    ];
    for (const [refs, reason] of cases) {
      const res = await submit(manager, "uuid-x", { values: { additional_photos: refs } });
      expect(res.status, reason).toBe(400);
      expect(await json<{ error: string; detail: string }>(res)).toEqual({
        error: "invalid_additional_photos", detail: reason,
      });
    }
    expect(await submissionCount()).toBe(0);
    expect((await poolRow(id)).claimed_by_submission).toBeNull(); // nothing claimed by any 400
  });
});

describe("/api/submit — reference validation against the pool (422/409)", () => {
  it("unknown, foreign-uploader, wrong-job and wrong-date refs are ONE indistinguishable 422", async () => {
    const foreign = await seedPoolRow({ uploaded_by: "mgr.max" });
    const wrongDate = await seedPoolRow({ work_date: "2026-07-01" });
    const wrongJob = await seedPoolRow({ job_id: "JOB-B" });
    for (const id of [999_999, foreign, wrongDate, wrongJob]) {
      const res = await submit(manager, "uuid-x", { values: { additional_photos: [{ pool_id: id }] } });
      expect(res.status, String(id)).toBe(422);
      expect((await json<{ error: string }>(res)).error).toBe("unknown_photo_ref");
    }
    expect(await submissionCount()).toBe(0);
  });

  it("a refused ref → 422 photo_refused (a refused photo can never enter a filed report)", async () => {
    const refused = await seedPoolRow({ status: "refused", photo_json: null });
    const res = await submit(manager, "uuid-x", { values: { additional_photos: [{ pool_id: refused }] } });
    expect(res.status).toBe(422);
    expect((await json<{ error: string }>(res)).error).toBe("photo_refused");
  });
});

describe("/api/submit — the atomic claim", () => {
  it("claims every referenced row (pending AND clean) for the submission uuid; payload carries the refs verbatim", async () => {
    const p1 = await uploadOk(manager);
    const p2 = await uploadOk(manager);
    await env.DB.prepare("UPDATE daily_photo_pool SET status='clean', photo_json=NULL, box_file_id='bx' WHERE id=?1").bind(p2).run();
    const refs = [{ pool_id: p1, caption: "trench west" }, { pool_id: p2 }];
    const res = await submit(manager, "uuid-1", { values: { prepared_by: "Mo", additional_photos: refs } });
    expect(res.status, await res.clone().text()).toBe(200);
    expect((await poolRow(p1)).claimed_by_submission).toBe("uuid-1");
    expect((await poolRow(p2)).claimed_by_submission).toBe("uuid-1");
    const row = await env.DB.prepare("SELECT payload_json FROM submissions WHERE submission_uuid='uuid-1'").first<{ payload_json: string }>();
    expect((JSON.parse(row!.payload_json) as { additional_photos: unknown }).additional_photos).toEqual(refs);
  });

  it("DOUBLE-CLAIM: a second submission referencing an already-claimed row → 409, NO submission row, other refs left unclaimed", async () => {
    const p1 = await uploadOk(manager);
    const p2 = await uploadOk(manager);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: p1 }] } })).status).toBe(200);

    const res = await submit(manager, "uuid-2", { values: { additional_photos: [{ pool_id: p2 }, { pool_id: p1 }] } });
    expect(res.status).toBe(409);
    expect((await json<{ error: string }>(res)).error).toBe("photo_already_claimed");
    expect(await submissionCount()).toBe(1); // uuid-2 was never inserted (claim-first)
    expect((await poolRow(p1)).claimed_by_submission).toBe("uuid-1"); // untouched
    expect((await poolRow(p2)).claimed_by_submission).toBeNull(); // no partial footprint
  });

  it("the guard-in-WHERE bites structurally: the claim UPDATE cannot steal a row claimed by another uuid", async () => {
    // The race the pre-check can't see (row claimed between check and batch) — proven at the
    // SQL layer: the exact claim predicate updates 0 rows for a foreign claim marker.
    const id = await seedPoolRow({ claimed_by_submission: "uuid-first" });
    const r = await env.DB.prepare(
      `UPDATE daily_photo_pool SET claimed_by_submission = ?1
       WHERE id = ?2 AND job_id = ?3 AND work_date = ?4 AND uploaded_by = ?5
         AND status IN ('pending','clean')
         AND (claimed_by_submission IS NULL OR claimed_by_submission = ?1
              OR (?6 IS NOT NULL AND claimed_by_submission = ?6))`,
    ).bind("uuid-second", id, "JOB-A", DATE, "mgr.mo", null).run();
    expect(r.meta.changes).toBe(0);
    expect((await poolRow(id)).claimed_by_submission).toBe("uuid-first");
  });

  it("the SAME-uuid lost-ACK retry re-claims idempotently (200 twice)", async () => {
    const p1 = await uploadOk(manager);
    const body = { values: { additional_photos: [{ pool_id: p1 }] } };
    expect((await submit(manager, "uuid-1", body)).status).toBe(200);
    expect((await submit(manager, "uuid-1", body)).status).toBe(200);
    expect((await poolRow(p1)).claimed_by_submission).toBe("uuid-1");
    expect(await submissionCount()).toBe(1);
  });

  it("a same-uuid REPLACE that drops a ref RELEASES the dropped row back to the pool", async () => {
    const p1 = await uploadOk(manager);
    const p2 = await uploadOk(manager);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: p1 }, { pool_id: p2 }] } })).status).toBe(200);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: p1 }] } })).status).toBe(200);
    expect((await poolRow(p1)).claimed_by_submission).toBe("uuid-1");
    expect((await poolRow(p2)).claimed_by_submission).toBeNull(); // released, re-usable / deletable
  });

  it("an AMENDMENT (fresh uuid + amends_uuid) TRANSFERS the filed report's claims", async () => {
    const p1 = await uploadOk(manager);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: p1 }] } })).status).toBe(200);
    const res = await submit(manager, "uuid-2", {
      amends_uuid: "uuid-1",
      values: { additional_photos: [{ pool_id: p1, caption: "same photo, amended report" }] },
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect((await poolRow(p1)).claimed_by_submission).toBe("uuid-2");
  });

  it("a DIFFERENT uuid without amends_uuid cannot take over a claim (the amend path is explicit)", async () => {
    const p1 = await uploadOk(manager);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: p1 }] } })).status).toBe(200);
    const res = await submit(manager, "uuid-2", { values: { additional_photos: [{ pool_id: p1 }] } });
    expect(res.status).toBe(409);
  });

  it("ADVERSARIAL: an amends_uuid naming a NONEXISTENT submission is no-transfer — the claim 409s and stands", async () => {
    const p1 = await uploadOk(manager);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: p1 }] } })).status).toBe(200);
    // "uuid-ghost" exists nowhere in submissions — a raw attacker-minted body value. Unverified,
    // it would have ridden the claim predicate verbatim and transferred uuid-1's claim.
    const res = await submit(manager, "uuid-2", {
      amends_uuid: "uuid-ghost",
      values: { additional_photos: [{ pool_id: p1 }] },
    });
    expect(res.status).toBe(409);
    expect((await json<{ error: string }>(res)).error).toBe("photo_already_claimed");
    expect((await poolRow(p1)).claimed_by_submission).toBe("uuid-1"); // untouched
    expect(await submissionCount()).toBe(1); // claim-first: the attacker's row never landed
  });

  it("ADVERSARIAL: an amends_uuid naming an OTHER-FAMILY submission (wrong job/date) cannot transfer claims", async () => {
    // A REAL filed submission — but for ANOTHER work_date, so outside this (job, date) family.
    expect((await submit(manager, "uuid-other", { work_date: "2026-07-01" })).status).toBe(200);
    // A today-row claimed by that other-family uuid (the drifted / hostile linkage the
    // server-side verification must refuse to honor).
    const p1 = await seedPoolRow({ claimed_by_submission: "uuid-other" });
    const res = await submit(manager, "uuid-2", {
      amends_uuid: "uuid-other",
      values: { additional_photos: [{ pool_id: p1 }] },
    });
    expect(res.status).toBe(409);
    expect((await json<{ error: string }>(res)).error).toBe("photo_already_claimed");
    expect((await poolRow(p1)).claimed_by_submission).toBe("uuid-other"); // untouched
  });

  it("a same-uuid re-submit with the refs key ABSENT releases every prior claim (release-all — no stranding)", async () => {
    const p1 = await uploadOk(manager);
    const p2 = await uploadOk(manager, { photo: photo({ name: "b.jpg" }) });
    expect((await submit(manager, "uuid-1", {
      values: { additional_photos: [{ pool_id: p1 }, { pool_id: p2 }] },
    })).status).toBe(200);
    // The dropped-all-refs replace: additional_photos ABSENT entirely.
    expect((await submit(manager, "uuid-1")).status).toBe(200);
    expect((await poolRow(p1)).claimed_by_submission).toBeNull();
    expect((await poolRow(p2)).claimed_by_submission).toBeNull();
    // Released rows are pre-submit pool rows again: deletable (freeing their cap slot), not
    // stuck claim markers the delete route 409s on.
    expect((await post(manager, `/api/fieldops/daily-photo/${p1}/delete`)).status).toBe(200);
  });

  it("a same-uuid re-submit with an EMPTY refs list releases the prior claims too (empty ≠ absent, same release)", async () => {
    const p1 = await uploadOk(manager);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: p1 }] } })).status).toBe(200);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [] } })).status).toBe(200);
    expect((await poolRow(p1)).claimed_by_submission).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// DR-photo-pool Slice 2 — the internal screening-queue routes the Mac portal_poll
// _service_daily_photos pass drives, + the /api/internal/pending claim manifest.
// Bearer gate = requireInternalToken (the SAME middleware instance as
// GET /api/internal/pending — the fieldops-item-photo.test.ts precedent).
//   GET  /api/internal/daily-photos/pending    — pending-only (claimed + unclaimed),
//        oldest-first, photo_json + hmac + the HMAC-covered (job_id, work_date)
//   POST /api/internal/daily-photos/:id/result — ONE atomic batch (W4): disposition
//        + photo_json=NULL (DELETE-ON-SCREEN) + changes()-gated audit; idempotent
//        (found:false no-op); the CLAIM is never touched.
//   GET  /api/internal/pending — each submission row carries `daily_photos`
//        ({id, status, box_file_id} for its claimed pool rows).
// ─────────────────────────────────────────────────────────────────────────────
const INTERNAL_BEARER = "test-internal-token"; // == PORTAL_INTERNAL_API_TOKEN

const dailyPendingGet = (init: Parameters<typeof call>[1] = {}): Promise<Response> =>
  call("/api/internal/daily-photos/pending", init);
const postDailyResult = (
  id: number | string, body: unknown, bearer: string = INTERNAL_BEARER,
): Promise<Response> =>
  call(`/api/internal/daily-photos/${id}/result`, {
    method: "POST",
    ...(bearer === "" ? {} : { bearer }),
    body: JSON.stringify(body),
  });

interface DailyQueueRow {
  id: number;
  job_id: string;
  work_date: string;
  photo_json: string;
  hmac: string;
  created_at: number;
}

describe("DR S2 — GET /api/internal/daily-photos/pending", () => {
  it("401 without the internal bearer (and with a wrong one)", async () => {
    expect((await dailyPendingGet()).status).toBe(401);
    expect((await dailyPendingGet({ bearer: "wrong-token" })).status).toBe(401);
  });

  it("serves ONLY pending rows — claimed AND unclaimed alike — oldest-first, with job/date + hmac", async () => {
    const id1 = await uploadOk(manager);
    const id2 = await uploadOk(manager, { photo: photo({ name: "b.jpg" }) });
    // Claim id2 (a claim changes ownership, not screening need — still served).
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: id2 }] } })).status).toBe(200);
    // Make id2 older so it sorts first; screen a third row OUT of the queue.
    await env.DB.prepare("UPDATE daily_photo_pool SET created_at = created_at - 100 WHERE id=?1").bind(id2).run();
    const cleanId = await seedPoolRow({ status: "clean", photo_json: null });

    const res = await dailyPendingGet({ bearer: INTERNAL_BEARER });
    expect(res.status).toBe(200);
    const { daily_photos } = await json<{ daily_photos: DailyQueueRow[] }>(res);
    expect(daily_photos.map((r) => r.id)).toEqual([id2, id1]); // clean row excluded
    expect(daily_photos.map((r) => r.id)).not.toContain(cleanId);
    expect(daily_photos[0].job_id).toBe("JOB-A");
    expect(daily_photos[0].work_date).toBe(DATE);
    expect(typeof daily_photos[0].photo_json).toBe("string");
    expect(daily_photos[0].hmac).toMatch(/^[0-9a-f]{64}$/);
  });

  it("honors the limit param", async () => {
    const id1 = await uploadOk(manager);
    await uploadOk(manager, { photo: photo({ name: "b.jpg" }) });
    const limited = await call("/api/internal/daily-photos/pending?limit=1", { bearer: INTERNAL_BEARER });
    const page = await json<{ daily_photos: DailyQueueRow[] }>(limited);
    expect(page.daily_photos.map((r) => r.id)).toEqual([id1]);
  });
});

describe("DR S2 — POST /api/internal/daily-photos/:id/result — validation matrix", () => {
  let photoId: number;
  beforeEach(async () => {
    photoId = await uploadOk(manager);
  });

  it("401 without the internal bearer (and with a wrong one) — nothing applied", async () => {
    expect((await postDailyResult(photoId, { status: "clean", box_file_id: "b1" }, "")).status).toBe(401);
    expect((await postDailyResult(photoId, { status: "clean", box_file_id: "b1" }, "wrong-token")).status).toBe(401);
    expect((await poolRow(photoId)).status).toBe("pending");
  });

  it("400 on a bad id / bad body / unknown status", async () => {
    expect((await postDailyResult("abc", { status: "clean", box_file_id: "b1" })).status).toBe(400);
    expect((await postDailyResult(0, { status: "clean", box_file_id: "b1" })).status).toBe(400);
    expect((await postDailyResult(photoId, "not-an-object")).status).toBe(400);
    expect((await postDailyResult(photoId, null)).status).toBe(400);
    for (const status of ["pending", "CLEAN", "", 7]) {
      const res = await postDailyResult(photoId, { status });
      expect(res.status, String(status)).toBe(400);
      expect((await json<{ detail: string }>(res)).detail).toBe("status");
    }
  });

  it("400 clean-without-box_file_id; 400 refused-with-box_file_id (tight contract)", async () => {
    const noBox = await postDailyResult(photoId, { status: "clean" });
    expect(noBox.status).toBe(400);
    expect((await json<{ detail: string }>(noBox)).detail).toBe("box_file_id_required");

    const withBox = await postDailyResult(photoId, { status: "refused", box_file_id: "b1" });
    expect(withBox.status).toBe(400);
    expect((await json<{ detail: string }>(withBox)).detail).toBe("box_file_id_forbidden");

    expect((await poolRow(photoId)).status).toBe("pending"); // nothing applied
  });

  it("unknown id → { ok:true, found:false } (mark_filed's benign-no-op semantics)", async () => {
    const res = await postDailyResult(999_999, { status: "clean", box_file_id: "b1" });
    expect(res.status).toBe(200);
    expect(await json<{ ok: boolean; found: boolean }>(res)).toMatchObject({ ok: true, found: false });
  });
});

describe("DR S2 — result application (W4 batch, delete-on-screen, claim untouched)", () => {
  it("CLEAN: one batch → status flip + photo_json IS NULL + box_file_id + screened_at + audit", async () => {
    const photoId = await uploadOk(manager);
    const res = await postDailyResult(photoId, { status: "clean", box_file_id: "box-42" });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await json<{ found: boolean }>(res)).toMatchObject({ ok: true, found: true });

    const row = await poolRow(photoId);
    expect(row.status).toBe("clean");
    expect(row.photo_json).toBeNull(); // DELETE-ON-SCREEN: the bytes left D1
    expect(row.box_file_id).toBe("box-42");
    expect(row.screened_at).not.toBeNull();

    const audits = await auditRows("daily_photo_result");
    expect(audits).toHaveLength(1);
    const detail = JSON.parse(audits[0].detail!) as Record<string, unknown>;
    expect(detail).toMatchObject({
      daily_photo_id: photoId, job_id: "JOB-A", work_date: DATE,
      status: "clean", box_file_id: "box-42",
    });
    // The tripwire byte-check: no photo bytes anywhere after disposition.
    expect(audits[0].detail).not.toContain(JPEG_B64);
  });

  it("REFUSED on a CLAIMED row: bytes deleted, detail audited — the CLAIM STANDS (the filed report's marker)", async () => {
    const photoId = await uploadOk(manager);
    expect((await submit(manager, "uuid-1", { values: { additional_photos: [{ pool_id: photoId }] } })).status).toBe(200);

    const res = await postDailyResult(photoId, { status: "refused", detail: "L1:magic_mismatch" });
    expect(res.status).toBe(200);
    const row = await poolRow(photoId);
    expect(row.status).toBe("refused");
    expect(row.photo_json).toBeNull(); // delete-on-screen applies to refusals too
    expect(row.box_file_id).toBeNull();
    expect(row.claimed_by_submission).toBe("uuid-1"); // the claim is NEVER touched

    const audits = await auditRows("daily_photo_result");
    const detail = JSON.parse(audits[0].detail!) as Record<string, unknown>;
    expect(detail).toMatchObject({ status: "refused", detail: "L1:magic_mismatch" });
  });

  it("idempotent re-post → found:false, NO second audit, row unchanged", async () => {
    const photoId = await uploadOk(manager);
    expect((await postDailyResult(photoId, { status: "clean", box_file_id: "box-42" })).status).toBe(200);
    const after = await poolRows();

    const again = await postDailyResult(photoId, { status: "clean", box_file_id: "box-99" });
    expect(again.status).toBe(200);
    expect(await json<{ found: boolean; status: string }>(again)).toMatchObject({
      ok: true, found: false, status: "clean",
    });
    expect(await poolRows()).toEqual(after); // box_file_id NOT clobbered to box-99
    expect(await auditRows("daily_photo_result")).toHaveLength(1);
  });
});

describe("DR S2 — GET /api/internal/pending carries the daily-photo claim manifest", () => {
  interface PendingRow {
    submission_uuid: string;
    daily_photos: { id: number; status: string; box_file_id: string | null }[];
  }

  it("a claiming submission rides with {id, status, box_file_id} for each claimed row; others get []", async () => {
    const p1 = await uploadOk(manager);
    const p2 = await uploadOk(manager, { photo: photo({ name: "b.jpg" }) });
    expect((await submit(manager, "uuid-1", {
      values: { additional_photos: [{ pool_id: p1 }, { pool_id: p2 }] },
    })).status).toBe(200);
    // Screen p1 clean (the pass-before-drain ordering: the manifest reflects post-screen state).
    expect((await postDailyResult(p1, { status: "clean", box_file_id: "box-7" })).status).toBe(200);
    // A second submission with NO pool refs.
    expect((await submit(manager, "uuid-2")).status).toBe(200);

    const res = await call("/api/internal/pending", { bearer: INTERNAL_BEARER });
    expect(res.status).toBe(200);
    const { pending } = await json<{ pending: PendingRow[] }>(res);
    const withRefs = pending.find((r) => r.submission_uuid === "uuid-1")!;
    const withoutRefs = pending.find((r) => r.submission_uuid === "uuid-2")!;
    expect(withoutRefs.daily_photos).toEqual([]);
    expect(withRefs.daily_photos).toHaveLength(2);
    const byId = new Map(withRefs.daily_photos.map((m) => [m.id, m]));
    expect(byId.get(p1)).toMatchObject({ status: "clean", box_file_id: "box-7" });
    expect(byId.get(p2)).toMatchObject({ status: "pending", box_file_id: null });
  });
});

// ── 0063: optional material-line binding ────────────────────────────────────────────────────
// The binding is established at UPLOAD and validated against the job's own rows, because the
// submission's photo refs keep their exact {pool_id, caption?} shape — a foreign key must never
// be able to ride payload_json in from the client.

/** Seed an ACTIVE expected-material line on `jobId`; returns its id + line_uuid. */
async function seedLine(
  jobId: string,
  opts: { active?: 0 | 1 } = {},
): Promise<{ id: number; line_uuid: string }> {
  const lineUuid = crypto.randomUUID();
  await env.DB.prepare(
    `INSERT INTO job_expected_materials (job_id, description, qty, unit, status, seq, active, line_uuid)
     VALUES (?,?,?,?,?,?,?,?)`,
  )
    .bind(jobId, "piles", 40, "ea", "expected", 0, opts.active ?? 1, lineUuid)
    .run();
  const row = (await env.DB.prepare("SELECT id FROM job_expected_materials WHERE line_uuid=?")
    .bind(lineUuid)
    .first<{ id: number }>())!;
  return { id: row.id, line_uuid: lineUuid };
}

async function seedReceiptEvent(lineId: number, jobId: string): Promise<number> {
  await env.DB.prepare(
    `INSERT INTO material_receipt_events (event_uuid, line_id, job_id, kind, qty, actor)
     VALUES (?,?,?,?,?,?)`,
  )
    .bind(crypto.randomUUID(), lineId, jobId, "partial", 10, "mgr.mo")
    .run();
  const row = (await env.DB.prepare(
    "SELECT id FROM material_receipt_events WHERE line_id=? ORDER BY id DESC LIMIT 1",
  )
    .bind(lineId)
    .first<{ id: number }>())!;
  return row.id;
}

describe("daily photo — material-line binding (0063)", () => {
  it("an unbound photo stores NULL binding columns and omits the keys from photo_json", async () => {
    // The backward-compatibility assertion: an ordinary day photo must serialize exactly as it
    // did before 0063, or every pre-existing signature would change meaning.
    const id = await uploadOk(manager);
    const row = (await env.DB.prepare(
      "SELECT line_uuid, receipt_event_id, photo_json FROM daily_photo_pool WHERE id=?",
    )
      .bind(id)
      .first<{ line_uuid: string | null; receipt_event_id: number | null; photo_json: string }>())!;
    expect(row.line_uuid).toBeNull();
    expect(row.receipt_event_id).toBeNull();
    const parsed = JSON.parse(row.photo_json) as Record<string, unknown>;
    expect(Object.keys(parsed).sort()).toEqual(
      ["data", "gps", "name", "taken_at", "uploaded_by"],
    );
  });

  it("binds a line, stores it in the column AND inside the HMAC-covered photo_json", async () => {
    const line = await seedLine("JOB-A");
    const id = await uploadOk(manager, { line_uuid: line.line_uuid });
    const row = (await env.DB.prepare(
      "SELECT line_uuid, photo_json, hmac FROM daily_photo_pool WHERE id=?",
    )
      .bind(id)
      .first<{ line_uuid: string; photo_json: string; hmac: string }>())!;
    expect(row.line_uuid).toBe(line.line_uuid);
    // The column is a denormalization; the AUTHORITY is the signed photo_json.
    const parsed = JSON.parse(row.photo_json) as Record<string, unknown>;
    expect(parsed.line_uuid).toBe(line.line_uuid);
    // …and the signature actually covers it: recomputing over the stored string matches.
    const expected = await hmacHexTest(
      "test-hmac-payload-secret",
      ["daily_photo:v1", "JOB-A", DATE, row.photo_json].join("\n"),
    );
    expect(row.hmac).toBe(expected);
  });

  it("422 unknown_material_line for a line on ANOTHER job (no cross-job pointer)", async () => {
    const other = await seedLine("JOB-B");
    const res = await upload(manager, { line_uuid: other.line_uuid });
    expect(res.status).toBe(422);
    expect((await json<{ error: string }>(res)).error).toBe("unknown_material_line");
    expect(await poolRows()).toHaveLength(0); // refused BEFORE the insert
  });

  it("422 unknown_material_line for a DEACTIVATED line", async () => {
    const dead = await seedLine("JOB-A", { active: 0 });
    const res = await upload(manager, { line_uuid: dead.line_uuid });
    expect(res.status).toBe(422);
    expect((await json<{ error: string }>(res)).error).toBe("unknown_material_line");
  });

  it("422 unknown_material_line for a line that does not exist at all", async () => {
    const res = await upload(manager, { line_uuid: crypto.randomUUID() });
    expect(res.status).toBe(422);
  });

  it("binds a receipt event that belongs to the line", async () => {
    const line = await seedLine("JOB-A");
    const evId = await seedReceiptEvent(line.id, "JOB-A");
    const id = await uploadOk(manager, { line_uuid: line.line_uuid, receipt_event_id: evId });
    const row = (await env.DB.prepare(
      "SELECT receipt_event_id, photo_json FROM daily_photo_pool WHERE id=?",
    )
      .bind(id)
      .first<{ receipt_event_id: number; photo_json: string }>())!;
    expect(row.receipt_event_id).toBe(evId);
    expect((JSON.parse(row.photo_json) as Record<string, unknown>).receipt_event_id).toBe(evId);
  });

  it("422 unknown_receipt_event when the event belongs to a DIFFERENT line", async () => {
    // Otherwise a photo files against line A while pointing at an event on line B — reading as
    // evidence for a delivery it has nothing to do with.
    const lineA = await seedLine("JOB-A");
    const lineB = await seedLine("JOB-A");
    const evOnB = await seedReceiptEvent(lineB.id, "JOB-A");
    const res = await upload(manager, { line_uuid: lineA.line_uuid, receipt_event_id: evOnB });
    expect(res.status).toBe(422);
    expect((await json<{ error: string }>(res)).error).toBe("unknown_receipt_event");
  });

  it("400 receipt_event_without_line — an event alone skips the only ownership check", async () => {
    const line = await seedLine("JOB-A");
    const evId = await seedReceiptEvent(line.id, "JOB-A");
    const res = await upload(manager, { receipt_event_id: evId });
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("receipt_event_without_line");
  });

  it("400 on a malformed line_uuid / receipt_event_id (shape before semantics)", async () => {
    expect((await upload(manager, { line_uuid: 42 })).status).toBe(400);
    expect((await upload(manager, { line_uuid: "x".repeat(65) })).status).toBe(400);
    const line = await seedLine("JOB-A");
    expect(
      (await upload(manager, { line_uuid: line.line_uuid, receipt_event_id: "1" })).status,
    ).toBe(400);
  });

  it("the submission ref shape is UNCHANGED — a line_uuid on a ref is still rejected", async () => {
    // The binding lives at upload precisely so this stays closed. If this ever loosens, an
    // attacker-controlled foreign key rides payload_json into a filed submission.
    const refs = parseAdditionalPhotoRefs({
      additional_photos: [{ pool_id: 1, line_uuid: "anything" }],
    });
    expect(refs).toBe("ref_unknown_key");
  });
});
