import type { Context } from "hono";
import type { Env, Vars } from "./types";
import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
import { auditStmt, auditStmtIfChanged } from "./audit";
import { requireJob } from "./fieldops_scope";
import { scheduleMatchKey } from "./schedule_normalize";
import type { ScheduleTaskRow, ScheduleTasksResponse } from "./wire-types";

// ─────────────────────────────────────────────────────────────────────────────
// Living schedule task list (ADR-0006 PR-4) — worker/fieldops_schedule_tasks.ts
//
// The per-job task list (`job_schedule_tasks`, migration 0071) a committed schedule
// import authors and the office curates by hand. Send-free throughout (D1 writes only);
// bound params only; every mutation lands with its audit_log row in ONE D1 batch (W4);
// in-WHERE active=1 guards on every write.
//
//   • GET /api/fieldops/schedule-tasks?job_id — cap.jobtracker.read. ALL roles view the
//     schedule (operator decision 4) — same cap-only posture as the Job Tracker read;
//     no per-job ownership scope, because the tracker's job list is already visible to
//     every role that holds the cap. WHO fields (delivered_by / last_marked_by) resolve
//     to personnel DISPLAY NAMES via the W9 correlated subquery — the stored account
//     username never leaves the Worker.
//   • POST /api/fieldops/schedule-tasks (+ /:id/edit, /:id/deactivate) —
//     cap.jobtracker.manage (admin-only in practice: manager is withheld it, 0023).
//     The office's manual floor: a task the import missed, a correction, a retirement.
//     match_key is recomputed server-side on add AND edit through the ONE shared
//     normalizer (worker/schedule_normalize.ts) — the same function the commit route
//     and PR-6's diff engine use, so a hand-edited task keeps matching its revisions.
//
// PERCENT EDITS HERE DO **NOT** STAMP last_marked_by/last_marked_at. That pair is the
// PR-5 FIELD-MARK semantics — `last_marked_by IS NOT NULL` ⇔ "a human marked this in
// the portal", which is the reconcile %-conflict predicate (decision 9). An office
// correction through this route is list curation, not field progress, so it must not
// make a task look field-marked.
//
// ORDER DEPENDENCY: migration 0071 must be applied to the live D1 BEFORE this Worker
// deploys, or every route here 500s. (And `git -C ~/its pull origin main` FIRST — the
// stale-migrations-list lockout class.)
// ─────────────────────────────────────────────────────────────────────────────

type Ctx = Context<{ Bindings: Env; Variables: Vars }>;

const CAP_READ = "cap.jobtracker.read";
const CAP_MANAGE = "cap.jobtracker.manage";

// Bounds — shared with the commit route (fieldops_schedules.ts imports the reader below),
// so an imported task runs the IDENTICAL validation a hand-authored one does.
const MAX_TASK_NAME = 300;
const MAX_TASK_SECTION = 120;
const MAX_TASK_PREDECESSORS = 200;
const MAX_TASK_DURATION_DAYS = 5000;
const MAX_SORT_ORDER = 100_000_000;
// The read cap: a 300-task utility schedule fits 2×; past this the page would be
// unreadable anyway and the office splits the schedule.
const TASKS_READ_CAP = 600;

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

/** The validated content fields of one task — manual add/edit AND the commit route's
 *  imported rows all pass through `readScheduleTaskFields`, so the two paths cannot
 *  drift (the readExpectationFields precedent). Flags land as 0/1 (the stored shape). */
export type ScheduleTaskFields = {
  name: string;
  section: string | null;
  duration_days: number | null;
  start_date: string | null;
  finish_date: string | null;
  percent_done: number;
  is_milestone: 0 | 1;
  is_contract_milestone: 0 | 1;
  is_delivery: 0 | 1;
  predecessors_raw: string | null;
};

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

function optFlag(v: unknown): 0 | 1 | "bad" {
  if (v === undefined || v === null) return 0;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (v === 0 || v === 1) return v;
  return "bad";
}

/** Validate one task's content fields. Returns the cleaned tuple or an error CODE
 *  string (`invalid_task_*` vocabulary — src/lib/errorCopy.ts carries the human copy).
 *  EXPORTED for the PR-4 degenerate commit (fieldops_schedules.ts) and any later
 *  bulk path: an imported task must run the identical bounds a hand-authored one does. */
export function readScheduleTaskFields(body: Record<string, unknown>): ScheduleTaskFields | string {
  if (typeof body.name !== "string") return "invalid_task_name";
  const name = body.name.trim();
  if (name.length < 1 || name.length > MAX_TASK_NAME) return "invalid_task_name";
  const section = optBoundedText(body.section, MAX_TASK_SECTION);
  if (section === "bad") return "invalid_task_section";
  let duration_days: number | null = null;
  if (body.duration_days !== undefined && body.duration_days !== null && body.duration_days !== "") {
    if (
      typeof body.duration_days !== "number" || !Number.isSafeInteger(body.duration_days) ||
      body.duration_days < 0 || body.duration_days > MAX_TASK_DURATION_DAYS
    ) {
      return "invalid_task_duration";
    }
    duration_days = body.duration_days;
  }
  const start_date = optIsoDate(body.start_date);
  if (start_date === "bad") return "invalid_task_date";
  const finish_date = optIsoDate(body.finish_date);
  if (finish_date === "bad") return "invalid_task_date";
  let percent_done = 0;
  if (body.percent_done !== undefined && body.percent_done !== null && body.percent_done !== "") {
    if (
      typeof body.percent_done !== "number" || !Number.isSafeInteger(body.percent_done) ||
      body.percent_done < 0 || body.percent_done > 100
    ) {
      return "invalid_task_percent";
    }
    percent_done = body.percent_done;
  }
  const is_milestone = optFlag(body.is_milestone);
  const is_contract_milestone = optFlag(body.is_contract_milestone);
  const is_delivery = optFlag(body.is_delivery);
  if (is_milestone === "bad" || is_contract_milestone === "bad" || is_delivery === "bad") {
    return "invalid_task_flag";
  }
  const predecessors_raw = optBoundedText(body.predecessors_raw, MAX_TASK_PREDECESSORS);
  if (predecessors_raw === "bad") return "invalid_task_predecessors";
  return {
    name, section, duration_days, start_date, finish_date, percent_done,
    is_milestone, is_contract_milestone, is_delivery, predecessors_raw,
  };
}

export function registerScheduleTaskRoutes(app: FieldopsApp, gates: FieldopsGates): void {
  // ── GET /api/fieldops/schedule-tasks?job_id — the job's living task list. ──────────
  // cap.jobtracker.read (ALL roles view — decision 4). Active rows in document order.
  // Serves exactly what the Schedule page renders — never match_key (matching
  // internals), never a raw account username (W9: delivered_by / last_marked_by
  // resolve to display names; an unmatched account yields NULL).
  app.get(
    "/api/fieldops/schedule-tasks",
    gates.requireSession,
    gates.requireCapability(CAP_READ),
    async (c) => {
      const jobId = typeof c.req.query("job_id") === "string" ? (c.req.query("job_id") ?? "") : "";
      const jobErr = await requireJob(c, jobId); // 400 bad shape / 404 unknown job
      if (jobErr) return jobErr;
      const [tasks, jobRow] = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `SELECT t.id, t.task_uuid, t.job_id, t.section, t.name, t.duration_days,
                    t.start_date, t.finish_date, t.baseline_start_date, t.baseline_finish_date,
                    t.percent_done, t.schedule_percent,
                    t.is_milestone, t.is_contract_milestone, t.is_delivery,
                    t.delivered_date,
                    (SELECT p.name FROM personnel p WHERE p.username = t.delivered_by ORDER BY p.id ASC LIMIT 1)
                      AS delivered_by_name,
                    t.delivered_at, t.predecessors_raw, t.sort_order,
                    (SELECT p.name FROM personnel p WHERE p.username = t.last_marked_by ORDER BY p.id ASC LIMIT 1)
                      AS last_marked_by_name,
                    t.last_marked_at, t.created_at, t.updated_at
             FROM job_schedule_tasks t
             WHERE t.job_id = ?1 AND t.active = 1
             ORDER BY t.sort_order ASC, t.id ASC
             LIMIT ${TASKS_READ_CAP}`,
          )
          .bind(jobId),
        // The job's display name — the page heading says "Schedule — Deep Lake", not the
        // JOB-###### key (the Materials-page precedent; the route is deep-linkable so the
        // name must come from data, not navigation state).
        c.env.DB.prepare("SELECT project_name FROM jobs WHERE job_id = ?1").bind(jobId),
      ]);
      const rows = (tasks.results ?? []) as ScheduleTaskRow[];
      const payload: ScheduleTasksResponse = {
        tasks: rows,
        project_name:
          ((jobRow.results?.[0] as { project_name?: string } | undefined)?.project_name) ?? null,
        // A commit may create up to MAX_ROWS_TOTAL (2000) tasks while this read caps at
        // 600 — a silent cap would render a partial page as if it were the whole
        // schedule (2026-08-11 review W8). Exactly-at-cap reports truncated; the false
        // positive at exactly 600 real tasks is the honest direction to be wrong in.
        truncated: rows.length >= TASKS_READ_CAP,
      };
      return c.json(payload, 200);
    },
  );

  // ── POST /api/fieldops/schedule-tasks — manual add (office). ───────────────────────
  // The Tier-3 floor for the LIVE list: a task the import missed. task_uuid minted here
  // (the stable cross-revision identity); match_key recomputed through the ONE shared
  // normalizer; baselines = the entered dates (this IS the task's first commit).
  // sort_order defaults to end-of-list (max+10) unless the caller places it.
  app.post(
    "/api/fieldops/schedule-tasks",
    gates.requireSession,
    gates.requireCapability(CAP_MANAGE),
    async (c) => {
      const body = await readJsonBody(c);
      if (body === null) return c.json({ error: "bad_request" }, 400);
      const jobId = typeof body.job_id === "string" ? body.job_id : "";
      const jobErr = await requireJob(c, jobId);
      if (jobErr) return jobErr;
      const f = readScheduleTaskFields(body);
      if (typeof f === "string") return c.json({ error: f }, 400);
      let sortOrder: number | null = null;
      if (body.sort_order !== undefined && body.sort_order !== null) {
        if (
          typeof body.sort_order !== "number" || !Number.isSafeInteger(body.sort_order) ||
          body.sort_order < 0 || body.sort_order > MAX_SORT_ORDER
        ) {
          return c.json({ error: "invalid_seq" }, 400);
        }
        sortOrder = body.sort_order;
      }

      const actor = c.get("session").username;
      const taskUuid = crypto.randomUUID();
      const matchKey = scheduleMatchKey(f.section ?? "", f.name);
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `INSERT INTO job_schedule_tasks
               (task_uuid, job_id, section, name, match_key, duration_days,
                start_date, finish_date, baseline_start_date, baseline_finish_date,
                percent_done, is_milestone, is_contract_milestone, is_delivery,
                predecessors_raw, sort_order)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?7, ?8, ?9, ?10, ?11, ?12, ?13,
                     COALESCE(?14, (SELECT COALESCE(MAX(sort_order), 0) + 10
                                      FROM job_schedule_tasks WHERE job_id = ?2 AND active = 1)))
             RETURNING id`,
          )
          .bind(
            taskUuid, jobId, f.section, f.name, matchKey, f.duration_days,
            f.start_date, f.finish_date, f.percent_done,
            f.is_milestone, f.is_contract_milestone, f.is_delivery,
            f.predecessors_raw, sortOrder,
          ),
        auditStmt(c, actor, "schedule_task_create", jobId, {
          job_id: jobId, task_uuid: taskUuid, name: f.name, section: f.section,
        }),
      ]);
      const newId = (res[0].results?.[0] as { id: number } | undefined)?.id ?? null;
      return c.json({ ok: true, id: newId, task_uuid: taskUuid }, 201);
    },
  );

  // ── POST /api/fieldops/schedule-tasks/:id/edit — full-replace the content fields. ──
  // Guarded in-WHERE active=1; audit conditional on changes()=1 in the SAME batch (W4).
  // match_key recomputed (a renamed task must keep matching its future revisions).
  // A PERCENT edit here deliberately does NOT stamp last_marked_by/last_marked_at —
  // that is the PR-5 field-mark semantics (`last_marked_by IS NOT NULL` ⇔ a human
  // marked it in the portal, the reconcile %-conflict predicate); an office correction
  // is curation, not field progress.
  app.post(
    "/api/fieldops/schedule-tasks/:id/edit",
    gates.requireSession,
    gates.requireCapability(CAP_MANAGE),
    async (c) => {
      const id = parseIdParam(c.req.param("id"));
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const body = await readJsonBody(c);
      if (body === null) return c.json({ error: "bad_request" }, 400);
      const f = readScheduleTaskFields(body);
      if (typeof f === "string") return c.json({ error: f }, 400);

      const actor = c.get("session").username;
      const matchKey = scheduleMatchKey(f.section ?? "", f.name);
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            `UPDATE job_schedule_tasks
                SET name = ?2, section = ?3, match_key = ?4, duration_days = ?5,
                    start_date = ?6, finish_date = ?7, percent_done = ?8,
                    is_milestone = ?9, is_contract_milestone = ?10, is_delivery = ?11,
                    predecessors_raw = ?12, updated_at = unixepoch()
              WHERE id = ?1 AND active = 1`,
          )
          .bind(
            id, f.name, f.section, matchKey, f.duration_days,
            f.start_date, f.finish_date, f.percent_done,
            f.is_milestone, f.is_contract_milestone, f.is_delivery,
            f.predecessors_raw,
          ),
        auditStmtIfChanged(c, actor, "schedule_task_update", String(id), {
          task_id: id, name: f.name, section: f.section, percent_done: f.percent_done,
        }),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
      return c.json({ ok: true, id }, 200);
    },
  );

  // ── POST /api/fieldops/schedule-tasks/:id/deactivate — soft delete (idempotent). ───
  // active=0 keeps the row (a marked task is history; PR-6's reconcile also needs the
  // tombstone to tell "removed by the office" from "never existed"). Second call →
  // 200 already_inactive with NO second audit (the expected-materials shape).
  app.post(
    "/api/fieldops/schedule-tasks/:id/deactivate",
    gates.requireSession,
    gates.requireCapability(CAP_MANAGE),
    async (c) => {
      const id = parseIdParam(c.req.param("id"));
      if (id === null) return c.json({ error: "invalid_id" }, 400);
      const actor = c.get("session").username;
      const res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            "UPDATE job_schedule_tasks SET active = 0, updated_at = unixepoch() WHERE id = ?1 AND active = 1",
          )
          .bind(id),
        auditStmtIfChanged(c, actor, "schedule_task_deactivate", String(id), { task_id: id }),
      ]);
      if ((res[0].meta.changes ?? 0) === 0) {
        const row = await c.env.DB
          .prepare("SELECT id FROM job_schedule_tasks WHERE id = ?1")
          .bind(id)
          .first();
        return row
          ? c.json({ ok: true, id, already_inactive: true }, 200)
          : c.json({ error: "not_found" }, 404);
      }
      return c.json({ ok: true, id }, 200);
    },
  );
}
