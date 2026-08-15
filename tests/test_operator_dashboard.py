"""Smoke + fail-soft + escape/redaction tests for the operator dashboard (D1-1).

Also proves the read-only invariant in code: no route accepts a non-GET
method, and untrusted panel values render inert (HTML-escaped + redacted).
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from operator_dashboard.app import create_app
from operator_dashboard.sources import PANELS_BY_ID

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    # The Smartsheet panels share a process-wide TTL cache; clear it around
    # each test so a value cached by one test can't bleed into another.
    from operator_dashboard import cache

    cache._store.clear()
    yield
    cache._store.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_index_returns_200_with_all_panel_slots(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    for panel_id in PANELS_BY_ID:
        assert f"/panels/{panel_id}" in resp.text


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    # enriched: still starts with "ok" (KeepAlive probe) + carries the registry/
    # secret/panel counts so a booted-with-registries-intact state is visible.
    assert resp.text.startswith("ok")
    assert "registry_keys=" in resp.text and "panels=" in resp.text


@pytest.mark.parametrize("panel_id", list(PANELS_BY_ID))
def test_every_panel_renders_or_degrades_never_500(
    client: TestClient, panel_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the Smartsheet panels hermetic + fast: force their reads to raise
    # so they exercise the fail-soft ('unavailable') path instead of hitting
    # live Smartsheet. Local-file panels read the real ~/its tree (or degrade
    # if absent, e.g. in CI). Both must yield 200 — never a 500.
    import shared.review_queue as rq
    import shared.smartsheet_client as ss

    def _boom(*args: object, **kwargs: object) -> object:
        raise ConnectionError("network disabled in test")

    monkeypatch.setattr(ss, "get_rows", _boom, raising=False)
    monkeypatch.setattr(rq, "get_pending", _boom, raising=False)

    resp = client.get(f"/panels/{panel_id}")
    assert resp.status_code == 200
    assert "panel" in resp.text


def test_unknown_panel_degrades_not_crashes(client: TestClient) -> None:
    resp = client.get("/panels/does-not-exist")
    assert resp.status_code == 200
    assert "unknown panel" in resp.text


def test_untrusted_smartsheet_values_render_inert(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inject an adversarial cell value (a script tag) AND a secret-shaped
    # value into ITS_Errors, then prove the rendered HTML neutralizes both:
    # the <script> is HTML-escaped (autoescape) and the secret is redacted
    # (shared.redact) — neither reaches the browser live.
    import shared.smartsheet_client as ss
    from operator_dashboard import cache

    poison = [
        {
            "_row_id": 1,
            "Created At": "2026-07-10T00:00:00+00:00",
            "Severity": "ERROR",
            "Script": "evil",
            "Message": "<script>alert('xss')</script> password=hunter2",
        }
    ]
    cache._store.clear()  # the ITS_Errors fetch is TTL-cached; force a fresh read
    monkeypatch.setattr(ss, "get_rows", lambda sheet_id, **kw: list(poison))

    resp = client.get("/panels/errors_recent")
    assert resp.status_code == 200
    body = resp.text
    # XSS: the raw script tag must NOT appear; its escaped form must.
    assert "<script>alert('xss')</script>" not in body
    assert "&lt;script&gt;" in body
    # Secret: the redaction backstop masks the value.
    assert "hunter2" not in body
    assert "&lt;redacted&gt;" in body


def test_mutation_routes_are_the_expected_act_set() -> None:
    # The app has EXACTLY eleven mutating routes: Class-A edit, the elevated
    # Class-B edit, Class-C secret rotation, the Class-B interval edit (plist
    # re-install), Class-B daemon control (launchctl), the Class-B dashboard
    # self-restart (DASH-12), Class-B circuit-breaker clear, the two Class-B
    # error-log verbs (mark-resolved + clear), the Class-B review-queue resolve
    # (DASH-13), and the Class-C change-operator-PIN. Any other non-GET route is
    # a regression. (The send-queue/audit panels and the system map are GET-only
    # reads, so they do not appear here.)
    app = create_app()
    mutating: list[tuple[str, list[str]]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            continue  # e.g. the StaticFiles Mount has no fixed method set
        non_read = set(methods) - {"GET", "HEAD", "OPTIONS"}
        if non_read:
            mutating.append((getattr(route, "path", "?"), sorted(non_read)))
    assert sorted(mutating) == [
        ("/act/config", ["POST"]),
        ("/act/config/elevated", ["POST"]),
        ("/act/daemon/control", ["POST"]),
        ("/act/daemon/interval", ["POST"]),
        ("/act/dashboard/restart", ["POST"]),
        ("/act/errors/clear", ["POST"]),
        ("/act/errors/resolve", ["POST"]),
        ("/act/pin/change", ["POST"]),
        ("/act/review/resolve", ["POST"]),
        ("/act/secret/rotate", ["POST"]),
        ("/act/state/breaker-clear", ["POST"]),
    ], f"unexpected mutating routes: {mutating}"


def test_config_paths_mirror_live_shared_constants() -> None:
    # Drift guard: the dashboard's observation roots must equal the constants
    # owned by the shared modules (which resolve to ~/its/...). If those move,
    # this fails loudly instead of the panels silently reading the wrong tree.
    #
    # The LOGS side is compared to the genuine live path rather than to
    # `error_log.LOG_DIR`: the autouse `_redirect_live_log_dir` fixture repoints
    # that WRITE constant at tmp (so unit tests never append to the operator's
    # log), which would make an attribute-vs-attribute assert compare tmp to tmp.
    # The dashboard constant is deliberately NOT redirected — it is read-only —
    # so asserting it still resolves under ~/its is exactly the drift this guards.
    import shared.heartbeat as hb
    from operator_dashboard import config as dash_config

    # STATE_DIR is not redirected by any fixture, so the module-to-module compare
    # still bites directly.
    assert dash_config.STATE_DIR == hb.STATE_DIR
    # LOGS_DIR is, so compare both roots to the genuine live locations instead.
    assert dash_config.STATE_DIR.resolve() == (Path.home() / "its" / "state").resolve()
    assert dash_config.LOGS_DIR.resolve() == (Path.home() / "its" / "logs").resolve()


def test_heartbeats_cycles_join_survives_poll_suffix_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the row-id cache keys daemons as "<workstream>.<daemon>"
    # (e.g. 'safety_reports.weekly_send_poll') but the liveness file is
    # 'weekly_send_heartbeat.txt' — the cycles must still join despite the
    # '_poll' suffix mismatch (was silently blanking the busiest daemons).
    import shared.heartbeat as hb
    from operator_dashboard.sources.runtime_state import HeartbeatsSource

    (tmp_path / "weekly_send_heartbeat.txt").write_text("2026-07-10T00:00:00+00:00")
    (tmp_path / "portal_poll_heartbeat.txt").write_text("2026-07-10T00:00:00+00:00")
    (tmp_path / "heartbeat_row_ids.json").write_text(
        json.dumps(
            {
                "safety_reports.weekly_send_poll": {"row_id": 1, "total_cycles": 3533},
                "safety_reports.portal_poll": {"row_id": 2, "total_cycles": 40961},
            }
        )
    )
    monkeypatch.setattr(hb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hb, "HEARTBEAT_ROW_STATE_PATH", tmp_path / "heartbeat_row_ids.json")

    result = HeartbeatsSource().fetch()
    cycles_by_daemon = {r["daemon"]: r["cycles"] for r in result.rows}
    # '_poll'-suffix daemon joins despite the filename/cache-key name mismatch:
    assert cycles_by_daemon["weekly_send"] == "3533"
    # exact-match daemon still works:
    assert cycles_by_daemon["portal_poll"] == "40961"


def test_watchdog_panel_import_available_under_pytest() -> None:
    # Lock the watchdog panel's success path: `import scripts.watchdog` must
    # resolve (pinned to ITS_HOME on sys.path), so the panel renders rather
    # than degrading to 'unavailable' with a ModuleNotFoundError.
    from operator_dashboard.sources.watchdog_checks import WatchdogChecksSource

    result = WatchdogChecksSource().fetch()
    assert result.available, result.unavailable_reason


def test_send_queue_source_rolls_up_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # Read-only send-queue panel: buckets Send Status across the 4 review sheets,
    # FAILED drives error severity, HELD from a held_* status, PENDING counted.
    import shared.sheet_ids as sid
    import shared.smartsheet_client as ss
    from operator_dashboard.sources.smartsheet_panels import SendQueueSource

    rowsets = {
        sid.SHEET_WSR_HUMAN_REVIEW: [{"Send Status": "PENDING"}, {"Send Status": "SENT"}, {"Send Status": "held_oversized_packet"}],
        sid.SHEET_WPR_HUMAN_REVIEW: [{"Send Status": "FAILED"}],
        sid.SHEET_PO_PENDING_REVIEW: [],
        sid.SHEET_SUBCONTRACT_PENDING_REVIEW: [{"Send Status": "PENDING"}],
    }
    monkeypatch.setattr(ss, "get_rows", lambda sheet_id, **kw: list(rowsets.get(sheet_id, [])))
    result = SendQueueSource().fetch()
    assert result.available
    joined = " ".join(f"{r.get('status')}={r.get('count')}" for r in result.rows)
    assert "HELD" in joined and "FAILED" in joined and "PENDING" in joined
    assert result.severity == "error"  # a FAILED row makes the panel error-severity


def test_send_queue_source_fail_soft_per_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    # one unreachable sheet degrades to a "(unavailable)" row; the panel still renders
    import shared.sheet_ids as sid
    import shared.smartsheet_client as ss
    from operator_dashboard.sources.smartsheet_panels import SendQueueSource

    def get_rows(sheet_id: int, **kw: object) -> list[dict[str, str]]:
        if sheet_id == sid.SHEET_WSR_HUMAN_REVIEW:
            raise RuntimeError("sheet down")
        return [{"Send Status": "SENT"}]

    monkeypatch.setattr(ss, "get_rows", get_rows)
    result = SendQueueSource().fetch()
    assert result.available  # never crashes
    assert any(r.get("status") == "(unavailable)" for r in result.rows)


def test_procurement_lanes_panel_is_registered() -> None:
    # Registration tooth: the generic per-panel tests iterate PANELS_BY_ID, so
    # they cannot notice a panel silently DROPPED from the registry — this can.
    from operator_dashboard.sources.procurement_panel import ProcurementLanesSource

    assert "procurement_lanes" in PANELS_BY_ID
    assert isinstance(PANELS_BY_ID["procurement_lanes"], ProcurementLanesSource)


def test_procurement_lanes_rolls_up_status_and_change_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The read-only ledger panel: per lane, Status counts + a change-order census
    # (number contains "-CO" — a DISTINCT Track D2 document, not a duplicate) +
    # the most recent rows (number, status, modified), newest first.
    import shared.sheet_ids as sid
    import shared.smartsheet_client as ss
    from operator_dashboard.sources.procurement_panel import ProcurementLanesSource

    rowsets = {
        sid.SHEET_PO_LOG: [
            {"PO Number": "2026.104", "Status": "sent", "Last Modified": "2026-08-10T09:00:00"},
            {"PO Number": "2026.104-CO1", "Status": "pending_review", "Last Modified": "2026-08-14T12:00:00"},
            {"PO Number": "2026.105", "Status": "sent", "Last Modified": "2026-08-11T08:00:00"},
        ],
        sid.SHEET_SUBCONTRACT_LOG: [
            {"SC Number": "SC-2026-007", "Status": "executed", "Last Modified": "2026-08-01T10:00:00"},
            {"SC Number": "SC-2026-007-CO2", "Status": "draft", "Last Modified": "2026-08-15T07:00:00"},
        ],
        sid.SHEET_RFQ_LOG: [
            {"RFQ Number": "RFQ-2026-031", "Status": "responded", "Last Modified": "2026-08-12T11:00:00"},
        ],
    }
    monkeypatch.setattr(ss, "get_rows", lambda sheet_id, **kw: list(rowsets.get(sheet_id, [])))
    result = ProcurementLanesSource().fetch()

    assert result.available
    assert result.columns == ["lane", "kind", "item", "detail"]
    # Census + the understatement caveat naming the authoritative lifecycle read.
    assert "po 3" in result.summary and "subcontracts 2" in result.summary and "rfq 1" in result.summary
    assert "2 change orders" in result.summary
    assert "understate" in result.summary and "Procurement screen" in result.summary
    # Status rollup rows per lane.
    status_rows = {(r["lane"], r["item"]): r["detail"] for r in result.rows if r["kind"] == "status"}
    assert status_rows[("po", "sent")] == "2"
    assert status_rows[("po", "pending_review")] == "1"
    assert status_rows[("subcontracts", "executed")] == "1"
    assert status_rows[("rfq", "responded")] == "1"
    # Change-order census rows for the lanes that have one.
    co_rows = {r["lane"]: r["detail"] for r in result.rows if r["kind"] == "change orders"}
    assert co_rows == {"po": "1", "subcontracts": "1"}
    # Recent rows: newest first, detail = "status · modified".
    po_recent = [r for r in result.rows if r["lane"] == "po" and r["kind"] == "recent"]
    assert [r["item"] for r in po_recent] == ["2026.104-CO1", "2026.105", "2026.104"]
    assert po_recent[0]["detail"].startswith("pending_review · 2026-08-14")
    # A -CO recent row carries the not-a-duplicate tooltip (Tier-2 misread guard).
    assert "distinct document" in po_recent[0].get("_title_item", "")
    assert "_title_item" not in po_recent[1]


def test_procurement_lanes_fail_soft_per_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    # One unreachable ledger degrades to an "(unavailable)" row; the other lanes
    # still render and the panel never crashes the dashboard.
    import shared.sheet_ids as sid
    import shared.smartsheet_client as ss
    from operator_dashboard.sources.procurement_panel import ProcurementLanesSource

    def get_rows(sheet_id: int, **kw: object) -> list[dict[str, str]]:
        if sheet_id == sid.SHEET_SUBCONTRACT_LOG:
            raise RuntimeError("sheet down")
        return [{"PO Number": "2026.104", "RFQ Number": "RFQ-1", "Status": "sent"}]

    monkeypatch.setattr(ss, "get_rows", get_rows)
    result = ProcurementLanesSource().fetch()

    assert result.available  # never crashes
    unavailable = [r["lane"] for r in result.rows if r["kind"] == "(unavailable)"]
    assert unavailable == ["subcontracts"]
    assert any(r["lane"] == "po" and r["kind"] == "status" for r in result.rows)
    assert any(r["lane"] == "rfq" and r["kind"] == "status" for r in result.rows)
    assert "subcontracts ?" in result.summary


def _clear_panel_cache() -> None:
    import operator_dashboard.cache as cache

    with cache._lock:
        cache._store.clear()


def test_box_roots_panel_all_canonical_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five configured roots, five live folders under their canonical names → OK."""
    import shared.box_client as box
    import shared.smartsheet_client as ss
    from operator_dashboard.sources.box_roots import CANONICAL_ROOTS, BoxRootsSource

    ids = {setting: f"id-{i}" for i, (_n, setting, _w) in enumerate(CANONICAL_ROOTS)}
    names = {f"id-{i}": name for i, (name, _s, _w) in enumerate(CANONICAL_ROOTS)}
    monkeypatch.setattr(ss, "get_setting", lambda key, workstream: ids[key])
    monkeypatch.setattr(box, "get_folder_name", lambda fid: names[fid])
    _clear_panel_cache()

    result = BoxRootsSource().fetch()

    assert result.available and result.severity == "ok"
    assert len(result.rows) == 5
    assert all(r["status"] == "ok" for r in result.rows)


def test_box_roots_panel_missing_row_and_dead_id_are_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty config row and an id that resolves to nothing both drive ERROR —
    the two states where filings hold or would file into a void."""
    import shared.box_client as box
    import shared.smartsheet_client as ss
    from operator_dashboard.sources.box_roots import BoxRootsSource

    def get_setting(key: str, workstream: str) -> str | None:
        if key == "subcontracts.box.portal_root_folder_id":
            # A genuinely MISSING row RAISES (the real client contract — the
            # review caught the earlier mock returning None here, which modeled
            # a seam get_setting does not have).
            raise ss.SmartsheetNotFoundError("no row")
        if key == "field_ops.box.archive_root_folder_id":
            return None  # row present, Value blank
        return "id-x"

    def get_folder_name(fid: str) -> str:
        raise box.BoxNotFoundError("404")

    monkeypatch.setattr(ss, "get_setting", get_setting)
    monkeypatch.setattr(box, "get_folder_name", get_folder_name)
    _clear_panel_cache()

    result = BoxRootsSource().fetch()

    assert result.severity == "error"
    statuses = {r["root"]: r["status"] for r in result.rows}
    assert "MISSING" in statuses["ITS Subcontracts"]
    assert "EMPTY" in statuses["ITS Archive"]
    assert "NO folder" in statuses["ITS Safety Reports"]
    # Rows keep _sev for the template's per-row tinting (house convention).
    assert all("_sev" in r for r in result.rows)


def test_box_roots_panel_rename_is_warn_and_box_outage_never_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved-but-renamed root WARNs (legitimate but worth eyes); a Box
    transport failure degrades that root's row without failing the panel."""
    import shared.box_client as box
    import shared.smartsheet_client as ss
    from operator_dashboard.sources.box_roots import BoxRootsSource

    def get_folder_name(fid: str) -> str:
        if fid.endswith("safety_reports"):
            raise box.BoxError("503")
        return "Renamed By Operator"

    monkeypatch.setattr(ss, "get_setting", lambda key, workstream: f"id-{workstream}")
    monkeypatch.setattr(box, "get_folder_name", get_folder_name)
    _clear_panel_cache()

    result = BoxRootsSource().fetch()

    assert result.available and result.severity == "warn"
    statuses = " ".join(r["status"] for r in result.rows)
    assert "renamed" in statuses and "Box unreachable" in statuses


def test_box_roots_canonical_set_matches_the_builders() -> None:
    """Parity tooth: the panel's local canonical list must equal
    standup.BOX_ROOT_CONFIG_ROWS (the build/seed source of truth) — the heavy
    sys.path import happens HERE, not in the panel (scripts/migrations is not a
    package; a dashboard panel must not import a tenant-lifecycle migration)."""
    import importlib
    import sys

    migrations_dir = str(_REPO_ROOT / "scripts" / "migrations")
    if migrations_dir not in sys.path:
        sys.path.insert(0, migrations_dir)
    standup = importlib.import_module("standup")
    from operator_dashboard.sources.box_roots import CANONICAL_ROOTS

    # Exact equality: standup's seed tuple carries all five roots (the four
    # portal roots + the Track 6 archive root), and the panel watches the same set.
    assert set(CANONICAL_ROOTS) == set(standup.BOX_ROOT_CONFIG_ROWS)


def test_open_criticals_panel_counts_only_open_criticals(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fire-surface panel: OPEN CRITICAL = CRITICAL with a blank "Resolved At" (the canonical
    # errors_rotation predicate). A resolved CRITICAL and every WARN/ERROR are terminal → excluded.
    import shared.smartsheet_client as ss
    from operator_dashboard import cache
    from operator_dashboard.sources.smartsheet_panels import OpenCriticalsSource

    rows = [
        {"Severity": "CRITICAL", "Resolved At": "", "Script": "a", "Error": "x", "Timestamp": "2026-07-01", "_row_id": 1},
        {"Severity": "CRITICAL", "Resolved At": "", "Script": "a", "Error": "x", "Timestamp": "2026-07-02", "_row_id": 2},
        {"Severity": "CRITICAL", "Resolved At": "2026-07-03", "Script": "b", "Error": "y", "Timestamp": "2026-07-01", "_row_id": 3},  # resolved → terminal
        {"Severity": "WARN", "Resolved At": "", "Script": "c", "Error": "z", "Timestamp": "2026-07-01", "_row_id": 4},  # WARN → terminal
    ]
    cache._store.clear()
    monkeypatch.setattr(ss, "get_rows", lambda sheet_id, **kw: list(rows))
    p = OpenCriticalsSource().fetch()
    assert p.severity == "error"
    assert "2 open CRITICAL" in p.summary
    assert p.rows == [{"Script": "a", "Error": "x", "Count": "2", "Oldest": "2026-07-01", "_sev": "error"}]

    # a backlog with no OPEN criticals (all resolved / WARN) reads green + "0 open — clear"
    cache._store.clear()
    monkeypatch.setattr(ss, "get_rows", lambda sheet_id, **kw: [rows[2], rows[3]])
    clear = OpenCriticalsSource().fetch()
    assert clear.severity == "ok" and clear.summary == "0 open — clear" and clear.rows == []


def test_daemon_running_with_signal_exit_is_ok_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A live pid = healthy NOW; a prior signal exit (-15 SIGTERM = graceful restart) must NOT
    # paint a RUNNING daemon red. A loaded-but-NOT-running daemon with a bad exit stays ERROR.
    from operator_dashboard.sources.daemons import DaemonStatusSource

    src = DaemonStatusSource()
    monkeypatch.setattr(src, "_plist_labels", lambda: [
        "org.solutionsmith.its.dashboard", "org.solutionsmith.its.foo",
    ])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.dashboard": ("55622", "-15"),  # running + SIGTERM last-exit
        "org.solutionsmith.its.foo": ("-", "1"),              # NOT running + error exit
    })
    monkeypatch.setattr(src, "_uptime_by_pid", lambda pids: {})
    by = {r["daemon"]: r for r in src.fetch().rows}
    assert by["dashboard"]["_sev"] == "ok"        # running → OK despite -15
    assert by["dashboard"]["state"] == "running"
    # The raw "-15" is alarming and meaningless to an operator: a RUNNING daemon's
    # last-exit describes the PREVIOUS instance. Render a neutral signal label and
    # keep the raw truth in the tooltip.
    assert by["dashboard"]["last exit"] == "signal (SIGTERM)"
    assert "-15" in by["dashboard"]["_title_last exit"]
    assert by["foo"]["_sev"] == "error" and "exited 1" in by["foo"]["state"]


def test_daemon_last_exit_label_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only a RUNNING daemon's NEGATIVE (signal) last-exit is relabelled. A positive
    # exit code stays raw (it is a real prior failure), a stopped daemon keeps its
    # red "exited -15" (a genuine did-not-restart signal), and an unknown signal
    # number degrades to a generic label rather than raising.
    from operator_dashboard.sources.daemons import DaemonStatusSource

    src = DaemonStatusSource()
    monkeypatch.setattr(src, "_plist_labels", lambda: [])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.pos": ("101", "2"),       # running + positive prior exit
        "org.solutionsmith.its.stopped": ("-", "-15"),   # loaded, NOT running, signal exit
        "org.solutionsmith.its.weird": ("102", "-99"),   # running + unknown signal number
        "org.solutionsmith.its.clean": ("103", "0"),     # running + clean prior exit
    })
    monkeypatch.setattr(src, "_uptime_by_pid", lambda pids: {})
    by = {r["daemon"]: r for r in src.fetch().rows}

    assert by["pos"]["last exit"] == "2" and "_title_last exit" not in by["pos"]
    assert by["stopped"]["last exit"] == "-15"
    assert by["stopped"]["state"] == "exited -15" and by["stopped"]["_sev"] == "error"
    assert "_title_last exit" not in by["stopped"]
    assert by["weird"]["last exit"] == "signal (99)"
    assert "-99" in by["weird"]["_title_last exit"]
    assert by["clean"]["last exit"] == "0" and "_title_last exit" not in by["clean"]


def _patch_ps(
    monkeypatch: pytest.MonkeyPatch, dmod: ModuleType, run: Callable[..., object]
) -> None:
    # Patch the module attribute the daemons panel actually calls through
    # (`daemons.subprocess`), NOT `subprocess.run` on the shared stdlib module
    # object — the latter swaps `run` process-wide for every other importer.
    # `SubprocessError` rides along because the narrowed except-clause in
    # `_uptime_by_pid` resolves it off this same attribute.
    monkeypatch.setattr(
        dmod,
        "subprocess",
        SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError),
    )
    # The shared stdlib module object stays untouched — this RED-lights if the
    # patch is ever widened back to `setattr(dmod.subprocess, "run", ...)`.
    assert subprocess.run is not run


@pytest.mark.parametrize(
    ("etime", "expected"),
    [
        ("00:00", "0s"),
        ("12:34", "12m 34s"),
        ("01:02:03", "1h 2m"),
        ("03-17:08:42", "3d 17h"),
    ],
)
def test_parse_etime_forms(etime: str, expected: str) -> None:
    # `ps -o etime=` renders [[dd-]hh:]mm:ss — a just-respawned interval daemon
    # shows mm:ss, a same-day KeepAlive server hh:mm:ss, and only a long-lived
    # one dd-hh:mm:ss. All three forms must parse or the column silently
    # degrades to raw ps strings for most rows.
    from operator_dashboard.sources.base import fmt_timedelta
    from operator_dashboard.sources.daemons import _parse_etime

    td = _parse_etime(etime)
    assert td is not None
    assert fmt_timedelta(td) == expected


def test_parse_etime_rejects_garbage() -> None:
    from operator_dashboard.sources.daemons import _parse_etime

    assert _parse_etime("not-an-etime") is None
    assert _parse_etime("") is None


def test_daemon_uptime_column(monkeypatch: pytest.MonkeyPatch) -> None:
    # Uptime comes from ONE batched ps call covering EVERY running pid (two
    # here, so a per-row implementation would make two calls and fail the
    # single-call assertion); idle / not-loaded rows show a dash.
    from operator_dashboard.sources import daemons as dmod

    src = dmod.DaemonStatusSource()
    monkeypatch.setattr(src, "_plist_labels", lambda: ["org.solutionsmith.its.gone"])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.up": ("101", "0"),
        "org.solutionsmith.its.up2": ("102", "0"),
        "org.solutionsmith.its.idle": ("-", "0"),
    })
    seen: list[list[str]] = []

    def _fake_run(argv: list[str], **kw: object) -> object:
        seen.append(argv)
        return SimpleNamespace(stdout="  101 03-17:08:42\n  102 12:34\n", returncode=0)

    _patch_ps(monkeypatch, dmod, _fake_run)
    result = src.fetch()
    by = {r["daemon"]: r for r in result.rows}
    assert "uptime" in result.columns
    assert by["up"]["uptime"] == "3d 17h"
    assert by["up2"]["uptime"] == "12m 34s"
    assert by["idle"]["uptime"] == "—"
    assert by["gone"]["uptime"] == "—"
    # ONE call is the load-bearing claim (a per-row implementation makes two).
    # Assert the pid SET, not its order — sorting or de-duplicating the pid list
    # would be a legitimate implementation choice, and pinning the literal
    # "101,102" would RED-light on it for no real reason.
    assert len(seen) == 1
    assert seen[0][0] == "ps"
    assert set(seen[0][-1].split(",")) == {"101", "102"}


def test_daemon_uptime_unparseable_and_empty_and_failsoft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unparseable etime degrades to the raw string; a ps failure yields no
    # uptimes but still renders the panel; and with NO running pids the
    # subprocess is skipped entirely (`ps -p` with an empty list errors — on
    # this host it writes non-UTF8 bytes and raises UnicodeDecodeError).
    from operator_dashboard.sources import daemons as dmod

    src = dmod.DaemonStatusSource()
    monkeypatch.setattr(src, "_plist_labels", lambda: [])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.up": ("101", "0"),
    })
    _patch_ps(
        monkeypatch,
        dmod,
        lambda argv, **kw: SimpleNamespace(stdout="101 not-an-etime\n", returncode=0),
    )
    by = {r["daemon"]: r for r in src.fetch().rows}
    assert by["up"]["uptime"] == "not-an-etime"

    def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("ps unavailable")

    _patch_ps(monkeypatch, dmod, _boom)
    by = {r["daemon"]: r for r in src.fetch().rows}
    assert by["up"]["uptime"] == "—" and by["up"]["state"] == "running"


def test_daemon_uptime_guard_is_narrow_not_blanket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The subprocess guard is deliberately a NARROW tuple — (OSError,
    # SubprocessError, UnicodeDecodeError), the three real `ps` boundary
    # failures. Widening it to a blanket `except Exception` would silently
    # swallow a PROGRAMMING bug at the call site (a bad argv type, a wrong
    # keyword) into a permanent, traceless "—" on a panel whose whole job is
    # "never silent". Pin that here: an exception OUTSIDE the tuple must
    # propagate and surface as a visibly unavailable panel via
    # DataSource.fetch()'s fail-soft wrapper.
    from operator_dashboard.sources import daemons as dmod

    src = dmod.DaemonStatusSource()
    monkeypatch.setattr(src, "_plist_labels", lambda: [])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.up": ("101", "0"),
    })

    def _bad_call(*args: object, **kwargs: object) -> object:
        raise ValueError("embedded null byte in argv")

    _patch_ps(monkeypatch, dmod, _bad_call)
    result = src.fetch()
    assert result.available is False
    assert "ValueError" in (result.unavailable_reason or "")

    # And the converse, so the tuple is pinned from BOTH sides: each member of
    # the narrow tuple really is absorbed (panel keeps its real content).
    for exc in (
        OSError("ps missing"),
        subprocess.SubprocessError("ps blew up"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ):

        def _raise(*args: object, _e: BaseException = exc, **kwargs: object) -> object:
            raise _e

        _patch_ps(monkeypatch, dmod, _raise)
        absorbed = src.fetch()
        assert absorbed.available is True, exc
        by = {r["daemon"]: r for r in absorbed.rows}
        assert by["up"]["uptime"] == "—"
        assert by["up"]["state"] == "running"


def test_daemon_uptime_parse_bug_is_visible_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The subprocess guard must NOT swallow the parse path: a programming bug
    # there would otherwise render as a permanent silent "—" with no trace on a
    # panel whose whole job is "never silent". It must surface as a visibly
    # unavailable panel via DataSource.fetch()'s fail-soft wrapper instead.
    from operator_dashboard.sources import daemons as dmod

    src = dmod.DaemonStatusSource()
    monkeypatch.setattr(src, "_plist_labels", lambda: [])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.up": ("101", "0"),
    })
    _patch_ps(
        monkeypatch,
        dmod,
        lambda argv, **kw: SimpleNamespace(stdout="101 03-17:08:42\n", returncode=0),
    )

    def _bug(etime: str) -> object:
        raise TypeError("signature changed")

    monkeypatch.setattr(dmod, "_parse_etime", _bug)
    result = src.fetch()
    assert result.available is False
    assert "TypeError" in (result.unavailable_reason or "")


def test_daemon_uptime_no_running_pids_skips_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The empty-pid guard is load-bearing, so prove the SKIP directly: a
    # call-recording fake that would happily succeed must record ZERO calls.
    # (Without `if not pids: return {}` this records one call and RED-lights.)
    from operator_dashboard.sources import daemons as dmod

    src = dmod.DaemonStatusSource()
    calls: list[list[str]] = []

    def _recording_run(argv: list[str], **kw: object) -> object:
        calls.append(argv)
        return SimpleNamespace(stdout="", returncode=0)

    _patch_ps(monkeypatch, dmod, _recording_run)
    assert src._uptime_by_pid([]) == {}
    assert calls == []

    # And end-to-end: an all-idle table never reaches ps either.
    monkeypatch.setattr(src, "_plist_labels", lambda: [])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.idle": ("-", "0"),
    })
    by = {r["daemon"]: r for r in src.fetch().rows}
    assert by["idle"]["uptime"] == "—"
    assert calls == []


def test_daemon_panel_renders_last_exit_tooltip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end: the per-column '_title_<column>' convention reaches the HTML as a
    # quoted title attribute carrying the raw launchctl value.
    from operator_dashboard.sources import PANELS_BY_ID
    from operator_dashboard.sources.daemons import DaemonStatusSource

    src = PANELS_BY_ID["daemons"]
    assert isinstance(src, DaemonStatusSource)
    monkeypatch.setattr(src, "_plist_labels", lambda: [])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.dashboard": ("55622", "-15"),
    })
    monkeypatch.setattr(src, "_uptime_by_pid", lambda pids: {"55622": "2h 5m"})

    resp = client.get("/panels/daemons")
    assert resp.status_code == 200
    assert 'title="raw launchctl last-exit -15' in resp.text
    assert "signal (SIGTERM)" in resp.text
    assert "2h 5m" in resp.text


def test_daemon_detail_view_renders_last_exit_tooltip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The '_title_<column>' expression is DUPLICATED in two templates — the
    # panel card (_panel.html) and the full-page drill-down (view.html). The
    # card path is covered above; cover the drill-down too, or an edit that
    # touches only one copy ships a half-working convention with a green suite.
    from operator_dashboard.sources import PANELS_BY_ID
    from operator_dashboard.sources.daemons import DaemonStatusSource

    src = PANELS_BY_ID["daemons"]
    assert isinstance(src, DaemonStatusSource)
    monkeypatch.setattr(src, "_plist_labels", lambda: [])
    monkeypatch.setattr(src, "_launchctl_table", lambda: {
        "org.solutionsmith.its.dashboard": ("55622", "-15"),
    })
    monkeypatch.setattr(src, "_uptime_by_pid", lambda pids: {"55622": "2h 5m"})

    resp = client.get("/view/daemons")
    assert resp.status_code == 200
    assert 'title="raw launchctl last-exit -15' in resp.text
    assert "signal (SIGTERM)" in resp.text
    assert "2h 5m" in resp.text
    # The sibling '_link_<column>' convention shares the same duplicated
    # expression — assert the drill-down still emits a deep link, so a future
    # edit to view.html cannot drop one branch while keeping the other.
    assert 'class="cell-link"' in resp.text


def test_audit_trail_source_filters_to_config_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    # the ACT audit panel shows only the config editor's own rows (accountability
    # where the actions happen), and surfaces denials in the summary.
    import shared.smartsheet_client as ss
    from operator_dashboard.sources.smartsheet_panels import AuditTrailSource

    rows = [
        {"Script": "operator_dashboard.config_editor", "Error": "config_audit", "Message": "edit", "Severity": "WARN"},
        {"Script": "some.other.daemon", "Error": "other_noise", "Message": "noise", "Severity": "ERROR"},
        {"Script": "operator_dashboard.config_editor", "Error": "config_denied", "Message": "denied", "Severity": "WARN"},
    ]
    monkeypatch.setattr(ss, "get_rows", lambda sheet_id, **kw: list(rows))
    result = AuditTrailSource().fetch()
    assert result.available
    joined = " ".join(r.get("Error", "") for r in result.rows)
    assert "config_audit" in joined and "config_denied" in joined
    assert "other_noise" not in joined  # non-config-editor rows filtered out
    assert "denied" in result.summary  # a denial is surfaced in the summary


def test_manifest_and_icon_served_for_dock_install(client: TestClient) -> None:
    # Installable-as-a-Dock-app assets: the web-app manifest (correct content-type)
    # + the Evergreen-crest icon both serve, so Safari "Add to Dock" / Chrome
    # "Install" produce a standalone window (and the stray favicon 404 is gone).
    m = client.get("/manifest.json")
    assert m.status_code == 200
    assert "application/manifest+json" in m.headers.get("content-type", "")
    assert '"display": "standalone"' in m.text and "ITS" in m.text
    icon = client.get("/static/favicon.png")
    assert icon.status_code == 200
    assert icon.headers.get("content-type", "").startswith("image/")


def test_drilldown_view_shows_more_rows_than_panel(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Clicking a panel title opens /view/{panel_id} full-page with detail=True: the
    # capped panels (errors) return far more rows than the 25-row summary card.
    import shared.smartsheet_client as ss
    from operator_dashboard import cache

    cache._store.clear()
    rows = [{"Severity": "WARN", "Message": f"e{i}", "Script": "d"} for i in range(300)]
    monkeypatch.setattr(ss, "get_rows", lambda sheet_id, **kw: list(rows))
    detail = client.get("/view/errors_recent")
    assert detail.status_code == 200
    # the banner-extension back nav is the way out of a drill-down (Dock app has
    # no browser back button)
    assert "← Back to dashboard" in detail.text and 'class="subnav__back"' in detail.text
    assert "rows shown" in detail.text
    # detail cap (500) renders all 300; the panel card caps at 25
    assert detail.text.count('<tr class="sev-') == 300
    cache._store.clear()
    monkeypatch.setattr(ss, "get_rows", lambda sheet_id, **kw: list(rows))
    card = client.get("/panels/errors_recent")
    assert card.text.count('<tr class="sev-') == 25
    # the card title is a drill-down link; an unknown panel is fail-soft
    assert 'href="/view/errors_recent"' in card.text
    assert client.get("/view/nonexistent").status_code == 200


def test_asset_urls_are_content_versioned_and_html_is_no_cache() -> None:
    # Regression for BOTH Safari Dock-app cache failures: the blank page (cached
    # HTML shell + stale stylesheet) and the 2026-07-22 stale-map/config-tab
    # incident (Safari web apps import Safari's HTTP cache at creation and
    # revalidate `no-cache` pages unreliably). Every stylesheet/script URL must
    # carry the content-hash version, and page HTML must be `no-store` — never
    # cached at all, not merely revalidated.
    from operator_dashboard.app import ASSET_VERSION

    assert len(ASSET_VERSION) == 10 and all(c in "0123456789abcdef" for c in ASSET_VERSION)
    client = TestClient(create_app())
    r = client.get("/")
    assert f"/static/app.css?v={ASSET_VERSION}" in r.text
    assert f"/static/htmx.min.js?v={ASSET_VERSION}" in r.text
    assert r.headers["cache-control"] == "no-store"
    r = client.get("/system")
    assert f"/static/system-map.js?v={ASSET_VERSION}" in r.text
    assert r.headers["cache-control"] == "no-store"
    # Static assets are exempt — their URLs are versioned, so they may cache.
    r = client.get(f"/static/app.css?v={ASSET_VERSION}")
    assert r.status_code == 200
    assert r.headers.get("cache-control") not in ("no-cache", "no-store")


def test_watchdog_sweep_panel_reads_results_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sweep panel renders the per-check verdicts from the watchdog's
    results file: INFO renders ok, WARN colors the row + panel, and the
    summary counts the sweep."""
    import importlib

    from operator_dashboard.sources.watchdog_checks import WatchdogSweepSource

    wd = importlib.import_module("scripts.watchdog")

    payload = {
        "run_at": datetime.now(UTC).isoformat(),
        "alerts_suppressed": False,
        "results": [
            {"check": "_check_open_criticals", "letter": "B", "severity": "INFO",
             "summary": "0 open CRITICALs"},
            {"check": "_check_stale_held_rows", "letter": "T", "severity": "WARN",
             "summary": "1 stale HELD row"},
        ],
    }
    results_file = tmp_path / "watchdog_results.json"
    results_file.write_text(json.dumps(payload))
    monkeypatch.setattr(wd, "WATCHDOG_RESULTS_PATH", results_file, raising=False)

    result = WatchdogSweepSource().fetch()
    assert result.available, result.unavailable_reason
    assert result.severity == "warn"
    rows = {r["check"]: r for r in result.rows}
    assert rows["B · open_criticals"]["result"] == "ok"
    assert rows["T · stale_held_rows"]["result"] == "WARN"
    assert "2 checks" in result.summary
    assert "1 WARN" in result.summary


def test_watchdog_sweep_panel_missing_and_stale_degrade_softly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib

    from operator_dashboard.sources.watchdog_checks import WatchdogSweepSource

    wd = importlib.import_module("scripts.watchdog")

    # Missing file → informational (first run hasn't happened), never an error.
    monkeypatch.setattr(wd, "WATCHDOG_RESULTS_PATH", tmp_path / "absent.json", raising=False)
    result = WatchdogSweepSource().fetch()
    assert result.available
    assert result.severity == "info"
    assert "no sweep results" in result.summary

    # A sweep older than the daily cadence (+slack) is itself a warning even
    # when every check passed — "green but ancient" must not read as green.
    stale = {
        "run_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
        "alerts_suppressed": True,
        "results": [
            {"check": "_check_open_criticals", "letter": "B", "severity": "INFO",
             "summary": "0 open"},
        ],
    }
    results_file = tmp_path / "watchdog_results.json"
    results_file.write_text(json.dumps(stale))
    monkeypatch.setattr(wd, "WATCHDOG_RESULTS_PATH", results_file, raising=False)
    result = WatchdogSweepSource().fetch()
    assert result.severity == "warn"
    assert "MAINTENANCE sweep" in result.summary
