"""Tests for shared/smartsheet_client.py.

All SDK calls are mocked — these tests never hit Smartsheet's API and never
read the real Keychain. The module-level client + column-map caches are
reset between tests via the autouse `reset_state` fixture.

Run with: pytest -q tests/test_smartsheet_client.py
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
import smartsheet
import smartsheet.exceptions as sdk_exc

from shared import sheet_ids, smartsheet_client
from shared.smartsheet_client import (
    SmartsheetAuthError,
    SmartsheetCircuitOpenError,
    SmartsheetError,
    SmartsheetNotFoundError,
    SmartsheetPermissionError,
    SmartsheetRateLimitError,
    SmartsheetTransientError,
    SmartsheetValidationError,
    SmartsheetWriteCapabilityError,
)

# ---- Fixtures + helpers --------------------------------------------------


@pytest.fixture(autouse=True)
def reset_state(mocker):
    """Reset module-level caches and stub keychain reads for every test."""
    mocker.patch.object(smartsheet_client, "_client", None)
    smartsheet_client._column_maps.clear()
    mocker.patch(
        "shared.smartsheet_client.keychain.get_secret",
        side_effect=lambda key, *a, **kw: f"fake-{key}",
    )


def _api_error(status_code: int, *, code: int = 0, message: str = "boom") -> sdk_exc.ApiError:
    """Build a realistic ApiError with status_code on its nested result."""
    result = SimpleNamespace(status_code=status_code, code=code, message=message)
    error = SimpleNamespace(result=result)
    return sdk_exc.ApiError(error, message=message)


def _install_client(mocker) -> MagicMock:
    """Patch get_client() to return a mock SDK client."""
    client = MagicMock()
    mocker.patch.object(smartsheet_client, "get_client", return_value=client)
    return client


def _column(col_id: int, title: str) -> SimpleNamespace:
    return SimpleNamespace(id=col_id, title=title)


def _row(row_id: int, cells: list[tuple[int, object]]) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        cells=[SimpleNamespace(column_id=cid, value=v) for cid, v in cells],
    )


# ---- get_client lazy init -------------------------------------------------


def test_get_client_lazy_inits_once(mocker):
    sdk = mocker.patch("shared.smartsheet_client.smartsheet.Smartsheet")
    sdk.return_value = MagicMock()

    c1 = smartsheet_client.get_client()
    c2 = smartsheet_client.get_client()

    assert c1 is c2
    assert sdk.call_count == 1


def test_get_client_mounts_timeout_adapter(mocker):
    """A2: a default-timeout adapter is mounted on the SDK session for https + http
    so every SDK HTTP call is bounded (the SDK session has no default timeout)."""
    sdk = mocker.patch("shared.smartsheet_client.smartsheet.Smartsheet")
    client = MagicMock()
    sdk.return_value = client

    smartsheet_client.get_client()

    mounts = {c.args[0]: c.args[1] for c in client._session.mount.call_args_list}
    assert set(mounts) == {"https://", "http://"}
    for adapter in mounts.values():
        assert isinstance(adapter, smartsheet_client._TimeoutHTTPAdapter)
        assert adapter._timeout == smartsheet_client.SDK_NETWORK_TIMEOUT


def test_timeout_adapter_injects_default_timeout_when_absent(mocker):
    """The adapter injects its default timeout only when the caller omitted one;
    an explicit per-call timeout is preserved."""
    adapter = smartsheet_client._TimeoutHTTPAdapter(timeout=17)
    sent = mocker.patch("requests.adapters.HTTPAdapter.send")

    adapter.send(MagicMock())  # no timeout supplied
    assert sent.call_args.kwargs["timeout"] == 17

    adapter.send(MagicMock(), timeout=5)  # explicit timeout preserved
    assert sent.call_args.kwargs["timeout"] == 5


# ---- Exception translation -----------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, SmartsheetValidationError),
        (401, SmartsheetAuthError),
        (403, SmartsheetPermissionError),
        (404, SmartsheetNotFoundError),
        (429, SmartsheetRateLimitError),
        # 5xx is the SELF-HEALING class the SDK does NOT retry when the body carries
        # errorCode 4000 — it must translate to the transient type or neither the
        # bounded retry nor the pass-boundary fence can recognise it.
        (500, SmartsheetTransientError),
        (503, SmartsheetTransientError),
    ],
)
def test_api_error_translated_by_status(mocker, status, expected):
    client = _install_client(mocker)
    client.Sheets.get_sheet.side_effect = _api_error(status, message="nope")

    with pytest.raises(expected, match="nope"):
        smartsheet_client.get_sheet(123)
    # Every typed class stays a SmartsheetError so existing consumers + the breaker's
    # `count=SmartsheetError` are untouched by the new subclass.
    assert issubclass(expected, SmartsheetError)


def test_unexpected_request_error_translated_as_transient(mocker):
    """A requests-level ReadTimeout surfaces as UnexpectedRequestError from the SDK's
    `_request` BEFORE its retry loop can see it — the 2026-07-21 CRITICAL signature."""
    client = _install_client(mocker)
    client.Sheets.get_sheet.side_effect = sdk_exc.UnexpectedRequestError(
        requests.exceptions.ReadTimeout("timed out"), None
    )

    with pytest.raises(SmartsheetTransientError):
        smartsheet_client.get_sheet(123)


def test_http_error_translated(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.side_effect = sdk_exc.HttpError(502, b"bad gateway")

    with pytest.raises(SmartsheetTransientError, match="502"):
        smartsheet_client.get_sheet(123)


@pytest.mark.parametrize("status", [400, 401, 403, 407])
def test_non_5xx_http_error_is_not_transient(mocker, status):
    """A non-JSON error body is NOT by itself evidence of a self-healing fault: a captive
    portal / corporate proxy / Cloudflare challenge answers 4xx with an HTML page. Gating
    on status keeps that deterministic access problem paging, instead of being retried 3×
    and then softened to an ERROR by the pass-boundary fence."""
    client = _install_client(mocker)
    client.Sheets.get_sheet.side_effect = sdk_exc.HttpError(
        status, b"<html>proxy authentication required</html>"
    )

    with pytest.raises(SmartsheetError) as exc_info:
        smartsheet_client.get_sheet(123)

    assert not isinstance(exc_info.value, SmartsheetTransientError)


# ---- Column-map cache ----------------------------------------------------


def test_column_map_cached_after_first_fetch(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(1, "Setting"), _column(2, "Value")],
        rows=[],
    )

    assert smartsheet_client._column_map(99) == {"Setting": 1, "Value": 2}
    assert smartsheet_client._column_map(99) == {"Setting": 1, "Value": 2}

    assert client.Sheets.get_sheet.call_count == 1


def test_column_map_refetched_when_title_missing(mocker):
    # Cache built from sheet v1 (no "NewCol"); a row with "NewCol" forces a refetch
    # which now finds it. This is the *addition* case the doc covers.
    client = _install_client(mocker)
    v1 = SimpleNamespace(columns=[_column(1, "Setting"), _column(2, "Value")], rows=[])
    v2 = SimpleNamespace(
        columns=[_column(1, "Setting"), _column(2, "Value"), _column(3, "NewCol")],
        rows=[],
    )
    client.Sheets.get_sheet.side_effect = [v1, v2]

    smartsheet_client._column_map(7)  # warm the cache
    cells = smartsheet_client._resolve_cells(7, {"NewCol": "hi"})

    assert [c.column_id for c in cells] == [3]
    assert client.Sheets.get_sheet.call_count == 2


def test_renamed_column_still_keyerrors_after_refetch(mocker):
    # The rename failure mode the module docstring calls out: a refetch can't
    # invent the old title back into existence, so callers fail fast.
    client = _install_client(mocker)
    renamed = SimpleNamespace(columns=[_column(1, "NewName")], rows=[])
    client.Sheets.get_sheet.return_value = renamed

    # Cache is empty; _resolve_cells will fetch once (gets the renamed schema),
    # see "OldName" is missing, invalidate, refetch — and still miss.
    with pytest.raises(KeyError, match="OldName"):
        smartsheet_client._resolve_cells(7, {"OldName": "x"})

    # Refetch path means at least two get_sheet calls — confirms the
    # invalidate-and-refetch happened before we gave up.
    assert client.Sheets.get_sheet.call_count >= 2


def test_invalidate_column_cache_targeted_and_global(mocker):
    smartsheet_client._column_maps[1] = {"A": 10}
    smartsheet_client._column_maps[2] = {"B": 20}

    smartsheet_client.invalidate_column_cache(1)
    assert 1 not in smartsheet_client._column_maps
    assert 2 in smartsheet_client._column_maps

    smartsheet_client.invalidate_column_cache()
    assert smartsheet_client._column_maps == {}


# ---- _resolve_cells per-cell format + apply_column_styles (PR-I) ----------


def test_resolve_cells_attaches_per_cell_format_and_skips_meta():
    smartsheet_client._column_maps[1] = {"Name": 6, "Status": 5}
    try:
        cells = smartsheet_client._resolve_cells(
            1,
            {"Name": "x", "Status": "Active",
             "_formats": {"Status": ",,,,,,,,,7,,,,,,,"}, "_row_id": 9},
        )
    finally:
        smartsheet_client._column_maps.clear()
    by_col = {c.column_id: c for c in cells}
    # _formats + _row_id are meta — never columns.
    assert set(by_col) == {6, 5}
    assert by_col[5].format == ",,,,,,,,,7,,,,,,,"  # Status carries the format
    assert getattr(by_col[6], "format", None) in (None, "")  # Name unformatted


def test_apply_column_styles_sets_width_and_format(mocker):
    client = _install_client(mocker)
    client.Sheets.get_columns.return_value = SimpleNamespace(data=[
        SimpleNamespace(title="Submission", id=11, index=0),
        SimpleNamespace(title="Status", id=12, index=7),
    ])
    smartsheet_client.apply_column_styles(1, [
        {"title": "Submission", "width": 320, "format": ",,1,,,,,,38,7,,,,,,,"},
        {"title": "Status", "width": 110},
    ])
    assert client.Sheets.update_column.call_count == 2
    sheet_id, col_id, model = client.Sheets.update_column.call_args_list[0].args
    assert sheet_id == 1 and col_id == 11
    assert model.width == 320 and model.format == ",,1,,,,,,38,7,,,,,,,"


def test_apply_column_styles_empty_is_noop(mocker):
    client = _install_client(mocker)
    smartsheet_client.apply_column_styles(1, [])
    client.Sheets.get_columns.assert_not_called()


def test_apply_column_styles_unknown_title_raises(mocker):
    client = _install_client(mocker)
    client.Sheets.get_columns.return_value = SimpleNamespace(
        data=[SimpleNamespace(title="X", id=1, index=0)]
    )
    with pytest.raises(KeyError):
        smartsheet_client.apply_column_styles(1, [{"title": "Nope", "width": 100}])


# ---- get_rows + filtering -------------------------------------------------


def test_get_rows_returns_title_keyed_dicts(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(1, "Setting"), _column(2, "Value"), _column(3, "Workstream")],
        rows=[
            _row(101, [(1, "kill_switch_state"), (2, "ACTIVE"), (3, "global")]),
            _row(102, [(1, "anomaly_threshold"), (2, "0.85"), (3, "safety_reports")]),
        ],
    )

    rows = smartsheet_client.get_rows(42)

    assert rows == [
        {
            "_row_id": 101,
            "Setting": "kill_switch_state",
            "Value": "ACTIVE",
            "Workstream": "global",
        },
        {
            "_row_id": 102,
            "Setting": "anomaly_threshold",
            "Value": "0.85",
            "Workstream": "safety_reports",
        },
    ]


def test_get_rows_filters_match_all_keys_and_equality(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(1, "Setting"), _column(2, "Workstream")],
        rows=[
            _row(1, [(1, "x"), (2, "global")]),
            _row(2, [(1, "x"), (2, "safety_reports")]),
            _row(3, [(1, "y"), (2, "global")]),
        ],
    )

    rows = smartsheet_client.get_rows(
        42, filters={"Setting": "x", "Workstream": "global"}
    )
    assert [r["_row_id"] for r in rows] == [1]


# ---- get_setting ---------------------------------------------------------


def test_get_setting_returns_value_for_matching_workstream(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(1, "Setting"), _column(2, "Value"), _column(3, "Workstream")],
        rows=[
            _row(1, [(1, "kill_switch_state"), (2, "ACTIVE"), (3, "global")]),
            _row(2, [(1, "kill_switch_state"), (2, "PAUSED"), (3, "safety_reports")]),
        ],
    )

    assert (
        smartsheet_client.get_setting("kill_switch_state", workstream="safety_reports")
        == "PAUSED"
    )


def test_get_setting_requires_workstream_kwarg():
    # Positional call must fail — defending against the "silently default to
    # global" footgun that PR feedback called out.
    with pytest.raises(TypeError):
        smartsheet_client.get_setting("kill_switch_state", "global")  # type: ignore[misc]


def test_get_setting_missing_row_raises_not_found(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(1, "Setting"), _column(2, "Value"), _column(3, "Workstream")],
        rows=[],
    )

    with pytest.raises(SmartsheetNotFoundError, match="missing_key"):
        smartsheet_client.get_setting("missing_key", workstream="global")


def test_get_setting_queries_config_sheet(mocker):
    # Wiring check: the call must hit sheet_ids.SHEET_CONFIG, not a hardcoded
    # ID or some other sheet.
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(1, "Setting"), _column(2, "Value"), _column(3, "Workstream")],
        rows=[_row(1, [(1, "k"), (2, "v"), (3, "global")])],
    )

    smartsheet_client.get_setting("k", workstream="global")

    client.Sheets.get_sheet.assert_called_with(sheet_ids.SHEET_CONFIG)


# ---- add_rows / update_rows ----------------------------------------------


def test_add_rows_builds_payload_with_resolved_column_ids(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[
            _column(11, "Error"),
            _column(12, "Severity"),
            _column(13, "Script"),
        ],
        rows=[],
    )
    client.Sheets.add_rows.return_value = SimpleNamespace(
        result=[SimpleNamespace(id=9001), SimpleNamespace(id=9002)]
    )

    new_ids = smartsheet_client.add_rows(
        sheet_ids.SHEET_ERRORS,
        [
            {"Error": "boom", "Severity": "WARN", "Script": "watchdog"},
            {"Error": "second", "Severity": "INFO", "Script": "watchdog"},
        ],
    )

    assert new_ids == [9001, 9002]
    call_sheet_id, call_rows = client.Sheets.add_rows.call_args.args
    assert call_sheet_id == sheet_ids.SHEET_ERRORS
    assert len(call_rows) == 2
    # First row's resolved cells, by column_id:
    first_cells = {c.column_id: c.value for c in call_rows[0].cells}
    assert first_cells == {11: "boom", 12: "WARN", 13: "watchdog"}
    assert call_rows[0].to_bottom is True


def test_add_rows_empty_input_is_noop(mocker):
    client = _install_client(mocker)
    assert smartsheet_client.add_rows(42, []) == []
    client.Sheets.add_rows.assert_not_called()


def test_update_rows_requires_row_id(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(11, "Error")], rows=[]
    )
    with pytest.raises(ValueError, match="_row_id"):
        smartsheet_client.update_rows(42, [{"Error": "x"}])


def test_update_rows_strips_row_id_from_cells(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(11, "Error"), _column(12, "Severity")],
        rows=[],
    )

    smartsheet_client.update_rows(
        42, [{"_row_id": 500, "Error": "msg", "Severity": "INFO"}]
    )

    sent_rows = client.Sheets.update_rows.call_args.args[1]
    assert sent_rows[0].id == 500
    cell_titles = {c.column_id for c in sent_rows[0].cells}
    assert cell_titles == {11, 12}  # no spurious cell for _row_id


def test_write_error_translated(mocker):
    # add_rows must use the same translation path as reads.
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(11, "Error")], rows=[]
    )
    client.Sheets.add_rows.side_effect = _api_error(403, message="no write here")

    with pytest.raises(SmartsheetPermissionError, match="no write here"):
        smartsheet_client.add_rows(42, [{"Error": "x"}])


# ---- SDK 404 noise filter ------------------------------------------------


def _sdk_log_record(level: int, status_code: int) -> logging.LogRecord:
    """Build a LogRecord shaped exactly like the SDK's _log_request emission.

    The SDK calls `self._log.error('{...}', status_code, reason, content)` —
    the format-string's first positional arg is the status code. The filter
    inspects `record.args[0]`, so the record we build here is the minimum
    fixture that exercises that path.
    """
    return logging.LogRecord(
        name=smartsheet_client.SDK_LOGGER_NAME,
        level=level,
        pathname=__file__,
        lineno=0,
        msg='{"response": {"statusCode": %d, "reason": "%s", "content": %s}}',
        args=(status_code, "Not Found", '{"errorCode": 1006, "message": "Not Found"}'),
        exc_info=None,
    )


def test_404_filter_installed_on_sdk_logger():
    sdk_logger = logging.getLogger(smartsheet_client.SDK_LOGGER_NAME)
    assert any(
        isinstance(f, smartsheet_client._Suppress404JSON) for f in sdk_logger.filters
    ), "_Suppress404JSON filter was not installed at module-import time"


def test_404_filter_drops_404_error_record():
    filt = smartsheet_client._Suppress404JSON()
    record = _sdk_log_record(logging.ERROR, 404)

    assert filt.filter(record) is False


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_404_filter_passes_other_status_codes(status):
    filt = smartsheet_client._Suppress404JSON()
    record = _sdk_log_record(logging.ERROR, status)

    assert filt.filter(record) is True


def test_404_filter_passes_non_error_levels():
    # INFO/DEBUG/WARN records pass through even when args[0] would match —
    # only ERROR is the noise we care about.
    filt = smartsheet_client._Suppress404JSON()
    for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.CRITICAL):
        record = _sdk_log_record(level, 404)
        assert filt.filter(record) is True, f"level {level} was unexpectedly dropped"


def test_404_filter_passes_records_with_no_args():
    # Defensive: an ERROR record with no formatting args has nothing to inspect.
    filt = smartsheet_client._Suppress404JSON()
    record = logging.LogRecord(
        name=smartsheet_client.SDK_LOGGER_NAME,
        level=logging.ERROR,
        pathname=__file__,
        lineno=0,
        msg="some bare string",
        args=None,
        exc_info=None,
    )
    assert filt.filter(record) is True


# ---- get_settings_with_prefix -------------------------------------------


def _config_rows_sheet(rows):
    """Build a fake sheet with the ITS_Config column layout for get_rows."""
    titles = ["Setting", "Value", "Workstream"]
    cols = [_column(i + 1, t) for i, t in enumerate(titles)]
    sheet_rows = []
    for i, r in enumerate(rows, start=1):
        cells = [
            (1, r.get("Setting")),
            (2, r.get("Value")),
            (3, r.get("Workstream")),
        ]
        sheet_rows.append(_row(i, cells))
    return SimpleNamespace(columns=cols, rows=sheet_rows)


def test_get_settings_with_prefix_filters_by_prefix(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = _config_rows_sheet([
        {"Setting": "mail_intake.safety.max_idle_hours",
         "Value": "96", "Workstream": "global"},
        {"Setting": "mail_intake.procurement.max_idle_hours",
         "Value": "48", "Workstream": "global"},
        {"Setting": "system.state", "Value": "ACTIVE", "Workstream": "global"},
        {"Setting": "spend.absolute_floor_usd",
         "Value": "5.00", "Workstream": "global"},
    ])
    out = smartsheet_client.get_settings_with_prefix("mail_intake.")
    assert out == {
        "mail_intake.safety.max_idle_hours": "96",
        "mail_intake.procurement.max_idle_hours": "48",
    }


def test_get_settings_with_prefix_workstream_filter(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = _config_rows_sheet([
        {"Setting": "x.key1", "Value": "1", "Workstream": "global"},
        {"Setting": "x.key2", "Value": "2", "Workstream": "safety_reports"},
    ])
    out = smartsheet_client.get_settings_with_prefix(
        "x.", workstream="safety_reports",
    )
    assert out == {"x.key2": "2"}


def test_get_settings_with_prefix_skips_non_string_values(mocker):
    """A row whose Value cell is non-string is dropped (matches get_setting contract)."""
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = _config_rows_sheet([
        {"Setting": "x.string", "Value": "good", "Workstream": "global"},
        {"Setting": "x.numeric", "Value": 42, "Workstream": "global"},
        {"Setting": "x.none", "Value": None, "Workstream": "global"},
    ])
    out = smartsheet_client.get_settings_with_prefix("x.")
    assert out == {"x.string": "good"}


def test_get_settings_with_prefix_empty_when_no_match(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = _config_rows_sheet([
        {"Setting": "system.state", "Value": "ACTIVE", "Workstream": "global"},
    ])
    assert smartsheet_client.get_settings_with_prefix("absent.") == {}


def test_404_filter_suppresses_emission_through_real_logger(caplog):
    # End-to-end via the actual logger: emit through the installed pipeline
    # and confirm caplog never sees the 404 record. caplog hooks into the
    # logger's filter chain, so a filter-dropped record stays out.
    with caplog.at_level(logging.ERROR, logger=smartsheet_client.SDK_LOGGER_NAME):
        sdk_logger = logging.getLogger(smartsheet_client.SDK_LOGGER_NAME)
        sdk_logger.error(
            '{"response": {"statusCode": %d, "reason": "%s", "content": %s}}',
            404,
            "Not Found",
            '{"errorCode": 1006}',
        )
        sdk_logger.error(
            '{"response": {"statusCode": %d, "reason": "%s", "content": %s}}',
            500,
            "Server Error",
            '{"errorCode": 9999}',
        )

    # The 500 record should pass; the 404 record should not.
    messages = [r.getMessage() for r in caplog.records]
    assert any("500" in m for m in messages)
    assert not any('"statusCode": 404' in m for m in messages)


# ---- list_columns_with_options / update_column_options ------------------


def test_list_columns_with_options_returns_normalized_dicts(mocker):
    client = _install_client(mocker)
    cols = [
        SimpleNamespace(id=1, title="Setting", type="TEXT_NUMBER", options=None),
        SimpleNamespace(id=2, title="Status", type="PICKLIST", options=["a", "b", "c"]),
        SimpleNamespace(id=3, title="Tags", type="MULTI_PICKLIST", options=["x", "y"]),
    ]
    sheet = SimpleNamespace(columns=cols)
    client.Sheets.get_sheet.return_value = sheet

    out = smartsheet_client.list_columns_with_options(42)

    assert out == [
        {"id": 1, "title": "Setting", "type": "TEXT_NUMBER", "options": []},
        {"id": 2, "title": "Status", "type": "PICKLIST", "options": ["a", "b", "c"]},
        {"id": 3, "title": "Tags", "type": "MULTI_PICKLIST", "options": ["x", "y"]},
    ]
    # level=2 is LOAD-BEARING: without it the API downgrades a MULTI_PICKLIST column to
    # TEXT_NUMBER, which broke ensure_picklist_options + false-flagged audit_picklist_drift
    # on live multi-select dropdowns (2026-07-14 — ITS_Subcontractors.Trades / ITS_Vendors).
    client.Sheets.get_sheet.assert_called_once_with(42, include="columns", level=2)


def test_list_columns_with_options_unwraps_enumerated_value_type(mocker):
    """Regression guard: live SDK returns `col.type` as an EnumeratedValue
    wrapper, not a string. If we pass that wrapper back into a Column
    body (for update_column_options), the SDK silently strips it and
    the API rejects with errorCode 1090. Caller must receive a plain
    string. Discovered live during PR #48 re-smoke."""
    client = _install_client(mocker)

    class _FakeColumnType:
        # Mirrors the SDK's ColumnType enum member: `.name` is the string.
        def __init__(self, name):
            self.name = name

    class _FakeEnumeratedValue:
        # Mirrors smartsheet.types.EnumeratedValue: `.value` is the enum
        # member, which has `.name` as the wire string.
        def __init__(self, name):
            self.value = _FakeColumnType(name)

    cols = [
        SimpleNamespace(
            id=2, title="Status",
            type=_FakeEnumeratedValue("PICKLIST"),
            options=["a"],
        ),
    ]
    sheet = SimpleNamespace(columns=cols)
    client.Sheets.get_sheet.return_value = sheet

    out = smartsheet_client.list_columns_with_options(42)
    # type must be a plain str — otherwise update_column_options will
    # fail later with errorCode 1090.
    assert isinstance(out[0]["type"], str)
    assert out[0]["type"] == "PICKLIST"


def test_list_columns_with_options_translates_api_error(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.side_effect = _api_error(404, message="sheet not found")

    with pytest.raises(SmartsheetNotFoundError):
        smartsheet_client.list_columns_with_options(42)


def test_update_column_options_calls_sdk_and_invalidates_cache(mocker):
    client = _install_client(mocker)
    # Seed the column cache so we can assert the invalidation.
    smartsheet_client._column_maps[42] = {"Status": 99}

    smartsheet_client.update_column_options(
        42, 99, ["A", "B", "C"], column_type="PICKLIST"
    )

    client.Sheets.update_column.assert_called_once()
    args, _ = client.Sheets.update_column.call_args
    assert args[0] == 42
    assert args[1] == 99
    col = args[2]
    # Smartsheet's PUT /sheets/{sheetId}/columns/{columnId} rejects `id`
    # in the request body (errorCode 1032). The column_id flows through
    # the URL path (args[1]), not the model — the body must omit `id`.
    assert col.id is None
    assert list(col.options) == ["A", "B", "C"]
    # `type` IS required (errorCode 1090) when updating options.
    assert col.type == "PICKLIST"
    # Cache invalidated after the update so subsequent reads refresh.
    assert 42 not in smartsheet_client._column_maps


def test_update_column_options_body_omits_id(mocker):
    """Regression guard: API rejects body with `id` (errorCode 1032).

    Discovered live during PR #46 picklist smoke test post-#45 merge —
    the SDK mock didn't validate body shape so the test suite passed
    but the real Smartsheet API returned HTTP 400.
    """
    client = _install_client(mocker)
    smartsheet_client.update_column_options(42, 99, ["A"], column_type="PICKLIST")

    body = client.Sheets.update_column.call_args.args[2]
    assert body.id is None
    serialized = body.to_dict()
    assert "id" not in serialized
    assert "columnId" not in serialized


def test_update_column_options_body_includes_type(mocker):
    """Regression guard: API requires `type` when updating options
    (errorCode 1090). Discovered live during PR #47 re-smoke."""
    client = _install_client(mocker)
    smartsheet_client.update_column_options(
        42, 99, ["A"], column_type="PICKLIST"
    )

    body = client.Sheets.update_column.call_args.args[2]
    assert body.type == "PICKLIST"
    assert "type" in body.to_dict()


def test_update_column_options_accepts_multi_picklist(mocker):
    client = _install_client(mocker)
    smartsheet_client.update_column_options(
        42, 99, ["A"], column_type="MULTI_PICKLIST"
    )

    body = client.Sheets.update_column.call_args.args[2]
    assert body.type == "MULTI_PICKLIST"


def test_update_column_options_translates_auth_error(mocker):
    client = _install_client(mocker)
    client.Sheets.update_column.side_effect = _api_error(401, message="bad token")

    with pytest.raises(SmartsheetAuthError):
        smartsheet_client.update_column_options(
            42, 99, ["A"], column_type="PICKLIST"
        )


def test_update_column_options_handles_empty_options_list(mocker):
    client = _install_client(mocker)

    smartsheet_client.update_column_options(42, 99, [], column_type="PICKLIST")

    args, _ = client.Sheets.update_column.call_args
    col = args[2]
    assert list(col.options) == []


# ---- ensure_picklist_options: additive / idempotent / dry-run -----------


def _pl_cols(options, *, title="Reason", col_id=7, col_type="PICKLIST"):
    """A list_columns_with_options-shaped return with one option-bearing column."""
    return [
        {"id": 1, "title": "Item ID", "type": "TEXT_NUMBER", "options": []},
        {"id": col_id, "title": title, "type": col_type, "options": list(options)},
    ]


def _patch_cols(mocker, cols):
    return mocker.patch(
        "shared.smartsheet_client.list_columns_with_options", return_value=cols,
    )


def test_ensure_picklist_options_appends_only_missing(mocker):
    _patch_cols(mocker, _pl_cols(["a", "b"]))
    upd = mocker.patch("shared.smartsheet_client.update_column_options")

    result = smartsheet_client.ensure_picklist_options(42, "Reason", ["b", "c", "d"])

    assert result.applied is True
    assert result.added == ("c", "d")
    # Existing order preserved; new values appended in request order.
    assert result.final_options == ("a", "b", "c", "d")
    # The write is the FULL union (REPLACE-style API), not just the delta.
    upd.assert_called_once_with(42, 7, ["a", "b", "c", "d"], column_type="PICKLIST")


def test_ensure_picklist_options_idempotent_noop_when_all_present(mocker):
    _patch_cols(mocker, _pl_cols(["a", "b", "c"]))
    upd = mocker.patch("shared.smartsheet_client.update_column_options")

    result = smartsheet_client.ensure_picklist_options(42, "Reason", ["a", "b"])

    assert result.applied is False
    assert result.added == ()
    assert result.final_options == ("a", "b", "c")
    upd.assert_not_called()  # no write on a pure no-op


def test_ensure_picklist_options_dry_run_does_not_write(mocker):
    _patch_cols(mocker, _pl_cols(["a"]))
    upd = mocker.patch("shared.smartsheet_client.update_column_options")

    result = smartsheet_client.ensure_picklist_options(
        42, "Reason", ["a", "b"], dry_run=True,
    )

    assert result.applied is False
    assert result.added == ("b",)
    assert result.final_options == ("a", "b")
    upd.assert_not_called()


def test_ensure_picklist_options_dedups_and_skips_empty(mocker):
    _patch_cols(mocker, _pl_cols(["a"]))
    mocker.patch("shared.smartsheet_client.update_column_options")

    # "a" already present (and duplicated); b duplicated; "" skipped.
    result = smartsheet_client.ensure_picklist_options(
        42, "Reason", ["a", "a", "b", "b", "c", ""],
    )
    assert result.added == ("b", "c")  # duplicate collapsed, "" skipped
    # already_present is deduped + empty-skipped the SAME way as added (no
    # asymmetry): "a" appears once, not twice.
    assert result.already_present == ("a",)


def test_ensure_picklist_options_accepts_generator_values(mocker):
    """Regression: a one-shot generator must not silently empty already_present.

    The classification is a SINGLE pass over `values`, so a generator is fully
    honored for BOTH added and already_present (the two-iteration bug would
    leave already_present empty).
    """
    _patch_cols(mocker, _pl_cols(["a", "b"]))
    mocker.patch("shared.smartsheet_client.update_column_options")

    result = smartsheet_client.ensure_picklist_options(
        42, "Reason", (v for v in ["a", "b", "c"]),  # generator
    )
    assert result.added == ("c",)
    assert result.already_present == ("a", "b")  # NOT () — generator fully read


def test_ensure_picklist_options_missing_column_raises(mocker):
    _patch_cols(mocker, _pl_cols(["a"], title="Reason"))
    with pytest.raises(ValueError, match="not found"):
        smartsheet_client.ensure_picklist_options(42, "Nonexistent", ["x"])


def test_ensure_picklist_options_non_picklist_column_raises(mocker):
    _patch_cols(
        mocker,
        [{"id": 1, "title": "Notes", "type": "TEXT_NUMBER", "options": []}],
    )
    with pytest.raises(ValueError, match="not an option-bearing"):
        smartsheet_client.ensure_picklist_options(42, "Notes", ["x"])


# ---- create_picklist_column: additive column create ---------------------


def _install_add_columns(mocker, new_col_id: int = 555):
    """Patch get_client so add_columns returns a created column with `new_col_id`."""
    client = _install_client(mocker)
    client.Sheets.add_columns.return_value = SimpleNamespace(
        result=[SimpleNamespace(id=new_col_id)]
    )
    return client


def test_create_picklist_column_body_shape_and_return(mocker):
    """Body carries title/type/options/index; return is the new column id."""
    client = _install_add_columns(mocker, new_col_id=777)
    # Two existing columns → default append index == 2.
    mocker.patch(
        "shared.smartsheet_client.list_columns_with_options",
        return_value=[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
    )

    col_id = smartsheet_client.create_picklist_column(
        42, "Workstream", ["safety_reports", "global"],
    )

    assert col_id == 777
    args, _ = client.Sheets.add_columns.call_args
    assert args[0] == 42
    bodies = args[1]
    assert isinstance(bodies, list) and len(bodies) == 1
    body = bodies[0]
    assert body.title == "Workstream"
    assert body.type == "PICKLIST"
    assert list(body.options) == ["safety_reports", "global"]
    assert body.index == 2  # appended after the two existing columns
    # No restrict by default → validation not set true.
    assert not getattr(body, "validation", False)


def test_create_picklist_column_explicit_index_skips_count_read(mocker):
    """An explicit index is used verbatim and avoids the list_columns read."""
    client = _install_add_columns(mocker)
    listed = mocker.patch("shared.smartsheet_client.list_columns_with_options")

    smartsheet_client.create_picklist_column(42, "X", ["a"], index=0)

    body = client.Sheets.add_columns.call_args.args[1][0]
    assert body.index == 0
    listed.assert_not_called()  # explicit index → no count round-trip


def test_create_picklist_column_restrict_sets_validation(mocker):
    client = _install_add_columns(mocker)
    smartsheet_client.create_picklist_column(
        42, "X", ["a"], index=0, restrict_to_options=True,
    )
    body = client.Sheets.add_columns.call_args.args[1][0]
    assert body.validation is True


def test_create_picklist_column_invalidates_cache(mocker):
    _install_add_columns(mocker)
    smartsheet_client._column_maps[42] = {"A": 1}

    smartsheet_client.create_picklist_column(42, "X", ["a"], index=0)

    assert 42 not in smartsheet_client._column_maps  # invalidated post-create


def test_create_picklist_column_translates_permission_error(mocker):
    client = _install_client(mocker)
    client.Sheets.add_columns.side_effect = _api_error(403, message="denied")

    with pytest.raises(SmartsheetPermissionError):
        smartsheet_client.create_picklist_column(42, "X", ["a"], index=0)


# ---- find_sheet_by_name_in_folder / create_sheet_in_folder --------------


def _rest_get_folder_response(sheets: list[dict] | None, status: int = 200):
    """Build a mock requests.Response for the GET /folders/{id} endpoint.

    `sheets` is the list of {"id": int, "name": str} dicts to embed in
    the response body; pass None to model an API that omits the field
    entirely. status=200 unless the test wants an error path.
    """
    response = MagicMock()
    response.status_code = status
    body: dict = {"id": 7, "name": "fake-folder"}
    if sheets is not None:
        body["sheets"] = sheets
    response.json.return_value = body
    if status >= 400:
        from requests import HTTPError
        err = HTTPError(f"HTTP {status}")
        err.response = response
        response.raise_for_status.side_effect = err
        response.text = body.get("message", "error")
    else:
        response.raise_for_status.return_value = None
    return response


def _rest_workspace_response(body: dict, status: int = 200):
    """Mock requests.Response for GET /workspaces/{id}?loadAll=true."""
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body
    response.raise_for_status.return_value = None
    return response


def test_count_workspace_sheets_recurses_nested_folders(mocker):
    # 2 top-level sheets + a folder (3 sheets) with a sub-folder (1 sheet) = 6
    body = {
        "id": 99,
        "sheets": [{"id": 1}, {"id": 2}],
        "folders": [{
            "id": 10,
            "sheets": [{"id": 3}, {"id": 4}, {"id": 5}],
            "folders": [{"id": 11, "sheets": [{"id": 6}], "folders": []}],
        }],
    }
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_workspace_response(body),
    )
    assert smartsheet_client.count_workspace_sheets(99) == 6


def test_count_workspace_sheets_empty_workspace(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_workspace_response({"id": 99}),
    )
    assert smartsheet_client.count_workspace_sheets(99) == 0


def test_find_sheet_by_name_in_folder_matches_title(mocker):
    # PR #51: helper switched to direct REST; stub requests.get instead of SDK.
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_get_folder_response([
            {"id": 111, "name": "ITS_Errors"},
            {"id": 222, "name": "Picklist_Sync_Config"},
            {"id": 333, "name": "Other"},
        ]),
    )

    result = smartsheet_client.find_sheet_by_name_in_folder(7, "Picklist_Sync_Config")
    assert result == 222


def test_find_sheet_by_name_in_folder_returns_none_when_absent(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_get_folder_response([{"id": 111, "name": "Other"}]),
    )

    assert smartsheet_client.find_sheet_by_name_in_folder(7, "Nope") is None


def test_find_sheet_by_name_in_folder_handles_empty_folder(mocker):
    # Response body omits the "sheets" key entirely.
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_get_folder_response(None),
    )

    assert smartsheet_client.find_sheet_by_name_in_folder(7, "Anything") is None


def test_find_sheet_by_name_in_folder_translates_permission_error(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_get_folder_response(None, status=403),
    )

    with pytest.raises(SmartsheetPermissionError):
        smartsheet_client.find_sheet_by_name_in_folder(7, "Anything")


def test_find_sheet_by_name_in_folder_returns_first_match_on_duplicate(mocker):
    """Smartsheet doesn't enforce title uniqueness inside a folder; if a
    duplicate exists, return the first match (deterministic + cheap)."""
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_get_folder_response([
            {"id": 111, "name": "Dup"},
            {"id": 222, "name": "Dup"},
        ]),
    )

    assert smartsheet_client.find_sheet_by_name_in_folder(7, "Dup") == 111


# ---- list_workspace_share_emails (F22 approval-authority source) ---------


def _rest_shares_response(data: list[dict] | None, status: int = 200):
    """Mock requests.Response for GET /workspaces/{id}/shares (the `data` array)."""
    response = MagicMock()
    response.status_code = status
    body: dict = {}
    if data is not None:
        body["data"] = data
    response.json.return_value = body
    if status >= 400:
        from requests import HTTPError
        err = HTTPError(f"HTTP {status}")
        err.response = response
        response.raise_for_status.side_effect = err
        response.text = body.get("message", "error")
    else:
        response.raise_for_status.return_value = None
    return response


def test_list_workspace_share_emails_parses_normalizes_and_excludes_groups(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_shares_response([
            {"email": "Alice@X.com", "accessLevel": "ADMIN", "type": "USER"},
            {"email": "bob@x.com", "accessLevel": "EDITOR", "type": "USER"},
            {"email": "alice@x.com", "accessLevel": "VIEWER", "type": "USER"},  # dup (case)
            {"accessLevel": "EDITOR", "type": "GROUP", "groupId": 9},           # no email → excluded
        ]),
    )
    out = smartsheet_client.list_workspace_share_emails(6820552519247748)
    assert out == frozenset({"alice@x.com", "bob@x.com"})


def test_list_workspace_share_emails_empty_returns_empty_frozenset(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_shares_response([]),
    )
    assert smartsheet_client.list_workspace_share_emails(1) == frozenset()


def test_list_workspace_share_emails_missing_data_key_returns_empty(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_shares_response(None),
    )
    assert smartsheet_client.list_workspace_share_emails(1) == frozenset()


def test_list_workspace_share_emails_translates_permission_error(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_shares_response(None, status=403),
    )
    with pytest.raises(SmartsheetPermissionError):
        smartsheet_client.list_workspace_share_emails(1)


# ---- attach_pdf_to_row ----------------------------------------------------


def test_attach_pdf_to_row_replaces_same_named_then_attaches(mocker):
    client = _install_client(mocker)
    client.Attachments.list_row_attachments.return_value = SimpleNamespace(data=[
        SimpleNamespace(id=11, name="doc.pdf"),
        SimpleNamespace(id=12, name="other.pdf"),
    ])
    client.Attachments.attach_file_to_row.return_value = SimpleNamespace(
        result=SimpleNamespace(id=999)
    )
    att_id = smartsheet_client.attach_pdf_to_row(7, 100, "doc.pdf", b"%PDF-1.4 data")
    assert att_id == 999
    # only the SAME-named prior attachment is deleted (idempotent replace)
    client.Attachments.delete_attachment.assert_called_once_with(7, 11)
    sid, rid, file_tuple = client.Attachments.attach_file_to_row.call_args.args
    assert sid == 7 and rid == 100
    assert file_tuple[0] == "doc.pdf" and file_tuple[2] == "application/pdf"


def test_attach_pdf_to_row_no_replace_skips_listing(mocker):
    client = _install_client(mocker)
    client.Attachments.attach_file_to_row.return_value = SimpleNamespace(
        result=SimpleNamespace(id=5)
    )
    smartsheet_client.attach_pdf_to_row(7, 100, "x.pdf", b"data", replace=False)
    client.Attachments.list_row_attachments.assert_not_called()
    client.Attachments.delete_attachment.assert_not_called()


def test_attach_pdf_to_row_content_type_passthrough(mocker):
    """The Feature-B MIME fix: a non-PDF caller passes content_type and the tuple
    carries it (default unchanged — the two tests above pin application/pdf)."""
    client = _install_client(mocker)
    client.Attachments.attach_file_to_row.return_value = SimpleNamespace(
        result=SimpleNamespace(id=6)
    )
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    smartsheet_client.attach_pdf_to_row(
        7, 100, "package.docx", b"PK\x03\x04", replace=False, content_type=docx
    )
    _, _, file_tuple = client.Attachments.attach_file_to_row.call_args.args
    assert file_tuple[0] == "package.docx" and file_tuple[2] == docx


def test_attach_pdf_to_row_translates_sdk_error(mocker):
    client = _install_client(mocker)
    client.Attachments.list_row_attachments.return_value = SimpleNamespace(data=[])
    client.Attachments.attach_file_to_row.side_effect = _api_error(403, message="denied")
    with pytest.raises(SmartsheetPermissionError):
        smartsheet_client.attach_pdf_to_row(7, 100, "x.pdf", b"d")


def test_create_sheet_in_folder_returns_new_sheet_id(mocker):
    client = _install_client(mocker)
    created = SimpleNamespace(result=SimpleNamespace(id=555))
    client.Folders.create_sheet_in_folder.return_value = created

    columns = [
        {"title": "mapping_id", "type": "TEXT_NUMBER", "primary": True},
        {"title": "enabled", "type": "CHECKBOX"},
    ]
    sheet_id = smartsheet_client.create_sheet_in_folder(7, "Picklist_Sync_Config", columns)

    assert sheet_id == 555
    client.Folders.create_sheet_in_folder.assert_called_once()
    args, _ = client.Folders.create_sheet_in_folder.call_args
    assert args[0] == 7
    sheet_model = args[1]
    assert sheet_model.name == "Picklist_Sync_Config"
    titles = [c.title for c in sheet_model.columns]
    assert titles == ["mapping_id", "enabled"]


def test_create_sheet_in_folder_translates_api_error(mocker):
    client = _install_client(mocker)
    client.Folders.create_sheet_in_folder.side_effect = _api_error(
        429, message="rate limited"
    )

    with pytest.raises(SmartsheetRateLimitError):
        smartsheet_client.create_sheet_in_folder(7, "Whatever", [])


# ---- find_folder_by_name_in_folder ---------------------------------------


def _rest_get_folder_response_with_folders(
    folders: list[dict] | None, status: int = 200
):
    """Build a mock requests.Response with a `folders` field.

    Mirror of `_rest_get_folder_response` for the find-folder helper. The
    underlying REST endpoint is the same (GET /folders/{id}); the helper
    reads `body["folders"]` instead of `body["sheets"]`.
    """
    response = MagicMock()
    response.status_code = status
    body: dict = {"id": 7, "name": "fake-folder"}
    if folders is not None:
        body["folders"] = folders
    response.json.return_value = body
    if status >= 400:
        from requests import HTTPError
        err = HTTPError(f"HTTP {status}")
        err.response = response
        response.raise_for_status.side_effect = err
        response.text = body.get("message", "error")
    else:
        response.raise_for_status.return_value = None
    return response


def test_find_folder_by_name_in_folder_matches_title(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_get_folder_response_with_folders([
            {"id": 100, "name": "Week of 2026-02-16"},
            {"id": 200, "name": "Week of 2026-02-23"},
            {"id": 300, "name": "Week of 2026-03-09"},
        ]),
    )

    result = smartsheet_client.find_folder_by_name_in_folder(
        7, "Week of 2026-02-23"
    )
    assert result == 200


def test_find_folder_by_name_in_folder_returns_none_when_absent(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_get_folder_response_with_folders([
            {"id": 100, "name": "Week of 2026-02-16"},
        ]),
    )

    assert (
        smartsheet_client.find_folder_by_name_in_folder(7, "Week of 9999-99-99")
        is None
    )


def test_find_folder_by_name_in_folder_translates_rate_limit(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.get",
        return_value=_rest_get_folder_response_with_folders(None, status=429),
    )

    with pytest.raises(SmartsheetRateLimitError):
        smartsheet_client.find_folder_by_name_in_folder(7, "Anything")


# ---- create_folder_in_folder + create_sheet_in_folder_from_template -----


def _rest_post_response(result_id: int, status: int = 200):
    """Build a mock requests.Response wrapping `{"result": {"id": result_id}}`."""
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"result": {"id": result_id}}
    if status >= 400:
        from requests import HTTPError
        err = HTTPError(f"HTTP {status}")
        err.response = response
        response.raise_for_status.side_effect = err
        response.text = "error"
    else:
        response.raise_for_status.return_value = None
    return response


def test_create_folder_in_folder_returns_new_folder_id(mocker):
    post = mocker.patch(
        "shared.smartsheet_client.requests.post",
        return_value=_rest_post_response(999),
    )

    folder_id = smartsheet_client.create_folder_in_folder(7, "Week of 2026-05-18")

    assert folder_id == 999
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "https://api.smartsheet.com/2.0/folders/7/folders"
    assert kwargs["json"] == {"name": "Week of 2026-05-18"}


def test_create_folder_in_folder_translates_permission_error(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.post",
        return_value=_rest_post_response(0, status=403),
    )

    with pytest.raises(SmartsheetPermissionError):
        smartsheet_client.create_folder_in_folder(7, "Anything")


def test_create_sheet_in_folder_from_template_returns_new_sheet_id(mocker):
    post = mocker.patch(
        "shared.smartsheet_client.requests.post",
        return_value=_rest_post_response(1234),
    )

    sheet_id = smartsheet_client.create_sheet_in_folder_from_template(
        folder_id=42,
        name="Daily Reports — Week of 2026-05-18",
        template_sheet_id=7282977254887300,
    )

    assert sheet_id == 1234
    args, kwargs = post.call_args
    # Default (include=None) -> structure-only clone, no ?include= query string.
    assert args[0] == (
        "https://api.smartsheet.com/2.0/sheets/7282977254887300/copy"
    )
    assert kwargs["json"] == {
        "destinationType": "folder",
        "destinationId": 42,
        "newName": "Daily Reports — Week of 2026-05-18",
    }


def test_create_sheet_in_folder_from_template_passes_include_csv(mocker):
    post = mocker.patch(
        "shared.smartsheet_client.requests.post",
        return_value=_rest_post_response(1234),
    )

    smartsheet_client.create_sheet_in_folder_from_template(
        folder_id=42,
        name="copy",
        template_sheet_id=99,
        include=["data", "attachments"],
    )

    args, _ = post.call_args
    assert args[0].endswith("?include=data,attachments")


def test_create_sheet_in_folder_from_template_translates_not_found(mocker):
    mocker.patch(
        "shared.smartsheet_client.requests.post",
        return_value=_rest_post_response(0, status=404),
    )

    with pytest.raises(SmartsheetNotFoundError):
        smartsheet_client.create_sheet_in_folder_from_template(
            folder_id=42,
            name="copy",
            template_sheet_id=12345,
        )


# ---- _translate_smartsheet_error (contract lock) ------------------------
#
# The 4 REST helpers above all dispatch through the private
# `_translate_smartsheet_error` helper. Their per-helper tests above
# exercise it indirectly. These tests lock its contract directly so a
# future caller landing without parallel per-helper coverage still has a
# documented behavioral pin for the dispatch + context-prefix logic.


def test_translate_smartsheet_error_passes_through_2xx(mocker):
    """A 2xx response is a no-op — no exception, returns None."""
    response = _rest_get_folder_response([], status=200)

    result = smartsheet_client._translate_smartsheet_error(
        response, context="anything goes here", idempotent=True
    )
    assert result is None


def test_translate_smartsheet_error_raises_with_context_on_4xx(mocker):
    """A 400 response raises the typed SmartsheetValidationError (PERMANENT,
    shouldRetry=false); message carries context + status code + body excerpt so
    operator triage doesn't need a stack."""
    response = _rest_get_folder_response(None, status=400)
    response.text = "errorCode 1008: Unknown attribute 'destination'"

    with pytest.raises(SmartsheetValidationError) as exc_info:
        smartsheet_client._translate_smartsheet_error(
            response, context="copying sheet 99 into folder 42 as 'X'", idempotent=False
        )

    msg = str(exc_info.value)
    assert "copying sheet 99 into folder 42" in msg  # context prefix
    assert "HTTP 400" in msg  # status code
    assert "errorCode 1008" in msg  # body excerpt


def test_translate_smartsheet_error_raises_on_5xx_for_an_idempotent_lookup(mocker):
    """A 5xx on a READ is the SELF-HEALING class → SmartsheetTransientError (still a
    SmartsheetError, so every existing consumer and the breaker are unchanged)."""
    response = _rest_get_folder_response(None, status=500)
    response.text = "Internal Server Error"

    with pytest.raises(SmartsheetError) as exc_info:
        smartsheet_client._translate_smartsheet_error(
            response, context="finding folder 'X' in folder 7", idempotent=True
        )

    assert type(exc_info.value) is smartsheet_client.SmartsheetTransientError
    assert "HTTP 500" in str(exc_info.value)


def test_a_5xx_on_a_create_is_never_classified_retry_safe(mocker):
    """`is_transient_error()` is a PUBLIC predicate meaning "safe to re-issue the SAME
    call". A 5xx on a create carries NO information about whether the folder/sheet
    committed, and there is no idempotency key to settle it — so a future fence/retry
    consumer trusting the predicate would DUPLICATE a created folder. Nothing retries
    creates today; the classification is narrowed at the raise site so it cannot start."""
    response = _rest_get_folder_response(None, status=503)
    response.text = "Service Unavailable"

    with pytest.raises(SmartsheetError) as exc_info:
        smartsheet_client._translate_smartsheet_error(
            response, context="creating folder 'X' in parent 7", idempotent=False
        )

    assert type(exc_info.value) is SmartsheetError  # NOT the transient subclass
    assert smartsheet_client.is_transient_error(exc_info.value) is False
    assert "NON-IDEMPOTENT" in str(exc_info.value)


@pytest.mark.parametrize(
    "helper, kwargs",
    [
        ("create_folder_in_folder", {"parent_folder_id": 1, "name": "X"}),
        ("create_folder_in_workspace", {"workspace_id": 1, "name": "X"}),
    ],
)
def test_the_real_create_helpers_do_not_raise_a_retry_safe_error(mocker, helper, kwargs):
    """End-to-end through the REAL helper, not just the translator: a 5xx from a live
    create must not reach a caller wearing the retry-safe type."""
    mocker.patch.object(smartsheet_client, "get_client")
    mocker.patch.object(smartsheet_client.keychain, "get_secret", return_value="tok")
    response = _rest_get_folder_response(None, status=500)
    response.text = "Internal Server Error"
    mocker.patch.object(smartsheet_client.requests, "post", return_value=response)

    with pytest.raises(SmartsheetError) as exc_info:
        getattr(smartsheet_client, helper)(**kwargs)

    assert smartsheet_client.is_transient_error(exc_info.value) is False


@pytest.mark.parametrize(
    "helper, kwargs",
    [
        ("create_folder_in_folder", {"parent_folder_id": 1, "name": "X"}),
        ("create_folder_in_workspace", {"workspace_id": 1, "name": "X"}),
        (
            "create_sheet_in_folder_from_template",
            {"folder_id": 1, "name": "X", "template_sheet_id": 2},
        ),
    ],
)
def test_a_timeout_on_a_rest_create_is_never_classified_retry_safe(mocker, helper, kwargs):
    """The TIMEOUT half of the same defect, which round 3 left behind on these three POSTs.

    A `requests.ReadTimeout` on a create is strictly WORSE than a 5xx: the request was
    fully transmitted and only the response was lost, so the folder/sheet may well exist.
    These arms minted `SmartsheetTransientError` while the 5xx arm one line below already
    refused to."""
    mocker.patch.object(smartsheet_client, "get_client")
    mocker.patch.object(
        smartsheet_client.requests, "post",
        side_effect=requests.exceptions.ReadTimeout("timed out"),
    )

    with pytest.raises(SmartsheetError) as exc_info:
        getattr(smartsheet_client, helper)(**kwargs)

    assert smartsheet_client.is_transient_error(exc_info.value) is False
    assert "NON-IDEMPOTENT" in str(exc_info.value)


# ---- the SDK path: a WRITE is never retry-safe either (round-4 finding) ---
#
# Round 3 threaded `idempotent` through `_translate_smartsheet_error`, fixing the four
# raw-REST helpers — but `_translate` (the SDK path) still classified by STATUS ALONE, so
# an `add_rows` that 500s or times out yielded `SmartsheetTransientError` and the PUBLIC
# `is_transient_error()` answered True for it, flatly contradicting its own contract. It is
# load-bearing, not cosmetic: `sustained_failure.TransientFence` consults that predicate, so
# a failed WRITE at a fenced pass boundary was softened to a counted ERROR ("retrying next
# cycle") as though it were a safe-to-reissue read.

#: (SDK method path, a call to the REAL helper that reaches it). Every ITS Smartsheet WRITE
#: that goes through the SDK rather than raw REST.
_SDK_WRITE_HELPERS = [
    ("Sheets.add_rows", lambda: smartsheet_client.add_rows(999, [{"A": 1}])),
    ("Sheets.update_rows",
     lambda: smartsheet_client.update_rows(999, [{"_row_id": 1, "A": 2}])),
    ("Sheets.delete_rows", lambda: smartsheet_client.delete_rows(999, [1])),
    ("Sheets.update_column",
     lambda: smartsheet_client.update_column_options(
         999, 1, ["a"], column_type="PICKLIST")),
    ("Sheets.add_columns",
     lambda: smartsheet_client.create_picklist_column(999, "T", ["a"], index=0)),
    ("Sheets.delete_sheet", lambda: smartsheet_client.delete_sheet(999)),
    ("Sheets.move_sheet", lambda: smartsheet_client.move_sheet_to_folder(999, 7)),
    ("Folders.create_sheet_in_folder",
     lambda: smartsheet_client.create_sheet_in_folder(
         7, "X", [{"title": "p", "type": "TEXT_NUMBER", "primary": True}])),
    ("Attachments.attach_file_to_row",
     lambda: smartsheet_client.attach_pdf_to_row(
         999, 1, "f.pdf", b"%PDF-1.4", replace=False)),
]

#: The two SDK failure shapes that used to be softened. A timeout is the worse of the two
#: for a write: the request was transmitted and only the RESPONSE was lost.
_SDK_UNKNOWN_OUTCOME_FAILURES = [
    ("5xx", lambda: _api_error(500, code=4000, message="Internal Server Error")),
    ("timeout", lambda: sdk_exc.UnexpectedRequestError(
        requests.exceptions.ReadTimeout("timed out"), None)),
]


def _arm(client, dotted: str, exc) -> None:
    target = client
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    getattr(target, parts[-1]).side_effect = exc


@pytest.mark.parametrize("shape, make_exc", _SDK_UNKNOWN_OUTCOME_FAILURES,
                         ids=[s for s, _ in _SDK_UNKNOWN_OUTCOME_FAILURES])
@pytest.mark.parametrize("sdk_path, call", _SDK_WRITE_HELPERS,
                         ids=[p for p, _ in _SDK_WRITE_HELPERS])
def test_no_sdk_write_helper_is_ever_classified_retry_safe(
    mocker, sdk_path, call, shape, make_exc
):
    """Through the REAL helper, not just the translator: `is_transient_error()` must answer
    False for a 5xx and for a timeout on every SDK write, and the message must say the
    outcome is UNKNOWN rather than implying a harmless blip."""
    client = _install_client(mocker)
    # Column resolution is a READ that would otherwise need its own sheet fixture; the
    # write itself is what is under test.
    mocker.patch.object(smartsheet_client, "_resolve_cells", return_value=[])
    _arm(client, sdk_path, make_exc())

    with pytest.raises(SmartsheetError) as exc_info:
        call()

    assert not isinstance(exc_info.value, SmartsheetTransientError)
    assert smartsheet_client.is_transient_error(exc_info.value) is False, (
        f"a {shape} on {sdk_path} was reported retry-safe — the fence would soften a "
        "possibly-COMMITTED write into 'retrying next cycle'"
    )
    assert "UNKNOWN whether the write COMMITTED" in str(exc_info.value)


@pytest.mark.parametrize("shape, make_exc", _SDK_UNKNOWN_OUTCOME_FAILURES,
                         ids=[s for s, _ in _SDK_UNKNOWN_OUTCOME_FAILURES])
def test_sdk_reads_are_still_classified_retry_safe(mocker, shape, make_exc):
    """The other direction — narrowing the writes must not blunt the read path the whole
    branch exists to soften (`get_setting`/`get_rows` at a daemon's pass boundary)."""
    client = _install_client(mocker)
    client.Sheets.get_sheet.side_effect = make_exc()

    with pytest.raises(SmartsheetTransientError) as exc_info:
        smartsheet_client.get_rows(123)

    assert smartsheet_client.is_transient_error(exc_info.value) is True
    assert "UNKNOWN whether the write COMMITTED" not in str(exc_info.value)


def test_the_fence_cannot_soften_a_write_failure(mocker, tmp_path):
    """The consequence, asserted against the REAL fence rather than by inspection: this
    predicate is not an academic contract, it is what decides whether a failed write gets
    its immediate `uncaught_exception` CRITICAL or is quietly counted as a blip."""
    from shared import sustained_failure

    client = _install_client(mocker)
    mocker.patch.object(smartsheet_client, "_resolve_cells", return_value=[])
    logged: list = []
    mocker.patch("shared.error_log.log", side_effect=lambda *a, **kw: logged.append(a))
    fence = sustained_failure.TransientFence(
        "test_daemon",
        state_path=tmp_path / "fence.json",
        transient_error_code="test_daemon.read_transient",
        sustained_error_code="test_daemon.read_sustained",
    )

    client.Sheets.add_rows.side_effect = sdk_exc.UnexpectedRequestError(
        requests.exceptions.ReadTimeout("timed out"), None
    )
    with pytest.raises(SmartsheetError) as write_exc:
        smartsheet_client.add_rows(999, [{"A": 1}])
    assert fence.handle(write_exc.value) is False, (
        "the fence must RE-RAISE a failed write (→ CRITICAL), never halt-and-count it"
    )
    assert logged == []
    assert not (tmp_path / "fence.json").exists()

    # …while the read it was built for is still softened, exactly as before.
    client.Sheets.get_sheet.side_effect = sdk_exc.UnexpectedRequestError(
        requests.exceptions.ReadTimeout("timed out"), None
    )
    with pytest.raises(SmartsheetTransientError) as read_exc:
        smartsheet_client.get_rows(123)
    assert fence.handle(read_exc.value) is True


#: Every function in `shared/smartsheet_client.py` that translates an SDK exception, and
#: the `idempotent=` literals it passes. Held as DATA so the AST walk below can assert SET
#: EQUALITY: adding an SDK helper — or flipping one of these to True — forces a deliberate
#: edit here, which is the review moment where "is this a read?" actually gets asked. mypy
#: already forces the keyword to be PRESENT (it is required and keyword-only); this guards
#: the value, which is the half a type checker cannot see.
_TRANSLATE_IDEMPOTENCY = {
    # reads / lookups — safe to re-issue, so the self-healing type is correct
    "_fetch_column_map": {True},
    "get_sheet": {True},
    "get_row": {True},
    "get_rows": {True},
    "get_cell_history": {True},
    "find_row_by_primary": {True},
    "list_columns_with_options": {True},
    # writes — a 5xx/timeout leaves the outcome UNKNOWN, so never the retry-safe type
    "add_rows": {False},
    "update_rows": {False},
    "delete_rows": {False},
    "update_row_cells_by_id": {False},
    "add_row_by_id": {False},
    "update_column_options": {False},
    "create_picklist_column": {False},
    "create_sheet_in_folder": {False},
    "delete_sheet": {False},
    "move_sheet_to_folder": {False},
    # Track 6 folder relocation — all three are WRITES. A folder move is not idempotent in
    # the retry sense: re-issuing after an ambiguous 5xx could act on a folder that has
    # already re-parented, so `is_transient_error()` must never call these self-healing.
    "move_folder_to_folder": {False},
    "move_folder_to_workspace": {False},
    "rename_folder": {False},
    "attach_pdf_to_row": {False},
    # reads the live column list (True), then UPDATEs each column (False) — the one helper
    # that legitimately does both, which is why the table holds a SET per function.
    "apply_column_styles": {True, False},
}


def test_every_sdk_translation_site_declares_its_idempotency_deliberately():
    """PROVE-IT-BITES anchor for the round-4 fix: a new SDK helper that copy-pastes
    `idempotent=True` onto a WRITE RED-lights here rather than silently re-teaching
    `is_transient_error()` to lie about a possibly-committed mutation."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(smartsheet_client))
    found: dict[str, set[bool]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name != "_translate":
                continue
            literals = {
                kw.value.value for kw in node.keywords
                if kw.arg == "idempotent" and isinstance(kw.value, ast.Constant)
            }
            assert literals, (
                f"{fn.name}: `_translate` must be called with a LITERAL `idempotent=` — a "
                "computed value hides the read/write decision from review"
            )
            found.setdefault(fn.name, set()).update(literals)

    assert found == _TRANSLATE_IDEMPOTENCY, (
        "the SDK translation sites drifted from the declared read/write classification. "
        f"missing={set(_TRANSLATE_IDEMPOTENCY) - set(found)} "
        f"unexpected={set(found) - set(_TRANSLATE_IDEMPOTENCY)} "
        f"changed={ {k: (v, _TRANSLATE_IDEMPOTENCY.get(k)) for k, v in found.items()
                     if _TRANSLATE_IDEMPOTENCY.get(k) != v} }"
    )


# ---- find_row_by_primary + update_row_cells_by_id (PR #59.5) -------------


def test_find_row_by_primary_returns_matching_row_dict(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[
            _column(10, "Daemon Name"),
            _column(20, "Workstream"),
            _column(30, "Last Heartbeat"),
        ],
        rows=[
            _row(101, [(10, "other"), (20, "other_ws")]),
            _row(102, [(10, "safety_reports.portal_poll"), (20, "safety_reports"),
                       (30, "2026-05-21T19:00:00Z")]),
        ],
    )

    row = smartsheet_client.find_row_by_primary(
        sheet_id=697287746473860,
        primary_column_id=10,
        value="safety_reports.portal_poll",
    )
    assert row is not None
    assert row["_row_id"] == 102
    assert row["Daemon Name"] == "safety_reports.portal_poll"
    assert row["Workstream"] == "safety_reports"
    assert row["Last Heartbeat"] == "2026-05-21T19:00:00Z"


def test_find_row_by_primary_returns_none_on_miss(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.return_value = SimpleNamespace(
        columns=[_column(10, "Daemon Name")],
        rows=[_row(101, [(10, "other")])],
    )
    assert (
        smartsheet_client.find_row_by_primary(99, 10, "safety_reports.portal_poll")
        is None
    )


def test_find_row_by_primary_translates_sdk_error(mocker):
    client = _install_client(mocker)
    client.Sheets.get_sheet.side_effect = _api_error(404, message="missing sheet")
    with pytest.raises(SmartsheetNotFoundError):
        smartsheet_client.find_row_by_primary(99, 10, "x")


def test_update_row_cells_by_id_builds_cell_payload(mocker):
    client = _install_client(mocker)
    # update_row_cells_by_id does NOT use the title cache, so no get_sheet call.
    client.Sheets.update_rows.return_value = SimpleNamespace(result=[])

    smartsheet_client.update_row_cells_by_id(
        sheet_id=697287746473860,
        row_id=7461022174478212,
        cells_by_column_id={
            2052777316749188: "2026-05-21T19:00:00Z",  # last_heartbeat
            6556376944119684: "OK",                     # last_cycle_status
            8808176757804932: 1247,                      # total_cycles
        },
    )

    # The SDK Sheets.update_rows call took exactly one Row with three cells.
    assert client.Sheets.update_rows.call_count == 1
    sheet_id_arg, rows_arg = client.Sheets.update_rows.call_args.args
    assert sheet_id_arg == 697287746473860
    [row] = rows_arg
    assert row.id == 7461022174478212
    by_col = {c.column_id: c.value for c in row.cells}
    assert by_col == {
        2052777316749188: "2026-05-21T19:00:00Z",
        6556376944119684: "OK",
        8808176757804932: 1247,
    }


def test_update_row_cells_by_id_no_op_on_empty_payload(mocker):
    client = _install_client(mocker)
    smartsheet_client.update_row_cells_by_id(
        sheet_id=1, row_id=2, cells_by_column_id={}
    )
    client.Sheets.update_rows.assert_not_called()


def test_update_row_cells_by_id_translates_404(mocker):
    client = _install_client(mocker)
    client.Sheets.update_rows.side_effect = _api_error(404, message="row gone")
    with pytest.raises(SmartsheetNotFoundError):
        smartsheet_client.update_row_cells_by_id(1, 2, {3: "x"})


# ---- add_row_by_id (A1 self-provision create primitive) -----------------


def test_add_row_by_id_builds_cell_payload_and_returns_new_id(mocker):
    client = _install_client(mocker)
    # add_row_by_id does NOT use the title cache, so no get_sheet call.
    client.Sheets.add_rows.return_value = SimpleNamespace(
        result=[SimpleNamespace(id=7788)]
    )

    new_id = smartsheet_client.add_row_by_id(
        sheet_id=697287746473860,
        cells_by_column_id={
            8245226804383620: "safety_reports.weekly_send_poll",  # daemon_name
            926877409906564: "safety_reports",                   # workstream
            5430477037277060: True,                               # enabled
        },
    )

    assert new_id == 7788
    assert client.Sheets.add_rows.call_count == 1
    sheet_id_arg, rows_arg = client.Sheets.add_rows.call_args.args
    assert sheet_id_arg == 697287746473860
    [row] = rows_arg
    assert row.to_bottom is True
    by_col = {c.column_id: c.value for c in row.cells}
    assert by_col == {
        8245226804383620: "safety_reports.weekly_send_poll",
        926877409906564: "safety_reports",
        5430477037277060: True,
    }


def test_add_row_by_id_translates_sdk_error(mocker):
    client = _install_client(mocker)
    client.Sheets.add_rows.side_effect = _api_error(500, message="boom")
    with pytest.raises(SmartsheetError):
        smartsheet_client.add_row_by_id(1, {10: "x"})


# ---- delete_sheet + verify_write_capability (B2 write probe) --------------


def test_delete_sheet_calls_sdk(mocker):
    client = _install_client(mocker)
    smartsheet_client.delete_sheet(7788)
    client.Sheets.delete_sheet.assert_called_once_with(7788)


def test_delete_sheet_translates_error(mocker):
    client = _install_client(mocker)
    client.Sheets.delete_sheet.side_effect = _api_error(403, message="nope")
    with pytest.raises(SmartsheetPermissionError):
        smartsheet_client.delete_sheet(1)


# ---- move_sheet_to_folder (§51 archive-on-closure relocation) -------------


def test_move_sheet_to_folder_calls_sdk_with_container_destination(mocker):
    # MOVE (never delete): the SDK is called with a ContainerDestination naming the
    # target FOLDER by id — the archive-on-closure relocation contract.
    client = _install_client(mocker)
    smartsheet_client.move_sheet_to_folder(7788, 9999)
    client.Sheets.move_sheet.assert_called_once()
    args, _ = client.Sheets.move_sheet.call_args
    assert args[0] == 7788
    dest = args[1]
    assert isinstance(dest, smartsheet.models.ContainerDestination)
    assert dest.destination_type == "folder"
    assert dest.destination_id == 9999
    # a MOVE must never route through delete
    client.Sheets.delete_sheet.assert_not_called()


def test_move_sheet_to_folder_translates_error(mocker):
    client = _install_client(mocker)
    client.Sheets.move_sheet.side_effect = _api_error(404, message="gone")
    with pytest.raises(SmartsheetNotFoundError):
        smartsheet_client.move_sheet_to_folder(1, 2)


# ---- Track 6 folder relocation (move / rename / probes) ----------------------


def test_move_folder_to_folder_calls_sdk_with_folder_destination(mocker):
    client = _install_client(mocker)
    smartsheet_client.move_folder_to_folder(111, 222)
    client.Folders.move_folder.assert_called_once()
    args, _ = client.Folders.move_folder.call_args
    assert args[0] == 111
    dest = args[1]
    assert isinstance(dest, smartsheet.models.ContainerDestination)
    assert dest.destination_type == "folder"
    assert dest.destination_id == 222
    # A MOVE relocates. It must never route through a delete, and must not rename
    # (the API ignores new_name on /move — see the live footgun test).
    assert dest.new_name is None
    client.Folders.delete_folder.assert_not_called()


def test_move_folder_to_workspace_uses_the_workspace_destination_type(mocker):
    # The restore leg: a per-job Safety/Progress folder's source parent is a WORKSPACE,
    # not a folder, so un-archiving needs this variant.
    client = _install_client(mocker)
    smartsheet_client.move_folder_to_workspace(111, 333)
    args, _ = client.Folders.move_folder.call_args
    assert args[1].destination_type == "workspace"
    assert args[1].destination_id == 333


def test_rename_folder_sends_only_the_name(mocker):
    client = _install_client(mocker)
    smartsheet_client.rename_folder(111, "Safety")
    client.Folders.update_folder.assert_called_once()
    args, _ = client.Folders.update_folder.call_args
    assert args[0] == 111
    assert isinstance(args[1], smartsheet.models.Folder)
    assert args[1].name == "Safety"


@pytest.mark.parametrize(
    "fn, call",
    [
        ("move_folder_to_folder", lambda: smartsheet_client.move_folder_to_folder(1, 2)),
        ("move_folder_to_workspace", lambda: smartsheet_client.move_folder_to_workspace(1, 2)),
        ("rename_folder", lambda: smartsheet_client.rename_folder(1, "x")),
    ],
)
def test_folder_writes_translate_errors(mocker, fn, call):
    client = _install_client(mocker)
    client.Folders.move_folder.side_effect = _api_error(404, message="gone")
    client.Folders.update_folder.side_effect = _api_error(404, message="gone")
    with pytest.raises(SmartsheetNotFoundError):
        call()


def test_folder_writes_are_not_retry_enrolled():
    # A failed folder WRITE must never be re-issued in-process: the archive's correct retry
    # is durable and cross-cycle (re-drained from its queue with the ledger intact).
    for name in ("move_folder_to_folder", "move_folder_to_workspace", "rename_folder"):
        assert name not in smartsheet_client._TRANSIENT_RETRY_ENROLLED


def test_get_folder_name_reads_the_name_field(mocker):
    mocker.patch.object(smartsheet_client.keychain, "get_secret", return_value="tok")
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"id": 111, "name": "Bradley 1"}
    mocker.patch.object(smartsheet_client.requests, "get", return_value=resp)
    assert smartsheet_client.get_folder_name(111) == "Bradley 1"


def test_get_workspace_access_level_reads_accesslevel(mocker):
    mocker.patch.object(smartsheet_client.keychain, "get_secret", return_value="tok")
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"id": 9, "accessLevel": "ADMIN"}
    mocker.patch.object(smartsheet_client.requests, "get", return_value=resp)
    assert smartsheet_client.get_workspace_access_level(9) == "ADMIN"


def test_get_workspace_access_level_missing_field_fails_closed(mocker):
    # '' is not ADMIN, so a caller comparing against the allowed set refuses the move.
    # Guessing a level here would be the fail-OPEN mistake.
    mocker.patch.object(smartsheet_client.keychain, "get_secret", return_value="tok")
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"id": 9}
    mocker.patch.object(smartsheet_client.requests, "get", return_value=resp)
    assert smartsheet_client.get_workspace_access_level(9) == ""


# ---- delete_sheet_settling (probe create→delete eventual-consistency retry) ---


def test_delete_sheet_settling_retries_on_not_found_then_succeeds(mocker):
    sleep = mocker.patch("shared.smartsheet_client.time.sleep")
    delete = mocker.patch(
        "shared.smartsheet_client.delete_sheet",
        side_effect=[
            SmartsheetNotFoundError("HTTP 404 (code 1006): Not Found"),
            SmartsheetNotFoundError("HTTP 404 (code 5036): not yet propagated"),
            None,
        ],
    )
    smartsheet_client.delete_sheet_settling(123)
    assert delete.call_count == 3
    assert sleep.call_count == 2  # backoff between the 3 attempts


def test_delete_sheet_settling_reraises_after_exhaustion(mocker):
    mocker.patch("shared.smartsheet_client.time.sleep")
    mocker.patch(
        "shared.smartsheet_client.delete_sheet",
        side_effect=SmartsheetNotFoundError("HTTP 404 (code 1006): Not Found"),
    )
    with pytest.raises(SmartsheetNotFoundError):
        smartsheet_client.delete_sheet_settling(123, attempts=2)


def test_delete_sheet_settling_retries_5036_with_non_404_status(mocker):
    mocker.patch("shared.smartsheet_client.time.sleep")
    delete = mocker.patch(
        "shared.smartsheet_client.delete_sheet",
        side_effect=[
            SmartsheetError("HTTP 500 (code 5036): not yet propagated"),
            None,
        ],
    )
    smartsheet_client.delete_sheet_settling(123)
    assert delete.call_count == 2


def test_delete_sheet_settling_fails_fast_on_other_error(mocker):
    # A genuine non-eventual-consistency error is NOT retried — fail fast so a
    # real missing-sheet/permission problem surfaces immediately.
    delete = mocker.patch(
        "shared.smartsheet_client.delete_sheet",
        side_effect=SmartsheetPermissionError("HTTP 403 (code 1004): forbidden"),
    )
    with pytest.raises(SmartsheetPermissionError):
        smartsheet_client.delete_sheet_settling(123)
    assert delete.call_count == 1


def test_delete_sheet_settling_circuit_open_fails_fast(mocker):
    # CircuitOpenError is a SmartsheetError but NOT a not-found → fail fast.
    delete = mocker.patch(
        "shared.smartsheet_client.delete_sheet",
        side_effect=SmartsheetCircuitOpenError("breaker open"),
    )
    with pytest.raises(SmartsheetCircuitOpenError):
        smartsheet_client.delete_sheet_settling(123)
    assert delete.call_count == 1


def test_verify_write_capability_returns_new_sheet_id(mocker):
    create = mocker.patch(
        "shared.smartsheet_client.create_sheet_in_folder", return_value=9911
    )
    sid = smartsheet_client.verify_write_capability(folder_id=42)
    assert sid == 9911
    folder_arg, name_arg, cols_arg = create.call_args.args
    assert folder_arg == 42
    assert name_arg.startswith("_its_write_probe_")
    assert len(name_arg) <= 50  # Smartsheet sheet-name limit (errorCode 1041)
    assert cols_arg[0]["primary"] is True


def test_verify_write_capability_defaults_to_system_config_folder(mocker):
    create = mocker.patch(
        "shared.smartsheet_client.create_sheet_in_folder", return_value=1
    )
    smartsheet_client.verify_write_capability()
    assert create.call_args.args[0] == sheet_ids.FOLDER_SYSTEM_CONFIG


@pytest.mark.parametrize("err", [SmartsheetAuthError, SmartsheetPermissionError])
def test_verify_write_capability_wraps_auth_permission(mocker, err):
    # A read-only / mis-scoped token's CREATE 401/403 → the typed verdict.
    mocker.patch(
        "shared.smartsheet_client.create_sheet_in_folder",
        side_effect=err("read-only token"),
    )
    with pytest.raises(SmartsheetWriteCapabilityError, match="cannot create"):
        smartsheet_client.verify_write_capability()


def test_verify_write_capability_propagates_circuit_open(mocker):
    # A Smartsheet OUTAGE is not a token verdict — CircuitOpenError must
    # propagate UNWRAPPED so the caller treats it as inconclusive.
    mocker.patch(
        "shared.smartsheet_client.create_sheet_in_folder",
        side_effect=SmartsheetCircuitOpenError("breaker open"),
    )
    with pytest.raises(SmartsheetCircuitOpenError):
        smartsheet_client.verify_write_capability()


def test_resolve_cells_rejects_empty_multipicklist(mocker):
    """An empty collection is an UNWRITABLE MULTI_PICKLIST payload: the API answers
    errorCode 1012 "Required object attribute(s) are missing from your request:
    multiPickList.values[]". Emitting it produced an opaque HTTP 400 at the daemon —
    raise a self-explaining ValueError at construction instead, naming the column and
    the fix (omit the key to leave the cell untouched). Callers that mean "unchanged"
    drop the key (po_materials.vendors / subcontracts.subcontractors do)."""
    mocker.patch(
        "shared.smartsheet_client._column_map",
        return_value={"Supply Categories": 101, "Vendor Name": 102},
    )
    with pytest.raises(ValueError, match="Supply Categories"):
        smartsheet_client._resolve_cells(1, {"Supply Categories": []})

    # A NON-empty collection still builds a MULTI_PICKLIST objectValue.
    [cell] = smartsheet_client._resolve_cells(1, {"Supply Categories": ["modules"]})
    assert list(cell.object_value.values) == ["modules"]
