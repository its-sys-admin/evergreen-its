// Thin fetch wrappers over the same-origin Worker API. Cookies are same-origin;
// the signed session cookie is HttpOnly (set by the Worker) so it's never read here.

import { ERROR_COPY } from "./errorCopy";

export type Role = "submitter" | "manager" | "admin";

/** Human display label for a role KEY. DISPLAY-ONLY (Slice T): the 'submitter' tier is presented as
 *  "Subcontractor", but the KEY stays 'submitter' everywhere (option values, the API, the
 *  security-load-bearing fail-safe default in worker/auth.ts). Change the label here, never the key. */
export function roleLabel(role: Role): string {
  switch (role) {
    case "submitter":
      return "Subcontractor";
    case "manager":
      return "Manager";
    case "admin":
      return "Admin";
  }
}

export interface SessionUser {
  username: string;
  /** Authorization role from the server (coarse: submitter | admin). */
  role: Role;
  /** Capability keys granted to this account (migration 0013), resolved server-side
   *  per request. Drives the capability-gated nav/cards in the unified shell — but
   *  every action is independently re-gated server-side (the SPA gating is convenience,
   *  never the boundary). Defaults to [] for a pre-capability (old-Worker) session. */
  capabilities: string[];
  /** (#16) Site-wide feature flag from the server (a Worker var, NOT a capability): whether the
   *  recurring-checklist controls should render on the assign form. The assign route + cron are the
   *  real gates; this only hides UI when dark. OPTIONAL (consumers read it as `?? false`): login /
   *  fetchSession always populate it, but test fixtures + a pre-flag Worker session may omit it. */
  recurring_checklists_enabled?: boolean;
  /** (#17, Seam A) Site-wide feature flag from the server (a Worker var, NOT a capability): whether
   *  the assigned-inspection view should show the "Sign & log to progress report" action on a
   *  COMPLETE inspection. The emit route is the real gate; this only hides UI when dark. OPTIONAL
   *  (consumers read it as `?? false`): login / fetchSession always populate it, but test fixtures +
   *  a pre-flag Worker session may omit it. */
  checklist_progress_logging_enabled?: boolean;
}

async function postJson(path: string, body?: unknown): Promise<Response> {
  return fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function login(username: string, password: string): Promise<SessionUser> {
  const res = await postJson("/api/login", { username, password });
  if (!res.ok) {
    throw new Error(
      res.status === 401 ? "Invalid username or password." : "Login failed. Please try again.",
    );
  }
  const data = (await res.json()) as {
    user: { username: string; role: Role; capabilities?: string[] };
    recurring_checklists_enabled?: boolean;
    checklist_progress_logging_enabled?: boolean;
  };
  return {
    ...data.user,
    capabilities: data.user.capabilities ?? [],
    recurring_checklists_enabled: data.recurring_checklists_enabled ?? false,
    checklist_progress_logging_enabled: data.checklist_progress_logging_enabled ?? false,
  };
}

export async function fetchSession(): Promise<SessionUser | null> {
  const res = await fetch("/api/session", { credentials: "same-origin" });
  if (!res.ok) return null;
  const data = (await res.json()) as {
    user: { username: string; role: Role; capabilities?: string[] };
    recurring_checklists_enabled?: boolean;
    checklist_progress_logging_enabled?: boolean;
  };
  return {
    ...data.user,
    capabilities: data.user.capabilities ?? [],
    recurring_checklists_enabled: data.recurring_checklists_enabled ?? false,
    checklist_progress_logging_enabled: data.checklist_progress_logging_enabled ?? false,
  };
}

export async function logout(): Promise<void> {
  await postJson("/api/logout");
}

export interface Job {
  job_id: string;
  project_name: string;
  /** The Evergreen YYYY.NNN PROJECT number ('' when unassigned) — 0057. Builders
   *  auto-fill from this on a dropdown pick, falling back to the name-prefix parse.
   *  NOT the full identifier: the site segment rides in `site_phase` below. */
  job_no: string;
  /** The identifier's third segment (0064), 0 when the job has no site breakdown. The
   *  PO/subcontract builders auto-fill their Site/phase input from this; join the two
   *  with lib/jobNumber.formatJobNumber for display. */
  site_phase: number;
}

export async function fetchJobs(): Promise<Job[]> {
  const res = await fetch("/api/jobs", { credentials: "same-origin" });
  if (!res.ok) throw new Error("Could not load jobs.");
  return ((await res.json()) as { jobs: Job[] }).jobs;
}

export interface RecentSubmission {
  submission_uuid: string;
  values: Record<string, unknown>;
}

export async function fetchRecent(
  job: string,
  form: string,
  date: string,
): Promise<RecentSubmission | null> {
  const q = new URLSearchParams({ job, form, date });
  const res = await fetch(`/api/recent?${q.toString()}`, { credentials: "same-origin" });
  if (!res.ok) return null;
  return ((await res.json()) as { submission: RecentSubmission | null }).submission;
}

export interface SubmitBody {
  job_id: string;
  form_code: string;
  variant_label: string | null;
  work_date: string;
  values: Record<string, unknown>;
  submission_uuid: string;
  amends_uuid?: string | null;
  /** Admin "filled out as": the account this submission is attributed to. Omitted
   *  (or === the caller) for a normal self-submit. The server re-gates: a non-admin
   *  sending a non-self value is rejected 403, so this is never the security boundary. */
  submitted_as?: string;
}

// The Worker's total-values cap: /api/submit 413s `too_large` when
// JSON.stringify(values).length > PAYLOAD_MAX (worker/index.ts:521,:604). The client can compute
// the IDENTICAL quantity on the identical object before any network call, so an over-cap
// submission blocks here with actionable copy instead of a dead-end 413 (R3-F2). Keep in sync
// with worker/index.ts PAYLOAD_MAX.
export const SUBMIT_PAYLOAD_MAX = 1_800_000;

export async function submitForm(body: SubmitBody): Promise<void> {
  // R3-F2 pre-check — exact mirror of the Worker's measurement (same stringify on the same
  // object, same .length in UTF-16 code units), so anything that passes here passes there.
  // Photos are the only realistic way to approach the cap; the copy says what fixes it.
  if (JSON.stringify(body.values).length > SUBMIT_PAYLOAD_MAX) {
    throw new Error(ERROR_COPY.too_large);
  }
  const res = await postJson("/api/submit", body);
  if (!res.ok) {
    const data = (await res.json().catch(() => ({}))) as { error?: string; detail?: string };
    if (data.error === "unknown_job") {
      throw new Error("That job is no longer active — pick another.");
    }
    // `invalid_photo` carries its machine reason in `detail` (never the bytes) — prefer the
    // field-actionable detail copy (photo_too_large / too_many_photos / …) when we have it.
    const code = data.detail && ERROR_COPY[data.detail] ? data.detail : data.error;
    throw new Error(
      code && ERROR_COPY[code] ? ERROR_COPY[code] : "Submission failed. Please try again.",
    );
  }
}

// ── Request-driven canonical PDF download (PR-4 Part A) ──────────────────────
// The PM's downloadable copy IS the Box-filed PDF, byte-identical — there is no
// browser-side render. Nothing is cached until the user explicitly asks for it:
//   requestPdf  → flips the server "user wants this cached" flag (idempotent).
//   pdfStatus   → the 5s poll source for the "Preparing…" → "Download" transition.
//   downloadPdf → triggers the browser download of the reassembled PDF.
// The Mac daemon (not the Worker) fetches the filed PDF from Box, base64-chunks it
// into D1; the Worker's GET …/pdf reassembles + streams it. Ownership is re-gated
// server-side and a foreign uuid returns 404 (not 403 — no enumeration). The
// HttpOnly session cookie rides automatically via credentials:"same-origin".

export interface PdfRequestResult {
  ok: boolean;
  /** True when the cache is ALREADY populated (a prior request completed). */
  ready: boolean;
}

/** Ask the server to cache this submission's filed PDF for download. Idempotent:
 *  a repeat call on an already-cached submission just returns ready:true. */
export async function requestPdf(uuid: string): Promise<PdfRequestResult> {
  const res = await postJson(`/api/submissions/${encodeURIComponent(uuid)}/request-pdf`);
  if (!res.ok) throw new Error("Could not request the download.");
  const data = (await res.json().catch(() => ({}))) as { ok?: boolean; ready?: boolean };
  return { ok: data.ok ?? true, ready: data.ready ?? false };
}

export interface PdfStatus {
  requested: boolean;
  ready: boolean;
  /** Epoch seconds the cache is pruned (pdf_ready_at + 86400); null until ready. */
  expires_at: number | null;
}

/** Poll the cache state. `ready` flips true once the Mac daemon has finished
 *  uploading every chunk; `expires_at` is then the prune time. */
export async function pdfStatus(uuid: string): Promise<PdfStatus> {
  const res = await fetch(`/api/submissions/${encodeURIComponent(uuid)}/status`, {
    credentials: "same-origin",
  });
  if (!res.ok) throw new Error("Could not check the download status.");
  const data = (await res.json().catch(() => ({}))) as Partial<PdfStatus>;
  return {
    requested: data.requested ?? false,
    ready: data.ready ?? false,
    expires_at: data.expires_at ?? null,
  };
}

/** Trigger the browser download of the reassembled PDF. Intentionally does NOT call
 *  res.json(): the Worker streams application/pdf with Content-Disposition:
 *  attachment, and the HttpOnly cookie rides automatically on a same-origin
 *  navigation, so the browser honors the attachment without leaving the SPA. */
export function downloadPdf(uuid: string): void {
  window.location.assign(`/api/submissions/${encodeURIComponent(uuid)}/pdf`);
}

// ── PR-5: in-portal filed-form browse + multi-download ───────────────────────
// Any authenticated account may BROWSE an active job's filed forms and REQUEST their
// PDFs (mirrors the submit model: any account can submit to any active job). A requested
// download is bound to THIS account for 24h — a different account files its own request.

export interface FiledForm {
  submission_uuid: string;
  form_code: string;
  work_date: string;
  filed_at: number | null;
  /** This account holds a live (≤24h) request for this form. */
  requested: boolean;
  /** Downloadable now: the cache is populated AND this account has a live request. */
  ready: boolean;
}

/** One work-month bucket for the PR-6 Form Request cascade (Job → Month-Year → docs). */
export interface MonthBucket {
  /** "YYYY-MM" (the work-month, from work_date). */
  month: string;
  /** Filed-form count in that work-month. */
  count: number;
}

/** PR-6 cascade source: the work-months that have filed forms (newest-first, with counts)
 *  and the distinct form codes present for the job — populates the Month + Form dropdowns.
 *  A 404 (inactive/unknown job) returns empty arrays so the UI degrades cleanly. */
export async function fetchFiledMonths(
  jobId: string,
): Promise<{ months: MonthBucket[]; form_codes: string[] }> {
  const q = new URLSearchParams({ job_id: jobId });
  const res = await fetch(`/api/filed/months?${q.toString()}`, { credentials: "same-origin" });
  if (!res.ok) return { months: [], form_codes: [] };
  const data = (await res.json()) as { months?: MonthBucket[]; form_codes?: string[] };
  return { months: data.months ?? [], form_codes: data.form_codes ?? [] };
}

/** Browse an ACTIVE job's filed forms with THIS account's per-row request/ready state.
 *  PR-6: optionally narrow to a work-month (`month` = "YYYY-MM") and/or a `form_code`.
 *  With neither, the PR-5 all-filed behavior is unchanged. A 404 (inactive/unknown job)
 *  returns [] so the UI degrades cleanly. */
export async function fetchFiled(
  jobId: string,
  opts?: { month?: string; form_code?: string },
): Promise<FiledForm[]> {
  const q = new URLSearchParams({ job_id: jobId });
  if (opts?.month) q.set("month", opts.month);
  if (opts?.form_code) q.set("form_code", opts.form_code);
  const res = await fetch(`/api/filed?${q.toString()}`, { credentials: "same-origin" });
  if (!res.ok) return [];
  return ((await res.json()) as { filed: FiledForm[] }).filed;
}

/** Request caching for a batch of filed forms (≤20). The downloads are bound to THIS
 *  account for 24h. Returns how many were actually requested. */
export async function requestPdfs(uuids: string[]): Promise<number> {
  const res = await postJson("/api/request-pdfs", { uuids });
  if (!res.ok) throw new Error("Could not request the downloads.");
  return ((await res.json().catch(() => ({}))) as { requested?: number }).requested ?? 0;
}

// ── Admin: account management (session + role-gated /api/admin/*) ─────────────

export interface Account {
  username: string;
  role: Role;
  /** 1 = locked out (operator-disabled via the portal_admin CLI), 0 = active. */
  disabled: number;
  created_at: number;
}

/** Result of an admin mutation. `reauth` means the caller edited their OWN login
 *  (or role/deleted themselves) and must re-authenticate — the server cleared the
 *  session cookie. */
export interface AdminResult {
  ok: boolean;
  reauth?: boolean;
  username?: string;
  role?: Role;
}

/** Carries the server's machine-readable error code so the UI can map it to a
 *  human message (e.g. "last_admin" → "Can't remove the last admin"). */
export class AdminError extends Error {
  constructor(
    public code: string,
    public status: number,
  ) {
    super(code);
    this.name = "AdminError";
  }
}

async function adminPost(path: string, body: unknown): Promise<AdminResult> {
  const res = await postJson(path, body);
  const data = (await res.json().catch(() => ({}))) as AdminResult & { error?: string };
  if (!res.ok) throw new AdminError(data.error ?? `http_${res.status}`, res.status);
  return data;
}

export async function listAccounts(): Promise<Account[]> {
  const res = await fetch("/api/admin/users", { credentials: "same-origin" });
  if (res.status === 403) throw new AdminError("forbidden", 403);
  if (!res.ok) throw new Error("Could not load accounts.");
  return ((await res.json()) as { users: Account[] }).users;
}

export function createAccount(username: string, password: string, role: Role): Promise<AdminResult> {
  return adminPost("/api/admin/users", { username, password, role });
}

export function editCredentials(
  username: string,
  changes: { new_username?: string; new_password?: string },
): Promise<AdminResult> {
  return adminPost("/api/admin/users/credentials", { username, ...changes });
}

export function setRole(username: string, role: Role): Promise<AdminResult> {
  return adminPost("/api/admin/users/role", { username, role });
}

export function deleteAccount(username: string): Promise<AdminResult> {
  return adminPost("/api/admin/users/delete", { username });
}

// ── Admin: form-editor publish pipeline (session + role-gated /api/admin/publish) ──
// SEND-FREE enqueue: the Worker validates the composed definition and queues a
// publish_requests row; the Mac daemon is the sole privileged actuator (it commits /
// deploys). The SPA only ever (a) POSTs a request and (b) polls its status — it never
// writes the catalog or any form file directly. Mirrors the External Send Gate: the
// cloud can only queue.
import type { FormDefinition } from "../forms/types";

export type PublishOp = "create" | "edit" | "add_version" | "delete" | "rollback" | "recategorize";

/** The enqueue request. create/edit/add_version carry `definition`; delete/rollback
 *  carry `target_form_code` only; recategorize carries `category` only. `category` is also
 *  sent on `create` (the new parent's workflow). `identity` + `parent_form_code` come from the
 *  row identity (the server re-derives form_code = identity-v<version> from the definition). */
export interface PublishPayload {
  op: PublishOp;
  identity: string;
  parent_form_code: string;
  target_form_code?: string;
  definition?: FormDefinition;
  /** Workflow id (workflows.json) — required for create + recategorize; ignored otherwise. */
  category?: string;
}

/** A 400 from the publish gate carries a human-readable `reason` (the failing
 *  validation rule) — surface it verbatim. Distinct from AdminError (no `reason`). */
export class PublishError extends Error {
  constructor(
    public code: string,
    public status: number,
    public reason?: string,
  ) {
    super(reason ?? code);
    this.name = "PublishError";
  }
}

export interface PublishEnqueueResult {
  ok: boolean;
  id: number | null;
  status: "queued";
}

/** Enqueue a publish request. Throws PublishError on 400 (with the server `reason`),
 *  409 (publish_in_progress), or 403 (non-admin). */
export async function publishForm(payload: PublishPayload): Promise<PublishEnqueueResult> {
  const res = await postJson("/api/admin/publish", payload);
  const data = (await res.json().catch(() => ({}))) as
    & Partial<PublishEnqueueResult>
    & { error?: string; reason?: string };
  if (!res.ok) {
    throw new PublishError(data.error ?? `http_${res.status}`, res.status, data.reason);
  }
  return { ok: data.ok ?? true, id: data.id ?? null, status: "queued" };
}

/** One row of the publish state machine, as the status monitor renders it. */
export interface PublishRequest {
  id: number;
  created_at: string | number;
  updated_at: string | number;
  requested_by?: string;
  op: PublishOp;
  identity: string;
  parent_form_code: string;
  target_form_code: string | null;
  status: "queued" | "validated" | "tested" | "merged" | "live" | "archived" | "failed";
  failed_stage: string | null;
  failure_reason: string | null;
}

/** Read the publish state machine (most-recent first) for the monitor. */
export async function fetchPublishStatus(): Promise<PublishRequest[]> {
  const res = await fetch("/api/admin/publish-status", { credentials: "same-origin" });
  if (res.status === 403) throw new AdminError("forbidden", 403);
  if (!res.ok) throw new Error("Could not load publish status.");
  return ((await res.json()) as { requests: PublishRequest[] }).requests;
}

/** Clear all TERMINAL (archived / failed) publish requests from the monitor. Returns the
 *  count cleared. In-flight publishes are never touched (the Worker deletes only finished). */
export async function dismissFinishedPublishes(): Promise<number> {
  const res = await postJson("/api/admin/publish-dismiss");
  if (res.status === 403) throw new AdminError("forbidden", 403);
  if (!res.ok) throw new Error("Could not clear finished publishes.");
  return ((await res.json()) as { cleared?: number }).cleared ?? 0;
}

/** One request's full record incl. the composed definition_json — used to re-open a FAILED
 *  publish in the editor (so the admin's work isn't lost). */
export interface PublishRequestDetail {
  id: number;
  op: PublishOp;
  parent_form_code: string;
  identity: string;
  target_form_code: string | null;
  status: PublishRequest["status"];
  definition_json: string | null;
  category?: string | null;
}

export async function fetchPublishRequest(id: number): Promise<PublishRequestDetail> {
  const res = await fetch(`/api/admin/publish-request?id=${id}`, { credentials: "same-origin" });
  if (res.status === 403) throw new AdminError("forbidden", 403);
  if (!res.ok) throw new Error("Could not load the publish request.");
  return ((await res.json()) as { request: PublishRequestDetail }).request;
}
