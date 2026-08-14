import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
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
                      p.vendor_key, v.vendor_name
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
                      s.sub_key, sub.sub_name
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
        }));
      }
      const payload: JobProcurementResponse = { purchase_orders: pos, rfqs, subcontracts: subs };
      return c.json(payload, 200);
    },
  );
}
