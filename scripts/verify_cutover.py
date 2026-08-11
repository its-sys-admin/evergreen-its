"""verify_cutover — the §53 mechanical production-cutover gate (read-only).

Op Stds v20 §53 (sandbox-masks-production): a cutover claim is narrative until
it is mechanically verified. This script IS that verification — the Aug-3
tenant cutover (and the Aug-7 on-site re-run) is **not done** until it exits 0.
Companion docs:

- ``docs/operations/cutover_checklist.md`` (v2) — every checklist item that a
  machine can check cross-references a ``VC-NN`` id below.
- ``docs/operations/host_migration_runbook.md`` — Phase B runs a subset via
  ``--only`` before the tenant cutover exists.
- ``docs/operations/production_rollback.md`` — re-run after any rollback leg.

Design contract:

- **Read-only.** Keychain reads, ``launchctl list``, Smartsheet sheet reads,
  ``git status``/``rev-parse``/``ls-remote``, ``wrangler d1 migrations list``.
  No writes, no sends, no AI — this script must never need enrollment in the
  send/generation lists of ``tests/test_capability_gating.py``.
- **No ``@require_active``.** The cutover runs under ``system.state=MAINTENANCE``
  (and a rollback may run under PAUSED); the gate must execute in both.
- **Never silent.** Every check prints ``[PASS]``/``[FAIL] VC-NN slug`` with a
  one-line summary; failures carry details. A partial run (``--only``/``--skip``)
  prints a loud PARTIAL-RUN banner — a partial run is NOT a cutover verdict.
- **Secrets never printed.** The keychain check reports presence + length only
  (§54 discipline).

Checks (each independently selectable via ``--only`` / ``--skip``):

====== =============== ==============================================================
id     slug            what it proves
====== =============== ==============================================================
VC-01  keychain        all required Keychain secrets present (18: 11 non-Box + Box
                       triplet + ``ITS_PORTAL_PO_TOKEN`` + the config-actuator /
                       subcontract-poll daemon bearers + the operator-dashboard PIN)
VC-02  launchd         every shipped ``org.solutionsmith.its.*`` plist loaded EXCEPT any
                       label in ``DARK_UNLOADED_LABELS``, which must NOT be loaded (no
                       missing, no orphans, no dark send daemon running). That set is
                       EMPTY as of 2026-08-10 — every send lane has been activated; see
                       the constant for the rationale
VC-03  config          load-bearing ITS_Config rows present + non-default
                       (worker_base_url, from_mailbox rows, scheduled_send_local,
                       the polling/sync/intake gates, ``system.operator_email``,
                       the subcontract-poll gate rows) and — unless
                       ``--allow-sandbox`` or a named ``--profile`` exemption —
                       free of the mirror domain
VC-04  daemon-health   every Enabled ITS_Daemon_Health row has a heartbeat fresher
                       than 2 x its Interval Seconds (the schema's documented
                       staleness threshold)
VC-05  review-queue    ITS_Review_Queue reachable (read of pending rows succeeds)
VC-06  alerting        ITS_SENTRY_DSN + ITS_RESEND_API_KEY present and shape-valid
VC-07  git             repo on ``main``, working tree clean, HEAD == origin/main
VC-08  d1-migrations   ``wrangler d1 migrations list <db> --remote`` reports none
                       pending (one retry on transient Cloudflare 7403)
VC-09  heartbeat-url   ``system.heartbeat_url`` (UptimeRobot) configured, https
VC-10  approver-shares every manifest workspace carries its production approver
                       USER shares (manifest ⊆ live), zero mirror-domain share
                       residue, and no GROUP shares posing as F22 authority
====== =============== ==============================================================

PO enrollment note (WS1): ``po_send`` has LANDED (PR #500, ships dark via a seeded
``po_materials.po_send.polling_enabled=false`` row), so its production-address surface is
now enrolled below — ``po_materials.po_send.from_mailbox`` (VC-03 sandbox-scanned) plus the
two previously-unscanned ``worker_base_url`` copies (the ``progress_reports`` + ``po_materials``
Workstream rows of ``safety_reports.portal.worker_base_url``), closing the mechanical gap the
manual CL-14 grep used to backstop. The keychain check already requires the
``ITS_PORTAL_PO_TOKEN`` bearer. **UPDATE 2026-08-11:** ``po_send.polling_enabled`` IS now
enrolled — as ``non_empty``, never ``"true"``. The original deferral reasoned about enrolling it
as ``"true"``, which would indeed DEMAND PO send be live at cutover; ``non_empty`` asserts only
that the row EXISTS and leaves the value the operator's, so the External-Send-Gate concern does
not apply. Its structural twins ``subcontracts.subcontract_send.polling_enabled`` and
``po_materials.rfq_send.polling_enabled`` were already enrolled that way, so its absence was an
oversight rather than policy — surfaced by the boolean-gate parity test.
``scheduled_send_local`` remains unenrolled.

Dark-daemon-bearer + dashboard-PIN enrollment (WS2 / config editor / subcontracts,
operator directive 2026-07-12): three more Keychain secrets are now cutover-required
even though their consumers ship dark — same "provision-even-while-dark" rationale as
``ITS_PORTAL_PO_TOKEN``. ``ITS_PORTAL_CONFIG_TOKEN`` (the §50 ``config_actuator`` daemon
bearer) and ``ITS_PORTAL_SUB_TOKEN`` (the ``subcontract_poll`` daemon bearer) both back
LOADED-but-runtime-gated daemons, so their tokens must be present at cutover for the
activation cell-flip to work. ``ITS_OPERATOR_PIN`` gates the operator dashboard
(``operator_dashboard/auth.py``; launchd-managed, ``org.solutionsmith.its.dashboard``);
it is enrolled so the operator ACT surface is usable at cutover. VC-03 additionally now scans
``system.operator_email`` (the last-resort Resend page recipient, must be off the mirror
domain — CO-3) and asserts the three ``subcontracts.subcontract_poll.*`` gate rows are
seeded present (``non_empty``, NOT forced ``true`` — the dark-ship reflex: a missing gate
row leaves the operator no switch to flip). DEFERRED (NOT enrolled) until the SC-S4
subcontract SEND half is built: the subcontract ``from_mailbox`` / ``scheduled_send_local`` /
send ``polling_enabled`` rows — those daemons + rows do not exist yet.

Daemon-gate seed enrollment (2026-07-13 config-WARN-storm incident): VC-03 also asserts
the three previously-unenrolled rows from ``seed_daemon_gate_config.py`` are seeded
present — ``safety_reports.photo_screen.clamav_enabled`` and both
``<workstream>.compile_now_poll.polling_enabled`` copies (``non_empty``, NOT forced
``true`` — clamav is a dark security gate and the compile-now passes are operator
choices). Their ABSENCE caused the per-cycle ``config_row_missing`` WARN storm that
filled ITS_Errors to the 20k cap. The seed set's two ``progress_send`` rows were already
enrolled.

DEFERRED (deliberately NOT enrolled in VC-03) — ``smartsheet.retry.enabled`` /
``.max_extra_attempts`` / ``.backoff_seconds`` (2026-07-21, the bounded transient retry in
``shared/smartsheet_client.py``). They follow the ``circuit_breaker.*`` precedent EXACTLY,
and that precedent is a three-part shape — be precise about which part is which, because
an earlier draft of this note read the omission as "circuit_breaker is absent everywhere",
which is false:

  * SEEDED by ``scripts/seed_its_config.py``            — yes, both families.
  * REGISTERED in the dashboard's editable registry     — yes, both families.
  * ENROLLED in VC-03 here                              — no, neither family.

The third is the deliberate one. VC-03 asserts a row that MUST exist for cutover to be
sane; a shared-infrastructure tunable whose ``shared/defaults.py`` values ARE the intended
production behaviour does not qualify. Its read is resolved under
``circuit_breaker.bypass()``, ``_read_global_setting`` swallows a 404 by contract (so an
absent row causes no per-cycle WARN storm), and the resolved source rides every recovery
log line. Enrolling it as ``non_empty`` would turn a benign absence into a RED cutover
verdict — the opposite of what VC-03 is for. The rows ARE seeded so the operator has
something to edit; that is the phantom-switch fix, not a cutover assertion.

VC-10 approver-shares (CL-11 mechanization + CL-37's subcontracts leg): F22 approval
authority IS workspace USER-share membership (GROUP shares do NOT count — a group-only
share yields an EMPTY authorized set that silently fail-closes every send), so the gate
diffs each workspace named in ``scripts/migrations/production_shares_manifest.json``
against its LIVE share list via ``smartsheet_client.list_workspace_shares``. SUBSET
semantics, deliberately NOT set equality: the manifest approver set must be ⊆ the live
USER-share set and the mirror domain must show ZERO residue — the operator's own
account, its@, or any additional production share Seth grants may legitimately hold
shares beyond the manifest, and pinning equality would turn every such grant into a RED
cutover verdict. A GROUP share on a checked workspace FAILS the check: it carries no
F22 authority, and its presence invites the false belief that group members can
approve sends. ``--allow-sandbox`` waives the whole check (mirror dress rehearsals run
mirror-account shares by design); a ``--profile`` does NOT exempt VC-10 — a
profile-gated phase cutover still requires the production approver shares. The applier
half is ``scripts/migrations/seed_production_shares.py`` (guarded, plan-default,
ADD-only).

Usage::

    python -m scripts.verify_cutover                 # full gate (the cutover verdict)
    python -m scripts.verify_cutover --list          # enumerate checks
    python -m scripts.verify_cutover --only keychain,launchd
    python -m scripts.verify_cutover --skip d1-migrations
    python -m scripts.verify_cutover --allow-sandbox # mirror dress-rehearsal mode
    python -m scripts.verify_cutover --profile phase1-hybrid  # phase gate (see PROFILES)

Profiles vs ``--allow-sandbox``: a profile exempts a NAMED, phase-specific row set
from the VC-03 sandbox scan while everything else keeps the full production scan —
a profile run IS the cutover verdict for that phase. ``--allow-sandbox`` waives the
scan wholesale and is never a shippable verdict (§52/§53 narration); the two flags
are mutually exclusive so a profile gate can't be silently degraded.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from shared import keychain, review_queue, sheet_ids, smartsheet_client
from shared.smartsheet_client import SmartsheetNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---- constants -----------------------------------------------------------

LAUNCHD_PLIST_DIR = REPO_ROOT / "scripts" / "launchd"
LABEL_PREFIX = "org.solutionsmith.its."

# Dark-unloaded SEND daemons: their plist ships in scripts/launchd/ but they stay
# launchd-UNLOADED at cutover so a dark external-send path is not even running
# (send-gate defense-in-depth; operator decision 2026-07-12). VC-02 therefore does NOT
# require them loaded, and FAILS if one IS loaded — a send daemon live at cutover is a
# FIXED high-class External-Send-Gate event (Seth).
# First-enabling a send path = remove its label here + load its plist.
#
# CURRENTLY EMPTY, and that is a STATEMENT, not an oversight. Every send lane has now
# been deliberately activated by the operator — po-send, rfq-send and subcontract-send
# all read `polling_enabled = true` in ITS_Config AND are loaded in launchd. po-send and
# rfq-send sat in this set until 2026-08-10 while both halves of their activation had
# already happened, so VC-02 reported a "send-gate violation" describing a state the
# operator had chosen months earlier. A check that cries wolf about a deliberate decision
# is how a check stops being read — the same failure that let the Track 6 archive sit
# inert behind an unrun VC-03.
#
# The MECHANISM is retained in full: the moment a send daemon is meant to be dark again
# (a new lane shipping pre-go-live, or one deliberately re-darkened), add its label back
# here and VC-02 fails if it is loaded. Emptying the set narrows what VC-02 asserts today;
# it does not remove the assertion. The External Send Gate itself is unaffected — that is
# Invariant 1's two-process split plus human approval, enforced by
# tests/test_capability_gating.py, not by this frozenset.
DARK_UNLOADED_LABELS: frozenset[str] = frozenset()

# The mirror-tenant marker. Any load-bearing config value still containing this
# after cutover means a daemon is pointed at the sandbox (§53 sandbox-masks-
# production). `--allow-sandbox` relaxes this for mirror dress rehearsals.
SANDBOX_DOMAIN_MARKER = "evergreenmirror"

# The CL-11/CL-37 production approver-share manifest (data, operator-reviewed).
# VC-10 diffs each workspace it names against the live share list; the applier is
# scripts/migrations/seed_production_shares.py (guarded, plan-default, ADD-only).
APPROVER_SHARES_MANIFEST = (
    REPO_ROOT / "scripts" / "migrations" / "production_shares_manifest.json"
)

# Cutover PROFILES — each names a phase-specific set of (key, workstream) rows whose
# VC-03 sandbox scan is EXEMPTED. Deliberately data, not a code path: if a cutover
# leg slips (e.g. the M365 E5 ask lands late and the two from_mailbox rows must
# temporarily stay mirror), the exemption is a reviewed two-line addition here, never
# an --allow-sandbox run. Rows NOT listed keep the full production scan, and the
# exempted rows are still presence/non_empty-checked — only the domain scan is waived.
#
# phase1-hybrid (Evergreen Phase-1, week of 2026-07-21): Smartsheet + Box + M365 move
# to the production tenants while the portal deliberately STAYS on the mirror Worker
# (safety.evergreenmirror.com — the checklist's "portal doesn't move"), so exactly the
# three physical worker_base_url rows (one Setting name under three Workstream cells)
# are EXPECTED to carry the mirror domain at the Friday gate.
PROFILES: dict[str, frozenset[tuple[str, str]]] = {
    "phase1-hybrid": frozenset({
        ("safety_reports.portal.worker_base_url", "safety_reports"),
        ("safety_reports.portal.worker_base_url", "progress_reports"),
        ("safety_reports.portal.worker_base_url", "po_materials"),
    }),
}

# safety_portal/wrangler.jsonc `database_name` — wrangler keys `--local`/`--remote`
# migration state off the name, not the id.
D1_DATABASE_NAME = "its-safety-portal-db"
# Transient Cloudflare API error code observed on `d1 migrations list --remote`;
# tolerated with exactly ONE retry (a second occurrence is a real outage → FAIL).
D1_TRANSIENT_ERROR_MARKER = "7403"
WRANGLER_TIMEOUT_SECONDS = 180

# ITS_Daemon_Health staleness threshold multiplier — the schema's documented
# design intent ("feeds the stale-heartbeat threshold (2 x Interval Seconds)",
# blueprint references/daemon-health-schema.md).
DAEMON_HEALTH_STALE_MULTIPLIER = 2.0

# Keychain service names, verified against live HEAD (shared/*, safety_reports/*,
# progress_reports/*, field_ops/fieldops_sync.py, po_materials/*, subcontracts/*,
# operator_dashboard/auth.py, safety_portal/worker/*.ts). Presence-only check —
# values are NEVER printed (§54).
NON_BOX_SECRETS: tuple[str, ...] = (
    "ITS_SMARTSHEET_TOKEN",
    "ITS_ANTHROPIC_KEY",
    "ITS_RESEND_API_KEY",
    "ITS_SENTRY_DSN",
    "ITS_MS_TENANT_ID",
    "ITS_MS_CLIENT_ID",
    "ITS_MS_CLIENT_SECRET",
    "ITS_PORTAL_INTERNAL_TOKEN",
    "ITS_PORTAL_HMAC_SECRET",
    "ITS_PORTAL_ADMIN_TOKEN",
    "ITS_PORTAL_FIELDOPS_TOKEN",
)
# Box triplet: single-consumer refresh-token rotation — seeded ONLY in host-
# migration Phase B, on exactly one host (docs/operations/host_migration_runbook.md).
BOX_SECRETS: tuple[str, ...] = (
    "ITS_BOX_CLIENT_ID",
    "ITS_BOX_CLIENT_SECRET",
    "ITS_BOX_REFRESH_TOKEN",
)
# PO internal-tier bearer (Worker `requirePoToken`, safety_portal/worker/po.ts).
# Provisioned with WS1 S2; REQUIRED at cutover. Pre-provisioning runs of the
# keychain check will FAIL naming it — that is correct, not noise.
PO_SECRETS: tuple[str, ...] = ("ITS_PORTAL_PO_TOKEN",)
# Dark-but-loaded daemon bearers (config-actuator §50 + subcontract-poll + the
# estimate lane). Same provision-even-while-dark rationale as ITS_PORTAL_PO_TOKEN:
# the daemons are LOADED and runtime-gated false, so their bearer tokens must be
# present at cutover for the activation cell-flip to work. ITS_PORTAL_CONFIG_TOKEN =
# po_materials/config_actuator.py KC_BEARER; ITS_PORTAL_SUB_TOKEN =
# subcontracts/subcontract_poll.py KC_SUB_TOKEN; ITS_PORTAL_ESTIMATE_TOKEN =
# po_materials/estimate_poll.py (ADR-0004 red-team #1 — the estimate lane's OWN
# bearer, scoping only /api/po/estimates/internal/*, held by the highest-exposure
# process and deliberately separate from the RFQ token); ITS_PORTAL_RFQ_TOKEN =
# po_materials/rfq_poll.py KC_RFQ_TOKEN (ADR-0004 decision 4 — the RFQ lane's OWN
# bearer, scoping only /api/po/rfqs/internal/*; a compromised extraction daemon
# must never reach the RFQ send-lane control surface).
DARK_BEARER_SECRETS: tuple[str, ...] = (
    "ITS_PORTAL_CONFIG_TOKEN",
    "ITS_PORTAL_SUB_TOKEN",
    "ITS_PORTAL_ESTIMATE_TOKEN",
    # PR3b: the manifest importer's OWN bearer — same privilege-separation reason
    # as the estimate token (it decodes hostile PDF/xlsx bytes).
    "ITS_PORTAL_MANIFEST_TOKEN",
    "ITS_PORTAL_RFQ_TOKEN",
)
# Operator-dashboard PIN (operator_dashboard/auth.py PIN_KEYCHAIN_KEY). The dashboard
# ships dark + manual-start (no launchd plist), but the PIN is REQUIRED at cutover so the
# operator ACT surface is usable (operator directive 2026-07-12). Not a Worker bearer.
OPERATOR_SECRETS: tuple[str, ...] = ("ITS_OPERATOR_PIN",)

REQUIRED_SECRETS: tuple[str, ...] = (
    NON_BOX_SECRETS + BOX_SECRETS + PO_SECRETS + DARK_BEARER_SECRETS + OPERATOR_SECRETS
)


@dataclass(frozen=True)
class ConfigRow:
    """One load-bearing ITS_Config row VC-03 asserts.

    requirement:
        ``non_empty`` — row exists and Value is a non-blank string.
        ``true``      — row exists and Value is the string ``true`` (the
                        boolean-gate convention; a MISSING row reads as false
                        in daemon code, so presence is part of the check).
    sandbox_scan:
        when True, the value must not contain ``SANDBOX_DOMAIN_MARKER``
        (skipped under ``--allow-sandbox``).
    """

    key: str
    workstream: str
    requirement: str
    sandbox_scan: bool = False


# Verified against live HEAD constants (each daemon's CFG_* names). NOTE the
# documented footgun: `progress_reports.intake_enabled` is read under
# Workstream=safety_reports (intake's own workstream), not progress_reports.
CONFIG_ROWS: tuple[ConfigRow, ...] = (
    ConfigRow(
        "safety_reports.portal.worker_base_url", "safety_reports", "non_empty",
        sandbox_scan=True,
    ),
    ConfigRow(
        "safety_reports.weekly_send.from_mailbox", "safety_reports", "non_empty",
        sandbox_scan=True,
    ),
    ConfigRow(
        "progress_reports.progress_send.from_mailbox", "progress_reports", "non_empty",
        sandbox_scan=True,
    ),
    # The two previously-unscanned worker_base_url copies. `safety_reports.portal.worker_base_url`
    # is ONE Setting name read under THREE Workstream cells = 3 physical ITS_Config rows
    # (registry.py) — the safety_reports copy is scanned above; these are the progress_reports copy
    # (progress_weekly_generate.py) + the po_materials copy (config_actuator.py). All three MUST be
    # the production custom domain at cutover; enrolling them replaces the manual CL-14 grep backstop
    # with a mechanical sandbox scan.
    ConfigRow(
        "safety_reports.portal.worker_base_url", "progress_reports", "non_empty",
        sandbox_scan=True,
    ),
    ConfigRow(
        "safety_reports.portal.worker_base_url", "po_materials", "non_empty",
        sandbox_scan=True,
    ),
    # po_send LANDED (PR #500, dark). Its FROM address must be production regardless of whether
    # sending is enabled at cutover — enroll it (sandbox-scanned) so a mirror procurement@ residue
    # is caught. NOT enrolling po_send.polling_enabled / scheduled_send_local: demanding
    # polling_enabled="true" would force a send-enable (a high-class External-Send-Gate decision —
    # Seth); add them only once PO send is confirmed in the Aug-7 send scope. (docstring PO note.)
    ConfigRow(
        "po_materials.po_send.from_mailbox", "po_materials", "non_empty",
        sandbox_scan=True,
    ),
    # po_send's GATE row, enrolled 2026-08-11 by the Check-Y gate-parity test
    # (`tests/test_verify_cutover.py::test_every_declared_boolean_gate_is_enrolled`). It was the
    # ONLY declared boolean gate in the fleet with no CONFIG_ROWS entry — its exact structural
    # twins `subcontracts.subcontract_send.polling_enabled` and
    # `po_materials.rfq_send.polling_enabled` were both already enrolled `non_empty`, so its
    # absence was an oversight, not a policy.
    #
    # This does NOT contradict the "NOT enrolling po_send.polling_enabled" note above: that note
    # objects to demanding `"true"`, which would force a send-enable — a FIXED high-capability-class
    # External-Send-Gate decision (Seth). `non_empty` asserts only that the ROW EXISTS, so the
    # operator has a switch to flip; the VALUE stays entirely the operator's. Same dark-ship reflex
    # every other send gate here already rides. (`scheduled_send_local` is a str tuning row, not a
    # gate, so the mechanical bool-typed bound leaves it out.)
    ConfigRow("po_materials.po_send.polling_enabled", "po_materials", "non_empty"),
    # The two Box mirror-tree ROOTS — supersedes CO-3's "stay intentionally unenrolled".
    # CO-3 was right that sandbox_scan is N/A here (a Box folder id is a bare numeric
    # string; there is no `evergreenmirror` marker in it to scan, so a scan would assert
    # nothing), but it conflated that with PRESENCE — which is now both assertable and
    # worth asserting, because this PR adds scripts/migrations/build_box_roots.py: the
    # builder is create-only and writes no config row, so its entire output is these two
    # ids and its last cutover step is a MANUAL operator paste into ITS_Config. A skipped
    # or fat-fingered paste was previously caught by nothing, and an unset root silently
    # degrades every filing path (the safety_reports copy is read by intake, portal_poll,
    # weekly_generate, po_poll, rfq_poll, estimate_poll and subcontract_poll). `non_empty`,
    # never a value assertion — the id is tenant-specific and not ours to pin.
    ConfigRow("safety_reports.box.portal_root_folder_id", "safety_reports", "non_empty"),
    ConfigRow("progress_reports.box.portal_root_folder_id", "progress_reports", "non_empty"),
    # The Box ARCHIVE root (Track 6), same reasoning as the two above and the same
    # `non_empty`-never-a-value rule. Enrolled even though archiving ships dark: an unset
    # root is not inert here, it is a per-container FAILURE on every archive attempt
    # (job_archive._read_box_root refuses to read an unreadable tree as an empty one), so a
    # missed paste surfaces to the operator as repeated 4-of-6 `partial` archives rather
    # than as a quiet no-op. build_box_roots.py prints the id; standup.py seeds the row.
    ConfigRow("field_ops.box.archive_root_folder_id", "field_ops", "non_empty"),
    # Feature B (PO document attachments): the §34 screener's ClamAV gate must be
    # SEEDED PRESENT (non_empty, NOT forced 'true' — dark-ship reflex: it ships false
    # and stays false until clamd + pyclamd exist on the Mac; the deterministic L1/L2
    # layers run regardless). seed_po_materials_config.py seeds it.
    ConfigRow("po_materials.po_attach_screen.clamav_enabled", "po_materials", "non_empty"),
    ConfigRow("safety_reports.weekly_send.scheduled_send_local", "safety_reports", "non_empty"),
    ConfigRow("progress_reports.progress_send.scheduled_send_local", "progress_reports", "non_empty"),
    ConfigRow("safety_reports.portal_poll.polling_enabled", "safety_reports", "true"),
    ConfigRow("safety_reports.weekly_send.polling_enabled", "safety_reports", "true"),
    ConfigRow("progress_reports.progress_send.polling_enabled", "progress_reports", "true"),
    ConfigRow("progress_reports.intake_enabled", "safety_reports", "true"),
    ConfigRow("field_ops.fieldops_sync.sync_enabled", "field_ops", "true"),
    # The five per-stream sub-gates the master gate fans out to. Only the master was
    # enrolled, so a cutover could pass VC-03 with every field-capture stream silently
    # unseeded. `non_empty`, never forced 'true': materials_enabled carries a §51 rider
    # precondition in its Description and is not ours to assert ON.
    ConfigRow("field_ops.fieldops_sync.hours_enabled", "field_ops", "non_empty"),
    ConfigRow("field_ops.fieldops_sync.equipment_enabled", "field_ops", "non_empty"),
    ConfigRow("field_ops.fieldops_sync.materials_enabled", "field_ops", "non_empty"),
    ConfigRow("field_ops.fieldops_sync.incidents_enabled", "field_ops", "non_empty"),
    ConfigRow("field_ops.fieldops_sync.receipts_enabled", "field_ops", "non_empty"),
    # Track 6 archive pass. `non_empty`, never forced true — the dark-ship reflex: the row must
    # EXIST so activation is a visible cell-flip rather than a switch the operator cannot find,
    # but demanding "true" would force archiving on at cutover, which is an operator decision.
    ConfigRow("field_ops.fieldops_sync.archive_enabled", "field_ops", "non_empty"),
    # The §50 config actuator and the form-publish daemon: both LOADED, both gate-ON,
    # and neither runtime gate was enrolled — the privileged code-actuation rail could
    # arrive at a new host with no switch present. `non_empty` (dark-ship reflex).
    ConfigRow("po_materials.config_actuator.polling_enabled", "po_materials", "non_empty"),
    ConfigRow("safety_reports.publish_daemon.polling_enabled", "safety_reports", "non_empty"),
    # system.operator_email (CO-3): the last-resort Resend page recipient
    # (shared/resend_client.py) resolved when ITS_Config can't be read. Must be a
    # production address at cutover, so sandbox-scanned — a mirror residue
    # (seths@evergreenmirror.com) fails the gate. Global workstream. Closes the CL-12
    # manual-grep backstop with a mechanical scan.
    ConfigRow("system.operator_email", "global", "non_empty", sandbox_scan=True),
    # Subcontracts (operator scoped fully-in incl. send, 2026-07-12). subcontract_poll
    # reuses the safety_reports.portal.worker_base_url row (scanned above), so no new
    # worker_base_url row here. Assert the three subcontract_poll gate rows are SEEDED
    # PRESENT (non_empty, NOT forced 'true' — dark-ship reflex: a missing gate row leaves
    # no switch to flip; seed_subcontracts_config.py must have run). The gates ship false;
    # activation is a later operator cell-flip once the SC-S3c live smoke passes.
    # The po_poll gate trio — the exact structural mirror of the subcontract trio below,
    # and unlike it currently LIVE. It was never enrolled, so the PO generation lane
    # could arrive at a new host unseeded while the (dark) subcontract lane was checked.
    ConfigRow("po_materials.po_poll.polling_enabled", "po_materials", "non_empty"),
    ConfigRow("po_materials.po_poll.vendors_sync_enabled", "po_materials", "non_empty"),
    ConfigRow("po_materials.po_poll.status_sync_enabled", "po_materials", "non_empty"),
    ConfigRow("subcontracts.subcontract_poll.polling_enabled", "subcontracts", "non_empty"),
    ConfigRow("subcontracts.subcontract_poll.subcontractors_sync_enabled", "subcontracts", "non_empty"),
    ConfigRow("subcontracts.subcontract_poll.status_sync_enabled", "subcontracts", "non_empty"),
    # 2026-07-13 config-WARN-storm seeds (scripts/migrations/seed_daemon_gate_config.py):
    # the ABSENCE of these rows made daemons WARN config_row_missing per-cycle and filled
    # ITS_Errors to the 20k cap (the Check O storm-mode incident). Assert them SEEDED
    # PRESENT (non_empty, NOT forced 'true' — same dark-ship reflex as the subcontract
    # gates above): clamav_enabled is a dark security gate (stays 'false' until ClamAV is
    # installed on the Mac) and the compile_now_poll passes are operator-toggleable, so
    # demanding 'true' would pin an operator choice. The two progress_send rows from the
    # same seed set were already enrolled above.
    ConfigRow("safety_reports.photo_screen.clamav_enabled", "safety_reports", "non_empty"),
    ConfigRow("safety_reports.compile_now_poll.polling_enabled", "safety_reports", "non_empty"),
    ConfigRow("progress_reports.compile_now_poll.polling_enabled", "progress_reports", "non_empty"),
    # Subcontract SEND half (SC-S4, built 2026-07-15). The from_mailbox is production-address
    # surface (VC-03 sandbox-scanned — it holds the evergreenmirror.com mirror value, flagged
    # to repoint at cutover), enrolled exactly like po_send.from_mailbox. The send gate +
    # scheduled window are asserted SEEDED PRESENT (non_empty, NOT forced 'true' — the
    # dark-ship reflex: seed_subcontracts_send_config.py must have run so there is a switch to
    # flip). polling_enabled is deliberately NOT forced 'true': turning the subcontract send
    # gate on is a FIXED high-capability-class External-Send-Gate decision (Seth), same posture
    # as po_send's polling_enabled.
    ConfigRow(
        "subcontracts.subcontract_send.from_mailbox", "subcontracts", "non_empty",
        sandbox_scan=True,
    ),
    ConfigRow("subcontracts.subcontract_send.polling_enabled", "subcontracts", "non_empty"),
    ConfigRow("subcontracts.subcontract_send.scheduled_send_local", "subcontracts", "non_empty"),
    # Vendor-estimate importer (ADR-0004 Lane 1, PR-A). All three estimate_poll rows are
    # asserted SEEDED PRESENT (non_empty, NEVER forced 'true' — the dark-ship reflex: the
    # lane ships dark; polling_enabled seeds 'false' so activation is a visible operator
    # cell-flip, not a phantom hunt). poll_interval_seconds (default 120) + max_pages_preview
    # (default 12) are seeded alongside so the daemon's observable-config resolution reads
    # ITS_Config, not a silent hardcoded fallback.
    ConfigRow("po_materials.estimate_poll.polling_enabled", "po_materials", "non_empty"),
    ConfigRow("po_materials.estimate_poll.poll_interval_seconds", "po_materials", "non_empty"),
    # PR3b materials-manifest importer. `non_empty`, NEVER forced true — the lane
    # ships dark and activation is a §44 capability action, not a cutover checkbox.
    ConfigRow("field_ops.manifest_poll.polling_enabled", "field_ops", "non_empty"),
    ConfigRow("field_ops.manifest_poll.poll_interval_seconds", "field_ops", "non_empty"),
    ConfigRow("po_materials.estimate_poll.max_pages_preview", "po_materials", "non_empty"),
    # Extraction-ladder tier gates (ADR-0004 E4-E6, PR-B). Asserted SEEDED PRESENT
    # (non_empty, NEVER forced 'true' — the dark-ship reflex: all three seed 'false'
    # via seed_estimates_config.py so activation is a visible operator cell-flip
    # gated on the offline corpus eval, never a phantom hunt).
    ConfigRow("po_materials.estimate_extract.tier1_enabled", "po_materials", "non_empty"),
    ConfigRow("po_materials.estimate_extract.tier2_enabled", "po_materials", "non_empty"),
    ConfigRow("po_materials.estimate_extract.ocr_enabled", "po_materials", "non_empty"),
    # The VENDOR-SPREADSHEET twin of tier1_enabled (PR-B), missed when that PR enrolled
    # its three siblings. It is a real gate — `estimate_poll` declares it in
    # REQUIRED_CONFIG and branches on it — so its absence would silently return every
    # vendor .xlsx quote to hand-keying with no cutover assertion to catch it. Enrolled
    # `non_empty` like its siblings: presence is asserted, the value stays the operator's.
    ConfigRow("po_materials.estimate_extract.tier1_xlsx_enabled", "po_materials", "non_empty"),
    # Outbound-RFQ generation daemon (ADR-0004 Lane 2, R2). Both rfq_poll rows are
    # `non_empty` (never forced true — the gate ships dark; the row's PRESENCE is
    # what VC-03 asserts, the dark-gate reflex).
    ConfigRow("po_materials.rfq_poll.polling_enabled", "po_materials", "non_empty"),
    ConfigRow("po_materials.rfq_poll.poll_interval_seconds", "po_materials", "non_empty"),
    # Outbound-RFQ SEND daemon (ADR-0004 R3, built dark). The from_mailbox is production-
    # address surface (VC-03 sandbox-scanned — it holds the evergreenmirror.com mirror value,
    # flagged to repoint at cutover), enrolled exactly like po_send / subcontract_send. The
    # send gate + scheduled window are asserted SEEDED PRESENT (non_empty, NOT forced 'true'
    # — the dark-ship reflex: seed_rfq_send_config.py must have run so there is a switch to
    # flip). polling_enabled is deliberately NOT forced 'true': flipping the RFQ send gate on
    # is a FIXED high-capability-class External-Send-Gate decision (Seth), same posture as
    # po_send's / subcontract_send's polling_enabled. (The bearer ITS_PORTAL_RFQ_TOKEN is
    # already in DARK_BEARER_SECRETS from R2 — the send poller reuses it; no new secret.)
    ConfigRow(
        "po_materials.rfq_send.from_mailbox", "po_materials", "non_empty",
        sandbox_scan=True,
    ),
    ConfigRow("po_materials.rfq_send.polling_enabled", "po_materials", "non_empty"),
    ConfigRow("po_materials.rfq_send.scheduled_send_local", "po_materials", "non_empty"),
)


# ---- result plumbing (watchdog.py CheckResult style, PASS/FAIL binary) ----


@dataclass(frozen=True)
class Options:
    """Cross-check options resolved from the CLI."""

    allow_sandbox: bool = False
    # Active profile name (banner/observability) + its exemption set. Empty set =
    # no exemptions (the default full-production gate).
    profile: str | None = None
    sandbox_exempt: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class CheckOutcome:
    passed: bool
    summary: str
    details: str = ""


@dataclass(frozen=True)
class CheckSpec:
    check_id: str  # "VC-01" — the id the cutover checklist cross-references
    slug: str      # "keychain" — the --only/--skip handle
    description: str
    fn: Callable[[Options], CheckOutcome]


# ---- VC-01 keychain -------------------------------------------------------


def _check_keychain(opts: Options) -> CheckOutcome:
    """All required Keychain secrets present (presence + length only, §54)."""
    missing: list[str] = []
    present: list[str] = []
    for name in REQUIRED_SECRETS:
        try:
            value = keychain.get_secret(name)
        except keychain.KeychainError:
            missing.append(name)
            continue
        if value:
            present.append(f"{name} (len={len(value)})")
        else:
            missing.append(f"{name} (empty)")
    if missing:
        return CheckOutcome(
            passed=False,
            summary=f"{len(missing)} of {len(REQUIRED_SECRETS)} required secrets missing/empty.",
            details="missing: " + ", ".join(missing),
        )
    return CheckOutcome(
        passed=True,
        summary=f"{len(present)}/{len(REQUIRED_SECRETS)} required Keychain secrets present.",
    )


# ---- VC-02 launchd --------------------------------------------------------


def _expected_labels() -> set[str]:
    """The labels that MUST be loaded at cutover — every shipped plist MINUS the
    dark-unloaded send daemons (DARK_UNLOADED_LABELS). Derived, so a new daemon plist
    auto-enrolls in this check with no edit here."""
    shipped = {p.stem for p in LAUNCHD_PLIST_DIR.glob(f"{LABEL_PREFIX}*.plist")}
    return shipped - DARK_UNLOADED_LABELS


def _launchctl_list() -> str:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout


def _check_launchd(opts: Options) -> CheckOutcome:
    """`launchctl list` shows exactly the must-be-loaded ITS label set: every shipped
    plist loaded EXCEPT the dark-unloaded send daemons, which must NOT be loaded."""
    expected = _expected_labels()
    loaded: set[str] = set()
    for line in _launchctl_list().splitlines():
        parts = line.split()
        if parts and parts[-1].startswith(LABEL_PREFIX):
            loaded.add(parts[-1])
    missing = sorted(expected - loaded)
    orphans = loaded - expected
    dark_loaded = sorted(orphans & DARK_UNLOADED_LABELS)   # send daemon running = send-gate violation
    true_orphans = sorted(orphans - DARK_UNLOADED_LABELS)  # loaded but not shipped at all
    if missing or dark_loaded or true_orphans:
        details: list[str] = []
        if missing:
            details.append("not loaded: " + ", ".join(missing))
        if dark_loaded:
            details.append(
                "dark-unloaded SEND daemon IS loaded (send-gate violation): "
                + ", ".join(dark_loaded)
            )
        if true_orphans:
            details.append("loaded but not shipped (orphan): " + ", ".join(true_orphans))
        return CheckOutcome(
            passed=False,
            summary=(
                f"launchd label set mismatch ({len(missing)} missing, "
                f"{len(dark_loaded)} dark-loaded, {len(true_orphans)} orphan)."
            ),
            details="; ".join(details),
        )
    return CheckOutcome(
        passed=True,
        summary=(
            f"all {len(expected)} must-load ITS labels loaded; "
            f"{len(DARK_UNLOADED_LABELS)} send daemon(s) correctly unloaded (send-gate)."
        ),
    )


# ---- VC-03 config ---------------------------------------------------------


def _check_config(opts: Options) -> CheckOutcome:
    """Load-bearing ITS_Config rows present + non-default (+ sandbox-free)."""
    problems: list[str] = []
    exempted: list[str] = []  # profile-exempted rows that actually carried the mirror value
    for row in CONFIG_ROWS:
        try:
            value = smartsheet_client.get_setting(row.key, workstream=row.workstream)
        except SmartsheetNotFoundError:
            problems.append(f"{row.key} [{row.workstream}]: row MISSING")
            continue
        text = (value or "").strip()
        if not text:
            problems.append(f"{row.key} [{row.workstream}]: blank Value")
            continue
        if row.requirement == "true" and text.lower() != "true":
            problems.append(f"{row.key} [{row.workstream}]: expected 'true', got {text!r}")
            continue
        if (
            row.sandbox_scan
            and not opts.allow_sandbox
            and SANDBOX_DOMAIN_MARKER in text.lower()
        ):
            if (row.key, row.workstream) in opts.sandbox_exempt:
                # Profile-sanctioned mirror value — allowed, but named in the
                # summary so the exemption is never silent.
                exempted.append(f"{row.key} [{row.workstream}]")
                continue
            problems.append(
                f"{row.key} [{row.workstream}]: still points at the sandbox "
                f"({SANDBOX_DOMAIN_MARKER!r} in value)"
            )
    if problems:
        return CheckOutcome(
            passed=False,
            summary=f"{len(problems)} of {len(CONFIG_ROWS)} load-bearing config rows failed.",
            details="; ".join(problems),
        )
    suffix = " (sandbox values allowed)" if opts.allow_sandbox else ""
    if exempted:
        suffix += (
            f" ({len(exempted)} mirror row(s) exempted by --profile {opts.profile}: "
            + ", ".join(exempted)
            + ")"
        )
    return CheckOutcome(
        passed=True,
        summary=f"all {len(CONFIG_ROWS)} load-bearing ITS_Config rows present + non-default{suffix}.",
    )


# ---- VC-04 daemon-health --------------------------------------------------


def _parse_heartbeat(raw: object) -> datetime | None:
    """Parse the Last Heartbeat cell (ISO-8601 UTC per the schema). Naive
    strings are assumed UTC (defensive — the writer emits offset-aware)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _check_daemon_health(opts: Options) -> CheckOutcome:
    """Every Enabled ITS_Daemon_Health row fresh within 2 x Interval Seconds.

    Enabled=false rows are ignored — per the schema, Enabled marks "expected
    to write heartbeats" (report-filter metadata; the runtime gate is the
    ITS_Config row, which VC-03 covers).
    """
    rows = smartsheet_client.get_rows(sheet_ids.SHEET_DAEMON_HEALTH)
    now = datetime.now(UTC)
    problems: list[str] = []
    enabled_count = 0
    for row in rows:
        if not row.get("Enabled"):
            continue
        enabled_count += 1
        name = str(row.get("Daemon Name") or f"row {row.get('_row_id')}")
        heartbeat = _parse_heartbeat(row.get("Last Heartbeat"))
        if heartbeat is None:
            problems.append(f"{name}: no parseable Last Heartbeat")
            continue
        try:
            interval = float(str(row.get("Interval Seconds")))
        except (TypeError, ValueError):
            problems.append(f"{name}: no numeric Interval Seconds")
            continue
        age = (now - heartbeat).total_seconds()
        limit = DAEMON_HEALTH_STALE_MULTIPLIER * interval
        if age > limit:
            problems.append(f"{name}: heartbeat {age:.0f}s old (limit {limit:.0f}s)")
    if enabled_count == 0:
        return CheckOutcome(
            passed=False,
            summary="ITS_Daemon_Health has zero Enabled rows — nothing is heartbeating.",
        )
    if problems:
        return CheckOutcome(
            passed=False,
            summary=f"{len(problems)} of {enabled_count} enabled daemon rows stale/unparseable.",
            details="; ".join(problems),
        )
    return CheckOutcome(
        passed=True,
        summary=f"all {enabled_count} enabled daemon-health rows fresh (< 2x interval).",
    )


# ---- VC-05 review-queue ---------------------------------------------------


def _check_review_queue(opts: Options) -> CheckOutcome:
    """ITS_Review_Queue reachable (a read failure at cutover blinds triage)."""
    pending = review_queue.get_pending()
    return CheckOutcome(
        passed=True,
        summary=f"ITS_Review_Queue reachable ({len(pending)} pending row(s)).",
    )


# ---- VC-06 alerting -------------------------------------------------------


def _check_alerting(opts: Options) -> CheckOutcome:
    """Sentry DSN + Resend key present and shape-valid (values never printed)."""
    problems: list[str] = []
    try:
        dsn = keychain.get_secret("ITS_SENTRY_DSN")
        if not dsn.startswith("https://"):
            problems.append("ITS_SENTRY_DSN does not look like an https DSN")
    except keychain.KeychainError:
        problems.append("ITS_SENTRY_DSN missing from Keychain")
    try:
        resend_key = keychain.get_secret("ITS_RESEND_API_KEY")
        if not resend_key.startswith("re_"):
            problems.append("ITS_RESEND_API_KEY does not start with 're_'")
    except keychain.KeychainError:
        problems.append("ITS_RESEND_API_KEY missing from Keychain")
    if problems:
        return CheckOutcome(
            passed=False,
            summary="alerting credentials missing or malformed.",
            details="; ".join(problems),
        )
    return CheckOutcome(passed=True, summary="Sentry DSN + Resend key present and shape-valid.")


# ---- VC-07 git ------------------------------------------------------------


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def _check_git(opts: Options) -> CheckOutcome:
    """On main, clean tree, HEAD == origin/main (read-only: ls-remote, no fetch)."""
    problems: list[str] = []
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        problems.append(f"on branch {branch!r}, expected 'main'")
    if _git("status", "--porcelain"):
        problems.append("working tree not clean")
    local_head = _git("rev-parse", "HEAD")
    ls_remote = _git("ls-remote", "origin", "refs/heads/main")
    remote_head = ls_remote.split()[0] if ls_remote else ""
    if not remote_head:
        problems.append("could not resolve origin/main via ls-remote")
    elif local_head != remote_head:
        problems.append(
            f"HEAD {local_head[:9]} != origin/main {remote_head[:9]} (stale checkout)"
        )
    if problems:
        return CheckOutcome(
            passed=False,
            summary="git tree is not a clean origin/main checkout.",
            details="; ".join(problems),
        )
    return CheckOutcome(
        passed=True, summary=f"on main @ {local_head[:9]}, clean, matches origin/main."
    )


# ---- VC-08 d1-migrations --------------------------------------------------


def _run_wrangler_migrations_list() -> subprocess.CompletedProcess[str]:
    """One `wrangler d1 migrations list --remote` invocation (read-only).

    Runs from safety_portal/ so wrangler resolves wrangler.jsonc + the local
    node_modules install. NEVER run this from a stale checkout — the migrations
    folder IS the comparison baseline (forensic class #2); VC-07 enforces that.
    """
    return subprocess.run(
        ["npx", "wrangler", "d1", "migrations", "list", D1_DATABASE_NAME, "--remote"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT / "safety_portal",
        timeout=WRANGLER_TIMEOUT_SECONDS,
    )


def _check_d1_migrations(opts: Options) -> CheckOutcome:
    """Remote D1 has no pending migrations; one retry on transient 7403."""
    attempts = 0
    result = _run_wrangler_migrations_list()
    attempts += 1
    combined = (result.stdout or "") + (result.stderr or "")
    if D1_TRANSIENT_ERROR_MARKER in combined:
        result = _run_wrangler_migrations_list()
        attempts += 1
        combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0 and "no migrations to apply" in combined.lower():
        return CheckOutcome(
            passed=True,
            summary=f"remote D1 ({D1_DATABASE_NAME}) has no pending migrations"
            + (f" (after {attempts} attempts)" if attempts > 1 else "")
            + ".",
        )
    tail = "\n".join(combined.strip().splitlines()[-8:])
    return CheckOutcome(
        passed=False,
        summary=f"pending migrations or wrangler failure (rc={result.returncode}, "
        f"{attempts} attempt(s)).",
        details=tail,
    )


# ---- VC-09 heartbeat-url --------------------------------------------------


def _check_heartbeat_url(opts: Options) -> CheckOutcome:
    """UptimeRobot heartbeat URL configured (watchdog's external dead-man ping)."""
    try:
        value = smartsheet_client.get_setting("system.heartbeat_url", workstream="global")
    except SmartsheetNotFoundError:
        return CheckOutcome(
            passed=False,
            summary="ITS_Config row system.heartbeat_url [global] MISSING.",
        )
    text = (value or "").strip()
    if not text.startswith("https://"):
        return CheckOutcome(
            passed=False,
            summary="system.heartbeat_url is blank or not an https URL.",
        )
    return CheckOutcome(passed=True, summary="system.heartbeat_url configured (https).")


# ---- VC-10 approver-shares ------------------------------------------------


def _load_shares_manifest() -> dict[str, object]:
    """Parse the checked-in approver-share manifest (schema-validated at CI by
    tests/test_production_shares.py — the gate needs only a structural read)."""
    loaded: dict[str, object] = json.loads(
        APPROVER_SHARES_MANIFEST.read_text(encoding="utf-8")
    )
    return loaded


def _check_approver_shares(opts: Options) -> CheckOutcome:
    """F22 approver USER shares match the production manifest (CL-11 + CL-37).

    Per manifest workspace (id via its ``shared.sheet_ids`` constant): (a) the
    manifest approver emails must be a SUBSET of the live USER-share emails —
    subset, deliberately NOT equality, because the operator's own account / its@
    may legitimately hold shares beyond the manifest; (b) ZERO live USER shares
    on the mirror domain (each residue named — removal is a manual unshare, the
    seeder never deletes); (c) any GROUP share FAILS the check, named as
    non-counting toward F22 (a group-only share silently fail-closes every send,
    and a group share beside valid USER shares invites the false belief that
    group members hold approval authority).

    ``--allow-sandbox`` waives the whole check (PASS, no API call — mirror dress
    rehearsals run mirror-account shares by design). A ``--profile`` does NOT
    exempt VC-10: a profile-gated phase cutover still requires the production
    approver shares on every send-bearing workspace.
    """
    if opts.allow_sandbox:
        return CheckOutcome(
            passed=True,
            summary="approver-share check waived (sandbox mode — manifest diff waived).",
        )
    try:
        manifest = _load_shares_manifest()
        workspaces = manifest["workspaces"]
        mirror_domain = str(manifest["mirror_domain"]).strip().lower()
    except (OSError, ValueError, KeyError) as exc:
        return CheckOutcome(
            passed=False,
            summary=f"approver-share manifest unreadable ({APPROVER_SHARES_MANIFEST}).",
            details=f"{type(exc).__name__}: {exc}",
        )
    # The residue leg keys off the manifest's mirror_domain — pin it to the
    # SANDBOX_DOMAIN_MARKER single source so a manifest typo cannot silently
    # blind the check (adversarial review 2026-07-23; the seeder pins the same
    # value as EXPECTED_MIRROR_DOMAIN).
    if mirror_domain != f"{SANDBOX_DOMAIN_MARKER}.com":
        return CheckOutcome(
            passed=False,
            summary=(
                f"manifest mirror_domain {mirror_domain!r} != "
                f"'{SANDBOX_DOMAIN_MARKER}.com' — a typo here would BLIND the "
                "mirror-residue leg; fix the manifest."
            ),
        )
    mirror_suffix = "@" + mirror_domain
    if not isinstance(workspaces, list) or not workspaces:
        return CheckOutcome(
            passed=False,
            summary="approver-share manifest names zero workspaces.",
        )
    problems: list[str] = []
    checked = 0
    for ws in workspaces:
        constant = str(ws.get("constant"))
        label = f"{ws.get('name')} [{constant}]"
        workspace_id = int(getattr(sheet_ids, constant, 0) or 0)
        if not workspace_id:
            problems.append(f"{label}: sheet_ids.{constant} missing or 0 — cannot check")
            continue
        # Live-identity assert (adversarial review 2026-07-23): the constant is a
        # stored POINTER — prove the object it resolves to is still the workspace
        # the manifest MEANS, or an ID-drifted constant silently audits the wrong
        # object (false PASS, or a FAIL masking the real target). Per-workspace
        # isolation: one dead/drifted ID must not blind the other legs.
        try:
            live_name = str(
                smartsheet_client.get_workspace_name(workspace_id)).strip()
            expected_name = str(ws.get("name", "")).strip()
            if live_name != expected_name:
                problems.append(
                    f"{label}: sheet_ids.{constant}={workspace_id} resolves to a "
                    f"live workspace named {live_name!r}, not {expected_name!r} — "
                    "stale constant / regen pending; REFUSING to audit the wrong "
                    "object")
                continue
            shares = smartsheet_client.list_workspace_shares(workspace_id)
        except Exception as exc:  # noqa: BLE001 — named per-workspace, never blinds the rest
            problems.append(
                f"{label}: live fetch failed ({type(exc).__name__}: {exc})")
            continue
        user_emails = {
            str(share["email"]).strip().lower()
            for share in shares
            if share.get("email")
        }
        expected = {
            str(approver["email"]).strip().lower()
            for approver in ws.get("approvers", [])
        }
        for email in sorted(expected - user_emails):
            problems.append(f"{label}: missing approver USER share {email}")
        for email in sorted(e for e in user_emails if e.endswith(mirror_suffix)):
            problems.append(
                f"{label}: mirror-account share residue {email} (UNSHARE by hand — "
                "the seeder never deletes)"
            )
        for share in shares:
            if not share.get("email"):
                group_name = share.get("name") or f"groupId={share.get('groupId')}"
                problems.append(
                    f"{label}: GROUP share {group_name!r} does NOT count toward F22 "
                    "(group members hold NO approval authority — approvers need "
                    "individual USER shares)"
                )
        checked += 1
    if problems:
        return CheckOutcome(
            passed=False,
            summary=(
                f"approver-share audit failed on {checked} checked workspace(s) "
                f"({len(problems)} problem(s))."
            ),
            details="; ".join(problems),
        )
    return CheckOutcome(
        passed=True,
        summary=(
            f"all {checked} manifest workspaces carry the production approver USER "
            "shares (manifest ⊆ live), zero mirror residue, no GROUP shares."
        ),
    )


# ---- registry + harness ---------------------------------------------------

CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("VC-01", "keychain", "required Keychain secrets present", _check_keychain),
    CheckSpec("VC-02", "launchd", "loaded launchd label set matches shipped plists", _check_launchd),
    CheckSpec("VC-03", "config", "load-bearing ITS_Config rows present + non-default", _check_config),
    CheckSpec("VC-04", "daemon-health", "enabled daemon-health rows fresh (< 2x interval)", _check_daemon_health),
    CheckSpec("VC-05", "review-queue", "ITS_Review_Queue reachable", _check_review_queue),
    CheckSpec("VC-06", "alerting", "Sentry DSN + Resend key present, shape-valid", _check_alerting),
    CheckSpec("VC-07", "git", "repo on main, clean, matches origin/main", _check_git),
    CheckSpec("VC-08", "d1-migrations", "remote D1 has no pending migrations", _check_d1_migrations),
    CheckSpec("VC-09", "heartbeat-url", "UptimeRobot heartbeat URL configured", _check_heartbeat_url),
    CheckSpec("VC-10", "approver-shares", "F22 approver USER shares match the production manifest", _check_approver_shares),
)


def _resolve_selection(only: str | None, skip: str | None) -> list[CheckSpec]:
    """Filter CHECKS by --only/--skip (comma-separated slugs or VC ids).

    Unknown names raise ValueError — a typo silently skipping a gate check
    would be a fail-open misconfig.
    """
    by_handle = {spec.slug: spec for spec in CHECKS} | {spec.check_id: spec for spec in CHECKS}

    def parse(csv: str) -> list[CheckSpec]:
        specs: list[CheckSpec] = []
        for token in (t.strip() for t in csv.split(",")):
            if not token:
                continue
            if token not in by_handle:
                raise ValueError(
                    f"unknown check {token!r} — valid handles: "
                    + ", ".join(s.slug for s in CHECKS)
                )
            specs.append(by_handle[token])
        return specs

    if only:
        selected = parse(only)
    else:
        selected = list(CHECKS)
    if skip:
        skipped_ids = {spec.check_id for spec in parse(skip)}
        selected = [spec for spec in selected if spec.check_id not in skipped_ids]
    # Preserve canonical order + dedupe.
    seen: set[str] = set()
    ordered: list[CheckSpec] = []
    for spec in CHECKS:
        if spec.check_id in {s.check_id for s in selected} and spec.check_id not in seen:
            seen.add(spec.check_id)
            ordered.append(spec)
    return ordered


def _run_one(spec: CheckSpec, opts: Options) -> CheckOutcome:
    """Failure isolation: an exception inside a check is a FAIL for that check,
    not a crash of the gate (the remaining checks still run)."""
    try:
        return spec.fn(opts)
    except Exception as exc:  # noqa: BLE001 — deliberate harness-level catch
        return CheckOutcome(
            passed=False,
            summary=f"check raised {type(exc).__name__} (treated as FAIL).",
            details=str(exc),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_cutover",
        description="§53 mechanical cutover gate — read-only; exits 0 only when all selected checks pass.",
    )
    parser.add_argument("--only", help="comma-separated check slugs/ids to run (default: all)")
    parser.add_argument("--skip", help="comma-separated check slugs/ids to skip")
    parser.add_argument("--list", action="store_true", help="list checks and exit")
    # --allow-sandbox and --profile are mutually exclusive: a profile is a precise,
    # reviewed exemption set and must never be silently widened to a blanket waiver.
    sandbox_group = parser.add_mutually_exclusive_group()
    sandbox_group.add_argument(
        "--allow-sandbox",
        action="store_true",
        help="permit evergreenmirror.com values in VC-03 (mirror dress-rehearsal mode; "
        "NOT a cutover verdict)",
    )
    sandbox_group.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        help="phase gate: exempt exactly the named profile's row set from the VC-03 "
        "sandbox scan; all other rows stay production-scanned (a profile run IS the "
        "phase's cutover verdict)",
    )
    args = parser.parse_args(argv)

    if args.list:
        for spec in CHECKS:
            print(f"{spec.check_id}  {spec.slug:<15} {spec.description}")
        return 0

    try:
        selected = _resolve_selection(args.only, args.skip)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not selected:
        print("error: selection resolves to zero checks", file=sys.stderr)
        return 2

    opts = Options(
        allow_sandbox=bool(args.allow_sandbox),
        profile=args.profile,
        sandbox_exempt=PROFILES[args.profile] if args.profile else frozenset(),
    )
    partial = len(selected) != len(CHECKS)
    if partial:
        print("== PARTIAL RUN — not a cutover verdict (some checks not selected) ==")
    if opts.allow_sandbox:
        print("== --allow-sandbox — mirror values permitted; NOT a production verdict ==")
    if opts.profile:
        print(
            f"== --profile {opts.profile} — {len(opts.sandbox_exempt)} named row(s) "
            "exempt from the sandbox scan; everything else production-scanned =="
        )

    failures = 0
    for spec in selected:
        outcome = _run_one(spec, opts)
        marker = "PASS" if outcome.passed else "FAIL"
        print(f"[{marker}] {spec.check_id} {spec.slug} — {outcome.summary}")
        if outcome.details and not outcome.passed:
            for line in outcome.details.splitlines():
                print(f"        {line}")
        if not outcome.passed:
            failures += 1

    skipped = len(CHECKS) - len(selected)
    print(
        f"verify_cutover: {len(selected) - failures} passed, {failures} failed, "
        f"{skipped} skipped."
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
