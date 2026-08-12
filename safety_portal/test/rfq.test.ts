import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, p, g, json } from "./helpers";
import { hmacHex } from "../worker/hmac";
import { canonicalRfqJson, rfqCanonicalString, type RfqRow, type RfqLine } from "../worker/rfq";

// ─────────────────────────────────────────────────────────────────────────────
// RFQ composer R1 (ADR-0004) — worker/rfq.ts + migration 0056.
//
// Coverage: draft-create atomicity (parent + lines + vendor rows + audit in one
// batch); vendor_key validation (unknown / inactive / malformed / duplicate /
// over-cap → 400 with NOTHING written); draft-only full-replace update; generate
// allocation (per-job seq increments; the UNIQUE(rfq_number) backstop maps a
// planted collision to 409 rfq_number_conflict with the draft untouched); the
// stored rfq:v1 HMAC recomputed byte-for-byte with SORTED vendor_keys and ZERO
// price keys in the canonical; bearer isolation THREE ways (the po / estimate /
// sibling tokens all 401 on the rfq internal tier AND the rfq token opens
// neither sibling tier — red-team #1); mark-filed per-vendor in-WHERE +
// all-filed derivation + idempotent replay; status-sync forward-only (regression
// refused, per-vendor sent/responded + the derived partially_sent/sent rfq
// status); cancel; and every audit row batched with its mutation (W4).
// ─────────────────────────────────────────────────────────────────────────────

const RFQ_BEARER = "test-rfq-token";
const PO_BEARER = "test-po-token";
const EST_BEARER = "test-estimate-token";
const INTERNAL_BEARER = "test-internal-token";
const FIELDOPS_BEARER = "test-fieldops-token";
const ADMIN_BEARER = "test-admin-token";
const HMAC_SECRET = "test-hmac-payload-secret";

async function seedVendor(vendorKey: string, active = 1): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO po_vendors (vendor_key, vendor_name, contact_email, region, supply_categories, active, origin, sync_state, mirror_version, mirrored_version) " +
      "VALUES (?1,?2,?3,?4,?5,?6,'smartsheet','synced',0,0)",
  )
    .bind(vendorKey, `Vendor ${vendorKey}`, "sales@vendor.example", "Midwest", '["racking"]', active)
    .run();
}

// Vendor keys deliberately supplied UNSORTED — the canonical must sort them.
function draftBody(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    job_no: "2026.001",
    job_name: "Sunrise Solar",
    ship_to_name: "Sunrise Solar Site",
    ship_to_address: "100 Array Way",
    ship_to_state: "IL",
    scope_text: "Supply only: racking + hardware per plan set E-201.",
    due_date: "2026-08-01",
    line_items: [
      { part_number: "RK-100", description: "Rail 100", qty: 10, unit: "ea", line_note: "mill finish" },
      { description: "Freight to site" }, // qty null = "quote per unit"
    ],
    vendor_keys: ["VEN-000002", "VEN-000001"],
    ...over,
  };
}

async function rfqRow(id: number): Promise<Record<string, unknown> | null> {
  return await env.DB.prepare("SELECT * FROM rfqs WHERE id=?1").bind(id).first();
}
async function vendorRows(id: number): Promise<Record<string, unknown>[]> {
  const { results } = await env.DB
    .prepare("SELECT * FROM rfq_vendors WHERE rfq_id=?1 ORDER BY vendor_key")
    .bind(id)
    .all<Record<string, unknown>>();
  return results ?? [];
}
async function lineRows(id: number): Promise<Record<string, unknown>[]> {
  const { results } = await env.DB
    .prepare("SELECT * FROM rfq_line_items WHERE rfq_id=?1 ORDER BY position")
    .bind(id)
    .all<Record<string, unknown>>();
  return results ?? [];
}
async function auditCount(action: string): Promise<number> {
  const r = await env.DB
    .prepare("SELECT COUNT(*) AS n FROM audit_log WHERE action=?1")
    .bind(action)
    .first<{ n: number }>();
  return r?.n ?? 0;
}
async function countAll(table: "rfqs" | "rfq_line_items" | "rfq_vendors"): Promise<number> {
  const r = await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${table}`).first<{ n: number }>();
  return r?.n ?? 0;
}

/** create → generate: a queued two-vendor RFQ. */
async function makeQueued(admin: string, over: Record<string, unknown> = {}): Promise<{ id: number; rfq_number: string }> {
  const created = await p(admin, "/api/po/rfqs", draftBody(over));
  expect(created.status, await created.clone().text()).toBe(201);
  const { id } = await json<{ id: number }>(created);
  const gen = await p(admin, `/api/po/rfqs/${id}/generate`, {});
  expect(gen.status, await gen.clone().text()).toBe(200);
  const { rfq_number } = await json<{ rfq_number: string }>(gen);
  return { id, rfq_number };
}

function markFiled(body: Record<string, unknown>): Promise<Response> {
  return call("/api/po/rfqs/internal/mark-filed", {
    method: "POST", bearer: RFQ_BEARER, body: JSON.stringify(body),
  });
}
function statusSync(body: Record<string, unknown>): Promise<Response> {
  return call("/api/po/rfqs/internal/status-sync", {
    method: "POST", bearer: RFQ_BEARER, body: JSON.stringify(body),
  });
}
/** File every vendor of a queued rfq (→ 'generated'). */
async function fileAll(id: number): Promise<void> {
  const res = await markFiled({
    rfq_id: id,
    vendor_results: [
      { vendor_key: "VEN-000001", box_pdf_file_id: "bx-p1", box_form_file_id: "bx-f1", review_row_id: "row-1" },
      { vendor_key: "VEN-000002", box_pdf_file_id: "bx-p2", box_form_file_id: "bx-f2", review_row_id: "row-2" },
    ],
  });
  expect(res.status, await res.clone().text()).toBe(200);
  expect((await json<{ all_filed: boolean }>(res)).all_filed).toBe(true);
}

let admin: string, submitter: string;
beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM rfq_vendors"),
    env.DB.prepare("DELETE FROM rfq_line_items"),
    env.DB.prepare("DELETE FROM rfqs"),
    env.DB.prepare("DELETE FROM po_vendors"),
  ]);
  await provision("admin.rfq", "password123", "admin");
  await provision("submitter.rfq", "password123", "submitter");
  admin = await login("admin.rfq", "password123");
  submitter = await login("submitter.rfq", "password123");
  await seedVendor("VEN-000001");
  await seedVendor("VEN-000002");
  await seedVendor("VEN-000003", 0); // inactive — must be refused
});

// ── Capability gate (browser surface) ─────────────────────────────────────────
describe("cap.po.manage gate", () => {
  it("403s a submitter and 401s no-session on create/list/detail/generate/cancel", async () => {
    expect((await p(submitter, "/api/po/rfqs", draftBody())).status).toBe(403);
    expect((await g(submitter, "/api/po/rfqs")).status).toBe(403);
    expect((await g(submitter, "/api/po/rfqs/1")).status).toBe(403);
    expect((await p(submitter, "/api/po/rfqs/1/generate", {})).status).toBe(403);
    expect((await p(submitter, "/api/po/rfqs/1/cancel", {})).status).toBe(403);
    expect((await call("/api/po/rfqs")).status).toBe(401);
    expect((await call("/api/po/rfqs", { method: "POST", body: "{}" })).status).toBe(401);
  });
});

// ── Draft create ──────────────────────────────────────────────────────────────
describe("draft create", () => {
  it("201s: parent + server-positioned lines + pending vendor rows + audit land atomically", async () => {
    const res = await p(admin, "/api/po/rfqs", draftBody());
    expect(res.status, await res.clone().text()).toBe(201);
    const { id } = await json<{ id: number }>(res);

    const row = (await rfqRow(id))!;
    expect(row.status).toBe("draft");
    expect(row.rfq_number).toBeNull(); // NULL until generate
    expect(row.hmac).toBeNull();
    expect(row.job_no).toBe("2026.001");
    expect(row.scope_text).toBe("Supply only: racking + hardware per plan set E-201.");
    expect(row.due_date).toBe("2026-08-01");
    expect(row.created_by).toBe("admin.rfq");

    const lines = await lineRows(id);
    expect(lines.map((l) => l.position)).toEqual([1, 2]); // server-assigned array order
    expect(lines[0].description).toBe("Rail 100");
    expect(lines[0].qty).toBe(10);
    expect(lines[1].qty).toBeNull();

    const vend = await vendorRows(id);
    expect(vend.map((v) => v.vendor_key)).toEqual(["VEN-000001", "VEN-000002"]);
    expect(vend.every((v) => v.status === "pending")).toBe(true);
    expect(await auditCount("rfq_draft_create")).toBe(1);
  });

  it("400s unknown / inactive / malformed / duplicate / over-cap vendor_keys — and writes NOTHING", async () => {
    const cases: [unknown, string][] = [
      [["VEN-000001", "VEN-999999"], "unknown_vendor"], // unknown
      [["VEN-000003"], "unknown_vendor"], // exists but INACTIVE
      [["not-a-key"], "invalid_vendor_keys"],
      [["VEN-000001", "VEN-000001"], "duplicate_vendor_key"],
      [[], "invalid_vendor_keys"],
      [Array.from({ length: 13 }, (_, i) => `VEN-${String(i + 1).padStart(6, "0")}`), "invalid_vendor_keys"],
    ];
    for (const [vendor_keys, error] of cases) {
      const res = await p(admin, "/api/po/rfqs", draftBody({ vendor_keys }));
      expect(res.status, JSON.stringify(vendor_keys)).toBe(400);
      expect((await json<{ error: string }>(res)).error).toBe(error);
    }
    expect(await countAll("rfqs")).toBe(0);
    expect(await countAll("rfq_line_items")).toBe(0);
    expect(await countAll("rfq_vendors")).toBe(0);
    expect(await auditCount("rfq_draft_create")).toBe(0);
  });

  it("400s bad shapes: job_no, empty/oversize description, bad qty, bad due_date, no lines", async () => {
    expect((await p(admin, "/api/po/rfqs", draftBody({ job_no: "26.1" }))).status).toBe(400);
    expect((await p(admin, "/api/po/rfqs", draftBody({ line_items: [] }))).status).toBe(400);
    expect((await p(admin, "/api/po/rfqs", draftBody({ line_items: [{ description: "" }] }))).status).toBe(400);
    expect(
      (await p(admin, "/api/po/rfqs", draftBody({ line_items: [{ description: "x".repeat(513) }] }))).status,
    ).toBe(400);
    expect(
      (await p(admin, "/api/po/rfqs", draftBody({ line_items: [{ description: "ok", qty: -1 }] }))).status,
    ).toBe(400);
    expect((await p(admin, "/api/po/rfqs", draftBody({ due_date: "tomorrow" }))).status).toBe(400);
    expect(await countAll("rfqs")).toBe(0);
  });
});

// ── Draft update (draft-only full-replace + optimistic-lock version) ──────────
describe("draft update", () => {
  it("full-replaces lines + vendor rows, bumps draft_version; refuses after generate", async () => {
    const created = await json<{ id: number }>(await p(admin, "/api/po/rfqs", draftBody()));
    const before = (await rfqRow(created.id))!;
    const upd = await p(admin, `/api/po/rfqs/${created.id}/update`, draftBody({
      scope_text: "Revised scope.",
      line_items: [{ description: "Only line now", qty: 2.5, unit: "pal" }],
      vendor_keys: ["VEN-000001"],
    }));
    expect(upd.status, await upd.clone().text()).toBe(200);
    const after = (await rfqRow(created.id))!;
    expect(after.scope_text).toBe("Revised scope.");
    expect(after.draft_version).toBe((before.draft_version as number) + 1);
    expect((await lineRows(created.id)).map((l) => l.description)).toEqual(["Only line now"]);
    expect((await vendorRows(created.id)).map((v) => v.vendor_key)).toEqual(["VEN-000001"]);
    expect(await auditCount("rfq_draft_update")).toBe(1);

    const { id } = await makeQueued(admin, { job_no: "2026.002" });
    const refused = await p(admin, `/api/po/rfqs/${id}/update`, draftBody({ job_no: "2026.002" }));
    expect(refused.status).toBe(409);
    expect((await json<{ error: string }>(refused)).error).toBe("not_draft");
  });
});

// ── Generate: numbering + the rfq:v1 canonical ────────────────────────────────
describe("generate", () => {
  it("0070: two SITES of one project get INDEPENDENT sequences", async () => {
    // THE POINT OF THE CHANGE. MH405 and OG593 are 2026.384 sites 1 and 2; before this they
    // shared one sequence and produced numbers a vendor could not tell apart.
    const s1 = await makeQueued(admin, { job_no: "2026.384", site_phase: 1 });
    expect(s1.rfq_number).toBe("RFQ-2026.384.1-001");
    const s2 = await makeQueued(admin, { job_no: "2026.384", site_phase: 2 });
    expect(s2.rfq_number).toBe("RFQ-2026.384.2-001"); // NOT -002
    const s1b = await makeQueued(admin, { job_no: "2026.384", site_phase: 1 });
    expect(s1b.rfq_number).toBe("RFQ-2026.384.1-002"); // site 1 continues on its own
    expect((await rfqRow(s1.id))!.site_phase).toBe(1); // persisted, not just composed
    // job_no itself stays TWO-segment — 0064's model, and what keeps the estimate auto-bind
    // (which joins on rfqs.job_no) working.
    expect((await rfqRow(s1.id))!.job_no).toBe("2026.384");
  });

  it("0070: a site-0 job keeps the legacy site-less number and its own sequence", async () => {
    // Site 0 must emit NO segment. If it emitted `.0`, the allocator — which measures PAST the
    // prefix — would read an already-issued `RFQ-2026.900-001` against the longer
    // `RFQ-2026.900.0-` and mint `-002` beside an existing `-001`: two DIFFERENT strings, so
    // UNIQUE(rfq_number) would not catch it and the sequence would be silently wrong.
    const a = await makeQueued(admin, { job_no: "2026.900" });
    expect(a.rfq_number).toBe("RFQ-2026.900-001");
    const b = await makeQueued(admin, { job_no: "2026.900", site_phase: 0 });
    expect(b.rfq_number).toBe("RFQ-2026.900-002"); // explicit 0 == omitted: same family
    const c = await makeQueued(admin, { job_no: "2026.900", site_phase: 4 });
    expect(c.rfq_number).toBe("RFQ-2026.900.4-001"); // site-bearing family starts fresh
  });

  it("0070: an out-of-range site is refused, not silently clamped", async () => {
    for (const bad of [-1, 10000, 1.5, "2"]) {
      const res = await p(admin, "/api/po/rfqs", draftBody({ site_phase: bad }));
      expect(res.status, `site_phase ${JSON.stringify(bad)}`).toBe(400);
      expect(((await res.json()) as any).error).toBe("invalid_site_phase");
    }
  });

  it("allocates per-job sequence numbers; the stored hmac recomputes over the canonical (sorted vendor_keys, NO price keys)", async () => {
    const a = await makeQueued(admin);
    expect(a.rfq_number).toBe("RFQ-2026.001-001");
    // Second RFQ, same job: the seq increments past the allocated number. (The first
    // upload's sha-style dedupe does not exist here — distinct drafts are legitimate.)
    const b = await makeQueued(admin, {
      line_items: [{ description: "Different ask" }],
    });
    expect(b.rfq_number).toBe("RFQ-2026.001-002");
    // A different job starts its own sequence.
    const c = await makeQueued(admin, { job_no: "2026.007" });
    expect(c.rfq_number).toBe("RFQ-2026.007-001");

    const row = (await rfqRow(a.id))!;
    expect(row.status).toBe("queued");
    // Recompute the signature byte-for-byte from the STORED row (the Mac-side contract).
    const lines = (await lineRows(a.id)).map((l) => ({
      position: l.position, part_number: l.part_number, description: l.description,
      qty: l.qty, unit: l.unit, line_note: l.line_note,
    })) as RfqLine[];
    const vendorKeys = (await vendorRows(a.id)).map((v) => v.vendor_key as string).reverse(); // any order in
    const canonical = canonicalRfqJson(row as unknown as RfqRow, lines, vendorKeys);
    const expected = await hmacHex(HMAC_SECRET, rfqCanonicalString(a.id, a.rfq_number, canonical));
    expect(row.hmac).toBe(expected);

    const parsed = JSON.parse(canonical) as Record<string, unknown>;
    // vendor_keys are SORTED in the canonical regardless of supplied/read order.
    expect(parsed.vendor_keys).toEqual(["VEN-000001", "VEN-000002"]);
    // PRICE-FREE: no money-shaped key anywhere in the signed payload (ADR decision 2).
    expect(/cents|price|cost|total|amount/i.test(canonical)).toBe(false);
    expect(Object.keys(parsed)).toEqual([
      "rfq_number", "job_no", "job_name", "ship_to_name", "ship_to_address", "ship_to_city",
      "ship_to_state", "ship_to_zip", "delivery_contact_name", "delivery_contact_phone",
      "delivery_contact_email", "scope_text", "due_date", "line_items", "vendor_keys",
    ]);
    expect(await auditCount("rfq_generate")).toBe(3);
  });

  it("the UNIQUE(rfq_number) backstop maps a collision to 409 and leaves the draft untouched", async () => {
    await makeQueued(admin); // occupies RFQ-2026.001-001
    // Plant a FOREIGN-job row that squats on this job's next number: the MAX+1 scan
    // (WHERE job_no='2026.001') doesn't see it, so generate builds ...-002 and hits UNIQUE.
    await env.DB.prepare(
      "INSERT INTO rfqs (rfq_uuid, rfq_number, job_no, status, created_by) VALUES ('squat-uuid','RFQ-2026.001-002','2026.999','queued','test')",
    ).run();
    const created = await json<{ id: number }>(await p(admin, "/api/po/rfqs", draftBody()));
    const gen = await p(admin, `/api/po/rfqs/${created.id}/generate`, {});
    expect(gen.status).toBe(409);
    expect((await json<{ error: string }>(gen)).error).toBe("rfq_number_conflict");
    const row = (await rfqRow(created.id))!;
    expect(row.status).toBe("draft"); // untouched — the client just retries
    expect(row.rfq_number).toBeNull();
    expect(row.hmac).toBeNull();
  });

  it("re-checks vendor active-status at generate (TOCTOU): a vendor deactivated after the last draft-save → 409 vendor_inactive, nothing signed", async () => {
    // Draft passes the draft-time findUnknownVendor(active=1) check with both vendors.
    const created = await json<{ id: number }>(await p(admin, "/api/po/rfqs", draftBody()));
    // Now DEACTIVATE one of them — the window generate must re-close (draft-time check is stale).
    await env.DB.prepare("UPDATE po_vendors SET active=0 WHERE vendor_key='VEN-000002'").run();
    const gen = await p(admin, `/api/po/rfqs/${created.id}/generate`, {});
    expect(gen.status, await gen.clone().text()).toBe(409);
    const body = await json<{ error: string; vendor_key: string }>(gen);
    expect(body.error).toBe("vendor_inactive");
    expect(body.vendor_key).toBe("VEN-000002");
    // Nothing signed/queued — the draft is untouched (client re-activates the vendor and retries).
    const row = (await rfqRow(created.id))!;
    expect(row.status).toBe("draft");
    expect(row.rfq_number).toBeNull();
    expect(row.hmac).toBeNull();
    expect(await auditCount("rfq_generate")).toBe(0);
  });
});

// ── Bearer tier isolation, THREE ways (red-team #1) ───────────────────────────
describe("bearer isolation", () => {
  it("rejects no-token, wrong token, and EVERY sibling tier (incl. the PO and ESTIMATE tokens) on every rfq internal route", async () => {
    const routes: [string, string, unknown][] = [
      ["GET", "/api/po/rfqs/internal/pending", undefined],
      ["POST", "/api/po/rfqs/internal/mark-filed", { rfq_id: 1, vendor_results: [{ vendor_key: "VEN-000001" }] }],
      ["POST", "/api/po/rfqs/internal/status-sync", { rfq_id: 1, vendor_key: "VEN-000001", status: "sent" }],
    ];
    for (const [method, path, body] of routes) {
      const init = body === undefined ? { method } : { method, body: JSON.stringify(body) };
      expect((await call(path, init)).status, `${path} no token`).toBe(401);
      for (const bearer of ["wrong-token", PO_BEARER, EST_BEARER, INTERNAL_BEARER, FIELDOPS_BEARER, ADMIN_BEARER]) {
        expect((await call(path, { ...init, bearer })).status, `${path} bearer=${bearer}`).toBe(401);
      }
      expect((await call(path, { ...init, bearer: RFQ_BEARER })).status, `${path} rfq token`).not.toBe(401);
    }
  });

  it("the rfq token opens NEITHER the PO-daemon tier NOR the estimate tier (scope holds in all directions)", async () => {
    expect((await call("/api/po/internal/pending", { bearer: RFQ_BEARER })).status).toBe(401);
    expect(
      (await call("/api/po/internal/mark-filed", {
        method: "POST", bearer: RFQ_BEARER, body: JSON.stringify({ po_id: 1 }),
      })).status,
    ).toBe(401);
    expect((await call("/api/po/estimates/internal/pending", { bearer: RFQ_BEARER })).status).toBe(401);
    expect(
      (await call("/api/po/estimates/internal/result", {
        method: "POST", bearer: RFQ_BEARER, body: JSON.stringify({ estimate_id: 1, status: "refused" }),
      })).status,
    ).toBe(401);
  });
});

// ── Internal surface: pending / mark-filed / status-sync ──────────────────────
describe("internal surface", () => {
  it("pending serves queued rows with hmac + lines + vendor rows; drafts stay off the queue", async () => {
    await p(admin, "/api/po/rfqs", draftBody({ job_no: "2026.005" })); // draft — must not appear
    const { id, rfq_number } = await makeQueued(admin);
    const res = await call("/api/po/rfqs/internal/pending", { bearer: RFQ_BEARER });
    const { pending } = await json<{ pending: Record<string, unknown>[] }>(res);
    expect(pending.length).toBe(1);
    expect(pending[0].id).toBe(id);
    expect(pending[0].rfq_number).toBe(rfq_number);
    expect(pending[0].hmac).toBeTruthy();
    expect((pending[0].line_items as unknown[]).length).toBe(2);
    expect((pending[0].vendors as { status: string }[]).every((v) => v.status === "pending")).toBe(true);
  });

  it("mark-filed flips vendors pending→filed in-WHERE, derives queued→generated only when ALL filed, and replays idempotently", async () => {
    const { id } = await makeQueued(admin);
    // Partial fan-out: one of two vendors filed — the rfq MUST stay queued (crash recovery).
    const partial = await markFiled({
      rfq_id: id,
      vendor_results: [{ vendor_key: "VEN-000001", box_pdf_file_id: "bx-p1", review_row_id: "row-1" }],
    });
    expect(partial.status, await partial.clone().text()).toBe(200);
    const pBody = await json<{ filed: number; all_filed: boolean }>(partial);
    expect(pBody.filed).toBe(1);
    expect(pBody.all_filed).toBe(false);
    expect(((await rfqRow(id))!).status).toBe("queued");
    const v1 = (await vendorRows(id)).find((v) => v.vendor_key === "VEN-000001")!;
    expect(v1.status).toBe("filed");
    expect(v1.box_pdf_file_id).toBe("bx-p1");
    expect(v1.review_row_id).toBe("row-1");
    expect(await auditCount("rfq_vendor_filed")).toBe(1);
    expect(await auditCount("rfq_all_filed")).toBe(0);

    // The re-served pass finishes the fan-out (replaying vendor 1 no-ops in-WHERE).
    const finish = await markFiled({
      rfq_id: id,
      vendor_results: [
        { vendor_key: "VEN-000001", box_pdf_file_id: "bx-p1-replay", review_row_id: "row-1-replay" },
        { vendor_key: "VEN-000002", box_pdf_file_id: "bx-p2", box_form_file_id: "bx-f2", review_row_id: "row-2" },
      ],
    });
    const fBody = await json<{ filed: number; replayed: number; all_filed: boolean }>(finish);
    expect(fBody.filed).toBe(1);
    expect(fBody.replayed).toBe(1);
    expect(fBody.all_filed).toBe(true);
    expect(((await rfqRow(id))!).status).toBe("generated");
    // The replay did NOT clobber vendor 1's filing record (in-WHERE on status='pending').
    const v1After = (await vendorRows(id)).find((v) => v.vendor_key === "VEN-000001")!;
    expect(v1After.box_pdf_file_id).toBe("bx-p1");
    expect(await auditCount("rfq_vendor_filed")).toBe(2);
    expect(await auditCount("rfq_all_filed")).toBe(1);

    // Full replay: everything no-ops, nothing re-audits, statuses hold.
    const replay = await markFiled({
      rfq_id: id,
      vendor_results: [{ vendor_key: "VEN-000002", box_pdf_file_id: "bx-x", review_row_id: "row-x" }],
    });
    const rBody = await json<{ filed: number; all_filed: boolean }>(replay);
    expect(rBody.filed).toBe(0);
    expect(rBody.all_filed).toBe(false);
    expect(((await rfqRow(id))!).status).toBe("generated");
    expect(await auditCount("rfq_vendor_filed")).toBe(2);
    expect(await auditCount("rfq_all_filed")).toBe(1);
  });

  it("mark-filed cannot flip a DRAFT rfq's vendor rows to filed (the vendor UPDATE is scoped on the parent being queued)", async () => {
    // A draft (never generated) carries pending vendor rows, but a spoofed/replayed
    // receipt against its id must never file them — the vendor UPDATE is parent-status
    // scoped, so a non-queued rfq no-ops in-WHERE.
    const created = await json<{ id: number }>(await p(admin, "/api/po/rfqs", draftBody()));
    const res = await markFiled({
      rfq_id: created.id,
      vendor_results: [{ vendor_key: "VEN-000001", box_pdf_file_id: "bx-p1", review_row_id: "row-1" }],
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect((await json<{ filed: number }>(res)).filed).toBe(0); // nothing flipped
    expect(((await rfqRow(created.id))!).status).toBe("draft"); // still a draft
    expect((await vendorRows(created.id)).every((v) => v.status === "pending")).toBe(true);
    expect(await auditCount("rfq_vendor_filed")).toBe(0);
  });

  it("status-sync is forward-only: 'filed' is not syncable, responded-before-sent refuses, sent/responded derive the rfq status", async () => {
    const { id } = await makeQueued(admin);
    await fileAll(id);

    // The daemon can never stamp 'filed' (or regress anything) through status-sync.
    const bad = await statusSync({ rfq_id: id, vendor_key: "VEN-000001", status: "filed" });
    expect(bad.status).toBe(400);
    expect((await json<{ error: string }>(bad)).error).toBe("invalid_status");

    // responded before sent → forward-only in-WHERE refuses (found:false, row unchanged).
    const early = await statusSync({ rfq_id: id, vendor_key: "VEN-000001", status: "responded" });
    expect((await json<{ found: boolean }>(early)).found).toBe(false);
    expect((await vendorRows(id)).find((v) => v.vendor_key === "VEN-000001")!.status).toBe("filed");

    // First vendor sent → rfq derives partially_sent.
    const sent1 = await statusSync({ rfq_id: id, vendor_key: "VEN-000001", status: "sent" });
    const s1 = await json<{ found: boolean; rfq_status: string }>(sent1);
    expect(s1.found).toBe(true);
    expect(s1.rfq_status).toBe("partially_sent");
    const v1 = (await vendorRows(id)).find((v) => v.vendor_key === "VEN-000001")!;
    expect(v1.status).toBe("sent");
    expect(v1.sent_at).not.toBeNull();

    // Replayed 'sent' is a no-op (found:false) and cannot regress anything.
    const replay = await statusSync({ rfq_id: id, vendor_key: "VEN-000001", status: "sent" });
    expect((await json<{ found: boolean }>(replay)).found).toBe(false);
    expect(await auditCount("rfq_vendor_sent")).toBe(1);

    // Second vendor sent → all live vendors are out the door → rfq 'sent'.
    const sent2 = await statusSync({ rfq_id: id, vendor_key: "VEN-000002", status: "sent" });
    expect((await json<{ rfq_status: string }>(sent2)).rfq_status).toBe("sent");

    // A response links the estimate row and leaves the rfq status at 'sent'.
    const resp = await statusSync({
      rfq_id: id, vendor_key: "VEN-000001", status: "responded", responded_estimate_id: 42,
    });
    const rb = await json<{ found: boolean; rfq_status: string }>(resp);
    expect(rb.found).toBe(true);
    expect(rb.rfq_status).toBe("sent");
    const v1r = (await vendorRows(id)).find((v) => v.vendor_key === "VEN-000001")!;
    expect(v1r.status).toBe("responded");
    expect(v1r.responded_estimate_id).toBe(42);
    expect(await auditCount("rfq_vendor_responded")).toBe(1);

    // responded_estimate_id is refused alongside 'sent' (contract violation).
    const mixed = await statusSync({
      rfq_id: id, vendor_key: "VEN-000002", status: "sent", responded_estimate_id: 7,
    });
    expect(mixed.status).toBe(400);
  });
});

// ── Cancel ────────────────────────────────────────────────────────────────────
describe("cancel", () => {
  it("cancels a draft (vendor rows cancel in the same batch); refuses past queued", async () => {
    const created = await json<{ id: number }>(await p(admin, "/api/po/rfqs", draftBody()));
    const res = await p(admin, `/api/po/rfqs/${created.id}/cancel`, {});
    expect(res.status, await res.clone().text()).toBe(200);
    expect(((await rfqRow(created.id))!).status).toBe("canceled");
    expect((await vendorRows(created.id)).every((v) => v.status === "canceled")).toBe(true);
    expect(await auditCount("rfq_cancel")).toBe(1);

    const { id } = await makeQueued(admin, { job_no: "2026.002" });
    await fileAll(id); // → generated: past the cancelable states
    const refused = await p(admin, `/api/po/rfqs/${id}/cancel`, {});
    expect(refused.status).toBe(409);
    expect((await json<{ error: string }>(refused)).error).toBe("not_cancelable");
    expect(await auditCount("rfq_cancel")).toBe(1); // gated audit did not fire
  });
});

// ── Browser reads ─────────────────────────────────────────────────────────────
describe("list + detail", () => {
  it("list filters by status and carries vendor rows for badges; detail serves lines + vendors; neither serves the hmac", async () => {
    await p(admin, "/api/po/rfqs", draftBody({ job_no: "2026.003" }));
    const { id } = await makeQueued(admin);

    const all = await json<{ rfqs: Record<string, unknown>[] }>(await g(admin, "/api/po/rfqs"));
    expect(all.rfqs.length).toBe(2);
    expect(all.rfqs.every((r) => !("hmac" in r))).toBe(true);
    expect((all.rfqs.find((r) => r.id === id)!.vendors as unknown[]).length).toBe(2);

    const drafts = await json<{ rfqs: unknown[] }>(await g(admin, "/api/po/rfqs?status=draft"));
    expect(drafts.rfqs.length).toBe(1);

    const detail = await json<{ rfq: Record<string, unknown>; line_items: unknown[]; vendors: unknown[] }>(
      await g(admin, `/api/po/rfqs/${id}`),
    );
    expect(detail.rfq.id).toBe(id);
    expect("hmac" in detail.rfq).toBe(false);
    expect(detail.line_items.length).toBe(2);
    expect(detail.vendors.length).toBe(2);
    expect((await g(admin, "/api/po/rfqs/99999")).status).toBe(404);
  });
});
