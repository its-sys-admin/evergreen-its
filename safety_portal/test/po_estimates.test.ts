import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, p, g, json } from "./helpers";
import { hmacHex } from "../worker/hmac";
import {
  estimateCanonical,
  ESTIMATE_MAX_BYTES,
  EST_CHUNK_DECODED_MAX,
  PREVIEW_MAX_BYTES,
} from "../worker/po_estimates";
import { canonicalPoJson, type PoRow, type PoLine } from "../worker/po";
// The bundled tax config — derive money from source, never pin editable content
// (HOUSE_REFLEXES §5; the po.test.ts pattern).
import taxConfig from "../../po_materials/config/tax.json";

// ─────────────────────────────────────────────────────────────────────────────
// Vendor-estimate importer E1 (ADR-0004) — worker/po_estimates.ts + migrations
// 0054/0055 + the po.ts estimate_id draft-idempotency guard.
//
// Coverage: upload atomicity (parent + chunks + audit in one batch) + the est:v1
// HMAC; the partial-UNIQUE sha256 dedupe → 409 duplicate_estimate with NO row;
// bounds refusals (oversize / MIME / magic / filename); bearer-tier isolation
// (the PO token must NOT open the estimate pool and vice versa — red-team #1);
// claim-first in-WHERE; the result post (refused deletes chunks; extracted
// inserts extraction + lines; contract violations 400); preview upsert + size
// cap + the session PNG read; preview LIVENESS (terminal rows refuse the upsert
// 409 estimate_terminal; refused/rejected rows never serve the session read);
// dispose (line dispositions, delete-on-disposition, 409 already_disposed on the
// second call, the SERVER-SIDE preview-evidence gate — 422
// preview_evidence_required — and the po_id cross-check → 409
// po_estimate_mismatch); draft-create 409 estimate_already_imported; and
// estimate_id NEVER entering the po:v1 canonical.
// ─────────────────────────────────────────────────────────────────────────────

const EST_BEARER = "test-estimate-token";
const PO_BEARER = "test-po-token";
const INTERNAL_BEARER = "test-internal-token";
const FIELDOPS_BEARER = "test-fieldops-token";
const ADMIN_BEARER = "test-admin-token";
const HMAC_SECRET = "test-hmac-payload-secret";

// ── Byte fixtures ────────────────────────────────────────────────────────────
const PDF_BYTES = new TextEncoder().encode("%PDF-1.4\nvendor quote\n%%EOF");
const PDF_BYTES_B = new TextEncoder().encode("%PDF-1.4\na different quote\n%%EOF");
const PNG_BYTES = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);
const MIME_PDF = "application/pdf";

function b64(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

async function seedVendor(vendorKey: string): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO po_vendors (vendor_key, vendor_name, contact_email, region, supply_categories, active, origin, sync_state, mirror_version, mirrored_version) " +
      "VALUES (?1,?2,?3,?4,?5,1,'smartsheet','synced',0,0)",
  )
    .bind(vendorKey, `Vendor ${vendorKey}`, "sales@vendor.example", "Midwest", '["racking"]')
    .run();
}

function upload(
  admin: string,
  over: Partial<{ job_no: string; job_id: string; job_name: string; vendor_key: string; filename: string; mime: string; data_b64: string }> = {},
): Promise<Response> {
  return p(admin, "/api/po/estimates", {
    job_no: over.job_no ?? "2026.001",
    // 0069 — passed through only when supplied, so the default upload keeps exercising the
    // backward-compatible path where the caller knows no job_id.
    ...(over.job_id === undefined ? {} : { job_id: over.job_id }),
    job_name: over.job_name ?? "Sunrise Solar",
    vendor_key: over.vendor_key,
    filename: over.filename ?? "platt quote.pdf",
    mime: over.mime ?? MIME_PDF,
    data_b64: over.data_b64 ?? b64(PDF_BYTES),
  });
}

async function estRow(id: number): Promise<Record<string, unknown> | null> {
  return await env.DB.prepare("SELECT * FROM po_estimates WHERE id=?1").bind(id).first();
}
async function chunkCount(id: number): Promise<number> {
  const r = await env.DB
    .prepare("SELECT COUNT(*) AS n FROM po_estimate_chunks WHERE estimate_id=?1")
    .bind(id)
    .first<{ n: number }>();
  return r?.n ?? 0;
}
async function previewCount(id: number): Promise<number> {
  const r = await env.DB
    .prepare("SELECT COUNT(*) AS n FROM estimate_previews WHERE estimate_id=?1")
    .bind(id)
    .first<{ n: number }>();
  return r?.n ?? 0;
}
async function auditCount(action: string): Promise<number> {
  const r = await env.DB
    .prepare("SELECT COUNT(*) AS n FROM audit_log WHERE action=?1")
    .bind(action)
    .first<{ n: number }>();
  return r?.n ?? 0;
}

/** A valid PR-B-shaped extraction payload (the internal result contract). */
function extractionBody(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    tier: 3,
    schema_version: "1.0.0",
    doc_type: "quote",
    vendor_name: "Platt Electric",
    quote_number: "Q-4471",
    subtotal_cents: 123_450,
    grand_total_cents: 123_450,
    math_ok: 1,
    payload_json: JSON.stringify({ v: 1 }),
    lines: [
      {
        position: 1, section: "Racking", part_number: "RK-100", description: "Rail 100",
        qty: 10, unit: "ea", unit_cost_cents: 12_345, extended_cents: 123_450, math_ok: 1,
      },
      { position: 2, description: "Freight", qty: 1, unit_cost_cents: 0, extended_cents: 0, math_ok: 1 },
    ],
    ...over,
  };
}

async function postResult(body: Record<string, unknown>): Promise<Response> {
  return call("/api/po/estimates/internal/result", {
    method: "POST", bearer: EST_BEARER, body: JSON.stringify(body),
  });
}

/** upload → claim → result 'extracted' (+ a preview page unless withPreview=false — the
 *  preview-evidence-gate tests need an extracted estimate with NO rendered page). */
async function makeExtracted(admin: string, data: Uint8Array = PDF_BYTES, withPreview = true): Promise<number> {
  const res = await upload(admin, { data_b64: b64(data) });
  expect(res.status, await res.clone().text()).toBe(201);
  const { id } = await json<{ id: number }>(res);
  const claim = await call(`/api/po/estimates/internal/${id}/claim`, {
    method: "POST", bearer: EST_BEARER, body: "{}",
  });
  expect((await json<{ found: boolean }>(claim)).found).toBe(true);
  if (withPreview) {
    const pv = await call(`/api/po/estimates/internal/${id}/preview`, {
      method: "POST", bearer: EST_BEARER, body: JSON.stringify({ page: 1, png_b64: b64(PNG_BYTES) }),
    });
    expect(pv.status, await pv.clone().text()).toBe(200);
  }
  const result = await postResult({
    estimate_id: id, status: "extracted", box_file_id: `bx-${id}`, extraction: extractionBody(),
  });
  expect(result.status, await result.clone().text()).toBe(200);
  expect((await json<{ found: boolean }>(result)).found).toBe(true);
  return id;
}

// The PO draft body (the po.test.ts fixture shape) — money derived from bundled config.
function draftBody(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    vendor_key: "VEN-000001",
    job_no: "2026.001",
    site_phase: 2,
    job_name: "Sunrise Solar",
    ship_to_state: "IL",
    tax_mode: "auto",
    line_items: [
      { part_number: "RK-100", description: "Rail 100", qty: 10, unit: "ea", unit_cost_cents: 12_345 },
    ],
    ...over,
  };
}
// Derived from the bundled config (derive, never pin — HOUSE_REFLEXES §5).
const IL_RATE_BP: number = taxConfig.rates_bp.IL;

let admin: string, submitter: string;
beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM estimate_extraction_lines"),
    env.DB.prepare("DELETE FROM estimate_extractions"),
    env.DB.prepare("DELETE FROM estimate_previews"),
    env.DB.prepare("DELETE FROM po_estimate_chunks"),
    env.DB.prepare("DELETE FROM po_estimates"),
    env.DB.prepare("DELETE FROM rfq_vendors"),
    env.DB.prepare("DELETE FROM rfqs"),
    env.DB.prepare("DELETE FROM po_line_items"),
    env.DB.prepare("DELETE FROM purchase_orders"),
    env.DB.prepare("DELETE FROM po_vendors"),
    env.DB.prepare("UPDATE po_vendor_counter SET last_value=0 WHERE id=1"),
  ]);
  await provision("admin.est", "password123", "admin");
  await provision("submitter.est", "password123", "submitter");
  admin = await login("admin.est", "password123");
  submitter = await login("submitter.est", "password123");
  await seedVendor("VEN-000001");
});

// ── Capability gate (browser surface) ─────────────────────────────────────────
describe("cap.po.manage gate", () => {
  it("403s a submitter and 401s no-session on upload/list/detail/dispose", async () => {
    expect((await upload(submitter)).status).toBe(403);
    expect((await g(submitter, "/api/po/estimates")).status).toBe(403);
    expect((await g(submitter, "/api/po/estimates/1")).status).toBe(403);
    expect((await p(submitter, "/api/po/estimates/1/dispose", { action: "rejected" })).status).toBe(403);
    expect((await call("/api/po/estimates")).status).toBe(401);
    expect((await call("/api/po/estimates", { method: "POST", body: "{}" })).status).toBe(401);
  });
});

// ── Upload: happy path + signature + dedupe ───────────────────────────────────
describe("upload", () => {
  it("201s a valid PDF: row + gap-free chunks + audit land atomically; est:v1 HMAC/sha256 verify", async () => {
    const res = await upload(admin);
    expect(res.status, await res.clone().text()).toBe(201);
    const body = await json<{ id: number; size_bytes: number }>(res);
    expect(body.size_bytes).toBe(PDF_BYTES.length);

    const row = (await estRow(body.id))!;
    expect(row.status).toBe("pending");
    expect(row.job_no).toBe("2026.001");
    expect(row.filename).toBe("platt quote.pdf");
    expect(row.declared_mime).toBe(MIME_PDF);
    expect(row.uploaded_by).toBe("admin.est");
    expect(row.family_key).toBe(row.sha256); // sha fallback until body-derived identity (E4)
    expect(await chunkCount(body.id)).toBe(1);
    expect(await auditCount("po_estimate_upload")).toBe(1);

    const digest = await crypto.subtle.digest("SHA-256", PDF_BYTES as unknown as BufferSource);
    const sha = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
    expect(row.sha256).toBe(sha);
    const expected = await hmacHex(
      HMAC_SECRET,
      estimateCanonical(row.est_uuid as string, "2026.001", "platt quote.pdf", MIME_PDF, PDF_BYTES.length, sha),
    );
    expect(row.hmac).toBe(expected);
  });

  it("chunks a multi-chunk payload gap-free (decoded concatenation == original)", async () => {
    const big = new Uint8Array(Math.floor(EST_CHUNK_DECODED_MAX * 2.5));
    big.set(PDF_BYTES, 0);
    const res = await upload(admin, { data_b64: b64(big) });
    expect(res.status, await res.clone().text()).toBe(201);
    const { id } = await json<{ id: number }>(res);
    const { results } = await env.DB
      .prepare("SELECT chunk_index, chunk_total, chunk_b64 FROM po_estimate_chunks WHERE estimate_id=?1 ORDER BY chunk_index")
      .bind(id)
      .all<{ chunk_index: number; chunk_total: number; chunk_b64: string }>();
    expect(results!.length).toBe(3);
    expect(results!.every((c) => c.chunk_total === 3)).toBe(true);
    expect(results!.map((c) => c.chunk_index)).toEqual([0, 1, 2]);
    expect(results!.reduce((n, c) => n + atob(c.chunk_b64).length, 0)).toBe(big.length);
  });

  it("409s duplicate_estimate on an exact-byte replay of a LIVE row — and writes NOTHING", async () => {
    const first = await upload(admin);
    expect(first.status).toBe(201);
    const dup = await upload(admin, { filename: "renamed copy.pdf" }); // same bytes, different name
    expect(dup.status).toBe(409);
    expect((await json<{ error: string }>(dup)).error).toBe("duplicate_estimate");
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM po_estimates").first<{ n: number }>();
    expect(n?.n).toBe(1);
    expect(await auditCount("po_estimate_upload")).toBe(1);
    // No orphan chunks from the aborted batch either.
    const ch = await env.DB.prepare("SELECT COUNT(*) AS n FROM po_estimate_chunks").first<{ n: number }>();
    expect(ch?.n).toBe(1);
  });

  it("accepts a byte-twin again after the live row was refused (partial index frees the sha)", async () => {
    const first = await json<{ id: number }>(await upload(admin));
    await call(`/api/po/estimates/internal/${first.id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    const refused = await postResult({ estimate_id: first.id, status: "refused", detail: "wrong_doc_type:invoice" });
    expect(refused.status).toBe(200);
    const again = await upload(admin);
    expect(again.status, await again.clone().text()).toBe(201);
  });
});

// ── Upload bounds (Invariant 2) ───────────────────────────────────────────────
describe("upload bounds", () => {
  it("413s an over-cap file BEFORE decode; 400s bad job_no/filename; 422s MIME/magic mismatches", async () => {
    const oversize = "A".repeat((Math.ceil(ESTIMATE_MAX_BYTES / 3) + 4) * 4);
    const big = await upload(admin, { data_b64: oversize });
    expect(big.status).toBe(413);
    expect((await json<{ error: string }>(big)).error).toBe("estimate_too_large");

    expect((await upload(admin, { job_no: "26.1" })).status).toBe(400);
    for (const filename of ["../traversal.pdf", "dir/name.pdf", ".hidden.pdf", "spec\u202Efdp.pdf", ""]) {
      expect((await upload(admin, { filename })).status, JSON.stringify(filename)).toBe(400);
    }
    const badMime = await upload(admin, { filename: "legacy.doc", mime: "application/msword" });
    expect(badMime.status).toBe(422);
    expect((await json<{ error: string }>(badMime)).error).toBe("mime_not_allowed");
    const extMismatch = await upload(admin, { filename: "quote.png", mime: MIME_PDF });
    expect(extMismatch.status).toBe(422);
    expect((await json<{ error: string }>(extMismatch)).error).toBe("extension_mime_mismatch");
    // Declared PDF, actual PNG bytes — the tamper case.
    const magic = await upload(admin, { data_b64: b64(PNG_BYTES) });
    expect(magic.status).toBe(422);
    expect((await json<{ error: string }>(magic)).error).toBe("magic_mime_mismatch");
    // Nothing landed from any refusal.
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM po_estimates").first<{ n: number }>();
    expect(n?.n).toBe(0);
  });
});

// ── Bearer tier isolation (red-team #1) ───────────────────────────────────────
describe("bearer isolation", () => {
  it("rejects no-token, wrong token, and EVERY sibling tier (incl. the PO token) on every estimate-internal route", async () => {
    const routes: [string, string, unknown][] = [
      ["GET", "/api/po/estimates/internal/pending", undefined],
      ["POST", "/api/po/estimates/internal/1/claim", {}],
      ["GET", "/api/po/estimates/internal/1/chunks", undefined],
      ["POST", "/api/po/estimates/internal/result", { estimate_id: 1, status: "refused" }],
      ["POST", "/api/po/estimates/internal/1/preview", { page: 1, png_b64: b64(PNG_BYTES) }],
    ];
    for (const [method, path, body] of routes) {
      const init = body === undefined ? { method } : { method, body: JSON.stringify(body) };
      expect((await call(path, init)).status, `${path} no token`).toBe(401);
      for (const bearer of ["wrong-token", PO_BEARER, INTERNAL_BEARER, FIELDOPS_BEARER, ADMIN_BEARER]) {
        expect((await call(path, { ...init, bearer })).status, `${path} bearer=${bearer}`).toBe(401);
      }
      expect((await call(path, { ...init, bearer: EST_BEARER })).status, `${path} est token`).not.toBe(401);
    }
  });

  it("the estimate token does NOT open the PO-daemon tier (scope holds in both directions)", async () => {
    expect((await call("/api/po/internal/pending", { bearer: EST_BEARER })).status).toBe(401);
    expect(
      (await call("/api/po/internal/mark-filed", {
        method: "POST", bearer: EST_BEARER, body: JSON.stringify({ po_id: 1 }),
      })).status,
    ).toBe(401);
  });
});

// ── Internal surface: pending / claim / chunks / result ───────────────────────
describe("internal surface", () => {
  it("pending serves live rows with the hmac; claim flips pending→claimed once (in-WHERE); claimed still re-serves", async () => {
    const { id } = await json<{ id: number }>(await upload(admin));
    const pending = await call("/api/po/estimates/internal/pending", { bearer: EST_BEARER });
    const { estimates } = await json<{ estimates: Record<string, unknown>[] }>(pending);
    expect(estimates.length).toBe(1);
    expect(estimates[0].id).toBe(id);
    expect(estimates[0].hmac).toBeTruthy();
    expect(estimates[0].sha256).toBeTruthy();

    const claim1 = await call(`/api/po/estimates/internal/${id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    expect((await json<{ found: boolean }>(claim1)).found).toBe(true);
    expect(((await estRow(id))!).status).toBe("claimed");
    expect(await auditCount("po_estimate_claim")).toBe(1);

    const claim2 = await call(`/api/po/estimates/internal/${id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    expect((await json<{ found: boolean }>(claim2)).found).toBe(false);
    expect(await auditCount("po_estimate_claim")).toBe(1); // gated audit did not re-fire

    const again = await call("/api/po/estimates/internal/pending", { bearer: EST_BEARER });
    expect((await json<{ estimates: unknown[] }>(again)).estimates.length).toBe(1); // crash recovery
  });

  it("chunks serve bytes Mac-ward for live rows only", async () => {
    const { id } = await json<{ id: number }>(await upload(admin));
    const res = await call(`/api/po/estimates/internal/${id}/chunks`, { bearer: EST_BEARER });
    expect(res.status).toBe(200);
    const { chunks } = await json<{ chunks: { chunk_b64: string }[] }>(res);
    expect(chunks.length).toBe(1);
    expect(atob(chunks[0].chunk_b64).length).toBe(PDF_BYTES.length);
  });

  it("result 'refused' flips status + stores detail + DELETES chunks atomically; replay found:false", async () => {
    const { id } = await json<{ id: number }>(await upload(admin));
    await call(`/api/po/estimates/internal/${id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    const res = await postResult({ estimate_id: id, status: "refused", detail: "wrong_doc_type:ap_report" });
    expect(res.status, await res.clone().text()).toBe(200);
    expect((await json<{ found: boolean }>(res)).found).toBe(true);
    const row = (await estRow(id))!;
    expect(row.status).toBe("refused");
    expect(row.detail).toBe("wrong_doc_type:ap_report");
    expect(row.screened_at).not.toBeNull();
    expect(await chunkCount(id)).toBe(0); // delete-on-refusal
    expect(await auditCount("po_estimate_result")).toBe(1);

    const replay = await postResult({ estimate_id: id, status: "refused", detail: "wrong_doc_type:ap_report" });
    expect((await json<{ found: boolean }>(replay)).found).toBe(false);
    expect(await auditCount("po_estimate_result")).toBe(1);
    // Refused rows leave the pending scan and the chunks read 404s.
    expect((await call(`/api/po/estimates/internal/${id}/chunks`, { bearer: EST_BEARER })).status).toBe(404);
  });

  it("result 'extracted' inserts the extraction + ordered lines and stamps the row; chunks stay until dispose", async () => {
    const id = await makeExtracted(admin);
    const row = (await estRow(id))!;
    expect(row.status).toBe("extracted");
    expect(row.box_file_id).toBe(`bx-${id}`);
    expect(row.doc_type).toBe("quote");
    expect(row.extracted_at).not.toBeNull();
    expect(await chunkCount(id)).toBe(1); // bytes ride until disposition

    const ext = await env.DB
      .prepare("SELECT * FROM estimate_extractions WHERE estimate_id=?1 AND superseded=0")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(ext).not.toBeNull();
    expect(ext!.tier).toBe(3);
    expect(ext!.schema_version).toBe("1.0.0");
    expect(ext!.vendor_name).toBe("Platt Electric");
    const { results } = await env.DB
      .prepare(
        "SELECT position, description, disposition FROM estimate_extraction_lines WHERE extraction_id=?1 ORDER BY position",
      )
      .bind(ext!.id as number)
      .all<{ position: number; description: string; disposition: string }>();
    expect(results!.map((l) => l.position)).toEqual([1, 2]);
    expect(results!.every((l) => l.disposition === "pending")).toBe(true);
  });

  it("result contract violations 400: refused+box_file_id, extracted without extraction, extraction on needs_review, bad shapes", async () => {
    const { id } = await json<{ id: number }>(await upload(admin));
    expect((await postResult({ estimate_id: id, status: "refused", box_file_id: "bx" })).status).toBe(400);
    expect((await postResult({ estimate_id: id, status: "extracted" })).status).toBe(400);
    expect(
      (await postResult({ estimate_id: id, status: "needs_review", extraction: extractionBody() })).status,
    ).toBe(400);
    expect((await postResult({ estimate_id: id, status: "imported" })).status).toBe(400); // daemon can't dispose
    expect(
      (await postResult({
        estimate_id: id, status: "extracted",
        extraction: extractionBody({ lines: [{ position: 1, description: "", math_ok: 1 }] }),
      })).status,
    ).toBe(400);
    expect(
      (await postResult({
        estimate_id: id, status: "extracted",
        extraction: extractionBody({ subtotal_cents: -5 }),
      })).status,
    ).toBe(400);
    // Nothing landed from any refusal.
    expect(((await estRow(id))!).status).toBe("pending");
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM estimate_extractions").first<{ n: number }>();
    expect(n?.n).toBe(0);
  });
});

// ── Previews ──────────────────────────────────────────────────────────────────
describe("previews", () => {
  it("upserts a page (post twice = one row), 413s over the 1MB cap, 422s non-PNG; session GET serves image/png", async () => {
    const { id } = await json<{ id: number }>(await upload(admin));
    const post = (body: unknown) =>
      call(`/api/po/estimates/internal/${id}/preview`, {
        method: "POST", bearer: EST_BEARER, body: JSON.stringify(body),
      });
    expect((await post({ page: 1, png_b64: b64(PNG_BYTES) })).status).toBe(200);
    expect((await post({ page: 1, png_b64: b64(PNG_BYTES) })).status).toBe(200); // upsert replay
    expect(await previewCount(id)).toBe(1);

    const oversize = "A".repeat((Math.ceil(PREVIEW_MAX_BYTES / 3) + 4) * 4);
    const big = await post({ page: 2, png_b64: oversize });
    expect(big.status).toBe(413);
    expect((await post({ page: 2, png_b64: b64(PDF_BYTES) })).status).toBe(422); // not PNG bytes
    expect((await post({ page: 0, png_b64: b64(PNG_BYTES) })).status).toBe(400);

    // The browser side-by-side read (session + cap) — real PNG bytes, correct type.
    const img = await g(admin, `/api/po/estimates/${id}/preview/1`);
    expect(img.status).toBe(200);
    expect(img.headers.get("content-type")).toBe("image/png");
    expect(new Uint8Array(await img.arrayBuffer()).length).toBe(PNG_BYTES.length);
    expect((await g(admin, `/api/po/estimates/${id}/preview/9`)).status).toBe(404);
    // The detail read reports the count for the page nav.
    const detail = await json<{ preview_count: number }>(await g(admin, `/api/po/estimates/${id}`));
    expect(detail.preview_count).toBe(1);
  });
});

// ── Disposition + draft-import idempotency (red-team #4 / #11) ────────────────
describe("dispose + draft import", () => {
  it("imports: draft carries estimate_id; dispose stamps imported + line dispositions + deletes previews/chunks; second dispose 409s", async () => {
    const id = await makeExtracted(admin);
    const detail = await json<{ lines: { id: number }[] }>(await g(admin, `/api/po/estimates/${id}`));
    expect(detail.lines.length).toBe(2);

    const created = await p(admin, "/api/po/drafts", draftBody({ estimate_id: id }));
    expect(created.status, await created.clone().text()).toBe(201);
    const { id: poId } = await json<{ id: number }>(created);
    const poRow = await env.DB
      .prepare("SELECT estimate_id FROM purchase_orders WHERE id=?1")
      .bind(poId)
      .first<{ estimate_id: number }>();
    expect(poRow?.estimate_id).toBe(id);

    const disposed = await p(admin, `/api/po/estimates/${id}/dispose`, {
      action: "imported",
      po_id: poId,
      line_dispositions: [
        { line_id: detail.lines[0].id, disposition: "accepted" },
        { line_id: detail.lines[1].id, disposition: "rejected" },
      ],
    });
    expect(disposed.status, await disposed.clone().text()).toBe(200);
    const row = (await estRow(id))!;
    expect(row.status).toBe("imported");
    expect(row.po_id).toBe(poId);
    expect(row.disposed_at).not.toBeNull();
    expect(await previewCount(id)).toBe(0); // delete-on-disposition
    expect(await chunkCount(id)).toBe(0);
    expect(await auditCount("po_estimate_dispose")).toBe(1);
    const { results } = await env.DB
      .prepare(
        "SELECT l.position, l.disposition FROM estimate_extraction_lines l " +
          "JOIN estimate_extractions e ON e.id = l.extraction_id WHERE e.estimate_id=?1 ORDER BY l.position",
      )
      .bind(id)
      .all<{ position: number; disposition: string }>();
    expect(results!.map((l) => l.disposition)).toEqual(["accepted", "rejected"]);

    const again = await p(admin, `/api/po/estimates/${id}/dispose`, { action: "imported", po_id: poId });
    expect(again.status).toBe(409);
    expect((await json<{ error: string }>(again)).error).toBe("already_disposed");
    expect(await auditCount("po_estimate_dispose")).toBe(1);
  });

  it("rejects: needs_review → rejected; dispose validates its body (imported without po_id, rejected with po_id)", async () => {
    const { id } = await json<{ id: number }>(await upload(admin));
    await call(`/api/po/estimates/internal/${id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    expect((await postResult({ estimate_id: id, status: "needs_review" })).status).toBe(200);

    expect((await p(admin, `/api/po/estimates/${id}/dispose`, { action: "imported" })).status).toBe(400);
    expect((await p(admin, `/api/po/estimates/${id}/dispose`, { action: "rejected", po_id: 5 })).status).toBe(400);
    const ok = await p(admin, `/api/po/estimates/${id}/dispose`, { action: "rejected" });
    expect(ok.status, await ok.clone().text()).toBe(200);
    expect(((await estRow(id))!).status).toBe("rejected");
    // A pending (not yet reviewable) row can't be disposed either.
    const { id: id2 } = await json<{ id: number }>(await upload(admin, { data_b64: b64(PDF_BYTES_B) }));
    expect((await p(admin, `/api/po/estimates/${id2}/dispose`, { action: "rejected" })).status).toBe(409);
  });

  it("409s estimate_already_imported on a second draft carrying the same estimate_id — and writes NOTHING", async () => {
    const id = await makeExtracted(admin);
    const first = await p(admin, "/api/po/drafts", draftBody({ estimate_id: id }));
    expect(first.status).toBe(201);
    const before = await env.DB.prepare("SELECT COUNT(*) AS n FROM purchase_orders").first<{ n: number }>();
    const beforeLines = await env.DB.prepare("SELECT COUNT(*) AS n FROM po_line_items").first<{ n: number }>();

    const dup = await p(admin, "/api/po/drafts", draftBody({ estimate_id: id }));
    expect(dup.status).toBe(409);
    expect((await json<{ error: string }>(dup)).error).toBe("estimate_already_imported");
    const after = await env.DB.prepare("SELECT COUNT(*) AS n FROM purchase_orders").first<{ n: number }>();
    const afterLines = await env.DB.prepare("SELECT COUNT(*) AS n FROM po_line_items").first<{ n: number }>();
    expect(after?.n).toBe(before?.n); // no orphan parent
    expect(afterLines?.n).toBe(beforeLines?.n); // no orphan lines
    expect(await auditCount("po_draft_create")).toBe(1); // the gated audit didn't lie
  });

  it("a canceled/deleted carrier frees the estimate_id for re-import", async () => {
    const id = await makeExtracted(admin);
    const first = await json<{ id: number }>(await p(admin, "/api/po/drafts", draftBody({ estimate_id: id })));
    expect((await p(admin, `/api/po/${first.id}/delete`, {})).status).toBe(200); // hard-delete the draft
    const retry = await p(admin, "/api/po/drafts", draftBody({ estimate_id: id }));
    expect(retry.status, await retry.clone().text()).toBe(201);
  });

  it("a draft WITHOUT estimate_id is unaffected by the guard (plain creates still work twice)", async () => {
    expect((await p(admin, "/api/po/drafts", draftBody())).status).toBe(201);
    expect((await p(admin, "/api/po/drafts", draftBody())).status).toBe(201);
  });
});

// ── F1 — the SERVER-SIDE preview-before-accept fidelity gate ─────────────────
async function disposeAudit(): Promise<Record<string, unknown>> {
  const row = await env.DB
    .prepare("SELECT detail FROM audit_log WHERE action='po_estimate_dispose' ORDER BY id DESC LIMIT 1")
    .first<{ detail: string }>();
  expect(row).not.toBeNull();
  return JSON.parse(row!.detail) as Record<string, unknown>;
}

describe("dispose preview-evidence gate (server-side)", () => {
  /** Mint the provenance-carrying draft, then dispose 'imported' ACCEPTING every
   *  extraction line (the gated shape). `over` merges into the dispose body. */
  async function acceptedImport(id: number, over: Record<string, unknown> = {}): Promise<Response> {
    const detail = await json<{ lines: { id: number }[] }>(await g(admin, `/api/po/estimates/${id}`));
    const created = await p(admin, "/api/po/drafts", draftBody({ estimate_id: id }));
    expect(created.status, await created.clone().text()).toBe(201);
    const { id: poId } = await json<{ id: number }>(created);
    return p(admin, `/api/po/estimates/${id}/dispose`, {
      action: "imported",
      po_id: poId,
      line_dispositions: detail.lines.map((l) => ({ line_id: l.id, disposition: "accepted" })),
      ...over,
    });
  }

  it("422s an import that ACCEPTS extraction lines with zero previews and no acknowledgment — nothing changes", async () => {
    const id = await makeExtracted(admin, PDF_BYTES, false); // no preview rendered
    const res = await acceptedImport(id);
    expect(res.status, await res.clone().text()).toBe(422);
    expect((await json<{ error: string }>(res)).error).toBe("preview_evidence_required");
    expect(((await estRow(id))!).status).toBe("extracted"); // NOT disposed
    expect(await chunkCount(id)).toBe(1); // cleanup did not fire
    expect(await auditCount("po_estimate_dispose")).toBe(0);
  });

  it("the explicit no_preview_verified acknowledgment passes the gate; the audit records the path", async () => {
    const id = await makeExtracted(admin, PDF_BYTES, false);
    const res = await acceptedImport(id, { no_preview_verified: true });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(((await estRow(id))!).status).toBe("imported");
    const audit = await disposeAudit();
    expect(audit.no_preview_verified).toBe(true);
    expect(audit.preview_pages).toBe(0);
  });

  it("a rendered preview passes the gate; the audit records preview_pages > 0", async () => {
    const id = await makeExtracted(admin); // preview present
    const res = await acceptedImport(id);
    expect(res.status, await res.clone().text()).toBe(200);
    const audit = await disposeAudit();
    expect(audit.preview_pages).toBe(1);
    expect(audit.no_preview_verified).toBe(false);
  });

  it("manual-only imports (every extraction line REJECTED) are exempt — the SPA gate's exact scope", async () => {
    const id = await makeExtracted(admin, PDF_BYTES, false); // no preview, no flag
    const detail = await json<{ lines: { id: number }[] }>(await g(admin, `/api/po/estimates/${id}`));
    const created = await p(admin, "/api/po/drafts", draftBody({ estimate_id: id }));
    const { id: poId } = await json<{ id: number }>(created);
    const res = await p(admin, `/api/po/estimates/${id}/dispose`, {
      action: "imported",
      po_id: poId,
      line_dispositions: detail.lines.map((l) => ({ line_id: l.id, disposition: "rejected" })),
    });
    expect(res.status, await res.clone().text()).toBe(200); // no accepted/edited line → exempt
  });

  it("shape-guards no_preview_verified (a present non-boolean 400s)", async () => {
    const id = await makeExtracted(admin, PDF_BYTES, false);
    const res = await p(admin, `/api/po/estimates/${id}/dispose`, {
      action: "rejected", no_preview_verified: "yes",
    });
    expect(res.status).toBe(400);
    expect((await json<{ error: string }>(res)).error).toBe("invalid_no_preview_verified");
    expect(((await estRow(id))!).status).toBe("extracted"); // untouched
  });
});

// ── F2 — preview liveness (terminal rows refuse the write; refused never serves) ──
describe("preview liveness", () => {
  const postPreview = (id: number) =>
    call(`/api/po/estimates/internal/${id}/preview`, {
      method: "POST", bearer: EST_BEARER, body: JSON.stringify({ page: 1, png_b64: b64(PNG_BYTES) }),
    });

  it("preview POST on a refused estimate → 409 estimate_terminal, no row, no audit", async () => {
    const { id } = await json<{ id: number }>(await upload(admin));
    await call(`/api/po/estimates/internal/${id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    expect((await postResult({ estimate_id: id, status: "refused", detail: "wrong_doc_type:invoice" })).status).toBe(200);
    const pv = await postPreview(id);
    expect(pv.status).toBe(409);
    expect((await json<{ error: string }>(pv)).error).toBe("estimate_terminal");
    expect(await previewCount(id)).toBe(0);
    expect(await auditCount("po_estimate_preview")).toBe(0);
  });

  it("a preview posted BEFORE refusal stops serving after it (read-side backstop) — 404 on GET", async () => {
    const { id } = await json<{ id: number }>(await upload(admin));
    expect((await postPreview(id)).status).toBe(200); // live row: lands
    expect((await g(admin, `/api/po/estimates/${id}/preview/1`)).status).toBe(200);
    await call(`/api/po/estimates/internal/${id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    await postResult({ estimate_id: id, status: "refused", detail: "wrong_doc_type:invoice" });
    // Refusal deletes CHUNKS, not previews — the row lingers (the prune backstop reaps it)…
    expect(await previewCount(id)).toBe(1);
    // …but it must never serve.
    expect((await g(admin, `/api/po/estimates/${id}/preview/1`)).status).toBe(404);
  });

  it("an IMPORTED row's lingering evidence MAY still serve (operator revisit); a REJECTED row's never", async () => {
    const id = await makeExtracted(admin);
    const created = await p(admin, "/api/po/drafts", draftBody({ estimate_id: id }));
    const { id: poId } = await json<{ id: number }>(created);
    expect((await p(admin, `/api/po/estimates/${id}/dispose`, { action: "imported", po_id: poId })).status).toBe(200);
    // dispose deleted the previews; model a backstop-retained page directly.
    await env.DB
      .prepare("INSERT INTO estimate_previews (estimate_id, page, png_b64) VALUES (?1, 1, ?2)")
      .bind(id, b64(PNG_BYTES))
      .run();
    expect((await g(admin, `/api/po/estimates/${id}/preview/1`)).status).toBe(200);

    const id2 = await makeExtracted(admin, PDF_BYTES_B);
    expect((await p(admin, `/api/po/estimates/${id2}/dispose`, { action: "rejected" })).status).toBe(200);
    await env.DB
      .prepare("INSERT INTO estimate_previews (estimate_id, page, png_b64) VALUES (?1, 1, ?2)")
      .bind(id2, b64(PNG_BYTES))
      .run();
    expect((await g(admin, `/api/po/estimates/${id2}/preview/1`)).status).toBe(404);
  });
});

// ── F4 — the dispose po_id cross-check ───────────────────────────────────────
describe("dispose po_id cross-check", () => {
  it("409s po_estimate_mismatch when the supplied po_id does not reference THIS estimate; the row stays reviewable", async () => {
    const id = await makeExtracted(admin);
    // A plain draft with NO estimate provenance — a foreign po_id from this dispose's view.
    const foreign = await p(admin, "/api/po/drafts", draftBody());
    expect(foreign.status).toBe(201);
    const { id: foreignPoId } = await json<{ id: number }>(foreign);
    const res = await p(admin, `/api/po/estimates/${id}/dispose`, { action: "imported", po_id: foreignPoId });
    expect(res.status).toBe(409);
    expect((await json<{ error: string }>(res)).error).toBe("po_estimate_mismatch");
    const row = (await estRow(id))!;
    expect(row.status).toBe("extracted"); // NOT disposed
    expect(row.po_id).toBeNull();
    expect(await chunkCount(id)).toBe(1); // the guarded cleanup did not fire
    expect(await previewCount(id)).toBe(1);
    expect(await auditCount("po_estimate_dispose")).toBe(0); // gated audit did not lie
    // The provenance-carrying draft still imports cleanly afterward.
    const created = await p(admin, "/api/po/drafts", draftBody({ estimate_id: id }));
    const { id: poId } = await json<{ id: number }>(created);
    const ok = await p(admin, `/api/po/estimates/${id}/dispose`, { action: "imported", po_id: poId });
    expect(ok.status, await ok.clone().text()).toBe(200);
    expect(((await estRow(id))!).po_id).toBe(poId);
  });
});

// ── The po:v1 canonical NEVER carries estimate_id (red-team #4) ──────────────
describe("po:v1 canonical isolation", () => {
  it("canonicalPoJson output has no estimate_id key (HMAC parity with shared/portal_hmac.py holds)", () => {
    const po = {
      id: 7, po_number: "2026.001.2.0.0", job_no: "2026.001", site_phase: 2, supersede_seq: 0,
      revision: 0, vendor_key: "VEN-000001", job_id: "", job_name: "Sunrise Solar",
      ship_to_name: "", ship_to_address: "", ship_to_city: "", ship_to_state: "IL", ship_to_zip: "",
      delivery_contact_name: "", delivery_contact_phone: "", delivery_contact_email: "",
      sow_text: "", delivery_instructions: "", payment_terms_text: "", terms_profile_id: "t",
      terms_version: "v1", subtotal_cents: 123_450, tax_mode: "auto", tax_rate_bp: IL_RATE_BP,
      tax_cents: 1, shipping_cents: 0, total_cents: 123_451, line_column_variant: "default",
      supersedes_po_id: null, approver_name: "", approver_title: "",
      // What a SELECT * row would ALSO carry post-0055 — must never leak into the canonical:
      estimate_id: 42,
    } as PoRow & { estimate_id: number };
    const lines: PoLine[] = [{
      position: 1, part_number: "RK-100", description: "Rail 100", qty: 10, unit: "ea",
      unit_cost_cents: 12_345, extended_cents: 123_450, watts: null, panels: null, pallets: null,
      price_per_watt_microcents: null,
    }];
    const parsed = JSON.parse(canonicalPoJson(po, lines)) as Record<string, unknown>;
    expect("estimate_id" in parsed).toBe(false);
    expect((parsed.line_items as Record<string, unknown>[])[0].estimate_id).toBeUndefined();
  });
});

// ── R4 round-trip auto-bind (ADR-0004): a verified Tier-0 form binds its estimate to the RFQ ──
describe("rfq round-trip auto-bind", () => {
  const RFQ_NUMBER = "RFQ-2026.001-001";
  const VENDOR_KEY = "VEN-000001";

  /** Seed one rfqs row + one rfq_vendors row at `vendorStatus`. Returns the rfq id. */
  async function seedRfq(vendorStatus: string): Promise<number> {
    const row = await env.DB
      .prepare(
        "INSERT INTO rfqs (rfq_uuid, rfq_number, job_no, status, created_by) " +
          "VALUES (?1,?2,?3,'generated','admin.est') RETURNING id",
      )
      .bind(`uuid-${RFQ_NUMBER}`, RFQ_NUMBER, "2026.001")
      .first<{ id: number }>();
    const rfqId = row!.id;
    await env.DB
      .prepare("INSERT INTO rfq_vendors (rfq_id, vendor_key, status) VALUES (?1,?2,?3)")
      .bind(rfqId, VENDOR_KEY, vendorStatus)
      .run();
    return rfqId;
  }

  async function vendorRow(rfqId: number): Promise<Record<string, unknown> | null> {
    return await env.DB
      .prepare("SELECT status, responded_estimate_id FROM rfq_vendors WHERE rfq_id=?1 AND vendor_key=?2")
      .bind(rfqId, VENDOR_KEY)
      .first();
  }

  /** Upload → claim → post an extracted result carrying the rfq bind hint. Returns the estimate id. */
  async function extractedWithBind(over: { rfq_number?: string | null; rfq_vendor_key?: string | null }): Promise<number> {
    const res = await upload(admin);
    const { id } = await json<{ id: number }>(res);
    await call(`/api/po/estimates/internal/${id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    const result = await postResult({
      estimate_id: id, status: "extracted", box_file_id: `bx-${id}`, extraction: extractionBody(),
      rfq_number: over.rfq_number, rfq_vendor_key: over.rfq_vendor_key,
    });
    expect(result.status, await result.clone().text()).toBe(200);
    return id;
  }

  it("binds po_estimates.rfq_id/rfq_vendor_key + flips the vendor row to responded (filed→responded)", async () => {
    const rfqId = await seedRfq("filed");
    const estId = await extractedWithBind({ rfq_number: RFQ_NUMBER, rfq_vendor_key: VENDOR_KEY });
    const est = await estRow(estId);
    expect(est!.rfq_id).toBe(rfqId);
    expect(est!.rfq_vendor_key).toBe(VENDOR_KEY);
    const v = await vendorRow(rfqId);
    expect(v!.status).toBe("responded");
    expect(v!.responded_estimate_id).toBe(estId);
    expect(await auditCount("po_estimate_rfq_bound")).toBe(1);
    // rfqs.status is UNTOUCHED (its CHECK set has no 'responded').
    const rfq = await env.DB.prepare("SELECT status FROM rfqs WHERE id=?1").bind(rfqId).first<{ status: string }>();
    expect(rfq!.status).toBe("generated");
  });

  it("also binds from 'sent' (sent→responded — the RFQ email already went out)", async () => {
    const rfqId = await seedRfq("sent");
    await extractedWithBind({ rfq_number: RFQ_NUMBER, rfq_vendor_key: VENDOR_KEY });
    expect((await vendorRow(rfqId))!.status).toBe("responded");
  });

  it("ignores an unknown rfq_number — no bind, never 400s the result", async () => {
    const estId = await extractedWithBind({ rfq_number: "RFQ-9999.999-001", rfq_vendor_key: VENDOR_KEY });
    const est = await estRow(estId);
    expect(est!.rfq_id).toBeNull();
    expect(est!.status).toBe("extracted"); // the result itself still landed
    expect(await auditCount("po_estimate_rfq_bound")).toBe(0);
  });

  it("ignores a foreign vendor_key (a real RFQ, but not this vendor) — no bind", async () => {
    const rfqId = await seedRfq("filed");
    const estId = await extractedWithBind({ rfq_number: RFQ_NUMBER, rfq_vendor_key: "VEN-000999" });
    expect((await estRow(estId))!.rfq_id).toBeNull();
    expect((await vendorRow(rfqId))!.status).toBe("filed"); // untouched
    expect(await auditCount("po_estimate_rfq_bound")).toBe(0);
  });

  it("forward-only: a 'pending' vendor row is NOT flipped to responded", async () => {
    const rfqId = await seedRfq("pending");
    const estId = await extractedWithBind({ rfq_number: RFQ_NUMBER, rfq_vendor_key: VENDOR_KEY });
    // po_estimates still binds the identity, but the vendor row (pending) does not move.
    expect((await estRow(estId))!.rfq_id).toBe(rfqId);
    expect((await vendorRow(rfqId))!.status).toBe("pending");
  });

  it("a 'refused' result carrying real rfq hints does NOT flip the vendor row or audit a bind (review F1)", async () => {
    // The estimate daemon is the highest-exposure bearer; a refused (garbage/invoice) result
    // must never be able to forge a 'responded' pill for a real in-flight RFQ vendor.
    const rfqId = await seedRfq("filed");
    const res = await upload(admin);
    const { id } = await json<{ id: number }>(res);
    await call(`/api/po/estimates/internal/${id}/claim`, { method: "POST", bearer: EST_BEARER, body: "{}" });
    const result = await postResult({
      estimate_id: id, status: "refused", detail: "wrong_doc_type:invoice",
      rfq_number: RFQ_NUMBER, rfq_vendor_key: VENDOR_KEY,
    });
    expect(result.status, await result.clone().text()).toBe(200);
    expect((await estRow(id))!.rfq_id).toBeNull();            // po_estimates never bound
    expect((await vendorRow(rfqId))!.status).toBe("filed");   // vendor row NOT flipped
    expect(await auditCount("po_estimate_rfq_bound")).toBe(0); // no lying bind audit
  });

  it("ignores a cross-job rfq hint (real RFQ + vendor, but a different job) — no bind (review F2)", async () => {
    // The estimate's job is "2026.001" (the upload default); seed a real RFQ+vendor for a
    // DIFFERENT job. Without the job_no cross-check this would bind cleanly.
    const otherRfqNumber = "RFQ-2026.002-001";
    const rfqRow = await env.DB
      .prepare(
        "INSERT INTO rfqs (rfq_uuid, rfq_number, job_no, status, created_by) " +
          "VALUES (?1,?2,'2026.002','generated','admin.est') RETURNING id",
      )
      .bind(`uuid-${otherRfqNumber}`, otherRfqNumber)
      .first<{ id: number }>();
    await env.DB
      .prepare("INSERT INTO rfq_vendors (rfq_id, vendor_key, status) VALUES (?1,?2,'filed')")
      .bind(rfqRow!.id, VENDOR_KEY)
      .run();
    const estId = await extractedWithBind({ rfq_number: otherRfqNumber, rfq_vendor_key: VENDOR_KEY });
    expect((await estRow(estId))!.rfq_id).toBeNull(); // estimate job ≠ RFQ job → no bind
    expect(await auditCount("po_estimate_rfq_bound")).toBe(0);
  });

  it("0070 REGRESSION: a SITE-BEARING rfq_number still binds — rfqs.job_no stays two-segment", async () => {
    // This is the test that would have caught the rejected design. The auto-bind joins on
    // `rfq_number = ? AND job_no = ?`, where job_no comes from the ESTIMATE (always two
    // segments). Had the site been folded into rfqs.job_no instead of its own column,
    // '2026.001' would stop matching '2026.001.1', the bind would return zero rows, and the
    // whole R4 round-trip would die SILENTLY — 200 response, only `rfq_bound:false` in an
    // audit row. Numbering would still look perfect.
    const siteNumber = "RFQ-2026.001.1-001";
    const row = await env.DB
      .prepare(
        "INSERT INTO rfqs (rfq_uuid, rfq_number, job_no, site_phase, status, created_by) " +
          "VALUES (?1,?2,'2026.001',1,'generated','admin.est') RETURNING id",
      )
      .bind(`uuid-${siteNumber}`, siteNumber)
      .first<{ id: number }>();
    await env.DB
      .prepare("INSERT INTO rfq_vendors (rfq_id, vendor_key, status) VALUES (?1,?2,'filed')")
      .bind(row!.id, VENDOR_KEY)
      .run();
    const estId = await extractedWithBind({ rfq_number: siteNumber, rfq_vendor_key: VENDOR_KEY });
    expect((await estRow(estId))!.rfq_id).toBe(row!.id); // BOUND
    expect(await auditCount("po_estimate_rfq_bound")).toBe(1);
  });

  it("0069: an uploaded estimate carries job_id, and omitting it is still legal", async () => {
    // job_id is what lets the disposition screen resolve the SITE — job_no alone cannot
    // (2026.384 is both MH405 site 1 and OG593 site 2). Optional so the route stays
    // backward-compatible with a caller that only knows the project number.
    const withJob = await upload(admin, { job_id: "JOB-000034" });
    expect(withJob.status, await withJob.clone().text()).toBe(201);
    const id1 = ((await withJob.json()) as any).id as number;
    expect((await estRow(id1))!.job_id).toBe("JOB-000034");

    // Distinct bytes: identical content would hit the live-sha dedupe and 409, which is the
    // documented upload behaviour and not what this case is about.
    const other = new Uint8Array([...PDF_BYTES, 0x0a, 0x25, 0x25, 0x45, 0x4f, 0x46]);
    const without = await upload(admin, { data_b64: b64(other), filename: "second quote.pdf" });
    expect(without.status).toBe(201);
    const id2 = ((await without.json()) as any).id as number;
    expect((await estRow(id2))!.job_id).toBe(""); // NOT NULL DEFAULT '' — never null
  });

  it("a shape-bad rfq_vendor_key is ignored, never a 400", async () => {
    await seedRfq("filed");
    const estId = await extractedWithBind({ rfq_number: RFQ_NUMBER, rfq_vendor_key: "not-a-vendor-key" });
    expect((await estRow(estId))!.rfq_id).toBeNull();
  });
});
