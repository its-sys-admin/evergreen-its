import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
import type { JobTaskRow, JobTasksResponse, MyTask, MyTasksResponse, ViewerTaskPlacement } from "./wire-types";

// Assigned-Tasks tab (P4 field-ops feature) S1 — "My Tasks" READ. The subcontractor / manager sees
// the one-off tasks assigned to THEM. "Assigned to me" resolves the session's account → its linked
// personnel row (personnel.username == users.username, migration 0014's nullable soft link) → the
// tasks WHERE personnel_id = that. A session with NO linked personnel (no personnel row carrying its
// username) sees an EMPTY list — not an error. Send-free (D1 read only); cap.tasks.own gated.
//
// R1 contract refinements:
//   • ORDER: open work FIRST (status CASE open < in_progress < done), tiebreak created_at DESC —
//     the old `status ASC` was lexicographic and floated DONE tasks to the top of the tab.
//   • `assigned_by` (stamped by the create route since 0014; NULL on historical rows) + created_at
//     give the row its context ("who gave me this, when").
//   • `linked: boolean` — whether the session has an ACTIVE linked personnel row — lets the UI
//     distinguish "no tasks" from "your account isn't linked to the roster" (see the R1 spec's
//     empty-state reason codes; the sibling daily surface returns `reason`, fieldops_checklist.ts).
//
// CS4 (#12 waterfall collapse): the response ALSO carries `viewer_placement` — the caller's OWN
// standing placement (their linked ACTIVE personnel row's current_job + one indexed jobs lookup
// for the project name), the same personnel resolution fieldops_scope.resolveActorPersonnel uses.
// The Daily tab used to resolve this by downloading a FULL Job Tracker list page
// (fetchJobList("active") → viewer_current_job) as stage 1 of a 2-stage waterfall; now the one
// endpoint the My Tasks page already reads carries it, and the jobs-list query leaves the daily
// path entirely.
//
// SECURITY NOTE (deliberate, reviewed): cap.tasks.own now returns the caller's OWN placement.
// This is SELF-INFORMATION — the viewer's own roster row id, own display name (W9: display name
// only, and only to its owner), and own current_job — resolved strictly from the session username;
// no parameter selects whose placement is returned, so there is NO cross-user exposure. Previously
// the same fact rode cap.jobtracker.read (viewer_current_job on the jobs list); a cap.tasks.own-only
// account learns nothing here about any other account, person, or job beyond its own placement's
// project name — which its holder could already see on every form they file against that job.

// Per-account bound. A person's own assigned tasks are few; this cap is a defensive ceiling, not a
// paginated surface (unlike the Job Tracker's keyset legs).
const MY_TASKS_CAP = 500;
// Job-scoped read cap — a real job carries tens of assigned tasks; 500 bounds a pathological one.
const JOB_TASKS_CAP = 500;

export function registerMyTasksRoutes(app: FieldopsApp, gates: FieldopsGates): void {
  // GET /api/fieldops/tasks/mine — the caller's own assigned tasks across all jobs, with the job's
  // project name (LEFT JOIN jobs — a task's job_id is a soft ref, so a missing job → null name, never
  // a dropped row). Ordered open-first (CASE), then due-date urgency, then created_at DESC (see the
  // G2.6 note on the query). The personnel link is resolved by
  // matching personnel.username to the session username (authoritative from requireSession's D1 read);
  // no linked personnel → the JOIN yields nothing → empty list + linked:false.
  app.get(
    "/api/fieldops/tasks/mine",
    gates.requireSession,
    gates.requireCapability("cap.tasks.own"),
    async (c) => {
      const username = c.get("session").username;
      // The viewer's own ACTIVE personnel row (the same active=1 link + deterministic lowest-id
      // pick as fieldops_scope.resolveActorPersonnel) + their placement's project name via one
      // indexed jobs lookup (LEFT JOIN — current_job is a soft ref; a vanished job → null name).
      // Row present → linked:true; current_job present → viewer_placement (see the module-header
      // security note: self-information only).
      const viewerRow = await c.env.DB.prepare(
        `SELECT p.id AS personnel_id, p.name, p.current_job AS job_id, j.project_name
         FROM personnel p LEFT JOIN jobs j ON j.job_id = p.current_job
         WHERE p.username = ?1 AND p.active = 1
         ORDER BY p.id ASC LIMIT 1`,
      )
        .bind(username)
        .first<{ personnel_id: number; name: string; job_id: string | null; project_name: string | null }>();
      const viewerPlacement: ViewerTaskPlacement | null =
        viewerRow && viewerRow.job_id !== null
          ? {
              job_id: viewerRow.job_id,
              project_name: viewerRow.project_name,
              personnel_id: viewerRow.personnel_id,
              name: viewerRow.name,
            }
          : null;
      // (G2.6) ORDER BY extension — WITHIN each status band (the R1 open-first CASE, unchanged):
      //   1. dated tasks before undated (due_date IS NULL sorts last — no deadline, no urgency);
      //   2. due_date ASC — one ascending key gives overdue-first THEN soonest-due (an overdue
      //      date is simply the smallest 'YYYY-MM-DD'; no Pacific-today comparison needed in SQL);
      //   3. created_at DESC, id DESC — the pre-G2.6 tiebreak, now scoped to equal-due (and to
      //      the undated remainder, whose relative order is therefore EXACTLY the old contract).
      const sql = `
        SELECT t.id, t.job_id, j.project_name, t.description, t.status, t.created_at, t.assigned_by, t.due_date
        FROM task_assignments t
        JOIN personnel p ON p.id = t.personnel_id
        LEFT JOIN jobs j ON j.job_id = t.job_id
        WHERE p.username = ?1
        ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END ASC,
                 (t.due_date IS NULL) ASC, t.due_date ASC,
                 t.created_at DESC, t.id DESC
        LIMIT ?2
      `;
      const res = await c.env.DB.prepare(sql).bind(username, MY_TASKS_CAP).all<MyTask>();
      const payload: MyTasksResponse = {
        tasks: res.results ?? [],
        linked: viewerRow !== undefined && viewerRow !== null,
        viewer_placement: viewerPlacement,
      };
      return c.json(payload, 200);
    },
  );

  // ── GET /api/fieldops/tasks?job_id= — ONE job's assigned tasks (the Site Tasks page). ──────────
  // Read tier: cap.jobtracker.read — the identical exposure the job-detail response's tasks leg
  // already grants that capability (fieldops_jobtracker.ts), so no data class widens; this route
  // exists so the Site Tasks page doesn't over-fetch the detail's five other legs. WHO resolves
  // through personnel.name (display-name-only, House Reflex §5). `viewer_personnel_id` +
  // `viewer_privileged` power the client-side own-only gating on the status buttons — the WRITE
  // route re-enforces ownership server-side either way (its in-WHERE predicate), so these are
  // display hints, never the boundary.
  app.get(
    "/api/fieldops/tasks",
    gates.requireSession,
    gates.requireCapability("cap.jobtracker.read"),
    async (c) => {
      const jobId = c.req.query("job_id") ?? "";
      if (!jobId || jobId.length > 64) return c.json({ error: "invalid_job_id" }, 400);
      const job = await c.env.DB
        .prepare("SELECT project_name FROM jobs WHERE job_id = ?1")
        .bind(jobId)
        .first<{ project_name: string }>();
      if (!job) return c.json({ error: "unknown_job" }, 404);
      const username = c.get("session").username;
      const caps = c.get("capabilities");
      const viewer = await c.env.DB
        .prepare(
          "SELECT id FROM personnel WHERE username = ?1 AND active = 1 ORDER BY id ASC LIMIT 1",
        )
        .bind(username)
        .first<{ id: number }>();
      // Same ordering contract as /tasks/mine (open-first, dated-first due ASC, then recency).
      const res = await c.env.DB
        .prepare(
          `SELECT t.id, t.description, t.status, t.due_date, t.created_at,
                  t.personnel_id, p.name AS assignee_name
           FROM task_assignments t
           LEFT JOIN personnel p ON p.id = t.personnel_id
           WHERE t.job_id = ?1
           ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END ASC,
                    (t.due_date IS NULL) ASC, t.due_date ASC,
                    t.created_at DESC, t.id DESC
           LIMIT ${JOB_TASKS_CAP}`,
        )
        .bind(jobId)
        .all<JobTaskRow>();
      const payload: JobTasksResponse = {
        tasks: res.results ?? [],
        project_name: job.project_name,
        viewer_personnel_id: viewer?.id ?? null,
        viewer_privileged: caps.has("cap.jobtracker.manage") || caps.has("cap.tasks.assign"),
      };
      return c.json(payload, 200);
    },
  );
}
