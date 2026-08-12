"""Capability-gating tests for Foundation Mission v8 Invariant 1 (External Send Gate).

The architectural invariant: scripts that call the Anthropic API to generate customer-facing
content MUST NOT have the capability to send externally. Scripts that send externally MUST
NOT have an AI step. Enforced by static import inspection.

A successful prompt injection at the AI layer cannot cause external transmission, because
the AI is in a different process from the transmitter.

How to extend this test:
- When a new generation script lands, add it to GATED_SCRIPTS.
- When a new send script lands, add it to SEND_SCRIPTS.

The Safety Reports two-process refactor (`weekly_generate.py` + `weekly_send.py`) has landed,
so both lists are populated. Adding new entries is the entire enforcement mechanism for new
workstreams.

Run with: pytest -q tests/test_capability_gating.py
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Generation scripts: must NOT import any send capability.
# Each entry: (relative path from repo root, list of forbidden import substrings)
#
# Note on `graph_client` granularity: intake.py legitimately needs `graph_client`
# READ methods (`get_message`, `list_attachments`, `download_attachment`,
# `mark_read`) for the legacy email-ingestion path (now dormant). `mark_read` is an
# inbox-side write (isRead=True) but is NOT an external transmission — the External
# Send Gate covers customer-facing email, not inbox-cursor management. So
# `graph_client` (broad substring) is allowed; `send_mail` (narrow substring) remains
# forbidden. (`intake_poll.py` — RETIRED 2026-06-05, tombstone DELETED 2026-07-03 after
# launchctl verified no orphan job; a resurrected email poller must re-enroll here.) The
# per-substring AST check below catches `shared.graph_client.send_mail` via
# the "send_mail" needle even when `shared.graph_client` is imported. Future
# generation scripts that do NOT need Graph reads can use a stricter list
# that includes `graph_client` (see the commented templates).
GATED_SCRIPTS: list[tuple[str, list[str]]] = [
    (
        "safety_reports/intake.py",
        ["send_mail", "resend", "smtplib", "email.mime"],
    ),
    (
        # portal_poll is the Phase-5 pull-model daemon: it ingests untrusted portal
        # submissions (HMAC-verified) and files them via intake — generation-side,
        # ZERO external send. The point of the pull model is that the Python puller
        # is INSIDE this AST gate (the TS Worker was outside it). Its TWO egress paths
        # are both audited F02-allowlisted shared clients: shared/portal_client.py
        # (control-plane HTTP to OUR Worker — pull/mark-filed/pdf-request servicing) and
        # shared/box_client.py (Box read/upload, generation-side; added in PR-4 Part A for
        # the filed-PDF re-fetch). portal_poll itself imports NO raw network library and
        # no send capability — egress only through those two audited wrappers.
        "safety_reports/portal_poll.py",
        ["send_mail", "resend", "smtplib", "email.mime"],
    ),
    (
        # weekly_generate is now the DETERMINISTIC portal compile (Phase 5b): no
        # Graph reads, no external send, AND no LLM (the narrative-draft Anthropic
        # core was retired). `anthropic` (catches `anthropic` + `anthropic_client`
        # via substring) is forbidden alongside the send substrings — asserting the
        # compile stays deterministic, not just send-free.
        "safety_reports/weekly_generate.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # compile_core (Stage-0 A6) is the SHARED hardened compile loop both the safety weekly
        # compile and the future progress compile instantiate. It is stdlib-only orchestration
        # (per-job SIGALRM budget + pre-merge memory guard + per-job error fence) — no Graph,
        # no external send, no LLM. Same deterministic-actuation gate as weekly_generate.
        "safety_reports/compile_core.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # generate_core (P4) is the SHARED parameterized weekly-compile engine BOTH the safety
        # and progress weekly compiles instantiate (gather → merge → file Box → dual-write the
        # Rollup + review row), driven by a GenerateConfig. Deterministic: no Graph, no external
        # send, no LLM — the same actuation gate as weekly_generate + compile_core.
        "safety_reports/generate_core.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # publish_daemon (slice 3b) is the privileged form-publish actuator: it COMMITS +
        # DEPLOYS code but performs ZERO external customer transmission and no LLM step.
        # Forbid the send substrings + anthropic (deterministic actuation). Its HTTP egress
        # to OUR Worker lives in shared/portal_client.py (F02-allowlisted); the git/wrangler
        # ops are subprocess to the operator's toolchain, not Python send imports.
        "safety_reports/publish_daemon.py",
        ["send_mail", "resend", "smtplib", "email.mime", "anthropic", "anthropic_client"],
    ),
    (
        # compile_now_poll (Part B) is the on-demand compile poller: it reuses
        # weekly_generate's DETERMINISTIC compile on a Compile-Now trigger. No Graph reads,
        # no external send, no LLM — the same deterministic-actuation gate as weekly_generate.
        "safety_reports/compile_now_poll.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # photo_screen (PR-2) is the §34 portal-photo trust boundary: it inspects
        # UNTRUSTED inbound image bytes (magic/size → Pillow verify + re-encode →
        # optional ClamAV) and is DETERMINISTIC — no customer send AND no LLM. Forbid
        # the send substrings + graph_client (a pure screener needs no Graph) +
        # anthropic (assert it stays LLM-free). Its only egress is the optional,
        # config-gated clamd socket (pyclamd), allowlisted in NETWORK_LIB_ALLOWLIST.
        "safety_reports/photo_screen.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # progress_weekly_generate (P4) is the PROGRESS twin of weekly_generate — the thin
        # progress binding of generate_core (the progress GenerateConfig). DETERMINISTIC: no
        # Graph, no external send, no LLM. Generation half of the External Send Gate for the
        # Progress-Reporting workstream.
        "progress_reports/progress_weekly_generate.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # wpr_data (0067) assembles the CLIENT-FACING Weekly Production Report's data. It reads
        # the send-free Worker aggregate and Box, and builds the narrative sections BY RULE from
        # text the field already typed — the office's own words, reordered, never a model's.
        # Forbidding the LLM imports HERE is what makes that structural rather than conventional:
        # the client-facing document cannot acquire a generated narrative without this list
        # changing in a reviewed diff. `ollama_client` joins the list because a LOCAL model is
        # still a model — the estimate lane's Tier-2 precedent means it is a reachable import.
        "progress_reports/wpr_data.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client", "ollama_client"],
    ),
    (
        # fieldops_sync (P2.5 Slice 5) is the D1→Smartsheet job up-sync daemon: it pulls
        # dirty portal-created jobs and mirrors each UP into BOTH Active-Jobs sheets. SEND-FREE
        # + AI-FREE — the Smartsheet WRITE is the intended SoR-mirror capability, NOT a customer
        # send, and the HTTP egress to OUR Worker rides the F02-allowlisted shared.portal_client
        # (so this module imports no raw network library). Forbid the send substrings +
        # graph_client (a pure mirror needs no Graph) + anthropic (assert it stays LLM-free).
        # NOTE: named *_sync.py, NOT a convention suffix, so the enrollment meta-test does NOT
        # auto-flag it — this explicit entry is the enrollment.
        "field_ops/fieldops_sync.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # manifest_poll (PR3b) is the materials-manifest import daemon: it drains the
        # send-free D1 manifest pool, §34-screens each document, extracts its cell grid in
        # the KILLABLE sandbox child, parses it, files the ORIGINAL bytes to Box and posts
        # the reviewable grid back. It is the highest-exposure process in field_ops — it
        # decodes hostile PDF/xlsx bytes — which is exactly why it holds its OWN bearer and
        # why this entry matters: no send path, no LLM. Its HTTP egress to OUR Worker rides
        # the F02-allowlisted shared.portal_client, so it imports no raw network library.
        "field_ops/manifest_poll.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # Schedule lane (ADR-0006) — parent-side OCR entry: hands untrusted schedule-PDF
        # bytes to the KILLABLE sandbox child (Quartz render + rotation ladder + Apple
        # Vision, all in-child) and shape-validates the JSON that comes back. LOCAL
        # Vision only — the lane is cloud-AI-free by ADR decision 3, and this entry is
        # the tripwire that keeps it that way.
        "field_ops/schedule_ocr.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client", "ollama_client"],
    ),
    (
        # Schedule lane — PURE word-geometry → row reconstruction (no I/O at all). Gated
        # so a future convenience import (an HTTP fetch, an LLM "column guesser") fails
        # loudly here instead of riding a refactor in silently.
        "field_ops/schedule_geometry.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client", "ollama_client", "requests", "urllib"],
    ),
    (
        # Schedule lane — PURE semantic extraction over reconstructed rows (dates,
        # durations, percents, sections; flags-never-raises). Same rationale.
        "field_ops/schedule_parse.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client", "ollama_client", "requests", "urllib"],
    ),
    (
        # Track 6 job archive. Pure relocation of Smartsheet/Box folders — no AI, no send path.
        # Worth gating explicitly rather than relying on its caller: this module reaches BOTH
        # external systems directly, so a future "email the operator when an archive fails" would
        # be a natural-looking addition that must go through the error_log/alerting path instead.
        "field_ops/job_archive.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # po_poll (PO S4) is the Purchase-Order pull daemon — the ONE multi-pass Mac
        # half of the PO pipeline (drafts drain + HMAC verify + totals assert + render
        # + Box/PO_Log/PO_Pending_Review filing + §51 vendor sync + status mirror).
        # GENERATION-side of the External Send Gate: DETERMINISTIC (no LLM) and
        # customer-SEND-FREE — the SEPARATE po_send.py/po_send_poll.py (S5) transmit
        # only after F22-verified human approval. Its egress rides the F02-allowlisted
        # shared.portal_client (our Worker) + shared.box_client (filing) — this module
        # imports no raw network library and no send capability.
        "po_materials/po_poll.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # po_attach_screen (Feature B) is the §34 DOC-attachment trust boundary — the
        # PDF/OpenXML/image sibling of safety_reports/photo_screen (the first real
        # §34 Layer-2 document instantiation). It inspects UNTRUSTED inbound file
        # bytes (magic/consistency → PDF active-content scan → bounded OpenXML zip
        # walk → Pillow verify → optional ClamAV) and is DETERMINISTIC — no customer
        # send AND no LLM. Forbid the send substrings + graph_client + anthropic.
        # Its only egress is the optional, config-gated clamd socket (pyclamd),
        # allowlisted in NETWORK_LIB_ALLOWLIST exactly like photo_screen.
        "po_materials/po_attach_screen.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # po_generate (PO S4) is the DETERMINISTIC PO renderer + the Worker-matching
        # integer-cents money math (the render-time totals assert). Pure data → bytes:
        # no Graph, no external send, no LLM — the same deterministic-actuation gate
        # as weekly_generate. (Replaces the reserved standard_rfq_generate /
        # racking_module_rfq_generate stubs — RFQ is designed-in, built post-delivery
        # per decision D3.)
        "po_materials/po_generate.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # config_actuator (§50 config editor, slice 2) is the privileged PO-config actuator:
        # the twin of safety_reports/publish_daemon.py against the config_requests queue. It
        # COMMITS + DEPLOYS config (purchaser/tax/terms) but performs ZERO external customer
        # transmission and no LLM step. Forbid the send substrings + graph_client (a pure
        # actuator needs no Graph) + anthropic (deterministic actuation). Its HTTP egress to
        # OUR Worker is shared/portal_client.py (F02-allowlisted, below); the git/gh/wrangler/
        # npm ops are subprocess to the operator's toolchain, not Python send imports. NOTE:
        # named *_actuator.py, NOT a convention suffix, so the enrollment meta-test does NOT
        # auto-flag it — this explicit entry IS the enrollment.
        "po_materials/config_actuator.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # subcontract_generate (SC-S3) is the DETERMINISTIC subcontract renderer — the record →
        # filled contract-body text + (S3b) the .docx/.xlsx render, with the num2words price WORDS,
        # the SOV-sums-to-price guard, and the Layer-A legal gate. Pure data → document bytes: no
        # Graph, no external send, no LLM (the operator directive — no AI in the generation path),
        # the same gate as po_generate / weekly_generate.
        "subcontracts/subcontract_generate.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # subcontract_docx (SC-S3b) layers the EDITABLE .docx/.xlsx render on the S3a text core —
        # still pure data → document bytes (python-docx / openpyxl), runs the same gate chain before
        # emitting bytes, and has zero send / zero LLM (operator directive — no AI in generation).
        "subcontracts/subcontract_docx.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # subcontract_poll (SC-S3c) is the subcontract pull daemon — the ONE multi-pass
        # Mac half of the subcontract pipeline (drafts verify+render+file, subcontractor
        # down/up-sync, status mirror). GENERATION-side of the External Send Gate: it
        # renders + files + reports, but never transmits to a customer and runs no LLM
        # (all egress rides the F02-allowlisted portal_client / box_client /
        # smartsheet_client). subcontract_review is transitively covered (imported here).
        "subcontracts/subcontract_poll.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # estimate_poll (ADR-0004 Lane 1, PR-A) is the vendor-estimate pull daemon —
        # the highest-exposure process in the estimate lane (it decodes HOSTILE
        # vendor-document bytes). GENERATION-side of the External Send Gate:
        # DETERMINISTIC (no LLM — the ADR's local-only ladder lands later and is
        # local Ollama, never cloud) and customer-SEND-FREE. Egress rides the
        # F02-allowlisted portal_client (our Worker, ITS_PORTAL_ESTIMATE_TOKEN) +
        # box_client (filing) — no raw network library, no send capability.
        "po_materials/estimate_poll.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # estimate_log (PR-A) is the Estimate_Log ledger writer (the po_log twin
        # for the estimate lane). Pure Smartsheet-write helper via the audited
        # shared clients — no Graph, no send, no LLM.
        "po_materials/estimate_log.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # estimate_classify (PR-A) is the deterministic doc-type gate over UNTRUSTED
        # vendor-document text (keyword/regex scoring; its pdfplumber extract runs
        # inside the estimate_sandbox child, never in-process here). No Graph, no
        # send, no LLM — the invoice/ap_report refusal must stay deterministic.
        "po_materials/estimate_classify.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # estimate_preview (PR-A) renders source-page PNG previews (Quartz, inside
        # the estimate_sandbox child) for the disposition screen's side-by-side
        # fidelity check. Pure bytes → bytes — no Graph, no send, no LLM.
        "po_materials/estimate_preview.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # estimate_sandbox (PR-A, red-team #5) is the KILLABLE child-process runner
        # every hostile-input parse goes through (RLIMIT_AS cap + wall-clock
        # timeout; a wedged parse is reaped, the doc degrades, the daemon lives).
        # Its `subprocess` use is allowlisted in NETWORK_LIB_ALLOWLIST below; it
        # must still be send-free + LLM-free like every module touching hostile bytes.
        "po_materials/estimate_sandbox.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # estimate_parse (ADR-0004 E4, PR-B) is the Tier-1 DETERMINISTIC parse of
        # hostile vendor-document bytes: sandbox-routed pdfplumber payloads, data-
        # driven YAML vendor templates (never exec'd), the generic-table tier, and
        # the _js_round math gate. Pure local parsing — no Graph, no send, no LLM
        # (Tier-1 must stay deterministic; the ONLY AI in the lane is the LOCAL
        # Tier-2 in estimate_extract).
        "po_materials/estimate_parse.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # rfq_poll (ADR-0004 Lane 2, PR-C) is the outbound-RFQ generation daemon —
        # verify (rfq:v1) → per-vendor price-free render → Box → RFQ_Log +
        # RFQ_Pending_Review → mark-filed → status mirror. GENERATION-side of the
        # External Send Gate: DETERMINISTIC (no LLM, cloud or local) and
        # customer-SEND-FREE — the vendor send is PR-D's rfq_send. Egress rides the
        # F02-allowlisted portal_client (our Worker, ITS_PORTAL_RFQ_TOKEN — the
        # lane's OWN bearer, decision 4) + box_client + smartsheet_client.
        "po_materials/rfq_poll.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # estimate_extract (ADR-0004 E5, PR-B) is the Tier-2 LOCAL-LLM extraction —
        # local Ollama ONLY (shared/ollama_client, localhost-refusing, allowlisted
        # below); `anthropic*` is forbidden here so the lane can NEVER escalate to
        # cloud AI (the "sole live Anthropic consumer is intake.py" invariant).
        # GENERATION-side: untrusted pages are wrapped (Invariant 2), output is
        # schema-gated + math-checked + string-field anomaly-checked, and remains
        # ADVISORY until the human disposition accept. No Graph, no send.
        "po_materials/estimate_extract.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # rfq_generate (PR-C) is the deterministic PRICE-FREE RFQ PDF renderer
        # (reportlab invariant=1; every string through form_pdf's escaping path).
        # Pure data → document bytes — no Graph, no send, no LLM.
        "po_materials/rfq_generate.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # estimate_ocr (ADR-0004 E5, PR-B) recovers text from SCANNED estimates via
        # the sandboxed Quartz render (estimate_preview's laundered PNGs) + the
        # LOCAL macOS Vision framework (ocrmac, lazy import, degrades to []).
        # Pure local bytes → text — no Graph, no send, no LLM.
        "po_materials/estimate_ocr.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # rfq_naming (PR-C) is the pure RFQ filename/title helper (the po_naming
        # twin) — no I/O at all; enrolled so the naming surface every delivery
        # path shares can never quietly grow a capability.
        "po_materials/rfq_naming.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # quote_form (ADR-0004 decision 10 / E6, PR-B) renders the Tier-0 fillable
        # RFQ quote form and parses the RETURNED (hostile) form — pure bytes→bytes
        # openpyxl work over §34-screened input, with the red-team #3 formula
        # rejection. No Graph, no send, no LLM (cloud OR local — the Tier-0
        # round-trip is deterministic by doctrine). NOTE: not a convention suffix —
        # this explicit entry IS the enrollment.
        "po_materials/quote_form.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    (
        # eval_estimate_ladder (E6, PR-B) is the OPERATOR-RUN OFFLINE corpus eval —
        # by contract ZERO Worker/Smartsheet/Box egress (localhost Ollama only,
        # and only under --tier2, via the gated estimate_extract). Beyond the
        # send/AI forbids, ALSO forbid every egress client the daemons use: the
        # corpus replay must stay a pure local read forever. NOTE: scripts/ is
        # outside the convention meta-test — this explicit entry IS the enrollment.
        "scripts/eval_estimate_ladder.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client", "portal_client", "box_client",
         "smartsheet_client", "review_queue", "heartbeat"],
    ),
    (
        # rfq_log (PR-C) is the RFQ_Log (rfq, vendor) ledger writer (the
        # estimate_log twin). Pure Smartsheet-write helper via the audited shared
        # clients — no Graph, no send, no LLM.
        "po_materials/rfq_log.py",
        ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
         "anthropic", "anthropic_client"],
    ),
    # --- the OLDER twins of everything above -------------------------------
    # The RFQ/estimate lane (the newest) enrolled its naming / ledger / review /
    # reference helpers carefully. Their structural twins in the PO, subcontract,
    # safety and progress lanes — same job, same trust position, most of them
    # OLDER and more heavily used — were never enrolled, so nothing structurally
    # stopped any of them growing a send or AI import. None carries a convention
    # suffix (`_generate.py` / `_send.py` / `_poll.py`), so the enrollment
    # meta-test never demanded them either: they fell through BOTH nets.
    # Same standard forbid list as their enrolled twins.
    *(
        (
            path,
            ["graph_client", "send_mail", "resend", "smtplib", "email.mime",
             "anthropic", "anthropic_client"],
        )
        for path in (
            # naming + ledger writers (twins of rfq_naming / rfq_log / estimate_log)
            "po_materials/po_naming.py",
            "po_materials/po_log.py",
            # review-row writers — these stage the very rows the SEND daemons
            # dispatch, so a send or AI import here would sit directly on the gate
            # boundary
            "po_materials/po_review.py",
            "po_materials/rfq_review.py",
            "subcontracts/subcontract_review.py",
            "safety_reports/wsr_review.py",
            "progress_reports/wpr_review.py",
            # reference / computation helpers the delivery paths share
            "po_materials/vendors.py",
            "po_materials/terms.py",
            "subcontracts/money.py",
            "subcontracts/terms.py",
            "subcontracts/exhibit.py",
            "subcontracts/governing_law.py",
        )
    ),
]

# Send scripts: must NOT import any AI capability.
SEND_SCRIPTS: list[tuple[str, list[str]]] = [
    (
        "safety_reports/weekly_send.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # weekly_send_poll imports safety_reports.weekly_send which
        # transitively brings in graph_client.send_mail — that's the
        # intended send capability for the workstream. The AST gate
        # checks THIS file's imports specifically; anthropic / anthropic_client
        # must not appear at all.
        "safety_reports/weekly_send_poll.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # P1c: the parameterized dispatch core. It dispatches a send via the
        # bound `DaemonConfig.send_fn` (a partial of weekly_send.send_one_row) —
        # so it is a SEND script. NOTE: it ends in `_core.py`, not `_poll.py`, so
        # the convention-suffix enrollment meta-test does NOT auto-flag it; this
        # explicit entry is the enrollment. anthropic / anthropic_client must not
        # appear at all (the core imports no AI surface).
        "safety_reports/send_poll_core.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # P5: progress_send is the PROGRESS instantiation of the shared send engine —
        # it imports safety_reports.weekly_send (the dispatch logic, which transitively
        # brings in graph_client.send_mail, the intended send capability). anthropic /
        # anthropic_client must not appear at all (no LLM in the send half).
        "progress_reports/progress_send.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # P5: the progress send poller. Imports progress_send (→ weekly_send → graph
        # send) + send_poll_core. anthropic / anthropic_client must not appear at all.
        "progress_reports/progress_send_poll.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # S5b: po_send is the PO instantiation of the shared send engine — it imports
        # safety_reports.weekly_send (the dispatch logic, which transitively brings in
        # graph_client.send_mail, the intended send capability for the vendor audience).
        # anthropic / anthropic_client must not appear at all (no LLM in the send half).
        "po_materials/po_send.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # S5b: the PO send poller. Imports po_send (→ weekly_send → graph send) +
        # send_poll_core. anthropic / anthropic_client must not appear at all.
        "po_materials/po_send_poll.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # SC-S4: subcontract_send is the subcontract instantiation of the shared send engine —
        # it imports safety_reports.weekly_send (the dispatch logic, transitively graph_client.
        # send_mail, the intended send capability for the subcontractor audience). anthropic /
        # anthropic_client must not appear at all (no LLM in the send half).
        "subcontracts/subcontract_send.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # SC-S4: the subcontract send poller. Imports subcontract_send (→ weekly_send → graph
        # send) + send_poll_core. anthropic / anthropic_client must not appear at all.
        "subcontracts/subcontract_send_poll.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # ADR-0004 R3: rfq_send is the outbound-RFQ instantiation of the shared send engine —
        # it imports safety_reports.weekly_send (the dispatch logic, transitively
        # graph_client.send_mail, the intended send capability for the vendor audience) +
        # box_client (to fetch the quote form as the second attachment). No AI at all —
        # anthropic / anthropic_client AND ollama_client (ADR-0004 decision 12: send scripts
        # are local-AI-free too, so the RFQ round trip's extraction ladder can never leak into
        # the send half) must not appear.
        "po_materials/rfq_send.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
    (
        # ADR-0004 R3: the RFQ send poller. Imports rfq_send (→ weekly_send → graph send) +
        # send_poll_core. anthropic / anthropic_client AND ollama_client must not appear.
        "po_materials/rfq_send_poll.py",
        ["anthropic_client", "anthropic", "ollama_client"],
    ),
]


def _imports_in(path: Path) -> set[str]:
    """Return the STATIC imports of a file: each `import X` yields `X`; each `from X import Y`
    yields `X` AND `X.Y`. Honest reach limit: a DYNAMIC import — a bare `__import__(...)` builtin
    call or `importlib.import_module(...)` — is NOT captured here (neither is an Import/ImportFrom
    AST node). `importlib` is itself a NETWORK_NEEDLE, so a static `import importlib` is still
    flagged; a bare `__import__` call on the walked surface is a documented residual gap (the M2
    transitive-closure follow-up tightens it)."""
    tree = ast.parse(path.read_text())
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
                for alias in node.names:
                    imports.add(f"{node.module}.{alias.name}")
    return imports


@pytest.mark.parametrize("rel_path,forbidden", GATED_SCRIPTS)
def test_generation_script_does_not_import_send(rel_path: str, forbidden: list[str]):
    """Generation scripts must not import any send capability (Invariant 1)."""
    path = REPO_ROOT / rel_path
    assert path.exists(), f"missing: {rel_path}"
    imports = _imports_in(path)
    for needle in forbidden:
        for imp in imports:
            assert needle not in imp, (
                f"{rel_path} imports {imp!r}, which contains forbidden {needle!r}. "
                "External Send Gate violation — generation scripts cannot have send capability."
            )


@pytest.mark.parametrize("rel_path,forbidden", SEND_SCRIPTS)
def test_send_script_does_not_import_ai(rel_path: str, forbidden: list[str]):
    """Send scripts must not import any AI capability (Invariant 1)."""
    path = REPO_ROOT / rel_path
    assert path.exists(), f"missing: {rel_path}"
    imports = _imports_in(path)
    for needle in forbidden:
        for imp in imports:
            assert needle not in imp, (
                f"{rel_path} imports {imp!r}, which contains forbidden {needle!r}. "
                "External Send Gate violation — send scripts cannot have AI capability."
            )


def test_lists_documented():
    """Sanity check — both lists exist and are typed correctly."""
    assert isinstance(GATED_SCRIPTS, list)
    assert isinstance(SEND_SCRIPTS, list)


# =========================================================================
# F02 — repo-wide network-capability allowlist (additive defensive layer)
# =========================================================================
#
# The GATED_SCRIPTS / SEND_SCRIPTS checks above are the LANDED Invariant-1
# enforcement: per-script, forbidden-substring, narrow. This block ADDS an
# orthogonal second layer (audit finding F02) and does NOT modify them.
#
# It inverts the question: instead of "these NAMED scripts must not import
# send capability," it asserts "NO module on the untrusted-content surface
# may import a network-egress or process-spawn library UNLESS it is on an
# explicit allowlist." The point is that a module which should never touch
# the network CANNOT acquire that capability undetected — a future
# generation script that quietly `import requests` to exfiltrate fails this
# check at CI time, before it can ship.
#
# ---- Walk scope (operator decision 2026-05-29) --------------------------
#
# Walked roots = the Invariant-1 untrusted-content surface:
#   shared/          — the helper layer every workstream imports.
#   safety_reports/  — the only workstream package today (the AI generation
#                      scripts that process untrusted inbound content).
# Future workstream dirs (po_materials/, subcontracts/, …) get appended to
# WALKED_ROOTS as they land — same per-workstream extension discipline as
# GATED_SCRIPTS above.
#
# Deliberately NOT walked (each with reason — surfaced, not silently picked):
#   scripts/ (incl. scripts/migrations/) — operator-run launchd/CLI entry
#       points, one-shot config seeders, OAuth setup, and smoke tests. These
#       legitimately call REST directly (Smartsheet/Box seeding via
#       `requests`, the Graph smoke's `subprocess`, the OAuth catcher's
#       `http.server`/`urllib`) and are NOT in the untrusted-content path.
#       Walking them would force ~8 operational allowlist entries that
#       dilute the security signal and churn CI on every new migration.
#   smartsheet_migration/, box_migration/ — one-shot data-migration
#       utilities, not runtime workstream code.
#   tests/, docs/, prompts/, schemas/ — non-source / no runtime egress.
#
# ---- Allowlist membership (each entry justified — no entry without one) --
#   shared/graph_client.py      — Microsoft Graph REST (intake mail + sends).
#   shared/resend_client.py     — Resend REST (the canonical CRITICAL push leg).
#   shared/smartsheet_client.py — Smartsheet REST (SDK + direct REST helpers).
#   shared/heartbeat_client.py  — Healthchecks.io outbound beacon (F16 / PR #114).
#   shared/keychain.py          — `subprocess` for the macOS `security` CLI.
#                                 NOT network egress; the secret-store boundary.
#                                 Included because `subprocess` is a needle and
#                                 keychain is the one legitimate non-*_client
#                                 subprocess user on the walked surface.
#   shared/portal_client.py     — Safety Portal Worker REST (the pull-model
#                                 transport: GET /api/internal/pending +
#                                 POST /api/internal/mark-filed). The ONE audited
#                                 egress to the Worker, so portal_poll.py can pull
#                                 WITHOUT importing `requests` itself — keeping the
#                                 puller inside the capability gate the TS Worker
#                                 was outside of.
#   safety_reports/publish_daemon.py — the privileged form-publish ACTUATOR (slice
#                                 3b). `subprocess` is its whole purpose: it runs the
#                                 operator's own git / gh / wrangler to commit + deploy
#                                 a published form (the CF credential never leaves the
#                                 Mac). `socket.gethostname()` builds the per-host lease
#                                 owner. Its Worker HTTP egress goes through the audited
#                                 shared.portal_client (above), not `requests` directly,
#                                 and it stays in GATED_SCRIPTS (no customer send, no LLM).
#   shared/box_client.py        — Box SDK (`boxsdk`): client-document storage/retrieval.
#   shared/anthropic_client.py  — Anthropic SDK (`anthropic`): the LLM reasoning egress (the
#                                 GENERATION half of Invariant 1; GATED scripts forbid importing it).
#   shared/sentry_client.py     — Sentry SDK (`sentry_sdk`): CRITICAL exception-capture egress.
NETWORK_LIB_ALLOWLIST: frozenset[str] = frozenset({
    "shared/graph_client.py",
    "shared/resend_client.py",
    "shared/smartsheet_client.py",
    "shared/heartbeat_client.py",
    "shared/keychain.py",
    "shared/portal_client.py",
    "shared/box_client.py",
    "shared/anthropic_client.py",
    "shared/sentry_client.py",
    "safety_reports/publish_daemon.py",
    # config_actuator (§50 config editor, slice 2) is the privileged PO-config ACTUATOR — the
    # publish_daemon twin against config_requests. `subprocess` is its whole purpose (it runs
    # the operator's own git / gh / wrangler / npm to commit + deploy a config edit; the CF
    # credential never leaves the Mac). `socket.gethostname()` builds the per-host lease owner.
    # Its Worker HTTP egress goes through the audited shared.portal_client (above), not
    # `requests`, and it stays in GATED_SCRIPTS (no customer send, no LLM).
    "po_materials/config_actuator.py",
    # photo_screen's §34 Layer-3 lazily imports `pyclamd` (a needle below) to scan
    # uploaded portal photos against the LOCAL clamd daemon. config-gated OFF by default;
    # the scan is local-only AV, not customer egress. It stays in GATED_SCRIPTS (no send,
    # no LLM) — gating and the network allowlist are orthogonal (cf. publish_daemon).
    "safety_reports/photo_screen.py",
    # po_attach_screen (Feature B) — the §34 DOC-attachment sibling of photo_screen:
    # the SAME lazy, config-gated `pyclamd` L3 against the LOCAL clamd daemon
    # (po_materials.po_attach_screen.clamav_enabled, default OFF). Local-only AV,
    # not customer egress; stays in GATED_SCRIPTS (no send, no LLM).
    "po_materials/po_attach_screen.py",
    # WS2 operator dashboard (D1-1, read-only observability app) — the root is
    # walked (below) so a future dashboard module that quietly acquires network
    # capability is caught. These four legitimately import a tracked needle, all
    # benign + non-egress:
    #   daemons.py      — `subprocess` runs read-only `launchctl list` (fixed
    #                     argv, no shell, bounded timeout) to list daemon status.
    #   runtime_state.py / smartsheet_panels.py / watchdog_checks.py — `importlib`
    #                     lazily resolves INTERNAL modules only (shared.* /
    #                     watchdog) so a broken import degrades one panel; never a
    #                     dynamic import of a network lib. The app is read-only
    #                     (no customer send, no LLM) — not in GATED/SEND lists.
    "operator_dashboard/sources/daemons.py",
    "operator_dashboard/sources/runtime_state.py",
    "operator_dashboard/sources/smartsheet_panels.py",
    "operator_dashboard/sources/watchdog_checks.py",
    # WS2 D1-2 ACT surface: `importlib` lazily resolves INTERNAL modules only
    # (shared.smartsheet_client / shared.sheet_ids / shared.error_log) so a
    # broken import degrades the config editor rather than the app; never a
    # dynamic import of a network lib. It writes ONLY to ITS_Config (internal
    # SoR), never an external send — no anthropic/graph/resend import here.
    "operator_dashboard/act/config_write.py",
    # WS2 D1-3 Class-C secret rotation: `subprocess` runs `npx wrangler secret put`
    # (value on STDIN, never argv) to rotate a Worker bearer, cwd safety_portal/;
    # `importlib` lazily resolves shared.keychain / shared.error_log. Write-only —
    # it never reads a secret back and never logs a value. Not a customer send.
    "operator_dashboard/act/secret_rotate.py",
    # WS2 change-operator-PIN verb: `importlib` lazily resolves INTERNAL modules only
    # (shared.keychain / shared.error_log / operator_dashboard.auth) to WRITE a new PIN
    # via shared.keychain.set_secret. Write-only — never reads a PIN back (asserted by
    # tests/test_pin_change.py). No send, no AI, no subprocess, no network lib.
    "operator_dashboard/act/pin_change.py",
    # WS2 D1-3b interval-edit + Block-3 daemon-control verbs: `subprocess` runs
    # `scripts/launchd/install.sh load/unload <label> [<interval>]` and (kickstart)
    # `launchctl kickstart -k gui/<uid>/<label>`, all LABEL-ALLOWLISTED to
    # org.solutionsmith.its.* daemons, to re-install / start / stop / restart an ITS
    # daemon; `importlib` lazily resolves shared.smartsheet_client / shared.error_log.
    # It writes ONLY the ITS_Config poll_interval row + manages launchctl — no send, no AI.
    "operator_dashboard/act/daemon_ops.py",
    # WS2 Block 3 circuit-breaker clear: `importlib` lazily resolves INTERNAL modules
    # only (shared.circuit_breaker / shared.state_io / shared.error_log) to reset the
    # breaker state via the canonical state_io atomic writer. No send, no AI, no
    # subprocess, no network lib.
    "operator_dashboard/act/state_ops.py",
    # estimate_sandbox (ADR-0004 red-team #5): `subprocess` IS the control — it
    # re-invokes `python -m po_materials.estimate_sandbox <fn>` as a KILLABLE child
    # with an RLIMIT_AS memory cap (preexec_fn) + wall-clock timeout, so a hostile
    # vendor document that wedges/OOMs pdfplumber or the Quartz render is reaped
    # and the doc degrades instead of killing estimate_poll. Local process spawn
    # only — never network egress; stays in GATED_SCRIPTS (no send, no LLM).
    "po_materials/estimate_sandbox.py",
    # WS2 clear-error-log verb: `importlib` lazily resolves INTERNAL modules only
    # (shared.errors_rotation / shared.smartsheet_client / shared.sheet_ids /
    # shared.defaults / shared.error_log) to delete TERMINAL ITS_Errors rows (never an
    # open CRITICAL) via smartsheet_client.delete_rows. No send, no AI, no subprocess.
    "operator_dashboard/act/errors_ops.py",
    # DASH-12 restart-dashboard verb: `subprocess` spawns ONE detached
    # `/bin/sh -c 'sleep 1; exec launchctl kickstart -k gui/<uid>/<dashboard-label>'`
    # on the dashboard's OWN fixed label (restart-only — never a pull/deploy, never
    # another label; asserted by tests/test_dashboard_restart.py); `importlib` lazily
    # resolves shared.error_log for the pre-spawn audit. No send, no AI.
    "operator_dashboard/act/dashboard_ops.py",
    # DASH-13 review-queue resolve verb: `importlib` lazily resolves INTERNAL modules
    # only (shared.smartsheet_client / shared.sheet_ids / shared.defaults /
    # shared.error_log) to stamp PENDING ITS_Review_Queue rows terminal via
    # update_rows (filter-required; nothing deleted). No send, no AI, no subprocess.
    "operator_dashboard/act/review_ops.py",
    # System map (/system) live joins: `importlib` lazily resolves INTERNAL modules
    # only (operator_dashboard.sources.* / shared.errors_rotation / shared.heartbeat /
    # shared.smartsheet_client / shared.sheet_ids / troubleshooting.loader) so any
    # broken join degrades to an undecorated map. Read-only — no send, no AI, no
    # subprocess (launchctl state comes via the daemons panel source).
    "operator_dashboard/system_view.py",
    # shared/ollama_client.py (ADR-0004 E5): localhost-only Ollama inference — the
    # Tier-2 vendor-estimate extraction's ONLY egress. Imports `requests`, but
    # REFUSES any base_url whose host is not 127.0.0.1/localhost (OllamaClientError,
    # RED-tested in tests/test_ollama_client.py) — vendor pricing never leaves the
    # machine, and the client can never become a generic HTTP egress. Send scripts
    # must never import it (the send half is local-AI-free too, ADR-0004 dec. 12).
    "shared/ollama_client.py",
})

# Import needles that constitute network-egress or process-spawn capability.
# Matched on DOTTED-SEGMENT boundaries (not bare substring) — see
# `_import_matches_needle` — so `socket` does NOT collide with `socketserver`,
# and `http.client` does NOT collide with `http.server`. `urllib.request`
# (network) is gated but `urllib.parse` (pure string work) is not.
#
# F02 SDK blind-spot close (PR-5): the raw-HTTP needles above MISS egress-capable
# SDKs — a module could `import boxsdk` / `anthropic` / `smartsheet` / `msal` /
# `sentry_sdk` / `resend` and exfiltrate through a sanctioned channel without tripping
# F02. The SDKs are added as needles; their legitimate `shared/*_client.py` homes are
# allowlisted above. `importlib` is a needle too — a static `import importlib` is a
# dynamic-import escape hatch around this very static analysis (a bare `__import__`
# builtin call is a documented residual gap — see _imports_in + the M2 follow-up).
NETWORK_NEEDLES: frozenset[str] = frozenset({
    "requests",
    "httpx",
    "urllib.request",
    "urllib3",
    "socket",
    "subprocess",
    "http.client",
    # Egress-capable SDKs (F02 blind spot, PR-5) — each direct importer is allowlisted.
    "boxsdk",
    "anthropic",
    "smartsheet",
    "msal",
    "sentry_sdk",
    "resend",
    # ClamAV client (PR-2 §34 photo screening): talks to the clamd daemon over a
    # unix/network socket. Treated as egress-capable surface; its sole importer
    # (safety_reports/photo_screen.py) is allowlisted above.
    "pyclamd",
    # Dynamic-import escape hatch around static import analysis.
    "importlib",
})

# Source roots walked by the network allowlist. See the scope rationale above.
# progress_reports joined at P2 (the Progress Reporting workstream — its thin wpr_review
# module today, its *_generate/_send/_poll daemons at P4/P5, which the enrollment + F02
# checks above must then cover). field_ops joined at P2.5 Slice 5 (the job up-sync daemon
# fieldops_sync — its egress to OUR Worker rides shared.portal_client, so it imports no raw
# network library and trips no F02 needle, but the root is walked so a future field_ops module
# that quietly acquires network capability is caught). po_materials joined at S3 (the PO
# workstream — today only the pure terms/config loader, which imports nothing network-shaped;
# its gated po_poll/po_send daemons land at S4/S5 with same-PR enrollment above).
# operator_dashboard joined at WS2 D1-1 (the read-only observability app — its
# subprocess/importlib importers are allowlisted above; the root is walked so a
# later dashboard module, e.g. the D1-2 ACT surface, that acquires network
# capability is caught).
# subcontracts joined at SC-S3c (the subcontract workstream — its gated
# subcontract_generate/_docx renderers and the subcontract_poll daemon rely on
# portal_client / box_client for egress and import no raw network library, so they
# trip no F02 needle; the root is walked so a future subcontracts module that quietly
# acquires network capability is caught — the po_materials / field_ops precedent).
# troubleshooting + docs_pdf joined 2026-07-21 (coverage-gap audit): both are live
# first-party packages that had silently sat OUTSIDE this walk. Neither imports a needle
# today (troubleshooting/loader.py is a yaml tree loader; docs_pdf is a reportlab /
# markdown-it renderer), so enrolling them costs nothing now — but a guard that walks a
# hardcoded root list is only honest if the list tracks the packages that actually exist,
# and both are plausible future acquirers of network capability (a docs publisher that
# uploads, a troubleshooting loader that fetches).
#
# INVARIANT: this tuple is the SINGLE source of the walked runtime surface.
# tests/test_state_write_discipline.py IMPORTS it rather than re-declaring it, so the two
# guards cannot drift apart — they did drift: that file's private copy sat at 4 roots
# while its own comment claimed it was "kept in sync with the F02 WALKED_ROOTS".
WALKED_ROOTS: tuple[str, ...] = (
    "shared", "safety_reports", "progress_reports", "field_ops", "po_materials",
    "operator_dashboard", "subcontracts", "troubleshooting", "docs_pdf",
)


def _import_matches_needle(imported: str, needle: str) -> bool:
    """True iff `imported`'s leading dotted segments equal `needle`'s segments.

    Segment-boundary match, NOT substring:
      _import_matches_needle("socket", "socket")          -> True
      _import_matches_needle("socketserver", "socket")    -> False
      _import_matches_needle("requests.adapters", "requests") -> True
      _import_matches_needle("http.server", "http.client") -> False
      _import_matches_needle("urllib.request", "urllib.request") -> True
      _import_matches_needle("urllib.parse", "urllib.request")   -> False
    """
    imp_segments = imported.split(".")
    needle_segments = needle.split(".")
    return imp_segments[: len(needle_segments)] == needle_segments


def _network_needles_in(path: Path) -> list[str]:
    """Return the sorted network/subprocess needles a file directly imports."""
    imports = _imports_in(path)
    hits = {
        needle
        for imp in imports
        for needle in NETWORK_NEEDLES
        if _import_matches_needle(imp, needle)
    }
    return sorted(hits)


def test_no_unallowlisted_network_imports():
    """No module on the untrusted-content surface imports a network/subprocess
    library unless it is on NETWORK_LIB_ALLOWLIST (audit F02).

    This is the additive defensive inversion of the External Send Gate. It
    is orthogonal to GATED_SCRIPTS/SEND_SCRIPTS and must pass independently.
    """
    violations: list[tuple[str, list[str]]] = []
    for root in WALKED_ROOTS:
        root_dir = REPO_ROOT / root
        if not root_dir.is_dir():
            continue
        for path in sorted(root_dir.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            needles = _network_needles_in(path)
            if needles and rel not in NETWORK_LIB_ALLOWLIST:
                violations.append((rel, needles))

    assert not violations, (
        "Network/subprocess import outside the allowlist (audit F02):\n"
        + "\n".join(f"  {rel} imports {needles}" for rel, needles in violations)
        + "\n\nA module on the walked runtime surface (WALKED_ROOTS) acquired network or "
        "process-spawn capability. Either (a) remove the import and route the "
        "call through an audited shared/*_client.py, or (b) if the capability "
        "is genuinely legitimate, add the file to NETWORK_LIB_ALLOWLIST WITH a "
        "one-line rationale comment (see the allowlist block in this file)."
    )


def test_network_allowlist_has_no_stale_entries():
    """Every allowlisted file must still exist AND still import a needle.

    A stale entry (file deleted, or no longer imports a network/subprocess
    lib) is dead allowlist surface that rubber-stamps nothing — prune it so
    the allowlist stays an honest, scrutinized list.
    """
    for rel in sorted(NETWORK_LIB_ALLOWLIST):
        path = REPO_ROOT / rel
        assert path.exists(), (
            f"allowlisted file missing: {rel} — prune it from NETWORK_LIB_ALLOWLIST"
        )
        assert _network_needles_in(path), (
            f"allowlisted file {rel} imports no network/subprocess library — "
            "stale allowlist entry, prune it"
        )


# =========================================================================
# Enrollment meta-test — convert opt-in gating to opt-out-with-a-reason
# =========================================================================
#
# GATED_SCRIPTS / SEND_SCRIPTS above are HAND-MAINTAINED: the module docstring
# concedes "Adding new entries is the entire enforcement mechanism." So a NEW
# generation / send / daemon module that the author forgets to enroll is silently
# UN-gated — the capability-gate enrollment gap (forensic class #12; same shape as
# the #247 SENDING omission). This meta-test closes it for the documented naming
# convention (CLAUDE.md "Adding a new workstream"): every module named
# *_generate / *_send / *_poll on the workstream-runtime surface MUST be enrolled
# in GATED_SCRIPTS or SEND_SCRIPTS, or listed in ENROLLMENT_EXEMPT with a reason.
# A deliberately-MISnamed module still escapes (documented limit — the naming IS
# the convention), but the common "forgot to enroll the new daemon" case now fails
# at CI time rather than silently shipping an un-gated send path.

# Directories NOT on the workstream-runtime surface (operator-run / non-source).
_ENROLLMENT_SKIP_DIRS: frozenset[str] = frozenset({
    "tests", "scripts", "migrations", "smartsheet_migration", "box_migration",
    "docs", "prompts", "schemas", "node_modules", "build", "dist", "safety_portal",
})

# Suffixes that, by convention, denote a generation script, a send script, or an
# intake / compile / send polling daemon.
_ENROLLMENT_SUFFIXES: tuple[str, ...] = ("_generate.py", "_send.py", "_poll.py")
# Fast-follow: widen to include "_sync.py" so a future *_sync.py daemon auto-enrolls — it
# requires enrolling the pre-existing shared/picklist_sync.py in the same change (out of scope
# for the P2.5 Slice-5 PR; field_ops/fieldops_sync.py is already explicitly in GATED_SCRIPTS).

# Convention-matching modules that are deliberately NOT gen/send/daemon code.
# Each entry carries its reason. (Empty today — every current match is enrolled.)
ENROLLMENT_EXEMPT: dict[str, str] = {}


def _enrolled_paths() -> set[str]:
    return {rel for rel, _ in GATED_SCRIPTS} | {rel for rel, _ in SEND_SCRIPTS}


def _convention_named_modules() -> list[str]:
    out: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if any(part.startswith(".") or part in _ENROLLMENT_SKIP_DIRS for part in rel.parts):
            continue
        if rel.name.endswith(_ENROLLMENT_SUFFIXES):
            out.append(rel.as_posix())
    return out


def test_every_convention_named_script_is_enrolled_or_exempt():
    """Every *_generate / *_send / *_poll module on the workstream surface is
    enrolled in the External Send Gate (GATED_SCRIPTS / SEND_SCRIPTS) or explicitly
    exempt with a reason — closes the opt-in enrollment gap (Invariant 1, class #12)."""
    enrolled = _enrolled_paths()
    unenrolled = [
        rel for rel in _convention_named_modules()
        if rel not in enrolled and rel not in ENROLLMENT_EXEMPT
    ]
    assert not unenrolled, (
        "Convention-named generation/send/daemon module(s) NOT enrolled in the "
        "External Send Gate:\n" + "\n".join(f"  {r}" for r in unenrolled)
        + "\n\nAdd each to GATED_SCRIPTS (generation: must not import send capability) "
        "or SEND_SCRIPTS (send: must not import AI), per Invariant 1. If a match is "
        "genuinely neither, add it to ENROLLMENT_EXEMPT with a one-line reason."
    )


def test_enrollment_exempt_entries_still_exist():
    """No stale ENROLLMENT_EXEMPT entry (file deleted) — keep the exempt list honest."""
    for rel in sorted(ENROLLMENT_EXEMPT):
        assert (REPO_ROOT / rel).exists(), (
            f"ENROLLMENT_EXEMPT names a missing file: {rel} — prune it."
        )
