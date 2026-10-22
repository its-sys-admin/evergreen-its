import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, g, p, seedJob, seedPersonnel } from "./helpers";
import { LINE_DEACTIVATE_SQL, RECEIPT_VOID_SQL } from "../worker/fieldops_expected_materials";

// ─────────────────────────────────────────────────────────────────────────────
// Receipt-event VOID (migration 0080) — POST /api/fieldops/receipt-event/:id/void, the optional
// `supersedes_id` correction on /receipt + /receive, and the void-aware read rules every
// rollup surface shares (RECEIPT_ACTIVE_SQL / RECEIPT_ORDER_SQL / LINE_PROJECTION_SET_SQL).
//
//   • A mark is never deleted or edited. It is VOIDED (who / when / why) and then counts for
//     nothing: not in the list read's rollup, not in the stored line projection the §51
//     Material List mirror reads. The voided row itself stays in the read, annotated.
//   • Who may void = exactly who may mark (operator decision D1): cap.materials.receive +
//     manager|admin role (checked FIRST) + the per-job placement scope (admins bypass).
//   • A correction is a NEW mark naming the voided one (supersedes_id). It inherits the
//     original's ORDER position (decision D4), so a fix recorded weeks later sits where the
//     mistake sat instead of becoming "the latest thing the field told us".
//   • voided_by is stored as the account username but read as the personnel display name, else
//     the account's role label (decision D6). The username never leaves the Worker. A
//     subcontractor ('submitter') reading the list sees THAT a mark was voided, not who or why.
//   • D11: a line whose marks still count cannot be removed; marks already on a removed line
//     (pre-D11 data) can still be voided, and the list read returns those lines as
//     `removed_lines`. A removed line is void-only: no correction lands on it.
// All fixtures are synthetic. Runs against the REAL worker + Miniflare D1 (migrations
// auto-apply).
// ─────────────────────────────────────────────────────────────────────────────

const JOB = "JOB-T0001";
const OTHER_JOB = "JOB-T0002";

// Distinctive usernames so the never-leaks assertions cannot pass by accident.
const ADMIN_USER = "tvoid.admin";
const LEAD_USER = "tvoid.lead";
const OTHER_LEAD_USER = "tvoid.otherlead";
const SUB_USER = "tvoid.sub";
const LEAD_NAME = "Test Crew Lead";

let admin: string, lead: string, otherLead: string, submitter: string;

interface ListEvent {
  id: number;
  line_id: number;
  kind: string;
  qty: number | null;
  note: string | null;
  voided_at: number | null;
  voided_by_name: string | null;
  void_reason: string | null;
  supersedes_id: number | null;
  superseded_by_id: number | null;
}
interface ListLine {
  id: number;
  status: string;
  receipt_status: string | null;
  qty_received_total: number | null;
}
interface ListBody {
  expected_materials: ListLine[];
  removed_lines: ListLine[];
  receipt_events: ListEvent[];
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function lineRow(id: number): Promise<any> {
  return await env.DB.prepare("SELECT * FROM job_expected_materials WHERE id=?").bind(id).first();
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function eventRow(id: number): Promise<any> {
  return await env.DB.prepare("SELECT * FROM material_receipt_events WHERE id=?").bind(id).first();
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function audits(action: string): Promise<any[]> {
  return (await env.DB.prepare("SELECT * FROM audit_log WHERE action=? ORDER BY id ASC").bind(action).all())
    .results;
}
async function errOf(res: Response): Promise<string> {
  return ((await res.json()) as { error: string }).error;
}
async function createLine(jobId: string, description: string, qty = 10): Promise<number> {
  const res = await p(admin, "/api/fieldops/expected-material", { job_id: jobId, description, qty, unit: "EA" });
  expect(res.status, await res.clone().text()).toBe(201);
  return ((await res.json()) as { id: number }).id;
}
function mark(cookie: string, lineId: number, body: unknown): Promise<Response> {
  return p(cookie, `/api/fieldops/expected-material/${lineId}/receipt`, body);
}
/** A mark that must land; returns its event id. */
async function markOk(cookie: string, lineId: number, body: unknown): Promise<number> {
  const res = await mark(cookie, lineId, body);
  expect(res.status, await res.clone().text()).toBe(200);
  return ((await res.json()) as { event_id: number }).event_id;
}
function voidEvent(cookie: string, eventId: number | string, body: unknown): Promise<Response> {
  return p(cookie, `/api/fieldops/receipt-event/${eventId}/void`, body);
}
async function voidOk(cookie: string, eventId: number, reason = "Test void reason"): Promise<Response> {
  const res = await voidEvent(cookie, eventId, { reason });
  expect(res.status, await res.clone().text()).toBe(200);
  return res;
}
async function readList(cookie: string, jobId = JOB): Promise<{ body: ListBody; text: string }> {
  const res = await g(cookie, `/api/fieldops/expected-materials?job_id=${encodeURIComponent(jobId)}`);
  const text = await res.text();
  expect(res.status, text).toBe(200);
  return { body: JSON.parse(text) as ListBody, text };
}
async function listLine(lineId: number): Promise<ListLine> {
  const { body } = await readList(admin);
  const row = body.expected_materials.find((r) => r.id === lineId);
  expect(row).toBeDefined();
  return row!;
}
async function listEvents(lineId: number): Promise<ListEvent[]> {
  const { body } = await readList(admin);
  return body.receipt_events.filter((e) => e.line_id === lineId);
}

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM material_receipt_events"),
    env.DB.prepare("DELETE FROM material_shipments"),
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
  await provision(ADMIN_USER, "password123", "admin");
  await provision(LEAD_USER, "password123", "manager");
  await provision(OTHER_LEAD_USER, "password123", "manager");
  await provision(SUB_USER, "password123", "submitter");
  admin = await login(ADMIN_USER, "password123");
  lead = await login(LEAD_USER, "password123");
  otherLead = await login(OTHER_LEAD_USER, "password123");
  submitter = await login(SUB_USER, "password123");
  await seedJob(JOB);
  await seedJob(OTHER_JOB);
  // The crew lead is PLACED on JOB-T0001 (the ownership-scope anchor); the other lead is placed
  // on JOB-T0002. The admin deliberately has NO roster link (decision D6's role-label case).
  await seedPersonnel(LEAD_NAME, LEAD_USER, JOB);
  await seedPersonnel("Test Other Lead", OTHER_LEAD_USER, OTHER_JOB);
});

describe("A — double-mark, void one, every rollup is right again", () => {
  it("the list read's rollup AND the stored line projection drop the voided mark", async () => {
    const line = await createLine(JOB, "Test pile cap", 10);
    await markOk(lead, line, { kind: "delivered", qty: 10 });
    const dup = await markOk(lead, line, { kind: "delivered", qty: 10 }); // the double tap
    expect((await lineRow(line)).qty_received).toBe(20); // the over-count the feature exists to remove

    const res = await voidOk(lead, dup, "Double tap on the same load");
    expect(await res.json()).toEqual({ ok: true, event_id: dup, line_id: line, status: "received", qty_received: 10 });

    // The SPA list read (RECEIPT_ROLLUP_SQL).
    const l = await listLine(line);
    expect(l.receipt_status).toBe("delivered");
    expect(l.qty_received_total).toBe(10);
    // The STORED projection (what the §51 Material List mirror reads).
    const row = await lineRow(line);
    expect(row.qty_received).toBe(10);
    expect(row.status).toBe("received");

    // The voided mark is KEPT and annotated, never deleted.
    const ev = await eventRow(dup);
    expect(ev).not.toBeNull();
    expect(ev.voided_at).toBeGreaterThan(0);
    expect(ev.voided_by).toBe(LEAD_USER); // stored as the ACCOUNT (reads resolve display-only)
    expect(ev.void_reason).toBe("Double tap on the same load");
    const listed = (await listEvents(line)).find((e) => e.id === dup)!;
    expect(listed.voided_at).toBe(ev.voided_at);
    expect(listed.voided_by_name).toBe(LEAD_NAME);
    expect(listed.void_reason).toBe("Double tap on the same load");
  });

  it("voiding a line's only mark makes it read exactly like a never-marked line", async () => {
    const line = await createLine(JOB, "Test torque tube", 8);
    const only = await markOk(lead, line, { kind: "delivered", qty: 8 });
    await voidOk(admin, only);

    const l = await listLine(line);
    expect(l.receipt_status).toBeNull();
    expect(l.qty_received_total).toBeNull(); // NULL (nothing recorded), not 0
    const row = await lineRow(line);
    expect(row.status).toBe("expected");
    expect(row.qty_received).toBeNull();
    // No counting mark remains, so there is no receiver to show — and the VOIDER is never
    // stamped in as one.
    expect(row.received_at).toBeNull();
    expect(row.received_by).toBeNull();
  });
});

describe("B — a double void is one void", () => {
  it("second void → 409 already_voided; exactly one stamp; exactly one material_receipt_void audit row", async () => {
    const line = await createLine(JOB, "Test rebar bundle", 10);
    const e1 = await markOk(lead, line, { kind: "partial", qty: 4 });
    await voidOk(lead, e1, "Wrong line");
    const stamped = await eventRow(e1);

    const again = await voidEvent(admin, e1, { reason: "Second attempt" });
    expect(again.status).toBe(409);
    expect(await errOf(again)).toBe("already_voided");

    const after = await eventRow(e1);
    expect(after.voided_at).toBe(stamped.voided_at);
    expect(after.voided_by).toBe(LEAD_USER); // the FIRST voider, not overwritten
    expect(after.void_reason).toBe("Wrong line");

    const rows = await audits("material_receipt_void");
    expect(rows).toHaveLength(1);
    expect(rows[0].actor_username).toBe(LEAD_USER); // audit_log keeps the account (D6)
    expect(rows[0].target_username).toBe(JOB);
    expect(JSON.parse(rows[0].detail)).toEqual({
      event_id: e1,
      event_uuid: stamped.event_uuid,
      line_id: line,
      job_id: JOB,
      kind: "partial",
      qty: 4,
      reason: "Wrong line",
    });
  });

  // Concurrent requests SERIALIZE in the workerd test pool (six parallel voids with the
  // write-once guard deleted still produced exactly one 200), so a Promise.all "race" never
  // enters the window between the route's pre-check and its batch. The two tests below reach that
  // window deterministically instead: one runs the guarded write itself, the other injects the
  // competing write INSIDE the batch with a SQLite trigger.
  it("RECEIPT_VOID_SQL is write-once, re-asserts the active job, and reaches a REMOVED line (storage level)", async () => {
    const line = await createLine(JOB, "Test guard line", 10);
    const e1 = await markOk(lead, line, { kind: "partial", qty: 4 });
    const first = await env.DB.prepare(RECEIPT_VOID_SQL).bind(e1, LEAD_USER, "first").run();
    expect(first.meta.changes).toBe(1);
    const stamped = await eventRow(e1);
    const second = await env.DB.prepare(RECEIPT_VOID_SQL).bind(e1, ADMIN_USER, "second").run();
    expect(second.meta.changes).toBe(0);
    const after = await eventRow(e1);
    expect(after.voided_by).toBe(LEAD_USER);
    expect(after.void_reason).toBe("first");
    expect(after.voided_at).toBe(stamped.voided_at);

    // A job closed inside the race window: the write lands nowhere.
    const e2 = await markOk(lead, line, { kind: "partial", qty: 1 });
    await env.DB.prepare("UPDATE jobs SET status='closed' WHERE job_id=?1").bind(JOB).run();
    expect((await env.DB.prepare(RECEIPT_VOID_SQL).bind(e2, LEAD_USER, "x").run()).meta.changes).toBe(0);
    expect((await eventRow(e2)).voided_at).toBeNull();
    // A REMOVED line is no barrier (D11/W2): its counting marks must stay voidable, or a
    // removal that buried a double-count could never be undone.
    await env.DB.prepare("UPDATE jobs SET status='active' WHERE job_id=?1").bind(JOB).run();
    await env.DB.prepare("UPDATE job_expected_materials SET active=0 WHERE id=?1").bind(line).run();
    expect((await env.DB.prepare(RECEIPT_VOID_SQL).bind(e2, LEAD_USER, "x").run()).meta.changes).toBe(1);
    expect((await eventRow(e2)).voided_at).not.toBeNull();
  });

  it("a void whose guarded write lands nothing writes no audit row and leaves the line untouched", async () => {
    const line = await createLine(JOB, "Test lost-race line", 10);
    await markOk(lead, line, { kind: "delivered", qty: 5 });
    const dup = await markOk(lead, line, { kind: "delivered", qty: 5 });
    // Sentinel stamp: if the projection ran despite the void writing nothing, received_* would
    // be recomputed to the lead's mark and the sentinel would vanish.
    await env.DB.prepare(
      "UPDATE job_expected_materials SET received_at = 1700000000, received_by = ?2 WHERE id = ?1",
    ).bind(line, OTHER_LEAD_USER).run();
    // The lost race, simulated: the route's guarded UPDATE matches the row and is then dropped
    // (RAISE(IGNORE) skips it), exactly as if the guard had refused it. The trigger itself
    // writes NOTHING on purpose: D1's meta.changes counts trigger-made writes, so a trigger that
    // performed the competing void would mask the no-op the route must detect. Every pre-check
    // still holds on the re-read, so the route reports its fallback, job_not_active.
    await env.DB.prepare(
      `CREATE TRIGGER test_lost_void_race BEFORE UPDATE OF voided_at ON material_receipt_events
       WHEN OLD.voided_at IS NULL
       BEGIN
         SELECT RAISE(IGNORE);
       END`,
    ).run();
    try {
      const res = await voidEvent(admin, dup, { reason: "lost the race" });
      expect(res.status, await res.clone().text()).toBe(409);
      expect(await errOf(res)).toBe("job_not_active");
    } finally {
      await env.DB.prepare("DROP TRIGGER IF EXISTS test_lost_void_race").run();
    }
    expect((await eventRow(dup)).voided_at).toBeNull();
    expect(await audits("material_receipt_void")).toHaveLength(0);
    const row = await lineRow(line);
    expect(row.received_by).toBe(OTHER_LEAD_USER);
    expect(row.received_at).toBe(1700000000);
    expect(row.qty_received).toBe(10);
  });
});

describe("D — a correction links back to the mark it replaces", () => {
  it("supersedes_id / superseded_by_id in the read; sort_id inherits the original's position", async () => {
    const line = await createLine(JOB, "Test module pallet", 10);
    const wrong = await markOk(lead, line, { kind: "partial", qty: 4 });
    await voidOk(lead, wrong, "Qty typo — was 6");
    const fix = await markOk(lead, line, { kind: "partial", qty: 6, supersedes_id: wrong });

    const evs = await listEvents(line);
    const fixRow = evs.find((e) => e.id === fix)!;
    const wrongRow = evs.find((e) => e.id === wrong)!;
    expect(fixRow.supersedes_id).toBe(wrong);
    expect(fixRow.superseded_by_id).toBeNull();
    expect(wrongRow.supersedes_id).toBeNull();
    expect(wrongRow.superseded_by_id).toBe(fix);

    expect((await eventRow(fix)).sort_id).toBe(wrong);
    expect((await lineRow(line)).qty_received).toBe(6);
    const receipts = await audits("expected_material_receipt");
    expect(JSON.parse(receipts[receipts.length - 1].detail).supersedes_id).toBe(wrong);
  });

  it("the legacy /receive alias accepts supersedes_id too", async () => {
    const line = await createLine(JOB, "Test bolt kit", 5);
    const recvRes = await p(lead, `/api/fieldops/expected-material/${line}/receive`, { qty_received: 2 });
    expect(recvRes.status).toBe(200);
    const wrong = ((await recvRes.json()) as { event_id: number }).event_id;
    await voidOk(lead, wrong);
    const fixRes = await p(lead, `/api/fieldops/expected-material/${line}/receive`, { qty_received: 5, supersedes_id: wrong });
    expect(fixRes.status, await fixRes.clone().text()).toBe(200);
    const fix = ((await fixRes.json()) as { event_id: number }).event_id;
    expect((await eventRow(fix)).supersedes_id).toBe(wrong);
    expect((await lineRow(line)).qty_received).toBe(5);
  });
});

describe("E — D4: a correction recorded WEEKS later keeps the original's position", () => {
  it("partial(wrong) → delivered → void → correction weeks later → line stays received", async () => {
    const line = await createLine(JOB, "Test tracker post", 20);
    const wrongPartial = await markOk(lead, line, { kind: "partial", qty: 5 }); // should have been 10
    const delivered = await markOk(lead, line, { kind: "delivered", qty: 10 });
    await voidOk(lead, wrongPartial, "Partial qty was 10, not 5");

    // "Weeks later": push every existing timestamp three weeks into the past, and let other
    // marks land on another line in between so the correction's id is far from the original's.
    const weeks = 21 * 86_400;
    await env.DB.prepare(
      "UPDATE material_receipt_events SET created_at = created_at - ?1, voided_at = voided_at - ?1",
    ).bind(weeks).run();
    const busy = await createLine(JOB, "Test busy line", 100);
    for (let i = 0; i < 3; i++) await markOk(lead, busy, { kind: "partial", qty: 1 });

    const fixRes = await mark(lead, line, { kind: "partial", qty: 10, supersedes_id: wrongPartial });
    expect(fixRes.status, await fixRes.clone().text()).toBe(200);
    const fix = ((await fixRes.json()) as { event_id: number; status: string });
    // Without position inheritance the correction (highest id, kind partial) would be "latest"
    // and flip the line back to expected/partial.
    expect(fix.status).toBe("received");
    const row = await lineRow(line);
    expect(row.status).toBe("received");
    expect(row.qty_received).toBe(20);
    const l = await listLine(line);
    expect(l.receipt_status).toBe("delivered");
    expect(l.qty_received_total).toBe(20);

    // Newest-first by ORDER KEY: the delivered mark, then the correction in the slot the
    // mistake held, then the voided mistake itself (same key, lower id).
    expect((await listEvents(line)).map((e) => e.id)).toEqual([delivered, fix.event_id, wrongPartial]);
  });

  it("chain: void a correction and correct it again — still the original position", async () => {
    const line = await createLine(JOB, "Test cable reel", 20);
    const orig = await markOk(lead, line, { kind: "partial", qty: 3 });
    const delivered = await markOk(lead, line, { kind: "delivered", qty: 10 });
    await voidOk(lead, orig);
    const fix1 = await markOk(lead, line, { kind: "partial", qty: 4, supersedes_id: orig });
    await voidOk(lead, fix1, "Still wrong");
    const fix2 = await markOk(lead, line, { kind: "partial", qty: 10, supersedes_id: fix1 });

    expect((await eventRow(fix1)).sort_id).toBe(orig);
    expect((await eventRow(fix2)).sort_id).toBe(orig); // COALESCE(fix1.sort_id, fix1.id) = orig
    expect((await lineRow(line)).status).toBe("received");
    expect((await lineRow(line)).qty_received).toBe(20);
    const evs = await listEvents(line);
    expect(evs.map((e) => e.id)).toEqual([delivered, fix2, fix1, orig]);
    expect(evs.find((e) => e.id === orig)!.superseded_by_id).toBe(fix1);
    expect(evs.find((e) => e.id === fix1)!.superseded_by_id).toBe(fix2);
  });
});

describe("void — gates (decision D1: exactly who may mark)", () => {
  it("anon 401; submitter 403 forbidden_role (learns nothing, even for a missing id)", async () => {
    const line = await createLine(JOB, "Test gate line");
    const e1 = await markOk(lead, line, { kind: "delivered", qty: 1 });
    expect((await call(`/api/fieldops/receipt-event/${e1}/void`, { method: "POST", body: "{}" })).status).toBe(401);
    const sub = await voidEvent(submitter, e1, { reason: "x" });
    expect(sub.status).toBe(403);
    expect(await errOf(sub)).toBe("forbidden_role");
    const subMissing = await voidEvent(submitter, 999999, { reason: "x" });
    expect(subMissing.status).toBe(403);
    expect(await errOf(subMissing)).toBe("forbidden_role");
    expect((await eventRow(e1)).voided_at).toBeNull();
  });

  it("manager placed on ANOTHER job → 403 forbidden_job; own-job manager 200; admin any job 200", async () => {
    const line = await createLine(JOB, "Test scope line");
    const e1 = await markOk(lead, line, { kind: "partial", qty: 1 });
    const e2 = await markOk(lead, line, { kind: "partial", qty: 1 });
    const cross = await voidEvent(otherLead, e1, { reason: "not my job" });
    expect(cross.status).toBe(403);
    expect(await errOf(cross)).toBe("forbidden_job");
    expect((await eventRow(e1)).voided_at).toBeNull();

    await voidOk(lead, e1); // own placement — and any eligible person may void anyone's mark
    // The admin is placed nowhere and voids on a job that is not theirs.
    const otherLine = await createLine(OTHER_JOB, "Test other-job line");
    const e3 = await markOk(otherLead, otherLine, { kind: "partial", qty: 2 });
    await voidOk(admin, e3);
    await voidOk(admin, e2);
    expect(await audits("material_receipt_void")).toHaveLength(3);
  });

  it("invalid id → 400; unreadable body → 400 bad_request; missing event → 404", async () => {
    expect(await errOf(await voidEvent(lead, "abc", { reason: "x" }))).toBe("invalid_id");
    const bad = await call("/api/fieldops/receipt-event/1/void", { method: "POST", cookie: lead, body: "not json" });
    expect(bad.status).toBe(400);
    expect(await errOf(bad)).toBe("bad_request");
    const arr = await voidEvent(lead, 1, ["reason"]);
    expect(arr.status).toBe(400);
    expect(await errOf(arr)).toBe("bad_request");
    const missing = await voidEvent(lead, 999999, { reason: "x" });
    expect(missing.status).toBe(404);
    expect(await errOf(missing)).toBe("not_found");
  });

  it("reason: missing/blank → 400 reason_required; non-string or > 500 → 400 invalid_reason; 500 is fine", async () => {
    const line = await createLine(JOB, "Test reason line");
    const e1 = await markOk(lead, line, { kind: "partial", qty: 1 });
    for (const body of [{}, { reason: null }, { reason: "" }, { reason: "   " }]) {
      const res = await voidEvent(lead, e1, body);
      expect(res.status, JSON.stringify(body)).toBe(400);
      expect(await errOf(res)).toBe("reason_required");
    }
    for (const body of [{ reason: 42 }, { reason: ["x"] }, { reason: "r".repeat(501) }]) {
      const res = await voidEvent(lead, e1, body);
      expect(res.status, JSON.stringify(body).slice(0, 40)).toBe(400);
      expect(await errOf(res)).toBe("invalid_reason");
    }
    expect((await eventRow(e1)).voided_at).toBeNull();
    expect(await audits("material_receipt_void")).toHaveLength(0);

    await voidOk(lead, e1, `  ${"r".repeat(500)}  `);
    expect((await eventRow(e1)).void_reason).toBe("r".repeat(500)); // stored trimmed
  });

  it("a job that is not active → 409 job_not_active, nothing written", async () => {
    const line = await createLine(JOB, "Test closed-job line");
    const e1 = await markOk(lead, line, { kind: "delivered", qty: 3 });
    await env.DB.prepare("UPDATE jobs SET status='closed' WHERE job_id=?1").bind(JOB).run();
    const res = await voidEvent(admin, e1, { reason: "too late" });
    expect(res.status).toBe(409);
    expect(await errOf(res)).toBe("job_not_active");
    expect((await eventRow(e1)).voided_at).toBeNull();
    expect((await lineRow(line)).qty_received).toBe(3);
    expect(await audits("material_receipt_void")).toHaveLength(0);
  });

});

// ── D11 (operator decision 2026-10-21) ──────────────────────────────────────────────────────────
// Removing a line used to bury its marks: the line vanished from every read while its deliveries
// kept counting on the Receipts sheet, and nothing could void them (the void route resolved
// through the ACTIVE line only). Now a line whose marks still count cannot be removed, and the
// marks already stranded on removed lines (pre-D11 data — production holds some) stay voidable.
// That stranded state can no longer be made through the routes, so these tests make it at
// storage, the way production holds it.

/** A removed line carrying counting marks, as pre-D11 removals left it. */
async function strandedLine(jobId: string, cookie: string, description: string, qtys: number[]): Promise<{ line: number; events: number[] }> {
  const line = await createLine(jobId, description, 10);
  const events: number[] = [];
  for (const qty of qtys) events.push(await markOk(cookie, line, { kind: "delivered", qty }));
  await env.DB.prepare("UPDATE job_expected_materials SET active = 0 WHERE id = ?1").bind(line).run();
  return { line, events };
}

describe("D11 — a line whose marks still count cannot be removed (W1)", () => {
  it("delete → 409 line_has_deliveries: nothing deactivated, no audit row, the loads not cascaded", async () => {
    const line = await createLine(JOB, "Test delivered line", 10);
    const ship = await p(admin, "/api/fieldops/material-shipment", { line_id: line, bol_number: "BOL-T0001", qty: 10 });
    expect(ship.status, await ship.clone().text()).toBe(201);
    await markOk(lead, line, { kind: "delivered", qty: 10 });

    const res = await p(admin, `/api/fieldops/expected-material/${line}/delete`);
    expect(res.status, await res.clone().text()).toBe(409);
    expect(await errOf(res)).toBe("line_has_deliveries");
    expect((await lineRow(line)).active).toBe(1);
    expect(await audits("expected_material_deactivate")).toHaveLength(0);
    expect(await audits("material_shipment_cascade_deactivate")).toHaveLength(0);
    const load = await env.DB.prepare("SELECT active FROM material_shipments WHERE line_id = ?1").bind(line).first<{ active: number }>();
    expect(load!.active).toBe(1);

    // EVERY counting mark blocks, not only a delivery: a "Not delivered" report is still a field
    // record that a removal would bury.
    const reported = await createLine(JOB, "Test not-delivered line", 10);
    await markOk(lead, reported, { kind: "not_delivered", note: "Truck did not arrive" });
    const blocked = await p(admin, `/api/fieldops/expected-material/${reported}/delete`);
    expect(blocked.status).toBe(409);
    expect(await errOf(blocked)).toBe("line_has_deliveries");
  });

  it("allowed once every mark is voided — and the load cascade then runs exactly as before", async () => {
    const line = await createLine(JOB, "Test fully voided line", 10);
    const ship = await p(admin, "/api/fieldops/material-shipment", { line_id: line, bol_number: "BOL-T0002", qty: 10 });
    expect(ship.status).toBe(201);
    const e1 = await markOk(lead, line, { kind: "delivered", qty: 10 });
    const e2 = await markOk(lead, line, { kind: "partial", qty: 2 });
    await voidOk(lead, e1);
    expect((await p(admin, `/api/fieldops/expected-material/${line}/delete`)).status).toBe(409); // e2 still counts
    await voidOk(lead, e2);

    const res = await p(admin, `/api/fieldops/expected-material/${line}/delete`);
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await res.json()).toEqual({ ok: true, id: line });
    expect((await lineRow(line)).active).toBe(0);
    expect(await audits("expected_material_deactivate")).toHaveLength(1);
    expect(await audits("material_shipment_cascade_deactivate")).toHaveLength(1);
    // The voided marks are kept — removal never deletes history.
    expect((await eventRow(e1)).voided_at).not.toBeNull();
  });

  it("a line ALREADY removed (pre-D11, marks still counting) answers 200 already_inactive, not 409", async () => {
    const { line } = await strandedLine(JOB, lead, "Test stranded line", [5]);
    const res = await p(admin, `/api/fieldops/expected-material/${line}/delete`);
    expect(res.status).toBe(200);
    expect(((await res.json()) as { already_inactive?: boolean }).already_inactive).toBe(true);
    expect(await audits("expected_material_deactivate")).toHaveLength(0);
  });

  // The route has no pre-check to race past: the rule lives ONLY in the deactivate UPDATE's WHERE,
  // so a mark that lands before the UPDATE runs is seen by it. Proven at storage level, the
  // RECEIPT_VOID_SQL way (requests serialize in the test pool, see the note above).
  it("LINE_DEACTIVATE_SQL re-asserts 'no counting marks' IN its WHERE (storage level)", async () => {
    const line = await createLine(JOB, "Test raced line", 10);
    const e1 = await markOk(lead, line, { kind: "partial", qty: 3 }); // the mark that won the race
    expect((await env.DB.prepare(LINE_DEACTIVATE_SQL).bind(line).run()).meta.changes).toBe(0);
    expect((await lineRow(line)).active).toBe(1);
    await env.DB.prepare(RECEIPT_VOID_SQL).bind(e1, LEAD_USER, "x").run();
    expect((await env.DB.prepare(LINE_DEACTIVATE_SQL).bind(line).run()).meta.changes).toBe(1);
    expect((await lineRow(line)).active).toBe(0);
  });
});

describe("D11 — marks on a REMOVED line can still be voided (W2)", () => {
  it("a manager placed on the job voids one, an admin voids another; a void never re-activates the line", async () => {
    const { line, events: [e1, e2] } = await strandedLine(JOB, lead, "Test stranded pair", [3, 3]);
    const res = await voidOk(lead, e1, "Duplicate left on a removed line");
    expect(await res.json()).toMatchObject({ ok: true, event_id: e1, line_id: line });
    expect((await eventRow(e1)).voided_by).toBe(LEAD_USER);
    await voidOk(admin, e2); // the admin is placed nowhere: the bypass reaches removed lines too
    expect(await audits("material_receipt_void")).toHaveLength(2);
    expect((await lineRow(line)).active).toBe(0);
    // Both marks gone from the rollup the "Removed from the list" group reads.
    const { body } = await readList(admin);
    const removed = body.removed_lines.find((r) => r.id === line)!;
    expect(removed.qty_received_total).toBeNull();
    expect(removed.receipt_status).toBeNull();
  });

  it("the job scope still binds (cross-job manager 403), and a closed job is still frozen (409)", async () => {
    const { events: [e1] } = await strandedLine(JOB, lead, "Test stranded scope line", [2]);
    const cross = await voidEvent(otherLead, e1, { reason: "not my job" });
    expect(cross.status).toBe(403);
    expect(await errOf(cross)).toBe("forbidden_job");
    await env.DB.prepare("UPDATE jobs SET status='closed' WHERE job_id=?1").bind(JOB).run();
    const closed = await voidEvent(admin, e1, { reason: "too late" });
    expect(closed.status).toBe(409);
    expect(await errOf(closed)).toBe("job_not_active");
    expect((await eventRow(e1)).voided_at).toBeNull();
    expect(await audits("material_receipt_void")).toHaveLength(0);
  });

  it("a removed line is void-only: a CORRECTION on it is still refused (the mark route's active-line rule)", async () => {
    const { line, events: [e1] } = await strandedLine(JOB, lead, "Test stranded correction line", [4]);
    await voidOk(lead, e1);
    const fix = await mark(lead, line, { kind: "partial", qty: 2, supersedes_id: e1 });
    expect(fix.status).toBe(404);
    expect(await errOf(fix)).toBe("not_found");
    expect((await listEvents(line)).filter((e) => e.supersedes_id === e1)).toHaveLength(0);
  });
});

describe("the list read's removed_lines (W3)", () => {
  it("returns REMOVED lines that carry ANY receipt event — rollup applied, same row shape, seq order", async () => {
    const active = await createLine(JOB, "Test active line", 10);
    await markOk(lead, active, { kind: "partial", qty: 2 });
    const { line: counting, events: [c1] } = await strandedLine(JOB, lead, "Test removed counting", [5]);
    const { line: voidedOnly, events: [v1] } = await strandedLine(JOB, lead, "Test removed voided-only", [3]);
    await voidOk(lead, v1); // a voided mark is still history: the line stays listed
    const bare = await createLine(JOB, "Test removed bare", 10);
    expect((await p(admin, `/api/fieldops/expected-material/${bare}/delete`)).status).toBe(200);
    await strandedLine(OTHER_JOB, otherLead, "Test other-job removed", [1]);
    // seq order, not id order: the voided-only line sorts first.
    await env.DB.prepare("UPDATE job_expected_materials SET seq = 20 WHERE id = ?1").bind(counting).run();
    await env.DB.prepare("UPDATE job_expected_materials SET seq = 10 WHERE id = ?1").bind(voidedOnly).run();

    const { body } = await readList(admin);
    expect(body.removed_lines.map((r) => r.id)).toEqual([voidedOnly, counting]);
    expect(body.expected_materials.map((r) => r.id)).toEqual([active]);
    const byId = Object.fromEntries(body.removed_lines.map((r) => [r.id, r]));
    expect(byId[counting].receipt_status).toBe("delivered");
    expect(byId[counting].qty_received_total).toBe(5);
    expect(byId[voidedOnly].receipt_status).toBeNull();
    expect(byId[voidedOnly].qty_received_total).toBeNull();
    // The SAME row shape as an active line, so the SPA renders both with one component.
    expect(Object.keys(byId[counting]).sort()).toEqual(Object.keys(body.expected_materials[0]).sort());
    // Their events already ride the job-scoped events read.
    expect(body.receipt_events.map((e) => e.id)).toEqual(expect.arrayContaining([c1, v1]));
  });

  it("is an empty array — present, not absent — when nothing was removed", async () => {
    await createLine(JOB, "Test lone active line", 10);
    const { body } = await readList(lead);
    expect(body.removed_lines).toEqual([]);
  });
});

describe("void — the received_at / received_by / note rule", () => {
  it("never stamps the VOIDER: received_* fall back to the latest COUNTING mark", async () => {
    const line = await createLine(JOB, "Test stamp line", 10);
    const e1 = await markOk(lead, line, { kind: "partial", qty: 4 });
    const e2 = await markOk(lead, line, { kind: "partial", qty: 4 });
    await env.DB.prepare("UPDATE material_receipt_events SET created_at = 1700000100 WHERE id = ?1").bind(e1).run();
    await voidOk(admin, e2);

    const row = await lineRow(line);
    expect(row.received_by).toBe(LEAD_USER); // e1's actor — NOT the admin who voided
    expect(row.received_at).toBe(1700000100); // e1's created_at
    expect(row.qty_received).toBe(4);
  });

  it("note: replaced only when it IS the voided mark's note — by the latest counting non-null note, else NULL", async () => {
    const line = await createLine(JOB, "Test note line", 10);
    const e1 = await markOk(lead, line, { kind: "partial", qty: 3, note: "first load" });
    const e2 = await markOk(lead, line, { kind: "partial", qty: 4, note: "second load" });
    expect((await lineRow(line)).note).toBe("second load");
    await voidOk(lead, e2);
    expect((await lineRow(line)).note).toBe("first load");
    await voidOk(lead, e1);
    expect((await lineRow(line)).note).toBeNull();

    // A line note that is NOT the voided mark's note is left alone.
    const other = await createLine(JOB, "Test kept-note line", 10);
    await markOk(lead, other, { kind: "partial", qty: 3, note: "kept note" });
    const unnoted = await markOk(lead, other, { kind: "partial", qty: 4 }); // COALESCE keeps "kept note"
    await voidOk(lead, unnoted);
    expect((await lineRow(other)).note).toBe("kept note");

    // ...and the discriminating case: a note no MARK wrote (the resolve route's) survives a void,
    // where "always take the latest counting note" would swap in the mark's "alpha".
    const resolved = await createLine(JOB, "Test resolved-note line", 10);
    await markOk(lead, resolved, { kind: "partial", qty: 3, note: "alpha" });
    expect((await p(lead, `/api/fieldops/expected-material/${resolved}/flag-incident`, { note: "scratched" })).status).toBe(200);
    expect((await p(lead, `/api/fieldops/expected-material/${resolved}/resolve-incident`, { note: "office waived" })).status).toBe(200);
    const late = await markOk(lead, resolved, { kind: "partial", qty: 1 });
    await voidOk(lead, late);
    expect((await lineRow(resolved)).note).toBe("office waived");
  });

  it("an incident-flagged line stays 'incident' and keeps its received_* stamp; qty still tracks", async () => {
    const line = await createLine(JOB, "Test flagged line", 10);
    // flag-incident only fires from 'expected', so the flag comes first; the later deliveries are
    // accepted and the sticky rule keeps the line at 'incident'.
    const flag = await p(lead, `/api/fieldops/expected-material/${line}/flag-incident`, { note: "crate crushed" });
    expect(flag.status).toBe(200);
    await markOk(lead, line, { kind: "delivered", qty: 5, note: "first truck" });
    const e2 = await markOk(lead, line, { kind: "delivered", qty: 5, note: "second truck" });
    // Sentinel stamp so the assertion discriminates: on a NON-incident line the void would
    // rewrite received_* to the latest counting mark (the lead).
    await env.DB.prepare(
      "UPDATE job_expected_materials SET received_at = 1700000000, received_by = ?2 WHERE id = ?1",
    ).bind(line, OTHER_LEAD_USER).run();

    const res = await voidOk(admin, e2);
    expect(((await res.json()) as { status: string }).status).toBe("incident");
    const after = await lineRow(line);
    expect(after.status).toBe("incident");
    expect(after.received_at).toBe(1700000000);
    expect(after.received_by).toBe(OTHER_LEAD_USER);
    expect(after.qty_received).toBe(5); // the quantity still follows the ledger
    // W6: the note scrub applies on a flagged line too. The line's note IS the voided mark's, so
    // it falls back to the latest counting note — a withdrawn mark's words must not linger on
    // the Material List mirror just because the line is flagged.
    expect(after.note).toBe("first truck");
  });

  it("W6: on a flagged line the FLAG's own note survives a void (only the voided mark's note is scrubbed)", async () => {
    const line = await createLine(JOB, "Test flag-note line", 10);
    expect((await p(lead, `/api/fieldops/expected-material/${line}/flag-incident`, { note: "crate crushed" })).status).toBe(200);
    const e1 = await markOk(lead, line, { kind: "delivered", qty: 5 }); // un-noted: COALESCE keeps the flag's note
    expect((await lineRow(line)).note).toBe("crate crushed");
    await voidOk(admin, e1);
    expect((await lineRow(line)).note).toBe("crate crushed");
  });
});

describe("W5 — the mark projection stamps received_* / note only for the LATEST counting mark", () => {
  it("a back-positioned correction leaves received_at / received_by / note alone; an ordinary mark still stamps", async () => {
    const line = await createLine(JOB, "Test back-position line", 20);
    const wrong = await markOk(lead, line, { kind: "partial", qty: 5 });
    await markOk(lead, line, { kind: "delivered", qty: 10, note: "second truck" });
    await voidOk(lead, wrong, "Partial qty was 10");
    // Backdate the stamp the delivered mark left so a re-stamp is visible.
    await env.DB.prepare("UPDATE job_expected_materials SET received_at = 1700000000 WHERE id = ?1").bind(line).run();
    expect((await lineRow(line)).received_by).toBe(LEAD_USER);

    // The correction sits in the voided partial's slot, BEHIND the delivered mark — it is not
    // "what the field told us last", so it must not present itself as the line's last receipt.
    await markOk(admin, line, { kind: "partial", qty: 10, note: "corrected qty", supersedes_id: wrong });
    const after = await lineRow(line);
    expect(after.received_at).toBe(1700000000);
    expect(after.received_by).toBe(LEAD_USER);
    expect(after.note).toBe("second truck");
    expect(after.qty_received).toBe(20); // the ledger half of the projection still moves
    expect(after.status).toBe("received");

    // An ordinary mark is always the latest by order key, so it stamps exactly as before 0080.
    await markOk(admin, line, { kind: "partial", qty: 1, note: "third load" });
    const ordinary = await lineRow(line);
    expect(ordinary.received_by).toBe(ADMIN_USER);
    expect(ordinary.received_at).toBeGreaterThan(1700000000);
    expect(ordinary.note).toBe("third load");
  });

  it("a correction that IS the latest counting mark stamps like any mark", async () => {
    const line = await createLine(JOB, "Test latest-correction line", 10);
    const only = await markOk(lead, line, { kind: "partial", qty: 4, note: "typo" });
    await voidOk(lead, only);
    await markOk(admin, line, { kind: "partial", qty: 6, note: "fixed qty", supersedes_id: only });
    const row = await lineRow(line);
    expect(row.received_by).toBe(ADMIN_USER);
    expect(row.note).toBe("fixed qty");
  });

  it("a guarded INSERT that landed nothing stamps nothing (404, no audit, line untouched)", async () => {
    const line = await createLine(JOB, "Test lost-insert line", 10);
    await markOk(lead, line, { kind: "partial", qty: 2, note: "kept note" });
    await env.DB.prepare(
      "UPDATE job_expected_materials SET received_at = 1700000000, received_by = ?2 WHERE id = ?1",
    ).bind(line, OTHER_LEAD_USER).run();
    // The lost race, simulated the way the void race is: the INSERT is matched and then dropped,
    // exactly as if its in-WHERE guard had refused it (a line or load deactivated in between).
    await env.DB.prepare(
      `CREATE TRIGGER test_lost_mark_race BEFORE INSERT ON material_receipt_events
       BEGIN
         SELECT RAISE(IGNORE);
       END`,
    ).run();
    let res: Response;
    try {
      res = await mark(admin, line, { kind: "partial", qty: 3, note: "never landed" });
    } finally {
      await env.DB.prepare("DROP TRIGGER IF EXISTS test_lost_mark_race").run();
    }
    expect(res.status, await res.clone().text()).toBe(404);
    const row = await lineRow(line);
    expect(row.received_at).toBe(1700000000);
    expect(row.received_by).toBe(OTHER_LEAD_USER);
    expect(row.note).toBe("kept note");
    expect(row.qty_received).toBe(2);
    expect(await audits("expected_material_receipt")).toHaveLength(1);
  });
});

describe("W7 — a subcontractor sees THAT a mark was voided, not who or why", () => {
  it("submitter: void_reason + voided_by_name NULL (voided_at kept); manager + admin: both present", async () => {
    await seedPersonnel("Test Sub Crew", SUB_USER, JOB); // a placed subcontractor may READ the list
    const line = await createLine(JOB, "Test visibility line", 10);
    // Marked by the admin (no roster link, so no actor_name), voided by the lead: the lead's
    // display name can then reach the response ONLY as voided_by_name.
    const e1 = await markOk(admin, line, { kind: "partial", qty: 2 });
    await voidOk(lead, e1, "Distinctive void reason T0001");

    const sub = await readList(submitter);
    const subEv = sub.body.receipt_events.find((e) => e.id === e1)!;
    expect(subEv.voided_at).not.toBeNull();
    expect(subEv.voided_by_name).toBeNull();
    expect(subEv.void_reason).toBeNull();
    expect(sub.text).not.toContain("Distinctive void reason T0001");
    expect(sub.text).not.toContain(LEAD_NAME);

    for (const cookie of [lead, admin]) {
      const ev = (await readList(cookie)).body.receipt_events.find((e) => e.id === e1)!;
      expect(ev.voided_by_name).toBe(LEAD_NAME);
      expect(ev.void_reason).toBe("Distinctive void reason T0001");
    }
  });
});

describe("the mark + resolve-incident projections ignore voided marks", () => {
  it("a new mark recomputes qty/status from COUNTING marks only (LINE_PROJECTION_SET_SQL)", async () => {
    const line = await createLine(JOB, "Test recompute line", 10);
    const e1 = await markOk(lead, line, { kind: "delivered", qty: 10 });
    // Voided at the storage layer so this asserts the MARK route's projection on its own.
    await env.DB.prepare("UPDATE material_receipt_events SET voided_at = unixepoch(), voided_by = ?2, void_reason = 'x' WHERE id = ?1")
      .bind(e1, LEAD_USER).run();
    await markOk(lead, line, { kind: "partial", qty: 3 });
    const row = await lineRow(line);
    expect(row.qty_received).toBe(3);
    expect(row.status).toBe("expected");
  });

  it("resolve-incident reads the latest COUNTING mark (LINE_LATEST_ACTIVE_KIND_SQL)", async () => {
    const line = await createLine(JOB, "Test resolve line", 10);
    await markOk(lead, line, { kind: "delivered", qty: 10 });
    const e2 = await markOk(lead, line, { kind: "partial", qty: 2 });
    await env.DB.prepare("UPDATE material_receipt_events SET voided_at = unixepoch(), voided_by = ?2, void_reason = 'x' WHERE id = ?1")
      .bind(e2, LEAD_USER).run();
    expect((await p(lead, `/api/fieldops/expected-material/${line}/flag-incident`, { note: "scratched" })).status).toBe(200);
    const res = await p(lead, `/api/fieldops/expected-material/${line}/resolve-incident`, { note: "accepted as is" });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { status: string }).status).toBe("received");
  });
});

describe("correction (supersedes_id) — refusals", () => {
  it("non-integer → 400 invalid_supersedes_id", async () => {
    const line = await createLine(JOB, "Test shape line");
    for (const bad of ["7", 1.5, true, { id: 1 }]) {
      const res = await mark(lead, line, { kind: "partial", qty: 1, supersedes_id: bad });
      expect(res.status, JSON.stringify(bad)).toBe(400);
      expect(await errOf(res)).toBe("invalid_supersedes_id");
    }
    expect(await listEvents(line)).toHaveLength(0);
  });

  it("not voided → 409 supersedes_not_voided; another line or missing → 422 unknown_supersedes", async () => {
    const lineA = await createLine(JOB, "Test line A");
    const lineB = await createLine(JOB, "Test line B");
    const live = await markOk(lead, lineA, { kind: "partial", qty: 1 });
    const notVoided = await mark(lead, lineA, { kind: "partial", qty: 2, supersedes_id: live });
    expect(notVoided.status).toBe(409);
    expect(await errOf(notVoided)).toBe("supersedes_not_voided");

    const onB = await markOk(lead, lineB, { kind: "partial", qty: 1 });
    await voidOk(lead, onB);
    const otherLine = await mark(lead, lineA, { kind: "partial", qty: 2, supersedes_id: onB });
    expect(otherLine.status).toBe(422);
    expect(await errOf(otherLine)).toBe("unknown_supersedes");
    const missing = await mark(lead, lineA, { kind: "partial", qty: 2, supersedes_id: 999999 });
    expect(missing.status).toBe(422);
    expect(await errOf(missing)).toBe("unknown_supersedes");

    // Every refusal wrote nothing.
    expect(await listEvents(lineA)).toHaveLength(1);
    expect(await audits("expected_material_receipt")).toHaveLength(2);
  });

  it("a voided mark is corrected at most once → 409 already_corrected", async () => {
    const line = await createLine(JOB, "Test double-correct line");
    const wrong = await markOk(lead, line, { kind: "partial", qty: 1 });
    await voidOk(lead, wrong);
    await markOk(lead, line, { kind: "partial", qty: 2, supersedes_id: wrong });
    const twice = await mark(admin, line, { kind: "partial", qty: 3, supersedes_id: wrong });
    expect(twice.status).toBe(409);
    expect(await errOf(twice)).toBe("already_corrected");
    expect((await lineRow(line)).qty_received).toBe(2);
  });

  it("a correction that loses the race to the UNIQUE index is 409 already_corrected (never duplicate_event)", async () => {
    const line = await createLine(JOB, "Test race line");
    const wrong = await markOk(lead, line, { kind: "partial", qty: 1 });
    await voidOk(lead, wrong);
    // Requests serialize in the test pool (see the void race note above), so the competing
    // correction is injected INSIDE the batch: it lands first, and the route's own INSERT then
    // hits idx_material_receipt_events_supersedes — the path two truly concurrent corrections
    // take after both pass the pre-check.
    await env.DB.prepare(
      `CREATE TRIGGER test_lost_correction_race BEFORE INSERT ON material_receipt_events
       WHEN NEW.supersedes_id IS NOT NULL
       BEGIN
         INSERT INTO material_receipt_events (event_uuid, line_id, job_id, kind, qty, event_date, actor, supersedes_id)
         VALUES ('race-' || NEW.event_uuid, NEW.line_id, NEW.job_id, 'partial', 9, '2026-01-01', 'tvoid.otherlead', NEW.supersedes_id);
       END`,
    ).run();
    let res: Response;
    try {
      res = await mark(admin, line, { kind: "partial", qty: 3, supersedes_id: wrong });
    } finally {
      await env.DB.prepare("DROP TRIGGER IF EXISTS test_lost_correction_race").run();
    }
    expect(res.status, await res.clone().text()).toBe(409);
    expect(await errOf(res)).toBe("already_corrected");
    // The whole batch rolled back: neither correction, no audit row, no projection change.
    expect((await listEvents(line)).filter((e) => e.supersedes_id === wrong)).toHaveLength(0);
    expect(await audits("expected_material_receipt")).toHaveLength(1);
  });
});

describe("voided_by display (decision D6) — never a username", () => {
  it("an account with no roster link reads as its ROLE LABEL; the username never appears in any response", async () => {
    const line = await createLine(JOB, "Test display line");
    const e1 = await markOk(lead, line, { kind: "partial", qty: 1 });
    const e2 = await markOk(lead, line, { kind: "partial", qty: 1 });
    const texts: string[] = [];
    texts.push(await (await voidOk(admin, e1, "Admin void")).text()); // admin: no personnel link
    texts.push(await (await voidOk(lead, e2, "Lead void")).text());
    texts.push(await (await voidEvent(lead, e2, { reason: "again" })).text()); // 409 body
    const fix = await mark(lead, line, { kind: "partial", qty: 1, supersedes_id: e1 });
    texts.push(await fix.text());

    const { body, text } = await readList(admin);
    texts.push(text);
    texts.push((await readList(lead)).text);
    const adminLabel = (await env.DB.prepare("SELECT label FROM roles WHERE key='admin'").first<{ label: string }>())!.label;
    expect(adminLabel.length).toBeGreaterThan(0);
    const byId = Object.fromEntries(body.receipt_events.map((e) => [e.id, e]));
    expect(byId[e1].voided_by_name).toBe(adminLabel);
    expect(byId[e2].voided_by_name).toBe(LEAD_NAME);

    for (const t of texts) {
      for (const u of [ADMIN_USER, LEAD_USER]) expect(t, "the stored account username must never leave the Worker").not.toContain(u);
    }
  });
});
