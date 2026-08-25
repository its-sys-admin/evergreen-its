import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { call, provision, login, g, p, seedJob, seedPersonnel } from "./helpers";

// ─────────────────────────────────────────────────────────────────────────────
// Material receipts (M1) — job_expected_materials (migration 0031) + the expected-materials routes.
//   • Expectation CRUD is cap.materials.manage (admin + manager since migration 0079): add (catalog-pick validated against an
//     ACTIVE material_catalog row OR free-text with description REQUIRED) / edit (expected rows
//     only) / seq reorder / soft deactivate — every mutation + its audit in ONE batch (W4).
//   • The read + receive/flag-incident are cap.materials.receive with the PER-JOB ownership scope
//     (the /daily-form/status pattern): a non-admin only touches their OWN placement
//     (personnel.current_job === job_id, else 403 forbidden_job); cap.jobtracker.manage holders
//     query any job — and only that cap, since 2026-08-25.
//   • receive/flag-incident guard the transition IN-WHERE (status='expected'): repeat → 409
//     already_actioned with exactly ONE stamp + ONE audit row ever written.
//   • W9: received_by stores the account username but reads resolve DISPLAY NAME ONLY — an
//     unmatched account yields NULL and the raw username never appears in the response.
// Runs against the REAL worker with Miniflare D1 (migrations auto-apply); per-test isolation.
// ─────────────────────────────────────────────────────────────────────────────


// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function expRow(id: number): Promise<any> {
  return await env.DB.prepare("SELECT * FROM job_expected_materials WHERE id=?").bind(id).first();
}
async function audits(action: string): Promise<unknown[]> {
  return (await env.DB.prepare("SELECT * FROM audit_log WHERE action=?").bind(action).all()).results;
}
/** The line's receipt-event ledger (PR2, 0059), append order. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function events(lineId: number): Promise<any[]> {
  return (
    await env.DB.prepare("SELECT * FROM material_receipt_events WHERE line_id=? ORDER BY id ASC")
      .bind(lineId).all()
  ).results;
}
/** A seeded (0019) ACTIVE catalog id, for catalog-pick rows. */
async function seededCatalogId(): Promise<number> {
  const row = await env.DB.prepare("SELECT id FROM material_catalog WHERE model_id=?")
    .bind("Q.PEAK_DUO_XL-G11.3_BFG").first<{ id: number }>();
  expect(row).not.toBeNull();
  return row!.id;
}

interface WireRow {
  id: number;
  material_id: number | null;
  material_name: string | null;
  description: string | null;
  qty: number | null;
  unit: string | null;
  expected_date: string | null;
  status: string;
  received_at: number | null;
  received_by_name: string | null;
  qty_received: number | null;
  note: string | null;
  seq: number;
  line_uuid: string | null;
}
async function readList(cookie: string, jobId: string): Promise<WireRow[]> {
  const res = await g(cookie, `/api/fieldops/expected-materials?job_id=${encodeURIComponent(jobId)}`);
  expect(res.status, await res.clone().text()).toBe(200);
  return ((await res.json()) as { expected_materials: WireRow[] }).expected_materials;
}
/** Admin-created expectation on `jobId`; returns the new row id. */
async function createExp(cookie: string, jobId: string, fields: Record<string, unknown>): Promise<number> {
  const res = await p(cookie, "/api/fieldops/expected-material", { job_id: jobId, ...fields });
  expect(res.status, await res.clone().text()).toBe(201);
  return ((await res.json()) as { id: number }).id;
}

let admin: string, manager: string, submitter: string;

beforeEach(async () => {
  // Preserve the 0019 catalog seed; reset the mutable tables so every test starts clean.
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM personnel"),
    env.DB.prepare("DELETE FROM users"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
  await provision("admin.one", "password123", "admin");
  await provision("mgr.mo", "password123", "manager");
  await provision("sub.sam", "password123", "submitter");
  admin = await login("admin.one", "password123");
  manager = await login("mgr.mo", "password123");
  submitter = await login("sub.sam", "password123");
  await seedJob("JOB-A");
  await seedJob("JOB-B");
  // The default querying manager is PLACED on JOB-A (the ownership-scope anchor).
  await seedPersonnel("Mo Manager", "mgr.mo", "JOB-A");
});

describe("POST /api/fieldops/expected-material (create)", () => {
  it("gate: anon 401; submitter (no manage cap) 403; manager + admin 201 + row + ONE audit in the batch", async () => {
    const body = JSON.stringify({ job_id: "JOB-A", description: "Rebar bundles" });
    expect((await call("/api/fieldops/expected-material", { method: "POST", body })).status).toBe(401);
    expect((await p(submitter, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "x" })).status).toBe(403);
    // Migration 0079 granted `manager` cap.materials.manage, so a crew lead may author the list
    // for the job they are placed on. `submitter` above is the remaining negative case.
    expect((await p(manager, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "x" })).status).toBe(201);

    const id = await createExp(admin, "JOB-A", { description: "Rebar bundles", qty: 12, unit: "pallets", expected_date: "2026-07-10", seq: 10 });
    const row = await expRow(id);
    expect(row.job_id).toBe("JOB-A");
    expect(row.material_id).toBeNull();
    expect(row.description).toBe("Rebar bundles");
    expect(row.qty).toBe(12);
    expect(row.unit).toBe("pallets");
    expect(row.expected_date).toBe("2026-07-10");
    expect(row.status).toBe("expected");
    expect(row.active).toBe(1);
    expect(row.seq).toBe(10);
    // M2 (0039): the ADD-line INSERT mints a line_uuid (the mirror key) + defaults unplanned=0.
    expect(typeof row.line_uuid).toBe("string");
    expect(row.line_uuid.length).toBeGreaterThan(0);
    expect(row.unplanned).toBe(0);
    expect(await audits("expected_material_create")).toHaveLength(2); // W4: audit landed with each INSERT — the manager's and the admin's
  });

  it("validates material_id against an ACTIVE catalog row (unknown → 400; retired → 400; valid → 201)", async () => {
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", material_id: 999999 })).status).toBe(400);
    // Retire a fresh catalog type, then try to expect it — a retired type gains no NEW expectations.
    const catRes = await p(admin, "/api/fieldops/material", { model_id: "RETIRE-ME", category: "other" });
    const retiredId = ((await catRes.json()) as { id: number }).id;
    await p(admin, `/api/fieldops/material/${retiredId}/delete`);
    const res = await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", material_id: retiredId });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("invalid_material_id");

    const okId = await createExp(admin, "JOB-A", { material_id: await seededCatalogId() });
    expect((await expRow(okId)).material_id).toBe(await seededCatalogId());
    expect(await audits("expected_material_create")).toHaveLength(1);
  });

  it("bounds: free-text without description → 400 description_required; oversize/invalid fields → 400; bad job → 404/400", async () => {
    const res = await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A" });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("description_required");
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "x".repeat(257) })).status).toBe(400);
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "x", qty: -3 })).status).toBe(400);
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "x", qty: "lots" })).status).toBe(400);
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "x", unit: "u".repeat(33) })).status).toBe(400);
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "x", expected_date: "July 10" })).status).toBe(400);
    // (review NIT) the note bound on the action routes: an oversize note → 400 invalid_note.
    const created = await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "bolts" });
    expect(created.status).toBe(201);
    const rowId = ((await created.json()) as { id: number }).id;
    const big = await p(manager, `/api/fieldops/expected-material/${rowId}/receive`, { qty_received: 1, note: "n".repeat(501) });
    expect(big.status).toBe(400);
    expect(((await big.json()) as { error: string }).error).toBe("invalid_note");
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-A", description: "x", seq: -1 })).status).toBe(400);
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "JOB-NOPE", description: "x" })).status).toBe(404);
    expect((await p(admin, "/api/fieldops/expected-material", { job_id: "x".repeat(65), description: "x" })).status).toBe(400);
  });
});

describe("GET /api/fieldops/expected-materials — display fields + per-job ownership scope", () => {
  it("returns active rows in seq order with the resolved catalog name (free-text rows: material_name null)", async () => {
    const catId = await seededCatalogId();
    const late = await createExp(admin, "JOB-A", { description: "Later item", seq: 20 });
    const first = await createExp(admin, "JOB-A", { material_id: catId, qty: 40, unit: "panels", seq: 10 });
    await createExp(admin, "JOB-B", { description: "Other job's row", seq: 5 }); // wrong job — excluded
    const gone = await createExp(admin, "JOB-A", { description: "Deactivated", seq: 30 });
    await p(admin, `/api/fieldops/expected-material/${gone}/delete`);

    const rows = await readList(manager, "JOB-A"); // manager placed on JOB-A
    expect(rows.map((r) => r.id)).toEqual([first, late]); // seq order; deactivated + wrong-job excluded
    expect(rows[0].material_name).toBe("Q.PEAK_DUO_XL-G11.3_BFG");
    expect(rows[0].qty).toBe(40);
    expect(rows[0].unit).toBe("panels");
    expect(rows[0].status).toBe("expected");
    expect(rows[1].material_id).toBeNull();
    expect(rows[1].material_name).toBeNull();
    expect(rows[1].description).toBe("Later item");
    // M3 Slice 1: the GET response carries the stable per-line key (the ADD-line INSERT mints it).
    expect(typeof rows[0].line_uuid).toBe("string");
    expect((rows[0].line_uuid ?? "").length).toBeGreaterThan(0);
  });

  it("scope: own-job manager 200; cross-job manager 403 forbidden_job; unplaced submitter 403; admin any job 200", async () => {
    await createExp(admin, "JOB-B", { description: "B row" });
    const cross = await g(manager, "/api/fieldops/expected-materials?job_id=JOB-B");
    expect(cross.status).toBe(403);
    expect(((await cross.json()) as { error: string }).error).toBe("forbidden_job");
    expect((await g(submitter, "/api/fieldops/expected-materials?job_id=JOB-A")).status).toBe(403); // no roster link
    expect((await g(manager, "/api/fieldops/expected-materials?job_id=JOB-A")).status).toBe(200);
    expect((await g(admin, "/api/fieldops/expected-materials?job_id=JOB-B")).status).toBe(200); // bypass caps
    expect((await call("/api/fieldops/expected-materials?job_id=JOB-A")).status).toBe(401);
    expect((await g(manager, "/api/fieldops/expected-materials?job_id=JOB-NOPE")).status).toBe(404);
    expect((await g(manager, `/api/fieldops/expected-materials?job_id=${"x".repeat(65)}`)).status).toBe(400);
  });

  it("W9: received_by_name is the personnel DISPLAY name; an unmatched account → NULL, raw username never leaks", async () => {
    const withLink = await createExp(admin, "JOB-A", { description: "Linked receiver" });
    const noLink = await createExp(admin, "JOB-A", { description: "Unlinked receiver", seq: 20 });
    await p(manager, `/api/fieldops/expected-material/${withLink}/receive`, { qty_received: 1 }); // mgr.mo → "Mo Manager"
    await p(admin, `/api/fieldops/expected-material/${noLink}/receive`, { qty_received: 1 }); // admin.one has NO personnel row

    const res = await g(admin, "/api/fieldops/expected-materials?job_id=JOB-A");
    const body = await res.text();
    const rows = (JSON.parse(body) as { expected_materials: WireRow[] }).expected_materials;
    expect(rows.find((r) => r.id === withLink)?.received_by_name).toBe("Mo Manager");
    expect(rows.find((r) => r.id === noLink)?.received_by_name).toBeNull();
    expect(body).not.toContain("admin.one"); // the stored account username never leaves the Worker
    expect(body).not.toContain("mgr.mo");
  });
});

describe("POST /api/fieldops/expected-material/:id/update (edit — expected rows only)", () => {
  it("admin + manager edit content fields (200 + audit); received row → 409 not_editable; unknown → 404; submitter → 403", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Old", qty: 1 });
    expect((await p(submitter, `/api/fieldops/expected-material/${id}/update`, { description: "New" })).status).toBe(403);
    // manager holds cap.materials.manage since migration 0079 — and is placed on JOB-A.
    expect((await p(manager, `/api/fieldops/expected-material/${id}/update`, { description: "New" })).status).toBe(200);
    expect((await p(admin, `/api/fieldops/expected-material/${id}/update`, { description: "New text", qty: 5, unit: "ft" })).status).toBe(200);
    const row = await expRow(id);
    expect(row.description).toBe("New text");
    expect(row.qty).toBe(5);
    expect(row.unit).toBe("ft");
    expect(await audits("expected_material_update")).toHaveLength(2); // manager's edit + admin's edit — both hold cap.materials.manage

    await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 1 });
    const locked = await p(admin, `/api/fieldops/expected-material/${id}/update`, { description: "Rewrite history" });
    expect(locked.status).toBe(409);
    expect(((await locked.json()) as { error: string }).error).toBe("not_editable");
    expect((await expRow(id)).description).toBe("New text"); // untouched
    expect(await audits("expected_material_update")).toHaveLength(2); // unchanged by the locked edit — no THIRD audit

    expect((await p(admin, "/api/fieldops/expected-material/999999/update", { description: "x" })).status).toBe(404);
    expect((await p(admin, `/api/fieldops/expected-material/${id}/update`, { material_id: 999999 })).status).toBe(400);
  });
});

describe("POST /api/fieldops/expected-material/:id/seq (reorder) + /:id/delete (deactivate)", () => {
  it("seq writes on any ACTIVE row incl. received (200 + audit); invalid seq → 400; unknown → 404", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Movable" });
    await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 1 }); // received rows still reorder
    expect((await p(admin, `/api/fieldops/expected-material/${id}/seq`, { seq: 30 })).status).toBe(200);
    expect((await expRow(id)).seq).toBe(30);
    expect(await audits("expected_material_reorder")).toHaveLength(1);
    expect((await p(admin, `/api/fieldops/expected-material/${id}/seq`, { seq: -1 })).status).toBe(400);
    expect((await p(admin, "/api/fieldops/expected-material/999999/seq", { seq: 10 })).status).toBe(404);
    // manager holds cap.materials.manage since migration 0079; submitter is the negative case.
    expect((await p(manager, `/api/fieldops/expected-material/${id}/seq`, { seq: 10 })).status).toBe(200);
    expect((await p(submitter, `/api/fieldops/expected-material/${id}/seq`, { seq: 10 })).status).toBe(403);
  });

  it("deactivate is a soft-delete: active=0, idempotent 200 already_inactive with ONE audit; unknown → 404", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Doomed" });
    expect((await p(submitter, `/api/fieldops/expected-material/${id}/delete`)).status).toBe(403);
    expect((await p(admin, `/api/fieldops/expected-material/${id}/delete`)).status).toBe(200);
    expect((await expRow(id)).active).toBe(0); // soft — the row (and any receipt history) is kept
    expect(await audits("expected_material_deactivate")).toHaveLength(1);

    const again = await p(admin, `/api/fieldops/expected-material/${id}/delete`);
    expect(again.status).toBe(200);
    expect(((await again.json()) as { already_inactive?: boolean }).already_inactive).toBe(true);
    expect(await audits("expected_material_deactivate")).toHaveLength(1); // no second audit

    expect((await p(admin, "/api/fieldops/expected-material/999999/delete")).status).toBe(404);
    expect(await readList(admin, "JOB-A")).toHaveLength(0); // read excludes deactivated
  });
});

describe("POST /api/fieldops/expected-material/:id/receive — guard-in-WHERE + scope", () => {
  it("own-job manager receives: status/received_at/received_by + optional qty_received+note; ONE audit", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Panels", qty: 40 });
    const res = await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 38, note: "two short, backordered" });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { status: string }).status).toBe("received");
    const row = await expRow(id);
    expect(row.status).toBe("received");
    expect(row.received_at).toBeGreaterThan(0);
    expect(row.received_by).toBe("mgr.mo"); // stored ACCOUNT username (reads resolve display-only)
    expect(row.qty_received).toBe(38);
    expect(row.note).toBe("two short, backordered");
    expect(await audits("expected_material_receipt")).toHaveLength(1);
    // PR2 — /receive is now a 'delivered' EVENT; the ledger is the delivery SoR and the columns
    // above are its coarse projection.
    const ev = await events(id);
    expect(ev).toHaveLength(1);
    expect(ev[0].kind).toBe("delivered");
    expect(ev[0].qty).toBe(38);
    expect(ev[0].actor).toBe("mgr.mo"); // stored ACCOUNT username; reads resolve display-only
  });

  // PR2 REPLACES the M1 "double-receive → 409 already_actioned" contract. That one-shot guard is
  // exactly what made a partial delivery impossible to complete: a part number that arrives across
  // several loads has to be markable more than once. Repeats are now legal and each appends an
  // event; `qty_received` is RECOMPUTED as the ledger sum, never incremented, so it cannot drift.
  it("receive is REPEATABLE: each call appends an event, qty_received is the recomputed sum", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Piles", qty: 100 });
    expect((await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 40 })).status).toBe(200);
    expect((await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 35, note: "second load" })).status).toBe(200);

    const ev = await events(id);
    expect(ev).toHaveLength(2);
    expect(ev.map((e) => e.qty)).toEqual([40, 35]);
    const row = await expRow(id);
    expect(row.qty_received).toBe(75); // SUM over the ledger, not the last mark
    expect(row.status).toBe("received");
    expect(row.note).toBe("second load");
    expect(await audits("expected_material_receipt")).toHaveLength(2); // one audit per event
  });

  it("the three-way mark: partial → partial → delivered rolls up over the ledger", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Beams", qty: 120 });
    const mark = (body: unknown) => p(manager, `/api/fieldops/expected-material/${id}/receipt`, body);

    expect((await mark({ kind: "partial", qty: 50, note: "first truck" })).status).toBe(200);
    // Partial leaves the coarse projection at 'expected' — the line is not complete.
    expect((await expRow(id)).status).toBe("expected");
    expect((await mark({ kind: "partial", qty: 40 })).status).toBe(200);
    expect((await expRow(id)).qty_received).toBe(90);
    expect((await mark({ kind: "delivered", qty: 30 })).status).toBe(200);

    const row = await expRow(id);
    expect(row.status).toBe("received");
    expect(row.qty_received).toBe(120);
    expect((await events(id)).map((e) => e.kind)).toEqual(["partial", "partial", "delivered"]);
  });

  it("not_delivered REQUIRES a note and REFUSES a qty (an incoherent mark, not a silent drop)", async () => {
    const id = await createExp(admin, "JOB-A", { description: "No-show" });
    const mark = (body: unknown) => p(manager, `/api/fieldops/expected-material/${id}/receipt`, body);

    expect((await mark({ kind: "not_delivered" })).status).toBe(400);
    expect(((await (await mark({ kind: "not_delivered" })).json()) as { error: string }).error).toBe("note_required");
    expect((await mark({ kind: "not_delivered", qty: 5, note: "truck never came" })).status).toBe(400);
    expect((await mark({ kind: "nope", note: "x" })).status).toBe(400);
    expect(((await (await mark({ kind: "nope", note: "x" })).json()) as { error: string }).error).toBe(
      "invalid_receipt_kind",
    );

    expect((await mark({ kind: "not_delivered", note: "truck never came" })).status).toBe(200);
    const row = await expRow(id);
    expect(row.status).toBe("expected"); // still outstanding — that IS what not-delivered means
    expect(row.qty_received).toBeNull(); // SUM over a qty-less event is NULL, not 0
    expect((await events(id))[0].kind).toBe("not_delivered");
  });

  it("delivered + partial REQUIRE a qty, and the legacy /receive alias is not a side door", async () => {
    // A mark that does not say HOW MUCH arrived cannot move the outstanding balance: the page's
    // "still owed" figure and the §51 Material List both derive from SUM(events.qty), so a
    // qty-NULL 'delivered' flips the line green while it goes on owing everything. Found live on
    // JOB-000032 (37 lines) — forensic report 2026-08-24, defect D2.
    const id = await createExp(admin, "JOB-A", { description: "Torque tube", qty: 27 });
    const mark = (body: unknown) => p(manager, `/api/fieldops/expected-material/${id}/receipt`, body);
    const err = async (body: unknown) => ((await (await mark(body)).json()) as { error: string }).error;

    expect((await mark({ kind: "delivered" })).status).toBe(400);
    expect(await err({ kind: "delivered" })).toBe("qty_required");
    expect(await err({ kind: "partial" })).toBe("qty_required");
    // An empty string is "not filled in", not a zero.
    expect(await err({ kind: "delivered", qty: "" })).toBe("qty_required");
    // A note is not a substitute for the number.
    expect(await err({ kind: "delivered", note: "all of it turned up" })).toBe("qty_required");
    // Zero / negative were already refused and still are — a DIFFERENT code, deliberately: one
    // means "you left it blank", the other means "that is not a quantity".
    expect(await err({ kind: "delivered", qty: 0 })).toBe("invalid_qty");

    // Every refusal wrote NOTHING — no event, no audit row, no projection change.
    expect(await events(id)).toHaveLength(0);
    expect((await expRow(id)).status).toBe("expected");
    expect(await audits("expected_material_receipt")).toHaveLength(0);

    // The legacy one-tap alias funnels through the SAME validator with forcedKind='delivered',
    // so it cannot be used to write the qty-less events the /receipt route now refuses.
    const bare = await p(manager, `/api/fieldops/expected-material/${id}/receive`);
    expect(bare.status).toBe(400);
    expect(((await bare.json()) as { error: string }).error).toBe("qty_required");
    expect(await events(id)).toHaveLength(0);

    // ...and it still accepts the number under its own legacy field name.
    expect((await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 27 })).status).toBe(200);
    expect((await expRow(id)).qty_received).toBe(27);
  });

  it("a shipment reference must belong to THIS line (400 invalid_shipment_id otherwise)", async () => {
    const idA = await createExp(admin, "JOB-A", { description: "Line A" });
    const idB = await createExp(admin, "JOB-A", { description: "Line B" });
    const ship = await p(admin, "/api/fieldops/material-shipment", { line_id: idA, bol_number: "LD0867264", qty: 12 });
    expect(ship.status).toBe(201);
    const shipId = ((await ship.json()) as { id: number }).id;

    // Against its own line: fine. Against a sibling line: refused before any write.
    expect((await p(manager, `/api/fieldops/expected-material/${idA}/receipt`, { kind: "delivered", qty: 12, shipment_id: shipId })).status).toBe(200);
    expect((await p(manager, `/api/fieldops/expected-material/${idB}/receipt`, { kind: "delivered", qty: 12, shipment_id: shipId })).status).toBe(400);
    expect(await events(idB)).toHaveLength(0);
    expect((await events(idA))[0].shipment_id).toBe(shipId);
  });

  it("scope: cross-job manager 403 forbidden_job; unplaced submitter 403; admin any job 200; empty body OK", async () => {
    const idB = await createExp(admin, "JOB-B", { description: "B delivery" });
    const cross = await p(manager, `/api/fieldops/expected-material/${idB}/receive`, { qty_received: 1 });
    expect(cross.status).toBe(403);
    expect(((await cross.json()) as { error: string }).error).toBe("forbidden_job");
    expect((await p(submitter, `/api/fieldops/expected-material/${idB}/receive`)).status).toBe(403);
    expect((await p(admin, `/api/fieldops/expected-material/${idB}/receive`, { qty_received: 1 })).status).toBe(200);

    const idA = await createExp(admin, "JOB-A", { description: "A delivery" });
    expect((await p(manager, `/api/fieldops/expected-material/${idA}/receive`, { qty_received: -1 })).status).toBe(400);
    expect((await call(`/api/fieldops/expected-material/${idA}/receive`, { method: "POST" })).status).toBe(401);
  });

  it("a deactivated row 404s (not 409) — it is gone from the flow, not already-actioned", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Removed first" });
    await p(admin, `/api/fieldops/expected-material/${id}/delete`);
    expect((await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 1 })).status).toBe(404);
  });
});

describe("POST /api/fieldops/expected-material/:id/flag-incident", () => {
  it("note is REQUIRED (400 note_required); with note → status incident + stamp + ONE audit; repeat → 409", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Damaged crate" });
    const bare = await p(manager, `/api/fieldops/expected-material/${id}/flag-incident`);
    expect(bare.status).toBe(400);
    expect(((await bare.json()) as { error: string }).error).toBe("note_required");
    expect((await p(manager, `/api/fieldops/expected-material/${id}/flag-incident`, { note: "   " })).status).toBe(400);

    const res = await p(manager, `/api/fieldops/expected-material/${id}/flag-incident`, { note: "arrived crushed", qty_received: 3 });
    expect(res.status).toBe(200);
    const row = await expRow(id);
    expect(row.status).toBe("incident");
    expect(row.note).toBe("arrived crushed");
    // DELIBERATELY REWRITTEN 2026-08-11 (audit B6): the flag used to write the client's
    // qty_received straight onto the row, clobbering the ledger-derived value — this test
    // asserted row.qty_received === 3 and the two §51 sheets then disagreed forever. The
    // ledger owns that column now: the flag body's number is audited but never reaches the
    // row, so with zero events it is still NULL here.
    expect(row.qty_received).toBeNull();
    expect(row.received_by).toBe("mgr.mo");
    expect(await audits("expected_material_incident")).toHaveLength(1);

    // flag-incident KEEPS its one-shot posture — an incident is a distinct axis with its own
    // ledger (M3), and re-flagging is not a workflow anyone asked for.
    expect((await p(manager, `/api/fieldops/expected-material/${id}/flag-incident`, { note: "again" })).status).toBe(409);
    expect(await audits("expected_material_incident")).toHaveLength(1);

    // PR2: a delivery mark on a flagged line is now ACCEPTED (the goods did arrive) and recorded
    // in the ledger — but `incident` is STICKY, so the coarse status keeps showing the problem.
    // Under M1 this was a 409, which meant a flagged line could never record its eventual receipt.
    const recv = await p(manager, `/api/fieldops/expected-material/${id}/receive`, { qty_received: 2 });
    expect(recv.status).toBe(200);
    // The RESPONSE BODY must say 'incident' too, not just the row. The daily form reads `status`
    // off this payload, so a hardcoded "received" here would show the crew a resolved delivery
    // while the §51 Material List mirror (reading the same column) still flags the problem —
    // the two surfaces silently disagreeing. Asserting only expRow() misses exactly that.
    expect(((await recv.json()) as { status: string }).status).toBe("incident");
    const after = await expRow(id);
    expect(after.status).toBe("incident"); // NOT flipped to received
    expect(await events(id)).toHaveLength(1);
    expect(await audits("expected_material_receipt")).toHaveLength(1);
    // B6, the other half: the projection now RUNS on a flagged line — Delivered Qty tracks
    // the ledger (2) while only STATUS stays sticky, and the un-noted mark COALESCEs so the
    // flag's note survives on the row instead of being blanked. Before 2026-08-11 the whole
    // statement was guarded status <> 'incident': this row would have frozen at the clobbered
    // 3 while the Receipts sheet summed 2 — permanent cross-sheet divergence.
    expect(after.qty_received).toBe(2);
    expect(after.note).toBe("arrived crushed");
  });

  it("resolve-incident clears the flag by EXPLICIT human act — status lands where the ledger says", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Crushed then replaced" });
    await p(manager, `/api/fieldops/expected-material/${id}/flag-incident`, { note: "arrived crushed" });

    // note is REQUIRED — the record of HOW it was resolved is the point.
    const bare = await p(manager, `/api/fieldops/expected-material/${id}/resolve-incident`);
    expect(bare.status).toBe(400);
    expect(((await bare.json()) as { error: string }).error).toBe("note_required");

    // No delivered event yet → resolution returns the line to EXPECTED (still owed).
    const res = await p(manager, `/api/fieldops/expected-material/${id}/resolve-incident`, {
      note: "vendor is reshipping",
    });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { status: string }).status).toBe("expected");
    expect((await expRow(id)).status).toBe("expected");
    expect(await audits("expected_material_incident_resolve")).toHaveLength(1);

    // A second resolve has nothing to clear.
    const again = await p(manager, `/api/fieldops/expected-material/${id}/resolve-incident`, { note: "?" });
    expect(again.status).toBe(409);
    expect(((await again.json()) as { error: string }).error).toBe("not_flagged");
  });

  it("resolve-incident lands on RECEIVED when the latest ledger event is delivered", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Damaged but delivered" });
    await p(manager, `/api/fieldops/expected-material/${id}/flag-incident`, { note: "corner crushed" });
    // The goods fully arrived AFTER the flag (sticky held it at incident)...
    await p(manager, `/api/fieldops/expected-material/${id}/receipt`, { kind: "delivered", qty: 40 });
    expect((await expRow(id)).status).toBe("incident");
    // ...so an explicit resolve lands where the ledger points: received.
    const res = await p(manager, `/api/fieldops/expected-material/${id}/resolve-incident`, {
      note: "damage waived by the office",
    });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { status: string }).status).toBe("received");
    const row = await expRow(id);
    expect(row.status).toBe("received");
    expect(row.qty_received).toBe(40); // ledger-derived, untouched by the resolve
  });

  it("a delivery mark NEVER resolves the flag — only the explicit route does", async () => {
    const id = await createExp(admin, "JOB-A", { description: "Still a problem" });
    await p(manager, `/api/fieldops/expected-material/${id}/flag-incident`, { note: "wrong finish" });
    await p(manager, `/api/fieldops/expected-material/${id}/receipt`, { kind: "delivered", qty: 10 });
    await p(manager, `/api/fieldops/expected-material/${id}/receipt`, { kind: "delivered", qty: 5 });
    const row = await expRow(id);
    expect(row.status).toBe("incident"); // two full deliveries later, the problem still shows
    expect(row.qty_received).toBe(15); // while the quantities kept tracking the ledger
  });

  it("deactivating a line CASCADES to its scheduled loads (audit B9)", async () => {
    const id = await createExp(admin, "JOB-A", { description: "With loads" });
    const ship = await p(admin, "/api/fieldops/material-shipment", {
      line_id: id, bol_number: "BOL-9", qty: 10,
    });
    expect(ship.status, await ship.clone().text()).toBe(201);
    const del = await p(admin, `/api/fieldops/expected-material/${id}/delete`);
    expect(del.status).toBe(200);
    const loads = await env.DB
      .prepare("SELECT active FROM material_shipments WHERE line_id = ?1")
      .bind(id)
      .all<{ active: number }>();
    expect(loads.results).toHaveLength(1);
    // Orphaned loads were invisible before: job-scoped read, line-bucketed render —
    // fetched, counted against the LIMIT, shown nowhere, removable only by purge-job.
    expect(loads.results[0].active).toBe(0);
  });

  it("scope holds for flag-incident too: cross-job manager → 403 forbidden_job", async () => {
    const idB = await createExp(admin, "JOB-B", { description: "Not yours" });
    expect((await p(manager, `/api/fieldops/expected-material/${idB}/flag-incident`, { note: "nope" })).status).toBe(403);
  });
});
