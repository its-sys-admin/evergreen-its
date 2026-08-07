"""Live-API integration test for the Phase-5 Box primitives (Op Stds §30).

Default `pytest -q` SKIPS this file (pyproject addopts `-m 'not integration'`).
Run with `pytest -m integration`. Requires the Box OAuth credentials in Keychain
(ITS_BOX_CLIENT_ID / _SECRET / _REFRESH_TOKEN). NOT executed in CI.

Exercises the new Box WRITE paths: get_or_create_folder (find-or-create, with an
ITS-prefixed name per the operator naming rule) + upload_bytes (in-memory upload).
Cleans up via the boxsdk client directly (the public surface has no delete).
"""
from __future__ import annotations

import pytest

from shared import box_client

pytestmark = pytest.mark.integration

# A unique, ITS-prefixed folder under the Box user root for the round-trip.
TEST_FOLDER = "ITS _int_box_primitive_sandbox"


@pytest.fixture
def _client():
    try:
        return box_client.get_client()
    except box_client.BoxError as e:
        pytest.skip(f"Box credentials unavailable: {e!r}")


def test_get_or_create_folder_and_upload_bytes_round_trip(_client):
    # Create (or adopt) the ITS-prefixed test folder directly under root ("0").
    folder_id = box_client.get_or_create_folder("0", TEST_FOLDER)
    file_id = None
    try:
        assert folder_id and folder_id != "0"

        # Idempotent: a second call returns the same folder (no duplicate).
        assert box_client.get_or_create_folder("0", TEST_FOLDER) == folder_id

        # upload_bytes lands a real file with the given name.
        meta = box_client.upload_bytes(
            folder_id, "2026-06-05-jha.pdf", b"%PDF-1.4 integration test\n"
        )
        file_id = meta["id"]
        assert meta["name"] == "2026-06-05-jha.pdf"
        assert int(meta["size"]) > 0
    finally:
        if file_id is not None:
            _client.file(file_id).delete()
        _client.folder(folder_id).delete(recursive=True)


def test_upload_bytes_or_new_version_versions_on_conflict(_client):
    """Live §30 (PR-G): a same-named re-upload lands a NEW Box VERSION (stable file
    id), not a 409 — the Compile-Now recompile path. Proves the conflict →
    update_contents branch against the live API."""
    folder_id = box_client.get_or_create_folder("0", TEST_FOLDER)
    file_id = None
    try:
        name = "ITS-version-conflict-probe.pdf"
        first = box_client.upload_bytes_or_new_version(folder_id, name, b"%PDF-1.4 v1\n")
        file_id = first["id"]
        # Same name again → NEW VERSION of the SAME file (not a 409, not a 2nd file).
        second = box_client.upload_bytes_or_new_version(folder_id, name, b"%PDF-1.4 v2 longer\n")
        assert second["id"] == file_id  # stable file id across versions
        # Exactly one file with that name in the folder (no duplicate / suffix).
        names = [
            it["name"]
            for it in box_client.list_folder(folder_id, limit=1000)
            if it["type"] == "file"
        ]
        assert names.count(name) == 1
        # The current content is v2 (the version replaced, not appended).
        assert box_client.download_file(file_id) == b"%PDF-1.4 v2 longer\n"
    finally:
        if file_id is not None:
            _client.file(file_id).delete()
        _client.folder(folder_id).delete(recursive=True)


def test_mirror_tree_nesting_round_trip(_client):
    """Live §30 (PR-K): the ROOT → per-job → per-week nesting the portal mirror tree
    files into — get_or_create_folder under a throwaway root, twice, idempotent. Also
    the permission probe: a 403 here ⇒ the Box app lacks access to the configured root."""
    root = box_client.get_or_create_folder("0", TEST_FOLDER)
    try:
        job = box_client.get_or_create_folder(root, "Bradley 1")
        week = box_client.get_or_create_folder(job, "week of 2026-05-30")
        assert job != root and week != job
        # Idempotent: re-resolving returns the SAME folders (no duplicates).
        assert box_client.get_or_create_folder(root, "Bradley 1") == job
        assert box_client.get_or_create_folder(job, "week of 2026-05-30") == week
        # `week` is genuinely nested under `job`.
        job_children = {it["name"]: it["id"] for it in box_client.list_folder(job)}
        assert job_children.get("week of 2026-05-30") == week
    finally:
        _client.folder(root).delete(recursive=True)


def test_move_folder_relocates_atomically_and_preserves_file_ids(_client):
    """Live §30: the Box half of the job archive.

    The load-bearing assertion is `download_file(file_id)` AFTER the move. Box item IDs are
    stable across a re-parent, which is what lets an already-approved-but-unsent weekly
    packet still send once its job's folder has been archived — `weekly_send` resolves the
    compiled packet by Box FILE ID, not by path. If IDs did not survive, archiving a job
    would silently break every pending send for it.

    Also proves move+rename is ONE call (no moved-but-unrenamed window) and that re-issuing
    the same move is a harmless replay, which is what the resume path relies on.
    """
    root = box_client.get_or_create_folder("0", TEST_FOLDER)
    job = box_client.get_or_create_folder(root, "_int_mv_job")
    archive = box_client.get_or_create_folder(root, "_int_mv_archive")
    archive_job = box_client.get_or_create_folder(archive, "_int_mv_job_archived")

    payload = b"%PDF-1.4 archive move test\n"
    meta = box_client.upload_bytes(job, "packet.pdf", payload)
    file_id = meta["id"]

    try:
        # Precondition: the file is reachable and the folder sits under the live root.
        assert box_client.download_file(file_id) == payload

        out = box_client.move_folder(job, archive_job, new_name="Safety")

        # Folder id stable, re-parented, and renamed in the SAME call.
        assert out["id"] == str(job)
        assert out["name"] == "Safety"
        assert out["parent_id"] == str(archive_job)

        # THE assertion: the contained file's ID still resolves to the same bytes.
        assert box_client.download_file(file_id) == payload

        # Gone from the old parent, present under the new one.
        assert box_client.find_child_folder(root, "_int_mv_job") is None
        assert box_client.find_child_folder(archive_job, "Safety") == str(job)

        # Replay: re-issuing the identical move is a no-op, not a 409 escape.
        again = box_client.move_folder(job, archive_job, new_name="Safety")
        assert again["id"] == str(job)
    finally:
        # Recursive delete of the sandbox subtree — the ITS-prefixed test root only.
        try:
            _client.folder(archive).delete(recursive=True)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        try:
            _client.folder(job).delete(recursive=True)
        except Exception:  # noqa: BLE001
            pass
