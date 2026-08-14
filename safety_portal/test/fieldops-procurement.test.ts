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
