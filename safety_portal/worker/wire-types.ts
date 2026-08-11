// SINGLE-SOURCE WIRE TYPES — the Worker's JSON response shapes for the field-ops read surfaces
// the SPA consumes (optimization slice 3, finding #11). TYPE-ONLY module, importable from BOTH
// tsconfig scopes: the Worker types its `c.json` payloads with these (so a route edit that drifts
// a shape fails `tsc -p tsconfig.worker.json`), and the SPA libs re-export them for their callers
// and test fixtures (so the DailyReportTab fixture type-checks against the shape the Worker
// actually sends, not a hand-maintained copy). No framework, no codegen — just one definition.
//
// Covered endpoints:
//   • GET /api/fieldops/jobs                    → JobListResponse        (fieldops_jobtracker.ts)
//   • GET /api/fieldops/jobs/:job_id            → JobDetailResponse      (fieldops_jobtracker.ts)
//   • GET /api/fieldops/daily-form/status       → DailyFormStatus        (fieldops_daily_requirements.ts)
//   • GET /api/fieldops/daily-form/requirements → DailyRequirementsResponse (fieldops_daily_requirements.ts)
//   • GET /api/fieldops/expected-materials      → ExpectedMaterialsResponse (fieldops_expected_materials.ts)
//   • GET /api/fieldops/checklist/assigned      → AssignedInspectionsResponse (fieldops_checklist.ts)
//   • GET /api/fieldops/tasks/mine              → MyTasksResponse        (fieldops_tasks.ts)
//
// SPA re-export homes: src/lib/fieldops_jobtracker.ts, src/lib/fieldops_daily_form.ts,
// src/lib/fieldops_expected_materials.ts, src/lib/fieldops_checklist.ts, src/lib/fieldops_tasks.ts.

// ── GET /api/fieldops/jobs (job-tracker LIST) ────────────────────────────────────────────────────

export interface CrewMember {
  id: number;
  name: string;
  trade: string | null;
}

/** (R7) Detail crew row: + the linked account's role so pickers can pre-disable task-assign
 *  options the Worker's subcontractor-target guard will 403 (an assign-only manager may only
 *  target 'submitter'-linked personnel; no login → null → also rejected). Presentation only —
 *  the Worker re-gates; non-assigners receive null. */
export interface DetailCrewMember extends CrewMember {
  account_role: string | null;
}

export interface OpenTask {
  id: number;
  description: string;
  status: string;
  personnel_name: string | null;
  /** (G2.6) Optional deadline, 'YYYY-MM-DD' Pacific calendar date (migration 0035); null = no
   *  deadline. Overdue = status !== 'done' AND due_date < Pacific-today (myTasksShared.pacificToday). */
  due_date: string | null;
}

/** The canonical job-state field (`jobs.lifecycle`, migration 0021).
 *
 *  Do NOT infer state from `status`: that legacy column maps active→'active' but BOTH
 *  inactive and archived→'closed', so it structurally cannot distinguish the last two
 *  (`active` is a derived int with the same collapse). Anything user-facing reads
 *  `lifecycle`. Lockstep with `JOB_LIFECYCLES` in worker/constants.ts. */
export type JobLifecycle = "active" | "inactive" | "archived";

export interface JobRow {
  job_id: string;
  project_name: string;
  /** LEGACY. Retained because the list's status filter and its index key off it. */
  status: string;
  lifecycle: JobLifecycle;
  progress: number;
  client_name: string | null;
  crew: CrewMember[];
  open_tasks: OpenTask[];
}

export interface JobListResponse {
  jobs: JobRow[];
  next_cursor: string | null;
  /** (R7) Where the viewer's own linked roster row is placed — drives the "Your job" list badge.
   *  null = unlinked/unplaced. Optional for back-compat; the live worker always sends it. */
  viewer_current_job?: string | null;
}

// ── GET /api/fieldops/jobs/:job_id (job-tracker DETAIL) ─────────────────────────────────────────

export interface Task {
  id: number;
  description: string;
  status: string;
  created_at: number;
  personnel_id: number | null;
  personnel_name: string | null;
  /** (G2.6) Optional deadline, 'YYYY-MM-DD' (0035); null = no deadline. See OpenTask.due_date. */
  due_date: string | null;
}

export interface JobTimeEntry {
  uuid: string;
  hours: number | null;
  work_started_at: number | null;
  work_ended_at: number | null;
  recorded_at: number;
  notes: string | null;
  /** (G2.3) The entry's subject (personnel.id); null = job-level. Backs the amend-form prefill. */
  personnel_id: number | null;
  personnel_name: string | null;
  /** (R7) The task the entry was logged against (task_assignments.description); null = job-level. */
  task_id: number | null;
  task_description: string | null;
  /** (R7) WHO CREATED the entry — the write's actor_username stamp resolved to the roster display
   *  name. Display name ONLY (R1 W9 posture) — null when the recorder has no roster row; never a
   *  raw username. */
  recorded_by_name: string | null;
  /** (G2.3) This head row IS a correction (its amends_uuid is non-null) → "corrected" pill. */
  amended: boolean;
  /** (G2.3) A correction with hours = 0 (the void) → struck-through + "voided" pill. */
  voided: boolean;
  /** (G2.3) The viewer may amend/void this entry (they recorded it, or hold cap.personnel.manage) —
   *  worker-computed so the raw actor_username stays OFF the wire (W9 posture); drives the SPA's
   *  Edit/Void controls. UI convenience only — the amend route re-gates. */
  can_amend: boolean;
}

export interface EquipmentOnSite {
  id: number;
  name: string;
  kind: string | null;
  identifier: string | null;
  label: string | null;
  read_at: number | null;
}

export interface JobInspection {
  uuid: string;
  form_code: string;
  version: number;
  performed_at: number | null;
  recorded_at: number;
  equipment_name: string | null;
}

export interface JobClient {
  name: string;
  contact: string | null;
  phone: string | null;
  email: string | null;
}

/** 0057 — the routing/SoR block served on the detail header: display + the routing
 *  editor's seed values (pre-0057 the editor opened blank). Street stays in `address`;
 *  city/state/zip are the structured columns. */
export interface JobRoutingBlock {
  address: string;
  address_city: string;
  address_state: string;
  address_zip: string;
  stakeholder_name: string;
  stakeholder_email: string;
  stakeholder_phone: string;
  safety_contact_name: string;
  safety_contact_email: string;
  safety_cc: string[];
  progress_contact_name: string;
  progress_contact_email: string;
  progress_cc: string[];
}

/** Where a job sits in the archive workflow (migration 0058).
 *
 *  `requested` and `in_progress` mean the Mac-side pass owns the row; `partial` means SOME
 *  containers moved and the rest are retryable; `failed` means none did. `partial` is deliberately
 *  distinct from `failed` because the operator's repair differs between "4 of 6 moved" and
 *  "nothing happened". */
export type ArchiveState =
  | "none" | "requested" | "in_progress" | "complete" | "partial" | "failed";

/** Which way the in-flight relocation is going. '' when `state` is 'none'. */
export type ArchiveDirection = "" | "archive" | "unarchive";

/** One relocatable container's outcome, as the daemon reported it.
 *
 *  `label` is operator-facing copy ("Safety folder"), NOT the internal key — a half-archived job
 *  has to be legible without opening a runbook. */
export interface JobArchiveContainer {
  key: string;
  label: string;
  moved: boolean;
  note: string;
}

export interface JobArchiveStatus {
  state: ArchiveState;
  direction: ArchiveDirection;
  /** Epoch seconds; null when the job has never entered the workflow. */
  requested_at: number | null;
  completed_at: number | null;
  attempts: number;
  /** Empty until the daemon's first progress report. */
  containers: JobArchiveContainer[];
}

/** The response from POST /:job_id/archive and /:job_id/unarchive. */
export interface JobArchiveResponse {
  ok: true;
  job_id: string;
  archive: { state: ArchiveState; direction: ArchiveDirection };
}

export interface JobDetail {
  job_id: string;
  project_name: string;
  /** LEGACY — see JobRow.status. */
  status: string;
  lifecycle: JobLifecycle;
  /** Archive workflow state — `cap.job.archive` holders ONLY; null for everyone else, the same
   *  least-privilege shape as `routing`. */
  archive: JobArchiveStatus | null;
  progress: number;
  /** The Evergreen YYYY.NNN PROJECT number ('' when unassigned) — 0057. NOT the full
   *  identifier: the site segment rides in `site_phase`. */
  job_no: string;
  /** The identifier's third segment (0064), 0 when the job has no site breakdown. The SPA
   *  joins the two for display (lib/jobNumber.formatJobNumber) so the operator sees and
   *  edits `2026.384.1`, never the split. */
  site_phase: number;
  /** The routing/SoR seed block — cap.jobtracker.manage ONLY; null for read-tier callers
   *  (least-privilege: these are the external-send recipient/CC sets). */
  routing: JobRoutingBlock | null;
  client: JobClient | null;
  crew: DetailCrewMember[];
  tasks: Task[];
  time_entries: JobTimeEntry[];
  equipment_on_site: EquipmentOnSite[];
  inspections: JobInspection[];
}

/** (R7) The session user's own linked ACTIVE roster row — backs the log-time "Me (<name>)"
 *  default. null = no linked personnel (the form says so instead of guessing). */
export interface ViewerPersonnel {
  id: number;
  name: string;
}

export interface JobDetailResponse {
  job: JobDetail;
  cursors: { tasks: string | null; time: string | null; insp: string | null };
  /** Optional for back-compat with cached/older responses; the live worker always sends it. */
  viewer_personnel?: ViewerPersonnel | null;
}

// ── GET /api/fieldops/daily-form/status ─────────────────────────────────────────────────────────

/** The latest submission for one parent-form family on (job, date). `filed_by_name` is the
 *  personnel DISPLAY NAME resolved through submitted_as — NULL when the account has no roster
 *  link (never a raw username; the W9 posture — the UI drops the "by …" clause on NULL). */
export interface FiledEntry {
  filed_at: number; // epoch seconds (submissions.created_at)
  filed_by_name: string | null;
}

/** GET /api/fieldops/daily-form/status response. `filed` is keyed by PARENT form family (the
 *  DAILY_STATUS_FAMILIES set — src/shared/daily_families.ts) — a family with no submission for
 *  (job, date) is simply absent. `daily_filed` mirrors filed["daily-report"] (the banner's key). */
export interface DailyFormStatus {
  filed: Record<string, FiledEntry>;
  daily_filed: FiledEntry | null;
}

// ── GET /api/fieldops/daily-form/requirements ───────────────────────────────────────────────────

/** The closed requirement-item vocabulary (D1 job_daily_requirements.kind, migration 0030;
 *  number/date/select added by 0032 — slice D5). */
export type DailyRequirementKind =
  | "note"
  | "confirm"
  | "text"
  | "form_link"
  | "number"
  | "date"
  | "select";

/** One admin-authored per-job requirement item, as served by
 *  GET /api/fieldops/daily-form/requirements (active items only, seq order, bounded). */
export interface DailyRequirementItem {
  id: number;
  seq: number;
  kind: DailyRequirementKind;
  label: string;
  form_code: string | null; // form_link only: a catalog PARENT family code
  options: string[] | null; // select only: the pick-one choices (D1 stores JSON; served PARSED)
}

export interface DailyRequirementsResponse {
  job_id: string;
  items: DailyRequirementItem[];
}

// ── GET /api/fieldops/expected-materials ────────────────────────────────────────────────────────

export type ExpectedMaterialStatus = "expected" | "received" | "incident";

export interface ExpectedMaterialRow {
  id: number;
  material_id: number | null; // catalog-picked rows; null = free-text
  material_name: string | null; // resolved catalog model_id (display; null for free-text rows)
  description: string | null;
  qty: number | null;
  unit: string | null;
  expected_date: string | null; // YYYY-MM-DD
  status: ExpectedMaterialStatus;
  received_at: number | null; // epoch seconds, stamped by receive/flag-incident
  received_by_name: string | null; // DISPLAY NAME ONLY (W9) — null when the account has no roster link
  qty_received: number | null;
  note: string | null;
  seq: number;
  // M3 Slice 1 — the stable per-line mirror key (migration 0039). Carried into the daily form's
  // "Report a problem →" deep-link so a material-incident submission can reference THIS M2 line
  // (validated server-side in /api/submit). Nullable (schema-level); live rows are non-null.
  line_uuid: string | null;
  // ── Materials tracking (PR2, migration 0059) ──────────────────────────────────────────────
  part_number: string | null; // BOM part no.; the key PR3's manifest importer matches shipments on
  category: string | null; // BOM grouping (e.g. 'HARDWARE'); display + page grouping only
  /** Expected SHIP date (YYYY-MM-DD). Distinct from `expected_date`, which keeps its 0031
   *  meaning — the expected DELIVERY date. The UI labels the pair ship/delivery. */
  expected_ship_date: string | null;
  /** The delivery ROLLUP, DERIVED from material_receipt_events (latest event by id wins);
   *  null = never marked. THIS — not `status` — is the three-way delivery state. `status` stays
   *  the coarse legacy projection the daily form, the §51 Material List mirror and the M3
   *  incident join already read, and it carries the orthogonal `incident` flag. */
  receipt_status: MaterialReceiptKind | null;
  /** Running total: SUM of every event qty for this line. null = nothing quantified yet, which
   *  is deliberately distinct from a recorded 0. */
  qty_received_total: number | null;
}

/** One delivery mark. Append-only: material_receipt_events (0059) is the delivery SoR; the
 *  LINE's `status` is a coarse projection of it. */
export type MaterialReceiptKind = "delivered" | "partial" | "not_delivered";

export interface MaterialReceiptEventRow {
  id: number;
  line_id: number;
  shipment_id: number | null; // the load this mark was against, when known
  kind: MaterialReceiptKind;
  qty: number | null; // received on THIS event; always null for not_delivered
  note: string | null;
  event_date: string | null; // YYYY-MM-DD (defaults to Pacific today at write)
  created_at: number; // epoch seconds
  actor_name: string | null; // DISPLAY NAME ONLY (W9) — null when the account has no roster link
}

/** One SCHEDULED shipment attached to an expected-material LINE. A shipping-log row is a LOAD,
 *  not a line: ship + delivery dates and the BOL/load number live HERE, while the line's own
 *  expected_ship_date/expected_date are the office's line-level expectation. */
export interface MaterialShipmentRow {
  id: number;
  line_id: number;
  part_number: string | null; // what the shipping log said — provenance of the match
  bol_number: string | null;
  carrier: string | null;
  qty: number | null;
  unit: string | null;
  ship_date: string | null; // YYYY-MM-DD
  delivery_date: string | null; // YYYY-MM-DD
  seq: number;
  source: "manual" | "import";
}

export interface ExpectedMaterialsResponse {
  expected_materials: ExpectedMaterialRow[];
  // REQUIRED, not optional, on purpose: tsconfig covers src/, so every SPA mock that omits them
  // fails `npm run typecheck` — that is the registry teeth keeping the two surfaces in step.
  shipments: MaterialShipmentRow[];
  receipt_events: MaterialReceiptEventRow[];
}

// ── GET /api/fieldops/checklist/assigned ────────────────────────────────────────────────────────

export type ChecklistItemStatus = "open" | "done";

/** G1 — the item's photo lifecycle (latest item_photos row, migration 0036): pending = queued
 *  for the Mac §34 screen ("screening…"), clean = screened + filed to Box ("photo on file ✓"),
 *  refused = the screen refused it (retry allowed). NULL = no photo. Option D (RATIFIED): the
 *  SPA renders STATUS ONLY — no image bytes are ever served to a browser. */
export type ItemPhotoStatus = "pending" | "clean" | "refused";

/** One per-instance item state (the snapshot + completion row, migration 0026
 *  checklist_item_states). `filed_by` — WHO filed the submission that auto-closed this item
 *  (completed_by === '(auto)'): the personnel DISPLAY NAME only (W9 — no raw-username fallback);
 *  NULL for manually-completed / still-open items, or when no matching submission resolves
 *  (best-effort attribution). `photo_status` — the G1 photo lifecycle (derived from item_photos,
 *  NOT from photo_ref prefix parsing, so a clobbered/legacy photo_ref never lies about state). */
export interface ChecklistItemState {
  id: number;
  source_item_id: number | null;
  item_type: string;
  label: string | null;
  form_code: string | null;
  target_count: number | null;
  status: ChecklistItemStatus;
  note: string | null;
  photo_ref: string | null;
  completed_by: string | null;
  completed_at: number | null;
  value_num: number | null;
  filed_by: string | null;
  photo_status: ItemPhotoStatus | null;
  /** The item was authored "requires photo" (config_json.requires_photo on the source item, surfaced
   *  live) — it can't be marked done until a live photo is attached. SQLite has no bool → 0/1 over the
   *  wire; treat as truthy. */
  requires_photo: boolean;
}

/** POST /api/fieldops/checklist/item-state/:id/photo success body (G1 Slice 1). */
export interface ItemPhotoUploadResult {
  ok: boolean;
  photo_id: number;
  photo_status: "pending";
  photo_ref: string; // 'pending:<photo_id>' — stamped on the item state in the same batch
}

export interface AssignedInstance {
  id: number;
  job_id: string | null;
  project_name: string | null;
  instance_date: string | null;
  status: "open" | "complete";
  /** (R1) The assigned template's title, SNAPSHOTTED at assign time (migration 0029) — render
   *  this, never "Inspection #<id>". NULL only on legacy instances the backfill couldn't resolve. */
  template_title: string | null;
  created_at: number;
  /** (#17, Seam A) Whether this COMPLETE inspection has already been signed-off + logged to the
   *  weekly progress report (derived server-side from the emitted_submission_uuid one-shot marker,
   *  migration 0041). The assigned-inspection view shows the "Sign & log to progress report" action
   *  only when the feature is live AND status==='complete' AND !progress_logged; once logged it
   *  shows a "Logged to progress report ✓" pill instead. */
  progress_logged: boolean;
}

export interface AssignedInspection {
  instance: AssignedInstance;
  items: ChecklistItemState[];
}

/** (R1) `linked` = whether the session has an ACTIVE linked personnel row — an unlinked account
 *  CANNOT have assignments, so the UI can explain the empty list. Instances arrive OPEN-FIRST
 *  (server CASE ordering), newest first within a status band. */
export interface AssignedInspectionsResponse {
  inspections: AssignedInspection[];
  linked: boolean;
}

// ── Recurring checklists (#16) ────────────────────────────────────────────────────────────────
/** The cadence an inspection recurrence spawns on. Extensible — the Worker's RECURRENCE_CADENCES set
 *  (worker/fieldops_recurrence.ts) is the validation authority; keep this union in sync with it. */
export type RecurrenceCadence = "daily" | "weekly" | "biweekly" | "monthly";

/** One ACTIVE recurring definition (GET /api/fieldops/checklist/recurrences) — the admin visibility
 *  row for a per-job recurring generator (join-resolved assignee + job names). `last_generated_date`
 *  is the watermark the cron advances (NULL until the first spawn). */
export interface ChecklistRecurrence {
  id: number;
  template_id: number;
  template_title: string | null;
  assignee_personnel_id: number | null;
  assignee_name: string | null;
  job_id: string | null;
  project_name: string | null;
  cadence: string;
  anchor_date: string;
  last_generated_date: string | null;
  created_at: number;
}

// ── GET /api/fieldops/tasks/mine ────────────────────────────────────────────────────────────────

/** One of the caller's own assigned tasks (cap.tasks.own; resolved server-side through the
 *  session's linked personnel row). `assigned_by` = who last placed the task (actor username,
 *  stamped by the create/assign routes; NULL on pre-stamping historical rows). */
export interface MyTask {
  id: number;
  job_id: string;
  project_name: string | null;
  description: string;
  status: string;
  created_at: number;
  assigned_by: string | null;
  /** (G2.6) Optional deadline, 'YYYY-MM-DD' Pacific calendar date (migration 0035); null = no
   *  deadline. Within a status band the server orders dated tasks first, due_date ASC
   *  (overdue → soonest-due), undated last. Overdue = status !== 'done' AND < Pacific-today. */
  due_date: string | null;
}

/** (CS4 #12) The viewer's OWN current placement, resolved server-side from their linked ACTIVE
 *  personnel row (personnel.current_job → jobs). SELF-INFORMATION ONLY: `personnel_id`/`name` are
 *  the caller's own roster row (their own display name — the W9 posture), `job_id`/`project_name`
 *  their own standing placement; nothing about any other account or person rides this shape.
 *  null = unlinked OR linked-but-unplaced (disambiguate with `linked`). `project_name` is null
 *  only when the placement names a job the jobs table no longer carries (soft ref). */
export interface ViewerTaskPlacement {
  job_id: string;
  project_name: string | null;
  personnel_id: number;
  name: string;
}

/** GET /api/fieldops/tasks/mine. Tasks arrive OPEN-FIRST (open < in_progress < done, newest first
 *  within a band — server CASE ordering). `linked` = whether the session has an ACTIVE linked
 *  personnel row (an unlinked account CANNOT have tasks — the UI explains the empty list).
 *  `viewer_placement` collapses the Daily tab's placement waterfall: the tab used to derive its
 *  job from a full jobs-list page (fetchJobList → viewer_current_job); now the one endpoint it
 *  already reads carries the placement. */
export interface MyTasksResponse {
  tasks: MyTask[];
  linked: boolean;
  viewer_placement: ViewerTaskPlacement | null;
}

// ── Daily-report photo POOL (DR-photo-pool Slice 1, migration 0037) ─────────────────────────────

/** One additional-photo REFERENCE riding the submission payload (values.additional_photos).
 *  Tiny on purpose: the photo BYTES went to the pool via their own bounded request (POST
 *  /api/fieldops/daily-photo) because the inline site_photos field is payload-budgeted (CS2:
 *  280KB × 4 ≈ 1.49MB base64 < PAYLOAD_MAX 1.8MB — more inline photos cannot fit). At submit
 *  the Worker validates + CLAIMS every referenced pool row (fieldops_daily_photos.
 *  claimAdditionalPhotos); `caption` is display text only (untrusted downstream, ≤300 chars). */
export interface AdditionalPhotoRef {
  pool_id: number;
  caption?: string;
}

/** POST /api/fieldops/daily-photo success body. The pool row starts 'pending' (queued for the
 *  Slice-2 Mac §34 screen — the same lifecycle vocabulary as ItemPhotoStatus; Option D: status
 *  only, no bytes are ever served back). */
export interface DailyPhotoUploadResult {
  ok: boolean;
  pool_id: number;
  status: "pending";
}

/** One row of GET /api/fieldops/daily-photos (the actor's OWN pool rows for a (job, work_date):
 *  unclaimed, plus — in amend mode — rows claimed by the verified `amends=` submission) — STATUS
 *  ONLY, never photo bytes (Option D). `status` reuses the G1 ItemPhotoStatus vocabulary:
 *  pending "Screening…" / clean "Photo on file ✓" / refused retry. */
export interface DailyPoolPhotoRow {
  id: number;
  status: ItemPhotoStatus;
  created_at: number;
  screened_at: number | null;
  /** SQLite boolean (0/1). 1 = claimed by the verified `amends=` submission — the ONLY way a
   *  claimed row reaches the SPA (the amend read); it chips "Photo on file ✓" and is ref-drop
   *  only, never pool-deletable. 0 on every pre-submit pool row. */
  claimed: number;
}

/** GET /api/fieldops/daily-photos response envelope. */
export interface DailyPhotosListResponse {
  photos: DailyPoolPhotoRow[];
}

// ─── Materials-manifest import (PR3b) ────────────────────────────────────────────────────────
//
// The office uploads a BOM / shipping log; the Mac daemon parses it into a REVIEWABLE GRID plus a
// PROPOSED column map, and the validate screen disposes. These are the read shapes that surface
// carries. Request/body shapes live in src/lib/fieldops_manifests.ts per the scope rule.

/** `job_manifests.status` (migration 0060 CHECK). `parsed` is the only state the validate screen
 *  can act on; `committing` means a paged commit is mid-flight and `committed` is terminal. */
export type ManifestStatus =
  | "pending"
  | "claimed"
  | "refused"
  | "parsed"
  | "committing"
  | "committed"
  | "discarded";

/** `manifest_parse.ParsedRow.kind` — the classification the parser gave one source row.
 *  `meta` rows are document preamble (a DELTA BOM's CLIENT / PROJECT block), never importable. */
export type ManifestRowKind = "header" | "data" | "continuation" | "section" | "meta";

/** One manifest in the per-job list. Never carries the hmac and never the document bytes. */
export interface ManifestListRow {
  id: number;
  manifest_uuid: string;
  job_id: string;
  filename: string;
  declared_mime: string;
  size_bytes: number;
  status: ManifestStatus;
  /** Machine reason on a refusal (e.g. `screen:malicious:L3:…`, `extract_failed`). Never bytes. */
  detail: string | null;
  /** `manifest_parse` document profile — customer_bom / delta_bom / shipping_log / … */
  profile: string | null;
  row_count: number | null;
  mode: "merge" | "add_new" | null;
  committed_through_row: number;
  uploaded_by: string;
  box_file_id: string | null;
  created_at: number;
  parsed_at: number | null;
  committed_at: number | null;
}

/** GET /api/fieldops/manifests?job_id= */
export interface ManifestListResponse {
  manifests: ManifestListRow[];
}

/** The PROPOSED concept→column mapping. `mapping` is concept → column index; `labels` names the
 *  columns a header alone cannot distinguish (a DELTA BOM's seven identical QUANTITY headers get
 *  their product codes here); `qty_candidates` is every column that could plausibly BE the
 *  quantity, in the order the picker should offer them; `qty_default` is the evidence-backed
 *  pre-selection (the highest REV column on a revision BOM). JSON object keys are strings. */
export interface ManifestColumnMap {
  mapping: Record<string, number>;
  labels: Record<string, string>;
  qty_candidates: number[];
  qty_default: number | null;
}

/** GET /api/fieldops/manifests/:id — the header the validate screen renders its evidence from. */
export interface ManifestDetailResponse {
  manifest: ManifestListRow & {
    column_map_json: string | null;
    header_meta_json: string | null;
    /** The parser's notes, newline-joined — including the DELTA arithmetic cross-check
     *  ("revision cross-check: … equals the chosen revision on 47/47 rows"), which is the
     *  evidence the screen SHOWS for its pre-selected quantity column. */
    parse_notes: string | null;
    merge_options_json: string | null;
  };
  preview_pages: number[];
}

/** One row of the parsed grid, stored VERBATIM. `cells_json` is a JSON array of the source cells;
 *  nothing is pre-collapsed, so duplicate part numbers survive to get a per-row human decision. */
export interface ManifestGridRow {
  row_index: number;
  source_page: string | null;
  kind: ManifestRowKind;
  cells_json: string;
  /** Comma-joined `ParsedRow.flags` — `qty_unparseable`, `orphan_continuation`. '' when clean. */
  flags: string | null;
}

/** GET /api/fieldops/manifests/:id/rows?after=&limit= — cursor is `row_index`. */
export interface ManifestRowsResponse {
  rows: ManifestGridRow[];
}

/** GET /api/fieldops/manifests/:id/preview/:page — a rendered source page, base64 PNG. The ONLY
 *  view a browser gets of the source document; the original bytes never leave the Mac-ward tier. */
export interface ManifestPreviewResponse {
  page: number;
  png_b64: string;
}

/** An incoming line whose part number matches EXACTLY ONE existing active line. */
export interface ManifestPlanMatch {
  source_row_index: number;
  part_number: string;
  line_id: number;
}

/** An incoming line whose part number matches MORE THAN ONE existing line. Not a rounding error:
 *  duplicate part numbers are universal in the real BOMs (7000153 appears twice in three of the
 *  four sample Customer BOMs, under different groupings), so there is no single correct target
 *  and the screen MUST ask per row rather than silently pick a winner. */
export interface ManifestPlanAmbiguous {
  source_row_index: number;
  part_number: string;
  line_ids: number[];
}

/** A line the job already expects that this document does NOT mention. Reported, never
 *  auto-retired — a partial BOM is not a deletion order. */
export interface ManifestPlanAbsent {
  line_id: number;
  part_number: string | null;
}

/** POST /api/fieldops/manifests/:id/plan — a DRY RUN against the job's live lines. Writes nothing. */
export interface ManifestPlanResponse {
  ok: true;
  job_id: string;
  committed_through_row: number;
  counts: {
    incoming: number;
    matched: number;
    ambiguous: number;
    new: number;
    absent: number;
    existing: number;
  };
  matched: ManifestPlanMatch[];
  ambiguous: ManifestPlanAmbiguous[];
  absent: ManifestPlanAbsent[];
  projected_total: number;
  /** The materials read route caps a job at 500 lines; past that the page and the daily form
   *  SILENTLY truncate, so the screen warns BEFORE anyone commits. */
  would_exceed_line_cap: boolean;
}

/** POST /api/fieldops/manifests/:id/commit — ONE page of the import. `done` false means re-post
 *  the remainder; every line at or below `committed_through_row` is dropped before any write, so
 *  a replayed page is a genuine no-op rather than a duplicate import. */
export interface ManifestCommitResponse {
  ok: true;
  done: boolean;
  inserted: number;
  committed_through_row: number;
}
