"""Operator-facing briefs for every node on the system map.

One entry per `system_map` node id — sheets, daemons, scripts, the portal and
its Worker, Box, the send door, and the alert path. Each brief answers, in
plain language a NON-TECHNICAL Successor-Operator can act on: what the thing
is, what feeds and reads it, and what the operator does with it day-to-day.
Rendered in the `/system` detail rail beneath the node's blurb.

Kept as a companion module (not inline `MapNode` fields) so `system_map.py`
stays a scannable topology registry. Same import-light contract: pure data.
Entry order mirrors `system_map.NODES`.

Shape convention: ¶1 is WHAT THIS IS (what it does, what feeds it, who reads
what it writes), ¶2 opens "Day-to-day" and is WHAT YOU DO — including the
first thing to check when it misbehaves. The three borrowed-slot approval-desk
briefs are the deliberate exception: they answer "what you do" in ¶1 and spend
¶2 on the borrowed-column caveat, so the tests assert structure, not phrasing.
A brief must ADD to the node's `blurb`, which renders directly above it —
never restate it.

Writing discipline (HOUSE_REFLEXES §5): a brief states what a node MEANS and
how it behaves — never a live value, count, or gate state. ITS_Config is the
single source of live state; the rail's badges show it. Pair that with the
§44 boundary: pausing a capability is ordinary operator work, while enabling
a send lane, touching secrets, changing doctrine, or changing code escalates.

Parity: `tests/test_system_map.py` asserts every map node has a brief here and
every brief key is a real node — so a new node ships its brief in the same PR.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeBrief:
    what: str          # 2 plain-English paragraphs (split on \n\n by the template)
    key_label: str = ""  # "Key columns" for sheets · "Key signals" daemons · "Key facts"
    key_line: str = ""   # the one-line at-a-glance row; both empty when not useful


NODE_BRIEFS: dict[str, NodeBrief] = {
    # ── field · the crews' surface ──────────────────────────────────────────
    "spa": NodeBrief(
        what=(
            "Everything typed here posts to the cloud queue and is mastered there, with the "
            "Smartsheet mirrors following. The job, vendor, and subcontractor lists are the "
            "reverse: Smartsheet holds the record, the portal a synced copy. A missing card is a "
            "role setting on the Accounts screen, not a broken app."
            "\n\n"
            "Day-to-day there is nothing to run or tune. One person bounced to login usually "
            "means an idle timeout or a disabled account, and re-enabling takes a stored "
            "credential, not a portal button. Everyone locked out points at the cloud queue "
            "behind it — that recovery, like editing any page or form, escalates to the "
            "developer."
        ),
        key_label="Key facts",
        key_line=(
            "Field accounts stay signed in for 90 days, admins 30 idle minutes · a retried Submit "
            "can never duplicate a filing."
        ),
    ),
    # ── cloud queue · send-free ─────────────────────────────────────────────
    "worker": NodeBrief(
        what=(
            "The portal's back end: it serves the app crews load, holds their login accounts and "
            "audit trail, and is where the field-ops records — hours, tasks, inspections — live "
            "first before mirroring up to Smartsheet as standing trackers. A daily pass trims "
            "aged rows to stay under the 10 GB ceiling, but only ones already filed to Box."
            "\n\n"
            "Day-to-day there is nothing to run, start, stop, or gate — Cloudflare keeps it "
            "alive. If submissions stop reaching the Mac, check the pulling daemon's heartbeat: "
            "the watchdog separates \"can't reach the portal\" from \"reaching it but filing "
            "nothing.\" Deploys, migrations, token rotations, and admin lockouts are developer "
            "work — escalate."
        ),
        key_label="Key facts",
        key_line=(
            "The portal app crews load · the D1 queue the Mac's pulling daemons drain · the daily "
            "prune heartbeat (Check V)"
        ),
    ),
    # ── safety band ─────────────────────────────────────────────────────────
    "portal_poll": NodeBrief(
        what=(
            "Each cycle also pushes the ITS_Active_Jobs roster to the portal's dropdown, so a job "
            "added there reaches crews' phones within a cycle — and an empty roster is refused, "
            "never blanking the dropdown. A row whose signature fails is never filed: it lands "
            "security-flagged in the Review Queue."
            "\n\n"
            "Day-to-day it needs no attention. Its ITS_Config gate row pauses and resumes it — "
            "submissions wait, though the watchdog then flags it stale. If submissions stop "
            "appearing, check that gate, then heartbeat age and error rows. A credentials error "
            "stops filing: confirm the portal base-URL row is set; if it is, escalate to the "
            "developer — its tokens are secrets you never touch."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy cycle refreshes the heartbeat and watchdog marker · watchdog C = daemon "
            "stale, Q = can't fetch, R = fetching but draining nothing"
        ),
    ),
    "intake": NodeBrief(
        what=(
            "Intake isn't a daemon — it's the engine portal_poll calls once per submission, with "
            "no schedule or heartbeat. Each one is filed, already filed on a harmless re-pull, or "
            "refused — a job, form, payload, or photo it won't accept — into a Review-Queue or "
            "Orphaned-Reports row."
            "\n\n"
            "Day-to-day you don't run intake, you work what it refuses: clear the rows it opens, "
            "then read ITS_Errors. A photo refusal names a portal account to disable on the admin "
            "screen, but re-enabling it later takes a stored credential, not a button there — "
            "escalate that, an unexplained malicious photo, refusals repeating from one account, "
            "or any change to what intake accepts."
        ),
        key_label="Key facts",
        key_line=(
            "Refusals are permanent and stop retrying, while transient failures retry on their "
            "own; errors file under safety_reports.intake, week_folder, and week_sheet."
        ),
    ),
    "anthropic": NodeBrief(
        what=(
            "The one place ITS sends anything to a language model, and it serves the legacy email "
            "lane. An emailed report goes out tagged as data the model must never obey, and the "
            "answer must return a fixed field set with a confidence score. A malformed answer, "
            "low confidence, or an anomaly flag sends it to the Review Queue instead of filing. "
            "Crews' portal submissions are deterministic and never reach it."
            "\n\n"
            "Day-to-day there is nothing to run: no daemon, no heartbeat, no gate. When an "
            "extraction looks wrong, read the Review Queue, then ITS_Errors. Raising the "
            "threshold is ordinary work; the key and prompt are developer territory."
        ),
        key_label="Key facts",
        key_line=(
            "Review Queue reasons low-confidence-extraction / structured-output-edge / "
            "security-trigger · ITS_Errors classifier_malformed · ITS_Config classification_model "
            "+ confidence_threshold"
        ),
    ),
    "weekly_generate": NodeBrief(
        what=(
            "Each run gathers every Active job's Saturday-to-Friday week sheet. Recompiles are "
            "append-only, adding a version-numbered file, and an empty week still gets its rollup "
            "and approval rows. One job's failure is fenced to the Review Queue while the rest "
            "still compile. It writes no heartbeat, and only the system-wide pause stops it."
            "\n\n"
            "Day-to-day this is watch-only; approvals happen on the review sheet. A missed Friday "
            "re-fires on the next watchdog pass. If a job's row never appears, check the Review "
            "Queue and ITS_Errors for a compile timeout or memory-ceiling error, then re-run it. "
            "Escalate to the developer if it repeats or needs a code change."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: a fresh run marker each Friday and one review row per Active job. First "
            "check the review sheet, then Review Queue and ITS_Errors for this script."
        ),
    ),
    "compile_now_poll": NodeBrief(
        what=(
            "One daemon serves both safety and progress week sheets; the only trigger is a "
            "Compile Now box on the current week's Rollup row. Submission-row boxes only narrow "
            "the packet; clear ones compile the whole week. Success appends a Rollup snapshot and "
            "stages a PENDING approval row, never a send; a failure leaves the box set, so the "
            "next cycle retries."
            "\n\n"
            "Day-to-day: tick Compile Now for a packet before Friday; otherwise watch-only. If "
            "nothing appears within minutes, confirm the box is on the current week's Rollup row, "
            "then the workstream gate and system.state. A frozen heartbeat or a lock held for "
            "many minutes is developer work: freeing one is a host operation."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: heartbeat advancing each cycle, status OK · Stuck: heartbeat age first, "
            "then the workstream gate · 90s default cadence, tunable at reinstall"
        ),
    ),
    "sheet_active_jobs": NodeBrief(
        what=(
            "The master list of active jobs the Safety Portal shows field crews, including each "
            "job's address (auto-fills Work Location on forms) and who receives its weekly safety "
            "report. The portal-sync daemon reads it into the portal dropdown; the weekly safety "
            "send resolves TO + CC recipients from it at the moment of sending."
            "\n\n"
            "Day-to-day you keep it current: add new jobs, set Active/Inactive, and keep the "
            "safety-report contact and CC emails correct — a wrong email here changes who gets "
            "the weekly report; a blank one HOLDS the send rather than mis-sending."
        ),
        key_label="Key columns",
        key_line=(
            "Project Name · Job ID (stable key) · Address · Active · safety-reports contact + CC "
            "1–5 · Portal Job Key (system bridge — don't edit)"
        ),
    ),
    "sheet_orphaned_reports": NodeBrief(
        what=(
            "The lost-and-found for portal submissions naming a job ITS doesn't recognize, or one "
            "that's no longer active — they can't be filed to a job's week sheet, so they land "
            "here instead of drowning the Review Queue."
            "\n\n"
            "Day-to-day you check Pending rows, re-home the report to the right job (or discard "
            "it), and set Status accordingly."
        ),
        key_label="Key columns",
        key_line=(
            "Submission UUID · Job ID (the unresolved key) · Reason (job_not_found / "
            "job_inactive) · Box Link (the rendered PDF) · Status"
        ),
    ),
    "sheet_forms_catalog": NodeBrief(
        what=(
            "The menu of safety forms the portal offers, and which jobs each form appears on."
            "\n\n"
            "Day-to-day you rarely touch it — set a form Inactive to pull it from the portal, or "
            "set Available For Jobs to limit a form to specific jobs (blank = all jobs). Never "
            "edit Form Code: it is the stable key the code matches on."
        ),
        key_label="Key columns",
        key_line=(
            "Form Name · Form Code (stable key — never edit) · Active · Display Order · Available "
            "For Jobs"
        ),
    ),
    "sheet_week_sheets": NodeBrief(
        what=(
            "A dynamic family, not one fixed sheet: ITS find-or-creates one sheet per job per "
            "week under the job's folder, and every filed submission lands as a row there. The "
            "Friday compile gathers a week's rows into the weekly packet; the rollup row carries "
            "the Compile Now checkbox for an on-demand compile."
            "\n\n"
            "Day-to-day these are watch-only — tick Compile Now on a week's rollup row when you "
            "need the packet before Friday."
        ),
    ),
    "sheet_wsr": NodeBrief(
        what=(
            "The approval desk for Weekly Safety Reports — the human gate on the only path where "
            "safety reports leave the building. The Friday compile writes one row per job per "
            "week with the draft email body and the compiled PDF; the send daemon dispatches only "
            "rows a human approved."
            "\n\n"
            "Day-to-day this is your main approval surface: review the attached PDF, edit Email "
            "Body if needed (it IS what gets sent), then tick Approve for Scheduled Send (goes "
            "out Monday morning) or Send Now. Nothing sends without that tick, and the system "
            "verifies the approver is authorized (a workspace member)."
        ),
        key_label="Key columns",
        key_line=(
            "Job/Project · Week Of · Compiled PDF · Email Body (the send source of truth) · "
            "Approve for Scheduled Send / Send Now · Approved By/At · Send Status · Sent At"
        ),
    ),
    "box": NodeBrief(
        what=(
            "Every ITS-produced document lands here first, and everything else points at it. "
            "Approval rows carry a Box link and an attached copy, and the send daemons download "
            "the Box original at send time."
            "\n\n"
            "Day-to-day this is watch-only. When filing fails, check ITS_Errors for Box rows and "
            "the watchdog's token-freshness check — the sign-in credential renews whenever ITS "
            "talks to Box and expires 60 days after its last use, so a stopped daemon or long "
            "outage is the usual cause. Restarting that daemon is ordinary work; re-seeding the "
            "credential is secrets work — escalate. Never delete a filed document: a missing link "
            "HOLDS the send, a dead one retries until it gives up."
        ),
        key_label="Key facts",
        key_line=(
            "ITS signs in as a real Box user, not a service account — filed documents attribute "
            "to that account."
        ),
    ),
    "weekly_send": NodeBrief(
        what=(
            "The shared send engine — the progress, PO, RFQ and subcontract sends are all "
            "bindings of it. Send Now dispatches next cycle, scheduled approval on Monday at 7am "
            "Pacific. Recipients come off the job's row at send time, stakeholder deliberately "
            "excluded. A transient error becomes FAILED and retries; what it can't send safely "
            "becomes HELD."
            "\n\n"
            "Day-to-day you approve on the review sheet. If a report doesn't arrive, read its "
            "Send Status and Notes: HELD for a blank contact email is yours — fix the job row, "
            "set PENDING and re-tick approval. Other HELD reasons follow the runbook, and a "
            "workstream mismatch escalates. Pausing is ordinary; enabling a lane or changing who "
            "may approve is developer-level."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: fresh heartbeat, rows moving PENDING → SENT. Otherwise heartbeat age, the "
            "gate, then Send Status and Notes — SENDING may already have sent, so confirm first."
        ),
    ),
    # ── progress band ───────────────────────────────────────────────────────
    "progress_weekly_generate": NodeBrief(
        what=(
            "The progress twin of the safety compile, staggered half an hour behind it, on its "
            "own job roster. Per active job it builds two things: the five-page Weekly "
            "Production Report the client receives, and an internal packet of the filed daily "
            "reports. The approval row on WPR_human_review points at the report; the packet's "
            "link rides its Notes. A week with no dailies filed is written HELD, because only a "
            "person can tell a demobilized crew from one that forgot."
            "\n\n"
            "Watch-only. If a week's row never appears, check ITS_Errors — a missed "
            "Friday re-fires on its own. If a client got the daily-report stack instead of the "
            "report, the build failed and the compile fell back deliberately: look for "
            "client_report_failed and re-run that job."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: a WPR row per active job each week and a fresh run marker. Otherwise check "
            "ITS_Errors, the launchd job, and system.state — its only switch."
        ),
    ),
    "sheet_active_jobs_progress": NodeBrief(
        what=(
            "The progress workspace's own copy of the active-jobs list, carrying the "
            "progress-report recipient emails instead of the safety ones. The fieldops-sync "
            "daemon writes it automatically as a mirror of the portal's job list."
            "\n\n"
            "Day-to-day the only cells you own are the recipient columns: keep the Progress "
            "Reports Contact + CC emails correct. Leave Portal Job Key and Job ID alone — they "
            "are system-managed join keys."
        ),
        key_label="Key columns",
        key_line=(
            "Project Name · Job ID · Progress Reports Contact + CC 1–5 · Portal Job Key (system "
            "join key — don't edit)"
        ),
    ),
    "sheet_wpr": NodeBrief(
        what=(
            "The approval desk for Weekly Progress Reports — the exact progress-side twin of the "
            "safety WSR sheet: same columns, same approval checkboxes, same rules."
            "\n\n"
            "Day-to-day: review the compiled PDF, edit Email Body if needed, tick Approve for "
            "Scheduled Send or Send Now."
        ),
    ),
    "progress_send": NodeBrief(
        what=(
            "The progress twin of the safety send: it verifies each approved WPR row's approver "
            "against the Progress Reporting workspace, then emails the packet to recipients read "
            "from ITS_Active_Jobs_Progress at send time. Its one deliberate difference: a blank "
            "Progress Reports Contact falls back to the job's Stakeholder Email — logged, never "
            "silent — where safety would hold the report."
            "\n\n"
            "Day-to-day approval happens on the WPR sheet, not here. When a report doesn't "
            "arrive, read the row's Send Status and Notes: HELD means it refused rather than "
            "mis-sent — an unknown job, or both addresses blank. Nothing sending at all means "
            "nobody is shared into that workspace. Contamination HELDs, and re-enabling this "
            "daemon, go to the developer."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy is a heartbeat every cycle and WPR rows moving PENDING → SENT; when it "
            "stalls, check launchd state, then its polling_enabled gate, then the runbook."
        ),
    ),
    # ── field-ops band ──────────────────────────────────────────────────────
    "fieldops_sync": NodeBrief(
        what=(
            "It matches each portal job to its sheet row by Portal Job Key, so hand-added rows "
            "stay untouched, and overwrites portal-owned cells (name, address, contacts, CC, "
            "Active), leaving Notes and operator columns alone. Each sheet commits separately: a "
            "job that lands in safety but fails on progress stays queued and retries."
            "\n\n"
            "Day-to-day this is watch-only: make job and recipient corrections in the portal, "
            "since sheet-side edits are overwritten. If a job stops mirroring, check the "
            "heartbeat, then the Review Queue (workstream progress_reports) for a parked job. A "
            "rejected field-ops token is a secrets matter, and turning the mirror on is a "
            "doctrine decision — escalate to the developer."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: a heartbeat every cycle (~90s default, tunable) and no job left queued. If "
            "it goes stale, check launchd state, then the sync gate, then the runbook."
        ),
    ),
    "trackers": NodeBrief(
        what=(
            "Five write helpers, all driven by fieldops-sync. Hours, incidents, and receipts only "
            "gain rows — a receipt is append-only, corrected by a further event; its two derived "
            "columns (Line Status, Line Qty Received) move as the rollup updates. Equipment and "
            "the material list re-project current state: rows update in place and retire rather "
            "than delete."
            "\n\n"
            "Day-to-day this is watch-only; corrections happen in the portal. If a tracker stops "
            "filling, check the daemon's switch and that pass's, then the error journal: a "
            "permanent failure opens a Review Queue row naming the job. On a row-cap warning, "
            "rename-and-archive the sheet rather than delete rows; a closed job's archive move "
            "never retries, so do it by hand. A rejected credential is secrets work: escalate."
        ),
        key_label="Key facts",
        key_line=(
            "No daemon of its own: five passes inside fieldops-sync, each with its own switch — "
            "writes flow from the portal to the sheets, never back."
        ),
    ),
    "sheet_trackers": NodeBrief(
        what=(
            "A dynamic family of per-job standing trackers — one Hours Log (plus equipment, "
            "materials, incidents, and material-receipts siblings) per job, written one-way-up "
            "from the portal's field capture by fieldops-sync. Append-only: rows are corrected by "
            "superseding entries, never deleted."
            "\n\n"
            "Day-to-day these are watch-only office views; corrections happen in the portal, and "
            "the sheets follow."
        ),
    ),
    # ── procurement band — POs · estimates · RFQs ───────────────────────────
    "po_poll": NodeBrief(
        what=(
            "The Mac-side half of the purchase-order pipeline: it re-adds every total in whole "
            "cents against the signed draft and refuses a mismatch outright. What passes becomes "
            "a PDF in the job's Box folder, a PO_Log row and a row on the approval desk, while a "
            "separate pass screens the office's attached specs and drawings. It sends nothing."
            "\n\n"
            "Day-to-day this is watch-only. If POs stop appearing, check its gates in ITS_Config: "
            "gated off it writes no heartbeat, so stale means dark, not broken. Refusals land in "
            "the Review Queue, fenced until you fix the cause and clear the fence per the runbook "
            "— signature, totals and credential failures escalate to the developer."
        ),
        key_label="Key signals",
        key_line=(
            "Gates decide whether a cycle runs · with a pass on, heartbeat and marker refresh "
            "each ~90s · a marker stale past ~8 min raises watchdog Check C"
        ),
    ),
    "estimate_poll": NodeBrief(
        what=(
            "A surviving document files to the job's Vendor Quotes folder in Box, but every "
            "dollar stays advisory. Extracted lines reach a draft purchase order only when "
            "someone accepts them on the disposition screen, and not before the source preview "
            "loads or the no-preview box is ticked. A refusal writes a Review Queue row; a "
            "screening or wrong-type refusal deletes the upload, so the office re-uploads."
            "\n\n"
            "Day-to-day this is watch-only: for a stuck upload check heartbeat age, then its "
            "Review Queue row. A permanent refusal is remembered in a flag file; delete that "
            "entry to retry a misclassification, never a security verdict. An integrity failure, "
            "malicious verdict, rejected credentials, or a first switch-on escalate."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: a fresh heartbeat each ~120-second cycle and Estimate_Log rows for new "
            "uploads. No heartbeat → check the polling gate, then the launchd job, then the "
            "runbook."
        ),
    ),
    "manifest_poll": NodeBrief(
        what=(
            "The office uploads a bill of materials or a shipping log; this daemon turns it into "
            "a grid someone can check on screen. It re-verifies the signature and "
            "the bytes, screens the file, then reads the cells inside a throwaway child process, "
            "so a booby-trapped spreadsheet crashes that child instead of the daemon. The "
            "original is filed to the job's Materials folder in Box."
            "\n\n"
            "It deliberately decides nothing: on a revision BOM it only PROPOSES which quantity "
            "column is current, and the office confirms on the validate screen before any line "
            "is committed. A document it cannot read gets a Review Queue row asking for a better "
            "copy — ordinary, not a fault. An integrity failure, a malicious verdict, or a first "
            "switch-on escalate."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: a fresh heartbeat each ~120-second cycle and uploads reaching 'parsed'. "
            "No heartbeat → check the polling gate, then the launchd job, then the runbook."
        ),
    ),
    "rfq_poll": NodeBrief(
        what=(
            "An RFQ (request for quote) starts in the portal, where the office names its vendors. "
            "Each vendor gets its own price-free PDF and fillable quote form, filed to the job's "
            "Box folder and staged on the approval desk. Nothing reaches a vendor here: the send "
            "is a separate, human-approved step. An unknown vendor is fenced alone; the rest "
            "proceed."
            "\n\n"
            "Day-to-day this is watch-only. If nothing arrives, check the heartbeat, then the "
            "polling switch in ITS_Config. An unknown-vendor fence is yours: fix the ITS_Vendors "
            "row and re-issue that copy from the portal — it never re-issues itself. Signature or "
            "credential failures escalate to the developer."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy = a heartbeat about every two minutes (default interval, tunable) and no "
            "CRITICALs. Missing? Check the gate in ITS_Config, then whether the launchd job is "
            "loaded."
        ),
    ),
    "sheet_its_vendors": NodeBrief(
        what=(
            "The company's master vendor list — the single source of truth for vendor names, "
            "contact emails, and terms. The portal's vendor picker syncs from it, and the PO/RFQ "
            "send steps look up the vendor's Contact Email here at the moment of sending."
            "\n\n"
            "Day-to-day you keep vendor contact emails current — a blank email doesn't mis-send, "
            "it HOLDS the PO/RFQ until you fill it in. Deactivate vendors via Active rather than "
            "deleting rows, and never edit Vendor Key."
        ),
        key_label="Key columns",
        key_line=(
            "Vendor Key (permanent ID — don't touch) · Contact Email (what sends resolve) · "
            "Supply Categories · Default Terms Profile · Active"
        ),
    ),
    "sheet_po_log": NodeBrief(
        what=(
            "The purchase-order ledger: one row per PO showing its number, vendor, total, and "
            "lifecycle stage. The po-poll daemon writes it automatically as a mirror of the "
            "portal's authoritative database."
            "\n\n"
            "Day-to-day it's read-only — a convenient office view of all POs without opening the "
            "portal. Don't edit rows here (the portal is the master), and never do math on Total "
            "(it's a display string)."
        ),
    ),
    "sheet_po_pending_review": NodeBrief(
        what=(
            "The approval desk for outgoing purchase orders — same layout and rules as the "
            "weekly-report approval sheets: review, optionally edit Email Body, tick Approve for "
            "Scheduled Send or Send Now; only then does po-send email the vendor."
            "\n\n"
            "Note three columns are borrowed slots: \"Job ID\" holds the Vendor Key, \"Week Of\" "
            "holds the PO date, and \"Compiled PDF\" is the PO PDF link."
        ),
    ),
    "sheet_estimate_log": NodeBrief(
        what=(
            "The ledger of vendor quotes/estimates uploaded through the portal: one row per "
            "document showing whether it was imported, refused, or needs review. The "
            "estimate-poll daemon writes it automatically."
            "\n\n"
            "Day-to-day it's watch-only — the import history at a glance. Refused or needs-review "
            "items get their real handling in the portal's disposition screen or the Review "
            "Queue, not here."
        ),
    ),
    "sheet_rfq_log": NodeBrief(
        what=(
            "The ledger of outbound Requests-for-Quote: one row per RFQ per vendor (an RFQ sent "
            "to three vendors is three rows), tracking its lifecycle from queued through "
            "responded."
            "\n\n"
            "Day-to-day it's watch-only — a quick answer to \"which vendors have we asked, and who "
            "has responded.\" When a vendor's quote comes back through the portal, the row flips "
            "to responded on its own."
        ),
    ),
    "sheet_rfq_pending_review": NodeBrief(
        what=(
            "The approval desk for outgoing RFQs — one row per RFQ per vendor, same layout and "
            "rules as the PO approval sheet. On approval, rfq-send emails the vendor the "
            "price-free RFQ PDF plus a fillable quote form."
            "\n\n"
            "Same borrowed slots as the PO sheet (\"Job ID\" = Vendor Key). Its distinct Workstream "
            "tag is a safety wall so the PO and subcontract senders can never dispatch an RFQ row "
            "— never change it."
        ),
    ),
    "po_send": NodeBrief(
        what=(
            "It scans PO_Pending_Review for rows ticked Send Now or Approve for Scheduled Send "
            "(Monday's batch), confirms the approver is in the ITS — Purchase Orders workspace, "
            "then emails the PO PDF to the vendor address read live from ITS_Vendors. An unknown "
            "vendor, blank email, or missing PO number HOLDS the row."
            "\n\n"
            "Day-to-day you approve on the review sheet. When POs stop going out, check its gate "
            "and the approver workspace: an empty one blocks every send. Clear a HELD row by "
            "fixing the data, setting Send Status to PENDING and re-ticking approval — never "
            "force a send. Pausing is ordinary; turning the gate on, a wrong-Workstream HOLD, or "
            "repeated failures go to the developer."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: heartbeat refreshed each cycle, rows moving PENDING → SENT. Quiet? Check "
            "its gate and launchd state — a paused gate writes no heartbeat by design."
        ),
    ),
    "rfq_send": NodeBrief(
        what=(
            "The only send carrying two attachments — the price-free RFQ PDF and the vendor's "
            "fillable quote form, for the vendor to price and return. If Box can't supply the "
            "form, the RFQ goes out alone and is logged. Otherwise it mirrors the PO send, "
            "cleared by the same approvers who release POs, with the vendor's email read live "
            "from ITS_Vendors."
            "\n\n"
            "Day-to-day approvals happen on the RFQ approval sheet, not here. A row HELD for a "
            "missing vendor email, filed PDF or RFQ number needs the data fixed, Send Status set "
            "back to PENDING, and the approval re-ticked. Pausing is ordinary; turning the gate "
            "on, or a wrong-lane HELD, escalates."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: a fresh heartbeat every ~15-minute cycle, nothing stuck in SENDING "
            "(watchdog Check N). Quiet instead? Check the gate, then launchd, then the runbook."
        ),
    ),
    # ── subcontracts band ───────────────────────────────────────────────────
    "subcontract_poll": NodeBrief(
        what=(
            "It drains the portal's queue of subcontract drafts, re-checking each tamper seal, "
            "re-adding the schedule-of-values against the contract price, and taking "
            "subcontractor identity from the Smartsheet roster rather than the portal's copy. "
            "What survives becomes three editable files in the job's Box folder and a PENDING "
            "approval row; nothing reaches a subcontractor until a human approves."
            "\n\n"
            "Day-to-day this is watch-only. A stale heartbeat can mean switched off, not broken — "
            "check its gates first. A refused draft lands in the Review Queue and is skipped "
            "thereafter; where the runbook calls the cause operator-fixable, fix it and clear "
            "that draft from the skip-list file it names. Tamper-seal or price mismatches, and "
            "missing credentials, escalate."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: heartbeat refreshing each cycle, nothing fenced. Stale or fenced: check its "
            "three pass gates, then its error rows, then the runbook."
        ),
    ),
    "sheet_its_subcontractors": NodeBrief(
        what=(
            "The master subcontractor list — the single source of truth for subcontractor "
            "identity, contact emails, trades, and home state (which drives each contract's "
            "governing law). The subcontract send step resolves the recipient's Contact Email "
            "here at send time."
            "\n\n"
            "Day-to-day: keep contact emails and State accurate (a blank email HOLDS the send, "
            "never drops it), deactivate via Active rather than deleting, and never edit Sub Key."
        ),
        key_label="Key columns",
        key_line=(
            "Sub Key (permanent ID) · Contact Email · Trades · State (governing law derives from "
            "it) · Active"
        ),
    ),
    "sheet_subcontract_log": NodeBrief(
        what=(
            "The subcontract ledger: one row per subcontract, tracking it from pending review "
            "through executed. The subcontract-poll daemon writes it automatically as a mirror of "
            "the portal's database."
            "\n\n"
            "Day-to-day it's read-only — an office view of every subcontract's status. Don't edit "
            "rows, and never do math on the Total display string."
        ),
    ),
    "sheet_subcontract_pending_review": NodeBrief(
        what=(
            "The approval desk for outgoing subcontract packages — same layout and rules as the "
            "other send-approval sheets. On approval, subcontract-send emails the package "
            "(contract + Exhibit A + schedule of values, one editable ZIP) to the subcontractor."
            "\n\n"
            "Borrowed slots: \"Job ID\" = Sub Key, \"Week Of\" = subcontract date, \"Compiled PDF\" = "
            "the package link."
        ),
    ),
    "subcontract_send": NodeBrief(
        what=(
            "It emails approved subcontracts, but only when the approver belongs to the ITS — "
            "Subcontracts workspace rather than procurement's. The subcontractor gets one file "
            "and no CC, by design: a combined package zip of contract, Exhibit A and schedule of "
            "values. Send Now goes next cycle, scheduled approvals Monday morning. A blank "
            "Contact Email or a missing package holds the row instead of sending."
            "\n\n"
            "Day-to-day you fix the data behind a HELD row — usually the Contact Email in "
            "ITS_Subcontractors — and re-tick approval, never marking a row SENT yourself. Any "
            "other hold, a transport or credential failure, or turning the daemon's gate on "
            "escalates to the developer."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: fresh heartbeat each cycle, no row stuck in SENDING. Stale past two cycles "
            "— check launchd, then the gate."
        ),
    ),
    # ── outside world ───────────────────────────────────────────────────────
    "graph": NodeBrief(
        what=(
            "The send daemon hands Microsoft 365 a finished message, recipients resolved at that "
            "moment from the job, vendor, or subcontractor list. Microsoft delivers it from the "
            "workstream's own mailbox — which the ITS app must be permitted to send as — and "
            "keeps a copy in Sent Items, verifiable in Outlook."
            "\n\n"
            "Day-to-day this is watch-only. When something doesn't arrive, check the approval "
            "row's Send Status: a rejected message returns FAILED and retries, raising a CRITICAL "
            "and stopping after three failures, while an over-size packet is HELD, which retrying "
            "never fixes. Authentication or permission errors escalate to the developer, as does "
            "turning a send gate on; pausing one is ordinary operator work."
        ),
        key_label="Key facts",
        key_line=(
            "Sending mailbox: a per-lane ITS_Config setting · Sent Items proves what went out · "
            "150 MB is Microsoft's ceiling"
        ),
    ),
    # ── machine plane ───────────────────────────────────────────────────────
    "watchdog": NodeBrief(
        what=(
            "Green is silent: a passing check writes no error row. One failing check never stops "
            "the rest — each WARN or CRITICAL lands as an ITS_Errors row filed under the "
            "watchdog. Some checks also repair, re-firing a missed Friday compile or keeping "
            "sheets and disk from wedging."
            "\n\n"
            "Day-to-day this is watch-only: read the sweep panel, where a WARN points at the node "
            "it names, not the watchdog. A stale sweep means confirming the job is loaded — the "
            "daemons control restarts it — then checking system state, since PAUSED skips every "
            "check and MAINTENANCE silences alerts. Threshold edits in ITS_Config are ordinary; "
            "changing what a check tests is a developer change."
        ),
        key_label="Key signals",
        key_line=(
            "A fresh sweep with no WARN or CRITICAL letters is healthy; a sweep older than about "
            "a day means checking the job is loaded, then system state."
        ),
    ),
    "picklist_sync": NodeBrief(
        what=(
            "Smartsheet won't sync one sheet's dropdown to a master list on another; this job "
            "does it hourly. Per enabled mapping it adds what the target lacks and removes what "
            "the master dropped — never an option live cells still use, which stays and raises a "
            "mismatched-reference Review Queue row."
            "\n\n"
            "Day-to-day you clear those rows, choosing to clean up the cells using the value, "
            "restore it upstream, or accept the option. Error rows name a failing mapping — "
            "usually a target column renamed, no longer a dropdown, or unreadable. Three or more "
            "failures in a run raise a CRITICAL; anything pointing at credentials or code "
            "escalates to the developer."
        ),
        key_label="Key signals",
        key_line=(
            "A healthy hour refreshes the run marker; the watchdog sweep (Check C) flags one "
            "stale past three hours — start with the launchd job, then the log tail."
        ),
    ),
    "picklist_audit": NodeBrief(
        what=(
            "It checks the dropdown values the code expects against how the live sheets are "
            "configured, flagging a column that isn't a dropdown, an option list that no longer "
            "matches, or a column missing. It changes no dropdown: findings become WARN rows in "
            "ITS_Errors coded picklist_drift, and a finished run refreshes the marker the "
            "watchdog watches."
            "\n\n"
            "Day-to-day it's watch-only until a picklist_drift row appears; read its Message. If "
            "the live list is only missing values the code already has, the runbook's reconcile "
            "is yours: preview, confirm, commit, re-run — additive only, never removing an "
            "option. Anything else is a code or schema decision: escalate to the developer."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: fresh marker, no picklist_drift rows; exit code 1 means drift, not a crash. "
            "No heartbeat row — watchdog Check C staleness is its only liveness signal."
        ),
    ),
    "publish_daemon": NodeBrief(
        what=(
            "An admin publishes a form edit or retirement from the portal editor, which only "
            "queues the request. This daemon does the rest: it re-checks the edit against live "
            "code and the rules that keep forms legally complete, then commits, tests, merges, "
            "deploys the portal, and refreshes the Box blank-form archive."
            "\n\n"
            "Day-to-day this is watch-only — the portal's publish-status panel tracks each "
            "request. A failure raises a CRITICAL naming the stage and reason, usually a missing "
            "required section, restored in the editor before re-publishing. One stalled past 45 "
            "minutes is reclaimed as failed, freeing that form. Pausing only queues requests; "
            "turning it on, or any failure naming the deploy, credentials, or a migration, "
            "escalates."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: fresh heartbeat, requests reaching \"archived\". Otherwise check the stuck "
            "request's stage and reason, then the gate and runbook."
        ),
    ),
    "config_actuator": NodeBrief(
        what=(
            "The portal lets an office admin edit business config — purchaser identity, tax "
            "rates, a new terms or Exhibit A version — but can only queue it. This Mac-side "
            "daemon makes it real: it re-validates against live code, commits, merges, and "
            "redeploys the portal, stamping each step onto its status monitor."
            "\n\n"
            "Day-to-day you watch a queued change reach Live there, or read why it stopped. A "
            "rejected value means the edit was wrong — re-do it in the portal and the next cycle "
            "takes it. A failed merge or deploy, blocked migrations, clearing a version for legal "
            "use, or switching this rail on are developer calls; pausing it is ordinary work."
        ),
        key_label="Key signals",
        key_line=(
            "Healthy: a claimed change walks Validated → Tested → Live in minutes, not seconds — "
            "it waits on the automated checks. Stalled: check polling_enabled, heartbeat age, "
            "then the runbook."
        ),
    ),
    "dashboard": NodeBrief(
        what=(
            "Most panels read straight off this Mac — launchd, watchdog markers, the Smartsheet "
            "circuit breaker, daemon heartbeats, locks, the log tail — plus cached Smartsheet "
            "reads. The split is deliberate: local panels stay truthful when Smartsheet is "
            "unreachable. Every action you take is audited to ITS_Errors."
            "\n\n"
            "Day-to-day you start at the pulse strip, open whatever looks wrong, then act: pause "
            "a daemon, clear a tripped breaker, mark errors resolved, close out review rows. "
            "Restart the service if the page won't load. Escalate to the developer for an "
            "unprovisioned operator PIN, a setting it calls non-editable, or turning a send gate "
            "ON; pausing is always yours."
        ),
        key_label="Key facts",
        key_line=(
            "Localhost-only, reachable beyond this Mac over Tailscale, never public · panels "
            "refresh every 15 seconds · one \"unavailable\" card is fail-soft, not an outage"
        ),
    ),
    "error_log": NodeBrief(
        what=(
            "Shared plumbing, not a daemon — every script's wrapper reports failures here. It "
            "writes the local log first, then the ITS_Errors row, and on CRITICAL an operator "
            "email and a Sentry event under one correlation ID. Each leg is fenced, so one "
            "failing never blocks the others, and anything leaving this Mac is scrubbed for "
            "secrets while the local log keeps the full text."
            "\n\n"
            "Day-to-day there is nothing to run: no schedule, no on/off switch. Every occurrence "
            "gets its row; throttling belongs to the operator-alerts node. If rows stop "
            "appearing, that is Smartsheet: read the circuit-breaker runbook. Alert credentials "
            "and what raises a page: escalate to the developer."
        ),
        key_label="Key facts",
        key_line=(
            "No schedule, no gate — it runs inside every other script, and its own failures "
            "surface in the local log as [resend-alert-failed] / [sentry-capture-failed] markers."
        ),
    ),
    "sheet_config": NodeBrief(
        what=(
            "The system's settings panel: every on/off switch, polling interval, and tunable "
            "value ITS reads at runtime lives here as one row per setting. Daemons read it "
            "constantly; it is written by you — directly or through this dashboard's config "
            "editor — and by the sanctioned config-actuation path."
            "\n\n"
            "Day-to-day you edit values here to pause a daemon, flip a feature on, or tune a "
            "threshold — but always read a row's full Description first: some rows carry "
            "activation preconditions, and honoring them is doctrine."
        ),
        key_label="Key columns",
        key_line=(
            "Setting + Workstream (together identify a row) · Value (the live state) · "
            "Description (read before flipping) · Modified/Modified By (audit trail)"
        ),
    ),
    "sheet_errors": NodeBrief(
        what=(
            "The system's error journal: every warning, error, and critical alarm any ITS script "
            "raises writes one row here via the error-log decorator. The watchdog and this "
            "dashboard read it — an open CRITICAL (blank Resolved At) is what the \"am I on fire\" "
            "checks count."
            "\n\n"
            "Day-to-day you watch for CRITICAL rows, fix or acknowledge the underlying issue, "
            "then mark rows resolved with the dashboard's mark-resolved verb (it stamps Resolved "
            "At; a Script/Error-code filter is required). Open CRITICALs are never auto-deleted."
        ),
        key_label="Key columns",
        key_line=(
            "Error (code) · Severity · Script · Message · Correlation_ID (links the row to its "
            "alert email / Sentry event) · Resolved At (blank = open)"
        ),
    ),
    "sheet_review_queue": NodeBrief(
        what=(
            "The \"needs a human look\" inbox: anything ITS wasn't confident enough to handle on "
            "its own — low-confidence extractions, refused documents, security-flagged items — "
            "lands here rather than failing silently."
            "\n\n"
            "Day-to-day you review PENDING rows, act on the underlying item, and resolve them to "
            "APPROVED/REJECTED (the dashboard's review-resolve verb, filter required). Treat any "
            "row with Security Flag checked as top priority — that is the adversarial-input "
            "tripwire."
        ),
        key_label="Key columns",
        key_line=(
            "Item ID · Workstream · Summary · Reason · Severity · SLA Tier · Status (PENDING → "
            "APPROVED/REJECTED) · Security Flag · Resolution Notes"
        ),
    ),
    "sheet_daemon_health": NodeBrief(
        what=(
            "The heartbeat board for all background daemons: one row per daemon, updated in place "
            "every cycle, showing when it last ran and how the run went. Every polling daemon "
            "writes its own row; you and the watchdog read it."
            "\n\n"
            "Day-to-day this is watch-only — a stale Last Heartbeat or a bad Last Cycle Status "
            "means a daemon is stuck (the watchdog usually alerts first). The Enabled checkbox is "
            "display metadata only; the real on/off switch is the daemon's polling_enabled row in "
            "ITS_Config. This dashboard's heartbeats panel reads the same daemons' local "
            "heartbeat files directly, so it stays truthful even when Smartsheet is down."
        ),
        key_label="Key columns",
        key_line=(
            "Daemon Name · Last Heartbeat · Last Cycle Status · Items Processed · Total Cycles "
            "(lifetime, not daily) · Last Error Summary"
        ),
    ),
    "sheet_quarantine": NodeBrief(
        what=(
            "The holding pen for suspicious inbound email: any message from a sender not on the "
            "trusted list, or failing forgery checks, is logged here instead of being processed. "
            "Nothing automated ever acts on a quarantined message."
            "\n\n"
            "Day-to-day you skim new rows, decide whether each sender is legitimate, add good "
            "senders to the trusted-contacts allowlist, and mark rows Reviewed. This surface "
            "becomes load-bearing when the Email Triage workstream processes inbound mail."
        ),
        key_label="Key columns",
        key_line=(
            "Sender · Subject · Summary (first ~200 chars, deliberately never AI-processed) · "
            "Reviewed · Added to Allowlist · Notes (quarantine reason)"
        ),
    ),
    "sheet_project_routing": NodeBrief(
        what=(
            "The project-to-Box-folder map: it tells ITS which Box folder belongs to each "
            "project, so document filing can be re-routed without a developer editing code."
            "\n\n"
            "Day-to-day you only touch it when onboarding or retiring a project — add a row with "
            "the project name and its Box folder ID, or untick Active to retire one."
        ),
        key_label="Key columns",
        key_line="Project Name (exact-match key) · Box Folder ID · Active",
    ),
    "sheet_time_off": NodeBrief(
        what=(
            "The PTO calendar ITS consults when deciding who should review or receive something: "
            "one row per person per time-off span (start and end dates, both inclusive). The "
            "reviewer-chain logic reads it and automatically skips anyone who's out, promoting "
            "the next person in the chain; watchdog Check D scans two weeks ahead for windows "
            "where a chain would have nobody available."
            "\n\n"
            "Day-to-day: add a row when someone will be out (retroactive entries work too); "
            "nothing else to do."
        ),
        key_label="Key columns",
        key_line="person email · start date · end date (inclusive)",
    ),
    "sheet_picklist_sync_config": NodeBrief(
        what=(
            "The wiring diagram for dropdown-list syncing: each row maps a source column (e.g. a "
            "master database sheet) to a target sheet's dropdown so option lists stay consistent "
            "across sheets. The hourly picklist-sync job reads it; the Sunday audit checks it for "
            "drift."
            "\n\n"
            "Day-to-day you rarely touch it — add or disable a mapping row only when a new "
            "dropdown needs to track a master list. Removing an option that live cells still use "
            "is automatically blocked and routed to the Review Queue."
        ),
        key_label="Key columns",
        key_line="mapping_id · source_sheet_id/source_column · target_sheet_id/target_column · enabled",
    ),
    "registry_sheets": NodeBrief(
        what=(
            "The master-database references behind dropdown syncing — Equipment Master is the "
            "live canonical source picklist-sync propagates from. The Documentation Index (the "
            "card catalog of operator guides, with Box links to published PDFs) also lives in "
            "this supporting tier."
            "\n\n"
            "Day-to-day: edit Equipment Master when equipment options change and the hourly sync "
            "propagates them. The old Vendor DB / Subcontractor DB stubs are superseded by "
            "ITS_Vendors and ITS_Subcontractors — never edit the old stubs."
        ),
    ),
    "alerts": NodeBrief(
        what=(
            "A CRITICAL pages you: the ITS_Errors row, the alert email, and the forensic event "
            "all carry one Correlation_ID. Repeats of the same script and error code are "
            "suppressed for a tunable window, and email is capped per hour: a storm reaches you "
            "as a digest, not a flood, while the row lands every time. With a ping URL set, a "
            "dead Mac shows up only as a missed watchdog ping to an outside monitor."
            "\n\n"
            "Day-to-day nothing here needs running: start from the alert's Correlation_ID in "
            "ITS_Errors. If an expected page never came, check the dedupe window, the cap and the "
            "recipient in the config editor. Service keys are secrets: deeper escalates to the "
            "developer."
        ),
        key_label="Key facts",
        key_line=(
            "CRITICALs page you · email deduped and hourly-capped, the event deduped only, the "
            "ITS_Errors row never · window, cap, recipient in ITS_Config"
        ),
    ),
}
