"""System map (`/system`): registry parity teeth + route behavior.

The registry-parity tests are the anti-drift contract (HOUSE_REFLEXES §1
reconcile-every-registry): a NEW daemon (plist / TRACKED_JOBS marker) must get
a `system_map` node in the same PR, or these fail naming the missing piece.
Route tests are hermetic — every live join (Smartsheet, launchctl, heartbeat
files, the troubleshooting tree) is monkeypatched or verified fail-soft.
"""
from __future__ import annotations

import html
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from operator_dashboard import system_view
from operator_dashboard.app import create_app
from operator_dashboard.system_map import (
    BANDS,
    EDGES,
    LANES,
    NODE_BY_ERROR_SCRIPT,
    NODE_BY_LAUNCHD_LABEL,
    NODES,
    NODES_BY_ID,
)

_REPO = Path(__file__).resolve().parent.parent

# Captured at import time, BEFORE any fixture monkeypatches the module attr —
# lets the fail-soft test exercise the real permalink producer.
_REAL_SHEET_PERMALINK = system_view._sheet_permalink


# ── registry shape ───────────────────────────────────────────────────────────

def test_node_ids_unique_and_lanes_bands_valid() -> None:
    ids = [n.id for n in NODES]
    assert len(ids) == len(set(ids)), "duplicate node id"
    lanes = {lane_id for lane_id, _ in LANES}
    bands = {band_id for band_id, _ in BANDS}
    for n in NODES:
        assert n.lane in lanes, f"{n.id}: unknown lane {n.lane}"
        assert n.band in bands, f"{n.id}: unknown band {n.band}"
        assert n.blurb, f"{n.id}: empty blurb"


def test_edges_reference_real_nodes() -> None:
    for e in EDGES:
        assert e.src in NODES_BY_ID, f"edge src {e.src!r} has no node"
        assert e.dst in NODES_BY_ID, f"edge dst {e.dst!r} has no node"
        assert e.kind in ("push", "pull", "write", "read", "trigger", "send", "alert")


def test_send_edges_originate_only_from_send_half_nodes() -> None:
    # The map must never draw a transmission edge out of a generation-half node —
    # that would misrepresent the External Send Gate (Invariant 1).
    for e in EDGES:
        if e.kind == "send":
            assert NODES_BY_ID[e.src].send_half == "send", (
                f"send edge from non-send node {e.src}"
            )


# ── parity teeth (a new daemon must land here too) ───────────────────────────

def test_every_launchd_plist_label_has_a_node() -> None:
    plists = sorted(
        p.stem for p in (_REPO / "scripts" / "launchd").glob("org.solutionsmith.its.*.plist")
    )
    assert plists, "no plists found — repo layout changed?"
    missing = [lbl for lbl in plists if lbl not in NODE_BY_LAUNCHD_LABEL]
    assert not missing, (
        f"launchd labels with no system-map node: {missing} — add a node to "
        "operator_dashboard/system_map.py (registry reconciliation, HOUSE_REFLEXES §1)"
    )


def test_every_tracked_job_marker_has_a_node() -> None:
    # scripts/ is not a package — the repo's sys.path-insert idiom imports the
    # watchdog as top-level `watchdog` (a `from scripts import …` makes mypy see
    # the file under two module names; see tests/test_troubleshooting_tree.py).
    scripts_dir = _REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from watchdog import TRACKED_JOBS

    claimed = {n.marker for n in NODES if n.marker}
    missing = [m for m in TRACKED_JOBS if m not in claimed]
    assert not missing, (
        f"TRACKED_JOBS markers with no system-map node: {missing} — add a node (or a "
        "marker= join) in operator_dashboard/system_map.py"
    )


def test_every_send_half_node_has_an_inbound_human_approval_edge() -> None:
    """The Send Gate must be DRAWN, not merely implied.

    Invariant 1 says nothing transmits without a human approving that exact
    packet. On the map that contract IS the `port="human approval"` edge from a
    review sheet into the send-half node. A send node without one renders as a
    transmitter fed by nothing a human touched — the map would misrepresent the
    gate. (rfq_send shipped with only its ITS_Vendors recipient-lookup edge.)
    """
    approved = {e.dst for e in EDGES if e.port == "human approval"}
    missing = sorted(n.id for n in NODES if n.send_half == "send" and n.id not in approved)
    assert not missing, (
        f"send-half nodes with no inbound human-approval edge: {missing} — add the "
        'MapEdge(<review sheet>, <node>, "APPROVED rows only — F22", "read", '
        'port="human approval") so the map draws the External Send Gate crossing'
    )


def test_every_node_config_gate_is_reachable_in_the_config_editor() -> None:
    """A gate the map advertises must be a gate the operator can actually reach.

    Every `config_gate` / `extra_gates` key on a node is an ITS_Config row the
    operator is told controls that capability. If it is absent from the Class-A
    ACT registry the editor refuses to write it, so the map points at a switch
    that does not exist on the console — the "phantom gate" failure mode.
    Deliberate omissions go in gate_not_editable with a stated reason.
    """
    from operator_dashboard.act.registry import REGISTRY
    from operator_dashboard.system_map import gate_workstream

    # Gates deliberately NOT editable from the dashboard (each needs a reason).
    # These are surfaced READ-ONLY instead (registry.CLASS_E_DISPLAY), which the
    # companion test below asserts — "not editable" must never mean "invisible".
    gate_not_editable: dict[str, str] = {
        "po_materials.estimate_extract.tier1_enabled": "ADR-0004 extraction ladder — dark + unvalidated",
        "po_materials.estimate_extract.tier2_enabled": "ADR-0004 extraction ladder — dark + unvalidated",
        "po_materials.estimate_extract.ocr_enabled": "ADR-0004 extraction ladder — dark + unvalidated",
    }

    missing: list[str] = []
    for n in NODES:
        for gate in ([n.config_gate] if n.config_gate else []) + list(n.extra_gates):
            if gate in gate_not_editable:
                continue
            if (gate, gate_workstream(gate)) not in REGISTRY:
                missing.append(f"{n.id} -> {gate}")
    assert not missing, (
        f"system-map gates absent from the ACT config registry: {sorted(missing)} — add a "
        "ConfigEntry in operator_dashboard/act/registry.py (registry reconciliation, "
        "HOUSE_REFLEXES §1), or record a reason in gate_not_editable"
    )


def test_uneditable_node_gates_are_still_visible_read_only() -> None:
    """"Not editable" must never degrade into "invisible".

    A gate withheld from the config editor (an unvalidated AI extraction tier) is
    still state the operator must be able to READ — otherwise the console silently
    hides a capability switch. Every such gate has to appear in CLASS_E_DISPLAY,
    which renders the LIVE value with no edit control.
    """
    from operator_dashboard.act.registry import CLASS_E_DISPLAY

    displayed = {d.setting for d in CLASS_E_DISPLAY}
    ladder = [
        "po_materials.estimate_extract.tier1_enabled",
        "po_materials.estimate_extract.tier2_enabled",
        "po_materials.estimate_extract.ocr_enabled",
    ]
    missing = [g for g in ladder if g not in displayed]
    assert not missing, (
        f"gates withheld from the editor but not surfaced read-only: {missing} — add a "
        "DisplayEntry to CLASS_E_DISPLAY in operator_dashboard/act/registry.py"
    )
    # and they must NOT have leaked into the editable registry
    from operator_dashboard.act.registry import REGISTRY

    leaked = [g for g in ladder if (g, "po_materials") in REGISTRY]
    assert not leaked, (
        f"unvalidated extraction tiers became editable: {leaked} — promoting one is gated "
        "on scripts/eval_estimate_ladder.py qualifying a model on the production corpus"
    )
    # CLASS_E_DISPLAY and REGISTRY must stay DISJOINT: a key listed in both renders
    # twice on /config — once read-only, once with a live edit form — which reads as
    # "read-only" while still being writable.
    dual = sorted(
        d.setting for d in CLASS_E_DISPLAY if (d.setting, d.workstream) in REGISTRY
    )
    assert not dual, f"keys listed BOTH read-only and editable: {dual}"


def test_send_queue_panel_covers_every_review_sheet_feeding_a_send_node() -> None:
    """The send-queue panel must show EVERY lane's approval backlog.

    Its sheet list is hardcoded; a new send lane whose review sheet is omitted
    makes that lane's PENDING / HELD / FAILED rows invisible on the console while
    the panel still reads "all clear" — a silent blind spot, not an obvious gap.
    """
    import operator_dashboard.sources.smartsheet_panels as sp
    from shared import sheet_ids

    covered = {getattr(sheet_ids, attr, None) for _ws, attr in sp._SEND_QUEUE_SHEETS}
    missing = sorted(
        f"{NODES_BY_ID[e.src].label} (feeds {e.dst})"
        for e in EDGES
        if e.port == "human approval" and NODES_BY_ID[e.src].sheet_id not in covered
    )
    assert not missing, (
        f"review sheets feeding a send node but absent from the send-queue panel: {missing} "
        "— add them to _SEND_QUEUE_SHEETS in operator_dashboard/sources/smartsheet_panels.py"
    )


def test_runbook_paths_exist() -> None:
    for n in NODES:
        if n.runbook:
            assert (_REPO / n.runbook).is_file(), f"{n.id}: runbook {n.runbook} missing"


def test_docs_links_exist_and_are_servable() -> None:
    """Every extra doc link must point at a real file the /doc viewer can serve
    (only .md under the four allowlisted docs/ subdirs) — a dead rail link is
    worse than no link."""
    servable = ("runbooks", "enablement", "references", "troubleshooting")
    for n in NODES:
        for label, path in n.docs:
            assert label.strip(), f"{n.id}: docs entry with empty label"
            assert (_REPO / path).is_file(), f"{n.id}: doc {path} missing"
            parts = Path(path).parts
            assert parts[0] == "docs" and parts[1] in servable and path.endswith(".md"), (
                f"{n.id}: doc {path} is outside the /doc viewer's allowlist {servable}"
            )


def test_every_map_node_has_an_operator_brief() -> None:
    """Depth contract: EVERY node on the map must explain itself to a
    non-technical operator, not just the Smartsheet ones.

    A tile the operator can click but that answers no "what is this / what do I
    do" question is a dead end on the one page meant to teach the machine. This
    is also the registry tooth: a new daemon/sheet/surface ships its brief in
    the SAME PR (HOUSE_REFLEXES §1), or this fails naming it.
    """
    from operator_dashboard.node_briefs import NODE_BRIEFS

    missing = sorted(n.id for n in NODES if n.id not in NODE_BRIEFS)
    assert not missing, (
        f"map nodes with no operator brief: {missing} — add a NodeBrief in "
        "operator_dashboard/node_briefs.py"
    )
    orphans = sorted(k for k in NODE_BRIEFS if k not in NODES_BY_ID)
    assert not orphans, f"briefs keyed to nonexistent nodes: {orphans}"


def test_briefs_never_assert_live_state() -> None:
    """HOUSE_REFLEXES §5: static text states what a thing MEANS, never what it
    is currently set to — live state is one ITS_Config read away and drifts."""
    from operator_dashboard.node_briefs import NODE_BRIEFS

    # NB "at the moment of sending" is timing SEMANTICS (recipients resolve at
    # send time) — only bare tense-of-now phrases are banned.
    banned = ("currently", "ships dark", "as of 20", "right now")
    offenders = [
        f"{node_id}: {phrase!r}"
        for node_id, brief in NODE_BRIEFS.items()
        for phrase in banned
        if phrase in brief.what.lower() or phrase in brief.key_line.lower()
    ]
    assert not offenders, f"briefs asserting live state: {offenders}"


def test_briefs_keep_the_shape_the_rail_renders() -> None:
    """The rail renders `what` as paragraphs and `key_line` behind its label —
    both assumptions have to hold in the data, or a brief renders as one wall of
    text or as a bolded label with nothing after it.

    The length band is anti-slop teeth: the corpus reads as one voice only while
    every entry stays in the same register, and an unbounded brief drifts toward
    an essay in a 300px rail.

    Deliberately NOT asserted: that a paragraph opens "Day-to-day". It is the
    house convention (and documented in node_briefs.py), but the three
    borrowed-slot approval-desk briefs answer "what you do" in ¶1 and spend ¶2 on
    a schema caveat — pinning the phrasing would force a worse brief.
    """
    from operator_dashboard.node_briefs import NODE_BRIEFS

    problems: list[str] = []
    for node_id, brief in NODE_BRIEFS.items():
        paragraphs = brief.what.split("\n\n")
        if len(paragraphs) < 2:
            problems.append(f"{node_id}: {len(paragraphs)} paragraph(s), expected 2+")
        words = len(brief.what.split())
        if not 40 <= words <= 125:
            problems.append(f"{node_id}: {words} words, outside the 40-125 band")
        if brief.key_line and not brief.key_label:
            problems.append(f"{node_id}: key_line with no key_label — renders unlabeled")
        if brief.key_label and not brief.key_line:
            problems.append(f"{node_id}: key_label with no key_line — renders as a bare label")
        if len(brief.key_line.split()) > 32:
            problems.append(f"{node_id}: key_line is a paragraph, not a one-liner")
    assert not problems, f"briefs breaking the rail's rendering contract: {problems}"


def test_every_registered_watchdog_letter_is_badged_on_a_node() -> None:
    """Coverage contract: each registered watchdog check letter appears on the
    node it probes (so the map can answer "who watches this?" for everything).

    The letter set is pinned here against len(watchdog.CHECKS): adding or
    removing a check changes the count, fails this test, and forces BOTH the
    letter set and a node badge to be updated in the same PR.
    """
    scripts_dir = _REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from watchdog import CHECKS

    # 24 callables span 23 distinct letters (Check I has a safety + a progress
    # wrapper). E is deferred (Admin-API prerequisite) and not registered; F was
    # retired 2026-06-05; H never existed. Z joined 2026-08-17 (audit C-2).
    assert len(CHECKS) == 24, (
        "watchdog.CHECKS changed — update REGISTERED_LETTERS below AND badge the "
        "new check's subject node in system_map.py (watchdog_checks=...)"
    )
    registered_letters = set("ABCDGIJKLMNOPQRSTUVWXYZ")
    badged = {letter for n in NODES for letter in n.watchdog_checks}
    unbadged = sorted(registered_letters - badged)
    assert not unbadged, (
        f"registered watchdog letters on no node badge: {unbadged} — add the letter to "
        "its subject node's watchdog_checks in system_map.py"
    )
    unknown = sorted(badged - registered_letters)
    assert not unknown, f"node badges naming unregistered letters: {unknown}"


def test_every_live_sheet_constant_has_a_map_node() -> None:
    """Coverage contract: every live sheet in shared/sheet_ids.py is somewhere
    on the map — a sheet the system owns but the console never shows is an
    operational blind spot (the Orphaned-Reports class)."""
    from shared import sheet_ids

    # Deliberate exemptions — each with a stated reason.
    exempt = {
        "SHEET_TRUSTED_CONTACTS",    # 0 placeholder — dormant until Email Triage (Phase 1.4)
        "SHEET_WPR_PENDING_REVIEW",  # decommissioned (superseded by WSR_human_review)
        "SHEET_VENDOR_DB",           # decommissioned (superseded by ITS_Vendors)
        "SHEET_SUBCONTRACTOR_DB",    # retired in place (superseded by ITS_Subcontractors)
        "SHEET_EQUIPMENT_MASTER",    # aggregated in the registry_sheets node (no own chip)
    }
    node_sheet_ids = {n.sheet_id for n in NODES if n.sheet_id}
    missing = sorted(
        name
        for name in dir(sheet_ids)
        if name.startswith("SHEET_")
        and name not in exempt
        and isinstance(getattr(sheet_ids, name), int)
        and getattr(sheet_ids, name) > 0
        and getattr(sheet_ids, name) not in node_sheet_ids
    )
    assert not missing, (
        f"live sheet constants with no system-map node: {missing} — add a node (or a "
        "documented exemption) in operator_dashboard/system_map.py"
    )
    # Reverse: a node must never carry a sheet_id that sheet_ids.py doesn't know.
    known = {
        getattr(sheet_ids, name)
        for name in dir(sheet_ids)
        if name.startswith("SHEET_") and isinstance(getattr(sheet_ids, name), int)
    }
    rogue = sorted(n.id for n in NODES if n.sheet_id and n.sheet_id not in known)
    assert not rogue, f"nodes with sheet_ids unknown to shared/sheet_ids.py: {rogue}"


def test_script_paths_exist() -> None:
    for n in NODES:
        if n.script_path:
            assert (_REPO / n.script_path).exists(), f"{n.id}: script_path {n.script_path} missing"


def test_error_script_join_covers_the_known_identities() -> None:
    # Spot-lock the join keys the error panels rely on, including the two
    # UNDOTTED outliers (publish_daemon / config_actuator).
    for script, node in {
        "safety_reports.portal_poll": "portal_poll",
        "safety_reports.weekly_send_poll": "weekly_send",
        "po_materials.po_poll": "po_poll",
        "subcontracts.subcontract_send": "subcontract_send",
        "publish_daemon": "publish_daemon",
        "config_actuator": "config_actuator",
        "scripts.watchdog": "watchdog",
        "field_ops.fieldops_sync": "fieldops_sync",
        # ADR-0004 lane — rfq_send owns BOTH identities (poller + send module),
        # mirroring the po_send / subcontract_send shape.
        "po_materials.estimate_poll": "estimate_poll",
        "po_materials.rfq_poll": "rfq_poll",
        "po_materials.rfq_send_poll": "rfq_send",
        "po_materials.rfq_send": "rfq_send",
        # 2026-08-10 wiring pass: the receipts ledger (#38), the archive pass's
        # undotted identity, and the manifest importer — each had ITS_Errors rows
        # rendering with no /system?focus= link until joined here.
        "progress_reports.material_receipts": "trackers",
        "job_archive": "fieldops_sync",
        "field_ops.manifest_poll": "manifest_poll",
    }.items():
        assert NODE_BY_ERROR_SCRIPT.get(script) == node


def test_every_fieldops_pass_gate_is_on_the_node() -> None:
    """STRUCTURAL parity — the phantom-pass class. fieldops_sync grows a new mirror pass by
    adding a `field_ops.fieldops_sync.<pass>_enabled` ConfigKey; if the node's `extra_gates`
    is not updated in the same PR, the pass exists with no `/config?f=` deep link on the rail
    and the operator cannot find its switch (bit receipts_enabled AND archive_enabled,
    2026-08-10). Enumerating REQUIRED_CONFIG makes the NEXT pass fail here by name instead."""
    from field_ops import fieldops_sync  # noqa: PLC0415

    node = NODES_BY_ID["fieldops_sync"]
    declared_gates = {
        k.setting
        for k in fieldops_sync.REQUIRED_CONFIG
        if k.setting.startswith("field_ops.fieldops_sync.") and k.setting.endswith("_enabled")
    }
    on_node = {node.config_gate, *node.extra_gates}
    missing = declared_gates - on_node
    assert not missing, f"fieldops_sync pass gates missing from the system-map node: {missing}"


def test_manifest_poll_is_not_an_edgeless_orphan() -> None:
    """Until 2026-08-10 manifest_poll was the map's only EDGE-LESS daemon — drawn as a box
    that talks to nothing while live it pulls the pool, files to Box, posts the grid back,
    and tickets refusals. A daemon node with zero edges is a wiring miss, not a style choice."""
    touching = [e for e in EDGES if "manifest_poll" in (e.src, e.dst)]
    assert len(touching) >= 3, f"manifest_poll has only {len(touching)} edges"


# ── routes (hermetic) ────────────────────────────────────────────────────────

@pytest.fixture
def offline_joins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every live join returns empty — the map must render fully regardless."""
    monkeypatch.setattr(system_view, "_open_criticals_by_node", lambda: {})
    monkeypatch.setattr(system_view, "_launchd_state_by_node", lambda: {})
    monkeypatch.setattr(system_view, "_heartbeat_age_by_node", lambda: {})
    monkeypatch.setattr(system_view, "_gate_state_by_node", lambda: {})
    monkeypatch.setattr(system_view, "_troubleshoot_joins", lambda: {})
    monkeypatch.setattr(system_view, "_sheet_permalink", lambda _sheet_id: None)


def test_system_page_renders_every_node(offline_joins: None) -> None:
    client = TestClient(create_app())
    r = client.get("/system")
    assert r.status_code == 200
    for n in NODES:
        assert f'id="node-{n.id}"' in r.text, f"node {n.id} not rendered"
    # The two walls are structural — they must always be present.
    assert 'id="wall-send"' in r.text
    assert 'id="wall-ingress"' in r.text


def test_system_focus_param_marks_node(offline_joins: None) -> None:
    client = TestClient(create_app())
    r = client.get("/system?focus=portal_poll")
    assert r.status_code == 200
    assert 'data-focus="portal_poll"' in r.text
    # An unknown focus is dropped, never echoed back into the page.
    r = client.get("/system?focus=<script>alert(1)</script>")
    assert r.status_code == 200
    assert 'data-focus=""' in r.text


def test_node_rail_renders_and_unknown_is_soft(offline_joins: None) -> None:
    client = TestClient(create_app())
    r = client.get("/system/node/weekly_send")
    assert r.status_code == 200
    assert "send half — no AI" in r.text
    assert "/doc/runbooks/safety_weekly_send.md" in r.text
    r = client.get("/system/node/nope")
    assert r.status_code == 200
    assert "unknown node" in r.text


def test_sheet_node_rail_renders_brief_docs_and_permalink(
    offline_joins: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The depth surfaces: a sheet node's rail carries the operator brief, every
    doc link, and the Smartsheet out-link when the permalink resolves."""
    monkeypatch.setattr(
        system_view,
        "_sheet_permalink",
        lambda _sheet_id: "https://app.smartsheet.com/sheets/TESTTOKEN",
    )
    client = TestClient(create_app())
    r = client.get("/system/node/sheet_wsr")
    assert r.status_code == 200
    assert "What this is" in r.text                      # brief block header
    assert "approval desk for Weekly Safety Reports" in r.text
    assert "Key columns" in r.text
    assert 'href="https://app.smartsheet.com/sheets/TESTTOKEN"' in r.text
    assert "open in Smartsheet" in r.text
    # And a node with extra docs renders each link into the /doc viewer.
    r = client.get("/system/node/sheet_config")
    assert "/doc/references/its_config_dictionary.md" in r.text
    assert "/doc/runbooks/token_write_capability.md" in r.text


def test_every_node_rail_renders_its_brief(offline_joins: None) -> None:
    """End-to-end proof of the depth contract: click ANY tile — daemon, script,
    the portal, Box, the send door — and the rail answers "what is this / what do
    you do", not just the Smartsheet tiles.

    Asserted through the real route + template, so a data-side brief that the
    template never renders (a renamed field, a lost `{% if %}`) fails here.
    """
    from operator_dashboard.node_briefs import NODE_BRIEFS

    client = TestClient(create_app())
    for node in NODES:
        r = client.get(f"/system/node/{node.id}")
        assert r.status_code == 200, f"{node.id}: rail returned {r.status_code}"
        # Jinja autoescapes, so compare against the unescaped body — several
        # briefs open on an apostrophe ("The portal's back end").
        body = html.unescape(r.text)
        assert "What this is" in body, f"{node.id}: rail rendered no brief block"
        brief = NODE_BRIEFS[node.id]
        opener = brief.what.split(",")[0].split(" — ")[0][:40]
        assert opener in body, f"{node.id}: brief text absent from the rail"
        if brief.key_line:
            assert brief.key_label in body, f"{node.id}: key_line rendered without its label"


def test_non_sheet_node_rail_carries_a_labeled_key_line(offline_joins: None) -> None:
    """A daemon's at-a-glance row must render under its OWN label — the rail used
    to hardcode "Key columns", which is meaningless for a daemon."""
    client = TestClient(create_app())
    r = client.get("/system/node/po_poll")
    assert r.status_code == 200
    assert "Key signals" in r.text
    assert "Key columns" not in r.text


def test_permalink_fetch_is_fail_soft(offline_joins: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Smartsheet outage must degrade the rail to no out-link, never an error."""
    monkeypatch.setattr(system_view, "_sheet_permalink", _REAL_SHEET_PERMALINK)

    def bomb(_name: str) -> Any:
        raise RuntimeError("offline")

    monkeypatch.setattr(system_view.importlib, "import_module", bomb)
    client = TestClient(create_app())
    r = client.get("/system/node/sheet_wsr")
    assert r.status_code == 200
    assert "open in Smartsheet" not in r.text


def test_live_join_failure_degrades_to_plain_map(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every join helper swallows its own failure — force the underlying imports
    # to explode and assert the page still renders (fail-soft contract).
    import builtins

    real_import = builtins.__import__

    def bomb(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("shared.") or name == "troubleshooting.loader":
            raise RuntimeError("offline")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", bomb)
    client = TestClient(create_app())
    r = client.get("/system")
    assert r.status_code == 200
    assert 'id="wall-send"' in r.text


def test_open_criticals_rows_carry_map_links(monkeypatch: pytest.MonkeyPatch) -> None:
    import operator_dashboard.sources.smartsheet_panels as sp

    monkeypatch.setattr(
        sp,
        "_cached_error_rows",
        lambda: [
            {"_row_id": 1, "Timestamp": "2026-07-18", "Severity": "CRITICAL",
             "Script": "po_materials.po_poll", "Error": "uncaught_exception"},
            {"_row_id": 2, "Timestamp": "2026-07-18", "Severity": "CRITICAL",
             "Script": "some.retired_thing", "Error": "x"},
        ],
    )
    result = sp.OpenCriticalsSource().fetch()
    by_script = {r["Script"]: r for r in result.rows}
    assert by_script["po_materials.po_poll"]["_link_Script"] == "/system?focus=po_poll"
    assert "_link_Script" not in by_script["some.retired_thing"]  # no node → plain text


def test_troubleshoot_deep_link_renders_expanded() -> None:
    client = TestClient(create_app())
    r = client.get("/troubleshoot?wf=safety_report")
    assert r.status_code == 200
    assert "ts-workflow-open" in r.text  # expanded, not collapsed cards
    r = client.get("/troubleshoot?wf=not_a_workflow")
    assert r.status_code == 200
    assert "Nothing matches" in r.text


def test_pulse_renders_chips(monkeypatch: pytest.MonkeyPatch) -> None:
    # Panels themselves are fail-soft, so /pulse renders chips even offline.
    client = TestClient(create_app())
    r = client.get("/pulse")
    assert r.status_code == 200
    for name in ("daemons", "watchdog", "breaker", "criticals", "review", "sends"):
        assert name in r.text


def test_spanner_containers_pass_clicks_through() -> None:
    """Hit-target regression (2026-07-22): the Box spanner's transparent
    container overlays every per-band cell in its grid column (same grid-item
    z-index, later in DOM — a chip-level z-index cannot out-stack it) and
    swallowed clicks on all nine records-column sheet tiles. The fix is the
    pointer-events pass-through pair: the container ignores clicks, its own
    chip re-enables them. (True hit-testing needs a real browser — the live
    elementFromPoint sweep, 55/55 chips, is the bite test; this pins the
    load-bearing CSS so it can't silently vanish.)"""
    css = (_REPO / "operator_dashboard" / "static" / "app.css").read_text()
    span_rules = css.split(".sm-cell-span {", 1)[1].split("}", 1)[0]
    assert "pointer-events: none" in span_rules, "spanner container intercepts clicks again"
    assert ".sm-cell-span .sm-node { pointer-events: auto; }" in css, (
        "the spanner's own chip lost its pointer-events re-enable"
    )


def test_system_map_carries_no_numeric_sheet_id_literals():
    """The map reads sheet ids FROM shared/sheet_ids.py (single source of truth,
    operator directive 2026-07-23) — a numeric literal here would recreate the
    duplicated-ID surface the tenant rebuild had to remap. If a new node needs a
    sheet id, reference the sheet_ids constant (add one if missing)."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "operator_dashboard" / "system_map.py").read_text(encoding="utf-8")
    import re
    literals = re.findall(r"sheet_id=(\d+)", src)
    assert not literals, f"numeric sheet_id literals in system_map.py: {literals}"
