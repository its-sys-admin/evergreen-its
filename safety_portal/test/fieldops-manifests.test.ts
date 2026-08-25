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
    // project_name is JOINed from jobs (unsigned foldering metadata): the Mac keys the
    // Box per-job folder off the PROJECT NAME, not the raw job_id — the id-named-folder
    // bug the 2026-08-11 change fixed. A vanished job row serves NULL (daemon falls back).
    expect(body.manifests[0].project_name).toBe("Project JOB-A");
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

  // 15s timeout: seeds 450 rows one INSERT at a time before the assertion — measured ~6.5s on a
  // loaded runner vs the 5s default, and it flaked main-branch CI on #60's merge commit
  // (2026-08-11) exactly as the tech-debt entry predicted. Timing flake, not a regression.
  it("refuses a commit that would push the job past the line cap", { timeout: 15_000 }, async () => {
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

// ── 2026-08-11: merge is REAL, ambiguity is enforced SERVER-SIDE, shipping logs land as
// LOADS, and an interrupted commit can be abandoned. Before this block's subjects existed,
// mode:'merge' was validated, stored, and never branched on — the commit always INSERTed,
// so "Merge onto the matching line" silently duplicated every already-listed part (audit A2).
describe("manifest /commit — merge, resolutions, shipments", () => {
  async function lineByPart(jobId: string, part: string) {
    return env.DB
      .prepare(
        "SELECT id, description, qty, status, active FROM job_expected_materials " +
          "WHERE job_id = ?1 AND part_number = ?2 ORDER BY id ASC",
      )
      .bind(jobId, part)
      .all<{ id: number; description: string | null; qty: number | null; status: string; active: number }>();
  }
  async function lineCount(jobId: string): Promise<number> {
    const r = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_expected_materials WHERE job_id = ?1 AND active = 1")
      .bind(jobId)
      .first<{ n: number }>();
    return r?.n ?? 0;
  }
  async function shipments(jobId: string) {
    const { results } = await env.DB
      .prepare(
        "SELECT shipment_uuid, line_id, part_number, bol_number, qty, ship_date, delivery_date, source " +
          "FROM material_shipments WHERE job_id = ?1 ORDER BY id ASC",
      )
      .bind(jobId)
      .all<Record<string, unknown>>();
    return results ?? [];
  }

  it("merge UPDATEs a uniquely-matched line in place — no new row, document's non-null fields win", async () => {
    const id = await parsedManifest();
    await seedLine("JOB-A", "7006955", "old description");
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge", lines: [{ ...line(2, "7006955", "new description", 9), unit: null }],
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ inserted: 0, updated: 1, shipments: 0, done: true });
    const { results } = await lineByPart("JOB-A", "7006955");
    expect(results).toHaveLength(1); // updated IN PLACE — the A2 duplicate never appears
    expect(results[0]).toMatchObject({ description: "new description", qty: 9 });
  });

  it("merge REFUSES an undecided ambiguous part BEFORE any write", async () => {
    const id = await parsedManifest();
    await seedLine("JOB-A", "7000153", "first");
    await seedLine("JOB-A", "7000153", "second");
    const before = await lineCount("JOB-A");
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge", lines: [line(2, "7000153", "incoming", 5)],
    });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "ambiguous_unresolved", rows: [2] });
    expect(await lineCount("JOB-A")).toBe(before); // all-or-nothing: nothing landed
  });

  it("merge honors a valid resolution and REJECTS one outside the server's own match set", async () => {
    const id = await parsedManifest();
    await seedLine("JOB-A", "7000153", "first");
    await seedLine("JOB-A", "7000153", "second");
    await seedLine("JOB-A", "OTHER", "unrelated");
    const rows = (await lineByPart("JOB-A", "7000153")).results;
    const otherId = (await lineByPart("JOB-A", "OTHER")).results[0].id;

    // A resolution naming a line that is NOT among that part's matches is a forged claim,
    // not a choice — refused, even though the id exists on the same job.
    const forged = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge", lines: [line(2, "7000153", "incoming", 5)],
      resolutions: { 2: otherId },
    });
    expect(forged.status).toBe(409);
    expect(await forged.json()).toMatchObject({ error: "ambiguous_unresolved" });

    const ok = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge", lines: [line(2, "7000153", "incoming", 5)],
      resolutions: { 2: rows[1].id },
    });
    expect(ok.status).toBe(200);
    expect(await ok.json()).toMatchObject({ updated: 1, inserted: 0 });
    const after = (await lineByPart("JOB-A", "7000153")).results;
    expect(after.find((r) => r.id === rows[1].id)).toMatchObject({ description: "incoming", qty: 5 });
    expect(after.find((r) => r.id === rows[0].id)).toMatchObject({ description: "first" }); // untouched
  });

  it("merge leaves a received line's facts LOCKED and reports it skipped_locked", async () => {
    const id = await parsedManifest();
    await seedLine("JOB-A", "7006955", "as received");
    await env.DB
      .prepare("UPDATE job_expected_materials SET status='received' WHERE job_id='JOB-A' AND part_number='7006955'")
      .run();
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge", lines: [line(2, "7006955", "rewrite attempt", 99)],
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { updated: number; inserted: number; skipped_locked: unknown[] };
    expect(body.updated).toBe(0);
    expect(body.inserted).toBe(0);
    expect(body.skipped_locked).toHaveLength(1);
    const { results } = await lineByPart("JOB-A", "7006955");
    expect(results[0]).toMatchObject({ description: "as received", status: "received" });
  });

  it("merge counts only NEW lines against the cap — updates are free", { timeout: 15_000 }, async () => {
    const id = await parsedManifest();
    // Bulk-batch the seeding (one round trip) — the per-INSERT loop shape is the
    // documented CI timing flake.
    const stmts = [];
    for (let i = 0; i < 499; i++) {
      stmts.push(
        env.DB
          .prepare(
            "INSERT INTO job_expected_materials (job_id, description, part_number, seq, line_uuid) VALUES ('JOB-A', ?1, ?2, 10, ?3)",
          )
          .bind(`seed ${i}`, `M-${i}`, `lu-cap-${i}`),
      );
    }
    await env.DB.batch(stmts);
    // 90 matched lines → 90 UPDATEs, 0 inserts: passes even though 499 + 90 > 500.
    const merges = Array.from({ length: 90 }, (_, i) => line(i + 2, `M-${i}`, `updated ${i}`, 1));
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, { mode: "merge", lines: merges });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ inserted: 0, updated: 90 });
    // But genuinely NEW lines over the cap still refuse — on a FRESH manifest (distinct
    // bytes: the per-job sha dedupe correctly 409s a re-upload of the committed one, and
    // committed is done:true → takes no more pages).
    const id2 = await uploadOk("JOB-A", "Shipping Log.xlsx", XLSX_MIME, XLSX_B64);
    await call("/api/fieldops/manifests/internal/result", {
      method: "POST",
      bearer: MANIFEST_BEARER,
      body: JSON.stringify({ manifest_id: id2, status: "parsed", box_file_id: "box-2", row_count: 3 }),
    });
    const res2 = await p(admin, `/api/fieldops/manifests/${id2}/commit`, {
      mode: "merge", lines: [line(2, "BRAND-NEW", "one too many", 1), line(3, "BRAND-NEW-2", "two too many", 1)],
    });
    expect(res2.status).toBe(409);
    expect(await res2.json()).toMatchObject({ error: "line_cap_exceeded" });
  });

  it("shipments import lands loads on matched lines and creates line+load for unmatched parts", async () => {
    const id = await parsedManifest();
    await seedLine("JOB-A", "7006955", "the expected pile caps");
    const matchedLineId = (await lineByPart("JOB-A", "7006955")).results[0].id;
    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "add_new",
      import_as: "shipments",
      lines: [
        { ...line(2, "7006955", "load one", 40), expected_ship_date: "2026-08-12", expected_date: "2026-08-15", bol: "BOL-100" },
        { ...line(3, "NEW-PART", "a part the list did not have", 8), bol: "BOL-101" },
      ],
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ shipments: 2, inserted: 1, updated: 0, done: true });
    const loads = await shipments("JOB-A");
    expect(loads).toHaveLength(2);
    expect(loads[0]).toMatchObject({
      line_id: matchedLineId, part_number: "7006955", bol_number: "BOL-100",
      qty: 40, ship_date: "2026-08-12", delivery_date: "2026-08-15", source: "import",
    });
    // Deterministic uuid ⇒ a replayed page cannot silently duplicate the load.
    expect(loads[0].shipment_uuid).toBe(`mf${id}-r2`);
    // The unmatched part got a REAL line, and its load hangs off it.
    const created = (await lineByPart("JOB-A", "NEW-PART")).results;
    expect(created).toHaveLength(1);
    expect(loads[1]).toMatchObject({ line_id: created[0].id, bol_number: "BOL-101" });
  });

  it("a manifest stranded in 'committing' CAN be discarded; later commit pages then refuse", async () => {
    const id = await parsedManifest();
    // Page 1 lands and leaves the manifest committing (watermark 2 < the payload's 3 —
    // simulate the interrupted client by just not posting the rest).
    const first = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "add_new", lines: [line(2, "A-1"), line(3, "A-2")].slice(0, 1),
    });
    expect(first.status).toBe(200);
    await env.DB.prepare("UPDATE job_manifests SET status='committing' WHERE id = ?1").bind(id).run();

    const discard = await p(admin, `/api/fieldops/manifests/${id}/discard`);
    expect(discard.status).toBe(200);
    const row = await env.DB
      .prepare("SELECT status FROM job_manifests WHERE id = ?1").bind(id)
      .first<{ status: string }>();
    expect(row?.status).toBe("discarded");
    // The already-imported line SURVIVES — discard stops the import, it never unwinds it.
    expect((await lineByPart("JOB-A", "A-1")).results).toHaveLength(1);
    // And an in-flight page replaying AFTER the discard refuses rather than resurrecting it.
    const late = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "add_new", lines: [line(3, "A-2")],
    });
    expect(late.status).toBe(409);
    expect(await late.json()).toMatchObject({ error: "not_committable", status: "discarded" });
  });

  it("plan returns mode-aware projected totals", async () => {
    const id = await parsedManifest();
    await seedLine("JOB-A", "7006955");
    const res = await p(admin, `/api/fieldops/manifests/${id}/plan`, {
      lines: [line(2, "7006955"), line(3, "FRESH")],
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, number | boolean>;
    // 1 existing + 2 incoming if everything inserts; 1 existing + 1 fresh under merge.
    expect(body.projected_total_add_new).toBe(3);
    expect(body.projected_total_merge).toBe(2);
    expect(body.projected_total).toBe(3); // the bare field keeps the CONSERVATIVE number
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// CONTINUATION ROWS — a repeat truckload of the part above, never a second line.
//
// A shipping log records one part arriving across several loads: Deep Lake's is 51 parts
// in 56 rows, Kiwi's 49 in 50. `manifest_parse.classify_rows` marks the extras
// `kind='continuation'` and forward-fills the parent's identity INCLUDING its ORDER
// quantity. Committing one as a line produced a byte-identical duplicate that doubled the
// BOM requirement and discarded the BOL (Kiwi line 9243: part 805271-SHIP counted 1006
// against a real 503; Deep Lake, five more). Forensic report 2026-08-24, defects D1 + D4.
//
// The kind is read from `job_manifest_rows` SERVER-SIDE, never from the posted body — a
// client that could relabel a continuation could re-create the duplicate at will.
// ─────────────────────────────────────────────────────────────────────────────
describe("manifest /commit — continuation rows are LOADS, not lines", () => {
  /** Seed the stored parse for a row so the commit route can read its kind. */
  async function seedRowKind(manifestId: number, rowIndex: number, kind: string) {
    await env.DB
      .prepare(
        "INSERT INTO job_manifest_rows (manifest_id, row_index, source_page, kind, cells_json, flags) " +
          "VALUES (?1, ?2, 1, ?3, '[]', '[]')",
      )
      .bind(manifestId, rowIndex, kind)
      .run();
  }
  // Local copies: the /commit describe's own helpers are scoped to that block.
  async function linesByPart(jobId: string, part: string) {
    const { results } = await env.DB
      .prepare(
        "SELECT id, description, qty, status, active FROM job_expected_materials " +
          "WHERE job_id = ?1 AND part_number = ?2 ORDER BY id ASC",
      )
      .bind(jobId, part)
      .all<{ id: number; description: string; qty: number | null; status: string; active: number }>();
    return results ?? [];
  }
  async function countLines(jobId: string): Promise<number> {
    const r = await env.DB
      .prepare("SELECT COUNT(*) n FROM job_expected_materials WHERE job_id = ?1 AND active = 1")
      .bind(jobId)
      .first<{ n: number }>();
    return r?.n ?? 0;
  }
  /** Loads created BY THIS MANIFEST — the suite shares one DB, and shipment_uuid is
   *  deterministically `mf<manifest_id>-r<row>`, so this scopes exactly. */
  async function loadsFor(manifestId: number) {
    const { results } = await env.DB
      .prepare(
        "SELECT s.id, s.line_id, s.part_number, s.bol_number, s.qty FROM material_shipments s " +
          "WHERE s.shipment_uuid LIKE ?1 ORDER BY s.id ASC",
      )
      .bind(`mf${manifestId}-%`)
      .all<{ id: number; line_id: number; part_number: string; bol_number: string | null; qty: number | null }>();
    return results ?? [];
  }

  it("the Kiwi shape: a data row + its continuation make ONE line and ONE load", async () => {
    const id = await parsedManifest();
    await seedRowKind(id, 2, "data");
    await seedRowKind(id, 3, "continuation");

    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge",
      lines: [
        { ...line(2, "CONT-805271", "1P DRIVEN PILE W8X10 BEAM HS (HDG) - 150IN", 503), bol: "LD0872247" },
        // The parser forward-filled 503 into the continuation — the parent's ORDER qty.
        { ...line(3, "CONT-805271", "1P DRIVEN PILE W8X10 BEAM HS (HDG) - 150IN", 503), bol: "LD0872244" },
      ],
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await res.json()).toMatchObject({ inserted: 1, shipments: 1 });

    // ONE line, carrying the real requirement — not two carrying 1006 between them.
    const lines = await linesByPart("JOB-A", "CONT-805271");
    expect(lines).toHaveLength(1);
    expect(lines[0].qty).toBe(503);

    // The continuation became a LOAD on that line, keeping its own BOL.
    const loads = await loadsFor(id);
    expect(loads).toHaveLength(1);
    expect(loads[0].line_id).toBe(lines[0].id);
    expect(loads[0].bol_number).toBe("LD0872244");
    // qty is NULL, not 503: the forward-filled number is the ORDER quantity, and claiming
    // 503 arrived on a truck that carried 251 would be worse than recording nothing.
    expect(loads[0].qty).toBeNull();
  });

  it("attaches to an EXISTING line when a page boundary split it from its parent", async () => {
    const id = await parsedManifest();
    await seedRowKind(id, 2, "continuation");
    await seedLine("JOB-A", "CONT-805271", "already on the list");
    const before = await countLines("JOB-A");

    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge",
      lines: [{ ...line(2, "CONT-805271", "1P DRIVEN PILE", 503), bol: "LD0872244" }],
    });
    expect(res.status).toBe(200);
    expect(await countLines("JOB-A")).toBe(before); // no new line
    const loads = await loadsFor(id);
    expect(loads).toHaveLength(1);
    expect(loads[0].bol_number).toBe("LD0872244");
  });

  it("an ORPHAN continuation is refused BEFORE any write, naming its row", async () => {
    const id = await parsedManifest();
    await seedRowKind(id, 2, "continuation"); // nothing above it, nothing on the list
    const before = await countLines("JOB-A");

    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge",
      lines: [{ ...line(2, "CONT-805271", "1P DRIVEN PILE", 503), bol: "LD0872244" }],
    });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "orphan_continuation", rows: [2] });
    // All-or-nothing: nothing landed, not even a line for the orphan.
    expect(await countLines("JOB-A")).toBe(before);
    expect(await loadsFor(id)).toHaveLength(0);
  });

  it("add_new is held to the same rule — no mode makes a load into a line", async () => {
    const id = await parsedManifest();
    await seedRowKind(id, 2, "data");
    await seedRowKind(id, 3, "continuation");

    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "add_new",
      lines: [
        { ...line(2, "CONT-805275", "Beam", 1255), bol: "LD1" },
        { ...line(3, "CONT-805275", "Beam", 1255), bol: "LD2" },
      ],
    });
    expect(res.status).toBe(200);
    expect(await linesByPart("JOB-A", "CONT-805275")).toHaveLength(1);
    expect(await loadsFor(id)).toHaveLength(1);
  });

  it("import_as='shipments': a NEW part plus its continuation lands, it does not 409", async () => {
    // ManifestValidatePage auto-selects import_as='shipments' for every shipping-log profile,
    // so this is the DEFAULT path for the documents that carry continuations. The
    // shipment_new_line branch used to leave parentTarget null — its own continuation then
    // had no parent and refused the whole page (audit 2026-08-25).
    const id = await parsedManifest();
    await seedRowKind(id, 2, "data");
    await seedRowKind(id, 3, "continuation");

    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge",
      import_as: "shipments",
      lines: [
        { ...line(2, "CONT-NEWPART", "Not on the list yet", 40), bol: "LD-A" },
        { ...line(3, "CONT-NEWPART", "Not on the list yet", 40), bol: "LD-B" },
      ],
    });
    expect(res.status, await res.clone().text()).toBe(200);
    // ONE line created for the part, and BOTH rows recorded as loads against it.
    const lines = await linesByPart("JOB-A", "CONT-NEWPART");
    expect(lines).toHaveLength(1);
    const loads = await loadsFor(id);
    expect(loads).toHaveLength(2);
    expect(loads.every((l) => l.line_id === lines[0].id)).toBe(true);
    expect(loads.map((l) => l.bol_number).sort()).toEqual(["LD-A", "LD-B"]);
  });

  it("a continuation whose part does not match the row above it is REFUSED, not re-parented", async () => {
    // The validate screen lets a human untick any single row. Unticking a parent while keeping
    // its continuation would otherwise hang that truckload off whatever part happened to
    // precede it — silently, against the wrong line. The parser forward-fills the parent's
    // part number into the continuation, so disagreement means "not this row's parent".
    const id = await parsedManifest();
    await seedRowKind(id, 2, "data");
    await seedRowKind(id, 4, "continuation"); // row 3 (its real parent) was unticked
    const before = await countLines("JOB-A");

    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "merge",
      lines: [
        line(2, "CONT-OTHER", "an unrelated part", 5),
        { ...line(4, "CONT-ORPHANED", "the unticked parent's part", 99), bol: "LD-X" },
      ],
    });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "orphan_continuation", rows: [4] });
    expect(await countLines("JOB-A")).toBe(before); // all-or-nothing: even row 2 did not land
    expect(await loadsFor(id)).toHaveLength(0);
  });

  it("a plain repeated part number is STILL two lines — only a continuation is a load", async () => {
    // A manifest legitimately repeats a part across unrelated BOM groupings (0059's own
    // header says so). The rule keys on the parser's kind, not on part-number sameness.
    const id = await parsedManifest();
    await seedRowKind(id, 2, "data");
    await seedRowKind(id, 3, "data");

    const res = await p(admin, `/api/fieldops/manifests/${id}/commit`, {
      mode: "add_new",
      lines: [line(2, "CONT-REPEAT", "under HARDWARE", 10), line(3, "CONT-REPEAT", "under CONTROLS", 4)],
    });
    expect(res.status).toBe(200);
    expect(await linesByPart("JOB-A", "CONT-REPEAT")).toHaveLength(2);
    expect(await loadsFor(id)).toHaveLength(0);
  });
});
