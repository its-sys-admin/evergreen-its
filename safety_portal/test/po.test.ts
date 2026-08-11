import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, p, g, json } from "./helpers";
import { hmacHex } from "../worker/hmac";
import { canonicalPoJson, poCanonicalString, computeTotals, lineExtendedCents } from "../worker/po";
import type { PoRow, PoLine } from "../worker/po";
// The versioned PO config the WORKER bundles at build time (worker/po.ts:7-9). Import the SAME
// files so every assertion about served/computed config tracks the live values instead of pinning
// them. GUARD (HOUSE REFLEXES §5 — the config-editor merge-blocker class): never hard-code
// purchaser/tax/terms CONTENT here. The §50 config editor auto-merges edits on green CI, so a
// pinned entity/email/rate/version red-lights the instant the operator edits it and strands the
// edit PR (exactly how PR #511 got stuck). Assert derived/served-equals-source/shape only.
import taxConfig from "../../po_materials/config/tax.json";
import purchaserConfig from "../../po_materials/config/purchaser.json";
import deliveryContactsConfig from "../../po_materials/config/delivery_contacts.json";
import termsManifest from "../../po_materials/terms/manifest.json";

// ─────────────────────────────────────────────────────────────────────────────
// PO workstream S2 — worker/po.ts + migrations 0042/0043/0044.
//
// NOTE ON THE FILE NAME: the S2 brief said `po.spec.ts`, but vitest.config.ts collects
// `test/**/*.test.ts` ONLY — a `.spec.ts` file would be silently NOT RUN (the
// "green CI on a missing test proves nothing" class, HOUSE_REFLEXES §2). Hence `po.test.ts`.
//
// Coverage: the cap.po.manage gate (0044), draft→generate cents math + the totals-mismatch
// assert, atomic D7 number allocation + the UNIQUE family backstop, the po:v1 HMAC shape,
// the requirePoToken bearer tier + cross-token isolation (both directions), mark-filed
// idempotency, the vendors dirty-row fence + empty-sync refusal + the mark-mirrored
// watermark guard, the supersession flip on 'sent', and the cancel guards.
// ─────────────────────────────────────────────────────────────────────────────

const PO_BEARER = "test-po-token"; // == PORTAL_PO_API_TOKEN (vitest.config.ts)
const INTERNAL_BEARER = "test-internal-token"; // portal_poll's token — must be REJECTED on /api/po/internal/*
const FIELDOPS_BEARER = "test-fieldops-token"; // mirror daemon's token — must be REJECTED too
const ADMIN_BEARER = "test-admin-token"; // operator provisioning token — must be REJECTED too
const HMAC_SECRET = "test-hmac-payload-secret"; // == HMAC_PAYLOAD_SECRET (vitest.config.ts)

async function seedVendor(vendorKey: string, over: Partial<Record<string, unknown>> = {}): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO po_vendors (vendor_key, vendor_name, contact_email, region, supply_categories, active, origin, sync_state, mirror_version, mirrored_version) " +
      "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
  )
    .bind(
      vendorKey,
      (over.vendor_name as string) ?? `Vendor ${vendorKey}`,
      (over.contact_email as string) ?? "sales@vendor.example",
      (over.region as string) ?? "Midwest",
      (over.supply_categories as string) ?? '["racking"]',
      (over.active as number) ?? 1,
      (over.origin as string) ?? "smartsheet",
      (over.sync_state as string) ?? "synced",
      (over.mirror_version as number) ?? 0,
      (over.mirrored_version as number) ?? 0,
    )
    .run();
}

function draftBody(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    vendor_key: "VEN-000001",
    job_no: "2026.001",
    site_phase: 2,
    job_id: "JOB-000017",
    job_name: "Sunrise Solar",
    ship_to_name: "Evergreen Renewables LLC",
    ship_to_address: "100 Array Rd",
    ship_to_city: "Rockford",
    ship_to_state: "IL",
    ship_to_zip: "61101",
    delivery_contact_name: "Dana Field",
    delivery_contact_phone: "555-0100",
    delivery_contact_email: "dana@example.com",
    sow_text: "Supply and deliver racking components.",
    delivery_instructions: "Call site lead ahead of delivery.",
    payment_terms_text: "Net 30",
    terms_profile_id: "standard_17",
    terms_version: "v1",
    tax_mode: "auto",
    shipping_cents: 10_000,
    line_column_variant: "default",
    approver_name: "Alex Approver",
    approver_title: "Director of Procurement",
    line_items: [
      { part_number: "RK-100", description: "Rail 100", qty: 10, unit: "ea", unit_cost_cents: 12_345 },
      { part_number: "RK-200", description: "Clamp kit", qty: 2.5, unit: "box", unit_cost_cents: 1_000 },
    ],
    ...over,
  };
}
// Server math for draftBody(): 10×12345=123450; 2.5×1000=2500 → subtotal 125950 (pure line math,
// config-independent). draftBody ships to IL with tax_mode "auto", so the resolved rate is the
// bundled tax.json IL rate — derive tax/total from it (mirroring computeTotals'
// round(subtotal*bp/10000)) so the math is still independently checked but TRACKS any tax edit
// instead of pinning 900bp. subtotal_cents stays a literal (it is qty×unit_cost line math).
const SUBTOTAL_CENTS = 125_950;
const SHIPPING_CENTS = 10_000; // == draftBody().shipping_cents
const IL_BP = taxConfig.rates_bp.IL;
const EXPECTED_TAX_CENTS = Math.round((SUBTOTAL_CENTS * IL_BP) / 10_000);
const EXPECTED = {
  subtotal_cents: SUBTOTAL_CENTS,
  tax_cents: EXPECTED_TAX_CENTS,
  total_cents: SUBTOTAL_CENTS + EXPECTED_TAX_CENTS + SHIPPING_CENTS,
};

async function poRow(id: number): Promise<Record<string, unknown>> {
  return (await env.DB.prepare("SELECT * FROM purchase_orders WHERE id=?1").bind(id).first())!;
}

/** draft → queued helper: create + generate with the correct totals; returns the po id. */
async function makeQueued(admin: string, over: Record<string, unknown> = {}): Promise<number> {
  const created = await p(admin, "/api/po/drafts", draftBody(over));
  expect(created.status, await created.clone().text()).toBe(201);
  const { id } = await json<{ id: number }>(created);
  const gen = await p(admin, `/api/po/drafts/${id}/generate`, EXPECTED);
  expect(gen.status, await gen.clone().text()).toBe(200);
  return id;
}

/** queued → sent helper (mark-filed → approved → sent through the internal surface). */
async function driveToSent(id: number): Promise<void> {
  const filed = await call("/api/po/internal/mark-filed", {
    method: "POST", bearer: PO_BEARER, body: JSON.stringify({ po_id: id, box_file_id: `bx-${id}` }),
  });
  expect(filed.status).toBe(200);
  for (const status of ["approved", "sent"]) {
    const res = await call("/api/po/internal/status-sync", {
      method: "POST", bearer: PO_BEARER, body: JSON.stringify({ updates: [{ po_id: id, status }] }),
    });
    expect(res.status).toBe(200);
  }
}

let admin: string, submitter: string;
beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM po_line_items"),
    env.DB.prepare("DELETE FROM purchase_orders"),
    env.DB.prepare("DELETE FROM po_vendors"),
    env.DB.prepare("UPDATE po_vendor_counter SET last_value=0 WHERE id=1"),
  ]);
  await provision("admin.po", "password123", "admin");
  await provision("submitter.po", "password123", "submitter");
  admin = await login("admin.po", "password123");
  submitter = await login("submitter.po", "password123");
  await seedVendor("VEN-000001");
});

// ── Capability gate (migration 0044) ──────────────────────────────────────────
describe("cap.po.manage gate", () => {
  it("403s a submitter on every browser PO surface; 200s an admin", async () => {
    expect((await g(submitter, "/api/po/vendors")).status).toBe(403);
    expect((await g(submitter, "/api/po/pos")).status).toBe(403);
    expect((await g(submitter, "/api/po/terms")).status).toBe(403);
    expect((await g(submitter, "/api/po/config")).status).toBe(403);
    expect((await g(submitter, "/api/po/materials")).status).toBe(403);
    expect((await p(submitter, "/api/po/drafts", draftBody())).status).toBe(403);
    expect((await p(submitter, "/api/po/vendors", { vendor_name: "X" })).status).toBe(403);
    expect((await g(admin, "/api/po/vendors")).status).toBe(200);
  });

  it("401s an unauthenticated caller (no session)", async () => {
    expect((await call("/api/po/vendors")).status).toBe(401);
    expect((await call("/api/po/terms")).status).toBe(401);
    expect((await call("/api/po/config")).status).toBe(401);
    expect((await call("/api/po/materials")).status).toBe(401);
  });
});

// ── Material catalog picker feed (GET /api/po/materials) ──────────────────────
// A THIN read of the SAME material_catalog TYPE table (migration 0019, 36 seeded active types)
// the field-ops Materials Catalog admin manages — gated cap.po.manage so the PO builder reads
// the pick-list WITHOUT the field-ops cap.materials.receive. Identity projection only (no price).
describe("GET /api/po/materials", () => {
  it("serves active types to an admin, projecting id/model_id/manufacturer/category/key_specs only", async () => {
    const res = await g(admin, "/api/po/materials");
    expect(res.status, await res.clone().text()).toBe(200);
    const { materials } = await json<{ materials: Array<Record<string, unknown>> }>(res);
    expect(materials.length).toBeGreaterThanOrEqual(36); // the 0019 seed
    const known = materials.find((m) => m.model_id === "Q.PEAK_DUO_XL-G11.3_BFG");
    expect(known).toBeTruthy();
    expect(known!.manufacturer).toBe("Qcells");
    expect(known!.category).toBe("module");
    // The picker needs identity only — the price/provenance/active columns must NOT leak.
    expect(Object.keys(known!).sort()).toEqual(["category", "id", "key_specs", "manufacturer", "model_id"]);
    expect("unit_cost" in known!).toBe(false);
    expect("source_files" in known!).toBe(false);
    expect("active" in known!).toBe(false);
  });

  it("filters by ?category= (bound param) and never returns a soft-retired (active=0) type", async () => {
    // A distinctive retired row proves active-only; a distinctive active inverter proves the filter.
    await env.DB.prepare(
      "INSERT INTO material_catalog (model_id, manufacturer, category, key_specs, active) VALUES (?1,?2,?3,?4,?5)",
    ).bind("PO-MAT-RETIRED", "Acme", "module", "retired type", 0).run();
    await env.DB.prepare(
      "INSERT INTO material_catalog (model_id, manufacturer, category, key_specs, active) VALUES (?1,?2,?3,?4,?5)",
    ).bind("PO-MAT-ACTIVE-INV", "Acme", "inverter", "active inverter", 1).run();
    try {
      const all = await g(admin, "/api/po/materials");
      const allBody = await json<{ materials: Array<Record<string, unknown>> }>(all);
      expect(allBody.materials.some((m) => m.model_id === "PO-MAT-RETIRED")).toBe(false); // retired hidden
      expect(allBody.materials.some((m) => m.model_id === "PO-MAT-ACTIVE-INV")).toBe(true);

      const mods = await g(admin, "/api/po/materials?category=module");
      expect(mods.status).toBe(200);
      const modsBody = await json<{ materials: Array<Record<string, unknown>> }>(mods);
      expect(modsBody.materials.length).toBeGreaterThan(0);
      expect(modsBody.materials.every((m) => m.category === "module")).toBe(true); // filter honored
      expect(modsBody.materials.some((m) => m.model_id === "PO-MAT-ACTIVE-INV")).toBe(false); // inverter excluded
    } finally {
      await env.DB.prepare("DELETE FROM material_catalog WHERE model_id IN ('PO-MAT-RETIRED','PO-MAT-ACTIVE-INV')").run();
    }
  });
});

// ── Terms + config feeds (S2b wiring — the S3 files, build-time imported) ─────
describe("terms + config wiring", () => {
  it("terms serves the manifest profiles: ids, kinds, current version, tokens, attach render_line", async () => {
    const res = await g(admin, "/api/po/terms");
    expect(res.status).toBe(200);
    const { profiles } = await json<{ profiles: Array<Record<string, unknown>> }>(res);
    const byId = Object.fromEntries(profiles.map((pr) => [pr.id, pr]));
    // One vocabulary with the ITS_Vendors 'Default Terms Profile' picklist (S1/S3 parity).
    expect(Object.keys(byId).sort()).toEqual(Object.keys(termsManifest.profiles).sort());
    expect(byId.standard_17.kind).toBe("library");
    // current_version + tokens tracked from the source manifest (a terms add_version leaves these
    // unchanged; a future current_version promote must be free to merge — never pin the literal).
    const s17 = termsManifest.profiles.standard_17;
    expect(byId.standard_17.current_version).toBe(s17.current_version);
    expect(byId.standard_17.tokens).toEqual(
      (s17.versions as Record<string, { tokens: string[] }>)[s17.current_version].tokens,
    );
    expect(byId.negotiated_gtc.kind).toBe("attach");
    expect(String(byId.negotiated_gtc.render_line)).toContain("THIS PURCHASE ORDER IS SUBJECT");
    // Curated view — renderer implementation detail stays off the wire.
    for (const pr of profiles) expect(pr).not.toHaveProperty("versions");
  });

  it("config serves the D5 purchaser identity + the D8 tax table (comment fields stripped)", async () => {
    const res = await g(admin, "/api/po/config");
    expect(res.status).toBe(200);
    const cfg = await json<{
      purchaser: { entity: string; invoice_routing: { to: string; cc: string[] } };
      tax: { rates_bp: Record<string, number> };
    }>(res);
    // Served config must MATCH the bundled source (single source, no drift) — never pinned literals.
    expect(cfg.purchaser.entity).toBe(purchaserConfig.entity);
    expect(cfg.purchaser.invoice_routing.to).toBe(purchaserConfig.invoice_routing.to);
    expect(cfg.purchaser.invoice_routing.cc).toEqual(purchaserConfig.invoice_routing.cc);
    expect(cfg.tax.rates_bp).toEqual(taxConfig.rates_bp);
    // Shape sanity that survives ANY edit: routing 'to' is email-shaped, rates are non-neg integer bp.
    expect(cfg.purchaser.invoice_routing.to).toContain("@");
    expect(Object.values(cfg.tax.rates_bp).every((v) => Number.isInteger(v) && v >= 0)).toBe(true);
    expect(cfg).not.toHaveProperty("comment");
    expect(cfg.purchaser).not.toHaveProperty("comment");
  });

  it("config serves the delivery-contact suggestion list = the bundled source (Feature C, no drift)", async () => {
    const res = await g(admin, "/api/po/config");
    expect(res.status).toBe(200);
    const cfg = await json<{ delivery_contacts: { name: string; phone?: string; email?: string }[] }>(res);
    // Served-equals-source (HOUSE REFLEXES §5) — the §50 editor may change the CONTENT any time,
    // so assert against the SAME bundled JSON the Worker imports, never a pinned literal.
    expect(cfg.delivery_contacts).toEqual(deliveryContactsConfig.contacts);
    // Shape sanity that survives ANY edit: an array; every entry carries a non-empty string name.
    expect(Array.isArray(cfg.delivery_contacts)).toBe(true);
    for (const ct of cfg.delivery_contacts) {
      expect(typeof ct.name).toBe("string");
      expect(ct.name.length).toBeGreaterThan(0);
    }
  });

  it("the computeTotals tax table IS the imported S3 file (single source, no drift)", async () => {
    // Robust single-source check: the served config, the generate-time math, and the bundled
    // source file must all agree on the IL rate — assert the RELATIONSHIPS, never a literal 900.
    const res = await g(admin, "/api/po/config");
    const cfg = await json<{ tax: { rates_bp: Record<string, number> } }>(res);
    expect(cfg.tax.rates_bp.IL).toBe(taxConfig.rates_bp.IL); // served == bundled source
    const created = await p(admin, "/api/po/drafts", draftBody()); // ship_to IL, tax_mode auto
    const { totals } = await json<{ totals: { tax_rate_bp: number } }>(created);
    expect(totals.tax_rate_bp).toBe(cfg.tax.rates_bp.IL); // generate math == served config
  });
});

// ── Terms edit-text pre-fill (GET /api/po/terms/:profile_id/text) ─────────────
describe("terms edit-text pre-fill", () => {
  it("serves the current library version's body, header-stripped, derived from the manifest", async () => {
    const res = await g(admin, "/api/po/terms/standard_17/text");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<{ profile_id: string; version: string; text: string }>(res);
    expect(body.profile_id).toBe("standard_17");
    expect(body.version).toBe(termsManifest.profiles.standard_17.current_version); // derived, not pinned
    expect(typeof body.text).toBe("string");
    expect(body.text.length).toBeGreaterThan(0);
    expect(body.text.startsWith("<!--")).toBe(false); // provenance header stripped (matches the Mac renderer)
  });

  it("404s an attach profile (no versioned text) and an unknown profile", async () => {
    expect((await g(admin, "/api/po/terms/negotiated_gtc/text")).status).toBe(404);
    expect((await g(admin, "/api/po/terms/does_not_exist/text")).status).toBe(404);
  });

  it("401s no session; 403s without cap.po.manage (prove-the-control-bites)", async () => {
    expect((await call("/api/po/terms/standard_17/text")).status).toBe(401);
    expect((await g(submitter, "/api/po/terms/standard_17/text")).status).toBe(403);
  });
});

// ── Terms versions feed (GET /api/po/terms/:profile_id/versions) — the make-current picker ─────
describe("terms versions feed", () => {
  it("serves the curated version list (id + legal_review only), derived from the manifest", async () => {
    const res = await g(admin, "/api/po/terms/standard_17/versions");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<{
      profile_id: string;
      current_version: string;
      versions: Array<Record<string, unknown>>;
    }>(res);
    expect(body.profile_id).toBe("standard_17");
    expect(body.current_version).toBe(termsManifest.profiles.standard_17.current_version); // derived
    expect(body.versions.length).toBeGreaterThanOrEqual(1);
    const cur = body.versions.find((v) => v.version === body.current_version)!;
    expect(cur.legal_review).toBe("cleared"); // Layer B invariant: the current version is cleared
    // Curated — file names + sha256 hashes must NOT leak (renderer implementation detail).
    for (const v of body.versions) expect(Object.keys(v).sort()).toEqual(["legal_review", "version"]);
  });

  it("404s an attach profile + an unknown profile; 401/403 gated", async () => {
    expect((await g(admin, "/api/po/terms/negotiated_gtc/versions")).status).toBe(404);
    expect((await g(admin, "/api/po/terms/does_not_exist/versions")).status).toBe(404);
    expect((await call("/api/po/terms/standard_17/versions")).status).toBe(401);
    expect((await g(submitter, "/api/po/terms/standard_17/versions")).status).toBe(403);
  });
});

// ── Ship-to auto-fill feed (S6 follow-up: GET /api/po/jobs/:job_id/ship-to) ───
describe("ship-to auto-fill feed", () => {
  // Seed a routing-SoR job row (address + stakeholder). The jobs table isn't touched by the
  // file-level beforeEach, so this describe cleans + seeds it itself.
  async function seedRoutingJob(): Promise<void> {
    await env.DB.batch([env.DB.prepare("DELETE FROM jobs")]);
    await env.DB
      .prepare(
        "INSERT INTO jobs (job_id, project_name, active, address, stakeholder_name, stakeholder_phone, stakeholder_email) " +
          "VALUES (?1,?2,1,?3,?4,?5,?6)",
      )
      .bind(
        "JOB-000017",
        "2026.001 Sunrise Solar",
        "100 Array Rd, Rockford IL",
        "Dana Stakeholder",
        "555-0100",
        "dana@example.com",
      )
      .run();
  }
  beforeEach(seedRoutingJob);

  it("401s an unauthenticated caller (no session)", async () => {
    expect((await call("/api/po/jobs/JOB-000017/ship-to")).status).toBe(401);
  });

  it("403s a caller without cap.po.manage (prove-the-control-bites)", async () => {
    expect((await g(submitter, "/api/po/jobs/JOB-000017/ship-to")).status).toBe(403);
  });

  it("200s an admin with the routing-SoR ship-to block; city/state/zip empty (single address line)", async () => {
    const res = await g(admin, "/api/po/jobs/JOB-000017/ship-to");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<Record<string, unknown>>(res);
    expect(body).toEqual({
      job_id: "JOB-000017",
      job_no: "2026.001", // parsed YYYY.NNN prefix of the project name
      site_phase: 0, // 0064 — no third segment in the name ⇒ no site breakdown
      ship_to_name: "2026.001 Sunrise Solar",
      ship_to_address: "100 Array Rd, Rockford IL",
      ship_to_city: "",
      ship_to_state: "",
      ship_to_zip: "",
      delivery_contact_name: "Dana Stakeholder",
      delivery_contact_phone: "555-0100",
      delivery_contact_email: "dana@example.com",
    });
  });

  it("job_no is '' when the project name has no YYYY.NNN prefix", async () => {
    await env.DB.prepare("UPDATE jobs SET project_name=?1 WHERE job_id=?2")
      .bind("Sunrise Solar (no number)", "JOB-000017")
      .run();
    const res = await g(admin, "/api/po/jobs/JOB-000017/ship-to");
    const body = await json<{ job_no: string; site_phase: number; ship_to_name: string }>(res);
    expect(body.job_no).toBe("");
    expect(body.site_phase).toBe(0);
    expect(body.ship_to_name).toBe("Sunrise Solar (no number)");
  });

  // ── 0064: the name-prefix FALLBACK must not truncate a three-segment number ────────────────
  // PROVE-THE-CONTROL-BITES. Before 0064 this regex was /^(\d{4}\.\d{3})/ — open at the tail.
  // Against "2026.384.1 Coker Solar" it MATCHED and returned "2026.384", silently handing the PO
  // builder a number belonging to a DIFFERENT site of the same project. Every other job_no
  // validator in the system refuses loudly; this one corrupted. Reverting the regex to the old
  // form fails these two cases.
  it("0064: a three-segment name prefix yields BOTH parts, never a truncated job_no", async () => {
    await env.DB.prepare("UPDATE jobs SET project_name=?1, job_no='' WHERE job_id=?2")
      .bind("2026.384.1 Coker Solar", "JOB-000017")
      .run();
    const body = await json<{ job_no: string; site_phase: number }>(
      await g(admin, "/api/po/jobs/JOB-000017/ship-to"),
    );
    expect(body.job_no).toBe("2026.384");
    expect(body.site_phase).toBe(1); // the segment the old regex threw away
  });

  it("0064: a STORED identifier wins over the name parse, and both parts come from one source", async () => {
    // A deliberately CONFLICTING name: were the two mixed, this would compose 2026.384.7.
    await env.DB.prepare("UPDATE jobs SET project_name=?1, job_no=?2, site_phase=?3 WHERE job_id=?4")
      .bind("2020.999.7 Stale Name", "2026.384", 2, "JOB-000017")
      .run();
    const body = await json<{ job_no: string; site_phase: number }>(
      await g(admin, "/api/po/jobs/JOB-000017/ship-to"),
    );
    expect(body.job_no).toBe("2026.384");
    expect(body.site_phase).toBe(2); // the STORED site, not the name's 7
  });

  it("0064: a stored two-segment job_no reports site_phase 0, not the name's segment", async () => {
    await env.DB.prepare("UPDATE jobs SET project_name=?1, job_no=?2, site_phase=0 WHERE job_id=?3")
      .bind("2026.384.5 Named Site", "2026.384", "JOB-000017")
      .run();
    const body = await json<{ job_no: string; site_phase: number }>(
      await g(admin, "/api/po/jobs/JOB-000017/ship-to"),
    );
    expect(body.site_phase).toBe(0);
  });

  it("404s an unknown job_id", async () => {
    const res = await g(admin, "/api/po/jobs/JOB-999999/ship-to");
    expect(res.status).toBe(404);
    expect(await json<{ error: string }>(res)).toEqual({ error: "not_found" });
  });

  it("bound SQL: a SQL-ish job_id is treated as a literal → 404, no injection/error", async () => {
    const res = await g(admin, `/api/po/jobs/${encodeURIComponent("JOB-000017' OR '1'='1")}/ship-to`);
    expect(res.status).toBe(404); // the param binds as a literal; it matches no row
  });
});

// ── Vendors CRUD (portal side of the §51 rider) ───────────────────────────────
describe("vendors create/update", () => {
  it("create allocates VEN-###### and stamps origin=portal, sync_state=pending, mirror_version=1", async () => {
    const res = await p(admin, "/api/po/vendors", {
      vendor_name: "New Racking Co", region: "West", supply_categories: ["racking", "modules"],
    });
    expect(res.status, await res.clone().text()).toBe(201);
    const { vendor_key } = await json<{ vendor_key: string }>(res);
    // VEN-000001 is seeded — the allocator self-heals past the max suffix seen → 000002.
    expect(vendor_key).toBe("VEN-000002");
    const row = (await env.DB.prepare("SELECT * FROM po_vendors WHERE vendor_key=?1").bind(vendor_key).first())!;
    expect(row.origin).toBe("portal");
    expect(row.sync_state).toBe("pending");
    expect(row.mirror_version).toBe(1);
    expect(row.supply_categories).toBe('["racking","modules"]');
  });

  it("allocation self-heals past a down-synced high key (never collides)", async () => {
    await seedVendor("VEN-000009");
    const res = await p(admin, "/api/po/vendors", { vendor_name: "After Down-Sync" });
    const { vendor_key } = await json<{ vendor_key: string }>(res);
    expect(vendor_key).toBe("VEN-000010");
  });

  it("update re-dirties: sync_state=pending, mirror_version bumped, origin=portal; deactivate rides active:0 (never a delete)", async () => {
    const res = await p(admin, "/api/po/vendors/VEN-000001/update", {
      vendor_name: "Renamed Vendor", active: 0,
    });
    expect(res.status, await res.clone().text()).toBe(200);
    const row = (await env.DB.prepare("SELECT * FROM po_vendors WHERE vendor_key='VEN-000001'").first())!;
    expect(row.vendor_name).toBe("Renamed Vendor");
    expect(row.active).toBe(0);
    expect(row.origin).toBe("portal");
    expect(row.sync_state).toBe("pending");
    expect(row.mirror_version).toBe(1);
    // Row still exists (deactivate-not-delete) and the audit landed in the same batch (W4).
    const audits = await env.DB.prepare("SELECT * FROM audit_log WHERE action='po_vendor_update'").all();
    expect(audits.results!.length).toBe(1);
  });

  it("update of an unknown vendor → 404 and writes NO audit row", async () => {
    expect((await p(admin, "/api/po/vendors/VEN-999999/update", { vendor_name: "Ghost" })).status).toBe(404);
    const audits = await env.DB.prepare("SELECT * FROM audit_log WHERE action='po_vendor_update'").all();
    expect(audits.results!.length).toBe(0);
  });
});

// ── Draft → generate: cents math + totals assert + numbering ─────────────────
describe("draft → generate", () => {
  it("draft create stores SERVER-computed cents (extended/subtotal/tax/total)", async () => {
    const res = await p(admin, "/api/po/drafts", draftBody());
    expect(res.status, await res.clone().text()).toBe(201);
    const { id, totals } = await json<{ id: number; totals: Record<string, number> }>(res);
    expect(totals.subtotal_cents).toBe(EXPECTED.subtotal_cents);
    expect(totals.tax_rate_bp).toBe(IL_BP); // resolved IL auto rate (from bundled tax.json)
    expect(totals.tax_cents).toBe(EXPECTED.tax_cents);
    expect(totals.total_cents).toBe(EXPECTED.total_cents);
    const row = await poRow(id);
    expect(row.status).toBe("draft");
    expect(row.po_number).toBeNull(); // nullable-until-allocated (D7)
    expect(row.revision).toBeNull();
    const lines = await env.DB.prepare("SELECT * FROM po_line_items WHERE po_id=?1 ORDER BY position").bind(id).all();
    expect(lines.results!.length).toBe(2);
    expect((lines.results![0] as Record<string, unknown>).extended_cents).toBe(123_450);
    expect((lines.results![1] as Record<string, unknown>).extended_cents).toBe(2_500);
  });

  it("generate: happy path allocates the D7 number, signs, and queues", async () => {
    const created = await p(admin, "/api/po/drafts", draftBody());
    const { id } = await json<{ id: number }>(created);
    const gen = await p(admin, `/api/po/drafts/${id}/generate`, EXPECTED);
    expect(gen.status, await gen.clone().text()).toBe(200);
    const out = await json<{ po_number: string; revision: number }>(gen);
    expect(out.po_number).toBe("2026.001.2.0.0"); // {job_no}.{site}.{supersede}.{rev}
    expect(out.revision).toBe(0);
    const row = await poRow(id);
    expect(row.status).toBe("queued");
    expect(row.hmac).toBeTruthy();
  });

  it("generate REFUSES a blank terms_profile_id (422 missing_terms_profile); the DRAFT still saves", async () => {
    // Same gap class as subcontract owner_entity: blank terms_profile_id used to queue then fence
    // permanently at po_terms_error. Draft-level stays lenient; generate refuses the blank.
    const created = await p(admin, "/api/po/drafts", draftBody({ terms_profile_id: "" }));
    expect(created.status, "blank-terms draft should still save").toBe(201);
    const { id } = await json<{ id: number }>(created);
    const gen = await p(admin, `/api/po/drafts/${id}/generate`, EXPECTED);
    expect(gen.status).toBe(422);
    expect((await json<{ error: string }>(gen)).error).toBe("missing_terms_profile");
  });

  it("generate: totals mismatch is REJECTED (409) and the draft is untouched", async () => {
    const created = await p(admin, "/api/po/drafts", draftBody());
    const { id } = await json<{ id: number }>(created);
    const gen = await p(admin, `/api/po/drafts/${id}/generate`, { ...EXPECTED, total_cents: EXPECTED.total_cents + 1 });
    expect(gen.status).toBe(409);
    const body = await json<{ error: string; recomputed: Record<string, number> }>(gen);
    expect(body.error).toBe("totals_mismatch");
    expect(body.recomputed.total_cents).toBe(EXPECTED.total_cents);
    const row = await poRow(id);
    expect(row.status).toBe("draft");
    expect(row.po_number).toBeNull();
  });

  it("two generates in the same family allocate DISTINCT revisions (no collision)", async () => {
    const idA = await makeQueued(admin);
    const created = await p(admin, "/api/po/drafts", draftBody());
    const { id: idB } = await json<{ id: number }>(created);
    const gen = await p(admin, `/api/po/drafts/${idB}/generate`, EXPECTED);
    expect(gen.status).toBe(200);
    const a = await poRow(idA);
    const b = await poRow(idB);
    expect(a.po_number).toBe("2026.001.2.0.0");
    expect(b.po_number).toBe("2026.001.2.0.1"); // MAX(revision)+1 within the family
    expect(a.po_number).not.toBe(b.po_number);
  });

  it("the UNIQUE family index BITES on a duplicate allocated tuple (the race backstop)", async () => {
    // Inject the synthetic violation directly (two serial route calls cannot race in a
    // test) — prove-the-control-bites: the backstop the losing generate would hit.
    await env.DB.prepare(
      "INSERT INTO purchase_orders (po_uuid, po_number, job_no, site_phase, supersede_seq, revision, vendor_key, created_by, status) " +
        "VALUES ('u-1','2026.001.2.0.0','2026.001',2,0,0,'VEN-000001','t','queued')",
    ).run();
    await expect(
      env.DB.prepare(
        "INSERT INTO purchase_orders (po_uuid, po_number, job_no, site_phase, supersede_seq, revision, vendor_key, created_by, status) " +
          "VALUES ('u-2','2026.001.2.0.0-dup','2026.001',2,0,0,'VEN-000001','t','queued')",
      ).run(),
    ).rejects.toThrowError(/UNIQUE constraint failed/);
  });

  it("draft is NOT editable after generate (409 not_draft; lines untouched)", async () => {
    const id = await makeQueued(admin);
    const upd = await p(admin, `/api/po/drafts/${id}/update`, draftBody({
      line_items: [{ part_number: "X", description: "Swapped", qty: 1, unit: "ea", unit_cost_cents: 1 }],
    }));
    expect(upd.status).toBe(409);
    expect((await json<{ error: string }>(upd)).error).toBe("not_draft");
    const lines = await env.DB.prepare("SELECT COUNT(*) n FROM po_line_items WHERE po_id=?1").bind(id).first<{ n: number }>();
    expect(lines!.n).toBe(2); // the original two, not the swapped one
  });

  it("every draft update bumps draft_version; generate pins on the CURRENT version (stale-snapshot guard)", async () => {
    // The W5/W8 review finding: generate's read→sign→commit window must refuse to queue a
    // row whose HMAC signed a snapshot a concurrent edit replaced. The interleave itself
    // isn't deterministically drivable through the route surface (D1 serializes statements,
    // not requests), so this pins the two testable halves of the mechanism: the version
    // bump on every update, and generate binding the CURRENT (not initial) version.
    const created = await p(admin, "/api/po/drafts", draftBody());
    const { id } = await json<{ id: number }>(created);
    const v = async () =>
      (await env.DB.prepare("SELECT draft_version v FROM purchase_orders WHERE id=?1").bind(id).first<{ v: number }>())!.v;
    expect(await v()).toBe(0);
    expect((await p(admin, `/api/po/drafts/${id}/update`, draftBody())).status).toBe(200);
    expect(await v()).toBe(1);
    expect((await p(admin, `/api/po/drafts/${id}/update`, draftBody())).status).toBe(200);
    expect(await v()).toBe(2);
    // generate succeeds against the twice-updated draft — it pinned draft_version=2, not 0.
    const gen = await p(admin, `/api/po/drafts/${id}/generate`, EXPECTED);
    expect(gen.status, await gen.clone().text()).toBe(200);
  });

  it("'auto' tax FAILS CLOSED on a state missing from the table", async () => {
    const res = await p(admin, "/api/po/drafts", draftBody({ ship_to_state: "TX" }));
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("unknown_tax_state");
  });

  it("unknown vendor is a 422 (reference, not shape)", async () => {
    const res = await p(admin, "/api/po/drafts", draftBody({ vendor_key: "VEN-000042" }));
    expect(res.status).toBe(422);
  });

  it("per-watt integer math: extended = round(watts × ppw_microcents / 1e6)", () => {
    const line = { qty: 1, unit_cost_cents: null, watts: 400_000, price_per_watt_microcents: 32_500_000 };
    expect(lineExtendedCents(line)).toBe(13_000_000); // 400kW at $0.325/W = $130,000.00
    const totals = computeTotals(
      [{ ...line, position: 1, part_number: "", description: "mods", unit: "W", extended_cents: lineExtendedCents(line), panels: null, pallets: null }],
      "exempt", 0, 0, "IL",
    );
    expect(totals).toEqual({ subtotal_cents: 13_000_000, tax_rate_bp: 0, tax_cents: 0, total_cents: 13_000_000 });
  });
});

// ── HMAC (domain po:v1) ───────────────────────────────────────────────────────
describe("po:v1 HMAC", () => {
  it("the queued row's hmac recomputes from the canonical string (domain-prefixed, stable shape)", async () => {
    const id = await makeQueued(admin);
    const res = await call("/api/po/internal/pending", { bearer: PO_BEARER });
    const { pending } = await json<{ pending: (PoRow & { hmac: string; line_items: PoLine[] })[] }>(res);
    expect(pending.length).toBe(1);
    const row = pending[0];
    expect(row.id).toBe(id);
    const canonical = poCanonicalString(row.id, row.po_number!, canonicalPoJson(row, row.line_items));
    expect(canonical.startsWith("po:v1\n")).toBe(true); // the NEW domain — never replayable as a submission
    expect(row.hmac).toBe(await hmacHex(HMAC_SECRET, canonical));
  });

  it("a submission-domain signature over the same content does NOT match (domain separation)", async () => {
    const id = await makeQueued(admin);
    const res = await call("/api/po/internal/pending", { bearer: PO_BEARER });
    const { pending } = await json<{ pending: (PoRow & { hmac: string; line_items: PoLine[] })[] }>(res);
    const row = pending[0];
    const undomained = [String(id), row.po_number!, canonicalPoJson(row, row.line_items)].join("\n");
    expect(row.hmac).not.toBe(await hmacHex(HMAC_SECRET, undomained));
  });
});

// ── Bearer tier (requirePoToken) + cross-token isolation ─────────────────────
describe("requirePoToken tier", () => {
  const PO_INTERNAL_ROUTES: [string, string, unknown][] = [
    ["GET", "/api/po/internal/pending", undefined],
    ["POST", "/api/po/internal/mark-filed", { po_id: 1 }],
    ["POST", "/api/po/internal/status-sync", { updates: [{ po_id: 1, status: "approved" }] }],
    ["POST", "/api/po/internal/vendors/sync", { vendors: [{ vendor_key: "VEN-000001", vendor_name: "V" }] }],
    ["GET", "/api/po/internal/vendors/pending", undefined],
    ["POST", "/api/po/internal/vendors/mark-mirrored", { updates: [{ vendor_key: "VEN-000001", mirrored_version: 1 }] }],
  ];

  it("rejects no-token, a wrong token, and EVERY sibling tier's token on EVERY po-internal route", async () => {
    for (const [method, path, body] of PO_INTERNAL_ROUTES) {
      const init = body === undefined ? { method } : { method, body: JSON.stringify(body) };
      expect((await call(path, init)).status, `${path} no token`).toBe(401);
      for (const bearer of ["wrong-token", INTERNAL_BEARER, FIELDOPS_BEARER, ADMIN_BEARER]) {
        expect((await call(path, { ...init, bearer })).status, `${path} bearer=${bearer}`).toBe(401);
      }
      expect((await call(path, { ...init, bearer: PO_BEARER })).status, `${path} po token`).not.toBe(401);
    }
  });

  it("the PO token does NOT open the sibling tiers (reverse isolation)", async () => {
    expect((await call("/api/internal/pending", { bearer: PO_BEARER })).status).toBe(401);
    expect((await call("/api/internal/fieldops/pending-jobs", { bearer: PO_BEARER })).status).toBe(401);
    expect(
      (await call("/api/internal/admin/users", {
        method: "POST", bearer: PO_BEARER, body: JSON.stringify({ username: "x.y", password: "password123", role: "submitter" }),
      })).status,
    ).toBe(401);
  });
});

// ── mark-filed (queued → pending_review) ──────────────────────────────────────
describe("mark-filed", () => {
  it("flips queued→pending_review with box_file_id; a replay is a found:false no-op", async () => {
    const id = await makeQueued(admin);
    const first = await call("/api/po/internal/mark-filed", {
      method: "POST", bearer: PO_BEARER, body: JSON.stringify({ po_id: id, box_file_id: "bx-1" }),
    });
    expect(await json<{ ok: boolean; found: boolean }>(first)).toEqual({ ok: true, found: true });
    let row = await poRow(id);
    expect(row.status).toBe("pending_review");
    expect(row.box_file_id).toBe("bx-1");
    // Idempotent replay: no state change, no second audit row.
    const replay = await call("/api/po/internal/mark-filed", {
      method: "POST", bearer: PO_BEARER, body: JSON.stringify({ po_id: id, box_file_id: "bx-OTHER" }),
    });
    expect(await json<{ ok: boolean; found: boolean }>(replay)).toEqual({ ok: true, found: false });
    row = await poRow(id);
    expect(row.box_file_id).toBe("bx-1");
    const audits = await env.DB.prepare("SELECT * FROM audit_log WHERE action='po_mark_filed'").all();
    expect(audits.results!.length).toBe(1); // the changes()=1 guard kept the replay silent
  });
});

// ── status-sync + the supersession flip ───────────────────────────────────────
describe("status-sync + supersession", () => {
  it("approved only from pending_review; a stale replay cannot regress", async () => {
    const id = await makeQueued(admin);
    // approved while still 'queued' (mark-filed not yet received) → guarded no-op
    await call("/api/po/internal/status-sync", {
      method: "POST", bearer: PO_BEARER, body: JSON.stringify({ updates: [{ po_id: id, status: "approved" }] }),
    });
    expect((await poRow(id)).status).toBe("queued");
    await driveToSent(id);
    expect((await poRow(id)).status).toBe("sent");
    // stale 'approved' replay after 'sent' → guarded no-op (no regression)
    await call("/api/po/internal/status-sync", {
      method: "POST", bearer: PO_BEARER, body: JSON.stringify({ updates: [{ po_id: id, status: "approved" }] }),
    });
    expect((await poRow(id)).status).toBe("sent");
  });

  it("supersede clones a SENT PO (seq+1, revision reset, lines cloned); sending the successor flips the old PO in the same batch", async () => {
    const idA = await makeQueued(admin);
    await driveToSent(idA);

    // supersede → a new DRAFT clone
    const sup = await p(admin, `/api/po/${idA}/supersede`);
    expect(sup.status, await sup.clone().text()).toBe(201);
    const { id: idB } = await json<{ id: number }>(sup);
    const b = await poRow(idB);
    expect(b.status).toBe("draft");
    expect(b.supersede_seq).toBe(1);
    expect(b.revision).toBeNull();
    expect(b.po_number).toBeNull();
    expect(b.supersedes_po_id).toBe(idA);
    const bLines = await env.DB.prepare("SELECT * FROM po_line_items WHERE po_id=?1 ORDER BY position").bind(idB).all();
    expect(bLines.results!.length).toBe(2); // cloned
    // W4 regression (security-review BLOCKER): the clone's audit row must exist for a
    // MULTI-line PO — auditStmtIfChanged placed after the line-items INSERT read changes()=2
    // and silently skipped it. The audit stmt now sits directly after the parent INSERT.
    const cloneAudit = await env.DB.prepare("SELECT * FROM audit_log WHERE action='po_supersede_clone'").all();
    expect(cloneAudit.results!.length).toBe(1);
    // Old PO untouched until the successor ships (D7).
    expect((await poRow(idA)).status).toBe("sent");

    // Double-submit guard: a second supersede while the successor draft is live is a 409
    // naming the existing draft, not a sibling clone at the same supersede_seq.
    const dup = await p(admin, `/api/po/${idA}/supersede`);
    expect(dup.status).toBe(409);
    expect((await json<{ error: string; existing_id: number }>(dup)).existing_id).toBe(idB);

    // generate the clone (its own family branch: supersede_seq=1 → revision 0)
    const gen = await p(admin, `/api/po/drafts/${idB}/generate`, EXPECTED);
    expect(gen.status, await gen.clone().text()).toBe(200);
    expect((await json<{ po_number: string }>(gen)).po_number).toBe("2026.001.2.1.0");

    // successor reaches 'sent' → the predecessor flips 'superseded' in the SAME batch
    await driveToSent(idB);
    expect((await poRow(idB)).status).toBe("sent");
    expect((await poRow(idA)).status).toBe("superseded");
    const flip = await env.DB.prepare("SELECT * FROM audit_log WHERE action='po_superseded_flip'").all();
    expect(flip.results!.length).toBe(1);
  });

  it("supersede refuses a non-sent PO", async () => {
    const created = await p(admin, "/api/po/drafts", draftBody());
    const { id } = await json<{ id: number }>(created);
    const sup = await p(admin, `/api/po/${id}/supersede`);
    expect(sup.status).toBe(409);
  });
});

// ── cancel guards ─────────────────────────────────────────────────────────────
describe("cancel", () => {
  it("cancels draft / queued / pending_review; refuses approved / sent; 404s unknown", async () => {
    const created = await p(admin, "/api/po/drafts", draftBody());
    const { id: draftId } = await json<{ id: number }>(created);
    expect((await p(admin, `/api/po/${draftId}/cancel`)).status).toBe(200);
    expect((await poRow(draftId)).status).toBe("canceled");

    const sentId = await makeQueued(admin);
    await driveToSent(sentId);
    const refuse = await p(admin, `/api/po/${sentId}/cancel`);
    expect(refuse.status).toBe(409);
    expect((await json<{ error: string }>(refuse)).error).toBe("not_cancelable");
    expect((await poRow(sentId)).status).toBe("sent");

    expect((await p(admin, "/api/po/999999/cancel")).status).toBe(404);
  });

  it("delete: HARD-removes a DRAFT + its line items; refuses a generated row (no orphan); 404s unknown", async () => {
    const created = await p(admin, "/api/po/drafts", draftBody());
    const { id: draftId } = await json<{ id: number }>(created);
    const nLines = async (id: number) =>
      (await env.DB.prepare("SELECT COUNT(*) n FROM po_line_items WHERE po_id=?1").bind(id).first<{ n: number }>())!.n;
    expect(await nLines(draftId)).toBeGreaterThan(0);

    expect((await p(admin, `/api/po/${draftId}/delete`)).status).toBe(200);
    expect(await env.DB.prepare("SELECT id FROM purchase_orders WHERE id=?1").bind(draftId).first()).toBeNull();
    expect(await nLines(draftId)).toBe(0);

    const queuedId = await makeQueued(admin);
    const del = await p(admin, `/api/po/${queuedId}/delete`);
    expect(del.status).toBe(409);
    expect((await json<{ error: string }>(del)).error).toBe("not_deletable");
    expect((await poRow(queuedId)).status).toBe("queued");
    expect(await nLines(queuedId)).toBeGreaterThan(0);

    expect((await p(admin, "/api/po/999999/delete")).status).toBe(404);
  });
});

// ── vendors sync (down-sync fence, empty refusal, watermark) ──────────────────
describe("vendors internal sync", () => {
  it("down-sync upserts synced rows but the dirty-row fence preserves a pending portal edit", async () => {
    await seedVendor("VEN-000002", { vendor_name: "Sheet Two" });
    // Portal edit dirties VEN-000001.
    await p(admin, "/api/po/vendors/VEN-000001/update", { vendor_name: "Portal Edit" });
    const res = await call("/api/po/internal/vendors/sync", {
      method: "POST",
      bearer: PO_BEARER,
      body: JSON.stringify({
        vendors: [
          { vendor_key: "VEN-000001", vendor_name: "Sheet Clobber", active: 1 },
          { vendor_key: "VEN-000002", vendor_name: "Sheet Two Renamed", active: 1 },
          { vendor_key: "VEN-000003", vendor_name: "Sheet Three (new)", active: 1 },
        ],
      }),
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await json<{ ok: boolean; upserted: number; skipped_dirty: number }>(res)).toEqual({
      ok: true, upserted: 2, skipped_dirty: 1,
    });
    const dirty = (await env.DB.prepare("SELECT * FROM po_vendors WHERE vendor_key='VEN-000001'").first())!;
    expect(dirty.vendor_name).toBe("Portal Edit"); // the fence held
    expect(dirty.sync_state).toBe("pending");
    expect(dirty.origin).toBe("portal");
    const two = (await env.DB.prepare("SELECT vendor_name, sync_state FROM po_vendors WHERE vendor_key='VEN-000002'").first())!;
    expect(two.vendor_name).toBe("Sheet Two Renamed"); // non-dirty rows DO full-replace
    const three = (await env.DB.prepare("SELECT origin, sync_state FROM po_vendors WHERE vendor_key='VEN-000003'").first())!;
    expect(three.origin).toBe("smartsheet");
    expect(three.sync_state).toBe("synced");
  });

  it("REFUSES an empty payload (a Smartsheet read-miss must never wipe the cache)", async () => {
    const res = await call("/api/po/internal/vendors/sync", {
      method: "POST", bearer: PO_BEARER, body: JSON.stringify({ vendors: [] }),
    });
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("empty_vendors");
  });

  it("up-sync: pending read exposes the dirty row; mark-mirrored flips it ONLY at the unchanged watermark", async () => {
    await p(admin, "/api/po/vendors/VEN-000001/update", { vendor_name: "Portal Edit" }); // mirror_version 1
    const pend = await call("/api/po/internal/vendors/pending", { bearer: PO_BEARER });
    const { vendors } = await json<{ vendors: { vendor_key: string; mirror_version: number }[] }>(pend);
    expect(vendors.map((v) => v.vendor_key)).toEqual(["VEN-000001"]);
    expect(vendors[0].mirror_version).toBe(1);

    // A SECOND portal edit lands between the daemon's read and its commit → watermark moves.
    await p(admin, "/api/po/vendors/VEN-000001/update", { vendor_name: "Portal Edit v2" }); // mirror_version 2
    const stale = await call("/api/po/internal/vendors/mark-mirrored", {
      method: "POST", bearer: PO_BEARER,
      body: JSON.stringify({ updates: [{ vendor_key: "VEN-000001", mirrored_version: 1 }] }),
    });
    expect(await json<{ flipped: number; stale: number }>(stale)).toMatchObject({ flipped: 0, stale: 1 });
    expect((await env.DB.prepare("SELECT sync_state FROM po_vendors WHERE vendor_key='VEN-000001'").first())!.sync_state).toBe("pending");

    // The daemon re-reads (version 2) and commits at the live watermark → flips.
    const fresh = await call("/api/po/internal/vendors/mark-mirrored", {
      method: "POST", bearer: PO_BEARER,
      body: JSON.stringify({ updates: [{ vendor_key: "VEN-000001", mirrored_version: 2 }] }),
    });
    expect(await json<{ flipped: number; stale: number }>(fresh)).toMatchObject({ flipped: 1, stale: 0 });
    const row = (await env.DB.prepare("SELECT sync_state, mirrored_version FROM po_vendors WHERE vendor_key='VEN-000001'").first())!;
    expect(row.sync_state).toBe("synced");
    expect(row.mirrored_version).toBe(2);
  });
});
