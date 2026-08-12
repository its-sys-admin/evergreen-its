// Job-schedule import + living-task-list API client (ADR-0006 PR-4). Same-origin cookie
// fetch; no auth header — the bearer tier is the Mac schedule_poll daemon's internal
// surface only, never the browser's.
//
// The import surface (upload / list / rows / preview / plan / commit / discard) is gated
// `cap.jobtracker.manage`; the task-list read rides `cap.jobtracker.read` (all roles view —
// operator decision 4). The Worker re-gates every call; the SPA's capability checks drive
// affordances only (Invariant 2 — SPA gating is convenience, never the boundary).
//
// (R1) Errors: getJson/postJson throw ApiError (src/lib/errorCopy.ts) — err.message is HUMAN
// copy, err.code is the raw wire code pages branch on (e.g. 'duplicate_schedule',
// 'revision_reconcile_not_available').

import { ApiError, raiseApiError } from "./errorCopy";
import type {
  ScheduleCommitResponse,
  ScheduleDetailResponse,
  ScheduleListResponse,
  ScheduleMarkDeliveredResponse,
  ScheduleMarkMilestoneDoneResponse,
  ScheduleMarkProgressResponse,
  SchedulePlanResponse,
  SchedulePreviewResponse,
  ScheduleRowsResponse,
  ScheduleTasksResponse,
} from "../../worker/wire-types";

// Wire shapes — SINGLE-SOURCED in worker/wire-types.ts (the Worker types its c.json payloads
// with the same definitions, so a shape drift fails the typecheck on both sides); re-exported
// here so callers and test fixtures have one import path.
export type {
  ScheduleColumnMap,
  ScheduleCommitResponse,
  ScheduleDetailResponse,
  ScheduleGridRow,
  ScheduleListResponse,
  ScheduleListRow,
  ScheduleMarkDeliveredResponse,
  ScheduleMarkMilestoneDoneResponse,
  ScheduleMarkProgressResponse,
  SchedulePlanResponse,
  SchedulePreviewResponse,
  ScheduleRowKind,
  ScheduleRowsResponse,
  ScheduleStatus,
  ScheduleTaskRow,
  ScheduleTasksResponse,
} from "../../worker/wire-types";

export { ApiError };

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

async function postJson<T = { ok: boolean }>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

/** The MIME allowlist the Worker enforces — PDF ONLY (narrower than the manifest lane: the
 *  schedule parser reads Smartsheet Gantt PDF exports and nothing else, so accepting another
 *  type would queue a document that can only ever end in `refused`). Mirrored here so the
 *  file picker refuses locally instead of round-tripping a 422. */
export const SCHEDULE_ACCEPT = ".pdf,application/pdf";

/** Worker's SCHEDULE_MAX_BYTES (10 MB — the whole corpus tops out near 800 KB). Checked
 *  client-side so an oversize file fails instantly rather than after an upload the server
 *  will refuse anyway. */
export const SCHEDULE_MAX_BYTES = 10 * 1024 * 1024;

/** One page of the paged commit — matches the Worker's COMMIT_PAGE_TASKS so `commitAllSchedule`
 *  below makes exactly as many round trips as the server would page into. */
export const COMMIT_PAGE_TASKS = 100;

/** File → base64, without loading a second copy as a data: URL string. */
export async function fileToBase64(file: File): Promise<string> {
  const buf = new Uint8Array(await file.arrayBuffer());
  let bin = "";
  const STEP = 0x8000;
  for (let i = 0; i < buf.length; i += STEP) {
    bin += String.fromCharCode(...buf.subarray(i, i + STEP));
  }
  return btoa(bin);
}

// ── Import pool ──────────────────────────────────────────────────────────────────────

export async function fetchSchedules(jobId: string): Promise<ScheduleListResponse> {
  return getJson<ScheduleListResponse>(
    `/api/fieldops/schedules?job_id=${encodeURIComponent(jobId)}`,
  );
}

export async function fetchSchedule(id: number): Promise<ScheduleDetailResponse> {
  return getJson<ScheduleDetailResponse>(`/api/fieldops/schedules/${id}`);
}

/** One page of the proposed grid. `after` is a `row_index` cursor (naturally ordered, stable). */
export async function fetchScheduleRows(
  id: number,
  opts: { after?: number; limit?: number } = {},
): Promise<ScheduleRowsResponse> {
  const q = new URLSearchParams();
  if (opts.after !== undefined) q.set("after", String(opts.after));
  if (opts.limit !== undefined) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return getJson<ScheduleRowsResponse>(`/api/fieldops/schedules/${id}/rows${qs ? `?${qs}` : ""}`);
}

/** Drain the whole grid by following the `row_index` cursor. Bounded by `maxRows` so a
 *  pathological document cannot spin the browser forever. */
export async function fetchAllScheduleRows(
  id: number,
  { pageSize = 500, maxRows = 2000 } = {},
): Promise<ScheduleRowsResponse["rows"]> {
  const out: ScheduleRowsResponse["rows"] = [];
  let after = 0;
  for (;;) {
    const { rows } = await fetchScheduleRows(id, { after, limit: pageSize });
    out.push(...rows);
    if (rows.length < pageSize || out.length >= maxRows) return out;
    after = rows[rows.length - 1].row_index;
  }
}

export async function fetchSchedulePreview(
  id: number,
  page: number,
): Promise<SchedulePreviewResponse> {
  return getJson<SchedulePreviewResponse>(`/api/fieldops/schedules/${id}/preview/${page}`);
}

export async function uploadSchedule(
  jobId: string,
  file: File,
): Promise<{ ok: boolean; id: number | null; filename: string; size_bytes: number }> {
  const data_b64 = await fileToBase64(file);
  return postJson(`/api/fieldops/schedules`, {
    job_id: jobId,
    filename: file.name,
    // Some browsers hand a PDF picked from odd sources an empty type — the Worker
    // allowlists on the DECLARED mime, so default the only legal one.
    mime: file.type || "application/pdf",
    data_b64,
  });
}

export async function discardSchedule(id: number): Promise<{ ok: boolean; id: number }> {
  return postJson(`/api/fieldops/schedules/${id}/discard`);
}

// ── Plan / commit (PR-4 degenerate) ──────────────────────────────────────────────────

/** One row of the commit payload. `section` rows are never committed — they ride only for
 *  count transparency; every data row carries its RESOLVED section string (the validate
 *  screen's running-divider context), which is what the committed task stores. */
export interface ScheduleCommitRow {
  source_row_index: number;
  kind: "data" | "section";
  name?: string;
  section?: string | null;
  duration_days?: number | null;
  start_date?: string | null;
  finish_date?: string | null;
  percent_done?: number;
  is_milestone?: boolean;
  is_contract_milestone?: boolean;
  is_delivery?: boolean;
  predecessors_raw?: string | null;
}

/** DRY RUN. Writes nothing; PR-4 answers the DEGENERATE question — "is this a first import,
 *  and how many tasks would it create?" A non-degenerate answer means the job already has a
 *  task list and the commit will refuse (revision reconcile is a later update). */
export async function planSchedule(
  id: number,
  rows: ScheduleCommitRow[],
): Promise<SchedulePlanResponse> {
  return postJson<SchedulePlanResponse>(`/api/fieldops/schedules/${id}/plan`, { rows });
}

/** ONE page of the commit. Prefer `commitAllSchedule` unless you drive the paging yourself. */
export async function commitSchedule(
  id: number,
  rows: ScheduleCommitRow[],
): Promise<ScheduleCommitResponse> {
  return postJson<ScheduleCommitResponse>(`/api/fieldops/schedules/${id}/commit`, { rows });
}

/**
 * Drive the paged commit to completion, re-posting the SAME full payload each round.
 *
 * Deliberate (the manifest commitAll contract): the server drops every row at or below
 * `committed_through_row` before writing, so re-sending the whole set is a no-op for pages
 * that already landed — and the full payload is ALSO what keeps each task's document
 * ordinal (sort_order) stable across pages. If a response is lost mid-way, re-running this
 * resumes exactly where the server actually got to.
 */
export async function commitAllSchedule(
  id: number,
  rows: ScheduleCommitRow[],
  onProgress?: (landed: number, total: number) => void,
): Promise<{ inserted: number; pages: number }> {
  let inserted = 0;
  let pages = 0;
  const total = rows.filter((r) => r.kind === "data").length;
  // Bound the loop: each page must strictly advance the watermark, so at most one page per
  // row is possible. A server that stopped advancing would otherwise spin here forever.
  const maxPages = Math.ceil(Math.max(total, 1) / COMMIT_PAGE_TASKS) + 2;
  for (;;) {
    const res = await commitSchedule(id, rows);
    inserted += res.inserted;
    pages += 1;
    onProgress?.(inserted, total);
    if (res.done) return { inserted, pages };
    if (pages >= maxPages) {
      throw new Error(
        `commit did not finish after ${pages} pages (watermark ${res.committed_through_row}) — reload and check the schedule`,
      );
    }
  }
}

// ── Living task list ─────────────────────────────────────────────────────────────────

export async function fetchScheduleTasks(jobId: string): Promise<ScheduleTasksResponse> {
  return getJson<ScheduleTasksResponse>(
    `/api/fieldops/schedule-tasks?job_id=${encodeURIComponent(jobId)}`,
  );
}

/** Content fields of a manual task add/edit (the office floor — Worker route
 *  fieldops_schedule_tasks.ts; the commit path authors tasks through the same bounds). */
export interface ScheduleTaskDraft {
  name: string;
  section?: string | null;
  duration_days?: number | null;
  start_date?: string | null;
  finish_date?: string | null;
  percent_done?: number;
  is_milestone?: boolean;
  is_contract_milestone?: boolean;
  is_delivery?: boolean;
  predecessors_raw?: string | null;
  sort_order?: number;
}

export async function addScheduleTask(
  jobId: string,
  draft: ScheduleTaskDraft,
): Promise<{ ok: boolean; id: number | null; task_uuid: string }> {
  return postJson(`/api/fieldops/schedule-tasks`, { job_id: jobId, ...draft });
}

export async function editScheduleTask(
  id: number,
  draft: ScheduleTaskDraft,
): Promise<{ ok: boolean; id: number }> {
  return postJson(`/api/fieldops/schedule-tasks/${id}/edit`, draft);
}

export async function deactivateScheduleTask(
  id: number,
): Promise<{ ok: boolean; id: number; already_inactive?: boolean }> {
  return postJson(`/api/fieldops/schedule-tasks/${id}/deactivate`);
}

// ── Field mark-off (PR-5 — cap.schedule.mark; the Worker re-gates + per-job scopes) ───

/** Mark a task's percent done (quick-% chip or exact %). A % regression is allowed —
 *  corrections are real. Milestone rows accept only 0 or 100 (wire code `milestone_binary`). */
export async function markScheduleTaskProgress(
  id: number,
  percent: number,
): Promise<ScheduleMarkProgressResponse> {
  return postJson<ScheduleMarkProgressResponse>(
    `/api/fieldops/schedule-tasks/${id}/progress`,
    { percent },
  );
}

/** Mark a milestone done (percent 100 + the field-mark stamps). Idempotent — a repeat
 *  answers `already: true` without re-stamping. */
export async function markScheduleTaskMilestoneDone(
  id: number,
): Promise<ScheduleMarkMilestoneDoneResponse> {
  return postJson<ScheduleMarkMilestoneDoneResponse>(
    `/api/fieldops/schedule-tasks/${id}/milestone-done`,
  );
}

/** Mark a Deliveries task delivered. Omitting the date lets the Worker stamp today
 *  (Pacific); calling again with a different date corrects the record. */
export async function markScheduleTaskDelivered(
  id: number,
  deliveredDate?: string,
): Promise<ScheduleMarkDeliveredResponse> {
  return postJson<ScheduleMarkDeliveredResponse>(
    `/api/fieldops/schedule-tasks/${id}/delivered`,
    deliveredDate ? { delivered_date: deliveredDate } : {},
  );
}
