import { scheduleMatchKey } from "./schedule_normalize";

// ─────────────────────────────────────────────────────────────────────────────
// schedule_diff.ts — the revision-reconcile diff engine (ADR-0006 decision 9, PR-6).
//
// PURE on purpose: no Hono, no env, no D1 — `diffSchedule(input)` is a deterministic
// function from (proposed rows, living tasks) to a classification, so the whole
// decision table lives under plain vitest (test/schedule_diff.test.ts) and the /plan
// and /commit routes CANNOT disagree: both call this one function and the commit
// RE-DERIVES the diff server-side every page (client classes are never trusted).
//
// NO FUZZY MATCHING (decision 9). The ONLY join is scheduleMatchKey(section, name) —
// imported from schedule_normalize.ts, the same normalizer every writer of
// job_schedule_tasks.match_key uses — so a rename surfaces honestly as fresh+removed
// and the HUMAN links them (the `links` resolution), never a similarity guess.
//
// The classification (decision 9):
//   • matched    — exactly ONE living active task shares the key AND no other proposed
//                  row claims that task. Carries the per-pair field diff (dates two-way,
//                  the three-way % rule, silent-informational changes).
//   • ambiguous  — BLOCKING, no resolution channel: >1 living tasks share the key
//                  (duplicate_living), or ≥2 proposed rows hit the same living task
//                  (duplicate_proposed). The human fixes the duplication itself (rename/
//                  deactivate a task, or re-edit the upload) — a guessed first-match is
//                  exactly the silent data loss this lane exists to prevent.
//   • fresh      — no living hit; inserts (or is LINKED onto a removed task — a rename).
//   • removed    — a living active task no proposed row claims. BLOCKING when the task
//                  has portal progress (last_marked_by), a delivered mark, or is a
//                  contract milestone — those are never silently destroyed; otherwise
//                  the default resolution is remove.
//
// THE THREE-WAY PERCENT RULE (decision 9, verbatim):
//   rev == schedule_percent  → keep portal   (the revision didn't change its assertion;
//                                             the portal's own belief stands)
//   last_marked_by IS NULL   → take revision (nobody field-marked it; the schedule is
//                                             the only voice)
//   else                     → CONFLICT      (both moved — surface {portal, revision},
//                                             default keep portal)
// `schedule_percent` (what the LAST committed schedule asserted — 0071) is the base,
// NOT percent_done: that is what makes a portal mark distinguishable from a PM's
// re-export edit.
//
// A living task that is a candidate of ANY ambiguity is deliberately EXCLUDED from
// `removed`: it IS claimed, just ambiguously, and ambiguity blocks the whole commit —
// classifying it removed would invite a removal resolution for a task whose fate the
// ambiguity still owns.
// ─────────────────────────────────────────────────────────────────────────────

/** The validated content fields of one proposed row — STRUCTURALLY identical to
 *  fieldops_schedule_tasks.ScheduleTaskFields (readScheduleTaskFields' output), declared
 *  locally so this module (re-exported through wire-types into the SPA program) never
 *  drags the Hono/Env-typed worker modules in. Assignability is checked at the call
 *  sites: the routes pass real ScheduleTaskFields values, so a drift between the two
 *  declarations fails the typecheck where the diff is invoked. */
export interface DiffTaskFields {
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
}

/** One proposed (validated) revision row — the commit-row shape after
 *  readScheduleTaskFields, plus its document position. */
export interface DiffProposedRow {
  source_row_index: number;
  fields: DiffTaskFields;
}

/** One living task row as the diff consumes it (the job_schedule_tasks columns the
 *  classification and the commit application need — 0071). `match_key` is the STORED
 *  key: every writer computes it through scheduleMatchKey, so stored-vs-recomputed
 *  cannot drift; the diff recomputes only the INCOMING side. */
export interface DiffLivingTask {
  id: number;
  task_uuid: string;
  match_key: string;
  section: string | null;
  name: string;
  duration_days: number | null;
  start_date: string | null;
  finish_date: string | null;
  percent_done: number;
  schedule_percent: number | null;
  last_marked_by: string | null;
  delivered_date: string | null;
  is_milestone: number;
  is_contract_milestone: number;
  is_delivery: number;
  predecessors_raw: string | null;
  sort_order: number;
}

export interface DiffInput {
  proposed: DiffProposedRow[];
  living: DiffLivingTask[];
}

/** The three-way % outcome for one matched/linked pair. `portal` and `revision` are
 *  always carried (the SPA shows both numbers whatever the rule). */
export type SchedulePercentRule = "keep_portal" | "take_revision" | "conflict";
export interface SchedulePlanPercent {
  rule: SchedulePercentRule;
  portal: number;
  revision: number;
}

/** The two-way date diff on a matched pair — null when neither date moved. The portal
 *  never edits dates (marks touch % only), so any difference IS the revision speaking. */
export interface SchedulePlanDateChange {
  start_from: string | null;
  start_to: string | null;
  finish_from: string | null;
  finish_to: string | null;
}

/** Silent-informational change tags on a matched pair (rendered as a footnote, never a
 *  decision): raw-text/name/section drift under an equal match_key, duration, verbatim
 *  predecessors, and the milestone/delivery flags. */
export type ScheduleInfoChange =
  | "name"
  | "section"
  | "duration"
  | "predecessors"
  | "flags";

/** Wire shape of one matched pair (the /plan payload). */
export interface SchedulePlanMatched {
  source_row_index: number;
  task_id: number;
  task_uuid: string;
  name: string;
  section: string | null;
  date_change: SchedulePlanDateChange | null;
  percent: SchedulePlanPercent;
  info_changes: ScheduleInfoChange[];
}

export type ScheduleAmbiguityReason = "duplicate_living" | "duplicate_proposed";
export interface SchedulePlanAmbiguous {
  source_row_index: number;
  name: string;
  section: string | null;
  /** Living task ids the row could claim (duplicate_living), or the ONE task several
   *  proposed rows are fighting over (duplicate_proposed). */
  candidates: number[];
  reason: ScheduleAmbiguityReason;
}

export interface SchedulePlanFresh {
  source_row_index: number;
  name: string;
  section: string | null;
  start_date: string | null;
  finish_date: string | null;
}

export type ScheduleRemovalReason = "marked" | "delivered" | "contract_milestone";
export interface SchedulePlanRemoved {
  task_id: number;
  task_uuid: string;
  name: string;
  section: string | null;
  percent_done: number;
  start_date: string | null;
  finish_date: string | null;
  blocking: boolean;
  reasons: ScheduleRemovalReason[];
}

/** A matched pair with the full objects the COMMIT application needs (the wire shape
 *  above is derived from this by `toPlanMatched`). */
export interface DiffMatchedPair {
  source_row_index: number;
  fields: DiffTaskFields;
  task: DiffLivingTask;
  date_change: SchedulePlanDateChange | null;
  percent: SchedulePlanPercent;
  info_changes: ScheduleInfoChange[];
}

export interface DiffFreshRow {
  source_row_index: number;
  fields: DiffTaskFields;
}

export interface DiffRemovedTask {
  task: DiffLivingTask;
  blocking: boolean;
  reasons: ScheduleRemovalReason[];
}

export interface ScheduleDiffResult {
  matched: DiffMatchedPair[];
  ambiguous: SchedulePlanAmbiguous[];
  fresh: DiffFreshRow[];
  removed: DiffRemovedTask[];
}

/** The three-way % rule (module header). Exported so the field-diff arm is
 *  table-testable on its own. */
export function percentPlan(fields: DiffTaskFields, task: DiffLivingTask): SchedulePlanPercent {
  const revision = fields.percent_done;
  const portal = task.percent_done;
  if (task.schedule_percent !== null && revision === task.schedule_percent) {
    return { rule: "keep_portal", portal, revision };
  }
  if (task.last_marked_by === null) {
    return { rule: "take_revision", portal, revision };
  }
  return { rule: "conflict", portal, revision };
}

/** Why removing this task would be BLOCKING (empty array = default-removable).
 *  Exported for the removal-resolution validation in the commit route. */
export function removalReasons(task: DiffLivingTask): ScheduleRemovalReason[] {
  const reasons: ScheduleRemovalReason[] = [];
  if (task.last_marked_by !== null) reasons.push("marked");
  if (task.delivered_date !== null) reasons.push("delivered");
  if (task.is_contract_milestone === 1) reasons.push("contract_milestone");
  return reasons;
}

function fieldDiff(
  row: DiffProposedRow,
  task: DiffLivingTask,
): Pick<DiffMatchedPair, "date_change" | "percent" | "info_changes"> {
  const f = row.fields;
  const dateMoved = f.start_date !== task.start_date || f.finish_date !== task.finish_date;
  const date_change: SchedulePlanDateChange | null = dateMoved
    ? {
        start_from: task.start_date,
        start_to: f.start_date,
        finish_from: task.finish_date,
        finish_to: f.finish_date,
      }
    : null;
  const info: ScheduleInfoChange[] = [];
  if (f.name !== task.name) info.push("name");
  if ((f.section ?? null) !== (task.section ?? null)) info.push("section");
  if ((f.duration_days ?? null) !== (task.duration_days ?? null)) info.push("duration");
  if ((f.predecessors_raw ?? null) !== (task.predecessors_raw ?? null)) info.push("predecessors");
  // is_contract_milestone is deliberately NOT compared: it has no OCR signal (ADR-0006
  // "always a human decision") and the commit application PRESERVES the living value,
  // so a difference here would flag a change that will not happen.
  if (f.is_milestone !== task.is_milestone || f.is_delivery !== task.is_delivery) {
    info.push("flags");
  }
  return { date_change, percent: percentPlan(f, task), info_changes: info };
}

/**
 * Classify a revision upload against a living task list (module header). Deterministic:
 * outputs are ordered by source_row_index (matched / ambiguous / fresh) and by the
 * living list's own document order (removed), so two runs over the same input are
 * byte-identical — the /plan payload and every /commit page re-derivation agree.
 */
export function diffSchedule(input: DiffInput): ScheduleDiffResult {
  const proposed = [...input.proposed].sort((a, b) => a.source_row_index - b.source_row_index);
  const living = [...input.living].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);

  const byKey = new Map<string, DiffLivingTask[]>();
  for (const task of living) {
    const bucket = byKey.get(task.match_key);
    if (bucket) bucket.push(task);
    else byKey.set(task.match_key, [task]);
  }

  const ambiguous: SchedulePlanAmbiguous[] = [];
  const fresh: DiffFreshRow[] = [];
  // First pass: every proposed row resolves its key against the living index. A
  // single-hit row CLAIMS its task tentatively — the second pass demotes multi-claimed
  // tasks to duplicate_proposed ambiguity.
  const claims = new Map<number, DiffProposedRow[]>(); // task id → claiming rows
  const limbo = new Set<number>(); // living ids owned by an ambiguity — never `removed`
  for (const row of proposed) {
    const key = scheduleMatchKey(row.fields.section ?? "", row.fields.name);
    const hits = byKey.get(key) ?? [];
    if (hits.length === 0) {
      fresh.push({ source_row_index: row.source_row_index, fields: row.fields });
    } else if (hits.length > 1) {
      ambiguous.push({
        source_row_index: row.source_row_index,
        name: row.fields.name,
        section: row.fields.section,
        candidates: hits.map((t) => t.id),
        reason: "duplicate_living",
      });
      for (const t of hits) limbo.add(t.id);
    } else {
      const bucket = claims.get(hits[0].id);
      if (bucket) bucket.push(row);
      else claims.set(hits[0].id, [row]);
    }
  }

  const matched: DiffMatchedPair[] = [];
  const taskById = new Map(living.map((t) => [t.id, t]));
  for (const [taskId, rows] of claims) {
    const task = taskById.get(taskId)!;
    if (rows.length === 1) {
      const row = rows[0];
      matched.push({
        source_row_index: row.source_row_index,
        fields: row.fields,
        task,
        ...fieldDiff(row, task),
      });
    } else {
      // ≥2 proposed rows, one living task: every claimant is ambiguous, the task in limbo.
      for (const row of rows) {
        ambiguous.push({
          source_row_index: row.source_row_index,
          name: row.fields.name,
          section: row.fields.section,
          candidates: [taskId],
          reason: "duplicate_proposed",
        });
      }
      limbo.add(taskId);
    }
  }
  matched.sort((a, b) => a.source_row_index - b.source_row_index);
  ambiguous.sort((a, b) => a.source_row_index - b.source_row_index);

  const matchedIds = new Set(matched.map((m) => m.task.id));
  const removed: DiffRemovedTask[] = [];
  for (const task of living) {
    if (matchedIds.has(task.id) || limbo.has(task.id)) continue;
    const reasons = removalReasons(task);
    removed.push({ task, blocking: reasons.length > 0, reasons });
  }

  return { matched, ambiguous, fresh, removed };
}

// ── Wire projections (the /plan payload derives from the rich result) ─────────────────

export function toPlanMatched(pair: DiffMatchedPair): SchedulePlanMatched {
  return {
    source_row_index: pair.source_row_index,
    task_id: pair.task.id,
    task_uuid: pair.task.task_uuid,
    name: pair.fields.name,
    section: pair.fields.section,
    date_change: pair.date_change,
    percent: pair.percent,
    info_changes: pair.info_changes,
  };
}

export function toPlanFresh(row: DiffFreshRow): SchedulePlanFresh {
  return {
    source_row_index: row.source_row_index,
    name: row.fields.name,
    section: row.fields.section,
    start_date: row.fields.start_date,
    finish_date: row.fields.finish_date,
  };
}

export function toPlanRemoved(entry: DiffRemovedTask): SchedulePlanRemoved {
  return {
    task_id: entry.task.id,
    task_uuid: entry.task.task_uuid,
    name: entry.task.name,
    section: entry.task.section,
    percent_done: entry.task.percent_done,
    start_date: entry.task.start_date,
    finish_date: entry.task.finish_date,
    blocking: entry.blocking,
    reasons: entry.reasons,
  };
}
