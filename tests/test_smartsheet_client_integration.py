"""Live-API integration tests for shared/smartsheet_client.py helpers.

Why this file exists:
    PRs #47/#48/#49 each surfaced one body-shape mismatch the SDK accepted
    silently but the live Smartsheet API rejected. The class of bug:
    `SimpleNamespace`-based mocks at the SDK boundary don't enforce the
    live API's contract on body shape, required fields, or value
    wrapping (e.g. EnumeratedValue vs plain string). Three consecutive
    hotfix PRs is too many.

    This file exercises the full create → list → update → delete cycle
    against a real Smartsheet sandbox sheet. Any future shape drift
    surfaces here in one pass instead of three iterations.

How to run:
    Default `pytest -q` SKIPS this file (per pyproject.toml addopts:
    -m 'not integration'). To run:

        pytest -m integration

    Requires ITS_SMARTSHEET_TOKEN in macOS Keychain (the same source
    the runtime SDK uses). Without that, the test module-level
    `_token_available` fixture skips the whole module cleanly.

    Each test creates a sandbox sheet, exercises one cycle, then
    deletes the sheet in its `finally` block — no orphan state, even
    on test failure.

When to run:
    - Before merging any change to shared/smartsheet_client.py.
    - Before merging any change to shared/picklist_sync.py that touches
      the SDK call sites.
    - Periodically (operator judgment) to catch upstream SDK drift.

NOT run in CI: GitHub Actions doesn't have access to the operator's
Keychain. Running these in CI would require a sandbox token in
repository secrets, which is a deliberate decision the operator
hasn't made.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import requests  # type: ignore[import-untyped]
import smartsheet  # type: ignore[import-untyped]

from shared import keychain, sheet_ids, smartsheet_client

# Every test here creates a sandbox sheet then immediately reads/writes/deletes it,
# so all are exposed to Smartsheet's create→read/write eventual-consistency flapping
# (transient errorCode 1006 / HTTP 404 for several seconds after create; see
# docs/tech_debt.md "Smartsheet integration tests flake on create→read/write"). The
# entry's approach 1 (test-level reruns, no SUT churn — the retry must NOT live in
# shared/smartsheet_client.py, where a 404 must surface in production): each rerun
# re-runs the whole test against a FRESH sheet, so a transient not-found clears. A
# real assertion failure still surfaces after the reruns are exhausted. reruns_delay
# gives the lagging replica time to catch up before the retry.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.flaky(reruns=3, reruns_delay=2),
]


class _SecretToken:
    """Wraps the real ITS_SMARTSHEET_TOKEN so its value can never leak into a
    pytest failure traceback.

    pytest renders a failing test's fixture/argument values via ``repr()``.
    A fixture that returned the raw token string therefore printed the live
    secret into the traceback when one of these tests failed — which forced a
    real token rotation this session. ``__repr__`` here redacts (and ``str()``
    / f-strings fall back to it), so the value only escapes via an explicit
    ``.reveal()`` call — the REST cleanup helpers below are the sole callers.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the raw token. Call only where the real value is required
        (the ``Authorization: Bearer`` header in REST cleanup)."""
        return self._value

    def __repr__(self) -> str:
        return "<ITS_SMARTSHEET_TOKEN redacted>"


@pytest.fixture(scope="module")
def _token_available() -> _SecretToken:
    """Skip the whole module if ITS_SMARTSHEET_TOKEN isn't in Keychain.

    Returns the token wrapped in `_SecretToken` so the raw value cannot
    render in a failure traceback (see the class docstring).
    """
    try:
        token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    except Exception as e:
        pytest.skip(f"ITS_SMARTSHEET_TOKEN unavailable: {e!r}")
    if not token:
        pytest.skip("ITS_SMARTSHEET_TOKEN returned empty")
    return _SecretToken(token)


@pytest.fixture(scope="module", autouse=True)
def _reset_smartsheet_client() -> Iterator[None]:
    """Force a fresh real-token Smartsheet client for this module.

    `smartsheet_client._client` is a process-wide singleton built lazily from
    the keychain token. In an isolated `pytest -m integration` run the
    conftest keychain opt-out already guarantees it is built with the real
    token, so this fixture is a no-op there. But in a MIXED-process run (full
    suite / `pytest -m ''` / IDE "run all"), an earlier unit test runs with
    the autouse keychain stub active and can prime `_client` with the fake
    `"test-ITS_SMARTSHEET_TOKEN"` — which would then 401 here. Resetting on
    entry forces a rebuild from the (now real) keychain; resetting on exit
    keeps this module's real-token client from leaking into a unit test that
    runs afterward in the same process.
    """
    smartsheet_client._client = None
    yield
    smartsheet_client._client = None


def _delete_sheet_rest(sheet_id: int, token: _SecretToken) -> None:
    """Cleanup helper — direct REST DELETE (no SDK wrapper today).

    Takes the redacting `_SecretToken` (not a raw str) so the value cannot
    render in a traceback frame; `.reveal()` is called only to build the
    Authorization header.
    """
    requests.delete(
        f"https://api.smartsheet.com/2.0/sheets/{sheet_id}",
        headers={"Authorization": f"Bearer {token.reveal()}"},
    )


def _delete_folder_rest(folder_id: int, token: _SecretToken) -> None:
    """Cleanup helper — direct REST DELETE for a folder (no SDK wrapper today).

    Takes the redacting `_SecretToken` wrapper; see `_delete_sheet_rest`.
    """
    requests.delete(
        f"https://api.smartsheet.com/2.0/folders/{folder_id}",
        headers={"Authorization": f"Bearer {token.reveal()}"},
    )


def _sandbox_name(label: str) -> str:
    """Build a sandbox sheet name <= 50 chars (Smartsheet's hard limit
    on sheet.name; surfaced live during the first integration-test run
    as errorCode 1041).

    Layout: `_int_<label>_HHMMSS_µµµµµµ` — drops the date prefix to save
    9 chars and shortens the namespace prefix from `_integration_` (12)
    to `_int_` (5). HHMMSS + microseconds keeps uniqueness within a run.
    For `label="update_round_trip_multi"` (the longest label here): 5 +
    23 + 1 + 13 = 42 chars. Plenty of headroom for any new label up to
    ~30 chars before bumping the ceiling again.
    """
    ts = datetime.now(UTC).strftime("%H%M%S_%f")
    name = f"_int_{label}_{ts}"
    assert len(name) <= 50, (
        f"sandbox name {name!r} is {len(name)} chars; Smartsheet sheet "
        f"names must be <= 50 (errorCode 1041). Shorten label."
    )
    return name


# ---- list_columns_with_options: type normalization ---------------------


def test_list_columns_with_options_unwraps_picklist_type(_token_available):
    """list_columns_with_options must return col['type'] as plain str.

    Regression guard for PR #49: the live SDK wraps `type` for
    option-bearing columns in an `EnumeratedValue`. If the helper
    doesn't unwrap, downstream `update_column_options` calls send a
    body without `type` (the SDK strips the wrapped value silently)
    and the API rejects with errorCode 1090.

    Also covers MULTI_PICKLIST (multi-select dropdown): earlier notes here claimed
    Smartsheet returns `type=TEXT_NUMBER` for a MULTI_PICKLIST column read back after
    creation and dismissed it as an unfixable "API quirk". That was WRONG — the real
    cause was that `list_columns_with_options` read columns WITHOUT `level=2`, which the
    API requires to report MULTI_* column types (they downgrade to their base type
    otherwise). Fixed 2026-07-14 (level=2). This test now creates a MULTI_PICKLIST column
    and asserts it reads back AS MULTI_PICKLIST — the prove-it-bites for that fix (without
    level=2 the read-back is TEXT_NUMBER and this assertion fails).
    """
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("type_unwrap"),
        [
            {"title": "id_col", "type": "TEXT_NUMBER", "primary": True},
            {"title": "pl_col", "type": "PICKLIST", "options": ["seed"]},
            {"title": "mpl_col", "type": "MULTI_PICKLIST", "options": ["m-seed"]},
        ],
    )
    try:
        cols = smartsheet_client.list_columns_with_options(sheet_id)
        by_title = {c["title"]: c for c in cols}

        # type must be a plain str for all columns.
        assert isinstance(by_title["id_col"]["type"], str)
        assert by_title["id_col"]["type"] == "TEXT_NUMBER"

        assert isinstance(by_title["pl_col"]["type"], str)
        assert by_title["pl_col"]["type"] == "PICKLIST"
        assert by_title["pl_col"]["options"] == ["seed"]

        # MULTI_PICKLIST must report its TRUE type (level=2), not the TEXT_NUMBER
        # downgrade — the core of the 2026-07-14 fix.
        assert by_title["mpl_col"]["type"] == "MULTI_PICKLIST"
        assert by_title["mpl_col"]["options"] == ["m-seed"]
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


# ---- update_column_options: full round-trip ----------------------------


def test_update_column_options_round_trip_picklist(_token_available):
    """Full add cycle: create sheet → list → update options → list → verify.

    Verifies the body shape requirements landed by PRs #47, #48, #49 all
    hold end-to-end against the live API:
      - id NOT in body (PR #47, errorCode 1032)
      - type IS in body (PR #48, errorCode 1090)
      - type is plain str (PR #49)
    """
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("update_round_trip"),
        [
            {"title": "id_col", "type": "TEXT_NUMBER", "primary": True},
            {"title": "pl_col", "type": "PICKLIST", "options": ["seed"]},
        ],
    )
    try:
        cols = smartsheet_client.list_columns_with_options(sheet_id)
        pl_col = next(c for c in cols if c["title"] == "pl_col")
        new_options = ["Alpha", "Bravo", "Charlie"]

        smartsheet_client.update_column_options(
            sheet_id, pl_col["id"], new_options, column_type=pl_col["type"]
        )

        cols = smartsheet_client.list_columns_with_options(sheet_id)
        pl_col_after = next(c for c in cols if c["title"] == "pl_col")
        assert sorted(pl_col_after["options"]) == sorted(new_options)
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


# MULTI_PICKLIST TYPE read-back is now exercised in
# test_list_columns_with_options_unwraps_picklist_type above (the level=2 fix,
# 2026-07-14). The old note here — that Smartsheet returns type=TEXT_NUMBER for
# MULTI_PICKLIST and that it's an unfixable API quirk — was WRONG (missing level=2).
# A full add/update MULTI_PICKLIST *options* round-trip stays covered at unit level
# (tests/test_smartsheet_client.py::test_update_column_options_accepts_multi_picklist).


def test_update_column_options_replaces_not_appends(_token_available):
    """The API replaces the whole options list — confirm seed value is gone."""
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("replace_semantics"),
        [
            {"title": "id_col", "type": "TEXT_NUMBER", "primary": True},
            {"title": "pl_col", "type": "PICKLIST",
                "options": ["original_seed_value"]},
        ],
    )
    try:
        cols = smartsheet_client.list_columns_with_options(sheet_id)
        pl_col = next(c for c in cols if c["title"] == "pl_col")
        assert "original_seed_value" in pl_col["options"]

        smartsheet_client.update_column_options(
            sheet_id, pl_col["id"], ["NewOnly"], column_type=pl_col["type"]
        )

        cols = smartsheet_client.list_columns_with_options(sheet_id)
        pl_col_after = next(c for c in cols if c["title"] == "pl_col")
        # Original seed gone — replace semantics, not append.
        assert pl_col_after["options"] == ["NewOnly"]
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


# ---- ensure_picklist_options: additive, idempotent, no-removal ----------


def test_ensure_picklist_options_additive_round_trip(_token_available):
    """Live §30: additive ensure preserves existing options + order, appends
    only the missing, is idempotent on re-run, and previews without writing.

    This is the SDK-vs-Live guard for the picklist-drift reconcile: a
    SimpleNamespace mock would not catch the REPLACE-style body shape that the
    additive wrapper depends on (read current → union → write the full union).
    """
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("ensure_additive"),
        [
            {"title": "id_col", "type": "TEXT_NUMBER", "primary": True},
            {"title": "pl_col", "type": "PICKLIST",
                "options": ["seed_a", "seed_b"]},
        ],
    )
    try:
        # Add seed_b (already present) + two new — only the new two append,
        # existing seeds + order preserved.
        result = smartsheet_client.ensure_picklist_options(
            sheet_id, "pl_col", ["seed_b", "new_x", "new_y"],
        )
        # result.final_options is OUR deterministic construction (current+missing),
        # so its order is asserted exactly.
        assert result.applied is True
        assert result.added == ("new_x", "new_y")
        assert result.final_options == ("seed_a", "seed_b", "new_x", "new_y")

        live = smartsheet_client.list_columns_with_options(sheet_id)
        pl_after = next(c for c in live if c["title"] == "pl_col")
        # The LIVE re-read is compared as a SET — Smartsheet does not guarantee
        # API-side option-order preservation (see update_column_options docstring),
        # so an exact-order assert here would flake. The invariants that matter:
        # no removal (seeds survive) + the new values are present.
        assert set(pl_after["options"]) == {"seed_a", "seed_b", "new_x", "new_y"}
        assert "seed_a" in pl_after["options"] and "seed_b" in pl_after["options"]

        # Idempotent: re-running the same request issues no write.
        again = smartsheet_client.ensure_picklist_options(
            sheet_id, "pl_col", ["seed_b", "new_x", "new_y"],
        )
        assert again.applied is False
        assert again.added == ()
        assert again.final_options == ("seed_a", "seed_b", "new_x", "new_y")

        # dry_run previews the next addition without mutating the live column.
        preview = smartsheet_client.ensure_picklist_options(
            sheet_id, "pl_col", ["new_z"], dry_run=True,
        )
        assert preview.applied is False
        assert preview.added == ("new_z",)
        live2 = smartsheet_client.list_columns_with_options(sheet_id)
        pl_preview = next(c for c in live2 if c["title"] == "pl_col")
        assert "new_z" not in pl_preview["options"]
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


# ---- create_picklist_column: additive column create --------------------


def test_create_picklist_column_live_round_trip(_token_available):
    """Live §30: add a PICKLIST column to an existing sheet → read it back.

    The SDK-vs-Live guard for the Phase 3a add-column path: `add_columns` is a
    DIFFERENT POST flow from sheet-creation columns, and a SimpleNamespace mock
    can't prove the live API accepts the `{title,type,index,options}` body or
    that `list_columns_with_options` reads the new column back as a real
    PICKLIST with its seeded options (the audit's pass/fail check). Creates the
    sheet with one TEXT_NUMBER primary, adds the picklist column, asserts the
    read-back, then deletes the whole sheet in `finally`.
    """
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("create_pl_col"),
        [{"title": "id_col", "type": "TEXT_NUMBER", "primary": True}],
    )
    try:
        options = ["RELEASE", "DELETE", "ESCALATE"]
        col_id = smartsheet_client.create_picklist_column(
            sheet_id, "Disposition", options,
        )
        assert isinstance(col_id, int)

        cols = smartsheet_client.list_columns_with_options(sheet_id)
        by_title = {c["title"]: c for c in cols}
        assert "Disposition" in by_title, "new column must read back by title"
        added = by_title["Disposition"]
        assert added["id"] == col_id
        assert added["type"] == "PICKLIST"
        # Seeded options must match exactly (this is what audit_picklist_drift
        # compares the live allowed-set against — order-insensitive).
        assert set(added["options"]) == set(options)
        # Appended after the primary (default index == existing column count).
        assert cols[-1]["title"] == "Disposition"
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


# ---- find_sheet_by_name_in_folder + create_sheet_in_folder ------------


def test_find_sheet_by_name_in_folder_round_trip(_token_available):
    """Create → find → confirm match → cleanup. Idempotency-helper contract."""
    name = _sandbox_name("find_round_trip")
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        name,
        [{"title": "id_col", "type": "TEXT_NUMBER", "primary": True}],
    )
    try:
        found_id = smartsheet_client.find_sheet_by_name_in_folder(
            sheet_ids.FOLDER_SYSTEM_CONFIG, name
        )
        assert found_id == sheet_id

        # Negative case: a name that doesn't exist returns None.
        missing = smartsheet_client.find_sheet_by_name_in_folder(
            sheet_ids.FOLDER_SYSTEM_CONFIG, name + "_DOES_NOT_EXIST"
        )
        assert missing is None
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


# ---- find_folder_by_name_in_folder + create_folder_in_folder ----------


def test_find_folder_by_name_in_folder_round_trip(_token_available):
    """Create folder → find → cleanup. Mirrors the sheet round-trip.

    Sandbox parent is FOLDER_SYSTEM_CONFIG to match the existing
    integration-test precedent (no dedicated test-only folder constant
    today; see PR description for the trade-off discussion). The
    sandbox folder is name-namespaced via `_sandbox_name`, so it's
    visually distinguishable from real config artifacts and gets
    deleted in `finally` regardless of test outcome.
    """
    name = _sandbox_name("find_folder")
    folder_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, name
    )
    try:
        found_id = smartsheet_client.find_folder_by_name_in_folder(
            sheet_ids.FOLDER_SYSTEM_CONFIG, name
        )
        assert found_id == folder_id

        # Negative case: a name that doesn't exist returns None.
        missing = smartsheet_client.find_folder_by_name_in_folder(
            sheet_ids.FOLDER_SYSTEM_CONFIG, name + "_DOES_NOT_EXIST"
        )
        assert missing is None
    finally:
        _delete_folder_rest(folder_id, _token_available)


# ---- move_sheet_to_folder (§51 archive-on-closure relocation) ----------


def test_move_sheet_to_folder_relocates_live(_token_available):
    """Live §30: create a sheet in a SOURCE folder → move it to a DEST folder →
    confirm it landed in DEST and is gone from SOURCE. Pure relocation, no delete.

    SDK-vs-Live guard: a SimpleNamespace mock cannot prove the live API accepts the
    `ContainerDestination({destination_type:'folder', destination_id:...})` body that
    `move_sheet_to_folder` builds — this exercises the real POST /sheets/{id}/move.

    Both sandbox folders live under FOLDER_SYSTEM_CONFIG (the existing integration-test
    precedent — no dedicated test-only folder constant today) and are name-namespaced
    via `_sandbox_name`, so they're visually distinct from real artifacts. Everything
    (the sheet + BOTH folders) is deleted in `finally` regardless of outcome.
    """
    src_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("mv_src")
    )
    dst_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("mv_dst")
    )
    sheet_name = _sandbox_name("mv_sheet")
    sheet_id = smartsheet_client.create_sheet_in_folder(
        src_id,
        sheet_name,
        [{"title": "id_col", "type": "TEXT_NUMBER", "primary": True}],
    )
    try:
        # Precondition: the sheet is in SOURCE, not yet in DEST.
        assert smartsheet_client.find_sheet_by_name_in_folder(src_id, sheet_name) == sheet_id
        assert smartsheet_client.find_sheet_by_name_in_folder(dst_id, sheet_name) is None

        smartsheet_client.move_sheet_to_folder(sheet_id, dst_id)

        # Postcondition: relocated — present in DEST, absent from SOURCE (never deleted).
        assert smartsheet_client.find_sheet_by_name_in_folder(dst_id, sheet_name) == sheet_id
        assert smartsheet_client.find_sheet_by_name_in_folder(src_id, sheet_name) is None
    finally:
        _delete_sheet_rest(sheet_id, _token_available)
        _delete_folder_rest(src_id, _token_available)
        _delete_folder_rest(dst_id, _token_available)


# ---- Track 6 folder relocation (move_folder / rename_folder / the resume probe) --------
#
# These exist because a mocked test structurally CANNOT answer the questions the archive
# design rests on: does a folder move preserve the IDs and cell history of the sheets
# inside it, does a cross-WORKSPACE move behave like an in-workspace one, and does the move
# endpoint really ignore `new_name`. The existing move_sheet_to_folder docstring asserted
# "history is preserved" on no evidence at all — its own live smoke had never been run.


def test_move_folder_to_folder_relocates_live(_token_available):
    """Live §30: a folder — WITH a sheet inside — moves between folders, IDs intact.

    Asserts the property the whole archive depends on: this is a RELOCATION, not a
    copy-and-delete. The folder id and the contained sheet id must both survive, because
    the durable archive ledger keys off exactly those ids to answer "already moved?".
    """
    src_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("mvf_src")
    )
    dst_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("mvf_dst")
    )
    job_name = _sandbox_name("mvf_job")
    job_id = smartsheet_client.create_folder_in_folder(src_id, job_name)
    sheet_name = _sandbox_name("mvf_sheet")
    sheet_id = smartsheet_client.create_sheet_in_folder(
        job_id, sheet_name, [{"title": "id_col", "type": "TEXT_NUMBER", "primary": True}]
    )
    try:
        assert smartsheet_client.find_folder_by_name_in_folder(src_id, job_name) == job_id
        assert smartsheet_client.find_folder_by_name_in_folder(dst_id, job_name) is None

        smartsheet_client.move_folder_to_folder(job_id, dst_id)

        # The folder itself kept its ID and simply re-parented.
        assert smartsheet_client.find_folder_by_name_in_folder(dst_id, job_name) == job_id
        assert smartsheet_client.find_folder_by_name_in_folder(src_id, job_name) is None
        # And the sheet INSIDE it came along, same id — one call moved the whole subtree.
        assert smartsheet_client.find_sheet_by_name_in_folder(job_id, sheet_name) == sheet_id
    finally:
        _delete_sheet_rest(sheet_id, _token_available)
        _delete_folder_rest(job_id, _token_available)
        _delete_folder_rest(src_id, _token_available)
        _delete_folder_rest(dst_id, _token_available)


def test_move_folder_across_workspaces_preserves_history_live(_token_available):
    """Live §30: THE case the archive actually performs — a CROSS-WORKSPACE folder move.

    Every per-job Safety/Progress folder lives directly under its workspace, so archiving
    crosses a workspace boundary. Proves the sheet's cell HISTORY survives — the claim the
    pre-existing move_sheet_to_folder docstring made with no test behind it. If history did
    not survive, "archive" would silently mean "lose the audit trail".
    """
    src_folder = smartsheet_client.create_folder_in_workspace(
        sheet_ids.WORKSPACE_SYSTEM, _sandbox_name("xws_job")
    )
    dst_parent = smartsheet_client.create_folder_in_workspace(
        sheet_ids.WORKSPACE_ARCHIVE, _sandbox_name("xws_dst")
    )
    sheet_name = _sandbox_name("xws_sheet")
    sheet_id = smartsheet_client.create_sheet_in_folder(
        src_folder, sheet_name, [{"title": "id_col", "type": "TEXT_NUMBER", "primary": True}]
    )
    try:
        # Write a cell so there IS history to preserve, then capture it pre-move.
        row_id = smartsheet_client.add_rows(sheet_id, [{"id_col": "before-the-move"}])[0]
        cols = smartsheet_client.list_columns_with_options(sheet_id)
        col_title = cols[0]["title"]
        before = smartsheet_client.get_cell_history(sheet_id, row_id, col_title)
        assert before, "precondition: the write should have produced cell history"

        smartsheet_client.move_folder_to_workspace(src_folder, sheet_ids.WORKSPACE_ARCHIVE)

        # Folder + sheet ids unchanged, and the sheet is reachable at its new home.
        assert smartsheet_client.find_sheet_by_name_in_folder(src_folder, sheet_name) == sheet_id
        after = smartsheet_client.get_cell_history(sheet_id, row_id, col_title)
        assert len(after) == len(before), "cell history must survive a cross-workspace move"
    finally:
        _delete_sheet_rest(sheet_id, _token_available)
        _delete_folder_rest(src_folder, _token_available)
        _delete_folder_rest(dst_parent, _token_available)


def test_move_folder_silently_ignores_new_name_live(_token_available):
    """Live §30: the footgun, proven rather than asserted in prose.

    `ContainerDestination` carries a `new_name` field because the model is SHARED with Copy
    Folder. Setting it on a MOVE serializes fine and is ignored by the API. Without this
    test the warning in smartsheet_client is folklore — and the two-call move-then-rename
    sequence (and its crash window) would look like unnecessary ceremony.
    """
    src_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("nn_src")
    )
    dst_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("nn_dst")
    )
    original = _sandbox_name("nn_job")
    job_id = smartsheet_client.create_folder_in_folder(src_id, original)
    try:
        dest = smartsheet.models.ContainerDestination({
            "destination_type": "folder",
            "destination_id": dst_id,
            "new_name": "Safety",          # accepted by the model, ignored by /move
        })
        smartsheet_client.get_client().Folders.move_folder(job_id, dest)

        assert smartsheet_client.get_folder_name(job_id) == original, (
            "move must NOT rename — if this ever starts passing as 'Safety', the API gained "
            "newName support on /move and rename_folder's second call can be dropped"
        )
        assert smartsheet_client.find_folder_by_name_in_folder(dst_id, original) == job_id
    finally:
        _delete_folder_rest(job_id, _token_available)
        _delete_folder_rest(src_id, _token_available)
        _delete_folder_rest(dst_id, _token_available)


def test_move_then_rename_sequence_and_idempotent_rename_live(_token_available):
    """Live §30: the real archive sequence, plus the property that makes it resumable.

    move → rename is NOT atomic, so a crash can leave a folder moved-but-not-renamed.
    Re-issuing the rename must be a harmless no-op, which is what turns that intermediate
    state into "run it again" instead of a wedge.
    """
    dst_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("seq_dst")
    )
    src_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("seq_src")
    )
    job_id = smartsheet_client.create_folder_in_folder(src_id, _sandbox_name("seq_job"))
    label = _sandbox_name("Safety")
    try:
        smartsheet_client.move_folder_to_folder(job_id, dst_id)
        smartsheet_client.rename_folder(job_id, label)
        assert smartsheet_client.get_folder_name(job_id) == label
        assert smartsheet_client.find_folder_by_name_in_folder(dst_id, label) == job_id

        # Idempotent: the resume path re-issues this without checking first.
        smartsheet_client.rename_folder(job_id, label)
        assert smartsheet_client.get_folder_name(job_id) == label
    finally:
        _delete_folder_rest(job_id, _token_available)
        _delete_folder_rest(src_id, _token_available)
        _delete_folder_rest(dst_id, _token_available)


def test_move_folder_to_a_dead_destination_raises_not_found_live(_token_available):
    """Live §30 RED-light: a bad destination must raise the typed error, not pass silently."""
    src_id = smartsheet_client.create_folder_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG, _sandbox_name("dead_src")
    )
    try:
        with pytest.raises(smartsheet_client.SmartsheetError):
            smartsheet_client.move_folder_to_folder(src_id, 1)  # id 1 is not a real folder
    finally:
        _delete_folder_rest(src_id, _token_available)


def test_get_workspace_access_level_live(_token_available):
    """Live §30: the ADMIN pre-flight returns a real level for a workspace ITS owns."""
    level = smartsheet_client.get_workspace_access_level(sheet_ids.WORKSPACE_ARCHIVE)
    # The archive path requires ADMIN or OWNER on both ends; anything else must fail closed.
    assert level in {"ADMIN", "OWNER"}, (
        f"the ITS token identity has {level!r} on WORKSPACE_ARCHIVE — folder moves need "
        f"ADMIN_WORKSPACES, so archiving would 403 at runtime"
    )


# ---- find_row_by_primary + update_row_cells_by_id (PR #59.5) ------------


def test_find_row_by_primary_live_round_trip(_token_available):
    """Create sheet → add 2 rows → find by primary → update by ID → re-read.

    Exercises both new helpers against the live API in one cycle to catch
    body-shape drift the unit tests' SDK mocks can't catch (the PR #47/#48/#49
    failure mode this integration file was created for).
    """
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("find_by_primary"),
        [
            {"title": "Name", "type": "TEXT_NUMBER", "primary": True},
            {"title": "Status", "type": "TEXT_NUMBER"},
            {"title": "Count", "type": "TEXT_NUMBER"},
        ],
    )
    try:
        # Add two rows so the find_by_primary lookup has to discriminate.
        row_ids = smartsheet_client.add_rows(
            sheet_id,
            [
                {"Name": "alpha", "Status": "OK", "Count": "0"},
                {"Name": "beta",  "Status": "WARN", "Count": "5"},
            ],
        )
        assert len(row_ids) == 2

        # Look up the live columns to discover the Name column ID — the
        # primary-key lookup is by ID, so we need the live ID.
        cols = smartsheet_client.list_columns_with_options(sheet_id)
        name_col_id = next(c["id"] for c in cols if c["title"] == "Name")
        status_col_id = next(c["id"] for c in cols if c["title"] == "Status")
        count_col_id = next(c["id"] for c in cols if c["title"] == "Count")

        # find_row_by_primary returns the matching row's title-keyed dict.
        beta = smartsheet_client.find_row_by_primary(sheet_id, name_col_id, "beta")
        assert beta is not None
        assert beta["Name"] == "beta"
        assert beta["Status"] == "WARN"
        assert beta["_row_id"] == row_ids[1]

        # find_row_by_primary returns None on a missing primary value.
        gamma = smartsheet_client.find_row_by_primary(sheet_id, name_col_id, "gamma")
        assert gamma is None

        # update_row_cells_by_id updates by column ID, no title-cache lookup.
        smartsheet_client.update_row_cells_by_id(
            sheet_id,
            row_ids[1],
            {status_col_id: "OK", count_col_id: "99"},
        )

        # Re-read confirms the update landed.
        beta_after = smartsheet_client.find_row_by_primary(sheet_id, name_col_id, "beta")
        assert beta_after is not None
        assert beta_after["Status"] == "OK"
        assert beta_after["Count"] == "99"
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


def test_add_row_by_id_live_round_trip(_token_available):
    """Create sheet → add_row_by_id (ID-keyed create) → find → verify (A1).

    Guards the self-provision create path against the body-shape drift the
    unit-test SDK mocks can't catch — specifically the `result.result[0].id`
    return shape and the column_id-keyed Cell payload. Mirrors the
    find_row_by_primary round-trip; self-cleans by deleting the throwaway sheet.
    """
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("add_row_by_id"),
        [
            {"title": "Name", "type": "TEXT_NUMBER", "primary": True},
            {"title": "Status", "type": "TEXT_NUMBER"},
        ],
    )
    try:
        cols = smartsheet_client.list_columns_with_options(sheet_id)
        name_col_id = next(c["id"] for c in cols if c["title"] == "Name")
        status_col_id = next(c["id"] for c in cols if c["title"] == "Status")

        new_id = smartsheet_client.add_row_by_id(
            sheet_id,
            {name_col_id: "delta", status_col_id: "OK"},
        )
        assert isinstance(new_id, int)

        found = smartsheet_client.find_row_by_primary(sheet_id, name_col_id, "delta")
        assert found is not None
        assert found["_row_id"] == new_id
        assert found["Status"] == "OK"
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


def test_update_row_cells_by_id_raises_not_found_on_missing_row(_token_available):
    """A 404 on a non-existent row id surfaces as SmartsheetNotFoundError.

    Regression guard for the heartbeat-cache 404 invalidation path —
    shared/heartbeat.py's HeartbeatReporter relies on this exception type
    to know when to invalidate the heartbeat row-id cache.
    """
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("not_found_row"),
        [
            {"title": "Name", "type": "TEXT_NUMBER", "primary": True},
        ],
    )
    try:
        cols = smartsheet_client.list_columns_with_options(sheet_id)
        name_col_id = next(c["id"] for c in cols if c["title"] == "Name")
        bogus_row_id = 1  # No row with id 1 exists on a fresh sheet.
        with pytest.raises(smartsheet_client.SmartsheetNotFoundError):
            smartsheet_client.update_row_cells_by_id(
                sheet_id,
                bogus_row_id,
                {name_col_id: "anything"},
            )
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


def test_verify_write_capability_live(_token_available):
    """B2: the real write-capability probe creates a throwaway sheet (proving
    the live token can WRITE) and returns its id; cleanup goes through
    `delete_sheet_settling`, exercising the TIGHT back-to-back create→delete
    with NO settle wait.

    This is the regression lock for the B2-smoke finding: an immediate delete
    after create can 404 / errorCode 5036 (create→delete eventual consistency).
    The earlier version called the plain `delete_sheet` and passed only by
    winning the timing race; the settle retry makes it reliable. (Same flake
    class as the docs/tech_debt.md create→read entry.)

    A healthy read-write token passes; a read-only/mis-scoped token would raise
    SmartsheetWriteCapabilityError at the create step — which is the whole point.
    """
    sheet_id = smartsheet_client.verify_write_capability()
    assert isinstance(sheet_id, int)
    smartsheet_client.delete_sheet_settling(sheet_id)


# ---- list_workspace_share_emails: F22 approval-authority source ---------


def test_list_workspace_share_emails_live(_token_available):
    """Live §30: read the ITS — Safety Portal workspace share list — the F22
    approval-authority source (workspace membership). Read-only, no cleanup.

    Also the send-leg service-account permission probe: a 403 here means the ITS
    token cannot read WORKSPACE_SAFETY_PORTAL's shares (it must at least be shared
    on the workspace). Asserts the return is a frozenset of lowercased, @-bearing
    emails — the normalized shape `verify_approval` consumes.
    """
    emails = smartsheet_client.list_workspace_share_emails(
        sheet_ids.WORKSPACE_SAFETY_PORTAL
    )
    assert isinstance(emails, frozenset)
    for e in emails:
        assert isinstance(e, str) and "@" in e
        assert e == e.lower().strip()  # normalized (lowercased + stripped)


def test_list_workspace_shares_live(_token_available):
    """Live §30-style smoke: the RAW share records backing VC-10 (the cutover
    gate's F22 share audit). Read-only, no cleanup. Proves the live
    /workspaces/{id}/shares payload shape the mock-level tests hand-author —
    in particular that USER shares carry `email` and every record carries
    `accessLevel` (the GROUP-detection assumption: a share with no `email` is
    a group). (2026-07-23 adversarial-review advisory: run before relying on
    VC-10 at the actual cutover.)"""
    shares = smartsheet_client.list_workspace_shares(
        sheet_ids.WORKSPACE_SAFETY_PORTAL
    )
    assert isinstance(shares, tuple) and shares
    for share in shares:
        assert isinstance(share, dict)
        assert share.get("accessLevel")
        # every share is USER (has email) or GROUP (no email, has name/groupId)
        assert share.get("email") or share.get("name") or share.get("groupId")


def test_get_workspace_name_live(_token_available):
    """Live §30 smoke for the VC-10 stale-constant guard: GET /workspaces/{id}
    returns the human name for a live sheet_ids constant. Read-only."""
    name = smartsheet_client.get_workspace_name(sheet_ids.WORKSPACE_SAFETY_PORTAL)
    assert isinstance(name, str) and name.strip()


# ---- attach_pdf_to_row: live multipart upload + idempotent replace ------


def test_attach_pdf_to_row_live_round_trip(_token_available):
    """Live §30: attach a PDF to a row → list → re-attach (same name) → verify
    exactly one current attachment. SimpleNamespace SDK mocks can't catch the
    multipart file-upload body shape; this proves the live API accepts it AND that
    the replace (delete-same-named-then-attach) leaves exactly one copy."""
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("attach_pdf"),
        [{"title": "Name", "type": "TEXT_NUMBER", "primary": True}],
    )
    try:
        [row_id] = smartsheet_client.add_rows(sheet_id, [{"Name": "row-1"}])
        pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
        smartsheet_client.attach_pdf_to_row(sheet_id, row_id, "proof.pdf", pdf)
        atts = smartsheet_client.get_client().Attachments.list_row_attachments(
            sheet_id, row_id
        ).data
        assert [a.name for a in atts] == ["proof.pdf"]
        # Re-attach with the SAME name → replace, not accumulate → still exactly one.
        smartsheet_client.attach_pdf_to_row(sheet_id, row_id, "proof.pdf", pdf)
        atts2 = smartsheet_client.get_client().Attachments.list_row_attachments(
            sheet_id, row_id
        ).data
        assert [a.name for a in atts2] == ["proof.pdf"]
    finally:
        _delete_sheet_rest(sheet_id, _token_available)


def test_apply_column_styles_live_round_trip(_token_available):
    """Live §30 (PR-I): width applies on read-back; a column format applies + is
    INHERITED by a cell (the format isn't returned on the column itself — only via a
    cell, which the SimpleNamespace mocks can't model). Proves the live PUT path +
    the format-descriptor string the styling relies on."""
    sheet_id = smartsheet_client.create_sheet_in_folder(
        sheet_ids.FOLDER_SYSTEM_CONFIG,
        _sandbox_name("col_style"),
        [{"title": "Name", "type": "TEXT_NUMBER", "primary": True}],
    )
    try:
        smartsheet_client.apply_column_styles(
            sheet_id, [{"title": "Name", "width": 260, "format": ",,1,,,,,,38,7,,,,,,,"}]
        )
        cols = smartsheet_client.get_client().Sheets.get_columns(
            sheet_id, include_all=True
        ).data
        assert next(c for c in cols if c.title == "Name").width == 260
        # The format applies at the column level → a new cell inherits it.
        smartsheet_client.add_rows(sheet_id, [{"Name": "row-1"}])
        sheet = smartsheet_client.get_client().Sheets.get_sheet(sheet_id, include=["format"])
        assert getattr(sheet.rows[0].cells[0], "format", None) == ",,1,,,,,,38,7,,,,,,,"
    finally:
        _delete_sheet_rest(sheet_id, _token_available)
