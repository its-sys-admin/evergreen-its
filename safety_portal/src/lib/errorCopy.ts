// R1 — the shared error-copy foundation: every worker error code the field-ops feature can return,
// mapped to plain-language copy, plus the ApiError the lib fetch helpers throw.
//
// CONTRACT: pages branch on `err.code` (the raw wire code, e.g. 'below_target'), NEVER on
// `err.message` — the message is HUMAN COPY (this map) and may be reworded freely. The raw code +
// HTTP status are preserved on the ApiError (and echoed to console.warn by raiseApiError) so
// diagnosis never loses the wire truth. Unknown codes get a humanized generic fallback — no more
// raw "Request failed (409)" banners.
//
// The map consolidates every hand-rolled page translation that predated it (below_target,
// not_placed, already_assigned, login_not_allowed) — add new copy HERE, not in pages.

export const ERROR_COPY: Record<string, string> = {
  // ── auth / session / gates ─────────────────────────────────────────────────────────────────────
  unauthenticated: "You're not signed in — log in and try again.",
  bad_session: "Your session has expired — log in again.",
  invalid_credentials: "Incorrect username or password.",
  forbidden: "You don't have permission to do that.",
  forbidden_role: "The daily field report is for crew-lead managers and the office \u2014 this account can't file it.",

  forbidden_job: "You're not assigned to that job.",

  // ── generic request shape ──────────────────────────────────────────────────────────────────────
  bad_request: "The request couldn't be read — please try again.",
  not_found: "That item no longer exists — refresh and try again.",
  internal_error: "Something went wrong on the server — please try again.",
  // 503, signing secret absent. Retrying cannot help — mirrors hmac_secret_missing below.
  server_misconfigured: "The server isn't fully set up — contact the operator.",
  invalid_date: "That date couldn't be read — pick it again.",

  // ── tasks ──────────────────────────────────────────────────────────────────────────────────────
  forbidden_task: "You can only update tasks assigned to you.",
  forbidden_target: "Managers can only assign tasks to subcontractor accounts.",
  // Shared by tasks (≤256 chars) and PO line items (≤512) — context-neutral copy on purpose.
  invalid_description: "Enter a description — it's required and may be too long.",
  invalid_status: "That isn't a valid task status.",
  invalid_personnel_id: "Pick a valid person.",
  not_active: "That job is closed — work can't be added to it.",

  // ── checklists (templates / items / instances) ─────────────────────────────────────────────────
  invalid_item_type: "That isn't a valid checklist item type.",
  invalid_label: "Enter an item label (up to 256 characters).",
  invalid_seq: "The item's position must be a non-negative whole number.",
  form_code_required: "Form-linked items need a form code.",
  unknown_form_code: "That form code doesn't match any form in the catalog — pick one from the form list.",
  options_required: "Choice items need at least one option — add one per line.",
  invalid_options: "Options must be 1–20 non-empty choices of up to 120 characters each.",
  options_not_allowed: "Only choice items take an option list.",
  invalid_target_count: "The target must be a whole number of at least 1.",
  invalid_title: "Enter a checklist title (up to 256 characters).",
  invalid_active: "Active must be on or off.",
  no_default_template: "The daily checklist template hasn't been set up yet.",
  invalid_template_id: "Pick a valid checklist.",
  invalid_assignee: "Pick a valid person to assign to.",
  invalid_due_date: "Enter the due date as YYYY-MM-DD.",
  template_not_found: "That checklist no longer exists — refresh the list.",
  assignee_not_found: "That person isn't on the active roster.",
  already_assigned: "That checklist is already assigned for this job and date.",
  empty_template: "This checklist has no items yet — add at least one before assigning it.",
  job_and_date_required:
    "This checklist contains form-linked items — pick both a job and a due date so filings can check them off.",
  invalid_note: "The note is too long (up to 2000 characters).",
  invalid_photo_ref: "The photo reference isn't valid.",
  invalid_value_num: "Enter a non-negative number.",
  below_target: "The value you recorded is below the target — the item stays open.",
  // Shared by the checklist below-target acknowledgement, flag-incident, and the PR2
  // not-delivered mark — worded to serve all three (each is "record a shortfall, say why").
  note_required: "Add a note explaining the shortfall.",
  auto_close_only: "This item completes automatically when the linked form is filed — it can't be checked by hand.",
  no_instance: "There's no daily checklist for you today.",
  not_complete: "Finish the remaining checklist items first.",
  photo_required: "This item needs a photo before it can be completed — add one first.",
  invalid_status_filter: "That isn't a valid status filter.",

  // ── checklist sign-off → progress report (POST .../progress-log) ───────────────────────────────
  // The branch codes of the "Sign & log to progress report" flow. Every one of these used to
  // fall through to "Something went wrong (…). Please try again." — advice that is actively
  // false for a size failure or a dark gate, which is the exact failure the R3-F2 `too_large`
  // copy above was written to kill.
  signature_required: "Sign before logging this inspection.",
  signature_too_large:
    "That signature is too detailed to save — tap Clear and sign again with fewer strokes.",
  already_submitted: "This inspection has already been logged — refresh to see the filed copy.",
  progress_logging_disabled:
    "Logging inspections to the progress report is turned off — ask the office to enable it.",

  // ── recurring checklists (dark-gated behind RECURRING_CHECKLISTS_ENABLED) ──────────────────────
  recurring_disabled: "Repeating checklists are turned off — ask the office to enable them.",
  invalid_recurrence: "The repeat settings couldn't be read — set the schedule again.",
  invalid_cadence: "Pick a valid repeat schedule.",
  invalid_anchor_date: "That start date isn't a real calendar date — pick it again.",
  job_required: "A repeating checklist needs a job — pick one first.",

  // ── time entries ───────────────────────────────────────────────────────────────────────────────
  invalid_hours: "Hours must be a number greater than 0 and at most 24.",
  invalid_uuid: "The entry couldn't be identified — please retry.",
  invalid_amends_uuid: "The entry being corrected couldn't be identified.",
  invalid_notes: "The notes are too long (up to 2000 characters).",
  invalid_submitted_as: "That isn't a valid account to submit as.",
  unknown_attributed_user: "That account doesn't exist or is disabled.",
  forbidden_personnel: "You can only log time for yourself or crew you added.",
  unknown_task: "That task doesn't belong to this job.",
  uuid_conflict: "This entry was already saved.",
  // G2.3 — non-destructive amend/void
  use_amend_route: "To correct an existing entry, use its Edit control.",
  forbidden_amend: "You can only correct entries you recorded.",
  not_head: "This entry was already corrected — refresh to see the newest version.",
  void_requires_reason: "Add a short reason to void this entry.",

  // ── jobs / personnel / crew (shared field-ops vocabulary) ──────────────────────────────────────
  invalid_job_id: "Pick a valid job.",
  unknown_job: "That job doesn't exist or is closed.",
  job_exists: "A job with that ID already exists.",
  unknown_personnel: "That person isn't on the active roster.",
  unknown_client: "That client doesn't exist.",
  unknown_account: "That account doesn't exist.",
  invalid_id: "That reference isn't valid — refresh and try again.",
  invalid_name: "Enter a name (up to 128 characters).",
  invalid_trade: "The trade is too long (up to 64 characters).",
  invalid_username: "Usernames are first.last (letters, numbers, dots).",
  invalid_password: "The password doesn't meet the requirements.",
  invalid_role: "That isn't a valid role.",
  invalid_project_name: "Enter a project name.",
  invalid_lifecycle: "That isn't a valid job state.",
  // 409, not 400 — the intent is valid and the route is wrong (the use_amend_route shape).
  // Reachable only from a stale bundle or a direct call: the current UI cannot select "Archived".
  use_archive_route:
    "Archiving a job is a separate action — it moves the job's folders, so it needs its own confirmation.",
  // ── job archive / un-archive (cap.job.archive) ──────────────────────────────
  confirm_required: "Type the project name to confirm.",
  confirm_mismatch: "That doesn't match the project name — check the spelling and try again.",
  already_archived: "This job is already archived. Use Un-archive to bring its folders back.",
  not_archived: "This job isn't archived, so there's nothing to bring back.",
  archive_in_flight:
    "An archive or un-archive is already running for this job — wait for it to finish.",
  invalid_address: "The address isn't valid.",
  invalid_email: "Enter a valid email address.",
  invalid_phone: "The phone number isn't valid.",
  invalid_cc: "CC lists take up to 5 valid email addresses.",
  invalid_contact_name: "The contact name isn't valid.",
  invalid_client_name: "Enter a client name.",
  invalid_client_id: "Pick a valid client.",
  invalid_client_field: "One of the client fields isn't valid.",
  client_id_and_new_client: "Pick an existing client OR enter a new one — not both.",
  // Shared by the job-ID counter and the PO vendor-key counter — context-neutral on purpose.
  counter_unavailable: "A new number couldn't be issued — please try again.",
  exists: "That already exists.",
  username_already_linked: "That account is already linked to another person.",
  not_placed: "You must be placed on a job first. Ask your crew lead or the office to place you.",
  login_not_allowed: "Crew added here are field-only (no login).",
  // G2.3 — scoped crew retire guards (real workers escalate to the office)
  crew_has_foreign_time: "Someone else has logged time for this person — ask the office to retire them.",
  crew_on_other_job: "This person is placed on a different job — ask the office to retire them.",

  // ── form submit / photo payload (worker /api/submit — R3-F2 actionable-copy fix) ───────────────
  // `too_large` is the Worker's 413 on JSON.stringify(values).length > PAYLOAD_MAX (1_800_000,
  // worker/index.ts:521,:604); photos are what get a values payload anywhere near that, so the copy
  // says what actually fixes it — a retry alone can NEVER succeed. `invalid_photo` is the 400 whose
  // machine reason rides `detail` (validatePhotoValues, worker/index.ts:550-567); the three
  // field-actionable detail reasons get their own copy (submitForm prefers detail over error).
  too_large:
    "This report is too large to send — photos take most of the space. Remove a photo or two (or retake them at lower quality) and submit again.",
  invalid_photo: "One of the photos couldn't be accepted — remove it, then re-attach or retake it.",
  photo_too_large: "One of the photos is too large — remove it and retake it at a lower quality.",
  too_many_photos: "Too many photos on this form — the limit is 8 in total. Remove some and try again.",
  too_many_photos_in_field: "Too many photos in one place — the limit is 4 per photo field.",
  // The remaining photo-validation details (Worker validatePhotoValues) — actionable copy for each.
  mixed_photo_array: "One of the photo fields contains something that isn't a photo — remove and re-attach.",
  photo_meta_too_long: "A photo's caption/metadata is too long — retake or re-attach it.",
  photo_not_base64: "A photo didn't upload correctly — remove it and attach it again.",
  photo_bad_magic: "That file isn't a JPEG or PNG photo — attach a photo taken with your camera.",

  // ── checklist item photos (G1 Slice 1 — record-only capture; POST /item-state/:id/photo) ───────
  photo_already_attached: "This item already has a photo on file or in screening — one photo per item.",
  photo_not_supported: "Photos can't be attached to form-linked items — the filed form itself is the evidence.",
  invalid_photo_shape: "The photo didn't come through correctly — attach it again.",
  photo_upload_too_large: "That photo is too large to upload — retake it at a lower quality.",

  // ── daily-report additional-photo pool (DR-photo-pool Slice 1) ─────────────────────────────────
  invalid_work_date: "Pick a valid report date first, then add the photo.",
  pool_cap_reached: "The daily photo limit for this job and date has been reached — remove one to add another.",
  pool_backlogged: "Photo screening is backed up right now — try adding this photo again in a few minutes.",
  invalid_origin: "This upload source is not recognized — reload the page and add the photo again.",
  photo_claimed: "That photo already belongs to a filed report — it can't be removed from the pool.",
  not_deletable: "That photo was refused by screening — remove it from the list instead.",
  invalid_additional_photos: "The additional-photos list didn't come through correctly — remove and re-add the photos.",
  unknown_photo_ref: "One of the additional photos is no longer available — remove it and re-add the photo.",
  photo_refused: "One of the additional photos was refused by screening — remove it before submitting.",
  photo_already_claimed: "One of the additional photos already belongs to another filed report — remove it and re-add the photo.",

  // ── equipment / materials / rollup (the remaining field-ops write vocabulary) ──────────────────
  invalid_kind: "That isn't a valid equipment kind.",
  invalid_identifier: "The identifier is too long.",
  invalid_status_note: "The status note is too long.",
  invalid_log_type: "That isn't a valid log type.",
  invalid_detail: "The detail text isn't valid.",
  invalid_window: "That isn't a valid reporting window.",
  invalid_source_files: "The source-file list isn't valid.",
  invalid_material_id: "Pick a material from the catalog.",
  already_actioned: "That material was already actioned — refresh to see the current status.",
  not_editable: "That material can no longer be edited — it's already been received or flagged.",

  // ── materials tracking: the delivery ledger + scheduled loads (PR2, migration 0059) ────────────
  // A DISTINCT code from the pre-existing `invalid_kind` (the daily-requirement vocabulary):
  // one shared code cannot carry two different "pick from this list" meanings.
  invalid_receipt_kind: "Pick Delivered, Partially delivered, or Not delivered.",
  invalid_event_date: "That isn't a valid delivery date.",
  invalid_shipment_id: "That load isn't on this material line — pick one of its own loads.",
  invalid_line_id: "That material line no longer exists — refresh and try again.",
  // ── daily-photo → material-line binding (0063) ───────────────────────────────────────────────
  // Deliberately DISTINCT codes from invalid_line_id above: that one means "the line you are
  // marking vanished", these mean "the photo could not be attached to that line". Related cause,
  // different thing the field user has to actually do about it.
  unknown_material_line:
    "That material line isn't on this job any more — refresh, then attach the photo again.",
  unknown_receipt_event:
    "That delivery record isn't on this material line — refresh and attach the photo again.",
  receipt_event_without_line:
    "A delivery photo has to say which material it's for. Pick the line, then attach it again.",
  invalid_line_uuid: "That material line reference isn't valid — refresh and try again.",
  invalid_receipt_event_id: "That delivery reference isn't valid — refresh and try again.",
  duplicate_event:
    "That delivery was already recorded — refresh to see it.",
  invalid_category: "The category is too long (64 characters max).",
  invalid_expected_ship_date: "That isn't a valid expected ship date.",
  invalid_bol_number: "The BOL / load number is too long (64 characters max).",
  invalid_carrier: "The carrier name is too long (64 characters max).",
  invalid_ship_date: "That isn't a valid ship date.",
  invalid_delivery_date: "That isn't a valid delivery date.",

  // ── job daily requirements ─────────────────────────────────────────────────────────────────────
  daily_tab_form_code:
    "That form is filed from the Daily Report tab — it can't be added as a daily requirement.",
  too_many_items: "This job already has the maximum number of daily requirements — remove one first.",

  // ── weekly production report (worker/fieldops_report.ts — 0067) ───────────────────────────────
  // `invalid_job_id` and `unknown_job` are shared with the codes above and need no entry here.
  invalid_week: "That week couldn't be read — pick the week again from the report page.",
  // Deliberately NOT "try again": a malformed photo selection is a client-side defect, and
  // retrying the same save reproduces it. Reload is the remedy that can actually help, and the
  // second sentence protects the office from assuming their cleared photo page silently refilled.
  invalid_photos:
    "Your photo selection couldn't be read — reload the page and choose the photos again; nothing was saved, so your previous selection is unchanged.",

  // ── purchase orders (worker/po.ts vocabulary — S6) ─────────────────────────────────────────────
  invalid_vendor_key: "That vendor reference isn't valid — refresh and pick again.",
  invalid_vendor_name: "Enter a vendor name (up to 256 characters).",
  invalid_supply_categories: "The supply-category selection isn't valid.",
  invalid_default_terms_profile: "That isn't a valid terms profile.",
  invalid_gtc_reference: "The GTC reference is too long.",
  invalid_region: "That isn't a valid region.",
  vendor_exists: "That vendor already exists.",
  unknown_vendor: "That vendor doesn't exist or is inactive — pick another.",
  // SHARED across emitters that accept DIFFERENT shapes: the jobs-SoR write gate takes the full
  // Evergreen identifier (`2026.384.1`, split on the way in — 0064), while the PO / RFQ /
  // subcontract / estimate routes take only the two-segment project number. So this copy states
  // ONLY the part that is true everywhere; the Job Tracker's own inline pre-flight message is
  // what tells the operator about the site segment, on the one form that accepts it.
  invalid_job_no: "The job number must look like 2026.384 (year.number).",
  invalid_address_city: "The job city doesn't fit — up to 256 characters.",
  invalid_address_state: "The job state must be the 2-letter abbreviation (e.g. IL).",
  invalid_address_zip: "The job ZIP doesn't fit — up to 16 characters.",
  invalid_site_phase: "The site/phase must be a whole number from 0 to 9999.",
  invalid_ship_to: "One of the ship-to fields is too long.",
  invalid_ship_to_state: "Enter the ship-to state as a 2-letter code (required for automatic tax).",
  invalid_delivery_contact: "One of the delivery-contact fields isn't valid.",
  invalid_sow_text: "The scope of work is too long (up to 8000 characters).",
  invalid_delivery_instructions: "The delivery instructions are too long (up to 4000 characters).",
  invalid_payment_terms_text: "The payment terms are too long (up to 2000 characters).",
  invalid_terms_profile: "That isn't a valid terms profile.",
  invalid_terms_version: "The terms version isn't valid.",
  invalid_tax_mode: "That isn't a valid tax mode.",
  invalid_tax_rate_bp: "The tax override must be between 0% and 100%.",
  invalid_shipping_cents: "The shipping amount isn't valid.",
  invalid_line_column_variant: "That isn't a valid line-item layout.",
  invalid_line_items: "The line items didn't come through correctly — check each row.",
  invalid_part_number: "A part number is too long (up to 64 characters).",
  invalid_unit: "A unit label is too long (up to 32 characters).",
  invalid_qty: "Quantities must be non-negative numbers.",
  invalid_unit_cost_cents: "A unit cost isn't valid.",
  invalid_per_watt_fields: "The watts / panels / pallets values must be non-negative whole numbers.",
  invalid_price_per_watt: "A price-per-watt isn't valid.",
  per_watt_fields_required: "Per-watt lines need both watts and a price per watt.",
  unit_cost_required: "Every line needs a unit cost (or watts + price per watt).",
  line_total_overflow: "A line total is too large.",
  subtotal_overflow: "The subtotal is too large.",
  total_overflow: "The total is too large.",
  unknown_tax_state: "There's no tax-table entry for that state — use the exempt or override tax mode.",
  invalid_totals: "The displayed totals didn't come through correctly — please try again.",
  no_line_items: "Add at least one line item before generating.",
  totals_mismatch: "The server's recomputed totals differ from what was displayed — review the refreshed numbers and generate again.",
  draft_changed: "This draft changed while you were working — the latest version was reloaded; review and generate again.",
  po_number_conflict: "A PO number collision occurred — generate again to get the next revision.",
  not_draft: "That PO is no longer a draft — refresh the list.",
  // Reworded workstream-neutral (S5 FLAG-2): these three wire codes are shared by the PO and the
  // subcontract supersede/cancel routes (worker reuses the codes), but the code→copy map is global —
  // so the copy must read correctly for both. A subcontract supersedes from sent OR executed.
  not_supersedable: "That record can't be superseded from its current status.",
  supersede_in_progress: "A replacement for that record is already in progress — it was opened instead.",
  not_cancelable: "That record can't be canceled from its current status.",
  invalid_approver: "The approver name/title is too long.",
  hmac_secret_missing: "The server isn't fully configured to sign POs — contact the operator.",

  // ── config editor (worker/config.ts — §50 send-free enqueue) ─────────────────────────────────────
  config_edit_in_progress: "A change to this setting is already being processed — wait for it to finish, then submit again.",
  invalid_workstream: "That configuration area isn't recognized — refresh and try again.",
  invalid_artifact: "That configuration item isn't recognized — refresh and try again.",
  invalid_op: "That change isn't valid for this configuration item.",
  invalid_target_version: "The version name must be lowercase letters, numbers, and underscores (e.g. standard_17_v2).",
  invalid_payload: "The change was empty or couldn't be read — check the fields and try again.",
  payload_too_large: "That change is too large to submit — shorten it and try again.",
  config_not_terminal: "That change is still being processed — you can clear it once it's live, archived, or failed.",
  invalid_profile_id: "The profile id must be lowercase letters, numbers, and underscores (e.g. vendor_acme).",
  profile_exists: "A terms profile with that id already exists — add a new version to it instead of creating a new profile.",
  invalid_profile_kind: "Pick a profile kind — Library (versioned text) or Attach (a reference line to a negotiated GTC).",

  // ── subcontracts (worker/subcontract.ts vocabulary — SC-S5) ────────────────────────────────────────
  // Codes a cap.subcontracts.manage user can trigger. Codes already present above are reused as-is
  // (invalid_job_id, invalid_job_no, invalid_site_phase, invalid_project_name, invalid_trade,
  // invalid_notes, invalid_address, invalid_contact_name, invalid_active, invalid_terms_profile,
  // invalid_terms_version, invalid_description, invalid_unit, invalid_qty, invalid_approver,
  // invalid_id, counter_unavailable, invalid_default_terms_profile, line_total_overflow,
  // subtotal_overflow, draft_changed, not_draft, hmac_secret_missing).
  // Subcontractor directory:
  invalid_sub_name: "Enter a subcontractor name (up to 256 characters).",
  invalid_contact_email: "Enter a valid contact email address.",
  invalid_contact_phone: "The contact phone number is too long.",
  invalid_state: "Enter the state as a 2-letter code (e.g. CA).",
  invalid_trades: "The trade selection isn't valid — up to 20 trades, each up to 64 characters.",
  invalid_msa_reference: "The master subcontract agreement reference is too long.",
  invalid_coi_reference: "The certificate-of-insurance reference is too long.",
  invalid_license_number: "The license number is too long (up to 64 characters).",
  subcontractor_exists: "That subcontractor already exists.",
  unknown_subcontractor: "That subcontractor doesn't exist or is inactive — pick another.",
  invalid_sub_key: "That subcontractor reference isn't valid — refresh and pick again.",
  // Subcontract draft / builder:
  invalid_job_name: "The job name is too long (up to 256 characters).",
  invalid_owner_entity: "The owner entity is too long (up to 256 characters).",
  invalid_prime_contractor: "The prime contractor is too long (up to 256 characters).",
  invalid_site_name: "The site name is too long (up to 256 characters).",
  invalid_site_address: "The site address is too long (up to 512 characters).",
  invalid_governing_law_state: "Pick a governing-law state — a subcontract can't be generated without one.",
  // Render-required fields refused at Generate (defense-in-depth behind the in-builder flags).
  missing_owner_entity: "Enter the Owner entity (the Evergreen contracting SPV) before generating.",
  missing_project_name: "Enter the project name before generating.",
  missing_trade: "Pick a trade before generating — the Exhibit A scope can't render without one.",
  missing_terms_profile: "Pick a terms profile before generating.",
  invalid_exhibit_a_template_id: "That Exhibit A template reference isn't valid.",
  invalid_exhibit_a_template_version: "That Exhibit A template version isn't valid.",
  invalid_exhibit_a_work_text: "The Exhibit A scope of work is too long (up to 100,000 characters).",
  invalid_scope_summary: "The scope summary is too long (up to 512 characters).",
  invalid_price_basis: "Pick a price basis — Fixed or Not-to-exceed.",
  invalid_contract_price: "Enter a valid contract price.",
  invalid_retainage_bp: "The retainage must be between 0% and 100%.",
  invalid_start_date: "The start date isn't valid.",
  invalid_completion_date: "The completion date isn't valid.",
  invalid_template_family: "That isn't a valid subcontract template.",
  invalid_sov_lines: "The schedule of values didn't come through correctly — check each line.",
  invalid_item_number: "An item number is too long (up to 64 characters).",
  unit_price_required: "Every schedule-of-values line needs a unit price.",
  invalid_unit_price_cents: "A unit price isn't valid.",
  no_sov_lines: "Add at least one schedule-of-values line before generating.",
  sov_mismatch: "The schedule of values doesn't add up to the contract price — review the refreshed numbers and generate again.",
  sc_number_conflict: "A subcontract number collision occurred — generate again to get the next revision.",

  // ── materials-manifest import (PR3b — worker/fieldops_manifests.ts) ────────────────────────────
  // Upload-path codes reach the office directly. The internal-tier codes below them are posted by
  // the Mac daemon and should never surface in a browser, but they get copy anyway: a code with no
  // entry renders as "Something went wrong (…). Please try again.", and for a rejected upload that
  // advice is actively false.
  mime_not_allowed: "Only PDF and Excel (.xlsx) manifests can be imported — export the document and upload it again.",
  extension_mime_mismatch: "The file's extension doesn't match its type — re-export it and upload again.",
  magic_mime_mismatch: "That file isn't the type its name claims — re-export it as a PDF or .xlsx and upload again.",
  manifest_too_large: "That manifest is too large to import — split it or export a smaller range.",
  duplicate_manifest: "This job already has that exact manifest — open the existing one, or discard it first to re-import.",
  // Shared by the manifest AND schedule (ADR-0006) discard routes — worded for both.
  not_discardable: "That document can't be discarded now — its contents have already been committed.",
  preview_too_large: "A source-page preview was too large to store — the grid is still usable without it.",
  invalid_data: "The upload didn't come through intact — choose the file again.",
  invalid_page: "That preview page number isn't valid.",
  invalid_rows: "The parsed rows didn't come through correctly — the manifest needs re-importing.",
  invalid_row_count: "The parsed row count isn't valid — the manifest needs re-importing.",
  invalid_profile: "The detected document type isn't valid — the manifest needs re-importing.",
  invalid_column_map: "The proposed column map didn't come through correctly — the manifest needs re-importing.",
  invalid_header_meta: "The document's header details didn't come through correctly — the manifest needs re-importing.",
  invalid_parse_notes: "The parser's notes didn't come through correctly — the manifest needs re-importing.",
  invalid_box_file_id: "The filed-document reference isn't valid — the manifest needs re-importing.",
  box_file_id_on_refusal: "A refused manifest can't carry a filed-document reference — this is a bug; tell Seth.",

  // Import validate + commit (the routes that actually author a job's material list).
  invalid_lines: "The lines to import didn't come through correctly — reload the validate screen and try again.",
  invalid_line: "One of the lines is malformed — reload the validate screen and try again.",
  invalid_source_row_index: "A line lost track of which document row it came from — reload the validate screen.",
  invalid_mode: "Choose whether to merge these lines into the existing list or add them as new.",
  not_committable: "This manifest can't be committed — it was refused or discarded, so re-import the document.",
  line_cap_exceeded: "This job can hold 500 material lines and the import would exceed that — split it across jobs, or trim the list before committing.",
  duplicate_line: "A line collided with one that already exists — reload the validate screen to see the current list.",
  // 2026-08-11: real merge + the shipments import.
  invalid_import_as: "Choose whether these rows are material lines (a BOM) or scheduled loads (a shipping log).",
  // SHARED by the manifest merge (part → line picks) and the schedule reconcile
  // (links / removals / percent picks) — worded to serve both: each is "your review
  // choices no longer line up with what the server derived; re-run the preview".
  invalid_resolutions: "Your review choices didn't come through, or no longer match the current list — re-run the preview and decide them again.",
  // SHARED by the manifest commit (a part number matching >1 line) and the schedule
  // reconcile commit (a task name matching >1 living task, or two rows claiming one
  // task) — both mean "more than one existing entry could be meant; the import stays
  // blocked until that's resolved". The manifest preview offers a picker; the schedule
  // reconcile deliberately has NO picker (no fuzzy matching, ADR-0006 decision 9) —
  // its review screen names the duplicates so the human can fix them at the source.
  ambiguous_unresolved: "Some rows match more than one existing entry — open the preview, see which ones, and resolve the duplicates before importing.",
  invalid_bol: "A BOL / load number is too long — shorten it to 120 characters or less.",
  // Resolve-problem (the expected-materials incident flag's explicit counterpart).
  not_flagged: "This line has no problem flag to resolve — someone may have already cleared it.",

  // ── job-schedule import (ADR-0006 — worker/fieldops_schedules.ts) ──────────────────────────────
  // The lane is PDF-only, so its MIME codes are its own: the shared manifest copy above
  // recommends re-exporting as .xlsx, which would be actively-false advice here.
  schedule_mime_not_allowed:
    "Only a PDF project-schedule export can be imported — export the schedule as a PDF and upload it again.",
  schedule_magic_mime_mismatch:
    "That file isn't really a PDF — re-export the schedule as a PDF and upload it again.",
  schedule_too_large: "That schedule PDF is too large to import — re-export it and try again.",
  duplicate_schedule:
    "This job already has that exact schedule file — open the existing upload, or discard it first to re-import.",

  // ── schedule validate + commit (PR-4 — the living task list) ───────────────────────────────
  // Task-row bounds — the SAME reader gates the manual add/edit routes and the commit's
  // imported rows (worker/fieldops_schedule_tasks.ts readScheduleTaskFields), so one copy
  // per code serves both paths.
  invalid_task_rows: "The task rows didn't come through correctly — reload the validate screen and try again.",
  invalid_task_kind: "A row must be a task row or a section row — reload the validate screen.",
  invalid_task_name: "Every task needs a name (up to 300 characters).",
  invalid_task_section: "A section name is too long (up to 120 characters).",
  invalid_task_date: "Enter task dates as YYYY-MM-DD.",
  invalid_task_percent: "Percent done must be a whole number from 0 to 100.",
  invalid_task_duration: "A duration must be a whole number of days, up to 5000.",
  invalid_task_predecessors: "The predecessors text is too long (up to 200 characters).",
  invalid_task_flag: "The milestone / delivery markers must be on or off.",
  // ── schedule revision reconcile (PR-6 — the three-way diff's own refusals) ─────────
  // (`ambiguous_unresolved` and `invalid_resolutions` above are shared with the
  // manifest merge; `revision_reconcile_not_available` was the PR-4 refusal this flow
  // replaced — the Worker no longer sends it.)
  blocking_removals_unresolved:
    "Some tasks the new schedule drops have field progress, a delivered mark, or are contract milestones — decide keep-or-remove for each of them before importing.",
  invalid_link_target:
    "A task link no longer lines up — the task may have changed since you reviewed. Re-run the preview and link again.",
  schedule_not_committable:
    "This schedule can't be committed from its current state — refresh to see where it stands.",
  another_commit_in_progress:
    "Another schedule upload for this job is mid-import — finish or discard that one first.",

  // ── schedule field mark-off (PR-5 — cap.schedule.mark, worker/fieldops_schedule_tasks.ts) ──
  // `invalid_percent`, not `invalid_task_percent`: that code already means "the add/edit/commit
  // task CONTENT fields didn't validate" — the mark body's percent is its own wire field, and
  // one code cannot carry two meanings (the invalid_receipt_kind precedent above).
  invalid_percent: "Progress must be a whole number from 0 to 100.",
  milestone_binary: "A milestone is either done or not — mark it 0% or 100%.",
  not_a_milestone: "That task isn't a milestone — set its percent instead.",
  not_a_delivery: "That task isn't a delivery task, so it has no delivered mark.",
  invalid_delivered_date: "Enter the delivered date as YYYY-MM-DD.",

  // ── job payments (ADR-0006 PR-7 — worker/fieldops_payments.ts) ─────────────────────────────
  // `invalid_cycle_label`, not `invalid_label` — and `invalid_billing_cadence`, not
  // `invalid_cadence`: both bare codes already mean something else (a checklist item
  // label up to 256 characters; a checklist repeat schedule), and one code cannot
  // carry two meanings (the invalid_percent precedent above).
  invalid_billing_cadence: "Pick a billing cadence — monthly, semimonthly, milestone, or other.",
  invalid_cadence_detail: "The cadence detail is too long (up to 300 characters).",
  invalid_net_days: "Net days must be a whole number from 0 to 365.",
  invalid_notice_days: "Notice days must be a whole number from 1 to 365.",
  invalid_payment_note: "That note is too long (up to 2000 characters).",
  invalid_cycle_label: "Give the payment cycle a label (up to 120 characters).",
  invalid_submitted_date: "Enter the invoice-submitted date as YYYY-MM-DD.",
  invalid_amount_cents: "Enter the amount as a positive number of dollars and cents.",
  invalid_notice_kind: "A notice is either the nonpayment notice or the intent to suspend.",
  invalid_notice_date: "Enter the notice date as YYYY-MM-DD.",
  invalid_received_date: "Enter the received date as YYYY-MM-DD.",
  suspend_requires_nonpayment:
    "Record the Notice of Nonpayment first — the intent to suspend can only follow a recorded nonpayment notice.",
};

/** Humanize an unknown wire code: 'some_new_code' → 'some new code'. */
function humanizeCode(code: string): string {
  return code.replace(/_/g, " ");
}

/**
 * Translate a worker error code (+ HTTP status) into plain-language copy. Every mapped code gets its
 * copy; an unmapped code gets a humanized generic; no code at all falls back on the status class.
 */
export function errorText(code: string | null | undefined, status?: number): string {
  if (code && ERROR_COPY[code]) return ERROR_COPY[code];
  if (code) return `Something went wrong (${humanizeCode(code)}). Please try again.`;
  if (status === 401) return ERROR_COPY.bad_session;
  if (status === 403) return ERROR_COPY.forbidden;
  if (status === 404) return ERROR_COPY.not_found;
  if (status !== undefined && status >= 500) return ERROR_COPY.internal_error;
  return "Something went wrong. Please try again.";
}

/**
 * The error the lib fetch helpers throw on a non-OK response. `message` is human copy (errorText);
 * `code` is the raw wire code for page-level branching; `status` is the HTTP status.
 */
export class ApiError extends Error {
  readonly code: string | null;
  readonly status: number;

  constructor(code: string | null, status: number) {
    super(errorText(code, status));
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

/**
 * Shared non-OK handler for the lib fetch helpers: extract the worker's `{ error }` code (tolerant
 * of an empty/non-JSON body), preserve the raw code in console.warn, and throw the ApiError carrying
 * human copy + code + status.
 */
export async function raiseApiError(res: Response): Promise<never> {
  let code: string | null = null;
  try {
    const body = (await res.json()) as { error?: unknown };
    if (typeof body?.error === "string") code = body.error;
  } catch {
    /* non-JSON body → status-based copy */
  }
  console.warn(`API error ${res.status}${code ? `: ${code}` : ""} (${res.url})`);
  throw new ApiError(code, res.status);
}
