import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
import { encodeCursor, decodeCursor } from "./cursor";
import { coerceLifecycle } from "./constants";
import type {
  ArchiveDirection,
  ArchiveState,
  CrewMember,
  DetailCrewMember,
  EquipmentOnSite,
  JobArchiveContainer,
  JobDetailResponse,
  JobInspection,
  JobListResponse,
  JobRow,
  JobTimeEntry,
  OpenTask,
  Task,
  ViewerPersonnel,
} from "./wire-types";

// Response shapes per BRIEF C (job tracker) — single-sourced in wire-types.ts (the SPA re-exports
// the same types, so a payload edit that drifts a shape fails the typecheck on both sides). The
// Job Tracker spans the job lifecycle, so the LIST filters by a validated `status` param (NOT a
// hard active=1 gate) — F5.

const STATUS_VALUES = new Set(["active", "closed", "on_hold", "all"]);
const NESTED_CAP = 20; // crew / open-tasks cap per job on the LIST card
const LEG_CAP = 200; // crew / equipment-on-site cap on the DETAIL (non-paginated legs)

/** JSON-text CC cell → string[]; malformed → [] (the po.ts twin — never a throw). */
function parseJsonArray(v: unknown): string[] {
  if (typeof v !== "string" || !v) return [];
  try {
    const a = JSON.parse(v);
    return Array.isArray(a) ? a.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

/** The daemon's per-container archive report (JSON text) → typed rows.
 *
 *  Degrades to [] on anything malformed. A bad cell must render as "no report yet", never take the
 *  whole job-detail page down — the operator most needs this view precisely when something went
 *  wrong, which is also when the cell is most likely to be odd. */
function parseContainers(v: unknown): JobArchiveContainer[] {
  if (typeof v !== "string" || !v) return [];
  try {
    const a = JSON.parse(v);
    if (!Array.isArray(a)) return [];
    return a
      .filter((x) => x && typeof x === "object" && !Array.isArray(x))
      .map((x) => ({
        key: typeof x.key === "string" ? x.key : "",
        label: typeof x.label === "string" ? x.label : "",
        moved: x.moved === true,
        note: typeof x.note === "string" ? x.note : "",
      }))
      .filter((x) => x.key && x.label);
  } catch {
    return [];
  }
}

function parseLimit(raw: string | undefined): number {
  const n = parseInt(raw || "50");
  return Math.min(Math.max(isNaN(n) ? 50 : n, 1), 200);
}

export function registerJobTrackerRoutes(app: FieldopsApp, gates: FieldopsGates): void {
  // GET /api/fieldops/jobs — status-filtered keyset page + page-scoped crew/open-task batches
  app.get(
    "/api/fieldops/jobs",
    gates.requireSession,
    gates.requireCapability("cap.jobtracker.read"),
    async (c) => {
      const q = c.req.query();
      const limit = parseLimit(q.limit);
      // F5: validate status against the fixed set; default active. Never a hard active=1 gate.
      const status = STATUS_VALUES.has(q.status ?? "") ? (q.status as string) : "active";
      const cursor = decodeCursor(q.cursor);

      // Jobs page (keyset on project_name ASC, job_id ASC; status filter via idx_jobs_status_name).
      // `all` ⇒ the ?1='all' OR branch makes the status predicate a no-op. client_name comes from a
      // LEFT JOIN to clients (jobs has client_id, not a denormalized name).
      // `lifecycle` rides the row because it is the CANONICAL job-state field (migration 0021).
      // `status` is the LEGACY flag and collapses inactive+archived into 'closed', so a consumer
      // reading only `status` structurally cannot tell an archived job from an inactive one — which
      // is exactly why the SPA displayed both as "Inactive". The list status FILTER still keys off
      // `status` (unchanged, and it is what idx_jobs_status_name covers); this is display data.
      const sqlJobs = `
        SELECT j.job_id, j.project_name, j.status, j.lifecycle, c.name AS client_name
        FROM jobs j
        LEFT JOIN clients c ON c.id = j.client_id
        WHERE (?1 = 'all' OR j.status = ?1)
          AND (?2 IS NULL OR j.project_name > ?2 OR (j.project_name = ?2 AND j.job_id > ?3))
        ORDER BY j.project_name ASC, j.job_id ASC
        LIMIT ?4
      `;
      const jobsParams = cursor
        ? [status, (cursor.p as string | null) ?? null, (cursor.j as string | null) ?? null, limit]
        : [status, null, null, limit];

      const jobsRes = await c.env.DB.prepare(sqlJobs).bind(...jobsParams).all<{
        job_id: string;
        project_name: string;
        status: string;
        lifecycle: string;
        client_name: string | null;
      }>();

      // (R7) viewer_current_job — where the SESSION user's own linked ACTIVE roster row is placed
      // (personnel.current_job), so the list can badge "Your job" (the subcontractor's direct path
      // to logging time). NULL when unlinked or unplaced. Deterministic pick on the soft link.
      const viewerRow = await c.env.DB.prepare(
        "SELECT current_job FROM personnel WHERE username = ?1 AND active = 1 ORDER BY id ASC LIMIT 1",
      )
        .bind(c.get("session").username)
        .first<{ current_job: string | null }>();
      const viewerCurrentJob = viewerRow?.current_job ?? null;

      if (!jobsRes.results || jobsRes.results.length === 0) {
        const empty: JobListResponse = { jobs: [], next_cursor: null, viewer_current_job: viewerCurrentJob };
        return c.json(empty, 200);
      }

      const pageJobIds = jobsRes.results.map((r) => r.job_id);
      const placeholders = pageJobIds.map(() => "?").join(",");

      // Crew = the people PLACED on each job (personnel.current_job, migration 0023 — the P2.6
      // crew→job placement), NOT the distinct assignees of task_assignments. The unified job-create
      // flow converged crew onto placement: `assignPersonnel` sets current_job, so THIS is the crew
      // that the assign controls drive. Page-scoped (idx_personnel_current_job), windowed to
      // ≤NESTED_CAP per job IN SQL (bounded O(page·cap), not O(all placed personnel)). NESTED_CAP is
      // a code constant, not user input — safe to interpolate (matches Brief B's rn<=5). (Open tasks
      // stay task_assignments-based, page-scoped on idx_task_assignments_job — tasks ARE task-based.)
      const sqlCrew = `
        SELECT job_id, id, name, trade FROM (
          SELECT p.current_job AS job_id, p.id, p.name, p.trade,
                 ROW_NUMBER() OVER (PARTITION BY p.current_job ORDER BY p.name ASC, p.id ASC) AS rn
          FROM personnel p
          WHERE p.current_job IN (${placeholders}) AND p.active = 1
        ) WHERE rn <= ${NESTED_CAP}
      `;
      // (G2.6) due_date rides the card's open-task preview for the Overdue pill. The rn window
      // ORDER stays created_at DESC (newest-first preview, the pre-G2.6 contract) — which ≤NESTED_CAP
      // tasks appear is unchanged; urgency ORDERING lives on /tasks/mine.
      const sqlOpenTasks = `
        SELECT id, job_id, description, status, personnel_name, due_date FROM (
          SELECT t.id, t.job_id, t.description, t.status, t.due_date, p.name AS personnel_name,
                 ROW_NUMBER() OVER (PARTITION BY t.job_id ORDER BY t.created_at DESC, t.id DESC) AS rn
          FROM task_assignments t LEFT JOIN personnel p ON p.id = t.personnel_id
          WHERE t.job_id IN (${placeholders}) AND t.status != 'done'
        ) WHERE rn <= ${NESTED_CAP}
      `;

      const [crewRes, tasksRes] = await c.env.DB.batch([
        c.env.DB.prepare(sqlCrew).bind(...pageJobIds),
        c.env.DB.prepare(sqlOpenTasks).bind(...pageJobIds),
      ]);

      // Group crew + open tasks by job_id, capped ≤NESTED_CAP per job in JS.
      const crewByJob = new Map<string, CrewMember[]>();
      for (const r of (crewRes.results ?? []) as { job_id: string; id: number; name: string; trade: string | null }[]) {
        const arr = crewByJob.get(r.job_id) ?? [];
        if (arr.length < NESTED_CAP) arr.push({ id: r.id, name: r.name, trade: r.trade });
        crewByJob.set(r.job_id, arr);
      }
      const tasksByJob = new Map<string, OpenTask[]>();
      for (const r of (tasksRes.results ?? []) as { id: number; job_id: string; description: string; status: string; personnel_name: string | null; due_date: string | null }[]) {
        const arr = tasksByJob.get(r.job_id) ?? [];
        if (arr.length < NESTED_CAP) arr.push({ id: r.id, description: r.description, status: r.status, personnel_name: r.personnel_name, due_date: r.due_date });
        tasksByJob.set(r.job_id, arr);
      }

      const jobs: JobRow[] = jobsRes.results.map((j) => ({
        job_id: j.job_id,
        project_name: j.project_name,
        status: j.status,
        // Coerced through coerceLifecycle so a pre-0021 row with a NULL/unknown value renders as
        // 'active' rather than leaking an arbitrary string into the SPA's typed union.
        lifecycle: coerceLifecycle(j.lifecycle),
        client_name: j.client_name,
        crew: crewByJob.get(j.job_id) ?? [],
        open_tasks: tasksByJob.get(j.job_id) ?? [],
      }));

      const last = jobsRes.results[jobsRes.results.length - 1];
      const nextCursor =
        jobsRes.results.length === limit ? encodeCursor({ p: last.project_name, j: last.job_id }) : null;

      const payload: JobListResponse = { jobs, next_cursor: nextCursor, viewer_current_job: viewerCurrentJob };
      return c.json(payload, 200);
    },
  );

  // GET /api/fieldops/jobs/:job_id — header (+ client) + five history legs (Promise.all via batch).
  // F5: serves a job of ANY status (closed/on_hold included); 404 only on a truly unknown job_id.
  app.get(
    "/api/fieldops/jobs/:job_id",
    gates.requireSession,
    gates.requireCapability("cap.jobtracker.read"),
    async (c) => {
      const jobId = c.req.param("job_id");

      // Header by PK + client join (jobs.client_id → clients).
      // 0057 + the routing SoR block ride the header so the SPA can DISPLAY the job's
      // Evergreen number / structured address and SEED the routing editor with current
      // values (pre-0057 the editor opened blank and a save re-sent only what was typed).
      const sqlHeader = `
        SELECT j.job_id, j.project_name, j.status, j.lifecycle,
               j.job_no, j.site_phase, j.address, j.address_city, j.address_state, j.address_zip,
               j.stakeholder_name, j.stakeholder_email, j.stakeholder_phone,
               j.safety_contact_name, j.safety_contact_email, j.safety_cc,
               j.progress_contact_name, j.progress_contact_email, j.progress_cc,
               j.archive_state, j.archive_direction, j.archive_requested_at,
               j.archive_completed_at, j.archive_attempts, j.archive_detail,
               c.name AS client_name, c.contact AS client_contact,
               c.phone AS client_phone, c.email AS client_email
        FROM jobs j
        LEFT JOIN clients c ON c.id = j.client_id
        WHERE j.job_id = ?
      `;
      const header = await c.env.DB.prepare(sqlHeader).bind(jobId).first<{
        job_id: string;
        project_name: string;
        status: string;
        lifecycle: string;
        archive_state: string;
        archive_direction: string;
        archive_requested_at: number | null;
        archive_completed_at: number | null;
        archive_attempts: number;
        archive_detail: string;
        job_no: string;
        site_phase: number;
        address: string;
        address_city: string;
        address_state: string;
        address_zip: string;
        stakeholder_name: string;
        stakeholder_email: string;
        stakeholder_phone: string;
        safety_contact_name: string;
        safety_contact_email: string;
        safety_cc: string;
        progress_contact_name: string;
        progress_contact_email: string;
        progress_cc: string;
        client_name: string | null;
        client_contact: string | null;
        client_phone: string | null;
        client_email: string | null;
      }>();
      if (!header) {
        return c.json({ error: "not_found" }, 404);
      }

      const q = c.req.query();
      const limit = parseLimit(q.limit);
      const taskCursor = decodeCursor(q.task_cursor);
      const timeCursor = decodeCursor(q.time_cursor);
      const inspCursor = decodeCursor(q.insp_cursor);

      // tasks (all statuses), keyset (created_at, id). (G2.6) due_date rides the SELECT for the
      // Overdue pill; the ORDER BY is deliberately UNCHANGED — this leg is keyset-paginated on
      // (created_at, id) and a due-date sort would break the cursor contract. Urgency ordering
      // lives on /tasks/mine (bounded, cursorless); here the pill carries the signal.
      const sqlTasks = `
        SELECT id, description, status, created_at, personnel_id, due_date,
               (SELECT name FROM personnel WHERE id = task_assignments.personnel_id) AS personnel_name
        FROM task_assignments
        WHERE job_id = ?1
          AND (?2 IS NULL OR created_at < ?2 OR (created_at = ?2 AND id < ?3))
        ORDER BY created_at DESC, id DESC
        LIMIT ?4
      `;
      // crew = people PLACED on this job (personnel.current_job, migration 0023), bounded. Converged
      // onto placement to match the assign controls (see the list route's crew leg for the rationale);
      // NOT task_assignments (tasks stay their own leg above). idx_personnel_current_job keys it.
      // (R7) + account_role via users (users.username is UNIQUE → no row fanout): the SPA disables
      // task-assign options an assign-only manager would 403 on (see DetailCrewMember).
      const sqlCrew = `
        SELECT p.id, p.name, p.trade, u.role AS account_role
        FROM personnel p
        LEFT JOIN users u ON u.username = p.username
        WHERE p.current_job = ?1 AND p.active = 1
        LIMIT ?2
      `;
      // time_entries (job-scoped), keyset (created_at, uuid). time_entries has no recorded_at →
      // alias created_at AS recorded_at; the keyset pages on the real created_at.
      // (R7) attribution joins:
      //   • task_description — the label of the task the entry was logged against (time_entries.task_id
      //     → task_assignments.description, migration 0016). Scalar subquery: no fanout, NULL when job-level.
      //   • recorded_by_name — WHO CREATED the entry (display name ONLY — no raw-username
      //     fallback, per the R1 W9 posture in fieldops_checklist.ts; unresolved → NULL). The write stamps
      //     actor_username (the authenticated session user — fieldops_time_write rule 3), which is a
      //     users.username, not a personnel id; resolve a display name through the personnel link
      //     (personnel.username is a soft non-unique link → scalar subquery, prefer the active row).
      //     NULL name (recorder has no roster row) renders as "—" in the SPA — never the raw username.
      // (G2.3) HEADS ONLY — an entry a later row amends is superseded and never listed (NOT EXISTS,
      //     never NOT IN: a NULL amends_uuid in a NOT IN subquery poisons the whole predicate;
      //     idx_time_entries_amends, 0034, keys the probe). The raw t.amends_uuid/t.actor_username
      //     are selected ONLY to derive amended/voided/can_amend below and are STRIPPED before
      //     c.json (the W9 posture: no raw usernames on the wire).
      const sqlTime = `
        SELECT t.uuid, t.hours, t.work_started_at, t.work_ended_at,
               t.created_at AS recorded_at, t.notes, t.personnel_id, p.name AS personnel_name,
               t.task_id,
               (SELECT description FROM task_assignments WHERE id = t.task_id) AS task_description,
               (SELECT name FROM personnel WHERE username = t.actor_username
                ORDER BY active DESC, id ASC LIMIT 1) AS recorded_by_name,
               t.amends_uuid, t.actor_username
        FROM time_entries t LEFT JOIN personnel p ON p.id = t.personnel_id
        WHERE t.job_id = ?1
          AND NOT EXISTS (SELECT 1 FROM time_entries x WHERE x.amends_uuid = t.uuid)
          AND (?2 IS NULL OR t.created_at < ?2 OR (t.created_at = ?2 AND t.uuid < ?3))
        ORDER BY t.created_at DESC, t.uuid DESC
        LIMIT ?4
      `;
      // equipment-on-site — fan-out FIX: candidates restricted to equipment EVER on this job,
      // windowed to each one's latest read, kept only if that latest is still THIS job.
      const sqlEquip = `
        SELECT e.id, e.name, e.kind, e.identifier, loc.label, loc.read_at
        FROM (
          SELECT equipment_id, label, read_at, job_id,
                 ROW_NUMBER() OVER (PARTITION BY equipment_id ORDER BY recorded_at DESC, id DESC) rn
          FROM equipment_location
          WHERE equipment_id IN (SELECT DISTINCT equipment_id FROM equipment_location WHERE job_id = ?1)
        ) loc JOIN equipment e ON e.id = loc.equipment_id
        WHERE loc.rn = 1 AND loc.job_id = ?1
        LIMIT ?2
      `;
      // inspections (job-scoped), keyset (created_at, uuid); scalar cols only (no payload_json).
      const sqlInsp = `
        SELECT i.uuid, i.form_code, i.version, i.performed_at,
               i.created_at AS recorded_at,
               (SELECT name FROM equipment WHERE id = i.equipment_id) AS equipment_name
        FROM inspections i
        WHERE i.job_id = ?1
          AND (?2 IS NULL OR i.created_at < ?2 OR (i.created_at = ?2 AND i.uuid < ?3))
        ORDER BY i.created_at DESC, i.uuid DESC
        LIMIT ?4
      `;

      const taskParams = taskCursor
        ? [jobId, (taskCursor.c as number | null) ?? null, (taskCursor.i as number | null) ?? null, limit]
        : [jobId, null, null, limit];
      const timeParams = timeCursor
        ? [jobId, (timeCursor.c as number | null) ?? null, (timeCursor.u as string | null) ?? null, limit]
        : [jobId, null, null, limit];
      const inspParams = inspCursor
        ? [jobId, (inspCursor.c as number | null) ?? null, (inspCursor.u as string | null) ?? null, limit]
        : [jobId, null, null, limit];

      // (R7) viewer_personnel — the SESSION user's own linked ACTIVE roster row (id + name), so the
      // SPA's log-time form can offer an explicit "Me (<name>)" default that resolves to a REAL
      // personnel_id (replacing the ambiguous "— me / unassigned —" that attributed time to nobody).
      // NULL when the viewer has no linked active personnel — the SPA then says so explicitly.
      // personnel.username is a soft non-unique link → deterministic pick (lowest active id).
      const viewer = c.get("session").username;
      const sqlViewer = `
        SELECT id, name FROM personnel
        WHERE username = ?1 AND active = 1
        ORDER BY id ASC LIMIT 1
      `;

      const caps = c.get("capabilities");
      const canSeeRoles = caps.has("cap.tasks.assign") || caps.has("cap.jobtracker.manage");
      // The routing SoR block (stakeholder + the WSR/WPR send-recipient/CC emails) is
      // served ONLY to the capability that can EDIT it (the seed block's sole consumer) —
      // read-tier submitter/subcontractor accounts get null (adversarial review 2026-07-20:
      // serving it unconditionally was a least-privilege regression; pre-0057 this data was
      // reachable in-browser only at cap.po.manage / the internal token tier).
      const canSeeRouting = caps.has("cap.jobtracker.manage");
      // Archive state rides the detail for cap.job.archive holders only — same least-privilege
      // shape as `routing`. A read-tier viewer sees the job, not its relocation machinery.
      const canArchive = caps.has("cap.job.archive");
      const [tasksRes, crewRes, timeRes, equipRes, inspRes, viewerRes] = await c.env.DB.batch([
        c.env.DB.prepare(sqlTasks).bind(...taskParams),
        c.env.DB.prepare(sqlCrew).bind(jobId, LEG_CAP),
        c.env.DB.prepare(sqlTime).bind(...timeParams),
        c.env.DB.prepare(sqlEquip).bind(jobId, LEG_CAP),
        c.env.DB.prepare(sqlInsp).bind(...inspParams),
        c.env.DB.prepare(sqlViewer).bind(viewer),
      ]);

      const tasks = (tasksRes.results ?? []) as Task[];
      // (G2.3) Derive the chain-state booleans, then STRIP the raw columns (W9: the wire carries
      // display names + booleans, never actor usernames). can_amend mirrors the amend route's WHO
      // rule exactly (recorder OR cap.personnel.manage) so the SPA's Edit/Void controls only show
      // where the Worker would accept — the Worker stays the boundary either way.
      const canAmendAll = caps.has("cap.personnel.manage");
      const timeEntries = ((timeRes.results ?? []) as (JobTimeEntry & {
        amends_uuid: string | null;
        actor_username: string;
      })[]).map(({ amends_uuid, actor_username, ...t }): JobTimeEntry => ({
        ...t,
        amended: amends_uuid !== null,
        voided: amends_uuid !== null && t.hours === 0,
        can_amend: canAmendAll || actor_username === viewer,
      }));
      const inspections = (inspRes.results ?? []) as JobInspection[];

      const tasksCursor =
        tasks.length === limit
          ? encodeCursor({ c: tasks[tasks.length - 1].created_at, i: tasks[tasks.length - 1].id })
          : null;
      const timeNext =
        timeEntries.length === limit
          ? encodeCursor({ c: timeEntries[timeEntries.length - 1].recorded_at, u: timeEntries[timeEntries.length - 1].uuid })
          : null;
      const inspNext =
        inspections.length === limit
          ? encodeCursor({ c: inspections[inspections.length - 1].recorded_at, u: inspections[inspections.length - 1].uuid })
          : null;

      const payload: JobDetailResponse = {
        job: {
          job_id: header.job_id,
          project_name: header.project_name,
          status: header.status,
          lifecycle: coerceLifecycle(header.lifecycle),
          // 0057 + routing SoR: display + edit-form seeding. CC arrays are stored as
          // JSON text; parse defensively (a malformed cell yields [] — never a throw).
          job_no: header.job_no ?? "",
          // 0064 — the Evergreen identifier's third segment; the SPA recombines the two
          // for display so the operator reads and edits the full `YYYY.NNN.S` number.
          site_phase: header.site_phase ?? 0,
          archive: !canArchive ? null : {
            state: (header.archive_state || "none") as ArchiveState,
            direction: (header.archive_direction || "") as ArchiveDirection,
            requested_at: header.archive_requested_at ?? null,
            completed_at: header.archive_completed_at ?? null,
            attempts: header.archive_attempts ?? 0,
            // The daemon writes this as JSON; a malformed cell must degrade to "no report yet",
            // never throw and take the whole detail page down with it.
            containers: parseContainers(header.archive_detail),
          },
          routing: !canSeeRouting ? null : {
            address: header.address ?? "",
            address_city: header.address_city ?? "",
            address_state: header.address_state ?? "",
            address_zip: header.address_zip ?? "",
            stakeholder_name: header.stakeholder_name ?? "",
            stakeholder_email: header.stakeholder_email ?? "",
            stakeholder_phone: header.stakeholder_phone ?? "",
            safety_contact_name: header.safety_contact_name ?? "",
            safety_contact_email: header.safety_contact_email ?? "",
            safety_cc: parseJsonArray(header.safety_cc),
            progress_contact_name: header.progress_contact_name ?? "",
            progress_contact_email: header.progress_contact_email ?? "",
            progress_cc: parseJsonArray(header.progress_cc),
          },
          client: header.client_name
            ? {
                name: header.client_name,
                contact: header.client_contact,
                phone: header.client_phone,
                email: header.client_email,
              }
            : null,
          // (R7 review) account_role is org-hierarchy metadata — expose it ONLY to actors who can
          // actually assign tasks (the pickers that consume it); other readers get null.
          crew: ((crewRes.results ?? []) as DetailCrewMember[]).map((m) =>
            canSeeRoles ? m : { ...m, account_role: null },
          ),
          tasks,
          time_entries: timeEntries,
          equipment_on_site: (equipRes.results ?? []) as EquipmentOnSite[],
          inspections,
        },
        cursors: { tasks: tasksCursor, time: timeNext, insp: inspNext },
        viewer_personnel: (viewerRes.results?.[0] as ViewerPersonnel | undefined) ?? null,
      };
      return c.json(payload, 200);
    },
  );
}
