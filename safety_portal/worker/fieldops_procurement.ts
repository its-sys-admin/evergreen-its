import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
import type { Context } from "hono";
import type { Env, Vars } from "./types";
import { auditStmtIfChanged, isUniqueViolation } from "./audit";
import type { JobProcurementResponse, JobProcurementPo, JobProcurementRfq, JobProcurementSub } from "./wire-types";

// Per-job Procurement read for the Job Tracker (Track A8, operator ask 2026-08-13): the POs,
// RFQs and Subcontracts generated for ONE job, each with its lifecycle state, so the office
// tracks a job's paper from the job's own page.
//
// TRUTH SHAPE: D1 is AUTHORITATIVE for the three document lanes (ADR-0003 decision 2 — these
// are not stale mirrors), and the status columns already carry the lifecycle: POs
// draft→queued→pending_review→approved→sent (+superseded/canceled), subcontracts add
// `executed`, RFQs derive their parent status from per-vendor pending→filed→sent→responded.
// Everything at `approved` or beyond is a CACHE of the Mac/Smartsheet-side authoritative state
// with poll latency (the lanes' status-sync passes) — the SPA labels it "as of last sync".
// `rfqs.closed` exists in the CHECK but has NO writer today — deliberately not rendered as a
// reachable state here.
//
// CAPABILITY POSTURE (least-privilege; the fieldops_jobtracker routing-block precedent): the
// lanes' own admin-only caps gate their own data — cap.po.manage for POs + RFQs (one
// procurement family, one office; ADR-0004), cap.subcontracts.manage for subcontracts. The
// route requires AT LEAST ONE and returns ONLY the lanes the caller's caps allow (null for the
// others) — no new capability is minted, and commercial data (totals, contract prices) never
// reaches a session that couldn't open the lane pages themselves.
//
// READ-ONLY BY DOCTRINE: no send / approve / advance affordance exists here or in the SPA
// section (Invariant 1 — approval lives on the workspace share lists, enforced Mac-side by
// F22; 0051's header states send/execute approval is NOT a portal capability).
//
// ACCEPTED RESIDUAL RISK (adversarial review 2026-08-13): job_id and job_no are BOTH snapshots
// with no cross-consistency check — a tampered admin-tier draft call could pair a real job_no
// with a different job's job_id and surface the document on the wrong job's section (display-
// misleading only; numbering/HMAC/filing unaffected). Character-for-character the posture
// purchase_orders.job_id and subcontracts.job_id already carry; creation is admin-cap-gated.
//
// RFQs joined by rfqs.job_id (0075) — captured at draft time from now on; PRE-0075 rows carry
// '' and will never appear here (no backfill is possible — 0070's header; resolving by job_no
// would be the 0064 wrong-site corruption). The SPA hint states this.

const LANE_CAP_PO = "cap.po.manage";
const LANE_CAP_SUB = "cap.subcontracts.manage";
/** Per-lane row cap — a job's paper trail is dozens, not thousands; newest-first keeps the cap honest. */
const LANE_CAP_ROWS = 50;

export function registerJobProcurementRoutes(app: FieldopsApp, gates: FieldopsGates): void {
  app.get(
    "/api/fieldops/jobs/:job_id/procurement",
    gates.requireSession,
    async (c) => {
      const caps = c.get("capabilities");
      const canPo = caps.has(LANE_CAP_PO);
      const canSub = caps.has(LANE_CAP_SUB);
      if (!canPo && !canSub) return c.json({ error: "forbidden" }, 403);

      const jobId = c.req.param("job_id") ?? "";
      if (!jobId || jobId.length > 64) return c.json({ error: "invalid_job_id" }, 400);
      const job = await c.env.DB.prepare("SELECT 1 FROM jobs WHERE job_id = ?1").bind(jobId).first();
      if (!job) return c.json({ error: "unknown_job" }, 404);

      const stmts = [];
      if (canPo) {
        stmts.push(
          c.env.DB
            .prepare(
              // Vendor NAME server-side (never bare VEN-keys on a display surface); LEFT JOIN —
              // a vendor removed from the SoR must not drop the PO row.
              `SELECT p.id, p.po_number, p.revision, p.supersede_seq, p.status, p.total_cents,
                      p.updated_at, (p.box_file_id IS NOT NULL) AS filed,
                      p.vendor_key, v.vendor_name, p.accepted_at, p.accepted_by,
                      (SELECT COUNT(*) FROM procurement_change_orders co
                        WHERE co.doc_type='po' AND co.doc_id=p.id AND co.active=1) AS change_order_count
               FROM purchase_orders p LEFT JOIN po_vendors v ON v.vendor_key = p.vendor_key
               WHERE p.job_id = ?1
               ORDER BY p.updated_at DESC, p.id DESC LIMIT ${LANE_CAP_ROWS}`,
            )
            .bind(jobId),
          c.env.DB
            .prepare(
              // Per-vendor rollup counts ride each RFQ row — the vendor fan-out IS the RFQ's
              // lifecycle (the parent status alone hides a half-sent round).
              `SELECT r.id, r.rfq_number, r.status, r.due_date, r.updated_at,
                      (SELECT COUNT(*) FROM rfq_vendors rv WHERE rv.rfq_id = r.id AND rv.status != 'canceled') AS vendor_count,
                      (SELECT COUNT(*) FROM rfq_vendors rv WHERE rv.rfq_id = r.id AND rv.status = 'sent') AS sent_count,
                      (SELECT COUNT(*) FROM rfq_vendors rv WHERE rv.rfq_id = r.id AND rv.status = 'responded') AS responded_count
               FROM rfqs r
               WHERE r.job_id = ?1
               ORDER BY r.updated_at DESC, r.id DESC LIMIT ${LANE_CAP_ROWS}`,
            )
            .bind(jobId),
        );
      }
      if (canSub) {
        stmts.push(
          c.env.DB
            .prepare(
              `SELECT s.id, s.sc_number, s.revision, s.supersede_seq, s.status, s.trade,
                      s.contract_price_cents, s.updated_at, (s.box_file_id IS NOT NULL) AS filed,
                      s.sub_key, sub.sub_name, s.accepted_at, s.accepted_by,
                      (SELECT COUNT(*) FROM procurement_change_orders co
                        WHERE co.doc_type='subcontract' AND co.doc_id=s.id AND co.active=1) AS change_order_count
               FROM subcontracts s LEFT JOIN subcontractors sub ON sub.sub_key = s.sub_key
               WHERE s.job_id = ?1
               ORDER BY s.updated_at DESC, s.id DESC LIMIT ${LANE_CAP_ROWS}`,
            )
            .bind(jobId),
        );
      }
      const res = stmts.length > 0 ? await c.env.DB.batch(stmts) : [];

      let i = 0;
      let pos: JobProcurementPo[] | null = null;
      let rfqs: JobProcurementRfq[] | null = null;
      let subs: JobProcurementSub[] | null = null;
      if (canPo) {
        pos = ((res[i++].results ?? []) as Record<string, unknown>[]).map((r) => ({
          id: Number(r.id),
          po_number: (r.po_number as string | null) ?? null,
          revision: Number(r.revision ?? 0),
          supersede_seq: Number(r.supersede_seq ?? 0),
          status: String(r.status),
          total_cents: Number(r.total_cents ?? 0),
          updated_at: Number(r.updated_at ?? 0),
          filed: r.filed === 1,
          vendor_name: (r.vendor_name as string | null) ?? null,
          accepted_at: (r.accepted_at as string | null) ?? null,
          accepted_by: (r.accepted_by as string | null) ?? null,
          change_order_count: Number(r.change_order_count ?? 0),
        }));
        rfqs = ((res[i++].results ?? []) as Record<string, unknown>[]).map((r) => ({
          id: Number(r.id),
          rfq_number: (r.rfq_number as string | null) ?? null,
          status: String(r.status),
          due_date: (r.due_date as string | null) ?? null,
          updated_at: Number(r.updated_at ?? 0),
          vendor_count: Number(r.vendor_count ?? 0),
          sent_count: Number(r.sent_count ?? 0),
          responded_count: Number(r.responded_count ?? 0),
        }));
      }
      if (canSub) {
        subs = ((res[i++].results ?? []) as Record<string, unknown>[]).map((r) => ({
          id: Number(r.id),
          sc_number: (r.sc_number as string | null) ?? null,
          revision: Number(r.revision ?? 0),
          supersede_seq: Number(r.supersede_seq ?? 0),
          status: String(r.status),
          trade: String(r.trade ?? ""),
          contract_price_cents: Number(r.contract_price_cents ?? 0),
          updated_at: Number(r.updated_at ?? 0),
          filed: r.filed === 1,
          sub_name: (r.sub_name as string | null) ?? null,
          accepted_at: (r.accepted_at as string | null) ?? null,
          accepted_by: (r.accepted_by as string | null) ?? null,
          change_order_count: Number(r.change_order_count ?? 0),
        }));
      }
      const payload: JobProcurementResponse = { purchase_orders: pos, rfqs, subcontracts: subs };
      return c.json(payload, 200);
    },
  );

  // ── Lifecycle actions (Track D, operator directive 2026-08-14). ─────────────────────────────
  // The office tracks a document generated → submitted → accepted from the job's own screen.
  // POSTURE CHANGE (operator-sanctioned): the portal is now a SECOND, forward-only, audited
  // writer of the machine states it marks — every write is an exact-prior-state in-WHERE guard,
  // so it and the Mac status-sync (the same guard discipline) converge: whichever writes first
  // wins and the other no-ops. A wrong-state action is a 409 naming the current state, never a
  // silent no-op the office would misread as success.
  //
  // mark_submitted carries the SAME predecessor-supersession flip the Mac sync performs on its
  // own sent-write — without it, a manually-submitted revision would leave two in-force
  // documents. STILL SEND-FREE (Invariant 1): these record facts about paper that moved outside
  // ITS; nothing here transmits anything.
  app.post(
    "/api/fieldops/procurement/:doc_type/:id/lifecycle",
    gates.requireSession,
    async (c) => {
      const caps = c.get("capabilities");
      const docType = c.req.param("doc_type") ?? "";
      const id = parseInt(c.req.param("id"), 10);
      if (isNaN(id) || id < 1) return c.json({ error: "invalid_id" }, 400);
      let body: Record<string, unknown>;
      try {
        body = (await c.req.json()) as Record<string, unknown>;
      } catch {
        return c.json({ error: "bad_request" }, 400);
      }
      if (typeof body !== "object" || body === null || Array.isArray(body)) {
        return c.json({ error: "bad_request" }, 400);
      }
      const action = typeof body.action === "string" ? body.action : "";
      const actor = c.get("session").username;
      const today = new Date().toISOString().slice(0, 10);

      if (docType === "po") {
        if (!caps.has(LANE_CAP_PO)) return c.json({ error: "forbidden" }, 403);
        if (action === "mark_submitted") {
          const res = await c.env.DB.batch([
            c.env.DB
              .prepare(
                // approved-ONLY (review judgment call): matches the Mac sync's own guard, so a
                // manual mark can never skip the approval record — a pending_review document
                // 409s with its state; the office gets it approved first, then marks.
                "UPDATE purchase_orders SET status='sent', updated_at=unixepoch() " +
                  "WHERE id=?1 AND status='approved'",
              )
              .bind(id),
            auditStmtIfChanged(c, actor, "po_marked_submitted", String(id), { po_id: id, manual: true }),
            // The predecessor-supersession flip — byte-same rule as the Mac sync's sent-write.
            // ACCEPTED REPLAY SEMANTICS (review): on a replay whose primary write no-ops (already
            // sent → the route 409s), this flip can still fire and supersede a predecessor the
            // earlier attempt missed — deliberate CONVERGENCE toward the correct record (one
            // in-force document), identical to the sync's own latent shape, never a regression.
            c.env.DB
              .prepare(
                "UPDATE purchase_orders SET status='superseded', updated_at=unixepoch() " +
                  "WHERE id = (SELECT supersedes_po_id FROM purchase_orders WHERE id = ?1) " +
                  "AND status = 'sent' " +
                  "AND (SELECT status FROM purchase_orders WHERE id = ?1) = 'sent'",
              )
              .bind(id),
            auditStmtIfChanged(c, actor, "po_superseded_flip", String(id), { successor_po_id: id, manual: true }),
          ]);
          if ((res[0].meta.changes ?? 0) === 0) return refuseWrongState(c, "purchase_orders", id);
          return c.json({ ok: true });
        }
        if (action === "mark_accepted") {
          const res = await c.env.DB.batch([
            c.env.DB
              .prepare(
                "UPDATE purchase_orders SET accepted_at=?2, accepted_by=?3, updated_at=unixepoch() " +
                  "WHERE id=?1 AND status='sent' AND accepted_at IS NULL",
              )
              .bind(id, today, actor),
            auditStmtIfChanged(c, actor, "po_marked_accepted", String(id), { po_id: id }),
          ]);
          if ((res[0].meta.changes ?? 0) === 0) return refuseWrongState(c, "purchase_orders", id);
          return c.json({ ok: true });
        }
        if (action === "clear_accepted") {
          // The audited correction for a mis-click; the machine status is untouched.
          const res = await c.env.DB.batch([
            c.env.DB
              .prepare(
                "UPDATE purchase_orders SET accepted_at=NULL, accepted_by=NULL, updated_at=unixepoch() " +
                  "WHERE id=?1 AND accepted_at IS NOT NULL",
              )
              .bind(id),
            auditStmtIfChanged(c, actor, "po_accepted_cleared", String(id), { po_id: id }),
          ]);
          if ((res[0].meta.changes ?? 0) === 0) return refuseWrongState(c, "purchase_orders", id);
          return c.json({ ok: true });
        }
        return c.json({ error: "invalid_action" }, 400);
      }

      if (docType === "subcontract") {
        if (!caps.has(LANE_CAP_SUB)) return c.json({ error: "forbidden" }, 403);
        if (action === "mark_submitted") {
          const res = await c.env.DB.batch([
            c.env.DB
              .prepare(
                // approved-ONLY — same rule as the PO lane (see the comment there).
                "UPDATE subcontracts SET status='sent', updated_at=unixepoch() " +
                  "WHERE id=?1 AND status='approved'",
              )
              .bind(id),
            auditStmtIfChanged(c, actor, "sc_marked_submitted", String(id), { sc_id: id, manual: true }),
            c.env.DB
              .prepare(
                "UPDATE subcontracts SET status='superseded', updated_at=unixepoch() " +
                  "WHERE id = (SELECT supersedes_sc_id FROM subcontracts WHERE id = ?1) " +
                  "AND status IN ('sent','executed') " +
                  "AND (SELECT status FROM subcontracts WHERE id = ?1) = 'sent'",
              )
              .bind(id),
            auditStmtIfChanged(c, actor, "sc_superseded_flip", String(id), { successor_sc_id: id, manual: true }),
          ]);
          if ((res[0].meta.changes ?? 0) === 0) return refuseWrongState(c, "subcontracts", id);
          return c.json({ ok: true });
        }
        if (action === "mark_accepted") {
          // Accepted == the machine's countersign terminal ('executed', in the 0050 CHECK).
          // The ledger sync's own executed-write (also guarded from 'sent') converges — the
          // later writer no-ops. accepted_at/by record WHO logged the fact and when.
          const res = await c.env.DB.batch([
            c.env.DB
              .prepare(
                "UPDATE subcontracts SET status='executed', accepted_at=?2, accepted_by=?3, " +
                  "updated_at=unixepoch() WHERE id=?1 AND status='sent'",
              )
              .bind(id, today, actor),
            auditStmtIfChanged(c, actor, "sc_marked_accepted", String(id), { sc_id: id }),
          ]);
          if ((res[0].meta.changes ?? 0) === 0) return refuseWrongState(c, "subcontracts", id);
          return c.json({ ok: true });
        }
        return c.json({ error: "invalid_action" }, 400);
      }

      if (docType === "rfq") {
        if (!caps.has(LANE_CAP_PO)) return c.json({ error: "forbidden" }, 403);
        if (action === "close") {
          // The 0056 CHECK has carried 'closed' since birth with NO writer (the A8 recon's
          // dead-state finding) — this is that writer: the office retires a finished round.
          const res = await c.env.DB.batch([
            c.env.DB
              .prepare(
                "UPDATE rfqs SET status='closed', updated_at=unixepoch() " +
                  "WHERE id=?1 AND status IN ('generated','partially_sent','sent')",
              )
              .bind(id),
            auditStmtIfChanged(c, actor, "rfq_closed", String(id), { rfq_id: id }),
          ]);
          if ((res[0].meta.changes ?? 0) === 0) return refuseWrongState(c, "rfqs", id);
          return c.json({ ok: true });
        }
        return c.json({ error: "invalid_action" }, 400);
      }

      return c.json({ error: "invalid_doc_type" }, 400);
    },
  );

  // ── Change orders (Track D). ────────────────────────────────────────────────────────────────
  // Recorded against a PO or subcontract; job_id denormalized SERVER-SIDE from the parent row
  // (never client-supplied — a hostile job_id could not misfile a CO). seq is per-document,
  // assigned inside the INSERT (MAX+1) so two concurrent creates cannot collide silently — the
  // UNIQUE(doc_type, doc_id, seq) makes the loser retry visible.
  app.post("/api/fieldops/procurement/change-orders", gates.requireSession, async (c) => {
    const caps = c.get("capabilities");
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (typeof body !== "object" || body === null || Array.isArray(body)) {
      return c.json({ error: "bad_request" }, 400);
    }
    const docType = body.doc_type === "po" || body.doc_type === "subcontract" ? body.doc_type : null;
    const docId = typeof body.doc_id === "number" && Number.isSafeInteger(body.doc_id) && body.doc_id > 0 ? body.doc_id : null;
    const description = typeof body.description === "string" ? body.description.trim().slice(0, 2000) : "";
    const amount = typeof body.amount_cents === "number" && Number.isSafeInteger(body.amount_cents) &&
      Math.abs(body.amount_cents) <= 1_000_000_000_00 ? body.amount_cents : null;
    if (docType === null || docId === null || description === "" || amount === null) {
      return c.json({ error: "invalid_change_order" }, 400);
    }
    if (!caps.has(docType === "po" ? LANE_CAP_PO : LANE_CAP_SUB)) return c.json({ error: "forbidden" }, 403);
    const parentTable = docType === "po" ? "purchase_orders" : "subcontracts";
    const parent = await c.env.DB
      .prepare(`SELECT job_id FROM ${parentTable} WHERE id = ?1`)
      .bind(docId)
      .first<{ job_id: string }>();
    if (!parent) return c.json({ error: "unknown_document" }, 404);
    const actor = c.get("session").username;
    let res;
    try {
      res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            "INSERT INTO procurement_change_orders (job_id, doc_type, doc_id, seq, description, amount_cents, created_by) " +
              "SELECT ?1, ?2, ?3, COALESCE((SELECT MAX(seq) FROM procurement_change_orders WHERE doc_type=?2 AND doc_id=?3), 0) + 1, ?4, ?5, ?6 " +
              "RETURNING id, seq",
          )
          .bind(parent.job_id, docType, docId, description, amount, actor),
        auditStmtIfChanged(c, actor, "change_order_create", `${docType}:${docId}`, {
          doc_type: docType, doc_id: docId, amount_cents: amount,
        }),
      ]);
    } catch (e) {
      // The MAX+1 race-loser under UNIQUE(doc_type, doc_id, seq): a clean retriable 409, never
      // a generic 500 (review W5 — the po_number_conflict pattern).
      if (isUniqueViolation(e)) return c.json({ error: "change_order_seq_conflict" }, 409);
      throw e;
    }
    const row = (res[0].results?.[0] ?? null) as { id: number; seq: number } | null;
    if (!row) return c.json({ error: "internal_error" }, 500);
    return c.json({ ok: true, id: row.id, seq: row.seq }, 201);
  });

  app.post("/api/fieldops/procurement/change-orders/:id/decide", gates.requireSession, gates.requireAnyCapability([LANE_CAP_PO, LANE_CAP_SUB]), async (c) => {
    const id = parseInt(c.req.param("id"), 10);
    if (isNaN(id) || id < 1) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (typeof body !== "object" || body === null || Array.isArray(body)) {
      return c.json({ error: "bad_request" }, 400);
    }
    const status = body.status === "approved" || body.status === "rejected" ? (body.status as string) : null;
    if (status === null) return c.json({ error: "invalid_status" }, 400);
    const gateErr = await requireCoLaneCap(c, id);
    if (gateErr) return gateErr;
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE procurement_change_orders SET status=?2, decided_by=?3, decided_at=unixepoch() " +
            "WHERE id=?1 AND active=1 AND status='pending'",
        )
        .bind(id, status, actor),
      auditStmtIfChanged(c, actor, "change_order_decide", String(id), { change_order_id: id, status }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_pending" }, 409);
    return c.json({ ok: true });
  });

  app.post("/api/fieldops/procurement/change-orders/:id/deactivate", gates.requireSession, gates.requireAnyCapability([LANE_CAP_PO, LANE_CAP_SUB]), async (c) => {
    const id = parseInt(c.req.param("id"), 10);
    if (isNaN(id) || id < 1) return c.json({ error: "invalid_id" }, 400);
    const gateErr = await requireCoLaneCap(c, id);
    if (gateErr) return gateErr;
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare("UPDATE procurement_change_orders SET active=0 WHERE id=?1 AND active=1")
        .bind(id),
      auditStmtIfChanged(c, actor, "change_order_deactivate", String(id), { change_order_id: id }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
    return c.json({ ok: true });
  });

  // Per-document change-order list (the item screen's read).
  app.get("/api/fieldops/procurement/:doc_type/:id/change-orders", gates.requireSession, async (c) => {
    const docType = c.req.param("doc_type") ?? "";
    const id = parseInt(c.req.param("id"), 10);
    if ((docType !== "po" && docType !== "subcontract") || isNaN(id) || id < 1) {
      return c.json({ error: "invalid_id" }, 400);
    }
    const caps = c.get("capabilities");
    if (!caps.has(docType === "po" ? LANE_CAP_PO : LANE_CAP_SUB)) return c.json({ error: "forbidden" }, 403);
    const rows = await c.env.DB
      .prepare(
        "SELECT id, seq, description, amount_cents, status, created_by, created_at, decided_by, decided_at " +
          "FROM procurement_change_orders WHERE doc_type=?1 AND doc_id=?2 AND active=1 ORDER BY seq ASC",
      )
      .bind(docType, id)
      .all();
    return c.json({ change_orders: rows.results ?? [] }, 200);
  });
}

/** 409 naming the document's CURRENT state — a wrong-state lifecycle action must never read as
 *  success or as a silent no-op (the office is recording facts; tell them what the record says). */
async function refuseWrongState(
  c: Context<{ Bindings: Env; Variables: Vars }>,
  table: "purchase_orders" | "subcontracts" | "rfqs",
  id: number,
) {
  const row = await c.env.DB.prepare(`SELECT status FROM ${table} WHERE id = ?1`).bind(id).first<{ status: string }>();
  if (!row) return c.json({ error: "unknown_document" }, 404);
  return c.json({ error: "wrong_state", current_status: row.status }, 409);
}

/** The CO write gate: resolve the CO's lane, require that lane's own capability. Runs BEHIND
 *  the requireAnyCapability([PO, SUB]) middleware floor (review W6) — an uncapable session is
 *  403d before this SELECT, so row existence never leaks below the lane tier. */
async function requireCoLaneCap(
  c: Context<{ Bindings: Env; Variables: Vars }>,
  coId: number,
) {
  const row = await c.env.DB
    .prepare("SELECT doc_type FROM procurement_change_orders WHERE id = ?1")
    .bind(coId)
    .first<{ doc_type: string }>();
  if (!row) return c.json({ error: "not_found" }, 404);
  const caps = c.get("capabilities");
  if (!caps.has(row.doc_type === "po" ? LANE_CAP_PO : LANE_CAP_SUB)) return c.json({ error: "forbidden" }, 403);
  return null;
}

