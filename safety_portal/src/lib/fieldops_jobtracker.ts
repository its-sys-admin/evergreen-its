// Job Tracker read API client for Field Ops tab (BRIEF C).
// Same-origin fetch with session cookie; no auth header.
//
// (R1) Errors throw ApiError (src/lib/errorCopy.ts): err.message is HUMAN copy, err.code the raw
// wire code for page-level branching. Pages must branch on err.code, never err.message.
import { raiseApiError } from "./errorCopy";
import type { JobDetailResponse, JobListResponse } from "../../worker/wire-types";

// Wire shapes — SINGLE-SOURCED in worker/wire-types.ts (the Worker types its c.json payloads with
// the same definitions, so a shape drift fails the typecheck on both sides — and the DailyReportTab
// fixture now type-checks against what the Worker actually sends); re-exported here so existing
// importers keep their path.
export type {
  ArchiveDirection,
  ArchiveState,
  CrewMember,
  DetailCrewMember,
  EquipmentOnSite,
  JobClient,
  JobDetail,
  JobArchiveContainer,
  JobArchiveResponse,
  JobArchiveStatus,
  JobDetailResponse,
  JobInspection,
  JobListResponse,
  JobRow,
  JobTimeEntry,
  OpenTask,
  Task,
  ViewerPersonnel,
} from "../../worker/wire-types";

export type JobStatusFilter = "active" | "closed" | "on_hold" | "all";

export async function fetchJobList(status?: JobStatusFilter, cursor?: string): Promise<JobListResponse> {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (cursor) q.set("cursor", cursor);
  const res = await fetch(`/api/fieldops/jobs?${q.toString()}`, { credentials: "same-origin" });
  if (!res.ok) return raiseApiError(res);
  return ((await res.json()) as JobListResponse) ?? { jobs: [], next_cursor: null };
}

export async function fetchJobDetail(
  jobId: string,
  cursors?: { task?: string; time?: string; insp?: string },
): Promise<JobDetailResponse> {
  const q = new URLSearchParams();
  if (cursors?.task) q.set("task_cursor", cursors.task);
  if (cursors?.time) q.set("time_cursor", cursors.time);
  if (cursors?.insp) q.set("insp_cursor", cursors.insp);
  const res = await fetch(`/api/fieldops/jobs/${encodeURIComponent(jobId)}?${q.toString()}`, {
    credentials: "same-origin",
  });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as JobDetailResponse;
}

/** One configured delivery contact — a suggestion for the job-create Safety CC <datalist>. Mirrors
 *  the DeliveryContact shape worker/po.ts serves at GET /api/po/config (the SAME §50-edited bundle).
 *  Only entries with an `email` are usable as a CC suggestion (the CC field holds an email). */
export interface DeliveryContact {
  name: string;
  phone?: string;
  email?: string;
}

/** GET /api/fieldops/delivery-contacts — the configured delivery-contact suggestion list for the
 *  job-create Safety CC field's <datalist>. Gated cap.jobtracker.manage server-side (the job
 *  creator's own cap). SUGGESTIONS ONLY — free-text CC entry always stays accepted. */
export async function getDeliveryContacts(): Promise<DeliveryContact[]> {
  const res = await fetch("/api/fieldops/delivery-contacts", { credentials: "same-origin" });
  if (!res.ok) return raiseApiError(res);
  const body = (await res.json()) as { delivery_contacts?: DeliveryContact[] };
  return body.delivery_contacts ?? [];
}

// ── WRITE (P2.3 routes; same-origin cookie POST) ─────────────────────────────────────────────────
// NB: the READ routes are PLURAL (/api/fieldops/jobs…); the P2.3 WRITE routes are SINGULAR
// (/api/fieldops/job…, /api/fieldops/task…). Mirror the worker exactly — see fieldops_job_write.ts /
// fieldops_task_write.ts. The Worker re-gates every call server-side; UI capability checks are
// convenience only. Create/close/add-task/reassign-task → cap.jobtracker.manage; task status → cap.tasks.own.
async function postJson<T = { ok: boolean }>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

export type TaskStatus = "open" | "in_progress" | "done";

// Optional inline client for a brand-new portal-origin job (worker INSERTs into `clients` first).
export interface NewJobClient {
  name: string;
  contact?: string;
  phone?: string;
  email?: string;
}

// P2.5 — the portal is the authoritative writer of a job's routing source-of-truth. lifecycle is
// the canonical job-state field (active|inactive|archived); the legacy `active`/`status` flags are
// derived by the worker. Routing block + CC arrays mirror fieldops_job_write.ts parseRouting:
// every field optional, each CC array ≤5 email-shaped strings. The worker re-validates + re-gates.
// Sourced from the worker's wire-types so there is ONE definition of the union rather than two
// copies that could drift (the SPA reads it off JobRow/JobDetail, which the worker already types).
// Imported AND re-exported: a bare `export type { … } from` would satisfy downstream importers but
// would not bind the name in this module, where setLifecycle's own signature uses it.
import type { JobArchiveResponse, JobLifecycle } from "../../worker/wire-types";
export type { JobLifecycle };

export interface JobRouting {
  /** OPTIONAL rename (edit page, 2026-07-20): absent = unchanged; never blankable.
   *  A rename changes which per-job folder FUTURE filings open (find-or-create by name). */
  project_name?: string;
  /** The Evergreen job identifier AS TYPED — `YYYY.NNN` or `YYYY.NNN.S` (0057 + 0064);
   *  '' clears it. Sent whole: the Worker SPLITS it into (job_no, site_phase). See
   *  lib/jobNumber.ts for the matching client-side pair. */
  job_no?: string;
  address?: string;
  address_city?: string;
  address_state?: string;
  address_zip?: string;
  stakeholder_name?: string;
  stakeholder_email?: string;
  stakeholder_phone?: string;
  safety_contact_name?: string;
  safety_contact_email?: string;
  safety_cc?: string[];
  progress_contact_name?: string;
  progress_contact_email?: string;
  progress_cc?: string[];
}

// manage (cap.jobtracker.manage)
// Slice 6: the portal ASSIGNS the canonical Job ID (the office employee no longer types one); the
// request carries only Project Name (+ optional client/routing), and the assigned JOB-###### is
// returned in the response.
export async function createJob(
  body: {
    project_name: string;
    progress?: number;
    new_client?: NewJobClient;
  } & JobRouting,
): Promise<{ job_id: string }> {
  return postJson<{ ok: boolean; job_id: string }>("/api/fieldops/job", body);
}
// TOMBSTONE (R4-F5, 2026-07-03): the dead client fns `closeJob` (POST …/close) and
// `setJobProgress` (POST …/progress) were DELETED — zero SPA callers since setLifecycle (P2.5)
// superseded /close and the P6 rollup removed the manual progress slider. The WORKER routes were
// then ALSO deleted (operator approval 2026-07-03, the B3 green-light) — see the tombstones in
// worker/fieldops_job_write.ts; git history has both handlers.
// Set the canonical lifecycle (P2.5). THE close path: { lifecycle: 'inactive' } (the old bare
// /close alias is gone). The worker derives the legacy active/status flags and bumps the mirror version.
export async function setLifecycle(jobId: string, lifecycle: JobLifecycle): Promise<{ lifecycle: JobLifecycle }> {
  return postJson<{ ok: boolean; lifecycle: JobLifecycle }>(
    `/api/fieldops/job/${encodeURIComponent(jobId)}/lifecycle`,
    { lifecycle },
  );
}
// ── Archive / un-archive (ROADMAP Track 6) ──────────────────────────────────────────────────
//
// These do NOT move anything. They record intent; the Mac-side pass performs the relocation and
// reports back, which is why the UI polls `job.archive.state` rather than treating the 200 as
// "done". `confirm` must be the job's project_name EXACTLY (trim-only, case-sensitive) — the
// worker re-checks it server-side, so the modal is a usability affordance, not the control.
//
// Error codes worth branching on: confirm_mismatch (409), already_archived (409), not_archived
// (409), archive_in_flight (409). Copy for all of them lives in errorCopy.ts.
export async function archiveJob(jobId: string, confirm: string): Promise<JobArchiveResponse> {
  return postJson<JobArchiveResponse>(
    `/api/fieldops/job/${encodeURIComponent(jobId)}/archive`,
    { confirm },
  );
}

/** Bring an archived job's folders back. The job returns INACTIVE, never active — retrieving
 *  folders is not re-opening a job. */
export async function unarchiveJob(jobId: string, confirm: string): Promise<JobArchiveResponse> {
  return postJson<JobArchiveResponse>(
    `/api/fieldops/job/${encodeURIComponent(jobId)}/unarchive`,
    { confirm },
  );
}

// Edit the routing SoR block (address + stakeholder + safety/progress contacts + CC arrays). The
// worker FULL-OVERWRITES the routing fields (an omitted field → ''), so send the complete intended
// routing for the job. job_id/lifecycle/status are untouched.
export async function editContacts(jobId: string, routing: JobRouting): Promise<{ job_id: string }> {
  return postJson<{ ok: boolean; job_id: string }>(
    `/api/fieldops/job/${encodeURIComponent(jobId)}/contacts`,
    routing,
  );
}
// (G2.6) `due_date` — optional 'YYYY-MM-DD' deadline (any calendar date, past dates included);
// omitted = no deadline. The Worker validates the shape (invalid_due_date 400) and a later
// reassign never clears it.
export async function addTask(
  jobId: string,
  body: { description: string; personnel_id?: number; due_date?: string },
): Promise<{ id: number | null }> {
  return postJson<{ ok: boolean; id: number | null }>(`/api/fieldops/job/${encodeURIComponent(jobId)}/task`, body);
}

// field action (cap.tasks.own)
export async function setTaskStatus(taskId: number, status: TaskStatus): Promise<void> {
  await postJson(`/api/fieldops/task/${taskId}/status`, { status });
}

// (re)assign or clear a task's assignee (cap.jobtracker.manage — assigning who does a task is
// management, the same cap as add-task). `personnelId` null = unassign. The Worker validates the
// personnel_id is a real roster member and re-gates the capability.
export async function reassignTask(taskId: number, personnelId: number | null): Promise<void> {
  await postJson(`/api/fieldops/task/${taskId}/assign`, { personnel_id: personnelId });
}

// field action (cap.time.log). time_entries is an INTEGRITY-BAR table: the CLIENT generates `uuid`
// (idempotency / amend key) but the Worker stamps the server-authoritative record time — a forged
// body timestamp is ignored (see fieldops_time_write.ts). `hours` and `task_id` are optional; an
// omitted task → job-level time.
export async function logTime(body: {
  uuid: string;
  job_id: string;
  hours?: number;
  task_id?: number;
  personnel_id?: number;
  notes?: string;
}): Promise<{ uuid: string }> {
  return postJson<{ ok: boolean; uuid: string }>("/api/fieldops/time-entry", body);
}

// G2.3 — NON-DESTRUCTIVE amend/void (cap.time.log; recorder-or-manager, Worker re-gates). Creates a
// NEW chain row: fresh client `uuid`, amends_uuid = `targetUuid` (the :uuid param), job_id INHERITED
// server-side. FULL-REPLACEMENT body — an omitted personnel_id/task_id means job-level, NOT "keep
// old" (the caller prefills from the displayed row). `hours: 0` is the VOID and requires `notes`
// (the reason) — the Worker 422s `void_requires_reason` without it. Only the chain HEAD can be
// amended (409 `not_head` → refresh, the row was already corrected).
export async function amendTimeEntry(
  targetUuid: string,
  body: {
    uuid: string;
    hours: number;
    task_id?: number;
    personnel_id?: number;
    notes?: string;
    work_started_at?: number;
    work_ended_at?: number;
  },
): Promise<{ uuid: string }> {
  return postJson<{ ok: boolean; uuid: string }>(
    `/api/fieldops/time-entry/${encodeURIComponent(targetUuid)}/amend`,
    body,
  );
}
