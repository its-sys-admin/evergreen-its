import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, seedJob, seedPersonnel } from "./helpers";

// Material Receipts up-sync (PR4) — the field-ops delivery-ledger route
// (GET /api/internal/fieldops/material-receipts). An APPEND-ONLY EVENT LEDGER (not a snapshot): one
// row per `material_receipt_events` mark on an ACTIVE job. Immutable field events — the daemon never
// retires a row. Same field-ops token privilege separation as the sibling mirror queues.
//
// Unlike the incidents route this reads a real TABLE, and it carries two DERIVED columns the mirror
// shows: `line_status` and `line_qty_received` (the ROLLUP across every event for that line,
// recomputed here rather than stored so it cannot drift from the events it summarizes). Those two are
// what the tests below actually pin, because they are the only values that can be silently wrong.

const FIELDOPS_BEARER = "test-fieldops-token"; // == PORTAL_FIELDOPS_API_TOKEN (vitest.config.ts)
const INTERNAL_BEARER = "test-internal-token"; // portal_poll's token — must be REJECTED here
const ADMIN_BEARER = "test-admin-token"; // operator token — must be REJECTED here

const PATH = "/api/internal/fieldops/material-receipts";

interface ReceiptRow {
  event_uuid: string;
  job_id: string;
  project_name: string;
  kind: string;
  qty: number | null;
  note: string | null;
  event_date: string | null;
  line_uuid: string | null;
  material_description: string | null;
  unit: string | null;
  part_number: string | null;
  line_qty_expected: number | null;
  line_status: string | null;
  line_qty_received: number | null;
  bol_number: string | null;
  received_by_display: string | null;
}

async function readReceipts(bearer = FIELDOPS_BEARER): Promise<Response> {
  return call(PATH, { bearer });
}

async function rows(): Promise<ReceiptRow[]> {
  const res = await readReceipts();
  expect(res.status, await res.clone().text()).toBe(200);
  return ((await res.json()) as { receipts: ReceiptRow[] }).receipts;
}

/** Create an expected-materials line and return its row id. */
async function seedLine(
  jobId: string,
  opts: { description?: string; qty?: number; unit?: string; part?: string; status?: string } = {},
): Promise<number> {
  const r = await env.DB
    .prepare(
      "INSERT INTO job_expected_materials (job_id, description, qty, unit, part_number, status, seq, line_uuid) " +
        "VALUES (?1,?2,?3,?4,?5,?6,10,?7) RETURNING id",
    )
    .bind(
      jobId, opts.description ?? "Concrete pile cap", opts.qty ?? 10, opts.unit ?? "EA",
      opts.part ?? "7006955", opts.status ?? "expected", `lu-${jobId}-${Math.random()}`,
    )
    .first<{ id: number }>();
  return r!.id;
}

async function seedEvent(
  jobId: string, lineId: number,
  opts: { kind?: string; qty?: number | null; note?: string; actor?: string; date?: string; shipmentId?: number | null } = {},
): Promise<string> {
  const uuid = `ev-${jobId}-${Math.random().toString(36).slice(2)}`;
  await env.DB
    .prepare(
      "INSERT INTO material_receipt_events (event_uuid, line_id, job_id, shipment_id, kind, qty, note, event_date, actor) " +
        "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
    )
    .bind(
      uuid, lineId, jobId, opts.shipmentId ?? null, opts.kind ?? "partial",
      opts.qty === undefined ? 4 : opts.qty, opts.note ?? "", opts.date ?? "2026-06-26",
      opts.actor ?? "pm.pat",
    )
    .run();
  return uuid;
}

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM material_receipt_events"),
    env.DB.prepare("DELETE FROM material_shipments"),
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
  await seedJob("JOB-A", { projectName: "Deep Lake" });
});

describe("material-receipts internal route", () => {
  it("is closed to every sibling bearer and to no bearer at all", async () => {
    expect((await call(PATH)).status).toBe(401);
    expect((await readReceipts(INTERNAL_BEARER)).status).toBe(401);
    expect((await readReceipts(ADMIN_BEARER)).status).toBe(401);
    expect((await readReceipts()).status).toBe(200);
  });

  it("returns one row per event, joined to its line", async () => {
    const line = await seedLine("JOB-A", { description: "Pile cap", qty: 10, unit: "EA", part: "7006955" });
    await seedEvent("JOB-A", line, { kind: "partial", qty: 4, note: "half the piles" });

    const [row] = await rows();
    expect(row).toMatchObject({
      job_id: "JOB-A", project_name: "Deep Lake", kind: "partial", qty: 4,
      material_description: "Pile cap", unit: "EA", part_number: "7006955",
      line_qty_expected: 10, line_status: "expected", note: "half the piles",
    });
    expect(row.line_uuid).toBeTruthy();
  });

  it("ROLLS UP qty_received across every event for the line — the derived value", async () => {
    // The rollup is recomputed here rather than stored, precisely so it cannot drift from the
    // events it summarizes. Three partials must sum; each row reports the SAME total, because the
    // total is a property of the line, not of the event.
    const line = await seedLine("JOB-A", { qty: 10 });
    await seedEvent("JOB-A", line, { kind: "partial", qty: 4 });
    await seedEvent("JOB-A", line, { kind: "partial", qty: 3 });
    await seedEvent("JOB-A", line, { kind: "delivered", qty: 3 });

    const got = await rows();
    expect(got).toHaveLength(3);
    expect(got.map((r) => r.line_qty_received)).toEqual([10, 10, 10]);
    expect(got.map((r) => r.qty)).toEqual([4, 3, 3]);
  });

  it("keeps two lines' rollups independent", async () => {
    const a = await seedLine("JOB-A", { description: "Line A" });
    const b = await seedLine("JOB-A", { description: "Line B" });
    await seedEvent("JOB-A", a, { qty: 4 });
    await seedEvent("JOB-A", b, { qty: 9 });

    const byLine = Object.fromEntries((await rows()).map((r) => [r.material_description, r.line_qty_received]));
    expect(byLine).toEqual({ "Line A": 4, "Line B": 9 });
  });

  it("carries the BOL only when the event names a shipment", async () => {
    const line = await seedLine("JOB-A");
    const ship = await env.DB
      .prepare(
        "INSERT INTO material_shipments (shipment_uuid, line_id, job_id, bol_number) VALUES (?1,?2,?3,?4) RETURNING id",
      )
      .bind(`sh-${Math.random()}`, line, "JOB-A", "LD0867264")
      .first<{ id: number }>();
    await seedEvent("JOB-A", line, { shipmentId: ship!.id });
    await seedEvent("JOB-A", line, { shipmentId: null });

    const bols = (await rows()).map((r) => r.bol_number);
    expect(bols).toContain("LD0867264");
    expect(bols).toContain(null);
  });

  it("reflects a line flipped to incident — the live resolution signal", async () => {
    // line_status is DERIVED and legitimately changes after the event is written. That is exactly
    // why the mirror's upsert is change-only rather than insert-once.
    const line = await seedLine("JOB-A", { status: "expected" });
    await seedEvent("JOB-A", line);
    expect((await rows())[0].line_status).toBe("expected");

    await env.DB.prepare("UPDATE job_expected_materials SET status='incident' WHERE id=?1").bind(line).run();
    expect((await rows())[0].line_status).toBe("incident");
  });

  it("resolves the actor to a DISPLAY NAME, never the username (Reflex §5)", async () => {
    await seedPersonnel("Pat Miller", "pm.pat");
    const line = await seedLine("JOB-A");
    await seedEvent("JOB-A", line, { actor: "pm.pat" });

    const res = await readReceipts();
    const text = await res.clone().text();
    expect(((await res.json()) as { receipts: ReceiptRow[] }).receipts[0].received_by_display).toBe("Pat Miller");
    expect(text, "the raw account username must never leave the Worker").not.toContain("pm.pat");
  });

  it("bounds the working set to ACTIVE jobs", async () => {
    const line = await seedLine("JOB-A");
    await seedEvent("JOB-A", line);
    expect(await rows()).toHaveLength(1);

    await env.DB.prepare("UPDATE jobs SET status='inactive' WHERE job_id='JOB-A'").run();
    expect(await rows(), "a closed job's receipts leave the queue — its sheet is archive-moved").toHaveLength(0);
  });

  it("returns not_delivered marks, which carry a note and no qty", async () => {
    const line = await seedLine("JOB-A");
    await seedEvent("JOB-A", line, { kind: "not_delivered", qty: null, note: "truck never arrived" });

    const [row] = await rows();
    expect(row).toMatchObject({ kind: "not_delivered", qty: null, note: "truck never arrived" });
    // A not_delivered mark contributes nothing to the rollup rather than zeroing it.
    expect(row.line_qty_received).toBeNull();
  });
});
