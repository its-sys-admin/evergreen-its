import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, seedJob, seedPersonnel } from "./helpers";

// P7 Material List up-sync (M2) — the field-ops Material List snapshot route
// (GET /api/internal/fieldops/material-list-snapshot). A SNAPSHOT (re-projected each cycle) of the
// operator-authored per-job expected-materials list: one row per ACTIVE (active=1)
// job_expected_materials line on an ACTIVE job. No watermark, no mark-mirrored. Same field-ops
// token privilege separation as the job/hours/equipment mirror queues. DISPLAY-NAME-ONLY received_by.

const FIELDOPS_BEARER = "test-fieldops-token"; // == PORTAL_FIELDOPS_API_TOKEN (vitest.config.ts)
const INTERNAL_BEARER = "test-internal-token"; // portal_poll's token — must be REJECTED here
const ADMIN_BEARER = "test-admin-token"; // operator token — must be REJECTED here

const PATH = "/api/internal/fieldops/material-list-snapshot";

interface LineRow {
  line_uuid: string;
  job_id: string;
  project_name: string;
  material_id: number | null;
  catalog_name: string | null;
  description: string | null;
  qty: number | null;
  unit: string | null;
  expected_date: string | null;
  status: string;
  part_number: string | null;
  category: string | null;
  expected_ship_date: string | null;
  received_at: number | null;
  qty_received: number | null;
  received_by_display: string | null;
  note: string | null;
  unplanned: number;
  seq: number;
}

interface RosterRow {
  job_id: string;
  project_name: string;
}

// A catalog-picked line (material_id → material_catalog.id). Returns the new row id + its line_uuid.
async function seedLine(
  jobId: string,
  opts: {
    materialId?: number | null;
    description?: string | null;
    qty?: number | null;
    unit?: string | null;
    expectedDate?: string | null;
    status?: string;
    receivedAt?: number | null;
    receivedBy?: string | null;
    qtyReceived?: number | null;
    note?: string | null;
    seq?: number;
    active?: 0 | 1;
    unplanned?: 0 | 1;
    partNumber?: string | null;
    category?: string | null;
    expectedShipDate?: string | null;
  } = {},
): Promise<{ id: number; line_uuid: string }> {
  const lineUuid = crypto.randomUUID();
  await env.DB.prepare(
    `INSERT INTO job_expected_materials
       (job_id, material_id, description, qty, unit, expected_date, status,
        received_at, received_by, qty_received, note, seq, active, unplanned, line_uuid,
        part_number, category, expected_ship_date)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  )
    .bind(
      jobId,
      opts.materialId ?? null,
      opts.description ?? null,
      opts.qty ?? null,
      opts.unit ?? null,
      opts.expectedDate ?? null,
      opts.status ?? "expected",
      opts.receivedAt ?? null,
      opts.receivedBy ?? null,
      opts.qtyReceived ?? null,
      opts.note ?? null,
      opts.seq ?? 0,
      opts.active ?? 1,
      opts.unplanned ?? 0,
      lineUuid,
      opts.partNumber ?? null,
      opts.category ?? null,
      opts.expectedShipDate ?? null,
    )
    .run();
  const id = (await env.DB.prepare("SELECT id FROM job_expected_materials WHERE line_uuid=?")
    .bind(lineUuid)
    .first<{ id: number }>())!.id;
  return { id, line_uuid: lineUuid };
}

// A seeded (0019) ACTIVE catalog id, for catalog-pick rows.
async function seededCatalogId(): Promise<number> {
  const row = await env.DB.prepare("SELECT id FROM material_catalog WHERE model_id=?")
    .bind("Q.PEAK_DUO_XL-G11.3_BFG")
    .first<{ id: number }>();
  expect(row).not.toBeNull();
  return row!.id;
}

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
});

describe("GET /api/internal/fieldops/material-list-snapshot", () => {
  it("rejects the portal_poll + admin tokens and no token (privilege separation)", async () => {
    expect((await call(PATH)).status).toBe(401);
    expect((await call(PATH, { bearer: INTERNAL_BEARER })).status).toBe(401);
    expect((await call(PATH, { bearer: ADMIN_BEARER })).status).toBe(401);
    expect((await call(PATH, { bearer: FIELDOPS_BEARER })).status).toBe(200);
  });

  it("projects the full field set for a catalog-picked line on an active job", async () => {
    await seedJob("JOB-A", { projectName: "Job Alpha", status: "active" });
    const catId = await seededCatalogId();
    const { line_uuid } = await seedLine("JOB-A", {
      materialId: catId,
      description: "roof array",
      qty: 120,
      unit: "panels",
      expectedDate: "2026-07-10",
      status: "expected",
      seq: 10,
      partNumber: "7000153",
      category: "HARDWARE",
      expectedShipDate: "2026-07-02",
    });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    expect(res.status, await res.clone().text()).toBe(200);
    const { lines } = (await res.json()) as { lines: LineRow[] };
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatchObject({
      line_uuid,
      job_id: "JOB-A",
      project_name: "Job Alpha",
      material_id: catId,
      catalog_name: "Q.PEAK_DUO_XL-G11.3_BFG", // resolved from material_catalog.model_id (LEFT JOIN)
      description: "roof array",
      qty: 120,
      unit: "panels",
      expected_date: "2026-07-10",
      status: "expected",
      // 0059 additions. 0059 deliberately left this route on pre-0059 columns and deferred their
      // mirror exposure to PR4 — this pins that it happened.
      part_number: "7000153",
      category: "HARDWARE",
      expected_ship_date: "2026-07-02",
      unplanned: 0,
      seq: 10,
    });
  });

  it("projects expected_ship_date and expected_date as DISTINCT fields (ship vs delivery)", async () => {
    // 0059 is explicit that expected_date KEEPS its 0031 meaning (expected DELIVERY) and the new
    // column is the SHIP date. Collapsing the two would silently change the meaning of live data on
    // every surface already rendering "Expected Date".
    await seedJob("JOB-A", { projectName: "Job Alpha", status: "active" });
    await seedLine("JOB-A", {
      description: "piles",
      expectedDate: "2026-07-10",     // delivery
      expectedShipDate: "2026-07-02", // ship — earlier, and separately tracked
    });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    const { lines } = (await res.json()) as { lines: LineRow[] };
    expect(lines[0].expected_date).toBe("2026-07-10");
    expect(lines[0].expected_ship_date).toBe("2026-07-02");
    expect(lines[0].expected_date).not.toBe(lines[0].expected_ship_date);
  });

  it("a pre-0059 line projects part_number / category / expected_ship_date as NULL", async () => {
    // Every line written before the 0059 ALTERs has NULL there; the mirror must render blank cells
    // rather than fail, which is what the daemon's `or ""` mapping relies on.
    await seedJob("JOB-A", { projectName: "Job Alpha", status: "active" });
    await seedLine("JOB-A", { description: "legacy line" });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    const { lines } = (await res.json()) as { lines: LineRow[] };
    expect(lines[0]).toMatchObject({
      part_number: null,
      category: null,
      expected_ship_date: null,
    });
  });

  it("free-text line has NULL catalog_name; unplanned flag is projected", async () => {
    await seedJob("JOB-A", { projectName: "Job Alpha", status: "active" });
    await seedLine("JOB-A", { materialId: null, description: "Rebar bundles", unplanned: 1 });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    const { lines } = (await res.json()) as { lines: LineRow[] };
    expect(lines).toHaveLength(1);
    expect(lines[0].catalog_name).toBeNull();
    expect(lines[0].material_id).toBeNull();
    expect(lines[0].description).toBe("Rebar bundles");
    expect(lines[0].unplanned).toBe(1);
  });

  it("resolves received_by DISPLAY-NAME-ONLY (never the raw username; unmatched → NULL)", async () => {
    await seedJob("JOB-A", { projectName: "Job Alpha", status: "active" });
    await seedPersonnel("Mo Manager", "mgr.mo", "JOB-A");
    // Received by a KNOWN account → the display name; and a line received by an UNKNOWN account.
    await seedLine("JOB-A", { status: "received", receivedBy: "mgr.mo", receivedAt: 1751000000, qtyReceived: 100, seq: 10 });
    await seedLine("JOB-A", { status: "received", receivedBy: "ghost.acct", receivedAt: 1751000000, seq: 20 });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    const { lines } = (await res.json()) as { lines: LineRow[] };
    const bySeq = Object.fromEntries(lines.map((l) => [l.seq, l]));
    expect(bySeq[10].received_by_display).toBe("Mo Manager"); // display name, not "mgr.mo"
    expect(bySeq[20].received_by_display).toBeNull(); // unmatched account → NULL, never the username
    // The raw username never appears anywhere in the response body.
    expect(JSON.stringify(lines)).not.toContain("mgr.mo");
    expect(JSON.stringify(lines)).not.toContain("ghost.acct");
  });

  it("excludes deactivated (active=0) lines from `lines`", async () => {
    await seedJob("JOB-A", { projectName: "Job Alpha", status: "active" });
    await seedLine("JOB-A", { description: "Live line", active: 1 });
    await seedLine("JOB-A", { description: "Dead line", active: 0 });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    const { lines } = (await res.json()) as { lines: LineRow[] };
    expect(lines.map((l) => l.description)).toEqual(["Live line"]);
  });

  it("excludes lines whose job is non-active (closed/on_hold) — active-job filter", async () => {
    await seedJob("JOB-ACTIVE", { projectName: "Active Job", status: "active" });
    await seedJob("JOB-CLOSED", { projectName: "Closed Job", status: "closed" });
    await seedLine("JOB-ACTIVE", { description: "On active" });
    await seedLine("JOB-CLOSED", { description: "On closed" });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    const { lines } = (await res.json()) as { lines: LineRow[] };
    expect(lines.map((l) => l.description)).toEqual(["On active"]);
  });

  it("orders lines by project_name then seq then id", async () => {
    await seedJob("JOB-B", { projectName: "Bravo", status: "active" });
    await seedJob("JOB-A", { projectName: "Alpha", status: "active" });
    await seedLine("JOB-B", { description: "b1", seq: 10 });
    await seedLine("JOB-A", { description: "a2", seq: 20 });
    await seedLine("JOB-A", { description: "a1", seq: 10 });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    const { lines } = (await res.json()) as { lines: LineRow[] };
    expect(lines.map((l) => `${l.project_name}/${l.description}`)).toEqual([
      "Alpha/a1",
      "Alpha/a2",
      "Bravo/b1",
    ]);
  });

  it("returns jobs_with_materials: a job with ALL lines deactivated appears in the roster but NOT in lines (count-drops-to-zero reconcile)", async () => {
    await seedJob("JOB-ACTIVE", { projectName: "Active Job", status: "active" });
    // The job HAS material-line history (so it's in the roster) but every line is deactivated → zero
    // active lines. It MUST appear in `jobs_with_materials` so the daemon revisits its Material List
    // sheet and marks the now-stale rows Removed.
    await seedLine("JOB-ACTIVE", { description: "Deactivated", active: 0 });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    expect(res.status, await res.clone().text()).toBe(200);
    const { lines, jobs_with_materials } = (await res.json()) as {
      lines: LineRow[];
      jobs_with_materials: RosterRow[];
    };
    expect(lines).toHaveLength(0); // zero ACTIVE lines
    expect(jobs_with_materials.map((r) => r.job_id)).toContain("JOB-ACTIVE");
    expect(jobs_with_materials.find((r) => r.job_id === "JOB-ACTIVE")?.project_name).toBe("Active Job");
  });

  it("jobs_with_materials is DISTINCT and excludes jobs on non-active jobs / with no lines", async () => {
    await seedJob("JOB-WITH", { projectName: "Has Materials", status: "active" });
    await seedJob("JOB-NONE", { projectName: "No Materials", status: "active" });
    await seedJob("JOB-CLOSED", { projectName: "Closed", status: "closed" });
    // Two lines on the SAME active job → the roster must list it ONCE (DISTINCT).
    await seedLine("JOB-WITH", { description: "m1", seq: 10 });
    await seedLine("JOB-WITH", { description: "m2", seq: 20 });
    await seedLine("JOB-CLOSED", { description: "on closed" });

    const res = await call(PATH, { bearer: FIELDOPS_BEARER });
    const { jobs_with_materials } = (await res.json()) as { jobs_with_materials: RosterRow[] };
    expect(jobs_with_materials.map((r) => r.job_id)).toEqual(["JOB-WITH"]); // distinct, no NONE/CLOSED
  });
});
