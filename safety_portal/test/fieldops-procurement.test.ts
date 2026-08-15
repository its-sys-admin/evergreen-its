import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, g, p, seedJob } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// GET /api/fieldops/jobs/:job_id/procurement (Track A8) — the job detail's read-only
// Procurement section. Under test: the at-least-one-lane-cap gate (both lane caps are
// admin-only today, so manager/submitter 403); unknown job 404; per-lane rows with
// server-side NAME joins (never bare keys); the RFQ job_id capture (0075) — a drafted RFQ
// lands on its job's section, and a pre-0075-style row (job_id='') never appears.
// ─────────────────────────────────────────────────────────────────────────────

async function seedVendor(vendorKey: string): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO po_vendors (vendor_key, vendor_name, contact_email, region, supply_categories, active, origin, sync_state, mirror_version, mirrored_version) " +
      "VALUES (?1, 'Breaker Supply Co', 'v@x.com', '', '[]', 1, 'smartsheet', 'synced', 0, 0)",
  ).bind(vendorKey).run();
}

async function seedPo(jobId: string, over: { status?: string; vendorKey?: string | null; total?: number } = {}): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO purchase_orders (po_uuid, job_no, site_phase, job_id, job_name, vendor_key, status, subtotal_cents, tax_cents, shipping_cents, total_cents, created_by) " +
      "VALUES (?1, '2026.384', 1, ?2, 'Deep Lake', ?3, ?4, ?5, 0, 0, ?5, 'adm.p')",
  ).bind(crypto.randomUUID(), jobId, over.vendorKey ?? "VEN-000042", over.status ?? "pending_review", over.total ?? 123_45).run();
}

async function seedSub(jobId: string, over: { status?: string; subKey?: string | null } = {}): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO subcontractors (sub_key, sub_name, trades, active, origin, sync_state, mirror_version, mirrored_version) " +
      "VALUES ('SUB-000001', 'Trench Kings', '[]', 1, 'smartsheet', 'synced', 0, 0)",
  ).run().catch(() => {});
  await env.DB.prepare(
    "INSERT INTO subcontracts (sc_uuid, job_no, job_id, job_name, project_name, owner_entity, sub_key, trade, price_basis, contract_price_cents, retainage_bp, status, created_by) " +
      "VALUES (?1, '2026.384', ?2, 'Deep Lake', 'Deep Lake', 'EG SPV 1', ?3, 'civil', 'fixed', 500000, 0, ?4, 'adm.p')",
  ).bind(crypto.randomUUID(), jobId, over.subKey ?? "SUB-000001", over.status ?? "sent").run();
}

let admin: string, manager: string;

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM rfq_vendors"),
    env.DB.prepare("DELETE FROM rfq_line_items"),
    env.DB.prepare("DELETE FROM rfqs"),
    env.DB.prepare("DELETE FROM purchase_orders"),
    env.DB.prepare("DELETE FROM subcontracts"),
    env.DB.prepare("DELETE FROM subcontractors"),
    env.DB.prepare("DELETE FROM po_vendors"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
  await provision("adm.p", "password123", "admin");
  await provision("mgr.p", "password123", "manager");
  admin = await login("adm.p", "password123");
  manager = await login("mgr.p", "password123");
  await seedJob("JOB-P");
});

describe("GET /api/fieldops/jobs/:job_id/procurement", () => {
  it("403s a session holding neither lane cap; 404s an unknown job", async () => {
    expect((await g(manager, "/api/fieldops/jobs/JOB-P/procurement")).status).toBe(403);
    expect((await g(admin, "/api/fieldops/jobs/JOB-NOPE/procurement")).status).toBe(404);
    expect((await call("/api/fieldops/jobs/JOB-P/procurement")).status).toBe(401);
  });

  it("serves per-lane rows with server-side names — never bare keys", async () => {
    await seedVendor("VEN-000042");
    await seedPo("JOB-P", { vendorKey: "VEN-000042", status: "sent", total: 987_00 });
    await seedPo("JOB-ELSEWHERE", { vendorKey: "VEN-000042" }); // absent jobs row is fine — filtered by job_id
    await seedSub("JOB-P", { status: "executed" });
    const res = await g(admin, "/api/fieldops/jobs/JOB-P/procurement");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as {
      purchase_orders: { vendor_name: string | null; status: string; total_cents: number; filed: boolean }[] | null;
      subcontracts: { sub_name: string | null; status: string }[] | null;
      rfqs: unknown[] | null;
    };
    expect(body.purchase_orders).toHaveLength(1);
    expect(body.purchase_orders![0]).toMatchObject({
      vendor_name: "Breaker Supply Co", status: "sent", total_cents: 98700, filed: false,
    });
    expect(body.subcontracts).toHaveLength(1);
    expect(body.subcontracts![0]).toMatchObject({ sub_name: "Trench Kings", status: "executed" });
    expect(body.rfqs).toEqual([]);
  });

  it("captures the RFQ's job_id at draft (0075) and lists it; a job-less legacy row never appears", async () => {
    await seedVendor("VEN-000042");
    const created = await p(admin, "/api/po/rfqs", {
      job_id: "JOB-P",
      job_no: "2026.384",
      site_phase: 1,
      job_name: "Deep Lake",
      line_items: [{ description: "600V cable", qty: 500, unit: "ft" }],
      vendor_keys: ["VEN-000042"],
    });
    expect(created.status, await created.clone().text()).toBe(201);
    // A pre-0075 row: job_id '' — structurally invisible to the per-job read.
    await env.DB.prepare(
      "INSERT INTO rfqs (rfq_uuid, job_no, job_name, status, created_by) VALUES (?1, '2026.384', 'Deep Lake', 'draft', 'adm.p')",
    ).bind(crypto.randomUUID()).run();

    const res = await g(admin, "/api/fieldops/jobs/JOB-P/procurement");
    const body = (await res.json()) as { rfqs: { vendor_count: number; status: string }[] | null };
    expect(body.rfqs).toHaveLength(1);
    expect(body.rfqs![0]).toMatchObject({ status: "draft", vendor_count: 1 });
  });
});

// ── Track D: lifecycle actions + change orders ───────────────────────────────────
const PO_BEARER = "test-po-token"; // == PORTAL_PO_API_TOKEN in the test env (po.test.ts)

describe("procurement lifecycle (Track D)", () => {
  it("marks an APPROVED PO submitted (pending_review 409s — approval is never skipped), flips its predecessor, and converges with the Mac sync", async () => {
    await seedVendor("VEN-000042");
    // A sent predecessor + its successor (a revision chain).
    await seedPo("JOB-P", { status: "sent", vendorKey: "VEN-000042" });
    const pred = (await env.DB.prepare("SELECT id FROM purchase_orders ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
    await seedPo("JOB-P", { status: "pending_review", vendorKey: "VEN-000042" });
    const succ = (await env.DB.prepare("SELECT id FROM purchase_orders ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
    await env.DB.prepare("UPDATE purchase_orders SET supersedes_po_id=?2 WHERE id=?1").bind(succ, pred).run();

    // A pending_review document cannot skip the approval record — named 409 first.
    const early = await p(admin, `/api/fieldops/procurement/po/${succ}/lifecycle`, { action: "mark_submitted" });
    expect(early.status).toBe(409);
    expect(await early.json()).toMatchObject({ error: "wrong_state", current_status: "pending_review" });
    await env.DB.prepare("UPDATE purchase_orders SET status='approved' WHERE id=?1").bind(succ).run();

    const res = await p(admin, `/api/fieldops/procurement/po/${succ}/lifecycle`, { action: "mark_submitted" });
    expect(res.status, await res.clone().text()).toBe(200);
    const rows = await env.DB.prepare("SELECT id, status FROM purchase_orders ORDER BY id").all<{ id: number; status: string }>();
    expect(rows.results.find((r) => r.id === succ)!.status).toBe("sent");
    // The predecessor-supersession flip rode the same batch — no two in-force documents.
    expect(rows.results.find((r) => r.id === pred)!.status).toBe("superseded");

    // TWO-WRITER CONVERGENCE: the Mac sync later stamps 'sent' from the review sheet — its
    // exact-prior-state guard (status='approved') no-ops against the portal's earlier mark.
    const sync = await call("/api/po/internal/status-sync", {
      method: "POST", bearer: PO_BEARER,
      body: JSON.stringify({ updates: [{ po_id: succ, status: "sent" }] }),
    });
    expect(sync.status, await sync.clone().text()).toBe(200);
    const after = await env.DB.prepare("SELECT status FROM purchase_orders WHERE id=?1").bind(succ).first<{ status: string }>();
    expect(after!.status).toBe("sent"); // unchanged — benign no-op, no error, no regression
  });

  it("marks a PO accepted from sent (status stays sent; provenance recorded); wrong-state is a NAMED 409", async () => {
    await seedVendor("VEN-000042");
    await seedPo("JOB-P", { status: "sent", vendorKey: "VEN-000042" });
    const id = (await env.DB.prepare("SELECT id FROM purchase_orders ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
    const res = await p(admin, `/api/fieldops/procurement/po/${id}/lifecycle`, { action: "mark_accepted" });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status, accepted_at, accepted_by FROM purchase_orders WHERE id=?1").bind(id)
      .first<{ status: string; accepted_at: string | null; accepted_by: string | null }>();
    expect(row!.status).toBe("sent"); // acceptance rides its own columns — the machine state is untouched
    expect(row!.accepted_at).toBeTruthy();
    expect(row!.accepted_by).toBe("adm.p");
    // A second accept is a wrong-state refusal naming the current record, never a silent no-op.
    const again = await p(admin, `/api/fieldops/procurement/po/${id}/lifecycle`, { action: "mark_accepted" });
    expect(again.status).toBe(409);
    expect(await again.json()).toMatchObject({ error: "wrong_state", current_status: "sent" });
    // The audited correction clears it.
    const undo = await p(admin, `/api/fieldops/procurement/po/${id}/lifecycle`, { action: "clear_accepted" });
    expect(undo.status).toBe(200);
    const cleared = await env.DB.prepare("SELECT accepted_at FROM purchase_orders WHERE id=?1").bind(id).first<{ accepted_at: string | null }>();
    expect(cleared!.accepted_at).toBeNull();
  });

  it("marks a subcontract accepted → the machine's executed state, with provenance", async () => {
    await seedSub("JOB-P", { status: "sent" });
    const id = (await env.DB.prepare("SELECT id FROM subcontracts ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
    const res = await p(admin, `/api/fieldops/procurement/subcontract/${id}/lifecycle`, { action: "mark_accepted" });
    expect(res.status, await res.clone().text()).toBe(200);
    const row = await env.DB.prepare("SELECT status, accepted_by FROM subcontracts WHERE id=?1").bind(id)
      .first<{ status: string; accepted_by: string | null }>();
    expect(row!.status).toBe("executed");
    expect(row!.accepted_by).toBe("adm.p");
  });

  it("closes an RFQ round — the 0056 CHECK's dead 'closed' state finally has a writer", async () => {
    await seedVendor("VEN-000042");
    const created = await p(admin, "/api/po/rfqs", {
      job_id: "JOB-P", job_no: "2026.384", site_phase: 1,
      line_items: [{ description: "cable", qty: 1 }], vendor_keys: ["VEN-000042"],
    });
    const rfqId = ((await created.json()) as { id: number }).id;
    // A draft round is not closable (close retires a GENERATED/SENT round).
    const early = await p(admin, `/api/fieldops/procurement/rfq/${rfqId}/lifecycle`, { action: "close" });
    expect(early.status).toBe(409);
    await env.DB.prepare("UPDATE rfqs SET status='sent' WHERE id=?1").bind(rfqId).run();
    const res = await p(admin, `/api/fieldops/procurement/rfq/${rfqId}/lifecycle`, { action: "close" });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare("SELECT status FROM rfqs WHERE id=?1").bind(rfqId).first<{ status: string }>();
    expect(row!.status).toBe("closed");
  });

  it("gates lifecycle actions on the lane's own cap (manager 403)", async () => {
    await seedVendor("VEN-000042");
    await seedPo("JOB-P", { status: "sent", vendorKey: "VEN-000042" });
    const id = (await env.DB.prepare("SELECT id FROM purchase_orders ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
    expect((await p(manager, `/api/fieldops/procurement/po/${id}/lifecycle`, { action: "mark_accepted" })).status).toBe(403);
  });
});

describe("subcontract countersign undo (Q4)", () => {
  it("clear_accepted flips executed back to sent and clears the acceptance record; a second call 409s", async () => {
    await seedSub("JOB-P", { status: "executed" });
    const id = (await env.DB.prepare("SELECT id FROM subcontracts ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
    await env.DB.prepare("UPDATE subcontracts SET accepted_at='2026-08-15', accepted_by='adm.p' WHERE id=?1").bind(id).run();
    expect((await p(admin, `/api/fieldops/procurement/subcontract/${id}/lifecycle`, { action: "clear_accepted" })).status).toBe(200);
    const row = await env.DB.prepare("SELECT status, accepted_at, accepted_by FROM subcontracts WHERE id=?1").bind(id)
      .first<{ status: string; accepted_at: string | null; accepted_by: string | null }>();
    expect(row).toMatchObject({ status: "sent", accepted_at: null, accepted_by: null });
    expect((await p(admin, `/api/fieldops/procurement/subcontract/${id}/lifecycle`, { action: "clear_accepted" })).status).toBe(409);
  });
});

describe("change-order documents (Track D2)", () => {
  it("serves change_order_of/co_seq on both lanes so the SPA nests CO documents", async () => {
    await seedVendor("VEN-000042");
    await seedPo("JOB-P", { status: "sent", vendorKey: "VEN-000042" });
    const poId = (await env.DB.prepare("SELECT id FROM purchase_orders ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
    // The CO document, as the lane clone route would mint it (linkage columns set, no supersession).
    await env.DB.prepare(
      "INSERT INTO purchase_orders (po_uuid, job_no, site_phase, job_id, job_name, vendor_key, status, subtotal_cents, tax_cents, shipping_cents, total_cents, created_by, change_order_of, co_seq) " +
        "VALUES (?1, '2026.384', 1, 'JOB-P', 'Deep Lake', 'VEN-000042', 'draft', 100, 0, 0, 100, 'adm.p', ?2, 1)",
    ).bind(crypto.randomUUID(), poId).run();
    await seedSub("JOB-P", { status: "executed" });

    const res = await g(admin, "/api/fieldops/jobs/JOB-P/procurement");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as {
      purchase_orders: { id: number; change_order_of: number | null; co_seq: number | null }[] | null;
      subcontracts: { change_order_of: number | null; co_seq: number | null }[] | null;
    };
    const co = body.purchase_orders!.find((r) => r.change_order_of !== null);
    expect(co).toBeDefined();
    expect(co!.change_order_of).toBe(poId);
    expect(co!.co_seq).toBe(1);
    const base = body.purchase_orders!.find((r) => r.id === poId)!;
    expect(base.change_order_of).toBeNull();
    expect(body.subcontracts![0].change_order_of).toBeNull();
  });

  it("lifecycle marking works on a CO document and never touches its parent", async () => {
    await seedVendor("VEN-000042");
    await seedPo("JOB-P", { status: "sent", vendorKey: "VEN-000042" });
    const poId = (await env.DB.prepare("SELECT id FROM purchase_orders ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
    await env.DB.prepare(
      "INSERT INTO purchase_orders (po_uuid, po_number, job_no, site_phase, job_id, job_name, vendor_key, status, subtotal_cents, tax_cents, shipping_cents, total_cents, created_by, change_order_of, co_seq) " +
        "VALUES (?1, '2026.384.1.0.0-CO1', '2026.384', 1, 'JOB-P', 'Deep Lake', 'VEN-000042', 'approved', 100, 0, 0, 100, 'adm.p', ?2, 1)",
    ).bind(crypto.randomUUID(), poId).run();
    const coId = (await env.DB.prepare("SELECT id FROM purchase_orders WHERE change_order_of=?1").bind(poId).first<{ id: number }>())!.id;

    // mark_submitted on the CO: it flips to sent; the parent (also sent) is NOT superseded —
    // the flip keys on supersedes_po_id, which a CO never sets.
    expect((await p(admin, `/api/fieldops/procurement/po/${coId}/lifecycle`, { action: "mark_submitted" })).status).toBe(200);
    const parent = await env.DB.prepare("SELECT status FROM purchase_orders WHERE id=?1").bind(poId).first<{ status: string }>();
    expect(parent!.status).toBe("sent");
    expect((await p(admin, `/api/fieldops/procurement/po/${coId}/lifecycle`, { action: "mark_accepted" })).status).toBe(200);
  });
});
