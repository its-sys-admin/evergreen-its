import type { Context } from "hono";
import type { Env, Vars } from "./types";

// Shared per-job ownership-scope machinery (RUNTIME module — fieldops_gates.ts stays type-only).
// Extracted from the three identical copies in fieldops_checklist / fieldops_daily_requirements /
// fieldops_expected_materials (optimization slice 3, finding #2): this is a SECURITY GATE, and a
// triplicated gate is a partial-fan-out hazard — a fix applied to one copy silently misses the
// other two. One definition, N call sites.
//
// The BYPASS-CAP SETS stay in the calling modules on purpose (requireJobScope takes them as an
// explicit parameter): they are intentionally divergent — checklist + daily-requirements bypass on
// cap.jobtracker.manage / cap.checklist.manage, expected-materials on cap.jobtracker.manage ALONE
// (cap.materials.manage was removed from that set on 2026-08-25 when it was granted to the manager
// tier — see migration 0079) — and hiding the divergence inside this module would invite the
// opposite bug (one surface silently inheriting another's admin set).

type Ctx = Context<{ Bindings: Env; Variables: Vars }>;

/** Job must exist (active or not — checklists/requirements/expectations can be authored on any
 *  real job). Returns the error Response (400 bad shape / 404 unknown job), or null on success. */
export async function requireJob(c: Ctx, jobId: string): Promise<Response | null> {
  if (jobId.length < 1 || jobId.length > 64) return c.json({ error: "invalid_job_id" }, 400);
  const job = await c.env.DB.prepare("SELECT job_id FROM jobs WHERE job_id = ?1").bind(jobId).first();
  if (!job) return c.json({ error: "not_found" }, 404);
  return null;
}

/** Resolve the acting session → its linked ACTIVE personnel row (personnel.username ==
 *  users.username; the nullable soft link from migration 0014). Returns the personnel id +
 *  current_job placement, or null when the account has no active linked personnel row. active=1 so
 *  a retired roster person can't own live work. LIMIT 1 on the (unconstrained) username link —
 *  deterministic lowest id. */
export async function resolveActorPersonnel(
  c: Ctx,
): Promise<{ id: number; current_job: string | null } | null> {
  const username = c.get("session").username;
  const row = await c.env.DB.prepare(
    "SELECT id, current_job FROM personnel WHERE username = ?1 AND active = 1 ORDER BY id ASC LIMIT 1",
  )
    .bind(username)
    .first<{ id: number; current_job: string | null }>();
  return row ?? null;
}

/** Daily-report family role gate (operator directive 2026-07-03): the SOP daily field report is
 *  a MANAGER/ADMIN surface — a subcontractor (role key 'submitter', relabeled in migration 0027)
 *  must not reach it even when placed on a job. Gated on the SESSION ROLE (the per-request D1
 *  read requireSession sets — a closed three-value vocabulary, coerceRole fail-safe), NOT a new
 *  capability: the daily report is a role-tier boundary exactly like the submit-as admin gate,
 *  and minting a cap for a fixed two-role set would only re-create the vestigial-cap class the
 *  CS4 Slice-4 audit cleaned up. One definition, N call sites (the requireJobScope extraction
 *  rationale — a security gate must never be copy-pasted): the two daily-form reads, the two
 *  expected-material receipt writes, and the /api/submit daily-tab family check all call THIS.
 *  Returns the 403 Response (error 'forbidden_role'), or null when the role may file. */
export function requireDailyReportRole(c: Ctx): Response | null {
  const role = c.get("role");
  if (role !== "manager" && role !== "admin") return c.json({ error: "forbidden_role" }, 403);
  return null;
}

/** Per-job ownership scope (the /daily-form/status pattern, security-review posture): a non-bypass
 *  actor may only touch a job that is their OWN placement (linked ACTIVE personnel.current_job ===
 *  jobId) — 403 forbidden_job otherwise. `bypassCaps` is the calling surface's OWN admin set (see
 *  module header — the sets are intentionally divergent and always passed explicitly). Returns the
 *  403 Response, or null when the actor is in scope. */
export async function requireJobScope(
  c: Ctx,
  jobId: string,
  bypassCaps: readonly string[],
): Promise<Response | null> {
  const caps = c.get("capabilities");
  if (bypassCaps.some((cap) => caps.has(cap))) return null;
  const person = await resolveActorPersonnel(c);
  if (!person || person.current_job !== jobId) return c.json({ error: "forbidden_job" }, 403);
  return null;
}
