"""The system-map registry: every ITS runtime surface as one clickable node.

This is the CANONICAL topology the `/system` page renders — the machine-room
schematic. Nodes carry the JOIN KEYS that tie the map to every other surface:

- ``error_scripts``   — ITS_Errors "Script" values (`@its_error_log` names), so an
                        error row deep-links to its node and a node shows its
                        open CRITICALs.
- ``launchd_label``   — the `org.solutionsmith.its.*` label (daemons panel +
                        daemon-control verb + the troubleshooting tree's daemon
                        vocabulary, which is the label SUFFIX).
- ``heartbeat_stem``  — the `state/<stem>_heartbeat.txt` liveness file.
- ``config_gate``     — the ITS_Config key that turns the capability on/off
                        (workstream = the key's first dotted segment).
- ``watchdog_checks`` — the watchdog check letters whose SUBJECT is this node
                        (each letter is badged on exactly the surface it
                        probes; letters with no better home — the watchdog's
                        own infra checks — badge the watchdog node itself).
- ``runbook``         — the §43 successor-remediation doc served at /doc/….
- ``docs``            — extra (label, path) doc links for the detail rail.

EVERY node additionally carries an operator-grade brief in the companion
``node_briefs.py`` (kept separate so this registry stays scannable) — a new
node ships its brief in the same PR, enforced by ``tests/test_system_map.py``.

Layout is data too: ``lane`` (the left→right TRUST GRADIENT: field → cloud
queue → ‖HMAC wall‖ → generation → records/review → ‖SEND GATE‖ → send →
outside) and ``band`` (the workstream row). The two walls between lanes are
the page's thesis — Invariant 2 on the way in, Invariant 1 on the way out —
so wall positions are structural, not decoration.

Import-light on purpose: no Smartsheet / FastAPI imports here. Live-status
assembly (open CRITICALs, heartbeat ages, launchd state, gate values) lives in
``system_view.py``; ``tests/test_system_map.py`` holds the anti-drift parity
teeth (every launchd plist / tracked marker / gated script must have a node).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared import sheet_ids

# Lane ids in left→right trust order. WALL_AFTER names the lanes a wall follows.
LANES: tuple[tuple[str, str], ...] = (
    ("field", "Field"),
    ("cloud", "Cloud queue · send-free"),
    ("generation", "This Mac — generation (no send capability)"),
    ("records", "Records & human review"),
    ("send", "This Mac — send (no AI)"),
    ("outside", "Outside world"),
)
WALL_AFTER: dict[str, tuple[str, str]] = {
    # lane id -> (wall id, wall label)
    "cloud": ("wall-ingress", "Invariant 2 — untrusted ingress: bearer + HMAC re-verify · §34 screening"),
    "records": ("wall-send", "Invariant 1 — External Send Gate: human approval required"),
}

BANDS: tuple[tuple[str, str], ...] = (
    ("safety", "Safety reports"),
    ("progress", "Progress reports"),
    ("fieldops", "Field ops"),
    ("po", "Procurement — POs · vendor estimates · RFQs"),
    ("subcontracts", "Subcontracts"),
    ("machine", "Machine plane"),
)


@dataclass(frozen=True)
class MapNode:
    id: str
    label: str
    kind: str  # daemon | script | worker | sheet | store | external | ui
    lane: str
    band: str
    blurb: str
    error_scripts: tuple[str, ...] = ()
    launchd_label: str | None = None
    heartbeat_stem: str | None = None
    config_gate: str | None = None
    extra_gates: tuple[str, ...] = ()
    watchdog_checks: tuple[str, ...] = ()
    script_path: str | None = None
    runbook: str | None = None
    docs: tuple[tuple[str, str], ...] = ()  # extra (label, repo-relative path) doc links
    sheet_id: int | None = None
    send_half: str | None = None  # generation | send | None (capability-gating list)
    marker: str | None = None  # watchdog TRACKED_JOBS Check-C marker slug
    band_span: int = 1  # tall nodes span this many bands downward
    satellite: bool = False  # small side-chip (e.g. the Anthropic call)


@dataclass(frozen=True)
class MapEdge:
    src: str
    dst: str
    label: str
    kind: str  # push | pull | write | read | trigger | send | alert
    port: str | None = None  # label shown at a wall-crossing port glyph
    extra: dict[str, str] = field(default_factory=dict)


NODES: tuple[MapNode, ...] = (
    # ── field ────────────────────────────────────────────────────────────
    MapNode(
        id="spa", label="Safety Portal SPA", kind="ui", lane="field", band="safety",
        band_span=5,
        blurb="The crews' phones AND the office's screens. React app at "
              "safety.evergreenmirror.com — daily reports, hours, equipment, materials, PO "
              "and subcontract drafts, vendor-estimate uploads, manifest and schedule "
              "imports, and the weekly-production-report inputs all start here.",
        script_path="safety_portal/src", runbook="docs/runbooks/safety_portal_forms.md",
    ),
    # ── cloud queue ──────────────────────────────────────────────────────
    MapNode(
        id="worker", label="Cloudflare Worker + D1", kind="worker", lane="cloud", band="safety",
        band_span=5,
        blurb="The send-free edge station: validates each submission, signs it with an HMAC, "
              "and queues it in D1. It also derives the weekly production-report aggregate "
              "the Friday compile pulls. It can hold and serve data — it can never send "
              "anything.",
        script_path="safety_portal/worker/index.ts",
        runbook="docs/runbooks/safety_portal_admin_dashboard.md",
        watchdog_checks=("Q", "V"),
    ),
    # ── safety band ──────────────────────────────────────────────────────
    MapNode(
        id="portal_poll", label="portal_poll", kind="daemon", lane="generation", band="safety",
        blurb="Pulls queued submissions from the Worker every 60s over HTTPS, re-verifies "
              "every HMAC on the Mac, hands each one to intake, then posts the filed receipt.",
        error_scripts=("safety_reports.portal_poll",),
        launchd_label="org.solutionsmith.its.portal-poll", heartbeat_stem="portal_poll",
        config_gate="safety_reports.portal_poll.polling_enabled",
        watchdog_checks=("C", "Q", "R"), script_path="safety_reports/portal_poll.py",
        send_half="generation", marker="safety_portal_poll",
    ),
    MapNode(
        id="intake", label="intake", kind="script", lane="generation", band="safety",
        blurb="The 12-stage filing pipeline: §34-screens every photo, extracts fields with the "
              "system's only LLM call, renders the PDF, and files it to Box and the week sheet.",
        error_scripts=("safety_reports.intake", "safety_reports.week_folder", "safety_reports.week_sheet"),
        config_gate="safety_reports.intake.box_filing_enabled",
        script_path="safety_reports/intake.py",
        runbook="docs/runbooks/safety_photo_path.md", send_half="generation",
    ),
    MapNode(
        id="anthropic", label="Anthropic API", kind="external", lane="generation", band="safety",
        satellite=True,
        blurb="The one inference call in the system: intake's field extraction. External "
              "content goes in wrapped as untrusted data; a JSON schema comes back. No send "
              "capability exists in this half of the machine.",
        script_path="shared/anthropic_client.py",
    ),
    MapNode(
        id="weekly_generate", label="weekly_generate", kind="daemon", lane="generation", band="safety",
        blurb="Friday 14:00 deterministic compile: merges the week's filed PDFs into one packet "
              "per job, files it to Box, and stages a review row. No AI, no send.",
        error_scripts=("safety_reports.weekly_generate",),
        launchd_label="org.solutionsmith.its.weekly-generate",
        watchdog_checks=("C", "I"), script_path="safety_reports/weekly_generate.py",
        runbook="docs/runbooks/safety_weekly_generate.md", send_half="generation", marker="safety_weekly_generate",
    ),
    MapNode(
        id="compile_now_poll", label="compile_now_poll", kind="daemon", lane="generation", band="safety",
        blurb="Checks every 90s for a ticked 'Compile Now' box on a week sheet and runs the "
              "same compile on demand.",
        error_scripts=("safety_reports.compile_now_poll",),
        launchd_label="org.solutionsmith.its.compile-now-poll", heartbeat_stem="compile_now_poll",
        config_gate="safety_reports.compile_now_poll.polling_enabled",
        watchdog_checks=("C",), script_path="safety_reports/compile_now_poll.py",
        runbook="docs/runbooks/compile_now_poll.md", send_half="generation", marker="safety_compile_now_poll",
    ),
    MapNode(
        id="sheet_active_jobs", label="ITS_Active_Jobs", kind="sheet", lane="records", band="safety",
        blurb="The job roster: which jobs compile weekly, and who receives each job's safety "
              "packet (TO + CC resolved from here at send time).",
        sheet_id=sheet_ids.SHEET_ACTIVE_JOBS, runbook="docs/runbooks/safety_portal_job_management.md",
        docs=(("config-sheets guide", "docs/runbooks/safety_portal_config_sheets.md"),),
    ),
    MapNode(
        id="sheet_orphaned_reports", label="Orphaned Reports", kind="sheet", lane="records",
        band="safety", satellite=True,
        blurb="The lost-and-found: portal submissions naming an unknown or inactive job "
              "land here instead of a week sheet, so nothing is silently dropped.",
        sheet_id=sheet_ids.SHEET_ORPHANED_REPORTS,
        docs=(("data model reference", "docs/references/data_model_reference.md"),),
    ),
    MapNode(
        id="sheet_forms_catalog", label="ITS_Forms_Catalog", kind="sheet", lane="records",
        band="safety", satellite=True,
        blurb="The menu of safety forms the portal offers, and which jobs each form "
              "appears on.",
        sheet_id=sheet_ids.SHEET_FORMS_CATALOG, runbook="docs/runbooks/safety_portal_forms.md",
    ),
    MapNode(
        id="sheet_week_sheets", label="per-job week sheets", kind="sheet", lane="records", band="safety",
        blurb="One sheet per job per week: every filed submission lands as a row here, and the "
              "'Compile Now' checkbox lives on the rollup row.",
        runbook="docs/runbooks/week_sheet_config.md",
        docs=(("weekly compile runbook", "docs/runbooks/safety_weekly_generate.md"),),
    ),
    MapNode(
        id="sheet_wsr", label="WSR_human_review", kind="sheet", lane="records", band="safety",
        blurb="The safety review queue — a human reads the compiled packet and ticks the "
              "approval checkbox here. Nothing sends without that tick.",
        sheet_id=sheet_ids.SHEET_WSR_HUMAN_REVIEW, watchdog_checks=("T", "U"),
        runbook="docs/runbooks/safety_weekly_send.md",
    ),
    MapNode(
        id="box", label="Box", kind="store", lane="records", band="safety", band_span=5,
        blurb="The document system of record, organised as FIVE top-level roots since the "
              "2026-08-11/12 lane splits: per-workstream trees for Safety (+ materials "
              "manifests), Progress, Purchase Orders (+ RFQs and vendor quotes) and "
              "Subcontracts, plus the ITS Archive root closed jobs relocate into. Each root's "
              "folder id is an ITS_Config row; the 'Box roots' panel proves each id still "
              "resolves. Refresh token rotates on every exchange — watchdog Check P watches "
              "its freshness.",
        watchdog_checks=("P",), script_path="shared/box_client.py",
        runbook="docs/runbooks/box_token_freshness.md",
    ),
    MapNode(
        id="weekly_send", label="weekly send", kind="daemon", lane="send", band="safety",
        blurb="The send half for safety: a 15-min poll dispatches each APPROVED row through the "
              "shared send engine — recipients resolved at send time, approver verified against "
              "the workspace share list (F22). This process contains zero AI.",
        error_scripts=("safety_reports.weekly_send_poll", "safety_reports.weekly_send"),
        launchd_label="org.solutionsmith.its.weekly-send", heartbeat_stem="weekly_send",
        config_gate="safety_reports.weekly_send.polling_enabled",
        watchdog_checks=("C", "N"), script_path="safety_reports/weekly_send_poll.py",
        runbook="docs/runbooks/safety_weekly_send.md", send_half="send", marker="safety_weekly_send_poll",
    ),
    # ── progress band ────────────────────────────────────────────────────
    MapNode(
        id="progress_weekly_generate", label="progress_weekly_generate", kind="daemon",
        lane="generation", band="progress",
        blurb="Friday 14:30 deterministic compile against its own Active-Jobs roster: builds "
              "the client Weekly Production Report plus an internal packet of the dailies.",
        error_scripts=("progress_reports.progress_weekly_generate",),
        launchd_label="org.solutionsmith.its.progress-generate",
        watchdog_checks=("C", "I"), script_path="progress_reports/progress_weekly_generate.py",
        runbook="docs/runbooks/progress_weekly_generate.md", send_half="generation", marker="progress_weekly_generate",
    ),
    MapNode(
        id="sheet_active_jobs_progress", label="ITS_Active_Jobs_Progress", kind="sheet",
        lane="records", band="progress",
        blurb="The progress-side job roster, mirrored from the portal by fieldops_sync.",
        sheet_id=sheet_ids.SHEET_ACTIVE_JOBS_PROGRESS,
        runbook="docs/runbooks/progress_reporting_config_sheets.md",
    ),
    MapNode(
        id="sheet_wpr", label="WPR_human_review", kind="sheet", lane="records", band="progress",
        blurb="The progress review queue — same human-approval contract as the safety WSR sheet.",
        sheet_id=sheet_ids.SHEET_WPR_HUMAN_REVIEW, watchdog_checks=("T", "U"),
        runbook="docs/runbooks/progress_send.md",
    ),
    MapNode(
        id="progress_send", label="progress send", kind="daemon", lane="send", band="progress",
        blurb="Progress send dispatcher: binds the same shared, AI-free send engine as safety.",
        error_scripts=("progress_reports.progress_send_poll", "progress_reports.progress_send"),
        launchd_label="org.solutionsmith.its.progress-send", heartbeat_stem="progress_send",
        config_gate="progress_reports.progress_send.polling_enabled",
        watchdog_checks=("C", "N"), script_path="progress_reports/progress_send_poll.py",
        runbook="docs/runbooks/progress_send.md", send_half="send", marker="progress_send_poll",
    ),
    # ── field-ops band ───────────────────────────────────────────────────
    MapNode(
        id="fieldops_sync", label="fieldops_sync", kind="daemon", lane="generation", band="fieldops",
        blurb="The portal-as-writer mirror: pulls dirty portal-origin jobs and field capture "
              "from D1 and writes them UP into both Active-Jobs sheets, then drives the "
              "standing-tracker passes. Note: this band has no send lane at all.",
        # `job_archive` is joined HERE rather than given its own node: the archive has no plist,
        # no heartbeat and no identity of its own — it is a pass INSIDE this daemon, so its faults
        # are this daemon's faults. Without the join its six `archive_*` ITS_Errors codes rendered
        # as plain text with no `/system?focus=` link, because `NODE_BY_ERROR_SCRIPT` held no entry
        # for the undotted `job_archive` identity (`field_ops/job_archive.py` SCRIPT_NAME).
        error_scripts=("field_ops.fieldops_sync", "job_archive"),
        launchd_label="org.solutionsmith.its.fieldops-sync", heartbeat_stem="fieldops_sync",
        config_gate="field_ops.fieldops_sync.sync_enabled",
        extra_gates=(
            "field_ops.fieldops_sync.hours_enabled",
            "field_ops.fieldops_sync.equipment_enabled",
            "field_ops.fieldops_sync.materials_enabled",
            "field_ops.fieldops_sync.incidents_enabled",
            # The §51 receipts-ledger pass (#38). Same phantom-pass miss as archive below —
            # absent here until 2026-08-10, so the pass existed with no `/config?f=` deep link.
            "field_ops.fieldops_sync.receipts_enabled",
            # The Track 6 archive pass. Missing until 2026-08-10, which left it the ONLY pass with
            # no "pass gate" row and no `/config?f=` deep link on the node rail — a phantom pass an
            # operator could not find a switch for.
            "field_ops.fieldops_sync.archive_enabled",
        ),
        # X (#25) watches the archive pass this daemon drives: a job stopped at
        # partial/failed (terminal — never auto-retries) or a request never picked up.
        watchdog_checks=("C", "X"), script_path="field_ops/fieldops_sync.py",
        runbook="docs/runbooks/fieldops_sync.md", send_half="generation", marker="fieldops_sync",
    ),
    MapNode(
        id="trackers", label="standing trackers", kind="script", lane="generation", band="fieldops",
        blurb="Hours · equipment · materials · incidents · receipts: the §51 one-way-up mirrors "
              "fieldops_sync drives into their tracker sheets each pass.",
        error_scripts=(
            "progress_reports.hours_log", "progress_reports.equipment_status",
            "progress_reports.material_list", "progress_reports.material_incidents",
            "progress_reports.material_receipts",
        ),
        script_path="progress_reports/hours_log.py",
        runbook="docs/runbooks/hours_log_sync.md",
    ),
    MapNode(
        id="sheet_trackers", label="tracker sheets", kind="sheet", lane="records", band="fieldops",
        blurb="The standing tracker sheets: Hours Log, equipment status, material list, "
              "material incidents, material receipts — ITS-owned structured stores (§51).",
        runbook="docs/runbooks/hours_log_sync.md",
    ),
    # ── purchase-order band ──────────────────────────────────────────────
    MapNode(
        id="po_poll", label="po_poll", kind="daemon", lane="generation", band="po",
        blurb="The 90s PO daemon: pulls drafts, re-asserts every total in integer cents, "
              "renders the PO deterministically, §34-screens attachments, files to Box, and "
              "runs the vendor sync. No AI anywhere in this pipeline.",
        error_scripts=("po_materials.po_poll",),
        launchd_label="org.solutionsmith.its.po-poll", heartbeat_stem="po_poll",
        config_gate="po_materials.po_poll.polling_enabled",
        extra_gates=(
            "po_materials.po_poll.vendors_sync_enabled",
            "po_materials.po_poll.status_sync_enabled",
        ),
        watchdog_checks=("C",), script_path="po_materials/po_poll.py",
        runbook="docs/runbooks/po_poll.md", send_half="generation", marker="po_poll",
    ),
    MapNode(
        id="estimate_poll", label="estimate_poll", kind="daemon", lane="generation", band="po",
        blurb="The 120s vendor-estimate importer daemon (ADR-0004): pulls office-uploaded "
              "quote/estimate documents, re-verifies the est:v1 HMAC + digest, §34-screens, "
              "classifies doc-type (invoices/AP reports refused), files clean docs to Box, and "
              "posts page previews + extraction for human disposition. No AI — parsing runs in a "
              "killable sandbox; the local extraction ladder ships dark.",
        error_scripts=("po_materials.estimate_poll",),
        launchd_label="org.solutionsmith.its.estimate-poll", heartbeat_stem="estimate_poll",
        config_gate="po_materials.estimate_poll.polling_enabled",
        # The extraction ladder (E4-E6). Surfaced so the operator can SEE these are
        # off; they are Class-E READ-ONLY on the config page (dark + unvalidated —
        # no model qualified yet), deliberately not editable from the console.
        extra_gates=(
            "po_materials.estimate_extract.tier1_enabled",
            "po_materials.estimate_extract.tier2_enabled",
            "po_materials.estimate_extract.ocr_enabled",
        ),
        watchdog_checks=("C",), script_path="po_materials/estimate_poll.py",
        runbook="docs/runbooks/estimate_import_path.md", send_half="generation", marker="estimate_poll",
    ),
    MapNode(
        id="manifest_poll", label="manifest_poll", kind="daemon", lane="generation", band="fieldops",
        blurb="The 120s materials-manifest importer daemon (PR3b / ADR-0005): pulls "
              "office-uploaded BOMs and shipping logs, re-verifies the manifest:v1 HMAC + "
              "digest, §34-screens, extracts the cell grid in a KILLABLE sandbox child, and "
              "posts a REVIEWABLE GRID plus a proposed column map for the office to validate. "
              "No AI, and it never picks a quantity column for you — the human disposes.",
        error_scripts=("field_ops.manifest_poll",),
        launchd_label="org.solutionsmith.its.manifest-poll", heartbeat_stem="manifest_poll",
        config_gate="field_ops.manifest_poll.polling_enabled",
        watchdog_checks=("C",), script_path="field_ops/manifest_poll.py",
        runbook="docs/runbooks/material_manifest_import.md", send_half="generation",
        marker="manifest_poll",
    ),
    MapNode(
        id="schedule_poll", label="schedule_poll", kind="daemon", lane="generation", band="fieldops",
        blurb="The 120s job-schedule importer daemon (ADR-0006 PR-3): pulls office-uploaded "
              "project-schedule PDFs, re-verifies the schedule:v1 HMAC + digest, §34-screens, "
              "OCRs the vector-outline Gantt export inside a KILLABLE sandbox child (Apple "
              "Vision — local, no cloud AI), reconstructs the table, and posts a REVIEWABLE "
              "GRID plus source-page previews for the validate screen. The parser PROPOSES; "
              "the human commits.",
        error_scripts=("field_ops.schedule_poll",),
        launchd_label="org.solutionsmith.its.schedule-poll", heartbeat_stem="schedule_poll",
        config_gate="field_ops.schedule_poll.polling_enabled",
        watchdog_checks=("C",), script_path="field_ops/schedule_poll.py",
        runbook="docs/runbooks/schedule_import_path.md", send_half="generation",
        marker="schedule_poll",
    ),
    MapNode(
        id="rfq_poll", label="rfq_poll", kind="daemon", lane="generation", band="po",
        blurb="The 120s outbound-RFQ generation daemon (ADR-0004 Lane 2): pulls composed "
              "requests-for-quote from the Worker, re-verifies the rfq:v1 HMAC, and per vendor "
              "renders a PRICE-FREE RFQ PDF (ITS_Vendors snapshot), files it to Box, and stages "
              "an RFQ_Log + RFQ_Pending_Review row (tagged po_materials_rfq so po_send can never "
              "dispatch it). Deterministic, send-free, no AI; the vendor send is the PR-D lane.",
        error_scripts=("po_materials.rfq_poll",),
        launchd_label="org.solutionsmith.its.rfq-poll", heartbeat_stem="rfq_poll",
        config_gate="po_materials.rfq_poll.polling_enabled",
        watchdog_checks=("C",), script_path="po_materials/rfq_poll.py",
        runbook="docs/runbooks/rfq_generation_path.md", send_half="generation", marker="rfq_poll",
    ),
    MapNode(
        id="sheet_its_vendors", label="ITS_Vendors", kind="sheet", lane="records", band="po",
        blurb="The vendor roster (§51 down/up-sync with the portal's vendor picker).",
        sheet_id=sheet_ids.SHEET_ITS_VENDORS,
        runbook="docs/runbooks/po_poll.md",
        docs=(("purchase-orders guide", "docs/enablement/purchase_orders.md"),),
    ),
    MapNode(
        id="sheet_po_log", label="PO_Log", kind="sheet", lane="records", band="po",
        blurb="The PO ledger — one row per rendered PO, mirroring the D1 record.",
        sheet_id=sheet_ids.SHEET_PO_LOG,
        runbook="docs/runbooks/po_poll.md",
    ),
    MapNode(
        id="sheet_po_pending_review", label="PO_Pending_Review", kind="sheet", lane="records", band="po",
        blurb="The PO approval queue — human approval here releases a PO to its vendor.",
        sheet_id=sheet_ids.SHEET_PO_PENDING_REVIEW, watchdog_checks=("T", "U"),
        runbook="docs/runbooks/po_send.md",
    ),
    MapNode(
        id="sheet_estimate_log", label="Estimate_Log", kind="sheet", lane="records", band="po",
        blurb="The vendor-estimate ledger (ADR-0004 E2) — one row per uploaded quote/estimate "
              "document, carrying its screening disposition and the Box link to the filed original.",
        sheet_id=sheet_ids.SHEET_ESTIMATE_LOG, runbook="docs/runbooks/estimate_import_path.md",
    ),
    MapNode(
        id="sheet_rfq_log", label="RFQ_Log", kind="sheet", lane="records", band="po",
        blurb="The outbound-RFQ ledger (ADR-0004 R2) — one row per (RFQ, vendor), mirroring each "
              "price-free RFQ PDF filed to Box.",
        sheet_id=sheet_ids.SHEET_RFQ_LOG, runbook="docs/runbooks/rfq_generation_path.md",
    ),
    MapNode(
        id="sheet_rfq_pending_review", label="RFQ_Pending_Review", kind="sheet",
        lane="records", band="po",
        blurb="The RFQ approval queue — one row per (RFQ, vendor). A PO_Pending_Review schema twin "
              "tagged po_materials_rfq, so the PO and subcontract send daemons can never dispatch "
              "an RFQ row. Human approval here is what releases an RFQ to its vendor.",
        sheet_id=sheet_ids.SHEET_RFQ_PENDING_REVIEW, watchdog_checks=("T", "U"),
        runbook="docs/runbooks/rfq_send.md",
    ),
    MapNode(
        id="po_send", label="po send", kind="daemon", lane="send", band="po",
        blurb="PO send dispatcher (from procurement@): approved rows only, through the shared "
              "AI-free send engine.",
        error_scripts=("po_materials.po_send_poll", "po_materials.po_send"),
        launchd_label="org.solutionsmith.its.po-send", heartbeat_stem="po_send",
        config_gate="po_materials.po_send.polling_enabled",
        watchdog_checks=("C", "N"), script_path="po_materials/po_send_poll.py",
        runbook="docs/runbooks/po_send.md", send_half="send", marker="po_send_poll",
    ),
    MapNode(
        id="rfq_send", label="rfq send", kind="daemon", lane="send", band="po",
        blurb="RFQ send dispatcher (from procurement@, ADR-0004 R3): emails each APPROVED "
              "request-for-quote to its vendor with TWO attachments — the price-free RFQ PDF "
              "plus the fillable xlsx quote form — through the shared AI-free send engine. "
              "Tagged po_materials_rfq so po-send / subcontract-send can never dispatch it. "
              "Turning its gate ON is a FIXED External-Send-Gate decision; the live gate state is the badge above, not this text.",
        error_scripts=("po_materials.rfq_send_poll", "po_materials.rfq_send"),
        launchd_label="org.solutionsmith.its.rfq-send", heartbeat_stem="rfq_send",
        config_gate="po_materials.rfq_send.polling_enabled",
        watchdog_checks=("C", "N"), script_path="po_materials/rfq_send_poll.py",
        runbook="docs/runbooks/rfq_send.md", send_half="send", marker="rfq_send_poll",
    ),
    # ── subcontracts band ────────────────────────────────────────────────
    MapNode(
        id="subcontract_poll", label="subcontract_poll", kind="daemon", lane="generation", band="subcontracts",
        blurb="The 120s subcontract daemon: pulls drafts, runs the SOV-sums-to-price guard and "
              "the legal gate, fills the templates into an editable .docx/.xlsx package, and "
              "files the zip. Deterministic — no AI.",
        error_scripts=("subcontracts.subcontract_poll",),
        launchd_label="org.solutionsmith.its.subcontract-poll", heartbeat_stem="subcontract_poll",
        config_gate="subcontracts.subcontract_poll.polling_enabled",
        extra_gates=(
            "subcontracts.subcontract_poll.subcontractors_sync_enabled",
            "subcontracts.subcontract_poll.status_sync_enabled",
        ),
        watchdog_checks=("C",), script_path="subcontracts/subcontract_poll.py",
        runbook="docs/runbooks/subcontract_generation_path.md", send_half="generation", marker="subcontract_poll",
    ),
    MapNode(
        id="sheet_its_subcontractors", label="ITS_Subcontractors", kind="sheet", lane="records", band="subcontracts",
        blurb="The subcontractor roster — the send recipient is resolved from here by Sub Key.",
        sheet_id=sheet_ids.SHEET_ITS_SUBCONTRACTORS,
        runbook="docs/runbooks/subcontract_generation_path.md",
        docs=(("subcontracts guide", "docs/enablement/subcontracts.md"),),
    ),
    MapNode(
        id="sheet_subcontract_log", label="Subcontract_Log", kind="sheet", lane="records", band="subcontracts",
        blurb="The subcontract ledger, mirroring the D1 record.",
        sheet_id=sheet_ids.SHEET_SUBCONTRACT_LOG,
        runbook="docs/runbooks/subcontract_generation_path.md",
    ),
    MapNode(
        id="sheet_subcontract_pending_review", label="Subcontract_Pending_Review", kind="sheet",
        lane="records", band="subcontracts",
        blurb="The subcontract approval queue — human approval here releases the package.",
        sheet_id=sheet_ids.SHEET_SUBCONTRACT_PENDING_REVIEW, watchdog_checks=("T", "U"),
        runbook="docs/runbooks/subcontract_send.md",
    ),
    MapNode(
        id="subcontract_send", label="subcontract send", kind="daemon", lane="send", band="subcontracts",
        blurb="Subcontract send dispatcher: emails the approved Subcontract Package.zip to the "
              "subcontractor, from procurement@, through the shared AI-free engine.",
        error_scripts=("subcontracts.subcontract_send_poll", "subcontracts.subcontract_send"),
        launchd_label="org.solutionsmith.its.subcontract-send", heartbeat_stem="subcontract_send",
        config_gate="subcontracts.subcontract_send.polling_enabled",
        watchdog_checks=("C", "N"), script_path="subcontracts/subcontract_send_poll.py",
        runbook="docs/runbooks/subcontract_send.md", send_half="send", marker="subcontract_send_poll",
    ),
    # ── outside ──────────────────────────────────────────────────────────
    MapNode(
        id="graph", label="Microsoft Graph → recipients", kind="external", lane="outside", band="safety",
        band_span=5,
        blurb="The only door to a customer, vendor, or subcontractor inbox. Every edge into "
              "this node crossed the Send Gate: a human approved that exact packet first.",
        script_path="shared/graph_client.py",
    ),
    # ── machine plane ────────────────────────────────────────────────────
    MapNode(
        id="watchdog", label="watchdog", kind="daemon", lane="generation", band="machine",
        blurb="The daily 07:00 sweep: every registered check runs over the daemon markers, "
              "sheet backlogs, breaker, tokens, and send queues — then pings the external "
              "dead-man's switch. The eye chips on nodes above mark what it watches; its own "
              "infrastructure checks (blueprint guard, main-CI green, log rotation) badge here.",
        error_scripts=("scripts.watchdog",),
        launchd_label="org.solutionsmith.its.watchdog",
        watchdog_checks=("M", "S", "W"),
        script_path="scripts/watchdog.py", runbook="docs/runbooks/circuit_breaker.md",
        docs=(("log-dir rotation (Check W)", "docs/runbooks/log_dir_rotation.md"),),
    ),
    MapNode(
        id="picklist_sync", label="picklist_sync", kind="daemon", lane="generation", band="machine",
        blurb="Hourly cross-sheet PICKLIST option sync from the master DB sheets; removals are "
              "reference-checked so a live value is never deleted.",
        error_scripts=("scripts.run_picklist_sync",),
        launchd_label="org.solutionsmith.its.picklist-sync",
        watchdog_checks=("C",), script_path="scripts/run_picklist_sync.py", marker="safety_picklist_sync",
    ),
    MapNode(
        id="picklist_audit", label="picklist_audit", kind="daemon", lane="generation", band="machine",
        blurb="Sunday drift audit over the same picklist mappings (read-only).",
        error_scripts=("scripts.audit_picklist_drift",),
        launchd_label="org.solutionsmith.its.picklist-audit",
        watchdog_checks=("C",), script_path="scripts/audit_picklist_drift.py",
        runbook="docs/runbooks/picklist_drift_reconcile.md", marker="safety_picklist_audit",
    ),
    MapNode(
        id="publish_daemon", label="publish_daemon", kind="daemon", lane="generation", band="machine",
        blurb="The form-publish actuator: pulls approved form definitions from the Worker and "
              "runs the privileged commit → CI → deploy rail. Its ITS_Errors name is the "
              "undotted 'publish_daemon'.",
        error_scripts=("publish_daemon",),
        launchd_label="org.solutionsmith.its.publish-daemon", heartbeat_stem="publish_daemon",
        config_gate="safety_reports.publish_daemon.polling_enabled",
        script_path="safety_reports/publish_daemon.py",
        runbook="docs/runbooks/safety_portal_forms.md",
        watchdog_checks=("C",), marker="publish_daemon",
    ),
    MapNode(
        id="config_actuator", label="config_actuator", kind="daemon", lane="generation", band="machine",
        blurb="The §50 config actuator: pulls enqueued config edits from the Worker and commits "
              "them through PR → CI → deploy. Its ITS_Errors name is the undotted "
              "'config_actuator'.",
        error_scripts=("config_actuator",),
        launchd_label="org.solutionsmith.its.config-actuator", heartbeat_stem="config_actuator",
        config_gate="po_materials.config_actuator.polling_enabled",
        script_path="po_materials/config_actuator.py",
        runbook="docs/runbooks/config_actuator.md",
        watchdog_checks=("C",), marker="config_actuator",
    ),
    MapNode(
        id="dashboard", label="operator dashboard", kind="ui", lane="generation", band="machine",
        blurb="This console: read-only panels plus the PIN-gated ACT surface. It writes only "
              "ITS_Config and ITS_Errors/Review-Queue stamps — it never sends and never deploys.",
        launchd_label="org.solutionsmith.its.dashboard",
        script_path="operator_dashboard",
        runbook="docs/runbooks/operator_dashboard_config_editor.md",
    ),
    MapNode(
        id="error_log", label="error_log spine", kind="script", lane="generation", band="machine",
        blurb="Every script's @its_error_log wrapper lands here: one durable ITS_Errors row per "
              "occurrence, plus deduped CRITICAL pages to Resend and Sentry. A prolonged "
              "Smartsheet circuit-breaker outage pages through this spine too (Check J).",
        script_path="shared/error_log.py", watchdog_checks=("G", "J", "K"),
        runbook="docs/runbooks/circuit_breaker.md",
        docs=(("escalation matrix", "docs/references/escalation_matrix.md"),),
    ),
    MapNode(
        id="sheet_config", label="ITS_Config", kind="sheet", lane="records", band="machine",
        blurb="The runtime switchboard: every daemon reads its gates and tuning here each "
              "cycle. Edited only through the PIN-gated config editor. Check L's daily "
              "write probe proves the Smartsheet token can still write at all, and Check Y "
              "proves every load-bearing row still EXISTS — a missing row is an invisible "
              "off-switch, indistinguishable from 'false' to the daemon reading it.",
        sheet_id=sheet_ids.SHEET_CONFIG, runbook="docs/runbooks/operator_dashboard_config_editor.md",
        watchdog_checks=("L", "Y"),
        docs=(
            ("config dictionary (every setting)", "docs/references/its_config_dictionary.md"),
            ("token write probe (Check L)", "docs/runbooks/token_write_capability.md"),
        ),
    ),
    MapNode(
        id="sheet_errors", label="ITS_Errors", kind="sheet", lane="records", band="machine",
        blurb="The forensic error log. An open CRITICAL (blank Resolved At) is the 'am I on "
              "fire' surface; watchdog Check B counts them and Check O rotates the terminal rest.",
        sheet_id=sheet_ids.SHEET_ERRORS, watchdog_checks=("B", "O"),
        runbook="docs/runbooks/its_errors_triage.md",
        docs=(("data model reference", "docs/references/data_model_reference.md"),),
    ),
    MapNode(
        id="sheet_review_queue", label="ITS_Review_Queue", kind="sheet", lane="records", band="machine",
        blurb="Where ambiguity goes instead of silent failure: low-confidence extractions, "
              "refused attachments, fenced compile failures. Check A counts stale PENDING rows.",
        sheet_id=sheet_ids.SHEET_REVIEW_QUEUE, watchdog_checks=("A", "O"),
        runbook="docs/runbooks/review_queue_triage.md",
        docs=(("data model reference", "docs/references/data_model_reference.md"),),
    ),
    MapNode(
        id="sheet_daemon_health", label="ITS_Daemon_Health", kind="sheet", lane="records", band="machine",
        blurb="One row per polling daemon, updated in place each cycle — the operator's "
              "at-a-glance liveness sheet.",
        sheet_id=sheet_ids.SHEET_DAEMON_HEALTH, runbook="docs/runbooks/daemon_health_self_provision.md",
        docs=(("daemon reference (all daemons)", "docs/references/daemon_reference.md"),),
    ),
    MapNode(
        id="sheet_quarantine", label="ITS_Quarantine", kind="sheet", lane="records",
        band="machine", satellite=True,
        blurb="The holding pen for suspicious inbound email — logged, never processed. "
              "Load-bearing when Email Triage handles inbound mail.",
        sheet_id=sheet_ids.SHEET_QUARANTINE,
        docs=(("data model reference", "docs/references/data_model_reference.md"),),
    ),
    MapNode(
        id="sheet_project_routing", label="ITS_Project_Routing", kind="sheet", lane="records",
        band="machine", satellite=True,
        blurb="The project-to-Box-folder map: re-route document filing without touching code.",
        sheet_id=sheet_ids.SHEET_PROJECT_ROUTING,
        runbook="docs/runbooks/project_routing_onboarding.md",
    ),
    MapNode(
        id="sheet_time_off", label="ITS_Time_Off", kind="sheet", lane="records",
        band="machine", satellite=True,
        blurb="The PTO calendar: the reviewer chain skips anyone listed as out; Check D "
              "scans two weeks ahead for windows with nobody available.",
        sheet_id=sheet_ids.SHEET_TIME_OFF, watchdog_checks=("D",),
        runbook="docs/runbooks/time_off_reviewer_chain.md",
    ),
    MapNode(
        id="sheet_picklist_sync_config", label="Picklist_Sync_Config", kind="sheet",
        lane="records", band="machine", satellite=True,
        blurb="The wiring diagram for dropdown syncing: which master column feeds which "
              "sheet's picklist.",
        sheet_id=sheet_ids.SHEET_PICKLIST_SYNC_CONFIG,
        runbook="docs/runbooks/picklist_drift_reconcile.md",
        docs=(("picklist sync reference", "docs/references/picklist_sync.md"),),
    ),
    MapNode(
        id="registry_sheets", label="master DB sheets", kind="sheet", lane="records", band="machine",
        blurb="The master-database references behind dropdown syncing — Equipment Master is "
              "the live canonical source — plus the Documentation Index. The retired Vendor DB "
              "/ Subcontractor DB stubs are superseded by ITS_Vendors and ITS_Subcontractors.",
        docs=(("documentation index", "docs/references/documentation_index.md"),),
    ),
    MapNode(
        id="alerts", label="operator alerts", kind="external", lane="outside", band="machine",
        blurb="Resend (CRITICAL email) + Sentry (capture) + UptimeRobot (dead-man's switch). "
              "These page the OPERATOR — they are not customer sends, so they live outside the "
              "Send Gate on their own sanctioned path (§3.1).",
        script_path="shared/resend_client.py",
    ),
)

NODES_BY_ID: dict[str, MapNode] = {n.id: n for n in NODES}

# error_script -> node id (a node may own several script identities).
NODE_BY_ERROR_SCRIPT: dict[str, str] = {
    script: n.id for n in NODES for script in n.error_scripts
}

# launchd label -> node id (daemons panel + troubleshooting-tree daemon join —
# the tree's `what_happens.daemon` vocabulary is the label suffix).
NODE_BY_LAUNCHD_LABEL: dict[str, str] = {
    n.launchd_label: n.id for n in NODES if n.launchd_label
}

# state/<stem>_heartbeat.txt stem -> node id (heartbeats panel join).
NODE_BY_HEARTBEAT_STEM: dict[str, str] = {
    n.heartbeat_stem: n.id for n in NODES if n.heartbeat_stem
}

# watchdog TRACKED_JOBS Check-C marker slug -> node id (marker panel join).
NODE_BY_MARKER: dict[str, str] = {n.marker: n.id for n in NODES if n.marker}


EDGES: tuple[MapEdge, ...] = (
    MapEdge("spa", "worker", "submissions · field capture · PO/subcontract drafts · config edits", "push"),
    # safety
    MapEdge("worker", "portal_poll", "60s HTTPS pull — bearer + HMAC re-verified on the Mac", "pull",
            port="HMAC"),
    MapEdge("portal_poll", "intake", "one submission at a time", "trigger"),
    MapEdge("intake", "anthropic", "field extraction (untrusted-wrapped, schema-forced)", "read"),
    MapEdge("intake", "box", "file rendered PDF + screened photos", "write"),
    MapEdge("intake", "sheet_week_sheets", "append filed row", "write"),
    MapEdge("intake", "sheet_review_queue", "low-confidence / security-flag routing", "write"),
    MapEdge("intake", "sheet_orphaned_reports", "unknown / inactive Job ID → lost-and-found", "write"),
    MapEdge("sheet_active_jobs", "portal_poll", "job roster → portal dropdown sync", "read"),
    MapEdge("sheet_active_jobs", "weekly_generate", "compile roster", "read"),
    MapEdge("sheet_week_sheets", "weekly_generate", "gather the week's filed PDFs", "read"),
    MapEdge("sheet_week_sheets", "compile_now_poll", "'Compile Now' checkbox scan", "read"),
    MapEdge("compile_now_poll", "weekly_generate", "on-demand compile", "trigger"),
    MapEdge("weekly_generate", "box", "file weekly packet", "write"),
    MapEdge("weekly_generate", "sheet_wsr", "stage review row (PENDING)", "write"),
    MapEdge("sheet_wsr", "weekly_send", "APPROVED rows only — F22 approver verify", "read",
            port="human approval"),
    MapEdge("sheet_active_jobs", "weekly_send", "recipients resolved at send time", "read"),
    MapEdge("box", "weekly_send", "compiled packet attachment", "read"),
    MapEdge("weekly_send", "graph", "send_mail — the transmission edge", "send"),
    # progress
    MapEdge("sheet_active_jobs_progress", "progress_weekly_generate", "compile roster", "read"),
    # The Friday compile PULLS the Worker's send-free production-report aggregate
    # (GET /api/internal/production-report, migration 0067) — the same pull-edge shape as
    # every other Worker-draining Mac process on this map.
    MapEdge("worker", "progress_weekly_generate", "pull production-report aggregate", "pull",
            port="bearer"),
    MapEdge("progress_weekly_generate", "box", "file progress packet", "write"),
    MapEdge("progress_weekly_generate", "sheet_wpr", "stage review row (PENDING)", "write"),
    MapEdge("sheet_wpr", "progress_send", "APPROVED rows only — F22", "read", port="human approval"),
    MapEdge("progress_send", "graph", "send_mail", "send"),
    # field ops
    MapEdge("worker", "fieldops_sync", "pull dirty jobs · hours · equipment · materials · incidents", "pull",
            port="HMAC"),
    MapEdge("fieldops_sync", "sheet_active_jobs", "mirror portal jobs UP", "write"),
    MapEdge("fieldops_sync", "sheet_active_jobs_progress", "mirror portal jobs UP", "write"),
    MapEdge("fieldops_sync", "trackers", "drive the five passes", "trigger"),
    MapEdge("trackers", "sheet_trackers", "§51 one-way-up writes", "write"),
    # materials-manifest import (PR3b / ADR-0005). Until 2026-08-10 manifest_poll was the map's
    # only EDGE-LESS daemon — it rendered as an orphan box implying it talked to nothing, while
    # live it pulls the pool, files to Box, posts the grid back, and tickets refusals.
    MapEdge("worker", "manifest_poll", "pull uploaded manifests — manifest:v1 HMAC + digest re-verify",
            "pull", port="HMAC"),
    MapEdge("manifest_poll", "box", "file the ORIGINAL manifest bytes (job → Materials → Manifests)",
            "write"),
    MapEdge("manifest_poll", "worker", "post parsed grid + column-map proposal, result LAST", "push"),
    MapEdge("manifest_poll", "sheet_review_queue", "§34 / integrity / parse refusals", "write"),
    # job-schedule import (ADR-0006 PR-3) — the manifest lane's schedule sibling: pull
    # the pool, sandbox-OCR, file the original to Box, post the grid back, ticket refusals.
    MapEdge("worker", "schedule_poll", "pull uploaded schedules — schedule:v1 HMAC + digest re-verify",
            "pull", port="HMAC"),
    MapEdge("schedule_poll", "box", "file the ORIGINAL schedule PDF (job → Schedules)",
            "write"),
    MapEdge("schedule_poll", "worker", "post OCR'd grid + page previews, result LAST", "push"),
    MapEdge("schedule_poll", "sheet_review_queue", "§34 / integrity / unreadable refusals", "write"),
    # purchase orders
    MapEdge("worker", "po_poll", "pull drafts + attachments — HMAC + integer-cents re-assert", "pull",
            port="HMAC"),
    MapEdge("po_poll", "box", "file PO PDF + CLEAN attachments", "write"),
    MapEdge("worker", "estimate_poll", "pull uploaded estimates — est:v1 HMAC + digest re-verify", "pull",
            port="HMAC"),
    MapEdge("estimate_poll", "box", "file CLEAN screened quote docs", "write"),
    MapEdge("estimate_poll", "sheet_estimate_log", "ledger row per uploaded estimate", "write"),
    MapEdge("estimate_poll", "sheet_review_queue", "doc-type / §34 refusals + low-confidence disposition", "write"),
    MapEdge("worker", "rfq_poll", "pull composed RFQs — rfq:v1 HMAC re-verify", "pull",
            port="HMAC"),
    MapEdge("sheet_its_vendors", "rfq_poll", "vendor snapshot per RFQ copy — READ-ONLY", "read"),
    MapEdge("rfq_poll", "box", "file PRICE-FREE RFQ PDFs + xlsx quote forms (per vendor)", "write"),
    MapEdge("rfq_poll", "sheet_rfq_log", "ledger row per (rfq, vendor)", "write"),
    MapEdge("rfq_poll", "sheet_rfq_pending_review", "stage review row (PENDING)", "write"),
    MapEdge("rfq_poll", "sheet_review_queue", "unknown-vendor fences + bad-HMAC refusals", "write"),
    MapEdge("po_poll", "sheet_po_log", "ledger row + per-job mirror", "write"),
    MapEdge("po_poll", "sheet_po_pending_review", "stage review row (PENDING)", "write"),
    MapEdge("po_poll", "sheet_its_vendors", "§51 vendor down/up-sync", "write"),
    MapEdge("po_poll", "sheet_review_queue", "§34 SUSPICIOUS/MALICIOUS attachment refusals", "write"),
    MapEdge("sheet_po_pending_review", "po_send", "APPROVED rows only — F22", "read",
            port="human approval"),
    MapEdge("po_send", "graph", "send_mail (from procurement@)", "send"),
    MapEdge("sheet_rfq_pending_review", "rfq_send", "APPROVED rows only — F22 approver verify", "read",
            port="human approval"),
    MapEdge("sheet_its_vendors", "rfq_send", "recipient by Vendor Key", "read"),
    MapEdge("box", "rfq_send", "RFQ PDF + xlsx quote form attachments", "read"),
    MapEdge("rfq_send", "graph", "send_mail — RFQ PDF + xlsx form (from procurement@)", "send"),
    # subcontracts
    MapEdge("worker", "subcontract_poll", "pull drafts — sub:v1 HMAC", "pull", port="HMAC"),
    MapEdge("subcontract_poll", "box", "file Subcontract Package.zip", "write"),
    MapEdge("subcontract_poll", "sheet_subcontract_log", "ledger row + per-job mirror", "write"),
    MapEdge("subcontract_poll", "sheet_subcontract_pending_review", "stage review row (PENDING)", "write"),
    MapEdge("subcontract_poll", "sheet_its_subcontractors", "roster down/up-sync", "write"),
    MapEdge("sheet_its_subcontractors", "subcontract_send", "recipient by Sub Key", "read"),
    MapEdge("sheet_subcontract_pending_review", "subcontract_send", "APPROVED rows only — F22", "read",
            port="human approval"),
    MapEdge("subcontract_send", "graph", "send_mail (from procurement@)", "send"),
    # machine plane
    MapEdge("worker", "publish_daemon", "publish queue pull · commit + deploy push", "pull"),
    MapEdge("worker", "config_actuator", "config_requests pull · PR → CI → deploy", "pull"),
    MapEdge("sheet_picklist_sync_config", "picklist_sync", "mapping wiring (source → target)", "read"),
    MapEdge("registry_sheets", "picklist_sync", "canonical option source (Equipment Master)", "read"),
    MapEdge("watchdog", "sheet_time_off", "14-day reviewer-chain PTO scan (Check D)", "read"),
    MapEdge("dashboard", "sheet_config", "PIN-gated config edits", "write"),
    MapEdge("dashboard", "sheet_errors", "mark-resolved · clear terminal rows", "write"),
    MapEdge("error_log", "sheet_errors", "one row per occurrence", "write"),
    MapEdge("error_log", "alerts", "CRITICAL page — Resend + Sentry, deduped", "alert"),
    MapEdge("watchdog", "alerts", "daily heartbeat ping (dead-man's switch)", "alert"),
)


def edges_for(node_id: str) -> list[MapEdge]:
    """Every edge touching a node (for the detail rail)."""
    return [e for e in EDGES if e.src == node_id or e.dst == node_id]


def gate_workstream(gate_key: str) -> str:
    """The ITS_Config Workstream a dotted gate key is read under (its first segment)."""
    return gate_key.split(".", 1)[0]
