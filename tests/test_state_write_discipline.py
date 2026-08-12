"""Mechanically enforce the CLAUDE.md state-write rule (was review-only).

CLAUDE.md "What NOT to do":
  "Don't call Path.write_text or Path.write_bytes directly on any file under
   ~/its/state/. All state-file writes must go through shared/state_io.py helpers
   (atomic_write_json / atomic_write_text, wrapped in with_path_lock for
   read-modify-write triples on shared files). Direct write_text skips the
   atomic-write + lock guarantees and is rejected at review."

That rule lived only at review — forensic class #10 (non-atomic / in-place state
mutation: double external send, corruption races, audit-record clobber; worst
instance: an approved WSR emailed to the customer every 15 minutes on a transient
post-send write failure). This promotes it to a CI gate, mirroring the
capability-gating allowlist idiom in tests/test_capability_gating.py: NO module on
the runtime surface (the F02 WALKED_ROOTS, imported below — shared/ plus every
first-party workstream package) may call .write_text()/.write_bytes()
directly unless it is on STATE_WRITE_ALLOWLIST WITH a one-line rationale confirming
it is the sanctioned atomic-writer OR writes a NON-state path (a liveness marker,
a git-source artifact).

Run with: pytest -q tests/test_state_write_discipline.py
"""
from __future__ import annotations

import ast
from pathlib import Path

# Runtime surface walked — the same untrusted-content / daemon surface as the F02
# network allowlist (shared/ helpers + the workstream packages). scripts/ as a
# whole stays un-walked (operator-run one-shots), EXCEPT the tenant-lifecycle
# pair below: since #674 standup.py + wipe_tenant.py write the
# ~/its/state/standup_in_progress.json ACT-fence marker, so a future direct
# state write there must face this gate too (2026-07-23 verify pass).
#
# This used to be a hand-maintained COPY of the F02 list, with a comment claiming it was
# "kept in sync with the F02 WALKED_ROOTS in test_capability_gating.py" — it wasn't: it
# sat at 4 roots while F02 had grown to 7, leaving subcontracts/, field_ops/ and
# operator_dashboard/ (all live, all state-touching) walked by NEITHER guard. It is now
# IMPORTED from the F02 gate, so the claimed parity is structural and the two guards
# cannot drift again. (Coverage-gap audit, 2026-07-21.)
from tests.test_capability_gating import WALKED_ROOTS

# Individually-walked files outside WALKED_ROOTS (see the scripts/ note above).
EXTRA_WALKED_FILES: tuple[str, ...] = (
    "scripts/migrations/standup.py",
    "scripts/migrations/wipe_tenant.py",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

_WRITE_METHODS: frozenset[str] = frozenset({"write_text", "write_bytes"})

# Every direct .write_text()/.write_bytes() on the walked surface must be justified
# here. The reviewer adding an entry MUST confirm it either (a) IS the sanctioned
# atomic-writer, or (b) writes a path that is NOT under ~/its/state/.
STATE_WRITE_ALLOWLIST: dict[str, str] = {
    "shared/state_io.py":
        "the canonical atomic-write helper — temp-file write before os.replace IS its job",
    "safety_reports/compile_now_poll.py":
        "writes the ~/its/.watchdog/<job>.last_run liveness marker (NOT ~/its/state/)",
    "safety_reports/send_poll_core.py":
        "writes the ~/its/.watchdog/<job>.last_run liveness marker (NOT ~/its/state/) — "
        "the watchdog marker write moved here from weekly_send_poll.py in P1c",
    "safety_reports/generate_core.py":
        "writes the ~/its/.watchdog/<job>.last_run liveness marker (NOT ~/its/state/) — "
        "the watchdog marker write is parameterized here (P4); both weekly compiles use it "
        "(moved out of weekly_generate.py, which no longer writes directly)",
    "safety_reports/portal_poll.py":
        "writes the ~/its/.watchdog liveness marker (NOT ~/its/state/)",
    "safety_reports/publish_daemon.py":
        "writes the git-source forms catalog/definitions under safety_portal/forms/ AND the "
        "~/its/.watchdog/publish_daemon.last_run liveness marker (neither is ~/its/state/) — "
        "its ~/its/state/ writes (heartbeat files) all ride state_io",
    "po_materials/config_actuator.py":
        "writes the ~/its/.watchdog/config_actuator.last_run liveness marker (NOT ~/its/state/) — "
        "its git-source config/terms writes are delegated to config_apply.py (allowlisted below) "
        "and its ~/its/state/ writes (heartbeat files) all ride state_io",
    "po_materials/config_apply.py":
        "writes the git-source config + terms files (po_materials/config/*.json, po_materials/terms/*.md) "
        "that config_actuator commits + deploys (NOT ~/its/state/) — the config-editor analogue of "
        "publish_daemon's forms/catalog writes",
    "po_materials/po_poll.py":
        "writes the ~/its/.watchdog/po_poll.last_run liveness marker (NOT ~/its/state/) — "
        "its ~/its/state/ writes (po_poll_flagged.json + heartbeat files) all ride state_io",
    "po_materials/estimate_poll.py":
        "writes the ~/its/.watchdog/estimate_poll.last_run liveness marker (NOT ~/its/state/) — "
        "its ~/its/state/ writes (estimate_poll_flagged.json + heartbeat files) all ride state_io",
    "field_ops/manifest_poll.py":
        "writes the ~/its/.watchdog/manifest_poll.last_run liveness marker (NOT ~/its/state/) — "
        "its ~/its/state/ writes (manifest_poll_flagged.json + heartbeat files) all ride state_io",
    "field_ops/schedule_poll.py":
        "writes the ~/its/.watchdog/schedule_poll.last_run liveness marker (NOT ~/its/state/) — "
        "its ~/its/state/ writes (schedule_poll_flagged.json + heartbeat files) all ride state_io",
    "po_materials/rfq_poll.py":
        "writes the ~/its/.watchdog/rfq_poll.last_run liveness marker (NOT ~/its/state/) — "
        "its ~/its/state/ writes (rfq_poll_flagged.json + heartbeat files) all ride state_io",
    # The two below were surfaced by the 2026-07-21 widening (field_ops/ + subcontracts/
    # had never been walked). Both verified: single `_write_watchdog_marker()` writing
    # WATCHDOG_MARKER_DIR = ~/its/.watchdog, and every ~/its/state/ write in each module
    # already goes through state_io.with_path_lock + atomic_write_json.
    "field_ops/fieldops_sync.py":
        "writes the ~/its/.watchdog/<job>.last_run liveness marker (NOT ~/its/state/) — "
        "its ~/its/state/ writes (the pending-fetch-fail counter + heartbeat files) all "
        "ride state_io",
    "subcontracts/subcontract_poll.py":
        "writes the ~/its/.watchdog/<job>.last_run liveness marker (NOT ~/its/state/) — "
        "its ~/its/state/ writes (subcontract_flagged.json + heartbeat files) all ride "
        "state_io",
    "scripts/migrations/wipe_tenant.py":
        "writes the prewipe dump JSON under ~/its/logs/migrations/prewipe_* (NOT "
        "~/its/state/) — its one ~/its/state/ write (the #674 ACT-fence marker) rides "
        "state_io.atomic_write_json",
}


def _has_direct_write(path: Path) -> bool:
    """True iff the module statically calls ``<expr>.write_text(...)`` or
    ``<expr>.write_bytes(...)`` (an attribute-method call — the exact surface the
    CLAUDE.md rule forbids on state files)."""
    tree = ast.parse(path.read_text())
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _WRITE_METHODS
        for node in ast.walk(tree)
    )


def _modules_with_direct_write() -> set[str]:
    out: set[str] = set()
    for root in WALKED_ROOTS:
        root_dir = REPO_ROOT / root
        if not root_dir.is_dir():
            continue
        for path in sorted(root_dir.rglob("*.py")):
            if _has_direct_write(path):
                out.add(path.relative_to(REPO_ROOT).as_posix())
    for rel in EXTRA_WALKED_FILES:
        path = REPO_ROOT / rel
        if path.is_file() and _has_direct_write(path):
            out.add(rel)
    return out


def test_no_unallowlisted_direct_state_write():
    """No direct .write_text()/.write_bytes() on the runtime surface outside the
    allowlist (CLAUDE.md state-write rule, forensic class #10)."""
    violations = sorted(_modules_with_direct_write() - set(STATE_WRITE_ALLOWLIST))
    assert not violations, (
        "Direct .write_text()/.write_bytes() outside STATE_WRITE_ALLOWLIST "
        "(CLAUDE.md state-write rule, forensic class #10):\n"
        + "\n".join(f"  {v}" for v in violations)
        + "\n\nRoute every ~/its/state/ write through shared/state_io.py "
        "(atomic_write_json / atomic_write_text under with_path_lock). If this writes a "
        "NON-state path (a liveness marker, a git-source artifact), add the file to "
        "STATE_WRITE_ALLOWLIST WITH a one-line rationale confirming it is not a state write."
    )


def test_state_write_allowlist_has_no_stale_entries():
    """Every allowlisted file must still exist AND still do a direct write — a stale
    entry rubber-stamps nothing, so prune it (keeps the allowlist an honest list)."""
    writers = _modules_with_direct_write()
    for rel in sorted(STATE_WRITE_ALLOWLIST):
        assert (REPO_ROOT / rel).exists(), (
            f"STATE_WRITE_ALLOWLIST names a missing file: {rel} — prune it"
        )
        assert rel in writers, (
            f"{rel} no longer calls .write_text()/.write_bytes() — "
            "stale STATE_WRITE_ALLOWLIST entry, prune it"
        )
