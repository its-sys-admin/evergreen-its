/// <reference types="vite/client" />
import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, p, g, json } from "./helpers";
import { hmacHex } from "../worker/hmac";
import { canonicalSubJson, subCanonicalString, sovExtendedCents, computeSubtotal } from "../worker/subcontract";
import type { SubcontractRow, SovLine } from "../worker/subcontract";
// The versioned subcontract config the WORKER bundles at build time (worker/subcontract.ts:11-13).
// Import the SAME files so every assertion about served/computed config tracks the live values
// instead of pinning them. GUARD (HOUSE_REFLEXES §5 — the config-editor merge-blocker class): never
// hard-code contractor/terms CONTENT here. The §50 config editor auto-merges edits on green CI, so a
// pinned entity/version/legal_review red-lights the instant the operator edits it and strands the edit
// PR. Assert derived/served-equals-source/shape only. In particular standard_subcontract v1 ships
// legal_review "pending" BY DESIGN (Layer-A gate) — derive that assertion from the source, never pin.
import termsManifest from "../../subcontracts/terms/manifest.json";
import contractorConfig from "../../subcontracts/config/contractor.json";
// SC-S3b Exhibit A — the raw per-trade Article II bodies the WORKER bundles (worker/subcontract.ts
// EXHIBIT_RAW). Import the SAME files so the served-equals-source drift check tracks the corpus instead
// of pinning a substring (HOUSE_REFLEXES §5). These art2 bodies carry NO provenance header, so the
// route's stripTermsHeader is a no-op → served === raw. Also import the manifest for the trade_map.
import exhibitManifest from "../../subcontracts/exhibit/manifest.json";
import fencingArt2 from "../../subcontracts/exhibit/art2/fencing.md?raw";
import electricalArt2 from "../../subcontracts/exhibit/art2/electrical.md?raw";

// ─────────────────────────────────────────────────────────────────────────────
// Subcontracts workstream SC-S3c — worker/subcontract.ts + migrations 0049-0052.
//
// NOTE ON THE FILE NAME: vitest.config.ts collects `test/**/*.test.ts` ONLY — a `.spec.ts` file
// would be silently NOT RUN (the "green CI on a missing test proves nothing" class,
// HOUSE_REFLEXES §2). Hence `subcontract.test.ts`.
//
// Coverage: the cap.subcontracts.manage gate (0051), the terms/config feeds (pending legal_review
// derived-not-pinned), the subcontractor cache CRUD (state-validated, self-healing key alloc),
// draft→generate lump-sum cents math + the SOV-sums-to-price gate + the D7 number alloc + the UNIQUE
// family backstop, the sub:v1 HMAC shape + domain separation (never replayable as a PO), the NEW
// requireSubToken bearer tier + cross-token isolation (both directions, incl. the PO + config
// siblings), mark-filed idempotency, the status-sync machine WITH the 'executed' terminal + the
// supersession flip on 'sent', the cancel guards (refusing executed too), and the subcontractor
// down-sync dirty-row fence + empty refusal + malformed-batch reject + the mark-mirrored watermark.
// ─────────────────────────────────────────────────────────────────────────────

const SUB_BEARER = "test-sub-token"; // == PORTAL_SUB_API_TOKEN (vitest.config.ts)
const PO_BEARER = "test-po-token"; // sibling tier — must be REJECTED on /api/subcontracts/internal/*
const INTERNAL_BEARER = "test-internal-token"; // portal_poll's token — REJECTED too
const FIELDOPS_BEARER = "test-fieldops-token"; // mirror daemon's token — REJECTED too
const ADMIN_BEARER = "test-admin-token"; // operator provisioning token — REJECTED too
const CONFIG_BEARER = "test-config-token"; // config daemon's token — REJECTED too
const HMAC_SECRET = "test-hmac-payload-secret"; // == HMAC_PAYLOAD_SECRET (vitest.config.ts)

async function seedSubcontractor(subKey: string, over: Partial<Record<string, unknown>> = {}): Promise<void> {
  // NOTE: `state` (2-letter USPS), NOT `region` (the 0052 table rebuild). trades is JSON text.
  await env.DB.prepare(
    "INSERT INTO subcontractors (sub_key, sub_name, contact_email, state, trades, active, origin, sync_state, mirror_version, mirrored_version) " +
      "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
  )
    .bind(
      subKey,
      (over.sub_name as string) ?? `Sub ${subKey}`,
      (over.contact_email as string) ?? "pm@subcontractor.example",
      (over.state as string) ?? "VA",
      (over.trades as string) ?? '["electrical"]',
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
    sub_key: "SUB-000001",
    job_no: "2026.001",
    site_phase: 2,
    job_id: "JOB-000017",
    job_name: "Sunrise Solar",
    project_name: "2026.001 Sunrise Solar",
    owner_entity: "Bonacci 1, LLC", // the SPV — the 3-tier owner fan-out
    prime_contractor: "Evergreen Renewables of Virginia LLC",
    site_name: "Sunrise Array",
    site_address: "100 Array Rd, Rockford IL",
    governing_law_state: "VA", // parameterized jurisdiction (0050) — NOT hardcoded
    trade: "electrical",
    price_basis: "fixed",
    contract_price_cents: 5_000_000, // §2.1 source of truth ($50,000.00)
    retainage_bp: 1000,
    start_date: "2026-08-01",
    completion_date: "2026-12-15",
    terms_profile_id: "standard_subcontract",
    terms_version: "v1",
    template_family: "long_form",
    exhibit_a_template_id: "",
    exhibit_a_template_version: "",
    exhibit_a_work_text: "Furnish and install all electrical scope per the plans.",
    scope_summary: "Electrical",
    approver_name: "Alex Approver",
    approver_title: "Director of Construction",
    // SOV: a single derived line that MUST sum to contract_price_cents (the sums-to-price gate).
    // NO tax, NO shipping, NO per-watt (0050 dropped those columns) — a subcontract is a lump sum.
    sov_lines: [{ item_number: "1", description: "Electrical scope (lump)", qty: 1, unit: "ls", unit_price_cents: 5_000_000 }],
    ...over,
  };
}

// Server math for draftBody(): 1×5_000_000 = 5_000_000 == contract_price_cents (SOV gate passes).
// PURE line math, config-independent — no tax/config-derived number exists in the subcontract money
// path, so pinning this literal is safe (unlike the PO tax path).
const CONTRACT_PRICE_CENTS = 5_000_000;

async function subRow(id: number): Promise<Record<string, unknown>> {
  return (await env.DB.prepare("SELECT * FROM subcontracts WHERE id=?1").bind(id).first())!;
}

/** draft → queued helper: create + generate with the reconciling contract price; returns the id. */
async function makeQueued(admin: string, over: Record<string, unknown> = {}): Promise<number> {
  const created = await p(admin, "/api/subcontracts/drafts", draftBody(over));
  expect(created.status, await created.clone().text()).toBe(201);
  const { id } = await json<{ id: number }>(created);
  const gen = await p(admin, `/api/subcontracts/drafts/${id}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
  expect(gen.status, await gen.clone().text()).toBe(200);
  return id;
}

function markFiled(id: number): Promise<Response> {
  return call("/api/subcontracts/internal/mark-filed", {
    method: "POST", bearer: SUB_BEARER, body: JSON.stringify({ sc_id: id, box_file_id: `bx-${id}` }),
  });
}
function statusSync(id: number, status: string): Promise<Response> {
  return call("/api/subcontracts/internal/status-sync", {
    method: "POST", bearer: SUB_BEARER, body: JSON.stringify({ updates: [{ sc_id: id, status }] }),
  });
}
/** queued → the given terminal chain (mark-filed → each status through the internal surface). */
async function driveTo(id: number, ...statuses: string[]): Promise<void> {
  expect((await markFiled(id)).status).toBe(200);
  for (const s of statuses) expect((await statusSync(id, s)).status, `status-sync ${s}`).toBe(200);
}

let admin: string, submitter: string;
beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM sov_lines"),
    env.DB.prepare("DELETE FROM subcontracts"),
    env.DB.prepare("DELETE FROM subcontractors"),
    env.DB.prepare("UPDATE subcontractor_counter SET last_value=0 WHERE id=1"),
  ]);
  await provision("admin.sub", "password123", "admin");
  await provision("submitter.sub", "password123", "submitter");
  admin = await login("admin.sub", "password123");
  submitter = await login("submitter.sub", "password123");
  await seedSubcontractor("SUB-000001");
});

// ── Capability gate (migration 0051) ──────────────────────────────────────────
describe("cap.subcontracts.manage gate", () => {
  it("403s a submitter on every browser subcontract surface; 200s an admin", async () => {
    expect((await g(submitter, "/api/subcontracts/subcontractors")).status).toBe(403);
    expect((await g(submitter, "/api/subcontracts/subs")).status).toBe(403);
    expect((await g(submitter, "/api/subcontracts/terms")).status).toBe(403);
    expect((await g(submitter, "/api/subcontracts/config")).status).toBe(403);
    expect((await p(submitter, "/api/subcontracts/drafts", draftBody())).status).toBe(403);
    expect((await p(submitter, "/api/subcontracts/subcontractors", { sub_name: "X" })).status).toBe(403);
    expect((await g(admin, "/api/subcontracts/subcontractors")).status).toBe(200);
  });

  it("401s an unauthenticated caller (no session)", async () => {
    expect((await call("/api/subcontracts/subcontractors")).status).toBe(401);
    expect((await call("/api/subcontracts/terms")).status).toBe(401);
    expect((await call("/api/subcontracts/config")).status).toBe(401);
  });
});

// ── Terms + config feeds (SC-S2 files, build-time imported) ───────────────────
describe("terms + config wiring", () => {
  it("terms serves the manifest profiles: ids, kinds, current version, tokens, attach render_line", async () => {
    const res = await g(admin, "/api/subcontracts/terms");
    expect(res.status, await res.clone().text()).toBe(200);
    const { profiles } = await json<{ profiles: Array<Record<string, unknown>> }>(res);
    const byId = Object.fromEntries(profiles.map((pr) => [pr.id, pr]));
    // One vocabulary with the ITS_Subcontractors 'Default Terms Profile' picklist (derived parity).
    expect(Object.keys(byId).sort()).toEqual(Object.keys(termsManifest.profiles).sort());
    expect(byId.standard_subcontract.kind).toBe("library");
    const std = termsManifest.profiles.standard_subcontract;
    expect(byId.standard_subcontract.current_version).toBe(std.current_version); // derived, not pinned
    expect(byId.standard_subcontract.tokens).toEqual(
      (std.versions as Record<string, { tokens: string[] }>)[std.current_version].tokens,
    );
    expect(byId.negotiated_msa.kind).toBe("attach");
    expect(String(byId.negotiated_msa.render_line)).toContain("THE WORK DESCRIBED HEREIN IS PERFORMED UNDER");
    // Curated view — renderer implementation detail (versions/sha256/file) stays off the wire.
    for (const pr of profiles) expect(pr).not.toHaveProperty("versions");
  });

  it("config serves the contractor identity + payment-terms defaults + state list (comment stripped, NO tax)", async () => {
    const res = await g(admin, "/api/subcontracts/config");
    expect(res.status, await res.clone().text()).toBe(200);
    const cfg = await json<{
      contractor: { entity: string; address_lines: string[]; phone: string; signature_entity: string; prime_contractor_default: string };
      payment_terms: { retainage_bp: number };
      governing_law_states: string[];
    }>(res);
    // Served config MATCHES the bundled source (single source, no drift) — never pinned literals.
    expect(cfg.contractor.entity).toBe(contractorConfig.entity);
    expect(cfg.contractor.address_lines).toEqual(contractorConfig.address_lines);
    expect(cfg.contractor.phone).toBe(contractorConfig.phone);
    expect(cfg.contractor.signature_entity).toBe(contractorConfig.signature_entity);
    expect(cfg.contractor.prime_contractor_default).toBe(contractorConfig.prime_contractor_default);
    // A subcontract has NO tax table (the PO tax half is deliberately absent).
    expect(cfg).not.toHaveProperty("tax");
    // Governing-law list is a non-empty set of 2-letter codes incl. the corpus default VA.
    expect(cfg.governing_law_states).toContain("VA");
    expect(cfg.governing_law_states.every((s) => /^[A-Z]{2}$/.test(s))).toBe(true);
    // Maintainer comment/config_version fields never reach the wire.
    expect(cfg).not.toHaveProperty("comment");
    expect(cfg.contractor).not.toHaveProperty("comment");
  });
});

// ── Terms edit-text pre-fill (GET /api/subcontracts/terms/:profile_id/text) ───
describe("terms edit-text pre-fill", () => {
  it("serves the current library version's body, header-stripped, derived from the manifest", async () => {
    const res = await g(admin, "/api/subcontracts/terms/standard_subcontract/text");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<{ profile_id: string; version: string; text: string }>(res);
    expect(body.profile_id).toBe("standard_subcontract");
    expect(body.version).toBe(termsManifest.profiles.standard_subcontract.current_version); // derived
    expect(typeof body.text).toBe("string");
    expect(body.text.length).toBeGreaterThan(0);
    // The pre-fill serves the raw body regardless of legal_review (it is an edit convenience, not a
    // render — so this passes even though v1 is 'pending'); provenance header stripped.
    expect(body.text.startsWith("<!--")).toBe(false);
  });

  it("404s an attach profile (no versioned text) and an unknown profile", async () => {
    expect((await g(admin, "/api/subcontracts/terms/negotiated_msa/text")).status).toBe(404);
    expect((await g(admin, "/api/subcontracts/terms/does_not_exist/text")).status).toBe(404);
  });

  it("401s no session; 403s without cap.subcontracts.manage (prove-the-control-bites)", async () => {
    expect((await call("/api/subcontracts/terms/standard_subcontract/text")).status).toBe(401);
    expect((await g(submitter, "/api/subcontracts/terms/standard_subcontract/text")).status).toBe(403);
  });
});

// ── Terms versions feed (GET /api/subcontracts/terms/:profile_id/versions) ────
describe("terms versions feed", () => {
  it("serves the curated version list (id + legal_review only), legal_review DERIVED not pinned", async () => {
    const res = await g(admin, "/api/subcontracts/terms/standard_subcontract/versions");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<{
      profile_id: string;
      current_version: string;
      versions: Array<Record<string, unknown>>;
    }>(res);
    expect(body.profile_id).toBe("standard_subcontract");
    expect(body.current_version).toBe(termsManifest.profiles.standard_subcontract.current_version); // derived
    expect(body.versions.length).toBeGreaterThanOrEqual(1);
    const cur = body.versions.find((v) => v.version === body.current_version)!;
    // KEY DEVIATION vs PO: standard_subcontract v1 ships legal_review 'pending' BY DESIGN (the Layer-A
    // gate — the operator make-currents it via the config editor). Assert served-EQUALS-source, never
    // pin "cleared" (that would red-light immediately — the self-defeating config-content-pin class).
    const src = (termsManifest.profiles.standard_subcontract.versions as Record<string, { legal_review: string }>)[body.current_version];
    expect(cur.legal_review).toBe(src.legal_review);
    // Curated — file names + sha256 hashes must NOT leak (renderer implementation detail).
    for (const v of body.versions) expect(Object.keys(v).sort()).toEqual(["legal_review", "version"]);
  });

  it("404s an attach profile + an unknown profile; 401/403 gated", async () => {
    expect((await g(admin, "/api/subcontracts/terms/negotiated_msa/versions")).status).toBe(404);
    expect((await g(admin, "/api/subcontracts/terms/does_not_exist/versions")).status).toBe(404);
    expect((await call("/api/subcontracts/terms/standard_subcontract/versions")).status).toBe(401);
    expect((await g(submitter, "/api/subcontracts/terms/standard_subcontract/versions")).status).toBe(403);
  });
});

// ── Trade vocabulary feed (GET /api/subcontracts/trades) — the config-driven dropdown source ─
describe("trades vocabulary feed", () => {
  it("serves the manifest trade_map keys with insertion order preserved (the single SoT)", async () => {
    const res = await g(admin, "/api/subcontracts/trades");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<{ trades: string[] }>(res);
    // Order-preserving (NOT sorted) so a newly-created trade appends last, not into the curated grouping.
    expect(body.trades).toEqual(Object.keys(exhibitManifest.trade_map));
  });

  it("403s a submitter and 401s an unauthenticated caller (same gate as the sibling routes)", async () => {
    expect((await g(submitter, "/api/subcontracts/trades")).status).toBe(403);
    expect((await call("/api/subcontracts/trades")).status).toBe(401);
  });
});

// ── Exhibit A Article II pre-fill (GET /api/subcontracts/exhibit-templates?trade=) ─
describe("exhibit-templates Article II pre-fill", () => {
  it("resolves a Trade → its art2 body (served EQUALS the bundled source, derived not pinned)", async () => {
    const res = await g(admin, "/api/subcontracts/exhibit-templates?trade=Fencing");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<{ trade: string; template_key: string; article_ii: string }>(res);
    expect(body.trade).toBe("Fencing");
    // template_key is DERIVED from the manifest trade_map, never pinned.
    expect(body.template_key).toBe(exhibitManifest.trade_map.Fencing);
    // served-equals-source drift check — no provenance header on these bodies, so strip is a no-op.
    expect(body.article_ii).toBe(fencingArt2);
  });

  it("fans several electrical Trades onto the shared 'electrical' body (AC Electrical → electrical)", async () => {
    const res = await g(admin, "/api/subcontracts/exhibit-templates?trade=AC%20Electrical");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<{ trade: string; template_key: string; article_ii: string }>(res);
    expect(body.trade).toBe("AC Electrical");
    expect(body.template_key).toBe("electrical");
    expect(body.template_key).toBe(exhibitManifest.trade_map["AC Electrical"]);
    expect(body.article_ii).toBe(electricalArt2);
    // Distinct trades map to distinct bodies (fencing ≠ electrical) — no accidental single-body serve.
    expect(electricalArt2).not.toBe(fencingArt2);
  });

  it("400s an unknown Trade (invalid_trade); a prototype-pollution key does not resolve", async () => {
    const bad = await g(admin, "/api/subcontracts/exhibit-templates?trade=Plumbing");
    expect(bad.status).toBe(400);
    expect((await json<{ error: string }>(bad)).error).toBe("invalid_trade");
    // A missing ?trade is also invalid_trade (not a 500).
    expect((await g(admin, "/api/subcontracts/exhibit-templates")).status).toBe(400);
    // Own-property guard: __proto__ / constructor must not resolve to an Object.prototype built-in.
    expect((await g(admin, "/api/subcontracts/exhibit-templates?trade=__proto__")).status).toBe(400);
    expect((await g(admin, "/api/subcontracts/exhibit-templates?trade=constructor")).status).toBe(400);
  });

  it("401s no session; 403s without cap.subcontracts.manage (prove-the-control-bites)", async () => {
    expect((await call("/api/subcontracts/exhibit-templates?trade=Fencing")).status).toBe(401);
    expect((await g(submitter, "/api/subcontracts/exhibit-templates?trade=Fencing")).status).toBe(403);
  });
});

// ── Exhibit A config-editor routes (PR-B2 — versioned per-trade Article II templates, keyed) ─
describe("exhibit-keys config-editor routes", () => {
  const ROUTES = [
    "/api/subcontracts/exhibit-keys",
    "/api/subcontracts/exhibit-keys/civil/text",
    "/api/subcontracts/exhibit-keys/civil/versions",
  ];

  it("401s no session / 403s without cap.subcontracts.manage on all three routes", async () => {
    for (const p of ROUTES) {
      expect((await call(p)).status, p).toBe(401);
      expect((await g(submitter, p)).status, p).toBe(403);
    }
  });

  it("lists every template key with current_version, versions (legal_review), and its trades", async () => {
    const res = await g(admin, "/api/subcontracts/exhibit-keys");
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await json<{
      templates: { template_key: string; current_version: string; trades: string[]; versions: { version: string; legal_review: string }[] }[];
    }>(res);
    // Derived from the bundled manifest — assert SHAPE/derived, not pinned content (HOUSE_REFLEXES §5).
    const keys = body.templates.map((t) => t.template_key);
    expect(keys).toEqual(Object.keys(exhibitManifest.trade_templates).sort());
    const civil = body.templates.find((t) => t.template_key === "civil")!;
    expect(civil.trades).toContain("Civil");
    expect(civil.versions.every((v) => typeof v.legal_review === "string")).toBe(true);
    // AC/MV/DC Electrical all fan onto the 'electrical' key (the trade_map contract).
    const electrical = body.templates.find((t) => t.template_key === "electrical")!;
    expect(electrical.trades).toEqual(expect.arrayContaining(["AC Electrical", "MV Electrical", "DC Electrical"]));
  });

  it("serves a key's current Article II text (header-stripped) + its versions list", async () => {
    const txt = await g(admin, "/api/subcontracts/exhibit-keys/civil/text");
    expect(txt.status, await txt.clone().text()).toBe(200);
    const tbody = await json<{ template_key: string; version: string; article_ii: string }>(txt);
    expect(tbody.template_key).toBe("civil");
    expect(tbody.article_ii.length).toBeGreaterThan(0);

    const ver = await g(admin, "/api/subcontracts/exhibit-keys/civil/versions");
    const vbody = await json<{ current_version: string; versions: { version: string; legal_review: string }[] }>(ver);
    expect(vbody.versions.length).toBeGreaterThan(0);
    expect(vbody.versions.some((v) => v.version === vbody.current_version)).toBe(true);
  });

  it("404s an unknown template key / unknown version, incl. prototype-pollution keys", async () => {
    expect((await g(admin, "/api/subcontracts/exhibit-keys/nonexistent/text")).status).toBe(404);
    expect((await g(admin, "/api/subcontracts/exhibit-keys/civil/text?version=v999")).status).toBe(404);
    expect((await g(admin, "/api/subcontracts/exhibit-keys/nonexistent/versions")).status).toBe(404);
    // A __proto__/constructor version must NOT resolve an Object.prototype built-in (own-property guard).
    expect((await g(admin, "/api/subcontracts/exhibit-keys/civil/text?version=__proto__")).status).toBe(404);
    expect((await g(admin, "/api/subcontracts/exhibit-keys/civil/text?version=constructor")).status).toBe(404);
  });
});

// ── Job site-address auto-fill (C1 — GET /api/subcontracts/jobs/:job_id/site-address) ─
describe("job site-address auto-fill feed", () => {
  // Seed a job row with a synced Smartsheet address. The jobs table isn't touched by the file-level
  // beforeEach, so this describe cleans + seeds it itself (mirrors PO's ship-to test).
  async function seedJob(): Promise<void> {
    await env.DB.prepare("DELETE FROM jobs").run();
    await env.DB
      .prepare("INSERT INTO jobs (job_id, project_name, active, address) VALUES (?1,?2,1,?3)")
      .bind("JOB-000017", "2026.001 Sunrise Solar", "100 Array Rd, Rockford IL")
      .run();
  }
  beforeEach(seedJob);

  it("401s an unauthenticated caller (no session)", async () => {
    expect((await call("/api/subcontracts/jobs/JOB-000017/site-address")).status).toBe(401);
  });

  it("403s a caller without cap.subcontracts.manage (prove-the-control-bites)", async () => {
    expect((await g(submitter, "/api/subcontracts/jobs/JOB-000017/site-address")).status).toBe(403);
  });

  it("200s the SoR site_address for an admin", async () => {
    const res = await g(admin, "/api/subcontracts/jobs/JOB-000017/site-address");
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await json<Record<string, unknown>>(res)).toEqual({
      job_id: "JOB-000017",
      site_address: "100 Array Rd, Rockford IL",
    });
  });

  it("returns an empty site_address when the SoR address is blank (degrade to manual)", async () => {
    await env.DB.prepare("UPDATE jobs SET address='' WHERE job_id='JOB-000017'").run();
    const res = await g(admin, "/api/subcontracts/jobs/JOB-000017/site-address");
    expect(await json<{ job_id: string; site_address: string }>(res)).toEqual({
      job_id: "JOB-000017",
      site_address: "",
    });
  });

  it("404s an unknown job_id", async () => {
    const res = await g(admin, "/api/subcontracts/jobs/JOB-999999/site-address");
    expect(res.status).toBe(404);
    expect(await json<{ error: string }>(res)).toEqual({ error: "not_found" });
  });

  it("bound SQL: a SQL-ish job_id binds as a literal → 404, no injection/error", async () => {
    const res = await g(admin, `/api/subcontracts/jobs/${encodeURIComponent("JOB-000017' OR '1'='1")}/site-address`);
    expect(res.status).toBe(404);
  });
});

// ── Subcontractors CRUD (portal side of the §51 rider) ────────────────────────
describe("subcontractors create/update", () => {
  it("create allocates SUB-###### and stamps origin=portal, sync_state=pending, mirror_version=1", async () => {
    const res = await p(admin, "/api/subcontracts/subcontractors", {
      sub_name: "New Electrical Co", state: "OR", trades: ["electrical", "roofing"],
    });
    expect(res.status, await res.clone().text()).toBe(201);
    const { sub_key } = await json<{ sub_key: string }>(res);
    // SUB-000001 is seeded — the allocator self-heals past the max suffix seen → 000002.
    expect(sub_key).toBe("SUB-000002");
    const row = (await env.DB.prepare("SELECT * FROM subcontractors WHERE sub_key=?1").bind(sub_key).first())!;
    expect(row.origin).toBe("portal");
    expect(row.sync_state).toBe("pending");
    expect(row.mirror_version).toBe(1);
    expect(row.state).toBe("OR");
    expect(row.trades).toBe('["electrical","roofing"]');
  });

  it("create rejects a non-USPS state (400 invalid_state — a tightening PO's freetext region lacked)", async () => {
    const res = await p(admin, "/api/subcontracts/subcontractors", { sub_name: "Bad State Co", state: "West" });
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("invalid_state");
  });

  it("allocation self-heals past a down-synced high key (never collides)", async () => {
    await seedSubcontractor("SUB-000009");
    const res = await p(admin, "/api/subcontracts/subcontractors", { sub_name: "After Down-Sync" });
    const { sub_key } = await json<{ sub_key: string }>(res);
    expect(sub_key).toBe("SUB-000010");
  });

  it("update re-dirties: sync_state=pending, mirror_version bumped, origin=portal; deactivate rides active:0 (never a delete)", async () => {
    const res = await p(admin, "/api/subcontracts/subcontractors/SUB-000001/update", {
      sub_name: "Renamed Sub", state: "VA", active: 0,
    });
    expect(res.status, await res.clone().text()).toBe(200);
    const row = (await env.DB.prepare("SELECT * FROM subcontractors WHERE sub_key='SUB-000001'").first())!;
    expect(row.sub_name).toBe("Renamed Sub");
    expect(row.active).toBe(0);
    expect(row.origin).toBe("portal");
    expect(row.sync_state).toBe("pending");
    expect(row.mirror_version).toBe(1);
    // Row still exists (deactivate-not-delete) and the audit landed in the same batch (W4).
    const audits = await env.DB.prepare("SELECT * FROM audit_log WHERE action='sc_subcontractor_update'").all();
    expect(audits.results!.length).toBe(1);
  });

  it("update of an unknown subcontractor → 404 and writes NO audit row", async () => {
    expect((await p(admin, "/api/subcontracts/subcontractors/SUB-999999/update", { sub_name: "Ghost" })).status).toBe(404);
    const audits = await env.DB.prepare("SELECT * FROM audit_log WHERE action='sc_subcontractor_update'").all();
    expect(audits.results!.length).toBe(0);
  });
});

// ── Draft → generate: lump-sum cents math + SOV-sums-to-price gate + numbering ─
describe("draft → generate", () => {
  it("draft create stores SERVER-computed cents (extended/subtotal); no tax/total fields", async () => {
    const res = await p(admin, "/api/subcontracts/drafts", draftBody());
    expect(res.status, await res.clone().text()).toBe(201);
    const { id, subtotal_cents } = await json<{ id: number; subtotal_cents: number }>(res);
    expect(subtotal_cents).toBe(CONTRACT_PRICE_CENTS);
    const row = await subRow(id);
    expect(row.status).toBe("draft");
    expect(row.sc_number).toBeNull(); // nullable-until-allocated (D7)
    expect(row.revision).toBeNull();
    const lines = await env.DB.prepare("SELECT * FROM sov_lines WHERE subcontract_id=?1 ORDER BY position").bind(id).all();
    expect(lines.results!.length).toBe(1);
    expect((lines.results![0] as Record<string, unknown>).extended_cents).toBe(CONTRACT_PRICE_CENTS); // round(1×5_000_000)
  });

  it("draft create REJECTS an SOV set that does not sum to contract_price (400 sov_mismatch)", async () => {
    const res = await p(admin, "/api/subcontracts/drafts", draftBody({
      contract_price_cents: 5_000_000,
      sov_lines: [{ item_number: "1", description: "short", qty: 1, unit: "ls", unit_price_cents: 4_999_999 }],
    }));
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("sov_mismatch");
  });

  it("draft create ACCEPTS a full Article II body larger than the old 8000-char cap (electrical is ~20k)", async () => {
    // Regression: the electrical Article II template is ~20k chars; the old MAX_SCOPE=8000 (inherited from
    // PO's MAX_SOW) made an electrical subcontract outright unsaveable. The instance cap is now sized to the
    // config-editor's template authoring cap (100_000), so any authored template pre-fills AND saves.
    const bigWork = "A".repeat(20_000);
    const res = await p(admin, "/api/subcontracts/drafts", draftBody({ exhibit_a_work_text: bigWork }));
    expect(res.status, await res.clone().text()).toBe(201);
    const { id } = await json<{ id: number }>(res);
    expect((await subRow(id)).exhibit_a_work_text).toBe(bigWork); // stored in full, not truncated
  });

  it("draft create REJECTS an Exhibit A body over the 100_000 cap (400 invalid_exhibit_a_work_text)", async () => {
    const res = await p(admin, "/api/subcontracts/drafts", draftBody({ exhibit_a_work_text: "A".repeat(100_001) }));
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("invalid_exhibit_a_work_text");
  });

  it("generate REFUSES a blank render-required field (owner/project/trade → 422); the DRAFT still saves", async () => {
    // The draft-level boundary stays lenient (parseDraftBody length-only), so a partial draft SAVES; the
    // GENERATE commit refuses the blank — no more silent subcontract_render_failed fence minutes later.
    for (const [field, code] of [
      ["owner_entity", "missing_owner_entity"],
      ["project_name", "missing_project_name"],
      ["trade", "missing_trade"],
    ] as const) {
      const created = await p(admin, "/api/subcontracts/drafts", draftBody({ [field]: "" }));
      expect(created.status, `${field}: draft should still save`).toBe(201);
      const { id } = await json<{ id: number }>(created);
      const gen = await p(admin, `/api/subcontracts/drafts/${id}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
      expect(gen.status, `${field}: generate should 422`).toBe(422);
      expect((await json<{ error: string }>(gen)).error).toBe(code);
    }
  });

  it("generate: happy path allocates the D7 number, signs, and queues", async () => {
    const created = await p(admin, "/api/subcontracts/drafts", draftBody());
    const { id } = await json<{ id: number }>(created);
    const gen = await p(admin, `/api/subcontracts/drafts/${id}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
    expect(gen.status, await gen.clone().text()).toBe(200);
    const out = await json<{ sc_number: string; revision: number }>(gen);
    expect(out.sc_number).toBe("2026.001.2.0.0"); // {job_no}.{site}.{supersede}.{rev}
    expect(out.revision).toBe(0);
    const row = await subRow(id);
    expect(row.status).toBe("queued");
    expect(row.hmac).toBeTruthy();
  });

  it("generate: client contract-price skew is REJECTED (409 sov_mismatch); the draft is untouched", async () => {
    const created = await p(admin, "/api/subcontracts/drafts", draftBody());
    const { id } = await json<{ id: number }>(created);
    const gen = await p(admin, `/api/subcontracts/drafts/${id}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS + 1 });
    expect(gen.status).toBe(409);
    const body = await json<{ error: string; recomputed: Record<string, number> }>(gen);
    expect(body.error).toBe("sov_mismatch");
    expect(body.recomputed.subtotal_cents).toBe(CONTRACT_PRICE_CENTS);
    const row = await subRow(id);
    expect(row.status).toBe("draft");
    expect(row.sc_number).toBeNull();
  });

  it("two generates in the same family allocate DISTINCT revisions (no collision)", async () => {
    const idA = await makeQueued(admin);
    const created = await p(admin, "/api/subcontracts/drafts", draftBody());
    const { id: idB } = await json<{ id: number }>(created);
    const gen = await p(admin, `/api/subcontracts/drafts/${idB}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
    expect(gen.status).toBe(200);
    const a = await subRow(idA);
    const b = await subRow(idB);
    expect(a.sc_number).toBe("2026.001.2.0.0");
    expect(b.sc_number).toBe("2026.001.2.0.1"); // MAX(revision)+1 within the family
    expect(a.sc_number).not.toBe(b.sc_number);
  });

  it("the UNIQUE family index BITES on a duplicate allocated tuple (the race backstop)", async () => {
    // Inject the synthetic violation directly (two serial route calls cannot race in a test) —
    // prove-the-control-bites: the backstop the losing generate would hit (idx_sc_family_revision).
    await env.DB.prepare(
      "INSERT INTO subcontracts (sc_uuid, sc_number, job_no, site_phase, supersede_seq, revision, sub_key, created_by, status) " +
        "VALUES ('u-1','2026.001.2.0.0','2026.001',2,0,0,'SUB-000001','t','queued')",
    ).run();
    await expect(
      env.DB.prepare(
        "INSERT INTO subcontracts (sc_uuid, sc_number, job_no, site_phase, supersede_seq, revision, sub_key, created_by, status) " +
          "VALUES ('u-2','2026.001.2.0.0-dup','2026.001',2,0,0,'SUB-000001','t','queued')",
      ).run(),
    ).rejects.toThrowError(/UNIQUE constraint failed/);
  });

  it("draft is NOT editable after generate (409 not_draft; SOV lines untouched)", async () => {
    const id = await makeQueued(admin);
    const upd = await p(admin, `/api/subcontracts/drafts/${id}/update`, draftBody({
      sov_lines: [{ item_number: "X", description: "Swapped", qty: 1, unit: "ls", unit_price_cents: 5_000_000 }],
    }));
    expect(upd.status).toBe(409);
    expect((await json<{ error: string }>(upd)).error).toBe("not_draft");
    const lines = await env.DB.prepare("SELECT COUNT(*) n FROM sov_lines WHERE subcontract_id=?1").bind(id).first<{ n: number }>();
    expect(lines!.n).toBe(1); // the original one, not the swapped one
  });

  it("every draft update bumps draft_version; generate pins on the CURRENT version (stale-snapshot guard)", async () => {
    const created = await p(admin, "/api/subcontracts/drafts", draftBody());
    const { id } = await json<{ id: number }>(created);
    const v = async () =>
      (await env.DB.prepare("SELECT draft_version v FROM subcontracts WHERE id=?1").bind(id).first<{ v: number }>())!.v;
    expect(await v()).toBe(0);
    expect((await p(admin, `/api/subcontracts/drafts/${id}/update`, draftBody())).status).toBe(200);
    expect(await v()).toBe(1);
    expect((await p(admin, `/api/subcontracts/drafts/${id}/update`, draftBody())).status).toBe(200);
    expect(await v()).toBe(2);
    // generate succeeds against the twice-updated draft — it pinned draft_version=2, not 0.
    const gen = await p(admin, `/api/subcontracts/drafts/${id}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
    expect(gen.status, await gen.clone().text()).toBe(200);
  });

  it("unknown subcontractor is a 422 (reference, not shape)", async () => {
    const res = await p(admin, "/api/subcontracts/drafts", draftBody({ sub_key: "SUB-000042" }));
    expect(res.status).toBe(422);
  });

  it("governing_law fails closed at generate on an unresolvable state (422 invalid_governing_law_state)", async () => {
    // 'ZZ' is well-formed (passes the draft-save format check) but is not a resolvable jurisdiction —
    // the render's governing_law.resolve would raise, so generate refuses rather than queue a dead row.
    const created = await p(admin, "/api/subcontracts/drafts", draftBody({ governing_law_state: "ZZ" }));
    expect(created.status, await created.clone().text()).toBe(201);
    const { id } = await json<{ id: number }>(created);
    const gen = await p(admin, `/api/subcontracts/drafts/${id}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
    expect(gen.status).toBe(422);
    expect((await json<{ error: string }>(gen)).error).toBe("invalid_governing_law_state");
  });

  it("SOV integer math: extended = round(qty × unit_price_cents); subtotal sums the lines", () => {
    expect(sovExtendedCents({ qty: 2.5, unit_price_cents: 1_000 })).toBe(2_500);
    expect(sovExtendedCents({ qty: 1, unit_price_cents: 5_000_000 })).toBe(5_000_000);
    const lines: SovLine[] = [
      { position: 1, item_number: "1", description: "a", qty: 2.5, unit: "ls", unit_price_cents: 1_000, extended_cents: 2_500 },
      { position: 2, item_number: "2", description: "b", qty: 1, unit: "ls", unit_price_cents: 5_000_000, extended_cents: 5_000_000 },
    ];
    expect(computeSubtotal(lines)).toBe(5_002_500);
  });
});

// ── HMAC (domain sub:v1) ──────────────────────────────────────────────────────
describe("sub:v1 HMAC", () => {
  it("the queued row's hmac recomputes from the canonical string (domain-prefixed, stable shape)", async () => {
    const id = await makeQueued(admin);
    const res = await call("/api/subcontracts/internal/pending", { bearer: SUB_BEARER });
    const { pending } = await json<{ pending: (SubcontractRow & { hmac: string; sov_lines: SovLine[] })[] }>(res);
    expect(pending.length).toBe(1);
    const row = pending[0];
    expect(row.id).toBe(id);
    const canonical = subCanonicalString(row.id, row.sc_number!, canonicalSubJson(row, row.sov_lines));
    expect(canonical.startsWith("sub:v1\n")).toBe(true); // the NEW domain — never replayable as a PO/submission
    expect(row.hmac).toBe(await hmacHex(HMAC_SECRET, canonical));
  });

  it("a submission-domain or po:v1 signature over the same content does NOT match (domain separation)", async () => {
    const id = await makeQueued(admin);
    const res = await call("/api/subcontracts/internal/pending", { bearer: SUB_BEARER });
    const { pending } = await json<{ pending: (SubcontractRow & { hmac: string; sov_lines: SovLine[] })[] }>(res);
    const row = pending[0];
    const jsonPayload = canonicalSubJson(row, row.sov_lines);
    const undomained = [String(id), row.sc_number!, jsonPayload].join("\n");
    expect(row.hmac).not.toBe(await hmacHex(HMAC_SECRET, undomained));
    // A subcontract can never replay as a PO: the same content under the po:v1 prefix ≠ the sub:v1 sig.
    const poDomained = ["po:v1", String(id), row.sc_number!, jsonPayload].join("\n");
    expect(row.hmac).not.toBe(await hmacHex(HMAC_SECRET, poDomained));
  });
});

// ── Bearer tier (requireSubToken) + cross-token isolation ─────────────────────
describe("requireSubToken tier", () => {
  const SUB_INTERNAL_ROUTES: [string, string, unknown][] = [
    ["GET", "/api/subcontracts/internal/pending", undefined],
    ["POST", "/api/subcontracts/internal/mark-filed", { sc_id: 1 }],
    ["POST", "/api/subcontracts/internal/status-sync", { updates: [{ sc_id: 1, status: "approved" }] }],
    ["POST", "/api/subcontracts/internal/subcontractors/sync", { subcontractors: [{ sub_key: "SUB-000001", sub_name: "S", state: "VA" }] }],
    ["GET", "/api/subcontracts/internal/subcontractors/pending", undefined],
    ["POST", "/api/subcontracts/internal/subcontractors/mark-mirrored", { updates: [{ sub_key: "SUB-000001", mirrored_version: 1 }] }],
  ];

  it("rejects no-token, a wrong token, and EVERY sibling tier's token on EVERY sub-internal route", async () => {
    for (const [method, path, body] of SUB_INTERNAL_ROUTES) {
      const init = body === undefined ? { method } : { method, body: JSON.stringify(body) };
      expect((await call(path, init)).status, `${path} no token`).toBe(401);
      for (const bearer of ["wrong-token", INTERNAL_BEARER, FIELDOPS_BEARER, ADMIN_BEARER, PO_BEARER, CONFIG_BEARER]) {
        expect((await call(path, { ...init, bearer })).status, `${path} bearer=${bearer}`).toBe(401);
      }
      expect((await call(path, { ...init, bearer: SUB_BEARER })).status, `${path} sub token`).not.toBe(401);
    }
  });

  it("the SUB token does NOT open the sibling tiers (reverse isolation)", async () => {
    expect((await call("/api/internal/pending", { bearer: SUB_BEARER })).status).toBe(401);
    expect((await call("/api/po/internal/pending", { bearer: SUB_BEARER })).status).toBe(401);
    expect((await call("/api/internal/fieldops/pending-jobs", { bearer: SUB_BEARER })).status).toBe(401);
    expect(
      (await call("/api/internal/admin/users", {
        method: "POST", bearer: SUB_BEARER, body: JSON.stringify({ username: "x.y", password: "password123", role: "submitter" }),
      })).status,
    ).toBe(401);
  });
});

// ── mark-filed (queued → pending_review) ──────────────────────────────────────
describe("mark-filed", () => {
  it("flips queued→pending_review with box_file_id; a replay is a found:false no-op", async () => {
    const id = await makeQueued(admin);
    const first = await call("/api/subcontracts/internal/mark-filed", {
      method: "POST", bearer: SUB_BEARER, body: JSON.stringify({ sc_id: id, box_file_id: "bx-1" }),
    });
    expect(await json<{ ok: boolean; found: boolean }>(first)).toEqual({ ok: true, found: true });
    let row = await subRow(id);
    expect(row.status).toBe("pending_review");
    expect(row.box_file_id).toBe("bx-1");
    // Idempotent replay: no state change, no second audit row.
    const replay = await call("/api/subcontracts/internal/mark-filed", {
      method: "POST", bearer: SUB_BEARER, body: JSON.stringify({ sc_id: id, box_file_id: "bx-OTHER" }),
    });
    expect(await json<{ ok: boolean; found: boolean }>(replay)).toEqual({ ok: true, found: false });
    row = await subRow(id);
    expect(row.box_file_id).toBe("bx-1"); // the .docx Box id stays; the .xlsx is a Box-side sibling
    const audits = await env.DB.prepare("SELECT * FROM audit_log WHERE action='sc_mark_filed'").all();
    expect(audits.results!.length).toBe(1); // the changes()=1 guard kept the replay silent
  });
});

// ── status-sync + the supersession flip (WITH the 'executed' terminal) ────────
describe("status-sync + supersession", () => {
  it("approved only from pending_review; a stale replay cannot regress", async () => {
    const id = await makeQueued(admin);
    // approved while still 'queued' (mark-filed not yet received) → guarded no-op
    await statusSync(id, "approved");
    expect((await subRow(id)).status).toBe("queued");
    await driveTo(id, "approved", "sent");
    expect((await subRow(id)).status).toBe("sent");
    // stale 'approved' replay after 'sent' → guarded no-op (no regression)
    await statusSync(id, "approved");
    expect((await subRow(id)).status).toBe("sent");
  });

  it("sent only from approved; executed only from sent (the wet-signature terminal)", async () => {
    const id = await makeQueued(admin);
    await driveTo(id, "approved");
    // executed while only 'approved' (not yet 'sent') → guarded no-op
    await statusSync(id, "executed");
    expect((await subRow(id)).status).toBe("approved");
    await statusSync(id, "sent");
    expect((await subRow(id)).status).toBe("sent");
    await statusSync(id, "executed");
    expect((await subRow(id)).status).toBe("executed");
  });

  it("supersede clones a SENT subcontract (seq+1, revision reset, lines cloned); sending the successor flips the old one", async () => {
    const idA = await makeQueued(admin);
    await driveTo(idA, "approved", "sent");

    // supersede → a new DRAFT clone
    const sup = await p(admin, `/api/subcontracts/${idA}/supersede`);
    expect(sup.status, await sup.clone().text()).toBe(201);
    const { id: idB } = await json<{ id: number }>(sup);
    const b = await subRow(idB);
    expect(b.status).toBe("draft");
    expect(b.supersede_seq).toBe(1);
    expect(b.revision).toBeNull();
    expect(b.sc_number).toBeNull();
    expect(b.supersedes_sc_id).toBe(idA);
    const bLines = await env.DB.prepare("SELECT * FROM sov_lines WHERE subcontract_id=?1 ORDER BY position").bind(idB).all();
    expect(bLines.results!.length).toBe(1); // cloned
    // W4 regression: the clone's audit row must exist (auditStmtIfChanged sits directly after the
    // parent INSERT, reading changes()=1, not after the line INSERT).
    const cloneAudit = await env.DB.prepare("SELECT * FROM audit_log WHERE action='sc_supersede_clone'").all();
    expect(cloneAudit.results!.length).toBe(1);
    // Old subcontract untouched until the successor ships.
    expect((await subRow(idA)).status).toBe("sent");

    // Double-submit guard: a second supersede while the successor draft is live is a 409 naming it.
    const dup = await p(admin, `/api/subcontracts/${idA}/supersede`);
    expect(dup.status).toBe(409);
    expect((await json<{ error: string; existing_id: number }>(dup)).existing_id).toBe(idB);

    // generate the clone (its own branch: supersede_seq=1 → revision 0)
    const gen = await p(admin, `/api/subcontracts/drafts/${idB}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
    expect(gen.status, await gen.clone().text()).toBe(200);
    expect((await json<{ sc_number: string }>(gen)).sc_number).toBe("2026.001.2.1.0");

    // successor reaches 'sent' → the predecessor flips 'superseded' in the SAME batch
    await driveTo(idB, "approved", "sent");
    expect((await subRow(idB)).status).toBe("sent");
    expect((await subRow(idA)).status).toBe("superseded");
    const flip = await env.DB.prepare("SELECT * FROM audit_log WHERE action='sc_superseded_flip'").all();
    expect(flip.results!.length).toBe(1);
  });

  it("supersede is also allowed from EXECUTED (a countersigned in-force instrument)", async () => {
    const id = await makeQueued(admin);
    await driveTo(id, "approved", "sent", "executed");
    const sup = await p(admin, `/api/subcontracts/${id}/supersede`);
    expect(sup.status, await sup.clone().text()).toBe(201);
    expect((await json<{ supersedes_sc_id: number }>(sup)).supersedes_sc_id).toBe(id);
  });

  it("supersede refuses a draft/queued subcontract", async () => {
    const created = await p(admin, "/api/subcontracts/drafts", draftBody());
    const { id } = await json<{ id: number }>(created);
    const sup = await p(admin, `/api/subcontracts/${id}/supersede`);
    expect(sup.status).toBe(409);
    expect((await json<{ error: string }>(sup)).error).toBe("not_supersedable");
  });
});

// ── change-order documents (Track D2) ─────────────────────────────────────────
describe("change-order documents (Track D2)", () => {
  it("clones a SENT subcontract into a CO draft (SOV + config, seq allocated, NO supersession linkage); generate mints {parent}-CO{seq} with revision NULL; the CO shipping never retires the parent", async () => {
    const idA = await makeQueued(admin);
    await driveTo(idA, "approved", "sent");

    const co = await p(admin, `/api/subcontracts/${idA}/change-order`);
    expect(co.status, await co.clone().text()).toBe(201);
    const { id: idB, co_seq } = await json<{ id: number; co_seq: number }>(co);
    expect(co_seq).toBe(1);
    const b = await subRow(idB);
    expect(b.status).toBe("draft");
    expect(b.change_order_of).toBe(idA);
    expect(b.co_seq).toBe(1);
    expect(b.supersedes_sc_id).toBeNull(); // the flip sites key on this — structurally inert
    expect(b.supersede_seq).toBe((await subRow(idA)).supersede_seq); // verbatim, never +1
    expect(b.sc_number).toBeNull();
    const bLines = await env.DB.prepare("SELECT * FROM sov_lines WHERE subcontract_id=?1 ORDER BY position").bind(idB).all();
    expect(bLines.results!.length).toBe(1); // cloned
    const cloneAudit = await env.DB.prepare("SELECT * FROM audit_log WHERE action='sc_change_order_clone'").all();
    expect(cloneAudit.results!.length).toBe(1);

    // Generate: suffixed number off the parent's SIGNED number; revision stays NULL.
    const gen = await p(admin, `/api/subcontracts/drafts/${idB}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
    expect(gen.status, await gen.clone().text()).toBe(200);
    const parentNumber = (await subRow(idA)).sc_number as string;
    expect((await json<{ sc_number: string }>(gen)).sc_number).toBe(`${parentNumber}-CO1`);
    expect((await subRow(idB)).revision).toBeNull();

    // The CO reaching 'sent' must NOT flip the parent — both stay in force.
    await driveTo(idB, "approved", "sent");
    expect((await subRow(idB)).status).toBe("sent");
    expect((await subRow(idA)).status).toBe("sent");
    expect((await env.DB.prepare("SELECT * FROM audit_log WHERE action='sc_superseded_flip'").all()).results!.length).toBe(0);
  });

  it("allows a CO from EXECUTED; refuses a CO on a draft, a CO-of-CO, and superseding a CO; gates on the lane cap", async () => {
    const created = await p(admin, "/api/subcontracts/drafts", draftBody());
    const { id: draftId } = await json<{ id: number }>(created);
    expect((await p(admin, `/api/subcontracts/${draftId}/change-order`)).status).toBe(409);

    const idA = await makeQueued(admin);
    await driveTo(idA, "approved", "sent", "executed");
    expect((await p(submitter, `/api/subcontracts/${idA}/change-order`)).status).toBe(403);
    const co = await p(admin, `/api/subcontracts/${idA}/change-order`);
    expect(co.status, await co.clone().text()).toBe(201);
    const { id: coId } = await json<{ id: number }>(co);
    const gen = await p(admin, `/api/subcontracts/drafts/${coId}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
    expect(gen.status, await gen.clone().text()).toBe(200);
    await driveTo(coId, "approved", "sent");
    expect((await p(admin, `/api/subcontracts/${coId}/change-order`)).status).toBe(409);
    expect((await p(admin, `/api/subcontracts/${coId}/supersede`)).status).toBe(409);
  });

  it("refuses generating a CO whose parent is no longer in force; update locks counterparty + job identity but preserves linkage on a clean edit", async () => {
    const idA = await makeQueued(admin);
    await driveTo(idA, "approved", "sent");
    const co = await p(admin, `/api/subcontracts/${idA}/change-order`);
    const { id: coId } = await json<{ id: number }>(co);

    // Identity repoint refused; clean edit preserves the linkage.
    await seedSubcontractor("SUB-000099");
    const repoint = await p(admin, `/api/subcontracts/drafts/${coId}/update`, draftBody({ sub_key: "SUB-000099" }));
    expect(repoint.status).toBe(409);
    expect((await json<{ error: string }>(repoint)).error).toBe("co_identity_locked");
    const edit = await p(admin, `/api/subcontracts/drafts/${coId}/update`, draftBody());
    expect(edit.status, await edit.clone().text()).toBe(200);
    const row = await subRow(coId);
    expect(row.change_order_of).toBe(idA);
    expect(row.co_seq).toBe(1);

    // Parent leaves force → generate refused.
    await env.DB.prepare("UPDATE subcontracts SET status='superseded' WHERE id=?1").bind(idA).run();
    const gen = await p(admin, `/api/subcontracts/drafts/${coId}/generate`, { contract_price_cents: CONTRACT_PRICE_CENTS });
    expect(gen.status).toBe(409);
    expect((await json<{ error: string }>(gen)).error).toBe("parent_not_in_force");
  });
});

// ── cancel guards (refusing approved/sent/executed) ───────────────────────────
describe("cancel", () => {
  it("cancels draft / queued; refuses sent / executed; 404s unknown", async () => {
    const created = await p(admin, "/api/subcontracts/drafts", draftBody());
    const { id: draftId } = await json<{ id: number }>(created);
    expect((await p(admin, `/api/subcontracts/${draftId}/cancel`)).status).toBe(200);
    expect((await subRow(draftId)).status).toBe("canceled");

    const queuedId = await makeQueued(admin);
    expect((await p(admin, `/api/subcontracts/${queuedId}/cancel`)).status).toBe(200);
    expect((await subRow(queuedId)).status).toBe("canceled");

    const sentId = await makeQueued(admin);
    await driveTo(sentId, "approved", "sent");
    const refuse = await p(admin, `/api/subcontracts/${sentId}/cancel`);
    expect(refuse.status).toBe(409);
    expect((await json<{ error: string }>(refuse)).error).toBe("not_cancelable");
    expect((await subRow(sentId)).status).toBe("sent");

    const execId = await makeQueued(admin);
    await driveTo(execId, "approved", "sent", "executed");
    expect((await p(admin, `/api/subcontracts/${execId}/cancel`)).status).toBe(409);
    expect((await subRow(execId)).status).toBe("executed");

    expect((await p(admin, "/api/subcontracts/999999/cancel")).status).toBe(404);
  });

  it("delete: HARD-removes a DRAFT + its SOV lines; refuses a generated row (no orphan); 404s unknown", async () => {
    const created = await p(admin, "/api/subcontracts/drafts", draftBody());
    const { id: draftId } = await json<{ id: number }>(created);
    const nLines = async (id: number) =>
      (await env.DB.prepare("SELECT COUNT(*) n FROM sov_lines WHERE subcontract_id=?1").bind(id).first<{ n: number }>())!.n;
    expect(await nLines(draftId)).toBeGreaterThan(0);

    // Hard delete a draft → the row AND its SOV lines are gone.
    expect((await p(admin, `/api/subcontracts/${draftId}/delete`)).status).toBe(200);
    expect(await env.DB.prepare("SELECT id FROM subcontracts WHERE id=?1").bind(draftId).first()).toBeNull();
    expect(await nLines(draftId)).toBe(0);

    // A generated (queued) row is NOT deletable — 409, and its lines are NOT orphaned by the refusal.
    const queuedId = await makeQueued(admin);
    const del = await p(admin, `/api/subcontracts/${queuedId}/delete`);
    expect(del.status).toBe(409);
    expect((await json<{ error: string }>(del)).error).toBe("record_not_deletable");
    expect((await subRow(queuedId)).status).toBe("queued");
    expect(await nLines(queuedId)).toBeGreaterThan(0);

    expect((await p(admin, "/api/subcontracts/999999/delete")).status).toBe(404);
  });
});

// ── subcontractors internal sync (down-sync fence, empty refusal, watermark) ──
describe("subcontractors internal sync", () => {
  it("down-sync upserts synced rows but the dirty-row fence preserves a pending portal edit", async () => {
    await seedSubcontractor("SUB-000002", { sub_name: "Sheet Two" });
    // Portal edit dirties SUB-000001.
    await p(admin, "/api/subcontracts/subcontractors/SUB-000001/update", { sub_name: "Portal Edit", state: "VA" });
    const res = await call("/api/subcontracts/internal/subcontractors/sync", {
      method: "POST",
      bearer: SUB_BEARER,
      body: JSON.stringify({
        subcontractors: [
          { sub_key: "SUB-000001", sub_name: "Sheet Clobber", state: "VA", active: 1 },
          { sub_key: "SUB-000002", sub_name: "Sheet Two Renamed", state: "OR", active: 1 },
          { sub_key: "SUB-000003", sub_name: "Sheet Three (new)", state: "TX", active: 1 },
        ],
      }),
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await json<{ ok: boolean; upserted: number; skipped_dirty: number }>(res)).toEqual({
      ok: true, upserted: 2, skipped_dirty: 1,
    });
    const dirty = (await env.DB.prepare("SELECT * FROM subcontractors WHERE sub_key='SUB-000001'").first())!;
    expect(dirty.sub_name).toBe("Portal Edit"); // the fence held
    expect(dirty.sync_state).toBe("pending");
    expect(dirty.origin).toBe("portal");
    const two = (await env.DB.prepare("SELECT sub_name, sync_state FROM subcontractors WHERE sub_key='SUB-000002'").first())!;
    expect(two.sub_name).toBe("Sheet Two Renamed"); // non-dirty rows DO full-replace
    const three = (await env.DB.prepare("SELECT origin, sync_state FROM subcontractors WHERE sub_key='SUB-000003'").first())!;
    expect(three.origin).toBe("smartsheet");
    expect(three.sync_state).toBe("synced");
  });

  it("REFUSES an empty payload (a Smartsheet read-miss must never wipe the cache)", async () => {
    const res = await call("/api/subcontracts/internal/subcontractors/sync", {
      method: "POST", bearer: SUB_BEARER, body: JSON.stringify({ subcontractors: [] }),
    });
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("empty_subcontractors");
  });

  it("a malformed row rejects the WHOLE batch (a partial sync would silently desync the cache)", async () => {
    const res = await call("/api/subcontracts/internal/subcontractors/sync", {
      method: "POST",
      bearer: SUB_BEARER,
      body: JSON.stringify({
        subcontractors: [
          { sub_key: "SUB-000007", sub_name: "Would-Be Valid", state: "VA", active: 1 },
          { sub_key: "BADKEY", sub_name: "Malformed", state: "VA", active: 1 }, // fails SUB_KEY_RE
        ],
      }),
    });
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("invalid_row");
    // The valid sibling in the same batch was NOT inserted (whole-batch reject before any statement).
    const would = await env.DB.prepare("SELECT sub_key FROM subcontractors WHERE sub_key='SUB-000007'").first();
    expect(would).toBeNull();
  });

  it("up-sync: pending read exposes the dirty row; mark-mirrored flips it ONLY at the unchanged watermark", async () => {
    await p(admin, "/api/subcontracts/subcontractors/SUB-000001/update", { sub_name: "Portal Edit", state: "VA" }); // mirror_version 1
    const pend = await call("/api/subcontracts/internal/subcontractors/pending", { bearer: SUB_BEARER });
    const { subcontractors } = await json<{ subcontractors: { sub_key: string; mirror_version: number }[] }>(pend);
    expect(subcontractors.map((v) => v.sub_key)).toEqual(["SUB-000001"]);
    expect(subcontractors[0].mirror_version).toBe(1);

    // A SECOND portal edit lands between the daemon's read and its commit → watermark moves.
    await p(admin, "/api/subcontracts/subcontractors/SUB-000001/update", { sub_name: "Portal Edit v2", state: "VA" }); // mirror_version 2
    const stale = await call("/api/subcontracts/internal/subcontractors/mark-mirrored", {
      method: "POST", bearer: SUB_BEARER,
      body: JSON.stringify({ updates: [{ sub_key: "SUB-000001", mirrored_version: 1 }] }),
    });
    expect(await json<{ flipped: number; stale: number }>(stale)).toMatchObject({ flipped: 0, stale: 1 });
    expect((await env.DB.prepare("SELECT sync_state FROM subcontractors WHERE sub_key='SUB-000001'").first())!.sync_state).toBe("pending");

    // The daemon re-reads (version 2) and commits at the live watermark → flips.
    const fresh = await call("/api/subcontracts/internal/subcontractors/mark-mirrored", {
      method: "POST", bearer: SUB_BEARER,
      body: JSON.stringify({ updates: [{ sub_key: "SUB-000001", mirrored_version: 2 }] }),
    });
    expect(await json<{ flipped: number; stale: number }>(fresh)).toMatchObject({ flipped: 1, stale: 0 });
    const row = (await env.DB.prepare("SELECT sync_state, mirrored_version FROM subcontractors WHERE sub_key='SUB-000001'").first())!;
    expect(row.sync_state).toBe("synced");
    expect(row.mirrored_version).toBe(2);
  });
});
