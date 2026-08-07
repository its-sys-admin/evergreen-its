"""Tests for shared/box_client.py.

All boxsdk + Keychain interactions are mocked — these tests never hit the
network and never read the real Keychain. The module-level client cache
is reset between tests via the autouse `reset_box_state` fixture.

The CRITICAL test in this file is `test_store_tokens_persists_refresh_token`:
the refresh-token rotation invariant is the single most likely source of
silent breakage in production. If `_store_tokens` ever stops writing to
Keychain, ITS dies in 60 days. That test must stay green.

Run with: pytest -q tests/test_box_client.py
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from boxsdk.exception import (  # type: ignore[import-untyped]
    BoxAPIException,
    BoxOAuthException,
)

from shared import box_client
from shared.box_client import (
    BoxAuthError,
    BoxConflictError,
    BoxError,
    BoxNotFoundError,
    BoxRateLimitError,
    canonical_job_path,
)

# ---- Fixtures + helpers --------------------------------------------------


@pytest.fixture(autouse=True)
def reset_box_state(mocker):
    """Reset the module's client cache and stub Keychain reads for every test.

    A3: also isolate the new refresh-lock + freshness-marker writes from the real
    filesystem — `with_path_lock` and `atomic_write_json` are no-op'd so tests
    never flock or write under ~/its/state, and `error_log.log` is stubbed so the
    fail-open WARN paths never attempt a real ITS_Errors write.
    """
    mocker.patch.object(box_client, "_client", None)
    mocker.patch(
        "shared.box_client.keychain.get_secret",
        side_effect=lambda key, *a, **kw: f"fake-{key}",
    )
    mocker.patch("shared.box_client.keychain.set_secret")
    mocker.patch("shared.box_client.state_io.with_path_lock")
    mocker.patch("shared.box_client.state_io.atomic_write_json")
    mocker.patch("shared.error_log.log")


def _box_api_error(
    status: int,
    *,
    message: str = "boom",
    headers: dict | None = None,
) -> BoxAPIException:
    """Build a BoxAPIException with the shape `_translate` reads."""
    exc = BoxAPIException(
        status=status,
        message=message,
        headers=headers or {},
    )
    return exc


def _install_mocked_sdk(mocker):
    """Patch boxsdk.OAuth2 and boxsdk.Client at the box_client boundary.

    Returns a `(oauth_cls, client_cls, client_instance)` tuple so tests
    can assert on construction arguments, on Client method dispatch, and
    on the OAuth2 store_tokens wiring without ever touching real boxsdk
    internals.
    """
    oauth_cls = mocker.patch("shared.box_client.OAuth2")
    client_cls = mocker.patch("shared.box_client.Client")
    mocker.patch("shared.box_client.AuthorizedSession")  # A2: don't build a real session in unit tests
    instance = MagicMock()
    client_cls.return_value = instance
    return oauth_cls, client_cls, instance


# ---- get_client lazy-singleton + Keychain read ---------------------------


def test_get_client_lazy_init_reads_keychain_and_caches(mocker):
    oauth_cls, client_cls, _ = _install_mocked_sdk(mocker)

    c1 = box_client.get_client()
    c2 = box_client.get_client()

    assert c1 is c2
    assert client_cls.call_count == 1
    assert oauth_cls.call_count == 1


def test_get_client_passes_credentials_from_keychain_to_oauth2(mocker):
    oauth_cls, _, _ = _install_mocked_sdk(mocker)

    box_client.get_client()

    kwargs = oauth_cls.call_args.kwargs
    assert kwargs["client_id"] == "fake-ITS_BOX_CLIENT_ID"
    assert kwargs["client_secret"] == "fake-ITS_BOX_CLIENT_SECRET"
    assert kwargs["refresh_token"] == "fake-ITS_BOX_REFRESH_TOKEN"
    # access_token MUST start None — forces a refresh-token exchange
    # immediately so a stale access_token never gets used.
    assert kwargs["access_token"] is None


def test_get_client_wires_store_tokens_callback(mocker):
    """store_tokens MUST be wired to _store_tokens. Without it, refresh-
    token rotation does not persist and ITS dies within 60 days. This is
    the structural test that the wiring is present."""
    oauth_cls, _, _ = _install_mocked_sdk(mocker)

    box_client.get_client()

    assert oauth_cls.call_args.kwargs["store_tokens"] is box_client._store_tokens


def test_get_client_wires_network_timeout(mocker):
    """A2: every Box API call is bounded — boxsdk has no default network timeout.
    The AuthorizedSession is built with default_network_request_kwargs carrying the
    (connect, read) timeout and passed to Client as the session."""
    mocker.patch("shared.box_client.OAuth2")
    client_cls = mocker.patch("shared.box_client.Client")
    sess_cls = mocker.patch("shared.box_client.AuthorizedSession")

    box_client.get_client()

    assert sess_cls.call_args.kwargs["default_network_request_kwargs"] == {
        "timeout": box_client.BOX_NETWORK_TIMEOUT
    }
    assert client_cls.call_args.kwargs.get("session") is sess_cls.return_value


def test_get_client_keychain_failure_raises_box_auth_error(mocker):
    _install_mocked_sdk(mocker)
    mocker.patch(
        "shared.box_client.keychain.get_secret",
        side_effect=box_client.keychain.KeychainError(
            "Keychain entry not found: service='ITS_BOX_REFRESH_TOKEN'"
        ),
    )

    with pytest.raises(BoxAuthError, match="setup_box_oauth.py"):
        box_client.get_client()


# ---- CRITICAL: store_tokens persists rotated refresh token ---------------


def test_store_tokens_persists_refresh_token():
    """CRITICAL invariant — see module docstring.

    Box rotates the refresh token on every token exchange. The
    store_tokens callback receives (access_token, refresh_token) and
    must write the new refresh_token to Keychain synchronously, so the
    next ITS process invocation reads the rotated value rather than
    the now-invalid old one. If this test fails, ITS will die 60 days
    after merge.
    """
    set_spy = MagicMock()
    box_client.keychain.set_secret = set_spy

    box_client._store_tokens(
        access_token="new-access-token-value",
        refresh_token="new-rotated-refresh-token",
    )

    set_spy.assert_called_once_with(
        "ITS_BOX_REFRESH_TOKEN",
        "new-rotated-refresh-token",
    )


def test_store_tokens_does_not_persist_access_token():
    """Access tokens have a 60-min TTL and are re-fetched on demand inside
    the process; persisting them is pointless and would leak a short-lived
    secret into Keychain history."""
    set_spy = MagicMock()
    box_client.keychain.set_secret = set_spy

    box_client._store_tokens(
        access_token="ephemeral-access-token",
        refresh_token="rt",
    )

    # Exactly one Keychain write — refresh-token only.
    assert set_spy.call_count == 1
    services_written = [c.args[0] for c in set_spy.call_args_list]
    assert "ITS_BOX_REFRESH_TOKEN" in services_written
    # Access token must NOT appear anywhere in Keychain calls.
    for call in set_spy.call_args_list:
        assert "ephemeral-access-token" not in call.args


# ---- A3: refresh-lock + freshness marker ---------------------------------


def test_store_tokens_writes_freshness_marker(mocker):
    """A3: a successful persist stamps the freshness marker (watchdog Check P)."""
    awj = mocker.patch("shared.box_client.state_io.atomic_write_json")
    box_client.keychain.set_secret = MagicMock()

    box_client._store_tokens(access_token="a", refresh_token="r")

    assert awj.called
    path_arg, data_arg = awj.call_args.args
    assert path_arg == box_client.BOX_TOKEN_REFRESH_MARKER
    assert "last_refresh_utc" in data_arg


def test_store_tokens_marker_failure_is_nonfatal(mocker):
    """A3: a marker-write failure MUST NOT break refresh-token persistence —
    the Keychain write is the critical path."""
    mocker.patch(
        "shared.box_client.state_io.atomic_write_json",
        side_effect=OSError("disk full"),
    )
    set_spy = MagicMock()
    box_client.keychain.set_secret = set_spy

    box_client._store_tokens(access_token="a", refresh_token="r")  # must not raise

    set_spy.assert_called_once_with("ITS_BOX_REFRESH_TOKEN", "r")


def test_store_tokens_lock_timeout_fails_open(mocker):
    """A3: a refresh-lock timeout persists the rotated token UNLOCKED (fail-open)
    — an un-persisted token is fatal (60-day death), a lost lock is not."""
    mocker.patch(
        "shared.box_client.state_io.with_path_lock",
        side_effect=box_client.state_io.StateLockTimeoutError("locked"),
    )
    awj = mocker.patch("shared.box_client.state_io.atomic_write_json")
    set_spy = MagicMock()
    box_client.keychain.set_secret = set_spy

    box_client._store_tokens(access_token="a", refresh_token="r")

    set_spy.assert_called_once_with("ITS_BOX_REFRESH_TOKEN", "r")
    assert awj.called  # marker still written on the fail-open path


# ---- Error translation ---------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, BoxAuthError),
        (403, BoxAuthError),
        (404, BoxNotFoundError),
        (409, BoxConflictError),
        (429, BoxRateLimitError),
        (500, BoxError),
        (502, BoxError),
    ],
)
def test_box_api_error_translated_by_status(mocker, status, expected):
    _, _, instance = _install_mocked_sdk(mocker)
    err = _box_api_error(status, message="nope")

    # 429 needs exhausted retries before it surfaces as
    # BoxRateLimitError, so make every attempt fail with the same error.
    instance.file.return_value.content.side_effect = err
    mocker.patch("shared.box_client.time.sleep")

    with pytest.raises(expected, match="nope"):
        box_client.download_file("123")


def test_box_oauth_exception_surfaces_as_box_auth_error(mocker):
    """Auth-layer failures (token exchange itself) MUST surface as
    BoxAuthError regardless of HTTP status — they indicate the refresh
    token is bad and re-running setup_box_oauth.py is the recovery."""
    _, _, instance = _install_mocked_sdk(mocker)

    # BoxOAuthException requires status + message kwargs.
    instance.file.return_value.content.side_effect = BoxOAuthException(
        status=400,
        message="invalid_grant",
    )

    with pytest.raises(BoxAuthError, match="OAuth exchange failed"):
        box_client.download_file("123")


# ---- Retry behavior on 429/503 -------------------------------------------


def test_429_honors_retry_after_header_then_succeeds(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    sleep = mocker.patch("shared.box_client.time.sleep")

    err_429 = _box_api_error(429, headers={"Retry-After": "0.5"})
    instance.file.return_value.content.side_effect = [err_429, b"OK-bytes"]

    result = box_client.download_file("123")

    assert result == b"OK-bytes"
    sleep.assert_called_once_with(0.5)


def test_429_without_retry_after_falls_back_to_exponential_backoff(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    sleep = mocker.patch("shared.box_client.time.sleep")

    err_429 = _box_api_error(429, headers={})  # no Retry-After
    instance.file.return_value.content.side_effect = [err_429, b"OK"]

    box_client.download_file("123")

    # First retry — attempt index 0 → 2^0 = 1.0s backoff.
    sleep.assert_called_once_with(1.0)


def test_503_retries_then_succeeds(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    mocker.patch("shared.box_client.time.sleep")

    err_503 = _box_api_error(503, headers={})
    instance.file.return_value.content.side_effect = [err_503, b"OK"]

    assert box_client.download_file("123") == b"OK"


def test_429_after_max_retries_raises_rate_limit_error(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    sleep = mocker.patch("shared.box_client.time.sleep")

    err_429 = _box_api_error(429, message="slow down", headers={"Retry-After": "0.1"})
    instance.file.return_value.content.side_effect = [err_429] * box_client.MAX_RETRIES

    with pytest.raises(BoxRateLimitError, match="slow down"):
        box_client.download_file("123")

    # MAX_RETRIES attempts → (MAX_RETRIES - 1) sleeps between them.
    assert sleep.call_count == box_client.MAX_RETRIES - 1


def test_non_retriable_status_raises_immediately_without_sleep(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    sleep = mocker.patch("shared.box_client.time.sleep")

    instance.file.return_value.content.side_effect = _box_api_error(404)

    with pytest.raises(BoxNotFoundError):
        box_client.download_file("missing-file")

    sleep.assert_not_called()


def test_retry_after_unparseable_falls_back_to_backoff(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    sleep = mocker.patch("shared.box_client.time.sleep")

    err_429 = _box_api_error(429, headers={"Retry-After": "garbage"})
    instance.file.return_value.content.side_effect = [err_429, b"OK"]

    box_client.download_file("123")

    sleep.assert_called_once_with(1.0)


# ---- Public-API method wiring --------------------------------------------


def test_upload_file_returns_minimal_metadata_dict(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    uploaded = SimpleNamespace(id="f-1", name="report.pdf", size=12345)
    instance.folder.return_value.upload.return_value = uploaded

    result = box_client.upload_file("99", "/tmp/report.pdf")

    assert result == {"id": "f-1", "name": "report.pdf", "size": 12345}
    instance.folder.assert_called_once_with("99")
    instance.folder.return_value.upload.assert_called_once_with(
        "/tmp/report.pdf", file_name=None,
    )


def test_upload_file_forwards_explicit_name(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.upload.return_value = SimpleNamespace(
        id="f", name="x", size=1,
    )

    box_client.upload_file("99", "/tmp/local.pdf", name="renamed.pdf")

    instance.folder.return_value.upload.assert_called_once_with(
        "/tmp/local.pdf", file_name="renamed.pdf",
    )


def test_upload_file_conflict_raises_box_conflict_error(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.upload.side_effect = _box_api_error(
        409, message="item_name_in_use",
    )

    with pytest.raises(BoxConflictError, match="item_name_in_use"):
        box_client.upload_file("99", "/tmp/dup.pdf")


def test_list_folder_returns_minimal_items_and_passes_limit(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.get_items.return_value = [
        SimpleNamespace(id="1", name="A", type="folder"),
        SimpleNamespace(id="2", name="B.pdf", type="file"),
    ]

    items = box_client.list_folder("99", limit=50)

    assert items == [
        {"id": "1", "name": "A", "type": "folder"},
        {"id": "2", "name": "B.pdf", "type": "file"},
    ]
    instance.folder.return_value.get_items.assert_called_once_with(limit=50)


def test_search_passes_type_and_limit_and_returns_minimal_items(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    instance.search.return_value.query.return_value = [
        SimpleNamespace(id="x", name="match", type="file"),
    ]

    results = box_client.search("hello", type="file", limit=10)

    assert results == [{"id": "x", "name": "match", "type": "file"}]
    instance.search.return_value.query.assert_called_once_with(
        "hello", limit=10, result_type="file",
    )


def test_search_without_type_omits_result_type_kwarg(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    instance.search.return_value.query.return_value = []

    box_client.search("hello")

    call_kwargs = instance.search.return_value.query.call_args.kwargs
    assert "result_type" not in call_kwargs


def test_get_file_metadata_extracts_fields(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    instance.file.return_value.get.return_value = SimpleNamespace(
        id="f", name="r.pdf", size=42, modified_at="2026-05-20T13:00:00Z",
    )

    meta = box_client.get_file_metadata("f")

    assert meta == {
        "id": "f", "name": "r.pdf", "size": 42,
        "modified_at": "2026-05-20T13:00:00Z",
    }


def test_get_folder_by_path_walks_segments(mocker):
    """get_folder_by_path walks from root segment-by-segment using
    list_folder under the hood. Each segment must be matched as a folder
    (not file) before descent."""
    _, _, instance = _install_mocked_sdk(mocker)

    def items_for(folder_id):
        return {
            "0": [
                SimpleNamespace(id="100", name="Customer A", type="folder"),
            ],
            "100": [
                SimpleNamespace(id="200", name="2026", type="folder"),
                # decoy with the right name but wrong type — must be skipped
                SimpleNamespace(id="201", name="2026", type="file"),
            ],
        }[folder_id]

    instance.folder.side_effect = lambda fid: SimpleNamespace(
        get_items=lambda **kw: items_for(fid),
    )

    result = box_client.get_folder_by_path("Customer A/2026/")

    assert result == {"id": "200", "name": "2026", "type": "folder"}


def test_get_folder_by_path_missing_segment_raises_not_found(mocker):
    _, _, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.get_items.return_value = [
        SimpleNamespace(id="100", name="Other", type="folder"),
    ]

    with pytest.raises(BoxNotFoundError, match="not found"):
        box_client.get_folder_by_path("MissingCustomer/")


def test_get_folder_by_path_root_returns_root_shape(mocker):
    _install_mocked_sdk(mocker)
    result = box_client.get_folder_by_path("/")
    assert result == {"id": "0", "name": "All Files", "type": "folder"}


# ---- canonical_job_path --------------------------------------------------


@pytest.mark.parametrize(
    "customer,job_number,job_name,year,expected",
    [
        ("Evergreen", "2024.335", "Forefront", 2026,
         "/Evergreen/2024.335 — Forefront/2026/"),
        ("KSI", "2025.201", "Kiwi", 2026,
         "/KSI/2025.201 — Kiwi/2026/"),
    ],
)
def test_canonical_job_path_format(customer, job_number, job_name, year, expected):
    assert canonical_job_path(customer, job_number, job_name, year) == expected


# =========================================================================
# Phase-5 Safety Portal Box primitives — upload_bytes + get_or_create_folder
# =========================================================================


def _folder_item(id_: str, name: str, type_: str = "folder") -> SimpleNamespace:
    return SimpleNamespace(id=id_, name=name, type=type_)


def test_upload_bytes_streams_content_and_returns_metadata(mocker):
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.upload_stream.return_value = SimpleNamespace(
        id="900", name="2026-06-05-jha.pdf", size=2048
    )

    out = box_client.upload_bytes("123", "2026-06-05-jha.pdf", b"%PDF-1.4 ...")

    assert out == {"id": "900", "name": "2026-06-05-jha.pdf", "size": 2048}
    instance.folder.assert_called_with("123")
    # The bytes were wrapped in a stream and the name forwarded.
    call = instance.folder.return_value.upload_stream.call_args
    assert call.args[1] == "2026-06-05-jha.pdf"


def test_upload_bytes_conflict_raises_box_conflict(mocker):
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.upload_stream.side_effect = _box_api_error(
        409, message="item_name_in_use"
    )

    with pytest.raises(BoxConflictError):
        box_client.upload_bytes("123", "dup.pdf", b"x")


def test_upload_bytes_auth_failure_raises_box_auth(mocker):
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.upload_stream.side_effect = _box_api_error(401)
    with pytest.raises(BoxAuthError):
        box_client.upload_bytes("123", "x.pdf", b"x")


def test_get_or_create_folder_returns_existing_without_create(mocker):
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.get_items.return_value = [
        _folder_item("10", "Other"),
        _folder_item("11", "ITS Week of 2026-05-30 to 2026-06-05"),
    ]

    fid = box_client.get_or_create_folder("root", "ITS Week of 2026-05-30 to 2026-06-05")

    assert fid == "11"
    instance.folder.return_value.create_subfolder.assert_not_called()


def test_get_or_create_folder_creates_on_miss(mocker):
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.get_items.return_value = [_folder_item("10", "Other")]
    instance.folder.return_value.create_subfolder.return_value = SimpleNamespace(
        id="77", name="ITS Week of X"
    )

    fid = box_client.get_or_create_folder("root", "ITS Week of X")

    assert fid == "77"
    instance.folder.return_value.create_subfolder.assert_called_once_with("ITS Week of X")


def test_get_or_create_folder_conflict_refinds_existing(mocker):
    """Lost create-race: create returns 409, re-find adopts the winner's folder."""
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    # First find: miss. Post-409 re-find: the racer's folder is now present.
    instance.folder.return_value.get_items.side_effect = [
        [_folder_item("10", "Other")],
        [_folder_item("10", "Other"), _folder_item("88", "ITS Week of X")],
    ]
    instance.folder.return_value.create_subfolder.side_effect = _box_api_error(
        409, message="item_name_in_use"
    )

    fid = box_client.get_or_create_folder("root", "ITS Week of X")
    assert fid == "88"  # adopted the racer's folder, did not raise


# ---- move_folder (Track 6 archive) -------------------------------------------


def test_move_folder_moves_and_renames_in_one_call(mocker):
    """The Box/Smartsheet asymmetry, pinned.

    boxsdk emits a single PUT carrying both parent and name, so there is no
    moved-but-not-renamed crash window here — unlike the Smartsheet path, which needs a
    two-call move-then-rename. If this ever becomes two calls, the resume logic on the Box
    side has to grow an intermediate state it currently does not need.
    """
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.move.return_value = SimpleNamespace(
        id="500", name="Safety", parent=SimpleNamespace(id="900")
    )

    out = box_client.move_folder("500", "900", new_name="Safety")

    assert out == {"id": "500", "name": "Safety", "parent_id": "900"}
    instance.folder.return_value.move.assert_called_once()
    args, _ = instance.folder.return_value.move.call_args
    assert args[1] == "Safety"  # the rename rides the SAME call


def test_move_folder_without_a_rename_passes_none(mocker):
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.move.return_value = SimpleNamespace(
        id="500", name="Coker", parent=SimpleNamespace(id="900")
    )

    box_client.move_folder("500", "900")

    args, _ = instance.folder.return_value.move.call_args
    assert args[1] is None


def test_move_folder_409_adopts_when_the_existing_child_is_this_folder(mocker):
    """Replay, not collision: a prior run already moved+renamed it, so the retry no-ops.

    This is what lets the resume path re-issue the move without checking first.
    """
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.move.side_effect = _box_api_error(409, message="item_name_in_use")
    instance.folder.return_value.get_items.return_value = [_folder_item("500", "Safety")]

    out = box_client.move_folder("500", "900", new_name="Safety")

    assert out == {"id": "500", "name": "Safety", "parent_id": "900"}


def test_move_folder_409_raises_when_a_different_folder_holds_the_name(mocker):
    """A genuine collision must be loud.

    There is no merge primitive on either system, so silently adopting someone else's
    folder would fuse two job trees irreversibly.
    """
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.move.side_effect = _box_api_error(409, message="item_name_in_use")
    instance.folder.return_value.get_items.return_value = [_folder_item("777", "Safety")]

    with pytest.raises(BoxConflictError):
        box_client.move_folder("500", "900", new_name="Safety")


def test_box_client_exposes_no_folder_delete_primitive():
    """MOVE-ONLY by construction (prove-it-bites for the design constraint).

    A "move failed → delete and re-upload" recovery would be catastrophic and irreversible,
    so the primitive that would enable it must simply not exist in this module.
    """
    for banned in ("delete_folder", "rename_folder", "delete_file"):
        assert not hasattr(box_client, banned), (
            f"box_client.{banned} appeared — the archive path is MOVE-ONLY on purpose; "
            f"adding a delete primitive re-opens a destructive recovery route"
        )


def test_upload_bytes_oauth_exception_raises_box_auth(mocker):
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.upload_stream.side_effect = BoxOAuthException(
        400, "bad refresh token"
    )
    with pytest.raises(BoxAuthError, match="OAuth"):
        box_client.upload_bytes("1", "x.pdf", b"x")


def test_get_or_create_folder_409_but_refind_still_misses_reraises(mocker):
    """409 on create AND the post-conflict re-find still misses → re-raise (loud,
    never proceed with no folder)."""
    _oauth, _client_cls, instance = _install_mocked_sdk(mocker)
    instance.folder.return_value.get_items.side_effect = [
        [_folder_item("10", "Other")],  # initial find: miss
        [_folder_item("10", "Other")],  # post-409 re-find: still miss
    ]
    instance.folder.return_value.create_subfolder.side_effect = _box_api_error(
        409, message="item_name_in_use"
    )
    with pytest.raises(BoxConflictError):
        box_client.get_or_create_folder("root", "ITS Week of X")


# ---- upload_bytes_or_new_version (PR-G: version-on-conflict) --------------


def test_upload_bytes_or_new_version_no_conflict_passes_through(mocker):
    up = mocker.patch.object(
        box_client, "upload_bytes", return_value={"id": "1", "name": "x.pdf", "size": 3}
    )
    out = box_client.upload_bytes_or_new_version("F", "x.pdf", b"abc")
    assert out == {"id": "1", "name": "x.pdf", "size": 3}
    up.assert_called_once_with("F", "x.pdf", b"abc")


def test_upload_bytes_or_new_version_conflict_uploads_new_version(mocker):
    mocker.patch.object(box_client, "upload_bytes", side_effect=BoxConflictError("409"))
    mocker.patch.object(box_client, "_find_child_file", return_value="999")
    client = MagicMock()
    client.file.return_value.update_contents_with_stream.return_value = SimpleNamespace(
        id="999", name="x.pdf", size=5
    )
    mocker.patch.object(box_client, "get_client", return_value=client)
    out = box_client.upload_bytes_or_new_version("F", "x.pdf", b"abcde")
    assert out == {"id": "999", "name": "x.pdf", "size": 5}  # stable id = same file, new version
    client.file.assert_called_once_with("999")
    client.file.return_value.update_contents_with_stream.assert_called_once()


def test_upload_bytes_or_new_version_conflict_but_file_vanished_reraises(mocker):
    """A 409 whose conflicting file then can't be found (race) re-raises, never silent."""
    mocker.patch.object(box_client, "upload_bytes", side_effect=BoxConflictError("409"))
    mocker.patch.object(box_client, "_find_child_file", return_value=None)
    with pytest.raises(BoxConflictError):
        box_client.upload_bytes_or_new_version("F", "x.pdf", b"abc")
