import type { Context } from "hono";
import type { Env, Vars } from "./types";
import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { requireJob } from "./fieldops_scope";
import { pacificDateString } from "./fieldops_recurrence";
import { computeDueDate, derivePaymentState } from "./payments_derive";
import type {
  PaymentCycleCreateResponse,
  PaymentCycleView,
  PaymentReceiptRow,
  PaymentsResponse,
  PaymentTermsDefaultResponse,
  PaymentTermsRow,
} from "./wire-types";

// ─────────────────────────────────────────────────────────────────────────────
// Job payments (ADR-0006 decision 10, PR-7) — worker/fieldops_payments.ts
//
// Per-job payment terms + manual invoice cycles + append-only receipts, over migration
// 0073, with states DERIVED at read (payments_derive.ts — never stored). DISPLAY-ONLY:
// nothing here transmits, reminds, or generates a notice document (Invariant 1 — the
// ADR's alert engine is a later fold-in over this same schema). Send-free throughout
// (D1 writes only); bound params only; every mutation lands with its audit_log row in
// ONE D1 batch (W4); in-WHERE active=1 guards on every write; bounded readers.
//
// EVERY route gates session + cap.payments.manage — granted to ADMIN ONLY (0073;
// operator decision 4: commercially sensitive). Payment data appears in NO other
// route's response; the fieldops-payments suite RED-proofs both directions.
//
// THE DUE-DATE SNAPSHOT (decision 10): due_date is STORED at cycle create/edit as
// submitted + net_days from the terms OF THAT MOMENT. A later terms edit must NOT move
// an already-invoiced cycle's due date (the invoice went out under its day's terms);
// the snapshot recomputes ONLY when its OWN input — the submitted date — changes
// through /cycles/:id/edit.
//
// NOTICE RECORDING (decision 3 / operator decision 3): the /notice route RECORDS that a
// human sent a notice outside ITS — it never sends one. `suspend` REQUIRES a recorded
// nonpayment notice: enforced IN-WHERE (atomic with the write, the milestone_binary
// discipline), with an honest 409 suspend_requires_nonpayment when that is why zero
// rows changed. A re-post updates the date (a correction is real), audited from/to.
//
// ORDER DEPENDENCY: migration 0073 must be applied to the live D1 BEFORE this Worker
// deploys — the routes 500 without the tables, and resolveCapabilities (fail-closed)
// 403s every admin until the capability row lands. (And `git -C ~/its pull origin
// main` FIRST — the stale-migrations-list lockout class.)
// ─────────────────────────────────────────────────────────────────────────────

type Ctx = Context<{ Bindings: Env; Variables: Vars }>;

const CAP_PAYMENTS = "cap.payments.manage";

// Bounds. Money is INTEGER CENTS everywhere (the 0043 D8 rule); the cents cap is JS
// safe-integer paranoia sized far above any real invoice ($90T) — the point is that a
// bound EXISTS, not where it sits.
const MAX_LABEL = 120;
const MAX_CADENCE_DETAIL = 300;
const MAX_NOTE = 2000;
const MAX_AMOUNT_CENTS = 9_000_000_000_000_000;
const MAX_NET_DAYS = 365;
const MAX_NOTICE_DAYS = 365;
const CADENCES = ["monthly", "semimonthly", "milestone", "other"] as const;
// Read caps: a job with 200 payment cycles or 1000 receipts is far past anything real —
// the reader is bounded so a runaway insert loop cannot balloon the response.
const CYCLES_READ_CAP = 200;
const RECEIPTS_READ_CAP = 1000;

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

async function readJsonBody(c: Ctx): Promise<Record<string, unknown> | null> {
  let body: unknown;
  try {
    body = await c.req.json();
  } catch {
    return null;
  }
  return isPlainObject(body) ? body : null;
}

function parseIdParam(raw: string | undefined): number | null {
  const id = parseInt(raw ?? "", 10);
  return Number.isSafeInteger(id) && id > 0 && String(id) === (raw ?? "") ? id : null;
}

function optBoundedText(v: unknown, max: number): string | null | "bad" {
  if (v === undefined || v === null) return null;
  if (typeof v !== "string" || v.length > max) return "bad";
  const t = v.trim();
  return t.length ? t : null;
}

function optIsoDate(v: unknown): string | null | "bad" {
  if (v === undefined || v === null || v === "") return null;
  if (typeof v !== "string" || !DATE_RE.test(v)) return "bad";
  return v;
}

function intInRange(v: unknown, min: number, max: number): number | null {
  return typeof v === "number" && Number.isSafeInteger(v) && v >= min && v <= max ? v : null;
}

/** The terms columns every read serves — WHO columns (created_by/updated_by) stay
 *  Worker-side (raw account usernames never serve; the W9 posture). */
const TERMS_COLS =
  "job_id, billing_cadence, billing_cadence_detail, net_days, nonpayment_notice_days, " +
  "intent_to_suspend_days, notes, created_at, updated_at";

type TermsDbRow = PaymentTermsRow & { nonpayment_notice_days: number; intent_to_suspend_days: number };

type CycleDbRow = {
  id: number;
  cycle_uuid: string;
  job_id: string;
  seq: number;
  label: string;
  invoice_submitted_date: string | null;
  invoice_amount_cents: number | null;
  due_date: string | null;
  nonpayment_notice_date: string | null;
  suspend_notice_date: string | null;
  note: string | null;
  created_at: number;
  updated_at: number;
};

const CYCLE_COLS =
  "id, cycle_uuid, job_id, seq, label, invoice_submitted_date, invoice_amount_cents, " +
  "due_date, nonpayment_notice_date, suspend_notice_date, note, created_at, updated_at";

/** The validated content of a terms upsert — the one reader both PUT and any later bulk
 *  path run, so bounds can't drift (the readScheduleTaskFields precedent). */
type TermsFields = {
  billing_cadence: (typeof CADENCES)[number];
  billing_cadence_detail: string | null;
  net_days: number;
  nonpayment_notice_days: number;
  intent_to_suspend_days: number;
  notes: string | null;
};

function readTermsFields(body: Record<string, unknown>): TermsFields | string {
  const cadence = body.billing_cadence === undefined ? "monthly" : body.billing_cadence;
  if (typeof cadence !== "string" || !(CADENCES as readonly string[]).includes(cadence)) {
    // NOT the bare `invalid_cadence` — that wire code is the checklist recurrence's.
    return "invalid_billing_cadence";
  }
  const detail = optBoundedText(body.billing_cadence_detail, MAX_CADENCE_DETAIL);
  if (detail === "bad") return "invalid_cadence_detail";
  const net_days = intInRange(body.net_days, 0, MAX_NET_DAYS);
  if (net_days === null) return "invalid_net_days";
  const nonpayment_notice_days = intInRange(body.nonpayment_notice_days, 1, MAX_NOTICE_DAYS);
  if (nonpayment_notice_days === null) return "invalid_notice_days";
  const intent_to_suspend_days = intInRange(body.intent_to_suspend_days, 1, MAX_NOTICE_DAYS);
  if (intent_to_suspend_days === null) return "invalid_notice_days";
  const notes = optBoundedText(body.notes, MAX_NOTE);
  if (notes === "bad") return "invalid_payment_note";
  return {
    billing_cadence: cadence as TermsFields["billing_cadence"],
    billing_cadence_detail: detail,
    net_days, nonpayment_notice_days, intent_to_suspend_days, notes,
  };
}

export function registerPaymentRoutes(app: FieldopsApp, gates: FieldopsGates): void {
  const gate = [gates.requireSession, gates.requireCapability(CAP_PAYMENTS)] as const;

  /** The mutation routes' pre-load: the ACTIVE cycle row (404 anchor + audit from/to). */
  async function loadActiveCycle(c: Ctx, id: number): Promise<CycleDbRow | null> {
    return await c.env.DB
      .prepare(`SELECT ${CYCLE_COLS} FROM job_payment_cycles WHERE id = ?1 AND active = 1`)
      .bind(id)
      .first<CycleDbRow>();
  }

  async function loadTerms(c: Ctx, jobId: string): Promise<TermsDbRow | null> {
    return await c.env.DB
      .prepare(`SELECT ${TERMS_COLS} FROM job_payment_terms WHERE job_id = ?1`)
      .bind(jobId)
      .first<TermsDbRow>();
  }

  // ── GET /api/fieldops/payments?job_id — the whole Payments card in one read. ────────
  // Terms (or null) + active cycles in display order, each with its active receipts and
  // the DERIVED state (payments_derive, server `today` Pacific — the same calendar the
  // delivered mark stamps). project_name because the section is deep-linkable.
  app.get("/api/fieldops/payments", ...gate, async (c) => {
    const jobId = typeof c.req.query("job_id") === "string" ? (c.req.query("job_id") ?? "") : "";
    const jobErr = await requireJob(c, jobId); // 400 bad shape / 404 unknown job
    if (jobErr) return jobErr;
    const [termsRes, cyclesRes, receiptsRes, jobRow] = await c.env.DB.batch([
      c.env.DB.prepare(`SELECT ${TERMS_COLS} FROM job_payment_terms WHERE job_id = ?1`).bind(jobId),
      c.env.DB
        .prepare(
          `SELECT ${CYCLE_COLS} FROM job_payment_cycles
            WHERE job_id = ?1 AND active = 1
            ORDER BY seq ASC, id ASC LIMIT ${CYCLES_READ_CAP}`,
        )
        .bind(jobId),
      // Receipts for ALL the job's active cycles in one bounded read (grouped in JS) —
      // recorded_by stays Worker-side (W9 posture).
      c.env.DB
        .prepare(
          `SELECT r.id, r.cycle_id, r.received_date, r.amount_cents, r.note, r.recorded_at
             FROM job_payment_receipts r
            WHERE r.active = 1
              AND r.cycle_id IN (SELECT id FROM job_payment_cycles WHERE job_id = ?1 AND active = 1)
            ORDER BY r.received_date ASC, r.id ASC LIMIT ${RECEIPTS_READ_CAP}`,
        )
        .bind(jobId),
      c.env.DB.prepare("SELECT project_name FROM jobs WHERE job_id = ?1").bind(jobId),
    ]);
    const terms = (termsRes.results?.[0] as TermsDbRow | undefined) ?? null;
    const cycleRows = (cyclesRes.results ?? []) as CycleDbRow[];
    const receiptRows = (receiptsRes.results ?? []) as (PaymentReceiptRow & { cycle_id: number })[];
    const byCycle = new Map<number, PaymentReceiptRow[]>();
    for (const r of receiptRows) {
      const list = byCycle.get(r.cycle_id) ?? [];
      list.push({
        id: r.id, received_date: r.received_date, amount_cents: r.amount_cents,
        note: r.note, recorded_at: r.recorded_at,
      });
      byCycle.set(r.cycle_id, list);
    }
    const today = pacificDateString(Date.now());
    const cycles: PaymentCycleView[] = cycleRows.map((row) => {
      const receipts = byCycle.get(row.id) ?? [];
      const total = receipts.reduce((sum, r) => sum + r.amount_cents, 0);
      // The receipts read is date-ordered ASC, so the last element is the latest date.
      const lastReceiptDate = receipts.length ? receipts[receipts.length - 1].received_date : null;
      const d = derivePaymentState(row, total, lastReceiptDate, terms, today);
      return {
        ...row, receipts,
        state: d.state, balance_cents: d.balance_cents,
        days_overdue: d.days_overdue, days_until_due: d.days_until_due, paid_late: d.paid_late,
      };
    });
    const payload: PaymentsResponse = {
      terms,
      cycles,
      project_name:
        ((jobRow.results?.[0] as { project_name?: string } | undefined)?.project_name) ?? null,
    };
    return c.json(payload, 200);
  });

  // ── GET /api/fieldops/payments/terms-default?job_id — the same-client prefill. ──────
  // Operator decision 6: a new job's terms start from the SAME CLIENT's most recent
  // other job's terms (jobs.client_id → clients, 0014). Read-only, never errors on a
  // missing client — {terms:null} is the honest "nothing to prefill".
  app.get("/api/fieldops/payments/terms-default", ...gate, async (c) => {
    const jobId = typeof c.req.query("job_id") === "string" ? (c.req.query("job_id") ?? "") : "";
    const jobErr = await requireJob(c, jobId);
    if (jobErr) return jobErr;
    const job = await c.env.DB
      .prepare(
        "SELECT j.client_id, (SELECT name FROM clients WHERE id = j.client_id) AS client_name " +
          "FROM jobs j WHERE j.job_id = ?1",
      )
      .bind(jobId)
      .first<{ client_id: number | null; client_name: string | null }>();
    const empty: PaymentTermsDefaultResponse = { terms: null, client_name: null, source_project_name: null };
    if (!job || job.client_id === null) return c.json(empty, 200);
    // Most recent OTHER job of the same client that HAS terms (jobs.created_at orders
    // recency — the prefill wants the client's latest negotiated terms, not the oldest).
    const src = await c.env.DB
      .prepare(
        `SELECT t.billing_cadence, t.billing_cadence_detail, t.net_days,
                t.nonpayment_notice_days, t.intent_to_suspend_days, t.notes,
                t.created_at, t.updated_at, j.project_name
           FROM job_payment_terms t JOIN jobs j ON j.job_id = t.job_id
          WHERE j.client_id = ?1 AND t.job_id <> ?2
          ORDER BY j.created_at DESC LIMIT 1`,
      )
      .bind(job.client_id, jobId)
      .first<Omit<PaymentTermsRow, "job_id"> & { project_name: string }>();
    if (!src) return c.json({ ...empty, client_name: job.client_name }, 200);
    const { project_name, ...terms } = src;
    const payload: PaymentTermsDefaultResponse = {
      terms, client_name: job.client_name, source_project_name: project_name,
    };
    return c.json(payload, 200);
  });

  // ── PUT /api/fieldops/payments/terms — upsert the job's ONE terms row. ──────────────
  // INSERT .. ON CONFLICT(job_id) DO UPDATE: create and edit are the same idempotent
  // write (the 0073 UNIQUE is the anchor), so there is no create/edit race to lose.
  // NOTE the deliberate NON-effect: editing terms does NOT touch any cycle's stored
  // due_date snapshot (module header) — the fieldops-payments suite pins that.
  app.put("/api/fieldops/payments/terms", ...gate, async (c) => {
    const body = await readJsonBody(c);
    if (body === null) return c.json({ error: "bad_request" }, 400);
    const jobId = typeof body.job_id === "string" ? body.job_id : "";
    const jobErr = await requireJob(c, jobId);
    if (jobErr) return jobErr;
    const f = readTermsFields(body);
    if (typeof f === "string") return c.json({ error: f }, 400);

    const actor = c.get("session").username;
    await c.env.DB.batch([
      c.env.DB
        .prepare(
          `INSERT INTO job_payment_terms
             (job_id, billing_cadence, billing_cadence_detail, net_days,
              nonpayment_notice_days, intent_to_suspend_days, notes, created_by, updated_by)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8)
           ON CONFLICT(job_id) DO UPDATE SET
             billing_cadence = ?2, billing_cadence_detail = ?3, net_days = ?4,
             nonpayment_notice_days = ?5, intent_to_suspend_days = ?6, notes = ?7,
             updated_by = ?8, updated_at = unixepoch()`,
        )
        .bind(
          jobId, f.billing_cadence, f.billing_cadence_detail, f.net_days,
          f.nonpayment_notice_days, f.intent_to_suspend_days, f.notes, actor,
        ),
      auditStmt(c, actor, "payment_terms_upsert", jobId, {
        job_id: jobId, billing_cadence: f.billing_cadence, net_days: f.net_days,
        nonpayment_notice_days: f.nonpayment_notice_days,
        intent_to_suspend_days: f.intent_to_suspend_days,
      }),
    ]);
    const terms = await loadTerms(c, jobId);
    return c.json({ ok: true, terms }, 200);
  });

  // ── POST /api/fieldops/payments/cycles — create (label required, the rest optional).
  // due_date snapshot computed HERE, server-side, IF a submitted date arrived AND terms
  // exist; a submitted cycle without terms lands with due_date NULL and an explicit
  // response note (never a silent NULL). seq = max+10 (the schedule-task shape).
  app.post("/api/fieldops/payments/cycles", ...gate, async (c) => {
    const body = await readJsonBody(c);
    if (body === null) return c.json({ error: "bad_request" }, 400);
    const jobId = typeof body.job_id === "string" ? body.job_id : "";
    const jobErr = await requireJob(c, jobId);
    if (jobErr) return jobErr;
    if (typeof body.label !== "string") return c.json({ error: "invalid_cycle_label" }, 400);
    const label = body.label.trim();
    if (label.length < 1 || label.length > MAX_LABEL) return c.json({ error: "invalid_cycle_label" }, 400);
    const submitted = optIsoDate(body.invoice_submitted_date);
    if (submitted === "bad") return c.json({ error: "invalid_submitted_date" }, 400);
    let amount: number | null = null;
    if (body.invoice_amount_cents !== undefined && body.invoice_amount_cents !== null) {
      amount = intInRange(body.invoice_amount_cents, 0, MAX_AMOUNT_CENTS);
      if (amount === null) return c.json({ error: "invalid_amount_cents" }, 400);
    }
    const note = optBoundedText(body.note, MAX_NOTE);
    if (note === "bad") return c.json({ error: "invalid_payment_note" }, 400);

    const terms = await loadTerms(c, jobId);
    const dueDate = submitted !== null && terms !== null ? computeDueDate(submitted, terms.net_days) : null;

    const actor = c.get("session").username;
    const cycleUuid = crypto.randomUUID();
    let res;
    try {
      res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `INSERT INTO job_payment_cycles
               (cycle_uuid, job_id, seq, label, invoice_submitted_date, invoice_amount_cents,
                due_date, note, created_by, updated_by)
             VALUES (?1, ?2,
                     (SELECT COALESCE(MAX(seq), 0) + 10 FROM job_payment_cycles WHERE job_id = ?2 AND active = 1),
                     ?3, ?4, ?5, ?6, ?7, ?8, ?8)
             RETURNING id`,
          )
          .bind(cycleUuid, jobId, label, submitted, amount, dueDate, note, actor),
        auditStmt(c, actor, "payment_cycle_create", jobId, {
          job_id: jobId, cycle_uuid: cycleUuid, label,
          invoice_submitted_date: submitted, invoice_amount_cents: amount, due_date: dueDate,
        }),
      ]);
    } catch (e) {
      if (isUniqueViolation(e)) return c.json({ error: "uuid_conflict" }, 409);
      throw e;
    }
    const newId = (res[0].results?.[0] as { id: number } | undefined)?.id ?? null;
    const payload: PaymentCycleCreateResponse = {
      ok: true, id: newId, cycle_uuid: cycleUuid, due_date: dueDate,
      ...(submitted !== null && terms === null ? { note: "no_terms_no_due_date" as const } : {}),
    };
    return c.json(payload, 201);
  });

  // ── POST /api/fieldops/payments/cycles/:id/edit — label / amount / submitted / note. ─
  // THE SNAPSHOT RULE (decision 10): due_date recomputes from the CURRENT terms ONLY
  // when the submitted date CHANGES — the snapshot updates when ITS OWN input changes,
  // never as a side effect of a terms edit. An unchanged submitted date keeps the
  // stored due_date byte-for-byte, even if the terms moved since. Clearing the
  // submitted date (back to draft) clears the snapshot with it.
  app.post("/api/fieldops/payments/cycles/:id/edit", ...gate, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const body = await readJsonBody(c);
    if (body === null) return c.json({ error: "bad_request" }, 400);
    if (typeof body.label !== "string") return c.json({ error: "invalid_cycle_label" }, 400);
    const label = body.label.trim();
    if (label.length < 1 || label.length > MAX_LABEL) return c.json({ error: "invalid_cycle_label" }, 400);
    const submitted = optIsoDate(body.invoice_submitted_date);
    if (submitted === "bad") return c.json({ error: "invalid_submitted_date" }, 400);
    let amount: number | null = null;
    if (body.invoice_amount_cents !== undefined && body.invoice_amount_cents !== null) {
      amount = intInRange(body.invoice_amount_cents, 0, MAX_AMOUNT_CENTS);
      if (amount === null) return c.json({ error: "invalid_amount_cents" }, 400);
    }
    const note = optBoundedText(body.note, MAX_NOTE);
    if (note === "bad") return c.json({ error: "invalid_payment_note" }, 400);

    const cycle = await loadActiveCycle(c, id);
    if (!cycle) return c.json({ error: "not_found" }, 404);
    let dueDate = cycle.due_date;
    if (submitted !== cycle.invoice_submitted_date) {
      const terms = submitted !== null ? await loadTerms(c, cycle.job_id) : null;
      dueDate = submitted !== null && terms !== null ? computeDueDate(submitted, terms.net_days) : null;
    }

    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          `UPDATE job_payment_cycles
              SET label = ?2, invoice_submitted_date = ?3, invoice_amount_cents = ?4,
                  due_date = ?5, note = ?6, updated_by = ?7, updated_at = unixepoch()
            WHERE id = ?1 AND active = 1`,
        )
        .bind(id, label, submitted, amount, dueDate, note, actor),
      auditStmtIfChanged(c, actor, "payment_cycle_edit", cycle.job_id, {
        cycle_uuid: cycle.cycle_uuid, job_id: cycle.job_id, label,
        from_submitted: cycle.invoice_submitted_date, to_submitted: submitted,
        from_amount: cycle.invoice_amount_cents, to_amount: amount,
        from_due: cycle.due_date, to_due: dueDate,
      }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
    return c.json({ ok: true, id, due_date: dueDate }, 200);
  });

  // ── POST /api/fieldops/payments/cycles/:id/notice — RECORD a notice date. ────────────
  // body {kind: 'nonpayment'|'suspend', date?} (defaults today Pacific). Records that a
  // human sent the document OUTSIDE ITS — nothing is generated or transmitted here
  // (Invariant 1). suspend REQUIRES a recorded nonpayment notice, re-asserted IN-WHERE
  // (atomic with the write — a concurrent nonpayment-date clear cannot slip a suspend
  // record past a stale pre-load), and the zero-changes arm answers an honest 409.
  // A re-post updates the date (a correction is real); audit carries from/to.
  app.post("/api/fieldops/payments/cycles/:id/notice", ...gate, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const body = await readJsonBody(c);
    if (body === null) return c.json({ error: "bad_request" }, 400);
    const kind = body.kind;
    if (kind !== "nonpayment" && kind !== "suspend") return c.json({ error: "invalid_notice_kind" }, 400);
    let date: string;
    if (body.date === undefined || body.date === null || body.date === "") {
      date = pacificDateString(Date.now());
    } else if (typeof body.date === "string" && DATE_RE.test(body.date)) {
      date = body.date;
    } else {
      return c.json({ error: "invalid_notice_date" }, 400);
    }
    const cycle = await loadActiveCycle(c, id);
    if (!cycle) return c.json({ error: "not_found" }, 404);

    const actor = c.get("session").username;
    const from = kind === "nonpayment" ? cycle.nonpayment_notice_date : cycle.suspend_notice_date;
    const stmt =
      kind === "nonpayment"
        ? c.env.DB
            .prepare(
              `UPDATE job_payment_cycles
                  SET nonpayment_notice_date = ?2, updated_by = ?3, updated_at = unixepoch()
                WHERE id = ?1 AND active = 1`,
            )
            .bind(id, date, actor)
        : c.env.DB
            .prepare(
              // The suspend precondition lives IN-WHERE (see route header).
              `UPDATE job_payment_cycles
                  SET suspend_notice_date = ?2, updated_by = ?3, updated_at = unixepoch()
                WHERE id = ?1 AND active = 1 AND nonpayment_notice_date IS NOT NULL`,
            )
            .bind(id, date, actor);
    const res = await c.env.DB.batch([
      stmt,
      auditStmtIfChanged(c, actor, `payment_notice_${kind}`, cycle.job_id, {
        cycle_uuid: cycle.cycle_uuid, job_id: cycle.job_id, from, to: date,
      }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      // Zero changes = the guard bit or the row went away — say which, honestly (the
      // milestone-done shape): re-load and type the refusal.
      const now = await loadActiveCycle(c, id);
      if (!now) return c.json({ error: "not_found" }, 404);
      return c.json({ error: "suspend_requires_nonpayment" }, 409);
    }
    return c.json({ ok: true, id, kind, date }, 200);
  });

  // ── POST /api/fieldops/payments/cycles/:id/receipt — APPEND a money-received event. ──
  // {received_date? (defaults today Pacific), amount_cents > 0, note?}. Append-only:
  // partial payments and retainage are just more events; a correction is deactivate +
  // re-add (the 0073 posture). The derived state moves on its own at the next read.
  app.post("/api/fieldops/payments/cycles/:id/receipt", ...gate, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const body = await readJsonBody(c);
    if (body === null) return c.json({ error: "bad_request" }, 400);
    let receivedDate: string;
    if (body.received_date === undefined || body.received_date === null || body.received_date === "") {
      receivedDate = pacificDateString(Date.now());
    } else if (typeof body.received_date === "string" && DATE_RE.test(body.received_date)) {
      receivedDate = body.received_date;
    } else {
      return c.json({ error: "invalid_received_date" }, 400);
    }
    const amount = intInRange(body.amount_cents, 1, MAX_AMOUNT_CENTS);
    if (amount === null) return c.json({ error: "invalid_amount_cents" }, 400);
    const note = optBoundedText(body.note, MAX_NOTE);
    if (note === "bad") return c.json({ error: "invalid_payment_note" }, 400);
    const cycle = await loadActiveCycle(c, id);
    if (!cycle) return c.json({ error: "not_found" }, 404);

    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          `INSERT INTO job_payment_receipts (cycle_id, received_date, amount_cents, note, recorded_by)
           SELECT ?1, ?2, ?3, ?4, ?5
            WHERE EXISTS (SELECT 1 FROM job_payment_cycles WHERE id = ?1 AND active = 1)
           RETURNING id`,
        )
        .bind(id, receivedDate, amount, note, actor),
      auditStmtIfChanged(c, actor, "payment_receipt_add", cycle.job_id, {
        cycle_uuid: cycle.cycle_uuid, job_id: cycle.job_id,
        received_date: receivedDate, amount_cents: amount,
      }),
    ]);
    const newId = (res[0].results?.[0] as { id: number } | undefined)?.id;
    // The guarded INSERT wrote nothing ⇔ the cycle was deactivated between the load and
    // the batch (the in-WHERE re-assert; a receipt on a dead cycle would be orphaned).
    if (newId === undefined) return c.json({ error: "not_found" }, 404);
    return c.json({ ok: true, id: newId, cycle_id: id }, 201);
  });

  // ── POST /api/fieldops/payments/cycles/:id/deactivate — soft delete (idempotent). ────
  // The schedule-task shape: second call → 200 already_inactive, NO second audit. The
  // cycle's receipts stay attached to the tombstone (history, not display — the read
  // serves active cycles only).
  app.post("/api/fieldops/payments/cycles/:id/deactivate", ...gate, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE job_payment_cycles SET active = 0, updated_by = ?2, updated_at = unixepoch() WHERE id = ?1 AND active = 1",
        )
        .bind(id, actor),
      auditStmtIfChanged(c, actor, "payment_cycle_deactivate", String(id), { cycle_id: id }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const row = await c.env.DB
        .prepare("SELECT id FROM job_payment_cycles WHERE id = ?1")
        .bind(id)
        .first();
      return row
        ? c.json({ ok: true, id, already_inactive: true }, 200)
        : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id }, 200);
  });

  // ── POST /api/fieldops/payments/receipts/:id/deactivate — the correction path. ───────
  // Append-only ledger: the wrong event is tombstoned (kept), the right one re-added.
  app.post("/api/fieldops/payments/receipts/:id/deactivate", ...gate, async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare("UPDATE job_payment_receipts SET active = 0 WHERE id = ?1 AND active = 1")
        .bind(id),
      auditStmtIfChanged(c, actor, "payment_receipt_deactivate", String(id), { receipt_id: id }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const row = await c.env.DB
        .prepare("SELECT id FROM job_payment_receipts WHERE id = ?1")
        .bind(id)
        .first();
      return row
        ? c.json({ ok: true, id, already_inactive: true }, 200)
        : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id }, 200);
  });
}
