"""Structural + blast-radius tests for the four Phase-1 cutover gap-builders.

The modules under test (`scripts/migrations/`):

    build_system_workspace.py        (D1) "ITS — System" workspace + 4 folders
    build_safety_portal_workspace.py (D2) "ITS –– Safety Portal" workspace + 2 folders
    build_system_sheets.py           (D3) the 5 ITS — System sheets
    build_box_roots.py               (D4) the 2 ITS Box root folders

These builders run ONCE, by hand, against a CUSTOMER'S PRODUCTION tenant that
already holds Evergreen's live content. There is no staging rehearsal and no undo,
so the controls that matter are structural, and this module is where they bite:

  * CREATE-ONLY (invariant 1) — the mutating-verb scan below parses each module
    with `ast`, blanks every string literal (so a docstring saying "never renames"
    cannot mask a real `requests.put`), and greps the resulting CODE-ONLY text.
    `test_mutating_verb_scanner_detects_a_planted_violation` plants a synthetic
    `requests.put(...)` and proves the scanner RED-lights — the scan is not vacuous.
  * FIND -> ADOPT, NO CREATE (invariant 2) and IDEMPOTENT NO-OP (invariant 5) —
    every adopt-path test asserts the create mock was never called.
  * DRY-RUN writes nothing AND never prompts (invariant 6): the confirmation seam
    is monkeypatched to a raiser under --dry-run, so a prompt is a test failure.
  * DUPLICATE-NAME AMBIGUITY IS LOUD (invariant 8) — five sheets named "ITS_Errors"
    coexist in the live "02 — Logs" folder and only one is the live one. A silent
    first-match adopt is the failure mode; these tests assert the [WARN] names every
    matching id and that nothing is created.
  * D3 schema parity — the builder's columns must be a SUPERSET of what the writer
    modules actually address (titles derived from the writers' own source, not
    hardcoded here), its PICKLIST option sets must be SET-EQUAL to the
    `picklist_validation.REGISTRY` sets that gate those writes (the #247->#253
    class), and its key->title map must cover exactly the 12
    `sheet_ids.DAEMON_HEALTH_COLUMNS` keys (heartbeat writes are column-id-keyed).

All external calls are mocked — nothing here touches Smartsheet, Box or Keychain.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# sys.path-driven import (scripts/ has no __init__.py) — mirrors tests/test_po_s1_sheets.py.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _REPO_ROOT / "scripts" / "migrations"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))

import build_box_roots as d4  # noqa: E402
import build_safety_portal_workspace as d2  # noqa: E402
import build_system_sheets as d3  # noqa: E402
import build_system_workspace as d1  # noqa: E402

from shared import picklist_validation, sheet_ids  # noqa: E402

# The four modules under test, as source paths. Derived once, asserted non-empty and
# existing below, and iterated by the source-scanning guards — so a scan can never
# silently degrade into "checked nothing".
MODULE_PATHS: tuple[Path, ...] = (
    _MIGRATIONS_DIR / "build_system_workspace.py",
    _MIGRATIONS_DIR / "build_safety_portal_workspace.py",
    _MIGRATIONS_DIR / "build_system_sheets.py",
    _MIGRATIONS_DIR / "build_box_roots.py",
)


# =========================================================================
# Fake Smartsheet REST tenant (D1 / D2 / D3)
# =========================================================================


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeTenant:
    """Stateful stand-in for the Smartsheet REST surface the builders read.

    Routes the three GET shapes the builders issue (`/workspaces?includeAll=true`,
    `/workspaces/<id>`, `/folders/<id>`) against in-memory state, so the builders'
    §45 re-find-after-create sees the object the create just added. `get` / `post`
    are MagicMocks, so a test can assert `post` was NEVER called.
    """

    def __init__(
        self,
        *,
        workspaces: list[dict[str, Any]] | None = None,
        folders: dict[int, list[dict[str, Any]]] | None = None,
        sheets: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.workspaces: list[dict[str, Any]] = list(workspaces or [])
        self.folders: dict[int, list[dict[str, Any]]] = {
            k: list(v) for k, v in (folders or {}).items()
        }
        self.sheets: dict[int, list[dict[str, Any]]] = {
            k: list(v) for k, v in (sheets or {}).items()
        }
        self._next_id = 900_000_000_000_001
        self.get = MagicMock(side_effect=self._get)
        self.post = MagicMock(side_effect=self._post)

    def new_id(self) -> int:
        self._next_id += 2
        return self._next_id

    def _get(self, url: str, **_kw: Any) -> _FakeResponse:
        if "/workspaces?" in url:
            return _FakeResponse({"data": self.workspaces})
        if "/workspaces/" in url:
            wid = int(url.rsplit("/", 1)[-1])
            return _FakeResponse({"folders": self.folders.get(wid, [])})
        if "/folders/" in url:
            fid = int(url.rsplit("/", 1)[-1])
            return _FakeResponse({"sheets": self.sheets.get(fid, [])})
        raise AssertionError(f"unexpected GET {url!r}")

    def _post(self, url: str, **kw: Any) -> _FakeResponse:
        assert url.endswith("/workspaces"), f"unexpected POST {url!r}"
        # A workspace you CREATE, you OWN — D1/D2 hard-stop the create-into path on any
        # adopted workspace whose accessLevel is not "OWNER" (the sandbox-shared-into-
        # production trap), so a freshly created one must report OWNER or the idempotent
        # second-run adopt would refuse the object this same tenant just minted.
        new = {"id": self.new_id(), "name": kw["json"]["name"], "accessLevel": "OWNER"}
        self.workspaces.append(new)
        return _FakeResponse({"result": {"id": new["id"]}})

    # -- helpers the smartsheet_client stubs use to keep state coherent ----

    def add_folder(self, workspace_id: int, name: str) -> int:
        fid = self.new_id()
        self.folders.setdefault(workspace_id, []).append({"id": fid, "name": name})
        return fid

    def add_sheet(self, folder_id: int, name: str) -> int:
        sid = self.new_id()
        self.sheets.setdefault(folder_id, []).append({"id": sid, "name": name})
        return sid


def _install_tenant(monkeypatch: pytest.MonkeyPatch, module: Any, tenant: FakeTenant) -> None:
    """Point a builder's `requests` + auth headers at the fake tenant (no Keychain)."""
    monkeypatch.setattr(module, "requests", tenant)
    monkeypatch.setattr(module, "_headers", lambda: {})


def _no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ANY confirmation prompt an immediate test failure (dry-run assertions)."""

    def _boom(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("confirmation seam was invoked under --dry-run")

    monkeypatch.setattr(d1, "_confirm", _boom)
    monkeypatch.setattr(d3, "_confirm", _boom)
    monkeypatch.setattr(d4, "_confirm_live_writes", _boom)
    # D2 prompts through builtins.input directly (no named seam).
    monkeypatch.setattr("builtins.input", _boom)


def _answer(monkeypatch: pytest.MonkeyPatch, yes: bool) -> None:
    """Answer every builder's confirmation seam without touching stdin."""
    monkeypatch.setattr(d1, "_confirm", lambda _p: yes)
    monkeypatch.setattr(d3, "_confirm", lambda _p: yes)
    # D4's gate grew a second arg (the Box login it names in the prompt) — accept variadic.
    monkeypatch.setattr(d4, "_confirm_live_writes", lambda *_a: yes)
    monkeypatch.setattr("builtins.input", lambda _p="": "y" if yes else "n")


def _argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["builder", *args])


def _stub_folder_create(
    monkeypatch: pytest.MonkeyPatch, module: Any, tenant: FakeTenant
) -> MagicMock:
    mock = MagicMock(side_effect=tenant.add_folder)
    monkeypatch.setattr(module.smartsheet_client, "create_folder_in_workspace", mock)
    return mock


def _stub_sheet_create(monkeypatch: pytest.MonkeyPatch, tenant: FakeTenant) -> MagicMock:
    mock = MagicMock(side_effect=lambda folder_id, name, _cols: tenant.add_sheet(folder_id, name))
    monkeypatch.setattr(d3.smartsheet_client, "create_sheet_in_folder", mock)
    return mock


def _stub_whoami(
    monkeypatch: pytest.MonkeyPatch,
    login: str | None = None,
    name: str = "ITS Service Account",
) -> MagicMock:
    """Point D4's identity probe (`d4._whoami`) at a fixed `(login, name)`.

    `_whoami` fetches the authenticated Box account (`GET /users/me`) — a live call the
    hermetic-conftest guard would reject — so every D4 test that reaches `build_roots` must
    stub it. `login=None` defaults to `EXPECTED_BOX_LOGIN`, the normal-flow identity (no
    mismatch WARN). Pass a different login to drive the `box_identity_mismatch` path.
    """
    mock = MagicMock(return_value=(login or d4.EXPECTED_BOX_LOGIN, name))
    monkeypatch.setattr(d4, "_whoami", mock)
    return mock


def _stub_box_root(monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]]) -> MagicMock:
    """Point D4's auth probe at a fake Box root listing (and stub the identity probe).

    The builder's probe is `d4._list_box_root` (it forces the HTTP GET inside
    box_client's translation frame via the module-private `box_client._call` — the
    §42 lazy-iteration workaround), NOT `box_client.list_folder`. So the probe seam to
    mock is `_list_box_root`; monkeypatching `list_folder` would leave the real client
    to open a live network connection (the hermetic-conftest guard then fails the test).

    Also stubs `d4._whoami` to the EXPECTED login by DEFAULT, so every existing D4 test that
    reaches `build_roots` stays hermetic + on the no-mismatch happy path. Tests exercising a
    mismatch / whoami failure re-stub `_whoami` (last monkeypatch wins) AFTER this call.
    """
    mock = MagicMock(return_value=items)
    monkeypatch.setattr(d4, "_list_box_root", mock)
    _stub_whoami(monkeypatch)
    return mock


# ---- canonical fixtures mirroring the LIVE tenant shape ------------------

_WS_SYSTEM_ID = 2730369263921028
_SYSTEM_FOLDER_IDS = {
    d3.FOLDER_CONFIG: 1775005051709316,
    d3.FOLDER_LOGS: 6278604679079812,
    d3.FOLDER_QUEUES: 4026804865394564,
    d3.FOLDER_DAEMONS: 4871229795526532,
}
_WS_PORTAL_ID = 6820552519247748

# A distinctive permalink for the sandbox-shared-into-production workspace the ownership
# guard must refuse. Asserted verbatim in the not-owned tests so a builder that stops
# surfacing it (the operator's ONLY eyeball discriminator between the sandbox and the
# production plan) red-lights.
_SANDBOX_PERMALINK = "https://app.smartsheet.com/workspaces/SANDBOX-shared-into-prod"


def _ws(
    ws_id: int, name: str, *, access: str | None = "OWNER", permalink: str | None = None
) -> dict[str, Any]:
    """Build a workspace object for FakeTenant with an EXPLICIT accessLevel + permalink.

    The convergence pass made `accessLevel` load-bearing across D1/D2/D3: a workspace
    whose accessLevel is not "OWNER" (or is absent) hard-stops the create-into path — the
    sandbox-shared-into-production trap, one exact-name match so the ambiguity WARN never
    fires. FakeTenant already routes whatever dict it is handed straight through `_get`, so
    this helper only makes the ownership discriminators explicit at the seed site: pass
    `access="VIEWER"` / `access="EDITOR"` / `access=None` to drive a fail-closed adopt, and
    a `permalink` the test can then assert the builder surfaced. `access=None` seeds an
    accessLevel of literal None (the API-omitted-the-field case).
    """
    obj: dict[str, Any] = {"id": ws_id, "name": name, "accessLevel": access}
    if permalink is not None:
        obj["permalink"] = permalink
    return obj


def _system_tenant(*, with_sheets: bool = False) -> FakeTenant:
    """A tenant where the D1 workspace + its four folders already exist."""
    sheets: dict[int, list[dict[str, Any]]] = {}
    if with_sheets:
        for name, folder_name, _const, _cols in d3.SHEETS:
            fid = _SYSTEM_FOLDER_IDS[folder_name]
            sheets.setdefault(fid, []).append({"id": 10_000 + len(sheets), "name": name})
    return FakeTenant(
        # accessLevel=OWNER: D1's adopt path hard-stops on a non-OWNER workspace (the
        # sandbox-shared-into-production ownership check). D3 ignores accessLevel.
        workspaces=[{"id": _WS_SYSTEM_ID, "name": d1.WORKSPACE_NAME, "accessLevel": "OWNER"}],
        folders={
            _WS_SYSTEM_ID: [{"id": fid, "name": n} for n, fid in _SYSTEM_FOLDER_IDS.items()]
        },
        sheets=sheets,
    )


def _stub_daemon_health_columns(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    live = [
        {"id": 700_000 + i, "title": title}
        for i, (_key, title) in enumerate(d3.DAEMON_HEALTH_KEY_TO_TITLE)
    ]
    mock = MagicMock(return_value=live)
    monkeypatch.setattr(d3.smartsheet_client, "list_columns_with_options", mock)
    return mock


# =========================================================================
# 1 + 3. FIND -> ADOPT, NO CREATE / IDEMPOTENT RE-RUN
# =========================================================================


def test_d1_adopts_existing_workspace_and_folders_without_creating(monkeypatch, capsys):
    tenant = _system_tenant()
    _install_tenant(monkeypatch, d1, tenant)
    folder_create = _stub_folder_create(monkeypatch, d1, tenant)
    _answer(monkeypatch, True)  # would be allowed — must never be reached
    _argv(monkeypatch)

    assert d1.main() == 0

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert out.count("[skip]") == 5  # workspace + four folders
    assert str(_WS_SYSTEM_ID) in out


def test_d1_second_run_is_a_pure_no_op(monkeypatch, capsys):
    """IDEMPOTENT NO-OP: run against an empty tenant, then re-run — the second run
    creates nothing and reports the SAME ids the first run minted."""
    tenant = FakeTenant()
    _install_tenant(monkeypatch, d1, tenant)
    folder_create = _stub_folder_create(monkeypatch, d1, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d1.main() == 0
    first = capsys.readouterr().out
    assert tenant.post.call_count == 1
    assert folder_create.call_count == 4

    tenant.post.reset_mock()
    folder_create.reset_mock()
    assert d1.main() == 0
    second = capsys.readouterr().out

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    minted = set(re.findall(r"folder_id=(\d+)", first))
    assert minted and minted == set(re.findall(r"folder_id=(\d+)", second))


def test_d2_adopts_existing_workspace_and_both_folders(monkeypatch, capsys):
    tenant = FakeTenant(
        workspaces=[{"id": _WS_PORTAL_ID, "name": d2.WORKSPACE_NAME, "accessLevel": "OWNER"}],
        folders={
            _WS_PORTAL_ID: [
                {"id": 2261538947000196, "name": "00_Safety Portal"},
                {"id": 6765138574370692, "name": "00_Form Catalog"},
            ]
        },
    )
    _install_tenant(monkeypatch, d2, tenant)
    folder_create = _stub_folder_create(monkeypatch, d2, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d2.main() == 0

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert "2261538947000196" in out and "6765138574370692" in out


def test_d2_workspace_name_uses_two_en_dashes_not_an_em_dash():
    """Byte-exactness is the whole find: an em-dash here CREATES A DUPLICATE workspace
    in the customer's production plan (the live name is verified U+2013 U+2013)."""
    assert d2.WORKSPACE_NAME == "ITS –– Safety Portal"
    assert "—" not in d2.WORKSPACE_NAME
    assert d1.WORKSPACE_NAME == "ITS — System" == d3.WORKSPACE_NAME
    for folder_name, _const in d1.FOLDERS:
        assert "—" in folder_name and "–" not in folder_name
    for folder_name in d2.CANONICAL_FOLDER_NAMES:
        assert folder_name.isascii(), folder_name


def test_d3_adopts_existing_sheets_without_creating(monkeypatch, capsys):
    tenant = _system_tenant(with_sheets=True)
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 0

    sheet_create.assert_not_called()
    tenant.post.assert_not_called()
    out = capsys.readouterr().out
    assert out.count("[skip] sheet") == 5


def test_d3_second_run_is_a_pure_no_op(monkeypatch, capsys):
    tenant = _system_tenant()
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 0
    first = capsys.readouterr().out
    assert sheet_create.call_count == 5

    sheet_create.reset_mock()
    assert d3.main() == 0
    second = capsys.readouterr().out

    sheet_create.assert_not_called()
    tenant.post.assert_not_called()
    minted = set(re.findall(r"sheet_id=(\d+)", first))
    assert len(minted) == 5
    assert minted == set(re.findall(r"sheet_id=(\d+)", second))


def test_d4_adopts_existing_roots_without_creating(monkeypatch, capsys):
    root = [
        {"type": "folder", "id": "111", "name": "ITS Safety Reports"},
        {"type": "folder", "id": "222", "name": "ITS Progress Reports"},
        {"type": "folder", "id": "333", "name": "ITS Archive"},
        {"type": "folder", "id": "333", "name": "Evergreen Ops"},
    ]
    _stub_box_root(monkeypatch, root)
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d4.main() == 0

    create.assert_not_called()
    out = capsys.readouterr().out
    assert "folder_id=111" in out and "folder_id=222" in out
    assert "safety_reports.box.portal_root_folder_id" in out
    assert "progress_reports.box.portal_root_folder_id" in out


def test_d4_second_run_is_a_pure_no_op(monkeypatch):
    root = [
        {"type": "folder", "id": "111", "name": "ITS Safety Reports"},
        {"type": "folder", "id": "222", "name": "ITS Progress Reports"},
        {"type": "folder", "id": "333", "name": "ITS Archive"},
    ]
    _stub_box_root(monkeypatch, root)
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d4.main() == 0
    assert d4.main() == 0
    create.assert_not_called()


def test_d4_ignores_a_same_named_file_at_the_box_root(monkeypatch):
    """Exact-name adopt is folder-typed: a FILE named "ITS Safety Reports" must not be
    adopted as the root (Box allows a file and a folder to share a name)."""
    root = [{"type": "file", "id": "999", "name": "ITS Safety Reports"}]
    assert d4.find_root_matches(root, "ITS Safety Reports") == []


# =========================================================================
# 2. CREATE-BRANCH PAYLOADS
# =========================================================================


def test_d1_create_branch_uses_exact_names_and_the_new_workspace_as_parent(monkeypatch):
    tenant = FakeTenant()
    _install_tenant(monkeypatch, d1, tenant)
    folder_create = _stub_folder_create(monkeypatch, d1, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d1.main() == 0

    (_url,), kwargs = tenant.post.call_args
    assert kwargs["json"] == {"name": "ITS — System"}
    new_ws_id = tenant.workspaces[0]["id"]
    assert [c.args for c in folder_create.call_args_list] == [
        (new_ws_id, "01 — Config"),
        (new_ws_id, "02 — Logs"),
        (new_ws_id, "03 — Queues"),
        (new_ws_id, "04 — Daemons"),
    ]


def test_d2_create_branch_uses_exact_names_and_the_new_workspace_as_parent(monkeypatch):
    tenant = FakeTenant()
    _install_tenant(monkeypatch, d2, tenant)
    folder_create = _stub_folder_create(monkeypatch, d2, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d2.main() == 0

    (_url,), kwargs = tenant.post.call_args
    assert kwargs["json"] == {"name": d2.WORKSPACE_NAME}
    new_ws_id = tenant.workspaces[0]["id"]
    assert [c.args for c in folder_create.call_args_list] == [
        (new_ws_id, "00_Safety Portal"),
        (new_ws_id, "00_Form Catalog"),
    ]


def test_d3_create_branch_targets_the_right_folder_with_the_right_columns(monkeypatch):
    tenant = _system_tenant()
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    columns_readback = _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 0

    calls = {c.args[1]: c.args for c in sheet_create.call_args_list}
    assert set(calls) == {
        "ITS_Config", "ITS_Errors", "ITS_Quarantine", "ITS_Review_Queue", "ITS_Daemon_Health",
    }
    for sheet_name, folder_name, _const, columns in d3.SHEETS:
        folder_id, _name, passed = calls[sheet_name]
        assert folder_id == _SYSTEM_FOLDER_IDS[folder_name], sheet_name
        # The payload is a _cap_descriptions COPY of the schema (errorCode 1041
        # cap), so compare structure, not identity: same column order/titles/
        # types/options, and every description within the API limit.
        assert [(c["title"], c.get("type"), c.get("options")) for c in passed] == \
            [(c["title"], c.get("type"), c.get("options")) for c in columns], sheet_name
        assert all(
            len(c.get("description") or "") <= d3.SMARTSHEET_COLUMN_DESCRIPTION_MAX
            for c in passed), sheet_name
        titles = [c["title"] for c in passed]
        assert len(titles) == len(set(titles)), sheet_name
        assert sum(1 for c in passed if c.get("primary")) == 1, sheet_name
        assert passed[0].get("primary") is True, sheet_name
    # The column-id read-back is what keeps heartbeats from going silently dark.
    columns_readback.assert_called_once()


def test_d3_never_creates_a_workspace_or_a_folder(monkeypatch, capsys):
    """D3's blast radius stops at sheets: a missing folder is a REFUSAL with a pointer
    to D1, never a create (invariant 3 — scoped creation)."""
    # accessLevel=OWNER so the run reaches the folder-resolution path this test targets —
    # the converged ownership guard would otherwise fail closed on an absent accessLevel
    # FIRST (that fail-closed path has its own test below).
    tenant = FakeTenant(
        workspaces=[{"id": _WS_SYSTEM_ID, "name": d3.WORKSPACE_NAME, "accessLevel": "OWNER"}]
    )
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 1  # every sheet FAILED

    tenant.post.assert_not_called()
    sheet_create.assert_not_called()
    out = capsys.readouterr().out
    assert "folder_not_found" in out
    assert "build_system_workspace.py" in out


def test_d4_create_branch_parents_at_the_box_root(monkeypatch):
    _stub_box_root(monkeypatch, [])
    create = MagicMock(side_effect=["555", "666", "777"])
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d4.main() == 0
    assert [c.args for c in create.call_args_list] == [
        ("0", "ITS Safety Reports"),
        ("0", "ITS Progress Reports"),
        ("0", "ITS Archive"),
    ]


# =========================================================================
# 4 + 5. DRY-RUN CREATES NOTHING AND NEVER PROMPTS
# =========================================================================


@pytest.mark.parametrize("module", [d1, d2, d3], ids=["d1", "d2", "d3"])
def test_smartsheet_builders_dry_run_creates_nothing_and_never_prompts(
    module, monkeypatch, capsys
):
    tenant = FakeTenant() if module is not d3 else _system_tenant()
    _install_tenant(monkeypatch, module, tenant)
    folder_create = MagicMock()
    monkeypatch.setattr(module.smartsheet_client, "create_folder_in_workspace", folder_create)
    sheet_create = MagicMock()
    monkeypatch.setattr(module.smartsheet_client, "create_sheet_in_folder", sheet_create)
    _no_prompt(monkeypatch)
    _argv(monkeypatch, "--dry-run")

    assert module.main() == 0

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    sheet_create.assert_not_called()
    assert "[dry-run]" in capsys.readouterr().out


def test_d4_dry_run_makes_no_box_create_call_at_all(monkeypatch, capsys):
    probe = _stub_box_root(monkeypatch, [])
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _no_prompt(monkeypatch)
    _argv(monkeypatch, "--dry-run")

    assert d4.main() == 0

    create.assert_not_called()
    # The ONLY Box call a dry run may make is the read probe (_list_box_root).
    probe.assert_called_once()
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "No API create was attempted" in out


# =========================================================================
# 6. CONFIRMATION DECLINE
# =========================================================================


def test_d1_decline_creates_nothing_and_exits_zero(monkeypatch, capsys):
    tenant = FakeTenant()
    _install_tenant(monkeypatch, d1, tenant)
    folder_create = _stub_folder_create(monkeypatch, d1, tenant)
    _answer(monkeypatch, False)
    _argv(monkeypatch)

    assert d1.main() == 0

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    assert "declined" in capsys.readouterr().out.lower()


def test_d2_decline_creates_nothing_and_exits_zero(monkeypatch, capsys):
    tenant = FakeTenant()
    _install_tenant(monkeypatch, d2, tenant)
    folder_create = _stub_folder_create(monkeypatch, d2, tenant)
    _answer(monkeypatch, False)
    _argv(monkeypatch)

    assert d2.main() == 0

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    assert "declined" in capsys.readouterr().out.lower()


def test_d3_decline_creates_nothing_and_exits_zero(monkeypatch, capsys):
    """A decline must stop ALL FIVE sheets, not just the first: the gate caches the
    answer, so nothing at all is created."""
    tenant = _system_tenant()
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, False)
    _argv(monkeypatch)

    assert d3.main() == 0

    sheet_create.assert_not_called()
    tenant.post.assert_not_called()
    assert "declined" in capsys.readouterr().out.lower()


def test_d4_decline_creates_nothing_and_exits_zero(monkeypatch, capsys):
    _stub_box_root(monkeypatch, [])
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _answer(monkeypatch, False)
    _argv(monkeypatch)

    assert d4.main() == 0

    create.assert_not_called()
    assert "[abort]" in capsys.readouterr().out


def test_d3_gate_prompts_once_for_the_whole_run(monkeypatch):
    """Invariant 6 is ONE confirmation for the plan, not five — a per-object prompt is
    how an operator gets trained to hammer 'y'."""
    tenant = _system_tenant()
    _install_tenant(monkeypatch, d3, tenant)
    _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    confirm = MagicMock(return_value=True)
    monkeypatch.setattr(d3, "_confirm", confirm)
    _argv(monkeypatch)

    assert d3.main() == 0
    confirm.assert_called_once()


def test_d1_gate_prompts_once_for_the_whole_run(monkeypatch):
    """Invariant 6 for D1 (T5): ONE confirmation authorises the whole plan — the
    workspace PLUS all four folders — not five prompts. An EMPTY tenant is required:
    with the workspace absent every object routes through `gate.allow`, so deleting the
    LiveWriteGate memoization (`if self._answer is None:`) would show up as five calls.
    The old decline test used an empty tenant too, but a DECLINE never reaches the
    folder loop, so it could not catch a per-object prompt on the accept path."""
    tenant = FakeTenant()
    _install_tenant(monkeypatch, d1, tenant)
    _stub_folder_create(monkeypatch, d1, tenant)
    confirm = MagicMock(return_value=True)
    monkeypatch.setattr(d1, "_confirm", confirm)
    _argv(monkeypatch)

    assert d1.main() == 0
    assert tenant.post.call_count == 1  # workspace created...
    confirm.assert_called_once()        # ...and all five creates rode ONE prompt


def test_d2_gate_prompts_once_for_the_whole_run(monkeypatch):
    """Invariant 6 for D2 (T5). D2 has no named `_confirm` seam — it prompts through
    `builtins.input` directly — so the once-ness is asserted on the input mock."""
    tenant = FakeTenant()
    _install_tenant(monkeypatch, d2, tenant)
    _stub_folder_create(monkeypatch, d2, tenant)
    prompt = MagicMock(return_value="y")
    monkeypatch.setattr("builtins.input", prompt)
    _argv(monkeypatch)

    assert d2.main() == 0
    assert tenant.post.call_count == 1  # workspace + two folders...
    prompt.assert_called_once()         # ...authorised by ONE input() prompt


def test_d1_decline_short_circuits_the_folder_loop(monkeypatch, capsys):
    """T5 — a decline is CACHED and terminal: with the workspace already adopted (and
    OWNED) but its four folders ABSENT, the first folder prompts, the operator declines,
    and the loop must short-circuit — no re-prompt, no create. This is the case the
    empty-tenant decline test cannot reach (there the workspace is never created, so the
    folder loop is skipped for a DIFFERENT reason)."""
    tenant = FakeTenant(
        workspaces=[{"id": _WS_SYSTEM_ID, "name": d1.WORKSPACE_NAME, "accessLevel": "OWNER"}],
    )
    _install_tenant(monkeypatch, d1, tenant)
    folder_create = _stub_folder_create(monkeypatch, d1, tenant)
    confirm = MagicMock(return_value=False)
    monkeypatch.setattr(d1, "_confirm", confirm)
    _argv(monkeypatch)

    assert d1.main() == 0
    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    confirm.assert_called_once()  # one prompt, then the loop short-circuits
    assert "declined" in capsys.readouterr().out.lower()


def test_d2_decline_short_circuits_the_folder_loop(monkeypatch, capsys):
    """T5 for D2 — workspace adopted, both folders absent, decline. The cached 'no'
    stops the second folder from re-prompting or creating."""
    tenant = FakeTenant(
        workspaces=[{"id": _WS_PORTAL_ID, "name": d2.WORKSPACE_NAME, "accessLevel": "OWNER"}],
    )
    _install_tenant(monkeypatch, d2, tenant)
    folder_create = _stub_folder_create(monkeypatch, d2, tenant)
    prompt = MagicMock(return_value="n")
    monkeypatch.setattr("builtins.input", prompt)
    _argv(monkeypatch)

    assert d2.main() == 0
    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    prompt.assert_called_once()
    assert "declined" in capsys.readouterr().out.lower()


# =========================================================================
# 7. THE MUTATING-VERB GUARD (invariant 1, create-only)
# =========================================================================

# Regexes are matched against CODE-ONLY text: every string literal is blanked and the
# tree re-rendered before the scan, so a docstring promising "never renames" or a
# print saying "delete the duplicate" cannot mask — or fake — a hit.
_MUTATING_VERBS: tuple[tuple[str, str], ...] = (
    (r"requests\.put\b", "requests.put"),
    (r"requests\.delete\b", "requests.delete"),
    (r"requests\.patch\b", "requests.patch"),
    (r"\.put\(", ".put("),
    (r"\.patch\(", ".patch("),
    (r"\.update_", ".update_"),
    (r"\.delete_", ".delete_"),
    # T2 — `\.update_` masked the missing `\.delete\(`: a planted
    # `client.folder(fid).delete(recursive=True)` sailed through GREEN because a
    # companion `.update_info(...)` red-lit for it. The bare-verb method calls must be
    # caught in their own right (Box SDK object methods, not the requests.* verbs).
    (r"\.delete\(", ".delete("),
    (r"\.remove\(", ".remove("),
    (r"\.rename\(", ".rename("),
    (r"\.move\(", ".move("),
    (r"\bupdate_rows\b", "update_rows"),
    (r"\bdelete_rows\b", "delete_rows"),
    (r"\bupdate_column\b", "update_column"),
    (r"\bdelete_column\b", "delete_column"),
    (r"\bmove_", "move_"),
    (r"\brename\b", "rename"),
    # T3 — ADOPT-DON'T-TOUCH (invariant 2): these are all POST-shaped, so "create-only"
    # reads as satisfied, but every one WRITES INTO an object this script only adopted.
    (r"\badd_rows\b", "add_rows"),
    (r"\badd_columns\b", "add_columns"),
    (r"\battach_", "attach_"),
    (r"\badd_comment\b", "add_comment"),
    (r"\bcreate_discussion\b", "create_discussion"),
    (r"\bshare_", "share_"),
    (r"\bapply_column_styles\b", "apply_column_styles"),
)


class _StringBlanker(ast.NodeTransformer):
    """Replace every string constant (docstrings included) with an empty string."""

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:  # noqa: N802 - ast API
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _code_only_source(source: str) -> str:
    tree = _StringBlanker().visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _mutating_hits(source: str) -> list[str]:
    code = _code_only_source(source)
    return [label for pattern, label in _MUTATING_VERBS if re.search(pattern, code)]


# ---- T1: a SECOND scan, over the ORIGINAL (un-blanked) tree -------------------
#
# The string-blanking above is load-bearing (a docstring saying "never renames" must
# not fake a hit) but it ALSO erases two real violations:
#   * `requests.request("DELETE", url)` — the mutating verb is a STRING LITERAL argument,
#     blanked to "" before the grep, so the create-only scan reads it as a bare
#     `requests.request(` with no verb. The verb is DATA, so no whitelist can clear it:
#     the call SHAPE is the violation.
#   * `requests.post(".../shares", ...)` — a mutating action expressed as a URL PATH
#     SEGMENT rather than an HTTP verb, again blanked before the grep.
# So this pass runs on the ORIGINAL tree and flags the request call shape / URL directly.
_REQUEST_LIB_NAMES = frozenset({"requests", "session"})
_REQUEST_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request"}
)
_MUTATING_URL_SEGMENTS = (
    "/move", "/shares", "/copy", "/rename", "/discussions", "/attachments",
)


def _string_constants(node: ast.AST) -> list[str]:
    """Every str constant reachable under `node` — plain literals AND f-string parts."""
    return [
        sub.value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    ]


def _request_ast_violations(source: str) -> list[str]:
    """Flag `requests`/`session` calls the string-blanked scan structurally cannot see."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        base = node.func.value
        base_name = base.id if isinstance(base, ast.Name) else None
        if base_name not in _REQUEST_LIB_NAMES:
            continue
        attr = node.func.attr
        # (a) `.request(` — the verb rides in as a runtime string arg; ban the shape.
        if attr == "request":
            hits.append(f"{base_name}.request(")
        # (b) a mutating path segment in any string arg of a request call (URL-verb POST).
        if attr in _REQUEST_VERBS:
            for arg in node.args:
                for text in _string_constants(arg):
                    for seg in _MUTATING_URL_SEGMENTS:
                        if seg in text:
                            hits.append(f"{base_name}.{attr}(...{seg}...)")
    return hits


def test_module_paths_tuple_names_exactly_the_four_builders():
    """Non-vacuity anchor for every source-scanning test below: the scans iterate this
    tuple, so an empty or stale tuple would make them pass while checking nothing."""
    assert len(MODULE_PATHS) == 4
    assert len({p.name for p in MODULE_PATHS}) == 4
    for path in MODULE_PATHS:
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").strip(), path


@pytest.mark.parametrize("path", MODULE_PATHS, ids=lambda p: p.name)
def test_builder_source_is_create_only_and_adopt_dont_touch(path):
    """CREATE-ONLY (invariant 1) AND ADOPT-DON'T-TOUCH (invariant 2): no update/delete/
    rename/move verb, and no write INTO an adopted object (add_rows/add_columns/attach_/
    share_/…), on anything — including objects this script itself created."""
    source = path.read_text(encoding="utf-8")
    hits = _mutating_hits(source) + _request_ast_violations(source)
    assert not hits, (
        f"{path.name} is CREATE-ONLY / ADOPT-DON'T-TOUCH (blast-radius invariants 1+2) "
        f"but its code uses forbidden shape(s) {hits}. These builders run against a "
        f"customer's PRODUCTION tenant: GET + create-POST only, no update/delete/rename/"
        f"move and no write into an adopted object, ever."
    )


def test_mutating_verb_scanner_detects_a_planted_violation():
    """Prove-the-control-bites: the scan above is worthless unless it RED-lights on a
    real violation. Plant one of each shape and assert every one is caught."""
    planted = (
        '"""Never renames, never deletes — honest."""\n'
        "import requests\n"
        "def go(client, sheet_id, box):\n"
        '    requests.put("/x", json={})\n'
        '    requests.delete("/y")\n'
        '    requests.patch("/z", json={})\n'
        "    client.update_rows(sheet_id, [])\n"
        "    client.delete_rows(sheet_id, [])\n"
        "    client.update_column(1)\n"
        "    client.delete_column(1)\n"
        "    client.move_folder(1, 2)\n"
        "    client.rename(1)\n"
        # T2 — bare-verb method calls (the Box SDK object shapes `\\.update_` masked).
        "    box.folder(1).delete(recursive=True)\n"
        "    box.file(2).remove()\n"
        "    box.folder(1).rename('x')\n"
        "    box.folder(1).move(2)\n"
        # T3 — writes INTO an adopted object (POST-shaped, adopt-don't-touch).
        "    client.add_rows(sheet_id, [])\n"
        "    client.add_columns(sheet_id, [])\n"
        "    client.attach_url(sheet_id, 'u')\n"
        "    client.add_comment(1, 'x')\n"
        "    client.create_discussion(1)\n"
        "    client.share_workspace(1)\n"
        "    client.apply_column_styles(1)\n"
    )
    hits = set(_mutating_hits(planted))
    assert {
        "requests.put", "requests.delete", "requests.patch", ".update_", ".delete_",
        "update_rows", "delete_rows", "update_column", "delete_column", "move_", "rename",
        ".delete(", ".remove(", ".rename(", ".move(",
        "add_rows", "add_columns", "attach_", "add_comment", "create_discussion",
        "share_", "apply_column_styles",
    } <= hits
    # ...and a docstring/print that merely TALKS about renaming is not a hit.
    innocent = (
        '"""It never renames, re-parents, or deletes an adopted object."""\n'
        "def go():\n"
        '    print("identify the live object, delete or rename the rest")\n'
    )
    assert _mutating_hits(innocent) == []


def test_request_ast_scanner_detects_verb_and_url_violations():
    """T1 — prove the AST pass catches what string-blanking erases: the verb-as-data
    `requests.request("DELETE", ...)` and mutating URL PATH segments in a request call.
    Both are invisible to `_mutating_hits` (the literal is blanked before the grep)."""
    verb_as_data = (
        "import requests\n"
        "def go(url):\n"
        '    requests.request("DELETE", url)\n'
        '    session.request("PATCH", url)\n'
    )
    assert "requests.request(" in _request_ast_violations(verb_as_data)
    assert "session.request(" in _request_ast_violations(verb_as_data)
    # The string-blanked scan is BLIND to it — this is exactly why the AST pass exists.
    assert _mutating_hits(verb_as_data) == []

    url_verbs = (
        "import requests\n"
        "BASE = 'https://api.smartsheet.com/2.0'\n"
        "def go(fid, wid):\n"
        '    requests.post(f"{BASE}/folders/{fid}/move", json={})\n'
        '    requests.post(f"{BASE}/workspaces/{wid}/shares", json={})\n'
    )
    hits = _request_ast_violations(url_verbs)
    assert any("/move" in h for h in hits) and any("/shares" in h for h in hits)
    assert _mutating_hits(url_verbs) == []

    # A read GET on a benign URL, and a docstring MENTIONING "/shares", are NOT hits.
    innocent = (
        '"""We never POST to /shares or /move."""\n'
        "import requests\n"
        "def go(wid):\n"
        '    requests.get(f"/workspaces/{wid}", timeout=30)\n'
    )
    assert _request_ast_violations(innocent) == []


# =========================================================================
# 8. NO SEND / NO AI IMPORTS
# =========================================================================

_FORBIDDEN_IMPORTS = ("graph_client", "resend_client", "anthropic_client", "anthropic")


def _dynamic_import_target(node: ast.Call) -> str | None:
    """The dotted module name a `__import__` / `importlib.import_module` call pulls in.

    T4 — the static scan below walks `ast.Import`/`ast.ImportFrom` only, so a dynamic
    `__import__("shared.graph_client", fromlist=["send_mail"])` slips a forbidden module
    past it — and that shape is IDIOMATIC here (every builder already uses
    `__import__("pathlib")` for its sys.path insert), so a future edit could reach for it
    innocently. Collect the first string arg and split it like any other module path.
    """
    func = node.func
    is_dunder = isinstance(func, ast.Name) and func.id == "__import__"
    is_importlib = isinstance(func, ast.Attribute) and func.attr == "import_module"
    if not (is_dunder or is_importlib):
        return None
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
        node.args[0].value, str
    ):
        return node.args[0].value
    return None


def _imported_names(source: str) -> set[str]:
    """Every module path and imported name an `import` statement pulls in.

    Includes dynamic `__import__`/`importlib.import_module` targets (T4) so the no-send/
    no-AI guard cannot be bypassed by a runtime import.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(node.module.split("."))
                names.add(node.module)
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Call):
            target = _dynamic_import_target(node)
            if target is not None:
                names.update(target.split("."))
                names.add(target)
    return names


@pytest.mark.parametrize("path", MODULE_PATHS, ids=lambda p: p.name)
def test_builder_imports_no_send_and_no_ai_module(path):
    imported = _imported_names(path.read_text(encoding="utf-8"))
    banned = sorted(imported & set(_FORBIDDEN_IMPORTS))
    assert not banned, (
        f"{path.name} imports {banned} — a cutover builder has NO send capability and "
        f"NO AI step (Invariant 1, two-process model)."
    )


def test_forbidden_import_scanner_detects_a_planted_import():
    """Prove-the-control-bites for the import scan, static AND dynamic (T4)."""
    assert _imported_names("from shared import graph_client\n") & set(_FORBIDDEN_IMPORTS)
    assert _imported_names("import anthropic\n") & set(_FORBIDDEN_IMPORTS)
    assert _imported_names("from shared.resend_client import send\n") & set(_FORBIDDEN_IMPORTS)
    # T4 — dynamic imports must feed the same check.
    assert _imported_names(
        '__import__("shared.graph_client", fromlist=["send_mail"])\n'
    ) & set(_FORBIDDEN_IMPORTS)
    assert _imported_names('importlib.import_module("anthropic")\n') & set(_FORBIDDEN_IMPORTS)
    # ...but the idiomatic `__import__("pathlib")` sys.path insert is clean.
    assert not _imported_names('__import__("pathlib")\n') & set(_FORBIDDEN_IMPORTS)
    assert not _imported_names("from shared import keychain, smartsheet_client\n") & set(
        _FORBIDDEN_IMPORTS
    )


# =========================================================================
# 8b. NO SECRETS IN OUTPUT (invariant 7) — T6
# =========================================================================
#
# Every test monkeypatches `_headers` to `{}`, so the Keychain/token path is never
# EXECUTED and never observed at runtime. This SOURCE-level guard is the only thing that
# inspects it: no `print` may interpolate the bearer token, the auth headers, or the
# Keychain read into operator output.

_SECRET_NAMES = frozenset({"token", "_headers", "get_secret"})


def _secret_print_violations(source: str) -> list[str]:
    """Names of any secret-bearing symbol reached by an argument of a `print(...)` call.

    A hit is a `print` whose args (positional OR keyword) contain a `Name` or a `Call`
    named `token` / `_headers` / `get_secret` anywhere in their subtree — i.e. the token,
    the auth-header builder, or the raw Keychain read is being rendered to output.
    """
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        args = [*node.args, *(kw.value for kw in node.keywords)]
        for arg in args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name) and sub.id in _SECRET_NAMES:
                    hits.append(sub.id)
                elif isinstance(sub, ast.Call):
                    fn = sub.func
                    fname = (
                        fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute)
                        else None
                    )
                    if fname in _SECRET_NAMES:
                        hits.append(fname)
    return hits


@pytest.mark.parametrize("path", MODULE_PATHS, ids=lambda p: p.name)
def test_builder_never_prints_a_secret(path):
    hits = _secret_print_violations(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{path.name} interpolates {sorted(set(hits))} into a print — invariant 7 is "
        "names and ids ONLY, never the bearer token, the auth headers, or a Keychain read."
    )


def test_secret_print_scanner_detects_a_planted_violation():
    """Prove-the-control-bites for T6 — each shape a leak could take must RED-light,
    and a benign id print must not."""
    assert _secret_print_violations('def f():\n    print(f"token={_headers()}")\n')
    assert _secret_print_violations('token = "x"\nprint(token)\n')
    assert _secret_print_violations('print(get_secret("ITS_SMARTSHEET_TOKEN"))\n')
    assert _secret_print_violations('print("headers", file=_headers())\n')
    assert _secret_print_violations('print("x", "y", _headers())\n')
    # A plain id/name print is clean.
    assert _secret_print_violations('print(f"folder_id={new_id}")\n') == []
    assert _secret_print_violations('import sys\nprint("x", file=sys.stderr)\n') == []


# =========================================================================
# 9. D3 COLUMN SPEC >= WRITER EXPECTATIONS
# =========================================================================


def _writer_column_titles(rel_path: str, funcs: set[str] | None = None) -> set[str]:
    """Column titles a WRITER module actually addresses, derived from its own source.

    Title-keyed Smartsheet access shows up in exactly three shapes — a `{title: value}`
    dict literal, `row.get("Title")`, and `d["Title"]` — so collect those and drop
    non-titles (lowercase keys like the SLA-duration map, SCREAMING_CASE env-var names).
    Deriving instead of hardcoding is what makes this test TRACK the writer: rename a
    column in `shared/error_log.py` and the builder must follow or this reds.
    """
    tree = ast.parse((_REPO_ROOT / rel_path).read_text(encoding="utf-8"))
    scopes: list[ast.AST] = [tree]
    if funcs:
        scopes = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name in funcs
        ]
        assert scopes, f"no function {funcs} in {rel_path} — test wiring broke"
    found: set[str] = set()
    for scope in scopes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Dict):
                found.update(
                    k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                found.add(node.slice.value)
    return {t for t in found if t[:1].isupper() and not t.isupper()}


def _builder_titles(sheet_name: str) -> set[str]:
    for name, _folder, _const, columns in d3.SHEETS:
        if name == sheet_name:
            return {str(c["title"]) for c in columns}
    raise AssertionError(f"{sheet_name} is not in build_system_sheets.SHEETS")


# (sheet, writer module, restrict-to-functions) — the writers whose title-keyed
# access the builder's schema must satisfy.
_WRITER_SOURCES: tuple[tuple[str, str, set[str] | None], ...] = (
    ("ITS_Config", "shared/smartsheet_client.py", {"get_setting", "get_settings_with_prefix"}),
    ("ITS_Errors", "shared/error_log.py", None),
    ("ITS_Errors", "shared/errors_rotation.py", None),
    ("ITS_Quarantine", "shared/quarantine.py", None),
    ("ITS_Review_Queue", "shared/review_queue.py", None),
)


@pytest.mark.parametrize(
    ("sheet_name", "writer", "funcs"), _WRITER_SOURCES,
    ids=[f"{s}<-{w.split('/')[-1]}" for s, w, _f in _WRITER_SOURCES],
)
def test_d3_schema_covers_every_title_the_writer_addresses(sheet_name, writer, funcs):
    expected = _writer_column_titles(writer, funcs)
    assert expected, f"derived no titles from {writer} — test wiring broke"
    missing = expected - _builder_titles(sheet_name)
    assert not missing, (
        f"build_system_sheets' {sheet_name} schema is missing column(s) {sorted(missing)} "
        f"that {writer} writes or reads BY TITLE. A freshly built production sheet "
        f"without them fails the write at runtime."
    )


def test_d3_daemon_health_titles_are_a_superset_of_the_key_to_title_map():
    schema_titles = _builder_titles("ITS_Daemon_Health")
    mapped = {title for _key, title in d3.DAEMON_HEALTH_KEY_TO_TITLE}
    assert mapped <= schema_titles
    assert schema_titles == mapped  # MINIMAL SET: no extra columns either


# =========================================================================
# 10. D3 PICKLIST PARITY WITH THE REGISTRY (the #247 -> #253 class)
# =========================================================================

_REGISTRY_GATED_SHEETS: tuple[tuple[str, int], ...] = (
    ("ITS_Errors", sheet_ids.SHEET_ERRORS),
    ("ITS_Quarantine", sheet_ids.SHEET_QUARANTINE),
    ("ITS_Review_Queue", sheet_ids.SHEET_REVIEW_QUEUE),
)


def _builder_options(sheet_name: str) -> dict[str, list[str]]:
    for name, _folder, _const, columns in d3.SHEETS:
        if name == sheet_name:
            return {
                str(c["title"]): list(c["options"])
                for c in columns
                if c.get("type") == "PICKLIST"
            }
    raise AssertionError(f"{sheet_name} is not in build_system_sheets.SHEETS")


@pytest.mark.parametrize(
    ("sheet_name", "sheet_id"), _REGISTRY_GATED_SHEETS, ids=[s for s, _ in _REGISTRY_GATED_SHEETS]
)
def test_d3_picklist_options_are_set_equal_to_the_registry(sheet_name, sheet_id):
    """Every REGISTRY-gated column is a PICKLIST in the builder and its options are
    SET-EQUAL to the registry set.

    NARROWED docstring (T7): because the builder SOURCES these option lists from the very
    `picklist_validation` objects the REGISTRY is built from, the two set-inequality
    directions the old docstring warned about (builder<registry / builder>registry via a
    drifted hand-edit) cannot arise from a value edit — they are structurally equal. What
    this test really proves is the PARITY OF SHAPE: that each registry-gated column exists
    in the builder AS A PICKLIST at all (a column retyped away from PICKLIST, or renamed,
    red-lights here). The "is it actually sourced, not hand-typed" half is
    `test_d3_registry_gated_picklists_are_sourced_not_hand_typed` below — together they
    are the real #247->#253 guard.
    """
    registry = picklist_validation.REGISTRY[sheet_id]
    assert registry, f"{sheet_name} has no REGISTRY entry — test wiring broke"
    options = _builder_options(sheet_name)
    for column, allowed in registry.items():
        assert column in options, f"{sheet_name}.{column} is registry-gated but not a PICKLIST"
        assert set(options[column]) == set(allowed), (
            f"{sheet_name}.{column}: builder {sorted(options[column])} != registry "
            f"{sorted(allowed)} (the #247->#253 class)."
        )


_D3_SOURCE = (_MIGRATIONS_DIR / "build_system_sheets.py").read_text(encoding="utf-8")


def _assigned_value(node: ast.AST, name: str) -> ast.expr | None:
    """The RHS of a module-level `name = …` OR `name: T = …` (Assign / AnnAssign)."""
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == name for t in node.targets
    ):
        return node.value
    if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            and node.target.id == name and node.value is not None):
        return node.value
    return None


def _assign_names(node: ast.AST) -> list[str]:
    """The bound Name targets of an Assign / AnnAssign (empty for anything else)."""
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _d3_sheet_to_schema_var() -> dict[str, str]:
    """sheet name -> the module-level *_COLUMNS variable name, read from SHEETS' AST.

    The 4th element of each SHEETS tuple is a Name referencing the schema list. The
    sheet-name element is usually a Constant, but ITS_Daemon_Health's is the Name
    `SHEET_DAEMON_HEALTH_NAME` — resolved via the live module.
    """
    for node in ast.walk(ast.parse(_D3_SOURCE)):
        value = _assigned_value(node, "SHEETS")
        if value is None:
            continue
        assert isinstance(value, ast.Tuple)
        out: dict[str, str] = {}
        for elt in value.elts:
            assert isinstance(elt, ast.Tuple)
            name_node, schema_node = elt.elts[0], elt.elts[3]
            if isinstance(name_node, ast.Constant):
                sheet_name = str(name_node.value)
            elif isinstance(name_node, ast.Name):
                sheet_name = str(getattr(d3, name_node.id))
            else:  # pragma: no cover - defensive
                continue
            assert isinstance(schema_node, ast.Name), sheet_name
            out[sheet_name] = schema_node.id
        return out
    raise AssertionError("SHEETS assignment not found in build_system_sheets.py")


def _d3_column_options_nodes(schema_var: str) -> dict[str, ast.expr]:
    """title -> the AST node assigned to that column's `options` key, for one schema var."""
    for node in ast.walk(ast.parse(_D3_SOURCE)):
        value = _assigned_value(node, schema_var)
        if value is None:
            continue
        assert isinstance(value, ast.List)
        out: dict[str, ast.expr] = {}
        for col in value.elts:
            assert isinstance(col, ast.Dict)
            title: str | None = None
            options: ast.expr | None = None
            for k, v in zip(col.keys, col.values, strict=True):
                if isinstance(k, ast.Constant) and k.value == "title" and isinstance(v, ast.Constant):
                    title = str(v.value)
                elif isinstance(k, ast.Constant) and k.value == "options":
                    options = v
            if title is not None and options is not None:
                out[title] = options
        return out
    raise AssertionError(f"{schema_var} assignment not found in build_system_sheets.py")


def _d3_picklist_sourced_names() -> set[str]:
    """Module-level names in build_system_sheets whose value expression references
    `picklist_validation` — i.e. the option lists that are SOURCED, not hand-typed."""
    sourced: set[str] = set()
    for node in ast.walk(ast.parse(_D3_SOURCE)):
        names = _assign_names(node)
        if not names:
            continue
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if value is None:
            continue
        refs_pv = any(
            isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)
            and a.value.id == "picklist_validation"
            for a in ast.walk(value)
        )
        if refs_pv:
            sourced.update(names)
    return sourced


@pytest.mark.parametrize(
    ("sheet_name", "sheet_id"), _REGISTRY_GATED_SHEETS, ids=[s for s, _ in _REGISTRY_GATED_SHEETS]
)
def test_d3_registry_gated_picklists_are_sourced_not_hand_typed(sheet_name, sheet_id):
    """T7 — the real #247->#253 guard: every REGISTRY-gated column's `options` must be a
    REFERENCE to a picklist_validation-sourced module variable, NEVER a hand-typed list
    literal. A literal that happens to match the registry today drifts silently tomorrow;
    an AST `ast.Name` bound to a `picklist_validation.*` expression tracks it forever.
    (The set-equality test above cannot catch a literal — it compares VALUES, which a
    hand-typed-but-currently-correct list satisfies.)"""
    schema_var = _d3_sheet_to_schema_var()[sheet_name]
    options_nodes = _d3_column_options_nodes(schema_var)
    sourced = _d3_picklist_sourced_names()
    registry = picklist_validation.REGISTRY[sheet_id]
    for column in registry:
        node = options_nodes.get(column)
        assert node is not None, f"{sheet_name}.{column} is registry-gated but has no options node"
        assert isinstance(node, ast.Name), (
            f"{sheet_name}.{column} options are a hand-typed literal ({ast.dump(node)[:60]}…) "
            f"— registry-gated picklists must be SOURCED from picklist_validation."
        )
        assert node.id in sourced, (
            f"{sheet_name}.{column} options reference {node.id!r}, which is not assigned "
            f"from a picklist_validation expression — it is not provably sourced."
        )


def test_d3_ungated_workstream_columns_are_the_global_set_plus_field_ops():
    """ITS_Config + ITS_Daemon_Health are NOT registry-gated, so their Workstream columns
    are sourced-plus-`field_ops` — fieldops_sync writes that value to both."""
    expected = set(picklist_validation._WORKSTREAM_VALUES_GLOBAL) | {"field_ops"}
    for sheet_name in ("ITS_Config", "ITS_Daemon_Health"):
        assert set(_builder_options(sheet_name)["Workstream"]) == expected, sheet_name


def test_d3_daemon_health_status_options_come_from_the_writers_literal():
    """The status column is sourced from `heartbeat.HeartbeatStatus` itself, so a value
    added to the writer cannot ship without the builder knowing."""
    from typing import get_args

    from shared import heartbeat

    assert set(_builder_options("ITS_Daemon_Health")["Last Cycle Status"]) == set(
        get_args(heartbeat.HeartbeatStatus)
    )
    assert "CIRCUIT_OPEN" in _builder_options("ITS_Daemon_Health")["Last Cycle Status"]


def test_d3_quarantine_catch_all_is_other_not_global():
    """ITS_Quarantine's catch-all is `other`; ITS_Review_Queue's is `global`. Cross-wiring
    them is a documented footgun."""
    quarantine = set(_builder_options("ITS_Quarantine")["Workstream"])
    review = set(_builder_options("ITS_Review_Queue")["Workstream"])
    assert "other" in quarantine and "global" not in quarantine
    assert "global" in review and "other" not in review


def test_d3_every_picklist_option_list_is_unique_and_sourced_ones_are_sorted():
    """Sourced sets are `sorted()` for deterministic, diffable output. "Escalation Level"
    is the ONE hand-written literal (no registry entry, no code writer) and is ordered by
    ESCALATION TIER on purpose — sorting it would put L3 above L1 in the dropdown."""
    hand_written = {("ITS_Review_Queue", "Escalation Level")}
    for sheet_name, _folder, _const, _cols in d3.SHEETS:
        for column, options in _builder_options(sheet_name).items():
            assert len(options) == len(set(options)), f"{sheet_name}.{column} has duplicates"
            if (sheet_name, column) in hand_written:
                continue
            assert options == sorted(options), f"{sheet_name}.{column} is not sorted"
    assert _builder_options("ITS_Review_Queue")["Escalation Level"] == d3._ESCALATION_LEVELS


# =========================================================================
# 11. D3 DAEMON_HEALTH KEY COVERAGE
# =========================================================================


def test_d3_key_to_title_covers_exactly_the_twelve_sheet_ids_keys():
    keys = [key for key, _title in d3.DAEMON_HEALTH_KEY_TO_TITLE]
    assert len(keys) == len(set(keys)) == 12
    assert set(keys) == set(sheet_ids.DAEMON_HEALTH_COLUMNS), (
        "build_system_sheets.DAEMON_HEALTH_KEY_TO_TITLE must cover exactly the keys in "
        "sheet_ids.DAEMON_HEALTH_COLUMNS — heartbeat writes are COLUMN-ID-KEYED, so a key "
        "the builder cannot resolve leaves that cell permanently unwritten (and silently, "
        "because HeartbeatReporter is broad-except-isolated)."
    )
    assert dict(d3.DAEMON_HEALTH_KEY_TO_TITLE)["total_cycles"] == "Total Cycles Today"


def test_d3_column_id_readback_maps_every_key(monkeypatch):
    _stub_daemon_health_columns(monkeypatch)
    resolved = d3.read_daemon_health_column_ids(697287746473860)
    assert set(resolved) == set(sheet_ids.DAEMON_HEALTH_COLUMNS)
    assert all(v is not None for v in resolved.values())


def test_d3_column_id_readback_warns_on_a_missing_title(monkeypatch, capsys):
    """An unresolvable column must not be pasted silently as a partial dict."""
    live = [
        {"id": 700_000 + i, "title": title}
        for i, (_k, title) in enumerate(d3.DAEMON_HEALTH_KEY_TO_TITLE)
        if title != "Notes"
    ]
    monkeypatch.setattr(
        d3.smartsheet_client, "list_columns_with_options", MagicMock(return_value=live)
    )
    resolved = d3.read_daemon_health_column_ids(1)
    assert resolved["notes"] is None
    out = capsys.readouterr().out
    assert "daemon_health_column_missing" in out and "notes" in out


# =========================================================================
# 12 + 14. DUPLICATE-NAME AMBIGUITY IS LOUD (invariant 8)
# =========================================================================


def test_d3_duplicate_sheet_names_warn_with_every_id_and_create_nothing(monkeypatch, capsys):
    """The live "02 — Logs" folder holds FIVE sheets named ITS_Errors and only the LAST
    is the one the code uses. Adopt-first is correct; adopting SILENTLY is not."""
    duplicate_ids = [
        4195780532326276, 470411799121796, 2704945844277124, 4505679602601860, 8015637140950916,
    ]
    tenant = _system_tenant(with_sheets=True)
    logs_folder = _SYSTEM_FOLDER_IDS[d3.FOLDER_LOGS]
    tenant.sheets[logs_folder] = [
        {"id": sid, "name": "ITS_Errors"} for sid in duplicate_ids
    ] + [{"id": 137816716562308, "name": "ITS_Quarantine"}]
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 0

    sheet_create.assert_not_called()
    out = capsys.readouterr().out
    assert "duplicate_name_ambiguity" in out
    assert "5 sheets are named 'ITS_Errors'" in out
    for sid in duplicate_ids:
        assert str(sid) in out, sid
    assert "SHEET_ERRORS" in out  # names the constant NOT to flip yet


def test_d3_duplicate_folder_names_fail_closed_and_create_nothing_under_them(monkeypatch, capsys):
    """T8 — D3 has its OWN private `_find_folder_ids`/`_resolve_folders` (not shared with
    D1). A sibling agent changed ambiguous PARENT handling to FAIL CLOSED, so a duplicate
    '02 — Logs' folder does NOT adopt-and-warn like a terminal sheet: every sheet under it
    is `blocked-parent`, nothing is created there, and the run exits NONZERO. The sheets in
    the OTHER (unambiguous) folders still build — the fence is per-parent."""
    tenant = _system_tenant()
    logs_id = _SYSTEM_FOLDER_IDS[d3.FOLDER_LOGS]
    # A SECOND folder named "02 — Logs" — the duplicate-parent case.
    tenant.folders[_WS_SYSTEM_ID].append({"id": 999_111, "name": d3.FOLDER_LOGS})
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 1  # blocked-parent is a NON_TERMINAL_STATUS -> nonzero

    out = capsys.readouterr().out
    assert "duplicate_parent_ambiguity" in out
    assert str(logs_id) in out and "999111" in out  # every matching id named
    assert "blocked-parent" in out
    # The two Logs-folder sheets are refused; the other three (unambiguous folders) build.
    created = {c.args[1] for c in sheet_create.call_args_list}
    assert "ITS_Errors" not in created and "ITS_Quarantine" not in created


def test_d1_duplicate_parent_workspace_fails_closed(monkeypatch, capsys):
    """C2 + C3 (retarget) — the convergence changed D1's ambiguous PARENT workspace from
    adopt-first-and-warn to FAIL CLOSED: >1 exact-name workspace is a container this script
    creates the four folders inside, so it must NEVER guess which one. Two OWNER workspaces
    named 'ITS — System' ⇒ duplicate_parent_ambiguity, nothing created, exit 1, and the FLIP
    BLOCK renders WORKSPACE_SYSTEM as the <AMBIGUOUS …> sentinel — never a clean paste-ready
    id. (Was `..._warn_and_create_nothing`, which asserted the OLD adopt-first exit-0
    contract; retargeted, not deleted.)"""
    tenant = FakeTenant(
        workspaces=[
            _ws(111, d1.WORKSPACE_NAME, permalink="https://app.smartsheet.com/ws/a"),
            _ws(222, d1.WORKSPACE_NAME, permalink="https://app.smartsheet.com/ws/b"),
        ],
        folders={111: [{"id": fid, "name": n} for n, fid in _SYSTEM_FOLDER_IDS.items()]},
    )
    _install_tenant(monkeypatch, d1, tenant)
    folder_create = _stub_folder_create(monkeypatch, d1, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d1.main() == 1

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert "duplicate_parent_ambiguity" in out
    assert "111" in out and "222" in out                   # every matching id named
    assert "<AMBIGUOUS" in out                             # workspace rendered as the sentinel
    assert "WORKSPACE_SYSTEM       = 111" not in out       # never a clean paste-ready id
    assert "flip_block_incomplete" in out


def test_d2_duplicate_folder_names_warn_and_create_nothing(monkeypatch, capsys):
    tenant = FakeTenant(
        workspaces=[{"id": _WS_PORTAL_ID, "name": d2.WORKSPACE_NAME, "accessLevel": "OWNER"}],
        folders={
            _WS_PORTAL_ID: [
                {"id": 2261538947000196, "name": "00_Safety Portal"},
                {"id": 4444, "name": "00_Safety Portal"},
                {"id": 6765138574370692, "name": "00_Form Catalog"},
            ]
        },
    )
    _install_tenant(monkeypatch, d2, tenant)
    folder_create = _stub_folder_create(monkeypatch, d2, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d2.main() == 0

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert "duplicate_name_ambiguity" in out
    assert "2261538947000196" in out and "4444" in out


def test_d4_duplicate_root_names_warn_with_every_id_and_create_nothing(monkeypatch, capsys):
    root = [
        {"type": "folder", "id": "111", "name": "ITS Safety Reports"},
        {"type": "folder", "id": "777", "name": "ITS Safety Reports"},
        {"type": "folder", "id": "222", "name": "ITS Progress Reports"},
        {"type": "folder", "id": "333", "name": "ITS Archive"},
    ]
    _stub_box_root(monkeypatch, root)
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d4.main() == 0

    create.assert_not_called()
    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "'111'" in out and "'777'" in out
    assert "folder_id=111" in out  # adopts the FIRST, never a third


# =========================================================================
# 12b. OWNERSHIP HARD-STOP (C1) + PARENT-WORKSPACE AMBIGUITY (C2)
# =========================================================================
#
# The convergence pass made `accessLevel` the load-bearing signal for the
# sandbox-shared-into-production trap across all three Smartsheet builders. Before it, the
# FakeTenant fixtures always stamped accessLevel="OWNER", so this most-important new
# blast-radius control shipped with ZERO coverage (prove-the-control-bites, HOUSE_REFLEXES
# §2). These tests drive the non-OWNER, absent-accessLevel, and duplicate-parent adopts.
#
# The FLIP-BLOCK-leak rule is what each asserts on the output: an id that is not-owned /
# ambiguous / unresolved must NEVER render as a clean paste-ready integer. D1 and D2
# SUPPRESS the whole block on the not-owned path; D3 always prints the block but with every
# SHEET_* id rendered <unresolved> (+ a flip_block_incomplete WARN).


def test_d1_adopting_a_non_owner_workspace_fails_closed(monkeypatch, capsys):
    """C1 — accessLevel != OWNER hard-stop. A VIEWER-access 'ITS — System' is the
    sandbox-shared-into-production trap: exactly ONE exact-name match (so the ambiguity
    WARN never fires), but not OWNED. D1 must refuse — no folder create, exit 1,
    adopted_workspace_not_owned + the permalink named, and the WHOLE FLIP BLOCK suppressed
    so the sandbox id never reaches the clipboard as a clean paste line."""
    tenant = FakeTenant(
        workspaces=[_ws(_WS_SYSTEM_ID, d1.WORKSPACE_NAME, access="VIEWER",
                        permalink=_SANDBOX_PERMALINK)],
    )
    _install_tenant(monkeypatch, d1, tenant)
    folder_create = _stub_folder_create(monkeypatch, d1, tenant)
    _answer(monkeypatch, True)  # would ALLOW a create — must never be reached
    _argv(monkeypatch)

    assert d1.main() == 1

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert "adopted_workspace_not_owned" in out
    assert _SANDBOX_PERMALINK in out
    assert "=== FLIP BLOCK ===" not in out                        # block suppressed whole
    assert f"WORKSPACE_SYSTEM       = {_WS_SYSTEM_ID}" not in out  # no clean sandbox-id line


def test_d1_adopting_a_workspace_with_absent_accesslevel_fails_closed(monkeypatch, capsys):
    """C1 — an ABSENT accessLevel is UNKNOWN ownership; on a customer PRODUCTION tenant that
    fails closed exactly like a non-OWNER (converged rule 1 — the endpoint populates the
    field in practice, so an omission is anomalous)."""
    tenant = FakeTenant(
        workspaces=[_ws(_WS_SYSTEM_ID, d1.WORKSPACE_NAME, access=None,
                        permalink=_SANDBOX_PERMALINK)],
    )
    _install_tenant(monkeypatch, d1, tenant)
    folder_create = _stub_folder_create(monkeypatch, d1, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d1.main() == 1

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert "adopted_workspace_not_owned" in out
    assert _SANDBOX_PERMALINK in out
    assert "=== FLIP BLOCK ===" not in out


def test_d1_ensure_workspace_raises_workspace_not_owned(monkeypatch):
    """C1 (D1 only) — the refusal is a RAISE (WorkspaceNotOwnedError), so no code path can
    fall through to folder creation: 'adopt for reading' and 'create inside' are different
    privileges. main() catches it and suppresses the FLIP BLOCK (asserted above)."""
    tenant = FakeTenant(
        workspaces=[_ws(_WS_SYSTEM_ID, d1.WORKSPACE_NAME, access="EDITOR",
                        permalink=_SANDBOX_PERMALINK)],
    )
    _install_tenant(monkeypatch, d1, tenant)
    gate = d1.LiveWriteGate(dry_run=False)
    with pytest.raises(d1.WorkspaceNotOwnedError):
        d1.ensure_workspace(gate, dry_run=False)


def test_d2_adopting_a_non_owner_workspace_fails_closed(monkeypatch, capsys):
    """C1 — D2's accessLevel != OWNER hard-stop (converged with D1). A VIEWER 'ITS ––
    Safety Portal' is refused: no folder create, exit 1, adopted_workspace_not_owned +
    permalink, and the FLIP BLOCK SUPPRESSED whole. This is the leak D2 previously had —
    it called _print_flip_block unconditionally and rendered the adopted sandbox id as a
    clean WORKSPACE_SAFETY_PORTAL = <id> paste line."""
    tenant = FakeTenant(
        workspaces=[_ws(_WS_PORTAL_ID, d2.WORKSPACE_NAME, access="VIEWER",
                        permalink=_SANDBOX_PERMALINK)],
    )
    _install_tenant(monkeypatch, d2, tenant)
    folder_create = _stub_folder_create(monkeypatch, d2, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d2.main() == 1

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert "adopted_workspace_not_owned" in out
    assert _SANDBOX_PERMALINK in out
    assert "=== FLIP BLOCK ===" not in out                            # suppressed whole
    assert f"WORKSPACE_SAFETY_PORTAL = {_WS_PORTAL_ID}" not in out     # no sandbox-id leak


def test_d2_adopting_a_workspace_with_absent_accesslevel_fails_closed(monkeypatch, capsys):
    """C1 — an ABSENT accessLevel fails closed for D2 too. This is the exact D1-vs-D2
    divergence the convergence pass closed: D2 previously PROCEEDED on an absent
    accessLevel and would have built folders on the sandbox plan."""
    tenant = FakeTenant(
        workspaces=[_ws(_WS_PORTAL_ID, d2.WORKSPACE_NAME, access=None,
                        permalink=_SANDBOX_PERMALINK)],
    )
    _install_tenant(monkeypatch, d2, tenant)
    folder_create = _stub_folder_create(monkeypatch, d2, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d2.main() == 1

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert "adopted_workspace_not_owned" in out
    assert _SANDBOX_PERMALINK in out
    assert "=== FLIP BLOCK ===" not in out
    assert f"WORKSPACE_SAFETY_PORTAL = {_WS_PORTAL_ID}" not in out


def test_d3_adopting_a_non_owner_workspace_fails_closed(monkeypatch, capsys):
    """C1 — D3 creates sheets INSIDE the workspace, so it too must refuse a non-OWNER one
    (converged with D1/D2). A VIEWER 'ITS — System' ⇒ every sheet blocked-parent, no sheet
    create, exit 1, adopted_workspace_not_owned + permalink, and no clean SHEET_* id in the
    FLIP BLOCK (all render <unresolved>, flip_block_incomplete fires)."""
    tenant = _system_tenant()  # workspace + four folders present (OWNER); sheets absent
    tenant.workspaces[0]["accessLevel"] = "VIEWER"
    tenant.workspaces[0]["permalink"] = _SANDBOX_PERMALINK
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 1

    tenant.post.assert_not_called()
    sheet_create.assert_not_called()
    out = capsys.readouterr().out
    assert "adopted_workspace_not_owned" in out
    assert _SANDBOX_PERMALINK in out
    assert "blocked-parent" in out
    assert "flip_block_incomplete" in out


def test_d3_adopting_a_workspace_with_absent_accesslevel_fails_closed(monkeypatch, capsys):
    """C1 — an ABSENT accessLevel fails closed for D3 too (UNKNOWN ownership on a customer
    PRODUCTION tenant must not have five sheets built inside it)."""
    tenant = _system_tenant()
    tenant.workspaces[0]["accessLevel"] = None
    tenant.workspaces[0]["permalink"] = _SANDBOX_PERMALINK
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 1

    tenant.post.assert_not_called()
    sheet_create.assert_not_called()
    out = capsys.readouterr().out
    assert "adopted_workspace_not_owned" in out
    assert _SANDBOX_PERMALINK in out
    assert "blocked-parent" in out


def test_d2_duplicate_parent_workspace_fails_closed(monkeypatch, capsys):
    """C2 — D2's ambiguous PARENT workspace fails closed (converged with D3's
    _resolve_unique_parent + D1's DuplicateParentError). Two OWNER workspaces named 'ITS ––
    Safety Portal' ⇒ duplicate_parent_ambiguity naming both ids, no folder create, exit 1,
    and the FLIP BLOCK renders WORKSPACE_SAFETY_PORTAL as <AMBIGUOUS …>, never a clean id."""
    tenant = FakeTenant(
        workspaces=[
            _ws(111, d2.WORKSPACE_NAME, permalink="https://app.smartsheet.com/ws/a"),
            _ws(222, d2.WORKSPACE_NAME, permalink="https://app.smartsheet.com/ws/b"),
        ],
    )
    _install_tenant(monkeypatch, d2, tenant)
    folder_create = _stub_folder_create(monkeypatch, d2, tenant)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d2.main() == 1

    tenant.post.assert_not_called()
    folder_create.assert_not_called()
    out = capsys.readouterr().out
    assert "duplicate_parent_ambiguity" in out
    assert "111" in out and "222" in out
    assert "<AMBIGUOUS" in out
    assert "WORKSPACE_SAFETY_PORTAL = 111" not in out
    assert "flip_block_incomplete" in out


def test_d3_duplicate_parent_workspace_fails_closed(monkeypatch, capsys):
    """C2 — D3 does not create the workspace, but it still fails closed on a duplicate one:
    two OWNER workspaces named 'ITS — System' ⇒ _resolve_unique_parent returns None, every
    sheet is blocked-parent, nothing is created, exit 1."""
    tenant = FakeTenant(
        workspaces=[
            _ws(111, d3.WORKSPACE_NAME, permalink="https://app.smartsheet.com/ws/a"),
            _ws(222, d3.WORKSPACE_NAME, permalink="https://app.smartsheet.com/ws/b"),
        ],
        folders={111: [{"id": fid, "name": n} for n, fid in _SYSTEM_FOLDER_IDS.items()]},
    )
    _install_tenant(monkeypatch, d3, tenant)
    sheet_create = _stub_sheet_create(monkeypatch, tenant)
    _stub_daemon_health_columns(monkeypatch)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d3.main() == 1

    tenant.post.assert_not_called()
    sheet_create.assert_not_called()
    out = capsys.readouterr().out
    assert "duplicate_parent_ambiguity" in out
    assert "111" in out and "222" in out
    assert "blocked-parent" in out
    assert "flip_block_incomplete" in out


# =========================================================================
# 13. D4 UNAUTHENTICATED FAIL-LOUD
# =========================================================================


def test_d4_auth_failure_exits_nonzero_before_any_create(monkeypatch, capsys):
    monkeypatch.setattr(
        d4,
        "_list_box_root",
        MagicMock(side_effect=d4.box_client.BoxAuthError("refresh token revoked")),
    )
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _no_prompt(monkeypatch)  # an auth failure must never reach the prompt
    _argv(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        d4.main()

    assert exc.value.code == 2
    create.assert_not_called()
    err = capsys.readouterr().err
    assert "authentication FAILED" in err
    assert "setup_box_oauth.py" in err


def test_d4_non_auth_box_failure_exits_nonzero_before_any_create(monkeypatch, capsys):
    monkeypatch.setattr(
        d4,
        "_list_box_root",
        MagicMock(side_effect=d4.box_client.BoxError("503 from Box")),
    )
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _no_prompt(monkeypatch)
    _argv(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        d4.main()

    assert exc.value.code == 3
    create.assert_not_called()
    assert "nothing was created" in capsys.readouterr().err


def test_d4_auth_probe_runs_before_the_create_pass_even_in_dry_run(monkeypatch):
    """The probe is the fail-loud control; it must precede BOTH modes' find pass."""
    monkeypatch.setattr(
        d4,
        "_list_box_root",
        MagicMock(side_effect=d4.box_client.BoxAuthError("expired")),
    )
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _no_prompt(monkeypatch)
    _argv(monkeypatch, "--dry-run")

    with pytest.raises(SystemExit) as exc:
        d4.main()

    assert exc.value.code == 2
    create.assert_not_called()


# =========================================================================
# 13b. D4 BOX-IDENTITY DISCRIMINATOR (the D4 wrong-account control)
# =========================================================================
#
# Box has NO "OWNER of root" concept — every user owns their own root — so the Smartsheet
# accessLevel==OWNER hard-stop D1/D2/D3 use against the sandbox-shared-into-production trap
# has no in-band analog. D4's control is a HUMAN one: `_resolve_identity` reads the
# authenticated account (`_whoami`) and prints it LOUDLY on every run; a login that is not
# `EXPECTED_BOX_LOGIN` raises `[WARN] box_identity_mismatch`; and `_confirm_live_writes` NAMES
# that account in the y/N prompt, so a wrong / personal / sandbox identity is caught at the
# gate instead of silently creating the ITS roots in the wrong account and leaking their ids
# into the FLIP BLOCK. WARN-not-block: a non-production identity can be legitimate in
# validation, so the human confirmation IS the control.


def test_d4_reports_expected_identity_without_a_mismatch_warn(monkeypatch, capsys):
    """Normal flow — authenticated as EXPECTED_BOX_LOGIN: the identity is printed LOUDLY and
    NO mismatch WARN fires."""
    root = [
        {"type": "folder", "id": "111", "name": "ITS Safety Reports"},
        {"type": "folder", "id": "222", "name": "ITS Progress Reports"},
        {"type": "folder", "id": "333", "name": "ITS Archive"},
    ]
    _stub_box_root(monkeypatch, root)  # _whoami defaults to EXPECTED_BOX_LOGIN
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _answer(monkeypatch, True)
    _argv(monkeypatch)

    assert d4.main() == 0

    create.assert_not_called()  # both roots adopted
    out = capsys.readouterr().out
    assert f"authenticated as: {d4.EXPECTED_BOX_LOGIN}" in out
    assert "box_identity_mismatch" not in out


def test_d4_identity_mismatch_warns_and_names_the_login_in_the_prompt(monkeypatch, capsys):
    """A login != EXPECTED_BOX_LOGIN raises box_identity_mismatch naming BOTH logins, threads
    the ACTUAL login into the y/N prompt, and — WARN-not-block — STILL creates once the operator
    confirms (Box has no ownership discriminator to fail closed on; confirmation IS the control)."""
    _stub_box_root(monkeypatch, [])  # both roots absent -> create branch...
    _stub_whoami(monkeypatch, "seth@personal.com", "Seth Personal")  # ...under the WRONG account
    create = MagicMock(side_effect=["555", "666", "777"])
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    captured: dict[str, str] = {}

    def _capturing_input(prompt: str = "") -> str:
        captured["prompt"] = prompt
        return "y"

    # Let the REAL _confirm_live_writes run so its prompt string is observable.
    monkeypatch.setattr("builtins.input", _capturing_input)
    _argv(monkeypatch)

    assert d4.main() == 0

    out = capsys.readouterr().out
    assert "box_identity_mismatch" in out
    assert "seth@personal.com" in out            # the ACTUAL login named...
    assert d4.EXPECTED_BOX_LOGIN in out           # ...alongside the EXPECTED one
    # The identity appears in the confirmation prompt the operator must answer.
    assert "seth@personal.com" in captured["prompt"]
    # WARN-not-block: the operator confirmed, so the creates STILL proceed.
    assert [c.args for c in create.call_args_list] == [
        ("0", "ITS Safety Reports"),
        ("0", "ITS Progress Reports"),
        ("0", "ITS Archive"),
    ]


def test_d4_dry_run_prints_the_identity_and_never_prompts(monkeypatch, capsys):
    """--dry-run surfaces the identity (and any mismatch WARN) at PLAN time but makes NO create
    call and NEVER prompts — the whole point is the operator sees WHICH account before flipping."""
    _stub_box_root(monkeypatch, [])
    _stub_whoami(monkeypatch, "seth@personal.com", "Seth Personal")  # a mismatch must still surface
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _no_prompt(monkeypatch)  # a prompt under --dry-run is an immediate failure
    _argv(monkeypatch, "--dry-run")

    assert d4.main() == 0

    create.assert_not_called()
    out = capsys.readouterr().out
    assert "authenticated as: seth@personal.com" in out
    assert "box_identity_mismatch" in out
    assert "[dry-run]" in out


def test_d4_whoami_auth_failure_exits_nonzero_before_any_create(monkeypatch, capsys):
    """A _whoami that RAISES BoxAuthError fails loud exactly like the root-probe path: nonzero
    exit, the setup_box_oauth.py instruction, and NO create — an identity we cannot read is
    never silently skipped."""
    _stub_box_root(monkeypatch, [])  # the ROOT read succeeds (auth probe passes)...
    monkeypatch.setattr(  # ...but the IDENTITY read fails
        d4,
        "_whoami",
        MagicMock(side_effect=d4.box_client.BoxAuthError("token revoked mid-run")),
    )
    create = MagicMock()
    monkeypatch.setattr(d4.box_client, "get_or_create_folder", create)
    _no_prompt(monkeypatch)  # an auth failure must never reach the prompt
    _argv(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        d4.main()

    assert exc.value.code == 2
    create.assert_not_called()
    err = capsys.readouterr().err
    assert "authentication FAILED" in err
    assert "setup_box_oauth.py" in err


# =========================================================================
# MINIMAL SET (invariant 4) — the canonical name lists themselves
# =========================================================================


def test_canonical_object_lists_are_exactly_the_deliverable():
    assert [n for n, _c in d1.FOLDERS] == [
        "01 — Config", "02 — Logs", "03 — Queues", "04 — Daemons",
    ]
    assert d2.CANONICAL_FOLDER_NAMES == ("00_Safety Portal", "00_Form Catalog")
    assert [name for name, _f, _c, _cols in d3.SHEETS] == [
        "ITS_Config", "ITS_Errors", "ITS_Quarantine", "ITS_Review_Queue", "ITS_Daemon_Health",
    ]
    assert [n for n, _k, _w in d4.ROOT_FOLDERS] == [
        "ITS Safety Reports", "ITS Progress Reports", "ITS Archive",
    ]
    assert [k for _n, k, _w in d4.ROOT_FOLDERS] == [
        "safety_reports.box.portal_root_folder_id",
        "progress_reports.box.portal_root_folder_id",
        # Track 6. The SUFFIX differs from its two siblings on purpose (this is an archive
        # root, not a portal root) and that difference is load-bearing at cutover — see
        # production_repoint.ALLOWED_SETTING_SUFFIXES, which must list it or the row is
        # silently skipped and production keeps a sandbox folder id.
        "field_ops.box.archive_root_folder_id",
    ]
    assert d4.BOX_ROOT_FOLDER_ID == "0"


# =========================================================================
# Column-description API cap (errorCode 1041) — found live 2026-07-23
# =========================================================================


def test_cap_descriptions_truncates_over_limit_and_preserves_rest():
    cols = [
        {"title": "Long", "type": "TEXT_NUMBER", "description": "x" * 300},
        {"title": "Short", "type": "TEXT_NUMBER", "description": "fine"},
        {"title": "NoDesc", "type": "TEXT_NUMBER"},
    ]
    capped = d3._cap_descriptions("Sheet", cols)
    assert len(capped[0]["description"]) <= d3.SMARTSHEET_COLUMN_DESCRIPTION_MAX
    assert capped[1]["description"] == "fine"
    assert "description" not in capped[2]
    # source dicts are never mutated — the cap applies to the payload copy only
    assert len(cols[0]["description"]) == 300


def test_every_system_sheet_schema_survives_the_payload_cap():
    """The five schemas keep full prose in source; the PAYLOAD must always fit.
    The first fresh-tenant run failed 4/5 sheets on 275-308 char descriptions —
    this pins the payload path, not the prose."""
    for name, _folder, _const, cols in d3.SHEETS:
        for col in d3._cap_descriptions(name, cols):
            desc = col.get("description")
            if desc is not None:
                assert len(desc) <= d3.SMARTSHEET_COLUMN_DESCRIPTION_MAX, (name, col["title"])
