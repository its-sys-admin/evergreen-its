import { env } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import { pruneOldData, writePruneMeta, type PruneResult } from "../worker/prune";

// A3 — the daily D1 prune. Verifies the retention windows AND the load-bearing guard:
// an UNFILED submission (box_verified=0) is NEVER evicted, even when old.
// GS2 adds: per-stage failure isolation, the prune_meta heartbeat, the terminal
// publish_requests rider, and the checklist_instances/equipment_location guard union.

const NOW = 1_780_000_000; // a fixed "now" (~2026) so the test never drifts with wall clock
const DAY = 86_400;

beforeEach(async () => {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM submissions"),
    env.DB.prepare("DELETE FROM audit_log"),
    env.DB.prepare("DELETE FROM filed_pdfs"),
    env.DB.prepare("DELETE FROM pdf_requests"),
    env.DB.prepare("DELETE FROM time_entries"),
    env.DB.prepare("DELETE FROM task_assignments"),
    env.DB.prepare("DELETE FROM inspections"),
    env.DB.prepare("DELETE FROM job_daily_requirements"),
    env.DB.prepare("DELETE FROM job_expected_materials"),
    env.DB.prepare("DELETE FROM item_photos"),
    env.DB.prepare("DELETE FROM daily_photo_pool"),
    env.DB.prepare("DELETE FROM checklist_item_states"),
    env.DB.prepare("DELETE FROM checklist_instances"),
    env.DB.prepare("DELETE FROM equipment_location"),
    env.DB.prepare("DELETE FROM equipment"),
    env.DB.prepare("DELETE FROM publish_requests"),
    env.DB.prepare("DELETE FROM prune_meta"),
    env.DB.prepare("DELETE FROM estimate_extraction_lines"),
    env.DB.prepare("DELETE FROM estimate_extractions"),
    env.DB.prepare("DELETE FROM estimate_previews"),
    env.DB.prepare("DELETE FROM po_estimate_chunks"),
    env.DB.prepare("DELETE FROM po_estimates"),
    env.DB.prepare("DELETE FROM job_payment_receipts"),
    env.DB.prepare("DELETE FROM job_payment_cycles"),
    env.DB.prepare("DELETE FROM job_payment_terms"),
    env.DB.prepare("DELETE FROM jobs"),
  ]);
});

async function seedSub(uuid: string, boxVerified: number, filedAt: number | null): Promise<void> {
  await env.DB
    .prepare(
      "INSERT INTO submissions (submission_uuid, job_id, form_code, work_date, payload_json, created_at, box_verified, filed_at) VALUES (?,?,?,?,?,?,?,?)",
    )
    .bind(uuid, "JOB-1", "jha-v1", "2026-01-01", "{}", NOW, boxVerified, filedAt)
    .run();
}

async function remaining(): Promise<string[]> {
  const r = await env.DB
    .prepare("SELECT submission_uuid FROM submissions ORDER BY submission_uuid")
    .all<{ submission_uuid: string }>();
  return r.results.map((x) => x.submission_uuid);
}

describe("pruneOldData (A3 D1 housekeeping)", () => {
  it("STRIPS payload from FILED submissions older than 90d but KEEPS the metadata row (PR-5 Stage 1)", async () => {
    await seedSub("filed-old", 1, NOW - 100 * DAY);
    await seedSub("filed-recent", 1, NOW - 10 * DAY);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.stripped).toBe(1);
    expect(res.submissions).toBe(0); // nothing DELETED (no inactive job)
    expect(await remaining()).toEqual(["filed-old", "filed-recent"]); // both rows kept
    const old = await env.DB.prepare("SELECT payload_json FROM submissions WHERE submission_uuid='filed-old'").first<{ payload_json: string }>();
    const recent = await env.DB.prepare("SELECT payload_json FROM submissions WHERE submission_uuid='filed-recent'").first<{ payload_json: string }>();
    expect(old?.payload_json).toBe(""); // stripped
    expect(recent?.payload_json).toBe("{}"); // intact (within 90d)
  });

  it("deletes an INACTIVE-job filed row (PR-5 Stage 2) but NEVER an unfiled row (box_verified=0)", async () => {
    // JOB-1 is INACTIVE → its filed rows are deletable 30d after filing.
    await env.DB.prepare("INSERT OR REPLACE INTO jobs (job_id, project_name, active) VALUES ('JOB-1','J',0)").run();
    await seedSub("unfiled-old", 0, NOW - 200 * DAY); // box_verified=0 — Box has no copy → NEVER touched
    await seedSub("filed-old", 1, NOW - 200 * DAY);   // inactive job + old → Stage-2 delete

    const res = await pruneOldData(env.DB, NOW);

    expect(res.submissions).toBe(1); // the inactive-job filed row deleted
    expect(await remaining()).toEqual(["unfiled-old"]); // the unfiled one survives
    const unf = await env.DB.prepare("SELECT payload_json FROM submissions WHERE submission_uuid='unfiled-old'").first<{ payload_json: string }>();
    expect(unf?.payload_json).toBe("{}"); // unfiled payload NOT stripped either
  });

  it("keeps audit_log ~1 year, prunes older", async () => {
    await env.DB.prepare("INSERT INTO audit_log (created_at, actor_username, action) VALUES (?,?,?)").bind(NOW - 400 * DAY, "admin.one", "old").run();
    await env.DB.prepare("INSERT INTO audit_log (created_at, actor_username, action) VALUES (?,?,?)").bind(NOW - 10 * DAY, "admin.one", "recent").run();

    const res = await pruneOldData(env.DB, NOW);

    expect(res.audit).toBe(1);
    expect((await env.DB.prepare("SELECT COUNT(*) n FROM audit_log").first<{ n: number }>())!.n).toBe(1);
  });

  it("prunes DRAFT + CANCELED subcontract/PO rows (and their lines) after 90d; keeps on-path + recent", async () => {
    const seedSc = async (uuid: string, status: string, updatedAt: number, scNumber: string | null = null) => {
      await env.DB
        .prepare("INSERT INTO subcontracts (sc_uuid, job_no, sub_key, created_by, status, updated_at, sc_number) VALUES (?,?,?,?,?,?,?)")
        .bind(uuid, "2026.001", "SUB-1", "admin", status, updatedAt, scNumber)
        .run();
      const id = (await env.DB.prepare("SELECT id FROM subcontracts WHERE sc_uuid=?").bind(uuid).first<{ id: number }>())!.id;
      await env.DB.prepare("INSERT INTO sov_lines (subcontract_id, position) VALUES (?, 1)").bind(id).run();
      return id;
    };
    const seedPo = async (uuid: string, status: string, updatedAt: number, poNumber: string | null = null) => {
      await env.DB
        .prepare("INSERT INTO purchase_orders (po_uuid, job_no, site_phase, vendor_key, created_by, status, updated_at, po_number) VALUES (?,?,?,?,?,?,?,?)")
        .bind(uuid, "2026.001", 0, "V-1", "admin", status, updatedAt, poNumber)
        .run();
      const id = (await env.DB.prepare("SELECT id FROM purchase_orders WHERE po_uuid=?").bind(uuid).first<{ id: number }>())!.id;
      await env.DB.prepare("INSERT INTO po_line_items (po_id, position) VALUES (?, 1)").bind(id).run();
      return id;
    };
    const scDraftOld = await seedSc("sc-draft-old", "draft", NOW - 100 * DAY);
    const scCancelOld = await seedSc("sc-cancel-old", "canceled", NOW - 100 * DAY); // canceled-from-draft, no number
    const scCancelGen = await seedSc("sc-cancel-gen", "canceled", NOW - 100 * DAY, "2026.001.9.0.0"); // GENERATED-then-canceled → KEEP
    const scQueuedOld = await seedSc("sc-queued-old", "queued", NOW - 100 * DAY, "2026.001.8.0.0"); // ON-PATH → never pruned
    const scDraftRecent = await seedSc("sc-draft-recent", "draft", NOW - 10 * DAY);
    // ADR-0004 R2: the RFQ lane is the structural twin of purchase_orders but had NO
    // prune stage at all — drafts, cancels, their line items and up to 12 vendor rows
    // each accumulated in D1 forever with no automatic OR manual removal path.
    const seedRfq = async (uuid: string, status: string, updatedAt: number, rfqNumber: string | null = null) => {
      await env.DB
        .prepare("INSERT INTO rfqs (rfq_uuid, job_no, created_by, status, updated_at, rfq_number) VALUES (?,?,?,?,?,?)")
        .bind(uuid, "2026.001", "admin", status, updatedAt, rfqNumber)
        .run();
      const id = (await env.DB.prepare("SELECT id FROM rfqs WHERE rfq_uuid=?").bind(uuid).first<{ id: number }>())!.id;
      await env.DB.prepare("INSERT INTO rfq_line_items (rfq_id, position, description) VALUES (?,1,'Panels')").bind(id).run();
      await env.DB.prepare("INSERT INTO rfq_vendors (rfq_id, vendor_key) VALUES (?, 'V-1')").bind(id).run();
      return id;
    };
    const rfqDraftOld = await seedRfq("rfq-draft-old", "draft", NOW - 100 * DAY);
    const rfqCancelGen = await seedRfq("rfq-cancel-gen", "canceled", NOW - 100 * DAY, "RFQ-2026.001-001"); // GENERATED → KEEP
    const rfqDraftRecent = await seedRfq("rfq-draft-recent", "draft", NOW - 10 * DAY); // inside window → KEEP
    const poDraftOld = await seedPo("po-draft-old", "draft", NOW - 100 * DAY);
    const poCancelGen = await seedPo("po-cancel-gen", "canceled", NOW - 100 * DAY, "2026.001.9.0.0"); // GENERATED-then-canceled → KEEP
    const poQueuedOld = await seedPo("po-queued-old", "queued", NOW - 100 * DAY, "2026.001.8.0.0"); // ON-PATH → never pruned

    const res = await pruneOldData(env.DB, NOW);

    expect(res.subcontractDrafts).toBe(2); // draftOld + cancelOld (queued/recent/generated-canceled excluded)
    expect(res.poDrafts).toBe(1); // poDraftOld only
    expect(res.rfqDrafts).toBe(1); // rfqDraftOld only — generated-canceled + in-window kept
    const rfqKids = async (id: number) =>
      (await env.DB.prepare("SELECT (SELECT COUNT(*) FROM rfq_line_items WHERE rfq_id=?1) + (SELECT COUNT(*) FROM rfq_vendors WHERE rfq_id=?1) AS n").bind(id).first<{ n: number }>())!.n;
    expect(await env.DB.prepare("SELECT id FROM rfqs WHERE id=?").bind(rfqDraftOld).first()).toBeNull();
    expect(await rfqKids(rfqDraftOld)).toBe(0); // children cascaded, no orphans left
    // The numbering-reuse guard: a GENERATED-then-canceled RFQ is kept forever so its
    // number can never be recycled, exactly like its PO twin.
    expect(await env.DB.prepare("SELECT id FROM rfqs WHERE id=?").bind(rfqCancelGen).first()).not.toBeNull();
    expect(await env.DB.prepare("SELECT id FROM rfqs WHERE id=?").bind(rfqDraftRecent).first()).not.toBeNull();
    const gone = async (tbl: string, id: number) => (await env.DB.prepare(`SELECT id FROM ${tbl} WHERE id=?`).bind(id).first()) === null;
    const scLines = async (id: number) => (await env.DB.prepare("SELECT COUNT(*) n FROM sov_lines WHERE subcontract_id=?").bind(id).first<{ n: number }>())!.n;
    const poLines = async (id: number) => (await env.DB.prepare("SELECT COUNT(*) n FROM po_line_items WHERE po_id=?").bind(id).first<{ n: number }>())!.n;
    // aged draft/canceled + their lines gone
    expect(await gone("subcontracts", scDraftOld)).toBe(true);
    expect(await gone("subcontracts", scCancelOld)).toBe(true);
    expect(await scLines(scDraftOld)).toBe(0);
    expect(await gone("purchase_orders", poDraftOld)).toBe(true);
    expect(await poLines(poDraftOld)).toBe(0);
    // on-path + recent retained WITH their lines (no orphan / no over-prune)
    expect(await gone("subcontracts", scQueuedOld)).toBe(false);
    expect(await scLines(scQueuedOld)).toBe(1);
    expect(await gone("subcontracts", scDraftRecent)).toBe(false);
    expect(await gone("purchase_orders", poQueuedOld)).toBe(false);
    expect(await poLines(poQueuedOld)).toBe(1);
    // GENERATED-then-canceled rows (number allocated) are KEPT — deleting them would free the
    // sc_number/po_number + revision slot for a collision-reuse by a later generate.
    expect(await gone("subcontracts", scCancelGen)).toBe(false);
    expect(await scLines(scCancelGen)).toBe(1);
    expect(await gone("purchase_orders", poCancelGen)).toBe(false);
    expect(await poLines(poCancelGen)).toBe(1);
  });

  it("prunes REJECTED (box_verified=-1) rows after 30d, keeps recent + never the unfiled (M4/PR-4)", async () => {
    await seedSub("rej-old", -1, NOW - 40 * DAY);
    await seedSub("rej-recent", -1, NOW - 5 * DAY);
    await seedSub("unfiled-old", 0, NOW - 200 * DAY); // box_verified=0 still NEVER evicted
    const res = await pruneOldData(env.DB, NOW);
    expect(res.rejected).toBe(1);
    expect(await remaining()).toEqual(["rej-recent", "unfiled-old"]);
  });

  it("deletes an INACTIVE job with NO submissions; keeps active jobs + inactive jobs that still hold submissions", async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-active','A',1)"),
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-empty','E',0)"),   // inactive, no subs → delete
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-withsub','W',0)"), // inactive, has a recent sub → keep
    ]);
    // A FILED sub within the 30d Stage-2 grace → not deleted → its inactive job is kept this run.
    await env.DB
      .prepare("INSERT INTO submissions (submission_uuid, job_id, form_code, work_date, payload_json, created_at, box_verified, filed_at) VALUES ('s-w','J-withsub','jha-v1','2026-01-01','{}',?,1,?)")
      .bind(NOW, NOW - 5 * DAY)
      .run();

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(1); // only J-empty
    const left = await env.DB.prepare("SELECT job_id FROM jobs ORDER BY job_id").all<{ job_id: string }>();
    expect(left.results.map((j) => j.job_id)).toEqual(["J-active", "J-withsub"]);
  });

  // ── Track 6 archive fence (migration 0058) ────────────────────────────────────────────────
  //
  // THE PAIR. Two jobs identical in every respect the eight NOT-IN guards can see — inactive,
  // zero submissions, zero field-ops records — differing ONLY in archive_state. Without the
  // guard, an archived job is deleted on the very next nightly run (this stage has NO age
  // cutoff), taking archive_state, archive_detail, archive_folder_key and the recorded source
  // locations with it. Those are the only record of where each container came from, so losing
  // them makes the archive irreversible.
  it("KEEPS an archived job that is otherwise indistinguishable from a prunable one", async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active, archive_state) VALUES ('J-arch','Archived',0,'complete')"),
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active, archive_state) VALUES ('J-plain','Plain',0,'none')"),
    ]);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(1); // ONLY the un-archived one
    const left = await env.DB.prepare("SELECT job_id FROM jobs ORDER BY job_id").all<{ job_id: string }>();
    expect(left.results.map((j) => j.job_id)).toEqual(["J-arch"]);
  });

  it("keeps a job in ANY non-'none' archive state, including a failed/partial one", async () => {
    // A half-archived job is exactly the one an operator still needs the record for.
    await env.DB.batch([
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active, archive_state) VALUES ('J-req','R',0,'requested')"),
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active, archive_state) VALUES ('J-prog','P',0,'in_progress')"),
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active, archive_state) VALUES ('J-part','X',0,'partial')"),
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active, archive_state) VALUES ('J-fail','F',0,'failed')"),
    ]);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(0);
    const left = await env.DB.prepare("SELECT count(*) AS n FROM jobs").first<{ n: number }>();
    expect(left!.n).toBe(4);
  });

  it("a COMPLETED un-archive resets the row to prunable again (the guard is not a permanent pin)", async () => {
    // The daemon zeroes archive_state back to 'none' when an un-archive finishes; by then the job
    // is lifecycle='inactive' with active=0, i.e. exactly the pre-existing prune behaviour.
    await env.DB
      .prepare("INSERT INTO jobs (job_id, project_name, active, archive_state) VALUES ('J-back','B',0,'none')")
      .run();

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(1);
  });

  it("NEVER deletes an inactive job holding field-ops SoR (time_entries) with no submissions [P2.1 fence]", async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-fieldops','F',0)"), // inactive, no subs
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-bare','B',0)"),     // inactive, nothing → delete
    ]);
    // J-fieldops holds a time entry — D1-primary operational SoR, so the jobs-delete guard must keep it.
    await env.DB
      .prepare("INSERT INTO time_entries (uuid, job_id, actor_username) VALUES ('t-1','J-fieldops','pm.bob')")
      .run();

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(1); // only J-bare deleted
    const left = await env.DB.prepare("SELECT job_id FROM jobs ORDER BY job_id").all<{ job_id: string }>();
    expect(left.results.map((j) => j.job_id)).toEqual(["J-fieldops"]);
  });

  it("NEVER deletes an inactive job holding per-job content (job_daily_requirements / job_expected_materials) [Slice 1 R3-F4 fence]", async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-reqs','R',0)"), // inactive, holds a requirement → keep
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-mats','M',0)"), // inactive, holds an expected material → keep
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-bare','B',0)"), // inactive, nothing → delete
    ]);
    // Both tables are D1-PRIMARY (admin-authored per-job content, no copy outside D1;
    // restore path is D1 Time Travel) — deleting their job would orphan them invisibly.
    // A soft-deleted (active=0) row still guards: the forensic history is the record.
    await env.DB.batch([
      env.DB
        .prepare("INSERT INTO job_daily_requirements (job_id, seq, kind, label, active) VALUES ('J-reqs',10,'confirm','Client daily brief',0)"),
      env.DB
        .prepare("INSERT INTO job_expected_materials (job_id, description, seq) VALUES ('J-mats','Panels pallet',10)"),
    ]);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(1); // only J-bare deleted
    const left = await env.DB.prepare("SELECT job_id FROM jobs ORDER BY job_id").all<{ job_id: string }>();
    expect(left.results.map((j) => j.job_id)).toEqual(["J-mats", "J-reqs"]);
  });

  it("NEVER deletes an inactive job holding a living schedule task list (job_schedule_tasks) [ADR-0006 PR-4 fence]", async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-sched','S',0)"), // holds a task → keep
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-bare','B',0)"),  // nothing → delete
    ]);
    // The task list is D1-PRIMARY operational record (portal marks, baselines, delivered
    // stamps have no copy outside D1). A soft-deleted (active=0) task still guards — the
    // tombstone is the PR-6 reconcile's record — so seed one INACTIVE task and prove the
    // guard bites on it too.
    await env.DB
      .prepare(
        "INSERT INTO job_schedule_tasks (task_uuid, job_id, name, match_key, active) VALUES ('tu-guard','J-sched','Pile Installation','\npile installation',0)",
      )
      .run();

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(1); // only J-bare deleted
    const left = await env.DB.prepare("SELECT job_id FROM jobs ORDER BY job_id").all<{ job_id: string }>();
    expect(left.results.map((j) => j.job_id)).toEqual(["J-sched"]);
  });

  it("NEVER deletes an inactive job holding payment terms or cycles (job_payment_terms / job_payment_cycles) [ADR-0006 PR-7 fence]", async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-terms','T',0)"), // holds terms → keep
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-cycles','C',0)"), // holds a cycle → keep
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-bare','B',0)"),   // nothing → delete
    ]);
    // Both are D1-PRIMARY commercial records (0073): invoice amounts, recorded notice
    // dates, and — through the cycles — the receipt ledger (cycle-keyed, guarded
    // TRANSITIVELY: a receipt can only exist under a cycle, and the cycle guards the
    // job). A soft-deleted (active=0) cycle still guards — its receipts are history.
    await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO job_payment_terms (job_id, net_days, nonpayment_notice_days, intent_to_suspend_days, created_by, updated_by) " +
          "VALUES ('J-terms',30,10,14,'pm','pm')",
      ),
      env.DB.prepare(
        "INSERT INTO job_payment_cycles (cycle_uuid, job_id, label, active, created_by, updated_by) " +
          "VALUES ('cyc-guard','J-cycles','PP #1',0,'pm','pm')",
      ),
    ]);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(1); // only J-bare deleted
    const left = await env.DB.prepare("SELECT job_id FROM jobs ORDER BY job_id").all<{ job_id: string }>();
    expect(left.results.map((j) => j.job_id)).toEqual(["J-cycles", "J-terms"]);
  });
});

// ── PR-4 Part A — the filed_pdfs cache prune branch + D1 size telemetry. ──────────
async function seedChunk(uuid: string, index: number, total: number): Promise<void> {
  await env.DB
    .prepare("INSERT OR REPLACE INTO filed_pdfs (submission_uuid, chunk_index, chunk_total, chunk_b64) VALUES (?,?,?,?)")
    .bind(uuid, index, total, "QUJD") // "ABC"
    .run();
}
async function chunkUuids(): Promise<string[]> {
  const r = await env.DB
    .prepare("SELECT DISTINCT submission_uuid FROM filed_pdfs ORDER BY submission_uuid")
    .all<{ submission_uuid: string }>();
  return r.results.map((x) => x.submission_uuid);
}

describe("pruneOldData — PDF cache (filed_pdfs)", () => {
  it("drops chunks for a submission with NO live request, keeps those with a live request (PR-5)", async () => {
    // cache-norequest: cached, but its only request EXPIRED (>24h) → chunks dropped + reset.
    await seedSub("cache-norequest", 1, NOW - 10 * DAY);
    await env.DB.prepare("UPDATE submissions SET pdf_ready_at=? WHERE submission_uuid='cache-norequest'").bind(NOW - 100).run();
    await env.DB.prepare("INSERT INTO pdf_requests (submission_uuid, account, requested_at) VALUES ('cache-norequest','pm',?)").bind(NOW - 2 * DAY).run();
    await seedChunk("cache-norequest", 0, 1);
    // cache-live: cached WITH a live (within 24h) request → chunks survive.
    await seedSub("cache-live", 1, NOW - 10 * DAY);
    await env.DB.prepare("UPDATE submissions SET pdf_ready_at=? WHERE submission_uuid='cache-live'").bind(NOW - 100).run();
    await env.DB.prepare("INSERT INTO pdf_requests (submission_uuid, account, requested_at) VALUES ('cache-live','pm',?)").bind(NOW - 100).run();
    await seedChunk("cache-live", 0, 1);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.pdfChunks).toBe(1); // the no-live-request chunk
    expect(await chunkUuids()).toEqual(["cache-live"]); // live-request chunk survives
    // The no-live-request submission's pdf_ready_at was reset so a fresh request re-services.
    const reset = await env.DB.prepare("SELECT pdf_ready_at FROM submissions WHERE submission_uuid='cache-norequest'").first<{ pdf_ready_at: number | null }>();
    expect(reset?.pdf_ready_at).toBeNull();
  });

  it("deletes ORPHAN chunks whose parent submission is gone", async () => {
    // No submission row for this uuid — a pure orphan (parent already pruned).
    await seedChunk("orphan-uuid", 0, 1);
    await seedChunk("orphan-uuid", 1, 2);
    const res = await pruneOldData(env.DB, NOW);
    expect(res.pdfChunks).toBe(2);
    expect(await chunkUuids()).toEqual([]);
  });

  it("surfaces dbSizeBytes telemetry (present, non-negative)", async () => {
    await seedSub("any", 1, NOW - 10 * DAY);
    const res = await pruneOldData(env.DB, NOW);
    expect(typeof res.dbSizeBytes).toBe("number");
    expect(res.dbSizeBytes).toBeGreaterThanOrEqual(0);
  });
});

// ── GS2 — stage isolation: one stage throwing must not skip later stages. ────────
//
// The forced throw rides a poisoned D1 façade: prepare() delegates to the real DB except
// for SQL matching the target stage, whose statement rejects at run(). No schema mutation,
// no isolated-storage coupling — pruneOldData only ever calls db.prepare().
function poisonedDb(pattern: RegExp): typeof env.DB {
  const facade = {
    prepare(sql: string) {
      if (pattern.test(sql)) {
        const poisoned = {
          bind: () => poisoned,
          run: () => Promise.reject(new Error("forced stage failure (test)")),
          first: () => Promise.reject(new Error("forced stage failure (test)")),
          all: () => Promise.reject(new Error("forced stage failure (test)")),
        };
        return poisoned;
      }
      return env.DB.prepare(sql);
    },
  };
  return facade as unknown as typeof env.DB;
}

describe("pruneOldData — GS2 stage isolation", () => {
  it("a throw in an early stage (audit) no longer skips later stages; the failure is flagged", async () => {
    // Old-enough audit row (stage will throw before touching it) + an old FILED submission
    // the LATER strip stage must still process.
    await env.DB.prepare("INSERT INTO audit_log (created_at, actor_username, action) VALUES (?,?,?)").bind(NOW - 400 * DAY, "admin.one", "old").run();
    await seedSub("filed-old", 1, NOW - 100 * DAY);

    const res = await pruneOldData(poisonedDb(/DELETE FROM audit_log/), NOW);

    expect(res.failedStages).toEqual(["audit"]); // the failed stage is NAMED
    expect(res.audit).toBe(0);                   // its counter reads 0 (nothing deleted)
    expect(res.stripped).toBe(1);                // the LATER strip stage still ran
    const old = await env.DB.prepare("SELECT payload_json FROM submissions WHERE submission_uuid='filed-old'").first<{ payload_json: string }>();
    expect(old?.payload_json).toBe("");          // ...and really stripped the payload
    const auditLeft = await env.DB.prepare("SELECT COUNT(*) n FROM audit_log").first<{ n: number }>();
    expect(auditLeft!.n).toBe(1);                // the poisoned stage really did nothing
  });

  it("multiple failing stages all accumulate; the function never throws", async () => {
    await seedSub("filed-old", 1, NOW - 100 * DAY);
    const res = await pruneOldData(poisonedDb(/audit_log|publish_requests/), NOW);
    expect(res.failedStages).toEqual(["audit", "publish_requests"]);
    expect(res.stripped).toBe(1); // stages between/after the failures still ran
  });

  it("a clean run reports an empty failedStages", async () => {
    const res = await pruneOldData(env.DB, NOW);
    expect(res.failedStages).toEqual([]);
  });
});

// ── GS2 rider — terminal publish_requests hygiene prune (90d). ───────────────────
async function seedPublish(id: number, status: string, updatedAt: number): Promise<void> {
  await env.DB
    .prepare(
      "INSERT INTO publish_requests (id, created_at, updated_at, requested_by, op, parent_form_code, identity, status, definition_json) " +
        "VALUES (?,?,?,?,?,?,?,?,?)",
    )
    .bind(id, updatedAt - DAY, updatedAt, "admin.one", "edit", "jha", `jha-v${id}`, status, '{"blob":true}')
    .run();
}
async function publishIds(): Promise<number[]> {
  const r = await env.DB.prepare("SELECT id FROM publish_requests ORDER BY id").all<{ id: number }>();
  return r.results.map((x) => x.id);
}

describe("pruneOldData — GS2 terminal publish_requests retention", () => {
  it("deletes TERMINAL (archived/failed) rows >90d; keeps recent-terminal and ALL non-terminal", async () => {
    await seedPublish(1, "archived", NOW - 100 * DAY); // terminal + old → delete
    await seedPublish(2, "failed", NOW - 100 * DAY);   // terminal + old → delete
    await seedPublish(3, "archived", NOW - 10 * DAY);  // terminal + recent → keep
    await seedPublish(4, "queued", NOW - 100 * DAY);   // NON-terminal, however old → keep
    await seedPublish(5, "live", NOW - 100 * DAY);     // NON-terminal → keep

    const res = await pruneOldData(env.DB, NOW);

    expect(res.publishRequests).toBe(2);
    expect(await publishIds()).toEqual([3, 4, 5]);
  });
});

// ── GS2 rider — checklist_instances + equipment_location join the jobs-delete guard. ──
describe("pruneOldData — GS2 jobs-delete guard union", () => {
  it("NEVER deletes an inactive job whose only records are checklist instances / equipment locations", async () => {
    await env.DB.batch([
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-check','C',0)"), // holds a checklist instance → keep
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-loc','L',0)"),   // holds an equipment location → keep
      env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-bare','B',0)"),  // nothing → delete
    ]);
    await env.DB
      .prepare("INSERT INTO checklist_instances (kind, job_id, instance_date, status) VALUES ('daily','J-check','2026-01-01','complete')")
      .run();
    await env.DB.prepare("INSERT INTO equipment (id, name) VALUES (1,'Skid steer')").run();
    await env.DB
      .prepare("INSERT INTO equipment_location (equipment_id, job_id, label, recorded_at) VALUES (1,'J-loc','yard',?)")
      .bind(NOW)
      .run();

    const res = await pruneOldData(env.DB, NOW);

    expect(res.jobs).toBe(1); // only J-bare
    const left = await env.DB.prepare("SELECT job_id FROM jobs ORDER BY job_id").all<{ job_id: string }>();
    expect(left.results.map((j) => j.job_id)).toEqual(["J-check", "J-loc"]);
  });

  it("a NULL job_id row in a nullable guard table does NOT poison the NOT-IN (empty jobs still delete)", async () => {
    // checklist_instances.job_id / equipment_location.job_id are NULLABLE (unlike the other
    // guard tables). Without the IS NOT NULL filter, one NULL row makes `x NOT IN (…NULL…)`
    // evaluate NULL for EVERY x — silently disabling the whole jobs stage forever.
    await env.DB.prepare("INSERT INTO jobs (job_id, project_name, active) VALUES ('J-bare','B',0)").run();
    await env.DB
      .prepare("INSERT INTO checklist_instances (kind, job_id, instance_date, status) VALUES ('inspection',NULL,NULL,'open')")
      .run();
    await env.DB.prepare("INSERT INTO equipment (id, name) VALUES (1,'Barge')").run();
    await env.DB
      .prepare("INSERT INTO equipment_location (equipment_id, job_id, label, recorded_at) VALUES (1,NULL,'unavailable',?)")
      .bind(NOW)
      .run();

    const res = await pruneOldData(env.DB, NOW);

    expect(res.failedStages).toEqual([]);
    expect(res.jobs).toBe(1); // J-bare still deleted despite the NULL rows
  });
});

// ── G1 Slice 1 — the item_photos stuck-pending rider + orphan drop (migration 0036). ──
async function seedItemState(instanceId: number, label = "Walk the site"): Promise<number> {
  await env.DB
    .prepare("INSERT INTO checklist_item_states (instance_id, item_type, label, status) VALUES (?1,'manual_attest',?2,'open')")
    .bind(instanceId, label)
    .run();
  return (await env.DB.prepare("SELECT id FROM checklist_item_states ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
}
async function seedItemPhoto(itemStateId: number, status: string, createdAt: number, photoJson: string | null = '{"data":"x"}'): Promise<number> {
  await env.DB
    .prepare("INSERT INTO item_photos (item_state_id, status, photo_json, hmac, created_at) VALUES (?1,?2,?3,'testhmac',?4)")
    .bind(itemStateId, status, photoJson, createdAt)
    .run();
  return (await env.DB.prepare("SELECT id FROM item_photos ORDER BY id DESC LIMIT 1").first<{ id: number }>())!.id;
}
async function photoIds(): Promise<number[]> {
  const r = await env.DB.prepare("SELECT id FROM item_photos ORDER BY id").all<{ id: number }>();
  return r.results.map((x) => x.id);
}

describe("pruneOldData — G1 item_photos stuck-pending rider", () => {
  it("deletes PENDING rows older than 7d and clears their dangling 'pending:<id>' refs; fresh pending survives", async () => {
    await env.DB
      .prepare("INSERT INTO checklist_instances (id, kind, status) VALUES (77,'inspection','open')")
      .run();
    const stuckState = await seedItemState(77, "stuck");
    const freshState = await seedItemState(77, "fresh");
    const stuckId = await seedItemPhoto(stuckState, "pending", NOW - 8 * DAY);
    const freshId = await seedItemPhoto(freshState, "pending", NOW - 1 * DAY);
    // Bind the ref as TEXT (mirror the route's integer-column concat 'pending:<id>' exactly — a
    // numeric bind would concat as 'pending:1.0' and never match the prune's integer ref).
    await env.DB.prepare("UPDATE checklist_item_states SET photo_ref=?2 WHERE id=?1").bind(stuckState, `pending:${stuckId}`).run();
    await env.DB.prepare("UPDATE checklist_item_states SET photo_ref=?2 WHERE id=?1").bind(freshState, `pending:${freshId}`).run();

    const res = await pruneOldData(env.DB, NOW);

    expect(res.itemPhotos).toBe(1);
    expect(res.failedStages).toEqual([]);
    expect(await photoIds()).toEqual([freshId]); // only the fresh pending row remains
    // The stuck item returned to its no-photo state (ref cleared → the crew can re-attach)…
    const stuck = await env.DB.prepare("SELECT photo_ref FROM checklist_item_states WHERE id=?1").bind(stuckState).first<{ photo_ref: string | null }>();
    expect(stuck?.photo_ref).toBeNull();
    // …while the fresh item's ref is untouched.
    const fresh = await env.DB.prepare("SELECT photo_ref FROM checklist_item_states WHERE id=?1").bind(freshState).first<{ photo_ref: string | null }>();
    expect(fresh?.photo_ref).toBe(`pending:${freshId}`);
  });

  it("NEVER deletes clean/refused rows by age (delete-on-screen made them byte-free; Box holds the record)", async () => {
    await env.DB.prepare("INSERT INTO checklist_instances (id, kind, status) VALUES (78,'inspection','open')").run();
    const s1 = await seedItemState(78);
    const s2 = await seedItemState(78);
    const cleanId = await seedItemPhoto(s1, "clean", NOW - 400 * DAY, null);
    const refusedId = await seedItemPhoto(s2, "refused", NOW - 400 * DAY, null);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.itemPhotos).toBe(0);
    expect(await photoIds()).toEqual([cleanId, refusedId]);
  });

  it("drops ORPHANS whose item state no longer exists (any deletion path that missed the cancel cascade)", async () => {
    await env.DB.prepare("INSERT INTO checklist_instances (id, kind, status) VALUES (79,'inspection','open')").run();
    const liveState = await seedItemState(79);
    const liveId = await seedItemPhoto(liveState, "pending", NOW - 1 * DAY);
    // An orphan: points at an item state id that does not exist (recent — age alone wouldn't catch it).
    await seedItemPhoto(999_999, "refused", NOW - 1 * DAY, null);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.itemPhotos).toBe(1); // the orphan
    expect(await photoIds()).toEqual([liveId]);
  });

  it("a throw in the item_photos stage is isolated + NAMED (GS2 fence)", async () => {
    const res = await pruneOldData(poisonedDb(/DELETE FROM item_photos/), NOW);
    expect(res.failedStages).toEqual(["item_photos"]);
    expect(res.itemPhotos).toBe(0);
  });
});

// ── DR-photo-pool Slice 2 — the daily_photo_pool unclaimed/orphan rider. ─────────
async function seedDailyPhoto(over: {
  status?: string;
  photoJson?: string | null;
  createdAt?: number;
  claimedBy?: string | null;
  origin?: string;
} = {}): Promise<number> {
  const r = await env.DB
    .prepare(
      "INSERT INTO daily_photo_pool (job_id, work_date, uploaded_by, status, photo_json, hmac, created_at, claimed_by_submission, origin) " +
        "VALUES ('JOB-1','2026-01-01','mgr.mo',?1,?2,'h',?3,?4,?5) RETURNING id",
    )
    .bind(
      over.status ?? "pending",
      over.photoJson === undefined ? "{}" : over.photoJson,
      over.createdAt ?? NOW,
      over.claimedBy ?? null,
      over.origin ?? "field",
    )
    .first<{ id: number }>();
  return r!.id;
}
/** A saved weekly report selecting the given pool ids (the 0074 prune exemption source). */
async function seedWprSelection(poolIds: number[], jobId = "JOB-1", weekStart = "2025-12-27"): Promise<void> {
  const photos = poolIds.map((id) => ({ pool_id: id, box_file_id: "b" + id, caption: "", work_date: "2026-01-01" }));
  await env.DB
    .prepare("INSERT INTO job_weekly_report_inputs (job_id, week_start, photos_json) VALUES (?1, ?2, ?3)")
    .bind(jobId, weekStart, JSON.stringify(photos))
    .run();
}
async function dailyIds(): Promise<number[]> {
  const r = await env.DB.prepare("SELECT id FROM daily_photo_pool ORDER BY id").all<{ id: number }>();
  return r.results.map((x) => x.id);
}

describe("pruneOldData — DR daily_photo_pool unclaimed/orphan rider", () => {
  it("deletes UNCLAIMED rows >7d (any status — abandoned pre-submit uploads); fresh unclaimed survives", async () => {
    await seedDailyPhoto({ status: "pending", createdAt: NOW - 8 * DAY }); // stuck bytes → WARN path
    await seedDailyPhoto({ status: "clean", photoJson: null, createdAt: NOW - 8 * DAY }); // screened, never referenced
    const fresh = await seedDailyPhoto({ status: "pending", createdAt: NOW - 1 * DAY });

    const res = await pruneOldData(env.DB, NOW);

    expect(res.dailyPhotos).toBe(2);
    expect(res.failedStages).toEqual([]);
    expect(await dailyIds()).toEqual([fresh]);
  });

  it("NEVER deletes a CLAIMED row whose submission exists — it is the filed report's photo manifest", async () => {
    await seedSub("uuid-manifest", 1, NOW - 100 * DAY); // filed long ago, job still active
    const cleanManifest = await seedDailyPhoto({
      status: "clean", photoJson: null, createdAt: NOW - 400 * DAY, claimedBy: "uuid-manifest",
    });
    const refusedMarker = await seedDailyPhoto({
      status: "refused", photoJson: null, createdAt: NOW - 400 * DAY, claimedBy: "uuid-manifest",
    });

    const res = await pruneOldData(env.DB, NOW);

    expect(res.dailyPhotos).toBe(0);
    expect(await dailyIds()).toEqual([cleanManifest, refusedMarker]);
  });

  it("deletes ORPHANED claims (uuid absent from submissions) past the cutoff; a recent orphan survives the age guard", async () => {
    // The crashed-insert tail: claimed, but the submission INSERT never landed.
    await seedDailyPhoto({ status: "pending", createdAt: NOW - 8 * DAY, claimedBy: "uuid-never-landed" });
    // A claim written milliseconds before its submission INSERT (the prune must not sweep mid-flight).
    const inFlight = await seedDailyPhoto({ status: "pending", createdAt: NOW, claimedBy: "uuid-in-flight" });

    const res = await pruneOldData(env.DB, NOW);

    expect(res.dailyPhotos).toBe(1);
    expect(await dailyIds()).toEqual([inFlight]);
  });

  // ── 0074: retention split + selected-refs exemption ──────────────────────────
  it("retains an unclaimed office_wpr upload for 90d where a field row dies at 7d", async () => {
    const office = await seedDailyPhoto({ status: "clean", photoJson: null, createdAt: NOW - 8 * DAY, origin: "office_wpr" });
    await seedDailyPhoto({ status: "clean", photoJson: null, createdAt: NOW - 8 * DAY }); // field, dies
    const res = await pruneOldData(env.DB, NOW);
    expect(res.failedStages).toEqual([]);
    expect(res.dailyPhotos).toBe(1);
    expect(await dailyIds()).toEqual([office]);
  });

  it("deletes an unclaimed office_wpr upload past 90d", async () => {
    await seedDailyPhoto({ status: "clean", photoJson: null, createdAt: NOW - 91 * DAY, origin: "office_wpr" });
    const res = await pruneOldData(env.DB, NOW);
    expect(res.dailyPhotos).toBe(1);
    expect(await dailyIds()).toEqual([]);
  });

  it("a STUCK-PENDING office_wpr row still dies at 7d — it holds bytes and can never be selected", async () => {
    await seedDailyPhoto({ status: "pending", createdAt: NOW - 8 * DAY, origin: "office_wpr" });
    const res = await pruneOldData(env.DB, NOW);
    expect(res.dailyPhotos).toBe(1);
    expect(await dailyIds()).toEqual([]);
  });

  it("NEVER deletes a row selected in a saved weekly report — any origin, any age, orphaned or not", async () => {
    const sel = await seedDailyPhoto({ status: "clean", photoJson: null, createdAt: NOW - 400 * DAY });
    const orphanSel = await seedDailyPhoto({
      status: "clean", photoJson: null, createdAt: NOW - 400 * DAY, claimedBy: "uuid-gone",
    });
    await seedWprSelection([sel, orphanSel]);
    const res = await pruneOldData(env.DB, NOW);
    expect(res.dailyPhotos).toBe(0);
    expect(await dailyIds()).toEqual([sel, orphanSel]);
  });

  it("a photos_json entry WITHOUT pool_id cannot NULL-poison the exemption into a full-stage disable", async () => {
    // One malformed selection entry (no pool_id). If the NOT-IN subquery let its NULL through,
    // NOTHING would ever be deleted again — the silent-disable class the jobs-stage comment
    // documents. The 8d field row proves the stage still bites.
    await env.DB
      .prepare("INSERT INTO job_weekly_report_inputs (job_id, week_start, photos_json) VALUES ('JOB-1','2025-12-20',?1)")
      .bind(JSON.stringify([{ box_file_id: "b-no-pool-id", caption: "", work_date: "2026-01-01" }]))
      .run();
    await seedDailyPhoto({ status: "clean", photoJson: null, createdAt: NOW - 8 * DAY });
    const res = await pruneOldData(env.DB, NOW);
    expect(res.failedStages).toEqual([]);
    expect(res.dailyPhotos).toBe(1);
    expect(await dailyIds()).toEqual([]);
  });

  it("a throw in the daily_photo_pool stage is isolated + NAMED (GS2 fence)", async () => {
    const res = await pruneOldData(poisonedDb(/DELETE FROM daily_photo_pool/), NOW);
    expect(res.failedStages).toEqual(["daily_photo_pool"]);
    expect(res.dailyPhotos).toBe(0);
  });
});

// ── ADR-0004 E1 — the estimate artifact backstop (terminal-parent previews/chunks +
// refused/orphan extractions). Dispose/refusal delete these synchronously in the route
// batch; this stage is the belt-and-suspenders reap, mirroring the filed_pdfs orphan drop. ──
async function seedEstimate(over: {
  uuid: string;
  status: string;
  disposedAt?: number | null;
  screenedAt?: number | null;
  createdAt?: number;
}): Promise<number> {
  const r = await env.DB
    .prepare(
      "INSERT INTO po_estimates (est_uuid, job_no, filename, declared_mime, size_bytes, sha256, status, hmac, uploaded_by, created_at, screened_at, disposed_at) " +
        "VALUES (?1,'2026.001','q.pdf','application/pdf',10,?2,?3,'h','admin.one',?4,?5,?6) RETURNING id",
    )
    .bind(
      over.uuid,
      `sha-${over.uuid}`, // distinct per row — the partial UNIQUE sha256 index covers live rows
      over.status,
      over.createdAt ?? NOW - 200 * DAY,
      over.screenedAt ?? null,
      over.disposedAt ?? null,
    )
    .first<{ id: number }>();
  return r!.id;
}
async function seedEstimateBytes(id: number): Promise<void> {
  await env.DB.prepare("INSERT INTO estimate_previews (estimate_id, page, png_b64) VALUES (?1,1,'QUJD')").bind(id).run();
  await env.DB
    .prepare("INSERT INTO po_estimate_chunks (estimate_id, chunk_index, chunk_total, chunk_b64) VALUES (?1,0,1,'QUJD')")
    .bind(id)
    .run();
}
async function estimatePreviewN(id: number): Promise<number> {
  return (await env.DB.prepare("SELECT COUNT(*) n FROM estimate_previews WHERE estimate_id=?1").bind(id).first<{ n: number }>())!.n;
}
async function estimateChunkN(id: number): Promise<number> {
  return (await env.DB.prepare("SELECT COUNT(*) n FROM po_estimate_chunks WHERE estimate_id=?1").bind(id).first<{ n: number }>())!.n;
}

describe("pruneOldData — estimate artifact backstop (ADR-0004)", () => {
  it("reaps previews+chunks under TERMINAL parents aged 90d; keeps recent-terminal and NEVER touches LIVE rows", async () => {
    const oldRejected = await seedEstimate({ uuid: "e-old-rej", status: "rejected", disposedAt: NOW - 100 * DAY });
    const oldRefused = await seedEstimate({ uuid: "e-old-ref", status: "refused", screenedAt: NOW - 100 * DAY });
    const recentRejected = await seedEstimate({ uuid: "e-new-rej", status: "rejected", disposedAt: NOW - 10 * DAY });
    const liveOld = await seedEstimate({ uuid: "e-live", status: "extracted", createdAt: NOW - 400 * DAY });
    for (const id of [oldRejected, oldRefused, recentRejected, liveOld]) await seedEstimateBytes(id);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.failedStages).toEqual([]);
    expect(res.estimateArtifacts).toBe(4); // oldRejected + oldRefused, one preview + one chunk each
    expect(await estimatePreviewN(oldRejected)).toBe(0);
    expect(await estimateChunkN(oldRejected)).toBe(0);
    expect(await estimatePreviewN(oldRefused)).toBe(0);
    expect(await estimateChunkN(oldRefused)).toBe(0);
    expect(await estimatePreviewN(recentRejected)).toBe(1); // inside the 90d window
    expect(await estimateChunkN(recentRejected)).toBe(1);
    expect(await estimatePreviewN(liveOld)).toBe(1); // LIVE rows never reaped, however old
    expect(await estimateChunkN(liveOld)).toBe(1);
  });

  it("drops pure ORPHANS (no parent row) and refused-parent extractions with their lines; live extractions survive", async () => {
    // Orphan preview + chunk: the parent id does not exist (any path that misses the cascade).
    await env.DB.prepare("INSERT INTO estimate_previews (estimate_id, page, png_b64) VALUES (999999,1,'QUJD')").run();
    await env.DB
      .prepare("INSERT INTO po_estimate_chunks (estimate_id, chunk_index, chunk_total, chunk_b64) VALUES (999999,0,1,'QUJD')")
      .run();
    // A refused parent that (impossibly via the guarded result post) holds an extraction —
    // recent, so ONLY the extraction backstop (no age gate) can be what reaps it.
    const refused = await seedEstimate({ uuid: "e-ref-ext", status: "refused", screenedAt: NOW - 1 * DAY });
    const live = await seedEstimate({ uuid: "e-live-ext", status: "extracted" });
    const seedExtraction = async (estimateId: number): Promise<number> => {
      const r = await env.DB
        .prepare("INSERT INTO estimate_extractions (estimate_id, tier, schema_version, payload_json) VALUES (?1,3,'1.0.0','{}') RETURNING id")
        .bind(estimateId)
        .first<{ id: number }>();
      await env.DB
        .prepare("INSERT INTO estimate_extraction_lines (extraction_id, position, description) VALUES (?1,1,'rail')")
        .bind(r!.id)
        .run();
      return r!.id;
    };
    await seedExtraction(refused);
    const liveExt = await seedExtraction(live);

    const res = await pruneOldData(env.DB, NOW);

    expect(res.failedStages).toEqual([]);
    expect(res.estimateArtifacts).toBe(3); // orphan preview + orphan chunk + the refused-parent extraction
    const extLeft = await env.DB.prepare("SELECT id FROM estimate_extractions ORDER BY id").all<{ id: number }>();
    expect(extLeft.results.map((x) => x.id)).toEqual([liveExt]);
    // Children died with their extraction (no ON DELETE CASCADE — the stage deletes lines first).
    const linesLeft = await env.DB.prepare("SELECT DISTINCT extraction_id FROM estimate_extraction_lines").all<{ extraction_id: number }>();
    expect(linesLeft.results.map((x) => x.extraction_id)).toEqual([liveExt]);
    expect((await env.DB.prepare("SELECT COUNT(*) n FROM estimate_previews").first<{ n: number }>())!.n).toBe(0);
    expect((await env.DB.prepare("SELECT COUNT(*) n FROM po_estimate_chunks").first<{ n: number }>())!.n).toBe(0);
  });

  it("a throw in the estimate_artifacts stage is isolated + NAMED (GS2 fence)", async () => {
    const res = await pruneOldData(poisonedDb(/DELETE FROM estimate_previews/), NOW);
    expect(res.failedStages).toEqual(["estimate_artifacts"]);
    expect(res.estimateArtifacts).toBe(0);
  });
});

// ── GS2 — the prune_meta heartbeat row (migration 0033 + writePruneMeta). ────────
function syntheticResult(overrides: Partial<PruneResult> = {}): PruneResult {
  return {
    submissions: 1,
    stripped: 2,
    rejected: 3,
    audit: 4,
    pdfRequests: 5,
    pdfChunks: 6,
    publishRequests: 7,
    itemPhotos: 9,
    dailyPhotos: 10,
    jobs: 8,
    subcontractDrafts: 9,
    poDrafts: 10,
    rfqDrafts: 0,
    estimateArtifacts: 11,
    dbSizeBytes: 4096,
    sizeWarn: false,
    failedStages: [],
    ...overrides,
  };
}
async function metaRows(): Promise<
  { id: number; last_run_at: number; db_size_bytes: number; size_warn: number; counters_json: string; failed_stages_json: string }[]
> {
  const r = await env.DB
    .prepare("SELECT id, last_run_at, db_size_bytes, size_warn, counters_json, failed_stages_json FROM prune_meta")
    .all<{ id: number; last_run_at: number; db_size_bytes: number; size_warn: number; counters_json: string; failed_stages_json: string }>();
  return r.results;
}

describe("writePruneMeta — GS2 heartbeat record", () => {
  it("writes the one-row record with per-stage counters + failure flag", async () => {
    await writePruneMeta(env.DB, NOW, syntheticResult({ failedStages: ["audit"], sizeWarn: true, dbSizeBytes: 7_000_000_000 }));

    const rows = await metaRows();
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe(1);
    expect(rows[0].last_run_at).toBe(NOW);
    expect(rows[0].db_size_bytes).toBe(7_000_000_000);
    expect(rows[0].size_warn).toBe(1);
    expect(JSON.parse(rows[0].counters_json)).toEqual({
      submissions: 1, stripped: 2, rejected: 3, audit: 4,
      pdfRequests: 5, pdfChunks: 6, publishRequests: 7, itemPhotos: 9,
      dailyPhotos: 10, jobs: 8, subcontractDrafts: 9, poDrafts: 10, rfqDrafts: 0,
      estimateArtifacts: 11,
    });
    expect(JSON.parse(rows[0].failed_stages_json)).toEqual(["audit"]);
  });

  it("UPSERTs — a second run overwrites the single row (heartbeat, not history)", async () => {
    await writePruneMeta(env.DB, NOW - DAY, syntheticResult({ failedStages: ["jobs"] }));
    await writePruneMeta(env.DB, NOW, syntheticResult());

    const rows = await metaRows();
    expect(rows).toHaveLength(1); // still ONE row — no unbounded history
    expect(rows[0].last_run_at).toBe(NOW);
    expect(JSON.parse(rows[0].failed_stages_json)).toEqual([]); // clean run replaced the flag
  });

  it("is FENCED — a write failure (poisoned db) resolves without throwing", async () => {
    await expect(writePruneMeta(poisonedDb(/prune_meta/), NOW, syntheticResult())).resolves.toBeUndefined();
    expect(await metaRows()).toHaveLength(0); // and really wrote nothing
  });
});
