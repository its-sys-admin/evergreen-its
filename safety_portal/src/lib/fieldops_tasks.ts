// Assigned-Tasks tab (P4 field-ops feature) S1 — "My Tasks" read client.
// Same-origin fetch with the session cookie (no auth header). The Worker re-gates on cap.tasks.own.
// Status changes reuse the existing setTaskStatus route (cap.tasks.own) — re-exported here so the
// page has a single import surface.
//
// (R1) Errors throw ApiError (src/lib/errorCopy.ts): err.message is human copy, err.code the raw
// wire code (e.g. 'forbidden_task' when a subcontractor targets a task that isn't theirs).
import { raiseApiError } from "./errorCopy";
import type { JobTasksResponse, MyTasksResponse } from "../../worker/wire-types";

export { setTaskStatus, type TaskStatus } from "./fieldops_jobtracker";

// Wire shapes — SINGLE-SOURCED in worker/wire-types.ts (the Worker types its c.json payload with
// the same definitions, so a shape drift fails the typecheck on both sides); re-exported here so
// existing importers keep their path. The R1 response contract (open-first ordering, `linked`,
// `assigned_by`) and the CS4 `viewer_placement` (the caller's OWN placement — the Daily tab's
// placement source, replacing its fetchJobList stage) are documented on the definitions there.
export type { MyTask, MyTasksResponse, ViewerTaskPlacement } from "../../worker/wire-types";

export async function fetchMyTasks(): Promise<MyTasksResponse> {
  const res = await fetch("/api/fieldops/tasks/mine", { credentials: "same-origin" });
  if (!res.ok) return raiseApiError(res);
  return (
    ((await res.json()) as MyTasksResponse) ?? { tasks: [], linked: false, viewer_placement: null }
  );
}

export type { JobTaskRow, JobTasksResponse } from "../../worker/wire-types";

/** ONE job's assigned tasks (the Site Tasks page) — cap.jobtracker.read. */
export async function fetchJobTasks(jobId: string): Promise<JobTasksResponse> {
  const res = await fetch(`/api/fieldops/tasks?job_id=${encodeURIComponent(jobId)}`, {
    credentials: "same-origin",
  });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as JobTasksResponse;
}
