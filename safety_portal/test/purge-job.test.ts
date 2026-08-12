import { env, SELF } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";

// /api/internal/admin/purge-job — operator hard-delete of a job + ALL its D1 rows
// (submissions, the filed_pdfs cache, pdf_requests). Bearer-gated (requireAdminToken).

const BASE = "https://portal.test";
const ADMIN_BEARER = "test-admin-token"; // == PORTAL_ADMIN_API_TOKEN in vitest.config.ts
const TS = 1_780_000_000;

type Init = RequestInit & { bearer?: string };
function call(path: string, init: Init = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.bearer) headers.set("Authorization", `Bearer ${init.bearer}`);
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  return SELF.fetch(BASE + path, { ...init, headers });
}

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM job_payment_receipts"),
    env.DB.prepare("DELETE FROM job_payment_cycles"),
    env.DB.prepare("DELETE FROM job_payment_terms"),
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM filed_pdfs"),
    env.DB.prepare("DELETE FROM pdf_requests"),
    env.DB.prepare("DELETE FROM job_daily_requirements"),
    env.DB.prepare("DELETE FROM job_weekly_report_inputs"),
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM time_entries"),
    env.DB.prepare("DELETE FROM task_assignments"),
    env.DB.prepare("DELETE FROM inspections"),
    env.DB.prepare("DELETE FROM checklist_instances"),
    env.DB.prepare("DELETE FROM equipment_location"),
    env.DB.prepare("DELETE FROM job_manifest_chunks"),
    env.DB.prepare("DELETE FROM job_manifest_rows"),
    env.DB.prepare("DELETE FROM job_manifest_previews"),
    env.DB.prepare("DELETE FROM job_manifests"),
    env.DB.prepare("DELETE FROM job_schedule_chunks"),
    env.DB.prepare("DELETE FROM job_schedule_rows"),
    env.DB.prepare("DELETE FROM job_schedule_previews"),
    env.DB.prepare("DELETE FROM job_schedules"),
    env.DB.prepare("DELETE FROM job_schedule_tasks"),
    env.DB.prepare("DELETE FROM jobs"),
    env.DB.prepare("DELETE FROM audit_log"),
  ]);
});

async function seedJobWithData(job: string, uuid: string): Promise<void> {
  // equipment_location.equipment_id is a REAL foreign key (FKs are enforced in this
  // harness), so the referenced equipment row must exist first.
  await env.DB
    .prepare("INSERT OR IGNORE INTO equipment (id, name) VALUES (1, 'Crane 1')")
    .run();
  await env.DB.batch([
    env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES (?,?,1)").bind(job, "P"),
    env.DB
      .prepare(
        "INSERT INTO submissions (submission_uuid, job_id, form_code, work_date, payload_json, created_at, box_verified, filed_at) VALUES (?,?,?,?,?,?,1,?)",
      )
      .bind(uuid, job, "jha-v1", "2026-01-01", "{}", TS, TS),
    env.DB
      .prepare("INSERT INTO filed_pdfs (submission_uuid, chunk_index, chunk_total, chunk_b64) VALUES (?,?,?,?)")
      .bind(uuid, 0, 1, "QUJD"),
    env.DB
      .prepare("INSERT INTO pdf_requests (submission_uuid, account, requested_at) VALUES (?,?,?)")
      .bind(uuid, "pm", TS),
    // Slice 1 (R3-F4): the two per-job content tables join the cascade — 2 requirements +
    // 1 expected material give the response counts distinct values.
    env.DB
      .prepare("INSERT INTO job_daily_requirements (job_id, seq, kind, label) VALUES (?,10,'confirm','Client daily brief')")
      .bind(job),
    env.DB
      .prepare("INSERT INTO job_daily_requirements (job_id, seq, kind, label) VALUES (?,20,'text','Crane hours')")
      .bind(job),
    env.DB
      .prepare("INSERT INTO job_expected_materials (job_id, description, seq) VALUES (?, 'Panels pallet', 10)")
      .bind(job),
    // 0067: seven weeks of Weekly Production Report office inputs. SEVEN because every sibling
    // count in this seed is distinct (1/2/3/4/5/6) — a positional-index shift in the route's
    // results[] reads then cannot land on a value that looks correct.
    ...[1, 2, 3, 4, 5, 6, 7].map((w) =>
      env.DB
        .prepare("INSERT INTO job_weekly_report_inputs (job_id, week_start) VALUES (?,?)")
        .bind(job, `2026-0${w}-04`),
    ),
    // PR2 (0059): the materials children. Counts are DISTINCT from every sibling (2 shipments,
    // 3 events) so a mis-shifted positional index in purge-job's results[] cannot pass by
    // accidentally reading a neighbour's count — the exact failure mode that route warns about.
    env.DB
      .prepare("INSERT INTO material_shipments (shipment_uuid, line_id, job_id, bol_number) VALUES (?,1,?,'LD0867264')")
      .bind(`ship-a-${job}`, job),
    env.DB
      .prepare("INSERT INTO material_shipments (shipment_uuid, line_id, job_id, bol_number) VALUES (?,1,?,'LD0867268')")
      .bind(`ship-b-${job}`, job),
    env.DB
      .prepare("INSERT INTO material_receipt_events (event_uuid, line_id, job_id, kind, actor) VALUES (?,1,?,'partial','pm')")
      .bind(`ev-a-${job}`, job),
    env.DB
      .prepare("INSERT INTO material_receipt_events (event_uuid, line_id, job_id, kind, actor) VALUES (?,1,?,'partial','pm')")
      .bind(`ev-b-${job}`, job),
    env.DB
      .prepare("INSERT INTO material_receipt_events (event_uuid, line_id, job_id, kind, actor) VALUES (?,1,?,'delivered','pm')")
      .bind(`ev-c-${job}`, job),
    // The five job-context tables prune.ts guards a job on. purge-job's own comment
    // claimed it was "the explicit operator cleanup path (cascades both)" — it did
    // not touch any of them, so an operator purge returned ok:true while orphaning
    // payroll/billing-grade rows behind a now-absent job.
    env.DB
      .prepare("INSERT INTO time_entries (uuid, job_id, actor_username, hours) VALUES (?,?, 'pm', 8)")
      .bind(`te-${job}`, job),
    env.DB
      .prepare("INSERT INTO task_assignments (job_id, description) VALUES (?, 'Set panels')")
      .bind(job),
    env.DB
      .prepare(
        "INSERT INTO inspections (uuid, job_id, form_code, version, payload_json, actor_username) VALUES (?,?, 'insp-v1', 1, '{}', 'pm')",
      )
      .bind(`insp-${job}`, job),
    env.DB
      .prepare("INSERT INTO checklist_instances (kind, job_id, instance_date) VALUES ('daily', ?, '2026-01-01')")
      .bind(job),
    env.DB.prepare("INSERT INTO equipment_location (equipment_id, job_id) VALUES (1, ?)").bind(job),
  ]);
  // PR3b (0060): the manifest-import pool. Its three children key on manifest_id, not
  // job_id, so they must be seeded AFTER the parent exists. Counts are 1 / 4 / 6 / 5 —
  // every value distinct from each other and from every sibling in the batch, so a
  // one-position shift in purge-job's results[] reads cannot pass by reading a
  // neighbour's count. That positional hazard is what the route's own ⚠ comment warns
  // about, and inserting four DELETEs is exactly the change that triggers it.
  const mid = (
    await env.DB
      .prepare(
        "INSERT INTO job_manifests (manifest_uuid, job_id, filename, declared_mime, size_bytes, sha256, hmac, uploaded_by) " +
          "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
      )
      .bind(`man-${job}`, job, "Customer BOM.pdf", "application/pdf", 4096, `sha-${job}`, `mac-${job}`, "pm")
      .first<{ id: number }>()
  )!.id;
  await env.DB.batch([
    ...[0, 1, 2, 3].map((i) =>
      env.DB
        .prepare("INSERT INTO job_manifest_chunks (manifest_id, chunk_index, chunk_total, chunk_b64) VALUES (?,?,4,'QUJD')")
        .bind(mid, i),
    ),
    ...[1, 2, 3, 4, 5, 6].map((i) =>
      env.DB
        .prepare("INSERT INTO job_manifest_rows (manifest_id, row_index, kind, cells_json) VALUES (?,?, 'data', '[\"7006955\"]')")
        .bind(mid, i),
    ),
    ...[1, 2, 3, 4, 5].map((p) =>
      env.DB
        .prepare("INSERT INTO job_manifest_previews (manifest_id, page, png_b64) VALUES (?,?, 'QUJD')")
        .bind(mid, p),
    ),
  ]);
  // ADR-0006 (0066): the schedule-import pool joins the cascade the same way. Counts are
  // 2 / 3 / 7 / 8 — distinct from each other AND from the manifest pool's 1 / 4 / 6 / 5,
  // so the four newly-inserted DELETEs (which shift every later positional index in the
  // route's results[] reads by four) cannot mis-report by reading a neighbour's count.
  // Two parents (one superseded-style bare row) with distinct shas — the per-job partial
  // UNIQUE (job_id, sha256) forbids twins.
  const sid = (
    await env.DB
      .prepare(
        "INSERT INTO job_schedules (schedule_uuid, job_id, filename, declared_mime, size_bytes, sha256, hmac, uploaded_by) " +
          "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
      )
      .bind(`sch-${job}`, job, "Project Schedule.pdf", "application/pdf", 2048, `ssha-a-${job}`, `smac-${job}`, "pm")
      .first<{ id: number }>()
  )!.id;
  await env.DB.batch([
    env.DB
      .prepare(
        "INSERT INTO job_schedules (schedule_uuid, job_id, filename, declared_mime, size_bytes, sha256, hmac, uploaded_by) " +
          "VALUES (?,?,?,?,?,?,?,?)",
      )
      .bind(`sch2-${job}`, job, "Project Schedule rev2.pdf", "application/pdf", 2049, `ssha-b-${job}`, `smac2-${job}`, "pm"),
    ...[0, 1, 2].map((i) =>
      env.DB
        .prepare("INSERT INTO job_schedule_chunks (schedule_id, chunk_index, chunk_total, chunk_b64) VALUES (?,?,3,'QUJD')")
        .bind(sid, i),
    ),
    ...[1, 2, 3, 4, 5, 6, 7].map((i) =>
      env.DB
        .prepare("INSERT INTO job_schedule_rows (schedule_id, row_index, kind, cells_json) VALUES (?,?, 'data', '[\"Pile Installation\"]')")
        .bind(sid, i),
    ),
    ...[1, 2, 3, 4, 5, 6, 7, 8].map((p) =>
      env.DB
        .prepare("INSERT INTO job_schedule_previews (schedule_id, page, png_b64) VALUES (?,?, 'QUJD')")
        .bind(sid, p),
    ),
    // PR-4 (0071): the LIVING task list rides the cascade too — job-keyed directly. NINE
    // tasks: distinct from every sibling count in this seed (1/2/3/4/5/6/7/8 are all
    // taken), so the ONE newly-inserted DELETE — which shifts every later positional
    // results[] index by one — cannot mis-report by reading a neighbour's count.
    ...[1, 2, 3, 4, 5, 6, 7, 8, 9].map((i) =>
      env.DB
        .prepare(
          "INSERT INTO job_schedule_tasks (task_uuid, job_id, name, match_key, sort_order) VALUES (?,?,?,?,?)",
        )
        .bind(`task-${job}-${i}`, job, `Task ${i}`, `\ntask ${i}`, i * 10),
    ),
  ]);
  // PR-7 (0073): the payments family joins the cascade — terms 1 (the UNIQUE forces it),
  // cycles TEN and receipts ELEVEN (1–9 are all taken above), so the THREE newly-inserted
  // DELETEs — which shift every later positional results[] index by three — cannot
  // mis-report by reading a neighbour's count. Receipts key on cycle_id (all eleven on the
  // first cycle), so the cascade's job-keyed subquery is what this exercises.
  await env.DB
    .prepare(
      "INSERT INTO job_payment_terms (job_id, net_days, nonpayment_notice_days, intent_to_suspend_days, created_by, updated_by) " +
        "VALUES (?,30,10,14,'pm','pm')",
    )
    .bind(job)
    .run();
  const cycleId = (
    await env.DB
      .prepare(
        "INSERT INTO job_payment_cycles (cycle_uuid, job_id, seq, label, created_by, updated_by) " +
          "VALUES (?,?,10,'PP #1','pm','pm') RETURNING id",
      )
      .bind(`cyc-${job}-1`, job)
      .first<{ id: number }>()
  )!.id;
  await env.DB.batch([
    ...[2, 3, 4, 5, 6, 7, 8, 9, 10].map((i) =>
      env.DB
        .prepare(
          "INSERT INTO job_payment_cycles (cycle_uuid, job_id, seq, label, created_by, updated_by) " +
            "VALUES (?,?,?,?,'pm','pm')",
        )
        .bind(`cyc-${job}-${i}`, job, i * 10, `PP #${i}`),
    ),
    ...Array.from({ length: 11 }, (_, i) =>
      env.DB
        .prepare(
          "INSERT INTO job_payment_receipts (cycle_id, received_date, amount_cents, recorded_by) VALUES (?,?,100,'pm')",
        )
        .bind(cycleId, `2026-01-${String(i + 1).padStart(2, "0")}`),
    ),
  ]);
}

async function counts(job: string, uuid: string) {
  const q = async (sql: string, p: string) =>
    (await env.DB.prepare(sql).bind(p).first<{ n: number }>())!.n;
  return {
    jobs: await q("SELECT COUNT(*) n FROM jobs WHERE job_id=?", job),
    subs: await q("SELECT COUNT(*) n FROM submissions WHERE job_id=?", job),
    pdfs: await q("SELECT COUNT(*) n FROM filed_pdfs WHERE submission_uuid=?", uuid),
    reqs: await q("SELECT COUNT(*) n FROM pdf_requests WHERE submission_uuid=?", uuid),
    dailyReqs: await q("SELECT COUNT(*) n FROM job_daily_requirements WHERE job_id=?", job),
    weeklyReportInputs: await q("SELECT COUNT(*) n FROM job_weekly_report_inputs WHERE job_id=?", job),
    materials: await q("SELECT COUNT(*) n FROM job_expected_materials WHERE job_id=?", job),
    shipments: await q("SELECT COUNT(*) n FROM material_shipments WHERE job_id=?", job),
    receiptEvents: await q("SELECT COUNT(*) n FROM material_receipt_events WHERE job_id=?", job),
    timeEntries: await q("SELECT COUNT(*) n FROM time_entries WHERE job_id=?", job),
    tasks: await q("SELECT COUNT(*) n FROM task_assignments WHERE job_id=?", job),
    inspections: await q("SELECT COUNT(*) n FROM inspections WHERE job_id=?", job),
    checklists: await q("SELECT COUNT(*) n FROM checklist_instances WHERE job_id=?", job),
    equipLoc: await q("SELECT COUNT(*) n FROM equipment_location WHERE job_id=?", job),
    manifests: await q("SELECT COUNT(*) n FROM job_manifests WHERE job_id=?", job),
    manifestChunks: await q(
      "SELECT COUNT(*) n FROM job_manifest_chunks WHERE manifest_id IN (SELECT id FROM job_manifests WHERE job_id=?)", job),
    manifestRows: await q(
      "SELECT COUNT(*) n FROM job_manifest_rows WHERE manifest_id IN (SELECT id FROM job_manifests WHERE job_id=?)", job),
    manifestPreviews: await q(
      "SELECT COUNT(*) n FROM job_manifest_previews WHERE manifest_id IN (SELECT id FROM job_manifests WHERE job_id=?)", job),
    schedules: await q("SELECT COUNT(*) n FROM job_schedules WHERE job_id=?", job),
    scheduleChunks: await q(
      "SELECT COUNT(*) n FROM job_schedule_chunks WHERE schedule_id IN (SELECT id FROM job_schedules WHERE job_id=?)", job),
    scheduleRows: await q(
      "SELECT COUNT(*) n FROM job_schedule_rows WHERE schedule_id IN (SELECT id FROM job_schedules WHERE job_id=?)", job),
    schedulePreviews: await q(
      "SELECT COUNT(*) n FROM job_schedule_previews WHERE schedule_id IN (SELECT id FROM job_schedules WHERE job_id=?)", job),
    scheduleTasks: await q("SELECT COUNT(*) n FROM job_schedule_tasks WHERE job_id=?", job),
    paymentTerms: await q("SELECT COUNT(*) n FROM job_payment_terms WHERE job_id=?", job),
    paymentCycles: await q("SELECT COUNT(*) n FROM job_payment_cycles WHERE job_id=?", job),
    paymentReceipts: await q(
      "SELECT COUNT(*) n FROM job_payment_receipts WHERE cycle_id IN (SELECT id FROM job_payment_cycles WHERE job_id=?)", job),
  };
}

describe("POST /api/internal/admin/purge-job", () => {
  it("hard-deletes the job + cascades submissions/filed_pdfs/pdf_requests/requirements/materials, audits, leaves OTHER jobs", async () => {
    await seedJobWithData("JOB-PURGE", "u-purge");
    await seedJobWithData("JOB-KEEP", "u-keep");

    const res = await call("/api/internal/admin/purge-job", {
      method: "POST",
      bearer: ADMIN_BEARER,
      body: JSON.stringify({ job_id: "JOB-PURGE" }),
    });
    expect(res.status, await res.clone().text()).toBe(200);
    expect(await res.json()).toMatchObject({
      ok: true, found: true, job_id: "JOB-PURGE", job_deleted: 1, submissions: 1, pdfChunks: 1, pdfRequests: 1,
      requirements: 2, expectedMaterials: 1, // Slice 1 (R3-F4): per-job content cascades too
      // 0067 — the client-facing report's office record (OSHA counts, pending items) goes with
      // the job. 7 is distinct from every sibling count, so a positional shift fails loudly.
      weeklyReportInputs: 7,
      // PR2 — asserted BY NAME with distinct values: this is what catches a positional-index
      // shift in the route's results[] reads (2 ≠ 3 ≠ 1, so a swap cannot look correct).
      shipments: 2, receiptEvents: 3,
      // PR3b — same technique for the manifest pool. manifestChunks is the one that
      // matters most: it counts the ORIGINAL untrusted document bytes leaving with the job.
      manifests: 1, manifestChunks: 4, manifestRows: 6, manifestPreviews: 5,
      // ADR-0006 — the schedule pool, same rule: scheduleChunks counts the untrusted
      // schedule BYTES leaving with the job. scheduleTasks (0071, PR-4) is the living
      // task list going with it — 9 is distinct from every sibling, so the one inserted
      // DELETE's positional shift fails loudly instead of reading a neighbour's count.
      schedules: 2, scheduleChunks: 3, scheduleRows: 7, schedulePreviews: 8,
      scheduleTasks: 9,
      // PR-7 (0073) — asserted BY NAME with distinct values (10/11; terms is pinned at 1
      // by its per-job UNIQUE): the THREE inserted DELETEs shift every later positional
      // results[] index by three, and these are what catch a mis-shift. paymentReceipts
      // matters most — money-received events leaving with the job, via the cycle-keyed
      // subquery.
      paymentTerms: 1, paymentCycles: 10, paymentReceipts: 11,
    });

    expect(await counts("JOB-PURGE", "u-purge")).toEqual({
      jobs: 0, subs: 0, pdfs: 0, reqs: 0, dailyReqs: 0, materials: 0,
      weeklyReportInputs: 0,
      shipments: 0, receiptEvents: 0,
      timeEntries: 0, tasks: 0, inspections: 0, checklists: 0, equipLoc: 0,
      manifests: 0, manifestChunks: 0, manifestRows: 0, manifestPreviews: 0,
      schedules: 0, scheduleChunks: 0, scheduleRows: 0, schedulePreviews: 0,
      scheduleTasks: 0,
      paymentTerms: 0, paymentCycles: 0, paymentReceipts: 0,
    });
    // The OTHER job keeps every one of them — the cascade is job-scoped, not a sweep.
    expect(await counts("JOB-KEEP", "u-keep")).toEqual({
      jobs: 1, subs: 1, pdfs: 1, reqs: 1, dailyReqs: 2, materials: 1,
      weeklyReportInputs: 7,
      shipments: 2, receiptEvents: 3,
      timeEntries: 1, tasks: 1, inspections: 1, checklists: 1, equipLoc: 1,
      manifests: 1, manifestChunks: 4, manifestRows: 6, manifestPreviews: 5,
      schedules: 2, scheduleChunks: 3, scheduleRows: 7, schedulePreviews: 8,
      scheduleTasks: 9,
      paymentTerms: 1, paymentCycles: 10, paymentReceipts: 11,
    });
    const audit = await env.DB
      .prepare("SELECT action, target_username FROM audit_log WHERE action='purge-job'")
      .first<{ action: string; target_username: string }>();
    expect(audit).toMatchObject({ action: "purge-job", target_username: "JOB-PURGE" });
  });

  it("unknown job → ok:true, found:false, all counts 0 (idempotent)", async () => {
    const res = await call("/api/internal/admin/purge-job", {
      method: "POST", bearer: ADMIN_BEARER, body: JSON.stringify({ job_id: "NOPE" }),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({
      ok: true, found: false, job_deleted: 0, submissions: 0, pdfChunks: 0, pdfRequests: 0,
      requirements: 0, expectedMaterials: 0, weeklyReportInputs: 0,
    });
  });

  it("blank job_id → 400 invalid_job_id; non-object body → 400 bad_request", async () => {
    expect(
      (await call("/api/internal/admin/purge-job", { method: "POST", bearer: ADMIN_BEARER, body: JSON.stringify({}) })).status,
    ).toBe(400);
    expect(
      (await call("/api/internal/admin/purge-job", { method: "POST", bearer: ADMIN_BEARER, body: "null" })).status,
    ).toBe(400);
  });

  it("requires the admin bearer (401 without)", async () => {
    expect(
      (await call("/api/internal/admin/purge-job", { method: "POST", body: JSON.stringify({ job_id: "X" }) })).status,
    ).toBe(401);
  });
});
