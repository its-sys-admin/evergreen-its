"""Generate the ITS_Config data dictionary (WS3 / D2-2) — deterministic + network-free.

WHAT THIS IS
------------
A single deterministic generator that emits, from the *in-repo* config definitions, a
plain-language **ITS_Config data dictionary**: every runtime ITS_Config key ITS reads —
its Setting name, its Workstream scope, its default value, its type, and a one-line
purpose — for the operator + the WS2 dashboard. It NEVER reads live Smartsheet; the
source of truth is the daemons' own ``REQUIRED_CONFIG: list[ConfigKey]`` declarations
(``shared/required_config.py``, issue #336) plus the small set of shared-infrastructure
keys those declarations deliberately omit (read by shared helpers, not per-daemon).

Two outputs, both regenerated in place:

  * ``docs/references/its_config_dictionary.md``   — the branded-manual source (rendered
    to PDF by ``scripts/build_docs_pdfs.py`` once registered in the §6a manifest).
  * ``operator_dashboard/config_defaults.json``    — the machine-readable twin the WS2
    operator dashboard consumes (schema below).

SOURCES (all in-repo, all network-free)
---------------------------------------
1. **Daemon ``REQUIRED_CONFIG``** — the authoritative, self-maintaining source. Every
   polling / scheduled daemon declares a module-level ``REQUIRED_CONFIG`` enumerating the
   ``(Setting, Workstream, default, kind, description)`` tuples it resolves at runtime
   (the #336 observable-config ledger). This generator DISCOVERS every module that
   declares one (a filesystem scan for the declaration marker), imports it, and reads the
   list — so a new daemon's keys appear automatically, with the real resolved constant
   values (importing resolves ``CFG_* = "…"`` constants; an AST parse would not).
2. **Shared-infrastructure keys** (``SHARED_INFRA_KEYS``) — the keys read by *shared*
   helpers that ``REQUIRED_CONFIG`` intentionally does NOT duplicate across every caller
   (``system.state`` / ``system.operator_email`` / ``alerting.*`` / ``circuit_breaker.*``
   / ``picklist_sync.*`` / ``smartsheet.sheet_count_*``). Their defaults are sourced by
   importing ``shared.defaults`` (a pure constants module) so they never drift from code.
3. **Purpose prose** — most ``ConfigKey`` declarations carry no ``description`` (the field
   exists but is usually blank), so this generator supplies the human-readable purpose:
   a ``ConfigKey.description`` wins when present, else an exact-match ``PURPOSE_OVERRIDES``
   entry, else a suffix-family pattern (``*.polling_enabled`` etc.). A key with NO purpose
   from any source is surfaced LOUDLY on stderr (``--check`` fails) — never silently blank.

PURITY / DETERMINISM
--------------------
No network, no Smartsheet, no state writes beyond the two output files. The generated
bytes are a pure function of the repo tree: keys sorted by ``(workstream, setting)``, no
timestamp / git-SHA embedded (the PDF footer adds the SHA at render time, not here), so a
re-run on an unchanged tree produces byte-identical output — the idempotency the WS3
doc-currency pipeline relies on.

CLI
---
    python -m scripts.generate_config_dictionary            # regenerate both outputs
    python -m scripts.generate_config_dictionary --check    # verify committed == fresh (CI-friendly)
    python -m scripts.generate_config_dictionary --stdout   # print the markdown, write nothing

``--check`` regenerates in memory and diffs against the committed files (mirrors
``build_docs_pdfs --check`` / ``regen_doc_indexes --check``): exit 0 when current, 1 when a
regenerate-and-re-record is owed, 2 on any purpose gap. It is intentionally NOT wired as a
blocking CI gate during the docs-program build-out (warn-only, like its siblings).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── paths ─────────────────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
MD_OUT = REPO_ROOT / "docs" / "references" / "its_config_dictionary.md"
JSON_OUT = REPO_ROOT / "operator_dashboard" / "config_defaults.json"

JSON_SCHEMA_VERSION = 1

# Top-level roots scanned for daemon modules that declare ``REQUIRED_CONFIG``. Bounded to
# the Python source roots so the scan never walks ``node_modules`` / ``.git`` / venvs.
_SCAN_ROOTS = ("field_ops", "po_materials", "progress_reports", "safety_reports", "scripts", "subcontracts")
_EXCLUDE_PARTS = frozenset({".claude", "__pycache__", ".venv", ".venv-wt"})
# ``shared/required_config.py`` DEFINES ConfigKey/REQUIRED_CONFIG; this generator merely
# names the marker string — neither is a consumer, so both are skipped explicitly.
_SKIP_FILES = frozenset({
    "shared/required_config.py",
    "scripts/generate_config_dictionary.py",
})


def _declares_required_config(text: str) -> bool:
    """True iff a module genuinely DECLARES ``REQUIRED_CONFIG: list[ConfigKey] = …`` at the
    top level — a real annotated assignment, not a mere string-literal mention of the marker
    (so this generator + the ``required_config`` module do not match themselves)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("REQUIRED_CONFIG:") and "list[ConfigKey]" in stripped and "=" in stripped:
            return True
    return False


# ── intermediate records ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RawKey:
    """One ``ConfigKey`` as declared by a single source (a daemon or a shared helper)."""

    setting: str
    workstream: str
    default: object
    kind: str
    description: str
    source: str      # the declaring module (e.g. "safety_reports.portal_poll")
    origin: str      # "daemon" | "shared-infra"


@dataclass(frozen=True)
class MergedKey:
    """One unique ``(setting, workstream)`` key after merging all declarers."""

    setting: str
    workstream: str
    default: object
    kind: str
    purpose: str
    read_by: tuple[str, ...]   # every module that declares this key, sorted
    origin: str


# ── shared-infrastructure keys (read by shared helpers, not per-daemon REQUIRED_CONFIG) ────
def shared_infra_keys() -> list[RawKey]:
    """The ITS_Config keys read by SHARED helpers, which ``REQUIRED_CONFIG`` deliberately
    does not re-declare across every caller (``required_config.py`` docstring). Defaults are
    sourced from ``shared.defaults`` (a pure constants module) so they track the code."""
    from shared import defaults as d

    def rk(setting: str, default: object, kind: str, helper: str) -> RawKey:
        return RawKey(setting, "global", default, kind, "", helper, "shared-infra")

    return [
        rk("system.state", "ACTIVE", "str", "shared.kill_switch"),
        rk("system.operator_email", d.OPERATOR_EMAIL_FALLBACK, "str", "shared.resend_client"),
        rk("alerting.dedupe_window_minutes", d.ALERTING_DEDUPE_WINDOW_MINUTES, "int", "shared.alert_dedupe"),
        rk("alerting.max_alerts_per_hour", d.ALERTING_MAX_ALERTS_PER_HOUR, "int", "shared.alert_dedupe"),
        rk("circuit_breaker.enabled", d.CIRCUIT_BREAKER_ENABLED, "bool", "shared.circuit_breaker"),
        rk("circuit_breaker.failure_threshold", d.CIRCUIT_BREAKER_FAILURE_THRESHOLD, "int", "shared.circuit_breaker"),
        rk("circuit_breaker.cooldown_seconds", d.CIRCUIT_BREAKER_COOLDOWN_SECONDS, "int", "shared.circuit_breaker"),
        rk("picklist_sync.size_warn_threshold", d.PICKLIST_SIZE_WARN_THRESHOLD, "int", "shared.picklist_sync"),
        rk("picklist_sync.size_hard_halt_threshold", d.PICKLIST_SIZE_HARD_HALT_THRESHOLD, "int", "shared.picklist_sync"),
        rk("smartsheet.sheet_count_ceiling", d.SHEET_COUNT_CEILING, "int", "shared.sheet_capacity"),
        rk("smartsheet.sheet_count_margin", d.SHEET_COUNT_MARGIN, "int", "shared.sheet_capacity"),
        rk("smartsheet.retry.enabled", d.SMARTSHEET_RETRY_ENABLED, "bool", "shared.smartsheet_client"),
        rk("smartsheet.retry.max_extra_attempts", d.SMARTSHEET_RETRY_MAX_EXTRA_ATTEMPTS, "int",
           "shared.smartsheet_client"),
        rk("smartsheet.retry.backoff_seconds", ",".join(str(s) for s in d.SMARTSHEET_RETRY_BACKOFF_SECONDS),
           "str", "shared.smartsheet_client"),
    ]


# ── purpose prose (generator-maintained; ConfigKey.description wins when present) ───────────
# Exact-match human purpose for a Setting name. These describe WHAT the key controls in
# plain language for the operator manual; the machine facts (default/scope/type) come from
# the live declarations, not from here.
PURPOSE_OVERRIDES: dict[str, str] = {
    "system.state": "The system kill switch. ACTIVE = normal; PAUSED / MAINTENANCE make every "
                    "daemon exit cleanly at entry. Fail-open: an unreadable value is treated as "
                    "ACTIVE. This is an operator-convenience pause, not a security control.",
    "system.operator_email": "Where out-of-band operator alerts (Resend) are sent when ITS_Config "
                             "cannot be read — the last-resort page recipient during a Smartsheet outage.",
    "system.heartbeat_url": "The external UptimeRobot heartbeat URL the watchdog pings each run so a "
                            "total MacBook-death (the watchdog can't alert about itself) is caught.",
    "subcontracts.subcontract_poll.subcontractors_sync_enabled":
        "Gate for subcontract_poll's §51 subcontractor-sync passes: the ITS_Subcontractors "
        "full-replace down-sync into the Worker's D1 cache AND the dirty-subcontractor up-sync "
        "back into ITS_Subcontractors (bridge-key find-or-create by Sub Key). Off leaves both "
        "caches stale until it is turned back on; nothing external is sent either way.",
    "alerting.dedupe_window_minutes": "How long (minutes) a repeated CRITICAL alert is suppressed on "
                                      "the push legs (email + Sentry) before it can fire again. The "
                                      "per-occurrence ITS_Errors record is never suppressed.",
    "alerting.max_alerts_per_hour": "Global ceiling on operator alert emails per hour across all keys, "
                                    "so a flapping failure cannot fire unbounded email. The record is "
                                    "never capped — only the email fan-out.",
    "circuit_breaker.enabled": "Whether the Smartsheet circuit breaker is armed. When tripped it "
                               "short-circuits Smartsheet calls during an outage to fail fast.",
    "circuit_breaker.failure_threshold": "Consecutive Smartsheet failures before the breaker opens (trips).",
    "circuit_breaker.cooldown_seconds": "How long (seconds) the breaker stays open before a trial half-open call.",
    "circuit_breaker.prolonged_open_alert_seconds": "How long (seconds) the breaker may stay open before the "
                                                    "watchdog fires a prolonged-open CRITICAL page.",
    "picklist_sync.size_warn_threshold": "Option count on a synced picklist that triggers a WARN (a large but "
                                         "still-processed list).",
    "picklist_sync.size_hard_halt_threshold": "Option count that HARD-HALTS that one mapping's sync (a runaway "
                                              "guardrail).",
    "smartsheet.sheet_count_ceiling": "Per-workspace sheet-count ceiling; a new week/period sheet that would land "
                                      "past it routes to the Review Queue instead of being created silently.",
    "smartsheet.sheet_count_margin": "Headroom below the ceiling at which the sheet-capacity guard starts warning.",
    "smartsheet.retry.enabled": "Whether ITS re-issues a Smartsheet READ that failed with a 5xx or a network "
                                "timeout — the two classes the Smartsheet SDK does not retry itself. Writes are "
                                "never retried. Set false for a pure pass-through escape hatch.",
    "smartsheet.retry.max_extra_attempts": "How many EXTRA attempts a failed Smartsheet read gets before the error "
                                           "is raised to the caller (0 = no retry).",
    "smartsheet.retry.backoff_seconds": "Comma-separated wait (seconds) before each extra attempt, e.g. '2.0,5.0'. "
                                        "The last value repeats if there are more attempts than entries.",
    "safety_reports.portal.worker_base_url": "Base URL of the Safety Portal Cloudflare Worker. The portal pull / "
                                             "PO / progress daemons hit its send-free internal API here. Repointed "
                                             "to the custom domain (safety.evergreenmirror.com) after deploy.",
    "safety_reports.intake.box_filing_enabled": "Whether intake files the rendered safety PDF to Box. Off keeps the "
                                                "pipeline running but skips the Box upload.",
    "safety_reports.intake.mailbox": "The mailbox the (now-dormant, legacy) safety email-intake path read from. The "
                                     "live path is the portal PULL model; this remains for the retired email caller.",
    "safety_reports.photo_screen.clamav_enabled": "Turns on the ClamAV leg of the §34 photo screen (magic + Pillow "
                                                  "verify + re-encode always run; this adds the AV scan). Default OFF.",
    "safety_reports.intake.allowed_senders": "Comma-separated sender allowlist for the intake extraction path (the "
                                             "retired email-PDF intake; the live path is the portal PULL). Empty = none set.",
    "safety_reports.intake.classification_model": "The Anthropic model the intake extraction/classification step uses. "
                                                 "Legacy email-intake path; dormant.",
    "safety_reports.intake.confidence_threshold": "Extraction-confidence floor (0–1). Below it, an item routes to the "
                                                 "Review Queue instead of being trusted (Op Stds confidence scoring).",
    "safety_reports.intake.review_queue_on_low_confidence": "Whether a below-threshold extraction is routed to the "
                                                           "Review Queue (true) rather than dropped.",
    "progress_reports.intake_enabled": "Gate for progress-report intake. NOTE: read under the safety_reports "
                                       "workstream (intake's own workstream), not progress_reports — a documented footgun.",
    "progress_reports.box.portal_root_folder_id": "Box root folder ID under which progress-report packets are filed.",
    "field_ops.box.archive_root_folder_id": "Box root folder ID the Track 6 job archive relocates a closed job's "
                                            "Safety, Progress, Purchase Orders and Subcontracts containers beneath "
                                            "(ITS Archive/<Job>/<Workstream>). "
                                            "Unset = the archive's Box leg fails those four containers rather than "
                                            "silently reporting them moved.",
    "field_ops.fieldops_sync.sync_enabled": "Master gate for the portal→Smartsheet job mirror (fieldops_sync). "
                                            "Ships OFF; the operator flips it on at cutover after the mirror slices land.",
    "field_ops.fieldops_sync.hours_enabled": "Per-stream gate: mirror crew hours from the portal into Smartsheet.",
    "field_ops.fieldops_sync.equipment_enabled": "Per-stream gate: mirror equipment status from the portal into Smartsheet.",
    # NB: this gate drives the material LIST mirror (/material-list-snapshot → the per-job Material
    # List sheet), NOT deliveries — those are `receipts_enabled` below. The text used to read
    # "material receipts", which was harmless while no receipts gate existed and actively misleading
    # the moment one did.
    "field_ops.fieldops_sync.materials_enabled": "Per-stream gate: mirror the expected-material list from the portal "
                                                "into Smartsheet. (Activation is gated on the §51 rider — read the "
                                                "row's Description before flipping.)",
    "field_ops.fieldops_sync.incidents_enabled": "Per-stream gate: mirror material incidents from the portal into Smartsheet.",
    "field_ops.fieldops_sync.receipts_enabled": "Per-stream gate: mirror the delivery-receipt ledger (delivered / "
                                               "partial / not-delivered marks) from the portal into the per-job "
                                               "Material Receipts sheet. Append-only — the ledger re-projects in "
                                               "full each cycle, so pausing loses nothing.",
    "field_ops.fieldops_sync.archive_enabled": "Gate for the Track 6 job-archive pass: drains the portal's archive queue "
                                               "and relocates a closed job's eight Smartsheet + Box containers into the "
                                               "archive (and back). Ships OFF; until it is on, the portal's Archive "
                                               "button records intent that nothing acts on.",
    "po_materials.config_actuator.polling_enabled": "Runtime gate for the §50 config actuator daemon (applies approved "
                                                    "workstream-config changes on the Mac).",
    "po_materials.po_poll.polling_enabled": "Runtime gate for the PO pull daemon (pulls submitted POs from "
                                            "the Worker). Off pauses the pull; queued POs wait in the Worker "
                                            "and nothing is lost. Generation only — this gate never sends.",
    "po_materials.po_poll.vendors_sync_enabled": "Sub-gate: push the vendor list down to the portal PO "
                                                 "dropdown. Off freezes the portal's vendor dropdown at its "
                                                 "last synced contents; nothing external is sent either way.",
    "po_materials.po_poll.status_sync_enabled": "Sub-gate: sync PO statuses back to the portal. Off leaves "
                                                "portal-side PO statuses stale until it is turned back on; "
                                                "nothing external is sent either way.",
}

# Suffix-family fallbacks — a purpose for a whole class of keys, keyed by the last dotted
# segment. Used only when neither ConfigKey.description nor PURPOSE_OVERRIDES matches.
_SUFFIX_PATTERNS: tuple[tuple[str, str], ...] = (
    (".polling_enabled", "Runtime on/off gate for the {daemon} daemon. False pauses it without "
                         "unloading its launchd job (the canonical runtime gate, distinct from the "
                         "report-filter Enabled checkbox)."),
    (".poll_interval_seconds", "How often (seconds) the {daemon} daemon polls."),
    (".from_mailbox", "The M365 mailbox the {daemon} send daemon sends approved email FROM."),
    (".row_cap_warn_threshold", "Row-count on the mirror sheet at which {daemon} WARNs that the sheet is "
                                "approaching the Smartsheet per-sheet row cap."),
    (".box_filing_enabled", "Whether {daemon} files its rendered PDF to Box."),
    (".vendors_sync_enabled", "Sub-gate for {daemon}: push the vendor list to the portal."),
    (".status_sync_enabled", "Sub-gate for {daemon}: sync statuses back to the portal."),
    (".scheduled_send_local", "Local-time window (e.g. `MON 07:00`) at/after which a row approved with "
                              "**Approve for Scheduled Send** may dispatch on the {daemon} path."),
    (".job_timeout_seconds", "Per-job wall-clock ceiling (seconds) for the {daemon} weekly compile; a job "
                             "exceeding it is fenced to the Review Queue, not left to hang."),
    (".merge_memory_ceiling_bytes", "Memory ceiling (bytes) for the {daemon} PDF-merge step; a packet whose "
                                    "merge would exceed it is refused rather than risk OOMing the daemon."),
    (".evergreen_contact_name", "The name ITS uses for the Evergreen Renewables office/contact in this "
                                "workstream's report copy."),
)


def _daemon_label(setting: str) -> str:
    """A readable daemon/subsystem label from a dotted setting name (drops the leaf)."""
    parts = setting.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else setting


def resolve_purpose(setting: str, description: str) -> str:
    """Purpose prose for a key: ``ConfigKey.description`` → exact override → suffix family →
    "" (an empty string flags a gap the caller surfaces loudly)."""
    if description.strip():
        return description.strip()
    if setting in PURPOSE_OVERRIDES:
        return PURPOSE_OVERRIDES[setting]
    for suffix, template in _SUFFIX_PATTERNS:
        if setting.endswith(suffix):
            return template.format(daemon=_daemon_label(setting))
    return ""


# ── discovery + import ─────────────────────────────────────────────────────────────────────
def _import_name(path: Path) -> str:
    """Dotted module name for a package file; the bare stem for a non-package script (which
    the caller imports after putting ``scripts/`` on ``sys.path``)."""
    if (path.parent / "__init__.py").exists():
        return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
    return path.stem


def discover_daemon_modules() -> list[tuple[str, Path]]:
    """Sorted ``(import_name, path)`` for every module declaring ``REQUIRED_CONFIG``.

    Self-maintaining: a new daemon that declares the ledger is picked up with no edit here.
    Deterministic: bounded roots, sorted, deduped by import name."""
    found: dict[str, Path] = {}
    for root in _SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT)
            if rel.as_posix() in _SKIP_FILES:
                continue
            if any(part in _EXCLUDE_PARTS for part in rel.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not _declares_required_config(text):
                continue
            found[_import_name(path)] = path
    return sorted(found.items())


def _import_module(name: str) -> Any:
    """Import a discovered module. Non-package scripts import by stem, so ensure ``scripts/``
    is importable first. Import failures propagate (a lost key must never be silent)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


def collect_daemon_keys() -> list[RawKey]:
    """Import every discovered daemon and flatten its ``REQUIRED_CONFIG`` to ``RawKey``s."""
    out: list[RawKey] = []
    for name, _path in discover_daemon_modules():
        mod = _import_module(name)
        rc = getattr(mod, "REQUIRED_CONFIG", None)
        if not isinstance(rc, list):
            # A file matched the declaration marker but exposes no list — surface it LOUDLY
            # (never silently drop a daemon's keys) and skip rather than abort the whole run.
            print(f"WARNING {name}: REQUIRED_CONFIG is missing or not a list — skipped",
                  file=sys.stderr)
            continue
        for ck in rc:
            out.append(RawKey(
                setting=str(ck.setting), workstream=str(ck.workstream), default=ck.default,
                kind=str(ck.kind), description=str(getattr(ck, "description", "")),
                source=name, origin="daemon",
            ))
    return out


# ── merge ─────────────────────────────────────────────────────────────────────────────────
def merge_keys(raw: list[RawKey]) -> tuple[list[MergedKey], list[str], list[str]]:
    """Merge ``RawKey``s to unique ``(setting, workstream)`` keys.

    Returns ``(merged, conflicts, gaps)``: ``conflicts`` note keys whose declarers disagree
    on default/kind (a real bug worth surfacing); ``gaps`` are keys with no purpose."""
    groups: dict[tuple[str, str], list[RawKey]] = {}
    for rk in raw:
        groups.setdefault((rk.setting, rk.workstream), []).append(rk)

    merged: list[MergedKey] = []
    conflicts: list[str] = []
    gaps: list[str] = []
    for (setting, workstream), members in sorted(groups.items()):
        first = members[0]
        defaults_seen = {repr(m.default) for m in members}
        kinds_seen = {m.kind for m in members}
        if len(defaults_seen) > 1 or len(kinds_seen) > 1:
            conflicts.append(
                f"{setting} [{workstream}]: declarers disagree — "
                f"defaults={sorted(defaults_seen)} kinds={sorted(kinds_seen)} "
                f"(sources: {sorted({m.source for m in members})})"
            )
        description = next((m.description for m in members if m.description.strip()), "")
        purpose = resolve_purpose(setting, description)
        if not purpose:
            gaps.append(f"{setting} [{workstream}]")
        merged.append(MergedKey(
            setting=setting, workstream=workstream, default=first.default, kind=first.kind,
            purpose=purpose or "—",
            read_by=tuple(sorted({m.source for m in members})),
            origin=first.origin,
        ))
    return merged, conflicts, gaps


def collect_all() -> tuple[list[MergedKey], list[str], list[str]]:
    """The full pipeline: daemon keys + shared-infra keys → merged, sorted, with diagnostics."""
    return merge_keys(collect_daemon_keys() + shared_infra_keys())


# ── rendering ──────────────────────────────────────────────────────────────────────────────
def _fmt_default(default: object, kind: str) -> str:
    """Human-readable default for the markdown table."""
    if kind == "bool":
        return "true" if bool(default) else "false"
    if default == "" or default is None:
        return "*(unset)*"
    return str(default)


def _cell(text: str) -> str:
    """Escape a value for a GFM pipe-table cell (pipes + newlines would break the row)."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _workstream_heading(ws: str) -> str:
    labels = {
        "global": "Global / shared-infrastructure keys",
        "safety_reports": "Safety Reports",
        "progress_reports": "Progress Reports",
        "field_ops": "Field-Ops (portal → Smartsheet mirror)",
        "po_materials": "Purchase Orders & Materials",
    }
    return labels.get(ws, ws)


def render_markdown(keys: list[MergedKey]) -> str:
    """Render the merged keys to the data-dictionary markdown (deterministic; no timestamp)."""
    by_ws: dict[str, list[MergedKey]] = {}
    for k in keys:
        by_ws.setdefault(k.workstream, []).append(k)
    # Stable section order: global first, then the workstreams alphabetically, then any extras.
    order = ["global", "field_ops", "po_materials", "progress_reports", "safety_reports"]
    ordered_ws = [w for w in order if w in by_ws] + sorted(w for w in by_ws if w not in order)

    lines: list[str] = []
    lines.append("---")
    lines.append("type: reference")
    lines.append("status: active")
    lines.append("generated_by: scripts/generate_config_dictionary.py")
    lines.append("workstream: null")
    lines.append("tags: [reference, a8, its-config, data-dictionary, generated]")
    lines.append("---")
    lines.append("")
    lines.append("<!-- GENERATED FILE — do not hand-edit. Regenerate with:")
    lines.append("       python -m scripts.generate_config_dictionary")
    lines.append("     Then re-record its sha256 in docs/enablement/manifest.yaml. -->")
    lines.append("")
    lines.append("# ITS_Config Data Dictionary")
    lines.append("")
    lines.append(
        "This is the operator reference for **ITS_Config** — the Smartsheet sheet where every "
        "runtime setting ITS reads is stored, one row per setting. It lists every key ITS looks "
        "up while running: what it controls, which **Workstream** row it lives under, its "
        "**default** (the value used when the row is missing, blank, or unreadable), and its type. "
        "It is generated from the code itself, so it always matches what the daemons actually read."
    )
    lines.append("")
    lines.append(
        "> **This page is generated.** It is produced by `scripts/generate_config_dictionary.py` "
        "from the daemons' own config declarations — never hand-edit it. If a value here looks "
        "wrong, the fix is in the code, not this page."
    )
    lines.append("")
    lines.append("## How to read this")
    lines.append("")
    lines.append(
        "- **Setting** is the exact value in the ITS_Config **Setting** column. **Workstream** is "
        "the value in the **Workstream** column — ITS matches on *both*, so the same Setting name "
        "can appear under two Workstreams and mean two different rows."
    )
    lines.append(
        "- **Default** is what ITS uses when the row is **missing, blank, or unreadable** — the "
        "read resolves to this value rather than raising, and a *missing* row is logged loudly (a "
        "config that \"ships dark\" has no row to flip until it is seeded)."
    )
    lines.append(
        "- **\"Resolves to the default\" is not the same as \"fails open.\"** The direction depends "
        "entirely on what the default *is*, and for the send gates it is deliberately the **safe** "
        "one: `po_materials.po_send.polling_enabled`, `po_materials.rfq_send.polling_enabled` and "
        "`subcontracts.subcontract_send.polling_enabled` all default **false**, so a missing or "
        "unreadable row leaves that send daemon **disabled** — it never falls open into "
        "transmitting. (`safety_reports.weekly_send.polling_enabled` and "
        "`progress_reports.progress_send.polling_enabled` default **true**.) Read each row's own "
        "Default column; do not assume one direction across the board."
    )
    lines.append(
        "- **Read by** names the daemon(s) that resolve the key at runtime — where to look when a "
        "setting is not taking effect."
    )
    lines.append("")

    for ws in ordered_ws:
        lines.append(f"## {_workstream_heading(ws)}")
        lines.append("")
        lines.append("| Setting | Type | Default | Purpose | Read by |")
        lines.append("|---|---|---|---|---|")
        for k in sorted(by_ws[ws], key=lambda x: x.setting):
            read_by = ", ".join(k.read_by)
            lines.append(
                f"| `{_cell(k.setting)}` | {k.kind} | {_cell(_fmt_default(k.default, k.kind))} "
                f"| {_cell(k.purpose)} | {_cell(read_by)} |"
            )
        lines.append("")

    lines.append("## Where this comes from")
    lines.append("")
    lines.append(
        "Each daemon declares the keys it reads in a `REQUIRED_CONFIG` list in its own source "
        "file (the observable-config ledger, issue #336); the shared-infrastructure keys are read "
        "by shared helpers. This dictionary is the union of those declarations, so it stays in "
        "step with the code. To refresh it after a config change, run "
        "`python -m scripts.generate_config_dictionary` and re-record its sha256 in the enablement "
        "manifest."
    )
    lines.append("")
    return "\n".join(lines)


def render_json(keys: list[MergedKey]) -> str:
    """Render the machine-readable twin the WS2 dashboard consumes (deterministic bytes)."""
    payload = {
        "schema_version": JSON_SCHEMA_VERSION,
        "description": (
            "ITS_Config data dictionary — generated by scripts/generate_config_dictionary.py "
            "from the in-repo daemon REQUIRED_CONFIG declarations. Network-free, deterministic."
        ),
        "keys": [
            {
                "setting": k.setting,
                "workstream": k.workstream,
                "kind": k.kind,
                "default": k.default,
                "purpose": k.purpose,
                "read_by": list(k.read_by),
                "origin": k.origin,
            }
            for k in sorted(keys, key=lambda x: (x.workstream, x.setting))
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def _report_diagnostics(conflicts: list[str], gaps: list[str]) -> None:
    for c in conflicts:
        print(f"CONFLICT {c}", file=sys.stderr)
    for g in gaps:
        print(f"NO PURPOSE {g} — add a PURPOSE_OVERRIDES entry or a ConfigKey description",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.generate_config_dictionary",
        description="Generate the ITS_Config data dictionary (markdown + JSON) from the in-repo "
                    "daemon REQUIRED_CONFIG declarations. Deterministic + network-free.",
    )
    parser.add_argument("--check", action="store_true",
                        help="verify the committed outputs match a fresh generation (no writes)")
    parser.add_argument("--stdout", action="store_true",
                        help="print the markdown to stdout and write nothing")
    args = parser.parse_args(argv)

    keys, conflicts, gaps = collect_all()
    md = render_markdown(keys)
    js = render_json(keys)
    _report_diagnostics(conflicts, gaps)

    if args.stdout:
        print(md)
        return 2 if gaps else 0

    if args.check:
        stale = []
        for path, fresh in ((MD_OUT, md), (JSON_OUT, js)):
            committed = path.read_text(encoding="utf-8") if path.is_file() else None
            if committed != fresh:
                stale.append(path.relative_to(REPO_ROOT).as_posix())
        if stale:
            print(f"\nSTALE: {', '.join(stale)} — run "
                  "`python -m scripts.generate_config_dictionary` and re-record the manifest sha256.",
                  file=sys.stderr)
            return 1
        print(f"(config dictionary current: {len(keys)} keys)")
        return 2 if gaps else 0

    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text(md, encoding="utf-8")
    JSON_OUT.write_text(js, encoding="utf-8")
    print(f"wrote {MD_OUT.relative_to(REPO_ROOT)} + {JSON_OUT.relative_to(REPO_ROOT)} "
          f"({len(keys)} keys)")
    return 2 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
