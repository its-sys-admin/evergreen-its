import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, g, p, seedJob, ADMIN_BEARER } from "./helpers";
import { manifestCanonical, MANIFEST_HMAC_DOMAIN } from "../worker/fieldops_manifests";

// ─────────────────────────────────────────────────────────────────────────────
// Materials-manifest import pool (PR3b) — worker/fieldops_manifests.ts.
//
// Real workerd + D1 (migrations auto-applied), no route mocks. The properties under test
// are the ones unit tests structurally cannot reach: bearer privilege separation, the
// per-JOB dedupe divergence, the claim-first lifecycle, byte-flow direction (chunks are
// Mac-ward ONLY), and the in-WHERE status guards that stop a late daemon post resurrecting
// a discarded manifest.
// ─────────────────────────────────────────────────────────────────────────────

const MANIFEST_BEARER = "test-manifest-token";
const ESTIMATE_BEARER = "test-estimate-token";
const PDF_MIME = "application/pdf";
const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** A minimal byte string whose first 8 bytes satisfy the magic sniff, base64-encoded. */
function b64(head: number[], tailLen = 16): string {
  const bytes = new Uint8Array(head.length + tailLen);
  bytes.set(head, 0);
  for (let i = head.length; i < bytes.length; i++) bytes[i] = 0x41;
  let bin = "";
  for (const byte of bytes) bin += String.fromCharCode(byte);
  return btoa(bin);
}
const PDF_B64 = b64([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37]); // "%PDF-1.7"
const XLSX_B64 = b64([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00, 0x00]); // PK\x03\x04
const PNG_B64 = b64([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]); // PNG

let admin = "";
let sub = "";

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_manifest_chunks"),
    env.DB.prepare("DELETE FROM job_manifest_rows"),
    env.DB.prepare("DELETE FROM job_manifest_previews"),
    env.DB.prepare("DELETE FROM job_manifests"),
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
  await provision("admin.one", "pw-admin-1234", "admin");
  await provision("sub.sam", "pw-subby-1234", "submitter");
  admin = await login("admin.one", "pw-admin-1234");
  sub = await login("sub.sam", "pw-subby-1234");
  await seedJob("JOB-A");
  await seedJob("JOB-B");
});

async function upload(
  cookie: string,
  jobId: string,
  filename = "Customer BOM.pdf",
  mime = PDF_MIME,
  data = PDF_B64,
): Promise<Response> {
  return p(cookie, "/api/fieldops/manifests", { job_id: jobId, filename, mime, data_b64: data });
}

async function uploadOk(jobId: string, filename?: string, mime?: string, data?: string): Promise<number> {
  const res = await upload(admin, jobId, filename, mime, data);
  expect(res.status, await res.clone().text()).toBe(201);
  return ((await res.json()) as { id: number }).id;
}

describe("manifest:v1 canonical (cross-language pin)", () => {
  it("builds the EXACT literal tests/test_manifest_hmac_parity.py pins on the Python side", () => {
    // THE cross-runtime contract. This literal and the one in the Python golden-vector suite
    // are the same bytes, written independently in each runtime — if either side reorders a
    // field, changes the separator, or drops the domain, exactly one of the two suites goes
    // red and names the drift. A shared helper would prove nothing; the duplication IS the test.
    expect(
      manifestCanonical(
        "3b71f8c0-52ad-4e19-9c7b-6d04a1e83f27",
        "JOB-2026-014",
        "25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf",
        "application/pdf",
        148902,
        "4e07408562bedb8b60ce05c1decfe3ad16b72230967de01f640b7e4729b49fce",
      ),
    ).toBe(
      "manifest:v1\n" +
        "3b71f8c0-52ad-4e19-9c7b-6d04a1e83f27\n" +
        "JOB-2026-014\n" +
        "25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf\n" +
        "application/pdf\n" +
        "148902\n" +
        "4e07408562bedb8b60ce05c1decfe3ad16b72230967de01f640b7e4729b49fce",
    );
  });

  it("renders size_bytes with String(), matching Python's str(int)", () => {
    expect(manifestCanonical("u", "j", "f", "m", 7, "s")).toContain("\n7\n");
    expect(MANIFEST_HMAC_DOMAIN).toBe("manifest:v1");
  });

  it("the stored HMAC really is this canonical under the payload secret", async () => {
    // Ties the pinned string to what the UPLOAD ROUTE actually signed — a canonical that is
    // correct but unused would pass the test above and still fail every Mac-side verify.
    const id = await uploadOk("JOB-A");
    const row = await env.DB
      .prepare("SELECT manifest_uuid, job_id, filename, declared_mime, size_bytes, sha256, hmac FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<Record<string, string | number>>();
    const canonical = manifestCanonical(
      String(row!.manifest_uuid), String(row!.job_id), String(row!.filename),
      String(row!.declared_mime), Number(row!.size_bytes), String(row!.sha256),
    );
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(env.HMAC_PAYLOAD_SECRET),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(canonical));
    const hex = [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
    expect(row!.hmac).toBe(hex);
  });
});

describe("manifest upload (browser tier)", () => {
  it("accepts a PDF and an XLSX, pooling bytes as chunks with the row + audit in one batch", async () => {
    const id = await uploadOk("JOB-A");
    const row = await env.DB
      .prepare("SELECT job_id, status, declared_mime, size_bytes, hmac, uploaded_by FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ job_id: "JOB-A", status: "pending", declared_mime: PDF_MIME, uploaded_by: "admin.one" });
    expect(String(row!.hmac)).toMatch(/^[0-9a-f]{64}$/);
    const chunks = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_manifest_chunks WHERE manifest_id = ?1")
      .bind(id)
      .first<{ n: number }>();
    expect(chunks!.n).toBe(1);
    const audit = await env.DB
      .prepare("SELECT action, target_username FROM audit_log WHERE action='job_manifest_upload'")
      .first<{ action: string; target_username: string }>();
    expect(audit).toMatchObject({ action: "job_manifest_upload", target_username: "JOB-A" });

    await uploadOk("JOB-A", "Shipping Log.xlsx", XLSX_MIME, XLSX_B64);
  });

  it("refuses a type the PARSER cannot read, even though the estimate lane allows it", async () => {
    // The narrower allowlist is deliberate: accepting a .png here would queue a document
    // that can only ever end in `refused`, after it had been screened and stored.
    const res = await upload(admin, "JOB-A", "photo.png", "image/png", PNG_B64);
    expect(res.status).toBe(422);
    expect(await res.json()).toMatchObject({ error: "mime_not_allowed" });
  });

  it("refuses a magic/MIME mismatch and an extension/MIME mismatch", async () => {
    const magic = await upload(admin, "JOB-A", "bom.pdf", PDF_MIME, XLSX_B64);
    expect(magic.status).toBe(422);
    expect(await magic.json()).toMatchObject({ error: "magic_mime_mismatch" });

    const ext = await upload(admin, "JOB-A", "bom.xlsx", PDF_MIME, PDF_B64);
    expect(ext.status).toBe(422);
    expect(await ext.json()).toMatchObject({ error: "extension_mime_mismatch" });
  });

  it("requires cap.materials.manage — a submitter cannot import a job's material list", async () => {
    const res = await upload(sub, "JOB-A");
    expect(res.status).toBe(403);
  });

  it("404s an unknown job rather than pooling bytes against nothing", async () => {
    const res = await upload(admin, "NO-SUCH-JOB");
    expect(res.status).toBe(404);
  });

  it("DEDUPE IS PER-JOB: the same master BOM may serve sibling jobs, but not the same job twice", async () => {
    // THE deliberate divergence from po_estimates' global sha index. Bradley 1 / Bradley 2
    // are separate jobs served by one document; a global index would let whichever job
    // imported it first lock the other one out with a 409 it could never clear.
    await uploadOk("JOB-A");
    const sibling = await upload(admin, "JOB-B");
    expect(sibling.status, await sibling.clone().text()).toBe(201);

    const dupe = await upload(admin, "JOB-A");
    expect(dupe.status).toBe(409);
    expect(await dupe.json()).toMatchObject({ error: "duplicate_manifest" });
  });

  it("a discarded manifest leaves the dedupe index, so the document can be re-imported", async () => {
    const id = await uploadOk("JOB-A");
    expect((await p(admin, `/api/fieldops/manifests/${id}/discard`)).status).toBe(200);
    const again = await upload(admin, "JOB-A");
    expect(again.status, await again.clone().text()).toBe(201);
  });
});

describe("manifest internal tier (bearer)", () => {
  it("is closed to every sibling bearer and to a session cookie — privilege separation", async () => {
    expect((await call("/api/fieldops/manifests/internal/pending")).status).toBe(401);
    expect((await call("/api/fieldops/manifests/internal/pending", { bearer: ESTIMATE_BEARER })).status).toBe(401);
    expect((await call("/api/fieldops/manifests/internal/pending", { bearer: ADMIN_BEARER })).status).toBe(401);
    expect((await g(admin, "/api/fieldops/manifests/internal/pending")).status).toBe(401);
    expect((await call("/api/fieldops/manifests/internal/pending", { bearer: MANIFEST_BEARER })).status).toBe(200);
  });

  it("the manifest bearer opens NO sibling tier", async () => {
    expect((await call("/api/po/estimates/internal/pending", { bearer: MANIFEST_BEARER })).status).toBe(401);
    expect((await call("/api/internal/pending", { bearer: MANIFEST_BEARER })).status).toBe(401);
  });

  it("serves pending rows with the HMAC the Mac verifies, oldest-first", async () => {
    const first = await uploadOk("JOB-A");
    await uploadOk("JOB-B", "Shipping Log.xlsx", XLSX_MIME, XLSX_B64);
    const res = await call("/api/fieldops/manifests/internal/pending", { bearer: MANIFEST_BEARER });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { manifests: Record<string, unknown>[] };
    expect(body.manifests).toHaveLength(2);
    expect(body.manifests[0].id).toBe(first);
    // The verify inputs must all be present or the Mac cannot recompute manifest:v1.
    for (const k of ["manifest_uuid", "job_id", "filename", "declared_mime", "size_bytes", "sha256", "hmac"]) {
      expect(body.manifests[0][k], `pending row missing ${k}`).toBeTruthy();
    }
  });

  it("claim is idempotent: first claim found:true, a re-claim found:false (crash recovery)", async () => {
    const id = await uploadOk("JOB-A");
    const one = await call(`/api/fieldops/manifests/internal/${id}/claim`, { method: "POST", bearer: MANIFEST_BEARER });
    expect(await one.json()).toMatchObject({ ok: true, found: true });
    const two = await call(`/api/fieldops/manifests/internal/${id}/claim`, { method: "POST", bearer: MANIFEST_BEARER });
    expect(await two.json()).toMatchObject({ ok: true, found: false });
    // A claimed row is STILL served — the servicing pass is idempotent and must re-run.
    const pending = (await (
      await call("/api/fieldops/manifests/internal/pending", { bearer: MANIFEST_BEARER })
    ).json()) as { manifests: { id: number }[] };
    expect(pending.manifests.map((m) => m.id)).toContain(id);
  });

  it("chunks are served Mac-ward only, and never to the browser at any URL", async () => {
    const id = await uploadOk("JOB-A");
    const mac = await call(`/api/fieldops/manifests/internal/${id}/chunks`, { bearer: MANIFEST_BEARER });
    expect(mac.status).toBe(200);
    const body = (await mac.json()) as { chunks: { chunk_b64: string }[] };
    expect(body.chunks).toHaveLength(1);
    expect(body.chunks[0].chunk_b64).toBe(PDF_B64);

    // No browser-tier route exposes the original bytes. The detail read is the closest
    // thing to one, so assert positively that it carries neither bytes nor the signature.
    const detail = await g(admin, `/api/fieldops/manifests/${id}`);
    expect(detail.status).toBe(200);
    expect(await detail.text()).not.toContain(PDF_B64);
    expect((await (await g(admin, `/api/fieldops/manifests/${id}`)).text())).not.toContain("hmac");
  });

  it("posts a grid page, upserts on replay, and refuses rows for a discarded manifest", async () => {
    const id = await uploadOk("JOB-A");
    const rows = [
      { row_index: 1, source_page: "xlsx:Export", kind: "header", cells: ["Part Number", "Qty"] },
      { row_index: 2, source_page: "xlsx:Export", kind: "data", cells: ["7006955", "4"], flags: "" },
    ];
    const post1 = await call(`/api/fieldops/manifests/internal/${id}/rows`, {
      method: "POST", bearer: MANIFEST_BEARER, body: JSON.stringify({ rows }),
    });
    expect(post1.status, await post1.clone().text()).toBe(200);
    expect(await post1.json()).toMatchObject({ ok: true, written: 2 });

    // Replay is a no-op in effect — the upsert keeps exactly two rows.
    await call(`/api/fieldops/manifests/internal/${id}/rows`, {
      method: "POST", bearer: MANIFEST_BEARER, body: JSON.stringify({ rows }),
    });
    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_manifest_rows WHERE manifest_id = ?1")
      .bind(id)
      .first<{ n: number }>();
    expect(n!.n).toBe(2);

    // Cells survive VERBATIM — the validate screen edits against them.
    const stored = await env.DB
      .prepare("SELECT cells_json FROM job_manifest_rows WHERE manifest_id = ?1 AND row_index = 2")
      .bind(id)
      .first<{ cells_json: string }>();
    expect(JSON.parse(stored!.cells_json)).toEqual(["7006955", "4"]);

    await p(admin, `/api/fieldops/manifests/${id}/discard`);
    const late = await call(`/api/fieldops/manifests/internal/${id}/rows`, {
      method: "POST", bearer: MANIFEST_BEARER, body: JSON.stringify({ rows }),
    });
    expect(late.status).toBe(404);
  });

  it("rejects a bad row kind and an over-wide row, naming the offending index", async () => {
    const id = await uploadOk("JOB-A");
    const bad = await call(`/api/fieldops/manifests/internal/${id}/rows`, {
      method: "POST",
      bearer: MANIFEST_BEARER,
      body: JSON.stringify({ rows: [{ row_index: 1, kind: "data", cells: [] }, { row_index: 2, kind: "nope", cells: [] }] }),
    });
    expect(bad.status).toBe(400);
    expect(await bad.json()).toMatchObject({ error: "invalid_row_kind", row: 1 });
  });

  it("result 'parsed' stamps the grid metadata and DROPS the pooled bytes", async () => {
    const id = await uploadOk("JOB-A");
    const res = await call("/api/fieldops/manifests/internal/result", {
      method: "POST",
      bearer: MANIFEST_BEARER,
      body: JSON.stringify({
        manifest_id: id, status: "parsed", box_file_id: "box-123", profile: "customer_bom",
        row_count: 80, column_map: { mapping: { part_number: 0 }, qty_default: 4 },
        header_meta: { CLIENT: "Evergreen" }, parse_notes: "header found in xlsx:Sheet1 at row 4",
      }),
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await res.json()).toMatchObject({ ok: true, found: true });
    const row = await env.DB
      .prepare("SELECT status, profile, row_count, box_file_id, parsed_at FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ status: "parsed", profile: "customer_bom", row_count: 80, box_file_id: "box-123" });
    expect(row!.parsed_at).toBeTruthy();
    // Bytes are gone once the grid exists — D1 holds them only mid-pipeline.
    const chunks = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_manifest_chunks WHERE manifest_id = ?1")
      .bind(id)
      .first<{ n: number }>();
    expect(chunks!.n).toBe(0);
  });

  it("result 'refused' drops the bytes too, and refuses to carry a filed-document ref", async () => {
    const id = await uploadOk("JOB-A");
    const contradiction = await call("/api/fieldops/manifests/internal/result", {
      method: "POST",
      bearer: MANIFEST_BEARER,
      body: JSON.stringify({ manifest_id: id, status: "refused", detail: "screen:malicious", box_file_id: "box-9" }),
    });
    expect(contradiction.status).toBe(400);
    expect(await contradiction.json()).toMatchObject({ error: "box_file_id_on_refusal" });

    const ok = await call("/api/fieldops/manifests/internal/result", {
      method: "POST",
      bearer: MANIFEST_BEARER,
      body: JSON.stringify({ manifest_id: id, status: "refused", detail: "screen:malicious" }),
    });
    expect(ok.status).toBe(200);
    const row = await env.DB
      .prepare("SELECT status, detail, box_file_id FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({ status: "refused", detail: "screen:malicious", box_file_id: null });
    const chunks = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_manifest_chunks WHERE manifest_id = ?1")
      .bind(id)
      .first<{ n: number }>();
    expect(chunks!.n).toBe(0);
  });

  it("a late result cannot resurrect a discarded manifest", async () => {
    const id = await uploadOk("JOB-A");
    await p(admin, `/api/fieldops/manifests/${id}/discard`);
    const late = await call("/api/fieldops/manifests/internal/result", {
      method: "POST", bearer: MANIFEST_BEARER, body: JSON.stringify({ manifest_id: id, status: "parsed" }),
    });
    expect(await late.json()).toMatchObject({ ok: true, found: false });
    const row = await env.DB
      .prepare("SELECT status FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<{ status: string }>();
    expect(row!.status).toBe("discarded");
  });

  it("refuses a status the daemon has no business stamping", async () => {
    const id = await uploadOk("JOB-A");
    for (const status of ["committed", "committing", "discarded", "pending"]) {
      const res = await call("/api/fieldops/manifests/internal/result", {
        method: "POST", bearer: MANIFEST_BEARER, body: JSON.stringify({ manifest_id: id, status }),
      });
      expect(res.status, `status=${status} should be refused`).toBe(400);
    }
  });
});

describe("manifest browser reads", () => {
  it("lists a job's manifests newest-first, without the signature", async () => {
    await uploadOk("JOB-A");
    await uploadOk("JOB-A", "Shipping Log.xlsx", XLSX_MIME, XLSX_B64);
    const res = await g(admin, "/api/fieldops/manifests?job_id=JOB-A");
    expect(res.status).toBe(200);
    const text = await res.clone().text();
    expect(text).not.toContain("hmac");
    const body = (await res.json()) as { manifests: { filename: string }[] };
    expect(body.manifests).toHaveLength(2);
    expect(body.manifests[0].filename).toBe("Shipping Log.xlsx");
  });

  it("pages the grid on a row_index cursor", async () => {
    const id = await uploadOk("JOB-A");
    const rows = Array.from({ length: 5 }, (_, i) => ({
      row_index: i + 1, kind: "data", cells: [`part-${i + 1}`],
    }));
    await call(`/api/fieldops/manifests/internal/${id}/rows`, {
      method: "POST", bearer: MANIFEST_BEARER, body: JSON.stringify({ rows }),
    });
    const page1 = (await (await g(admin, `/api/fieldops/manifests/${id}/rows?limit=2`)).json()) as {
      rows: { row_index: number }[];
    };
    expect(page1.rows.map((r) => r.row_index)).toEqual([1, 2]);
    const page2 = (await (await g(admin, `/api/fieldops/manifests/${id}/rows?after=2&limit=2`)).json()) as {
      rows: { row_index: number }[];
    };
    expect(page2.rows.map((r) => r.row_index)).toEqual([3, 4]);
  });

  it("serves a stored source-page preview and 404s a page that was never rendered", async () => {
    const id = await uploadOk("JOB-A");
    const post = await call(`/api/fieldops/manifests/internal/${id}/preview`, {
      method: "POST", bearer: MANIFEST_BEARER, body: JSON.stringify({ page: 1, png_b64: PNG_B64 }),
    });
    expect(post.status, await post.clone().text()).toBe(200);
    const got = (await (await g(admin, `/api/fieldops/manifests/${id}/preview/1`)).json()) as {
      page: number; png_b64: string;
    };
    expect(got).toMatchObject({ page: 1, png_b64: PNG_B64 });
    expect((await g(admin, `/api/fieldops/manifests/${id}/preview/2`)).status).toBe(404);
  });

  it("discard clears the grid, the previews and any surviving bytes in one batch", async () => {
    const id = await uploadOk("JOB-A");
    await call(`/api/fieldops/manifests/internal/${id}/rows`, {
      method: "POST",
      bearer: MANIFEST_BEARER,
      body: JSON.stringify({ rows: [{ row_index: 1, kind: "data", cells: ["x"] }] }),
    });
    await call(`/api/fieldops/manifests/internal/${id}/preview`, {
      method: "POST", bearer: MANIFEST_BEARER, body: JSON.stringify({ page: 1, png_b64: PNG_B64 }),
    });
    expect((await p(admin, `/api/fieldops/manifests/${id}/discard`)).status).toBe(200);
    for (const table of ["job_manifest_chunks", "job_manifest_rows", "job_manifest_previews"]) {
      const n = await env.DB
        .prepare(`SELECT COUNT(*) n FROM ${table} WHERE manifest_id = ?1`)
        .bind(id)
        .first<{ n: number }>();
      expect(n!.n, `${table} not cleared by discard`).toBe(0);
    }
    // A second discard is a 409 naming the state, not a silent 200.
    const again = await p(admin, `/api/fieldops/manifests/${id}/discard`);
    expect(again.status).toBe(409);
    expect(await again.json()).toMatchObject({ error: "not_discardable", status: "discarded" });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// /plan (dry run) + /commit (paged, watermarked). These are the routes that actually
// author a job's material list, so the properties under test are: an imported line is
// INDISTINGUISHABLE from a hand-authored one, a replay is a no-op, and an ambiguous
// part-number match is surfaced rather than silently resolved.
// ─────────────────────────────────────────────────────────────────────────────

/** Put a manifest into the `parsed` state so it may commit. */
async function parsedManifest(jobId = "JOB-A"): Promise<number> {
  const id = await uploadOk(jobId);
  await call("/api/fieldops/manifests/internal/result", {
    method: "POST",
    bearer: MANIFEST_BEARER,
    body: JSON.stringify({ manifest_id: id, status: "parsed", box_file_id: "box-1", row_count: 3 }),
  });
  return id;
}

function line(sourceRow: number, part: string, description = "Pile cap", qty = 4) {
  return { source_row_index: sourceRow, part_number: part, description, qty };
}

async function seedLine(jobId: string, part: string | null, description = "existing") {
  await env.DB
    .prepare(
      "INSERT INTO job_expected_materials (job_id, description, part_number, seq, line_uuid) VALUES (?1,?2,?3,10,?4)",
    )
    .bind(jobId, description, part, `lu-${jobId}-${part}-${Math.floor(Math.random() * 1e9)}`)
    .run();
}

describe("manifest /plan (dry run)", () => {
  it("classifies matched / new / absent and WRITES NOTHING", async () => {
    const id = await parsedManifest();
    await seedLine("JOB-A", "7006955");
    await seedLine("JOB-A", "9999999"); // on the list, absent from this document

    const res = await p(admin, `/api/fieldops/manifests/${id}/plan`, {
      lines: [line(2, "7006955"), line(3, "7000153")],
    });
    expect(res.status, await res.clone().text()).toBe(200);
    const body = (await res.json()) as any;
    expect(body.counts).toMatchObject({ incoming: 2, matched: 1, ambiguous: 0, new: 1, absent: 1 });
    expect(body.absent[0].part_number).toBe("9999999");

    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_expected_materials WHERE job_id='JOB-A'")
      .first<{ n: number }>();
    expect(n!.n, "a dry run must not write").toBe(2);
  });

  it("reports AMBIGUOUS when a part number matches more than one existing line", async () => {
    // Duplicate part numbers are universal in the real BOMs (7000153 appears twice in
    // three of the four sample Customer BOMs, under different groupings). Picking the
    // first match silently is precisely the failure this classification prevents.
    const id = await parsedManifest();
    await seedLine("JOB-A", "7000153", "under HARDWARE");
    await seedLine("JOB-A", "7000153", "under STRUCTURAL");

    const body = (await (
      await p(admin, `/api/fieldops/manifests/${id}/plan`, { lines: [line(2, "7000153")] })
    ).json()) as any;
    expect(body.counts).toMatchObject({ matched: 0, ambiguous: 1, new: 0 });
    expect(body.ambiguous[0].line_ids).toHaveLength(2);
  });

  it("warns when committing would push the job past the read route's line cap", async () => {
    const id = await parsedManifest();
    const many = Array.from({ length: 501 }, (_, i) => line(i + 2, `P-${i}`));
    const body = (await (
      await p(admin, `/api/fieldops/manifests/${id}/plan`, { lines: many })
    ).json()) as any;
    expect(body.would_exceed_line_cap).toBe(true);
  });
});

describe("manifest /commit (paged + watermarked)", () => {
  it("authors lines INDISTINGUISHABLE from hand-authored ones", async () => {
    const id = await parsedManifest();
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "add_new",
      lines: [line(2, "7006955", "Concrete pile cap", 4)],
    });
    expect(res.status, await res.clone().text()).toBe(200);

    const row = await env.DB
      .prepare(
        "SELECT job_id, description, part_number, qty, status, unplanned, active, line_uuid " +
          "FROM job_expected_materials WHERE job_id='JOB-A'",
      )
      .first<Record<string, unknown>>();
    expect(row).toMatchObject({
      job_id: "JOB-A", description: "Concrete pile cap", part_number: "7006955", qty: 4,
      // status stays 'expected' and unplanned stays 0: an import is on-manifest by
      // definition, and the delivery SoR is the receipt ledger — a shipping log's
      // delivery_date must never become status='received'.
      status: "expected", unplanned: 0, active: 1,
    });
    // line_uuid is UNIQUE and is the §51 mirror's find-or-create key. SQLite allows
    // multiple NULLs there, so omitting it would NOT error — it would silently break
    // the mirror's upsert authority. This is the fail-silent trap.
    expect(row!.line_uuid).toBeTruthy();

    const audit = await env.DB
      .prepare("SELECT COUNT(*) n FROM audit_log WHERE action='expected_material_create'")
      .first<{ n: number }>();
    expect(audit!.n).toBe(1);
  });

  it("REFUSES a part-number-only row rather than inventing a description", async () => {
    // readExpectationFields' description_required rule fires for the import exactly as it
    // does for the hand-authored create. Synthesizing one here would be invented field
    // data (§4); the row index tells the validate screen which line to fix.
    const id = await parsedManifest();
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "add_new",
      lines: [{ source_row_index: 2, part_number: "7006955", qty: 4 }],
    });
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({ error: "description_required", row: 0 });
    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_expected_materials WHERE job_id='JOB-A'")
      .first<{ n: number }>();
    expect(n!.n).toBe(0);
  });

  it("pages, advances the watermark, and a REPLAY is a no-op", async () => {
    const id = await parsedManifest();
    // 150 lines => two pages at COMMIT_PAGE_LINES=100.
    const lines = Array.from({ length: 150 }, (_, i) => line(i + 2, `P-${i}`));

    const first = (await (
      await p(admin, `/api/fieldops/manifests/${id}/commit`, { mode: "add_new", lines })
    ).json()) as any;
    expect(first).toMatchObject({ ok: true, done: false, inserted: 100 });
    expect(first.committed_through_row).toBe(101);

    // Re-posting the SAME payload must land nothing new — the watermark drops the first
    // page before any write, which is what makes a retried request safe.
    const replay = (await (
      await p(admin, `/api/fieldops/manifests/${id}/commit`, { mode: "add_new", lines })
    ).json()) as any;
    expect(replay.inserted).toBe(50);
    expect(replay.done).toBe(true);

    const n = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_expected_materials WHERE job_id='JOB-A'")
      .first<{ n: number }>();
    expect(n!.n, "150 source rows must yield exactly 150 lines across pages + a replay").toBe(150);

    const done = (await (
      await p(admin, `/api/fieldops/manifests/${id}/commit`, { mode: "add_new", lines })
    ).json()) as any;
    expect(done).toMatchObject({ ok: true, done: true, inserted: 0 });
    const manifest = await env.DB
      .prepare("SELECT status, mode, committed_at FROM job_manifests WHERE id = ?1")
      .bind(id)
      .first<Record<string, unknown>>();
    expect(manifest).toMatchObject({ status: "committed", mode: "add_new" });
    expect(manifest!.committed_at).toBeTruthy();
  });

  it("refuses to commit a manifest that is not parsed", async () => {
    const id = await uploadOk("JOB-A"); // still 'pending'
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "add_new", lines: [line(2, "7006955")],
    });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "not_committable", status: "pending" });
  });

  it("refuses a commit that would push the job past the line cap", async () => {
    const id = await parsedManifest();
    const many = Array.from({ length: 501 }, (_, i) => line(i + 2, `P-${i}`));
    // The first page is fine; seed the job to just under the cap so the page overflows it.
    for (let i = 0; i < 450; i++) await seedLine("JOB-A", `X-${i}`);
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, { mode: "add_new", lines: many });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "line_cap_exceeded" });
  });

  it("requires cap.materials.manage on both routes", async () => {
    const id = await parsedManifest();
    expect((await p(sub, `/api/fieldops/manifests/${id}/plan`, { lines: [line(2, "X")] })).status).toBe(403);
    expect(
      (await p(sub, `/api/fieldops/manifests/${id}/commit`, { mode: "add_new", lines: [line(2, "X")] })).status,
    ).toBe(403);
  });

  it("rejects an unknown mode rather than defaulting to one", async () => {
    const id = await parsedManifest();
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "replace", lines: [line(2, "7006955")],
    });
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({ error: "invalid_mode" });
  });
});
