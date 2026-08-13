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
 *  distinct from `failed` because the operator's repair differs between "4 of 8 moved" and
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
  /** The job's display name for the page heading ("Materials — Deep Lake", never the
   *  JOB-###### key). REQUIRED for the same mock-teeth reason; null when the job row
   *  vanished mid-read. */
  project_name: string | null;
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
  /** CONSERVATIVE (add_new) arithmetic — the mode that adds the most rows — so a generic
   *  consumer is never under-told. The per-mode pair below is what the screen shows. */
  projected_total: number;
  projected_total_add_new: number;
  /** Merge inserts only the UNMATCHED lines; matched + resolved ones become UPDATEs. */
  projected_total_merge: number;
  /** The materials read route caps a job at 500 lines; past that the page and the daily form
   *  SILENTLY truncate, so the screen warns BEFORE anyone commits. */
  would_exceed_line_cap: boolean;
  would_exceed_line_cap_merge: boolean;
}

/** POST /api/fieldops/manifests/:id/commit — ONE page of the import. `done` false means re-post
 *  the remainder; every line at or below `committed_through_row` is dropped before any write, so
 *  a replayed page is a genuine no-op rather than a duplicate import. */
export interface ManifestCommitResponse {
  ok: true;
  done: boolean;
  inserted: number;
  /** Merge-mode: existing lines this page rewrote in place. */
  updated: number;
  /** Shipments-import: loads landed in material_shipments this page. */
  shipments: number;
  /** Merge-mode: matched lines whose facts are LOCKED (received/incident) — reported,
   *  never silently rewritten. */
  skipped_locked: { source_row_index: number; line_id: number; status: string }[];
  committed_through_row: number;
}

// ─── Job-schedule import (ADR-0006) ──────────────────────────────────────────────────────────
//
// The office uploads a project-schedule PDF (Smartsheet Gantt export); the Mac daemon renders +
// OCRs it into a REVIEWABLE task grid plus a PROPOSED column map, and the validate screen
// disposes. These are the read shapes that surface carries. Request/body shapes live in
// src/lib/fieldops_schedules.ts per the scope rule.

/** `job_schedules.status` (migration 0066 CHECK). `parsed` is the only state the validate screen
 *  can act on; `committing` means a paged commit is mid-flight; `committed` means THIS upload is
 *  the job's governing schedule (at most one per job — idx_job_schedules_one_committed);
 *  `superseded` means a later revision's commit displaced it (the job's revision history). */
export type ScheduleStatus =
  | "pending"
  | "claimed"
  | "refused"
  | "parsed"
  | "committing"
  | "committed"
  | "superseded"
  | "discarded";

/** Proposed-row kind (0066 CHECK). `section` rows are the phase headers the Gantt export nests
 *  tasks under — LNTP Work / Deliveries / Civil / Mechanical / Electrical / … — and are what the
 *  committed task list stores as each task's `section`. */
export type ScheduleRowKind = "header" | "data" | "continuation" | "section" | "meta";

/** One schedule upload in the per-job list. Never carries the hmac and never the document
 *  bytes. Discarded rows are excluded server-side; superseded rows ARE served (revision
 *  history). */
export interface ScheduleListRow {
  id: number;
  schedule_uuid: string;
  job_id: string;
  filename: string;
  declared_mime: string;
  size_bytes: number;
  status: ScheduleStatus;
  /** Machine reason on a refusal (e.g. `screen:malicious:L3:…`, `ocr_failed`). Never bytes. */
  detail: string | null;
  /** `schedule_parse` document profile — gantt_export / grid_export / … */
  profile: string | null;
  row_count: number | null;
  committed_through_row: number;
  uploaded_by: string;
  box_file_id: string | null;
  created_at: number;
  parsed_at: number | null;
  committed_at: number | null;
  superseded_at: number | null;
}

/** GET /api/fieldops/schedules?job_id= */
export interface ScheduleListResponse {
  schedules: ScheduleListRow[];
}

/** The PROPOSED concept→column mapping for the OCR-reconstructed grid. `mapping` is concept →
 *  cell index (concepts: task_name / duration / start_date / finish_date / percent_done /
 *  predecessors); `labels` names columns the header alone cannot distinguish. JSON object keys
 *  are strings. */
export interface ScheduleColumnMap {
  mapping: Record<string, number>;
  labels: Record<string, string>;
}

/** GET /api/fieldops/schedules/:id — the header the validate screen renders its evidence from. */
export interface ScheduleDetailResponse {
  schedule: ScheduleListRow & {
    column_map_json: string | null;
    header_meta_json: string | null;
    /** The parser's evidence + consistency flags, newline-joined — including the
     *  plausibility warnings (implausible year, finish-before-start) that point the human
     *  at likely OCR digit misreads, which arrive at confidence 1.0 and are otherwise
     *  invisible. */
    parse_notes: string | null;
    resolutions_json: string | null;
  };
  preview_pages: number[];
}

/** One row of the proposed grid, stored VERBATIM. `cells_json` is a JSON array of the proposed
 *  cells; nothing is pre-collapsed, so a duplicated task name within a section survives to get
 *  a per-row human decision at reconcile. */
export interface ScheduleGridRow {
  row_index: number;
  source_page: string | null;
  kind: ScheduleRowKind;
  cells_json: string;
  /** Comma-joined consistency flags — `implausible_year`, `finish_before_start`,
   *  `percent_out_of_range`, `duration_span_mismatch`. '' when clean. */
  flags: string | null;
}

/** GET /api/fieldops/schedules/:id/rows?after=&limit= — cursor is `row_index`. */
export interface ScheduleRowsResponse {
  rows: ScheduleGridRow[];
}

/** GET /api/fieldops/schedules/:id/preview/:page — a rendered source page, base64 PNG. The ONLY
 *  view a browser gets of the source document; the original bytes never leave the Mac-ward
 *  tier. */
export interface SchedulePreviewResponse {
  page: number;
  png_b64: string;
}

/** One living-task-list row (`job_schedule_tasks`, migration 0071) as the Schedule page reads
 *  it. WHO fields are DISPLAY-NAME-ONLY (W9): `delivered_by_name` / `last_marked_by_name`
 *  resolve through personnel; the stored account username never leaves the Worker. Flags are
 *  the stored 0/1. `schedule_percent` is what the last committed schedule asserted (the PR-6
 *  three-way diff base); `percent_done` is the portal's current belief; `last_marked_at`
 *  non-null ⇔ a human marked the task in the portal (PR-5 — commits leave it NULL). */
export interface ScheduleTaskRow {
  id: number;
  task_uuid: string;
  job_id: string;
  section: string | null;
  name: string;
  duration_days: number | null;
  start_date: string | null;
  finish_date: string | null;
  /** Stamped at the task's OWN first commit, never rewritten — the slip anchor. */
  baseline_start_date: string | null;
  baseline_finish_date: string | null;
  percent_done: number;
  schedule_percent: number | null;
  is_milestone: number;
  is_contract_milestone: number;
  is_delivery: number;
  delivered_date: string | null;
  delivered_by_name: string | null;
  delivered_at: number | null;
  /** Verbatim from the source document — never parsed, display + provenance only. */
  predecessors_raw: string | null;
  sort_order: number;
  last_marked_by_name: string | null;
  last_marked_at: number | null;
  created_at: number;
  updated_at: number;
}

/** GET /api/fieldops/schedule-tasks?job_id= — active tasks in document order, plus the job's
 *  display name (the heading says the project, never the JOB-###### key). `truncated` means
 *  the 600-row read cap was hit and the page is NOT showing the whole schedule (a commit may
 *  create up to 2000 tasks) — the SPA surfaces it rather than rendering a silent partial. */
export interface ScheduleTasksResponse {
  tasks: ScheduleTaskRow[];
  project_name: string | null;
  truncated: boolean;
}

// The reconcile classification shapes (PR-6) are defined beside the diff engine itself —
// worker/schedule_diff.ts is pure, so a type-only re-export keeps one source of truth
// without giving this file a runtime dependency.
export type {
  ScheduleAmbiguityReason,
  ScheduleInfoChange,
  SchedulePercentRule,
  SchedulePlanAmbiguous,
  SchedulePlanDateChange,
  SchedulePlanFresh,
  SchedulePlanMatched,
  SchedulePlanPercent,
  SchedulePlanRemoved,
  ScheduleRemovalReason,
} from "./schedule_diff";
import type {
  SchedulePlanAmbiguous as PlanAmbiguous,
  SchedulePlanFresh as PlanFresh,
  SchedulePlanMatched as PlanMatched,
  SchedulePlanRemoved as PlanRemoved,
} from "./schedule_diff";

/** POST /api/fieldops/schedules/:id/plan — a DRY RUN, now the full three-way reconcile
 *  classification (ADR-0006 decision 9, PR-6). `degenerate` true means the job has no
 *  existing task list — every data row classifies `fresh` and the commit is a plain
 *  first import. Otherwise the four arrays carry the classified plan: matched pairs with
 *  their per-pair field diffs (dates two-way, the three-way % rule, silent-informational
 *  changes), BLOCKING ambiguities, fresh rows, and removed tasks flagged blocking
 *  (marked / delivered / contract-milestone — never silently destroyed). The plan writes
 *  nothing and never 409s; the COMMIT re-derives this same diff server-side. */
export interface SchedulePlanResponse {
  ok: true;
  degenerate: boolean;
  /** Always true since PR-6 — kept so a stale bundle's check keeps working. */
  revision_reconcile_available: boolean;
  counts: {
    incoming: number;
    new: number;
    existing: number;
    matched: number;
    ambiguous: number;
    removed: number;
    blocking_removals: number;
    percent_conflicts: number;
  };
  matched: PlanMatched[];
  ambiguous: PlanAmbiguous[];
  fresh: PlanFresh[];
  removed: PlanRemoved[];
}

/** POST /api/fieldops/schedules/:id/commit — ONE page of the 0066-watermark commit. `done`
 *  false means re-post the same full payload (and the same `resolutions`); every row at or
 *  below `committed_through_row` is dropped before any write, so a replayed page (a lost
 *  final response's retry) is an idempotent no-op rather than a duplicate task list.
 *  `inserted` counts fresh INSERTs, `updated` matched-pair UPDATEs, `linked` human-linked
 *  (rename) UPDATEs; `removed` counts the soft-deactivations, which land only on the FINAL
 *  page (removals are defined against the whole document). */
export interface ScheduleCommitResponse {
  ok: true;
  done: boolean;
  inserted: number;
  updated: number;
  linked: number;
  removed: number;
  committed_through_row: number;
}

/** POST /api/fieldops/schedule-tasks/:id/progress — the PR-5 field %-mark
 *  (cap.schedule.mark, per-job scoped). Echoes the persisted percent. */
export interface ScheduleMarkProgressResponse {
  ok: true;
  id: number;
  percent_done: number;
}

/** POST /api/fieldops/schedule-tasks/:id/milestone-done — `already` true on the idempotent
 *  repeat (the mark had landed before; nothing re-stamped, nothing re-audited). */
export interface ScheduleMarkMilestoneDoneResponse {
  ok: true;
  id: number;
  already?: true;
}

/** POST /api/fieldops/schedule-tasks/:id/delivered — echoes the persisted business date
 *  (the body's, or today Pacific when the body carried none). */
export interface ScheduleMarkDeliveredResponse {
  ok: true;
  id: number;
  delivered_date: string;
}

// ── Weekly Production Report (0067) ──────────────────────────────────────────────────────────
// The office screen and the Mac compile read the SAME payload from the SAME builder, so these
// shapes are the single source of truth for both. `worker/fieldops_report.ts` annotates its
// response with `ProductionReportResponse`, which is what makes a drift between the Worker and
// the SPA a typecheck failure rather than a runtime surprise on a client's document.

/** One day's weather as the daily report captured it. `inclement` is the OFFICE's marked
 *  weather-day claim, never inferred from `conditions` — it is a contractual delay claim. */
export interface WeeklyReportWeatherDay {
  work_date: string;
  conditions: string;
  avg_temp: string;
  inclement: boolean;
}

/** A labor-seed row. Either a JHA sign-in COMPANY (workers = the week's peak daily count of
 *  distinct signers under that company) or, when the week has no JHA sign-ins, a crew the field
 *  typed into crew_progress (workers = peak typed manpower; MAX, not sum — the same crew on five
 *  days is one crew). Man-hours are absent by design: `personnel` has no employer column and
 *  subcontractors create crew + file time too (migration 0027), so a per-company hours split
 *  would be invented data. */
export interface WeeklyReportCrew {
  company: string;
  workers: number;
  days: number;
}

/** A photo offered for curation. Only `status='clean'` pool rows WITH a `box_file_id` appear —
 *  a row earns that id only on a CLEAN §34 disposition, so an unscreened photo cannot reach a
 *  client report through this route. */
export interface WeeklyReportPhoto {
  pool_id: number;
  work_date: string;
  box_file_id: string;
  caption: string;
}

export interface WeeklyReportSafetyCount {
  month: number;
  to_date: number;
}

/** The office's Labor Report row. `workers`/`man_hours` are STRINGS so "" (not answered) stays
 *  distinguishable from "0" (answered zero) — the report prints an empty cell for the former. */
export interface WeeklyReportLaborRow {
  company: string;
  workers: string;
  man_hours: string;
}

/** The office-entered record — the three sections D1 structurally cannot derive, plus the two
 *  judgment calls (weather days, photo curation). */
export interface WeeklyReportOffice {
  header: {
    site_location: string;
    ess_management: string;
    mobilization_date: string;
    subcontractors: string[];
    prepared_by: string;
  };
  safety: Record<string, WeeklyReportSafetyCount>;
  weather: { inclement_dates: string[]; weather_days_to_date: number };
  labor: { rows: WeeklyReportLaborRow[] };
  /** THREE-STATE, like `photos`: null = never touched (the report uses its derived seed),
   *  "" = deliberately cleared, text = the office's words. */
  narrative: {
    critical_items: string | null;
    upcoming_activities: string | null;
    hazard_topics: string[];
  };
  pending: { rfis: string; submittals: string; ifc_review: string; change_orders: string };
  /** null = auto-select; [] = explicitly no photos; list = the office's ordered picks. */
  photos: WeeklyReportPhoto[] | null;
  /** true only when THIS week's row was saved. A carried-forward week is NOT saved, which is why
   *  its narrative still re-derives from this week's field text. */
  saved: boolean;
  /** The week these values were inherited from, or null. */
  carried_from: string | null;
  updated_by: string | null;
  updated_at: number | null;
}

/** GET /api/internal/production-report · GET /api/fieldops/weekly-report — one derivation, two
 *  gate tiers. `schedule` stays null until the ADR-0006 living task list lands. */
export interface ProductionReportResponse {
  job_id: string;
  week: { start: string; end: string; from: number; to: number };
  job: {
    project_name: string; address: string; address_city: string; address_state: string;
    job_no: string; site_phase: number; status: string;
  } | null;
  daily_report_count: number;
  weather: {
    days: WeeklyReportWeatherDay[];
    weather_days_week: number;
    weather_days_to_date: number;
  };
  // `seed_source` is REQUIRED, not optional: the builder always knows which derivation produced
  // `crews`, and optionality would let a consumer silently miss it. "jha" = the week's JHA
  // worker-acknowledgement sign-ins (preferred — a signer states an employer); "daily" = the
  // crew_progress free-text fallback. The Mac compile reads only total_hours/crews and ignores
  // unknown keys.
  labor: { total_hours: number; crews: WeeklyReportCrew[]; seed_source: "jha" | "daily" };
  crew_progress: { work_date: string; crew: string; manpower: string; progress: string }[];
  daily_notes: {
    work_date: string; tomorrows_goals: string; comments: string;
    safety_observations: string; manpower_total: string; prepared_by: string;
  }[];
  hazard_form_codes: string[];
  deliveries: {
    event_date: string; kind: string; item: string; part_number: string;
    qty: string; unit: string; vendor: string; bol_number: string; carrier: string;
  }[];
  material_incidents: { work_date: string; material: string; issue: string; details: string }[];
  photos: { available: WeeklyReportPhoto[]; selected: WeeklyReportPhoto[]; auto_selected: boolean };
  /** Page 3. NULL when the job has no committed schedule (0071) — the renderer prints its
   *  honest empty state. `percent: null` on an item is the table's "never reported" state (no
   *  portal mark, no committed schedule value), which prints as an em dash, never 0%. */
  schedule: null | {
    sections: { name: string; items: { label: string; percent: number | null }[] }[];
    /** Tasks past their finish date and short of 100%, oldest slip first — the Critical Items seed. */
    behind: {
      name: string; section: string; finish_date: string; percent: number;
      is_contract_milestone: boolean;
    }[];
    /** The server's Pacific date the behind-schedule set was derived against. */
    today: string;
    task_count: number;
  };
  office: WeeklyReportOffice;
  generated_at: number;
}

/** PUT /api/fieldops/weekly-report — omit `photos` entirely to leave curation on auto-select. */
export interface WeeklyReportSaveBody {
  job_id: string;
  week_start: string;
  header?: Partial<WeeklyReportOffice["header"]>;
  safety?: Record<string, Partial<WeeklyReportSafetyCount>>;
  weather?: { inclement_dates?: string[]; weather_days_to_date?: number };
  labor?: { rows?: WeeklyReportLaborRow[] };
  narrative?: Partial<WeeklyReportOffice["narrative"]>;
  pending?: Partial<WeeklyReportOffice["pending"]>;
  photos?: WeeklyReportPhoto[];
}

// ── Job payments (ADR-0006 decision 10, PR-7 — migration 0073) ──────────────────────────────
// Office-only (cap.payments.manage, admin — operator decision 4); DISPLAY-ONLY (Invariant 1:
// nothing here sends, reminds or generates a notice). Served by worker/fieldops_payments.ts;
// states derived at read by worker/payments_derive.ts — NEVER stored.

/** A cycle's DERIVED display state (payments_derive.ts — the exact precedence lives there).
 *  The *_sent states mean a human RECORDED that date; the *_due states mean the terms'
 *  clock says the next escalation step has fallen due. Nothing is ever sent by ITS. */
export type PaymentState =
  | "draft"
  | "awaiting"
  | "due_soon"
  | "overdue"
  | "nonpayment_notice_due"
  | "nonpayment_notice_sent"
  | "suspension_notice_due"
  | "suspension_notice_sent"
  | "paid";

/** The job's terms row as served. WHO columns (created_by/updated_by) deliberately never
 *  serve — raw account usernames stay Worker-side (the W9 posture), and the Payments card
 *  has no who-edited display. */
export interface PaymentTermsRow {
  job_id: string;
  billing_cadence: "monthly" | "semimonthly" | "milestone" | "other";
  billing_cadence_detail: string | null;
  net_days: number;
  nonpayment_notice_days: number;
  intent_to_suspend_days: number;
  notes: string | null;
  created_at: number;
  updated_at: number;
}

/** One APPEND-ONLY money-received event (active rows only; a correction is deactivate +
 *  re-add). recorded_by never serves (W9 posture). */
export interface PaymentReceiptRow {
  id: number;
  received_date: string;
  amount_cents: number;
  note: string | null;
  recorded_at: number;
}

/** One cycle as the Payments card renders it: the stored row + its active receipts + the
 *  DERIVED state and modifiers (payments_derive.ts, server `today` in Pacific).
 *  `due_date` is the STORED snapshot (submitted + net_days at submit time — 0073 header). */
export interface PaymentCycleView {
  id: number;
  cycle_uuid: string;
  job_id: string;
  seq: number;
  label: string;
  invoice_submitted_date: string | null;
  invoice_amount_cents: number | null;
  due_date: string | null;
  nonpayment_notice_date: string | null;
  suspend_notice_date: string | null;
  note: string | null;
  created_at: number;
  updated_at: number;
  receipts: PaymentReceiptRow[];
  state: PaymentState;
  /** amount − active receipts; null while the amount is unknown. */
  balance_cents: number | null;
  days_overdue: number | null;
  days_until_due: number | null;
  paid_late: boolean;
}

/** GET /api/fieldops/payments?job_id= — the whole Payments card in one read. */
export interface PaymentsResponse {
  terms: PaymentTermsRow | null;
  cycles: PaymentCycleView[];
  project_name: string | null;
}

/** GET /api/fieldops/payments/terms-default?job_id= — the same-client prefill (operator
 *  decision 6): the client's most recent OTHER job's terms, or null (no client on the job /
 *  no other job with terms — never an error). The names let the SPA label the button
 *  ("Copy from <client>'s latest job"). */
export interface PaymentTermsDefaultResponse {
  terms: Omit<PaymentTermsRow, "job_id"> | null;
  client_name: string | null;
  source_project_name: string | null;
}

/** POST /api/fieldops/payments/cycles — `due_date` echoes the stored snapshot;
 *  `note` is `no_terms_no_due_date` when a submitted cycle landed without terms
 *  (due date unknowable until terms exist and the submitted date is re-entered). */
export interface PaymentCycleCreateResponse {
  ok: true;
  id: number | null;
  cycle_uuid: string;
  due_date: string | null;
  note?: "no_terms_no_due_date";
}
