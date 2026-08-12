"""Tests for shared/state_io.py.

Filesystem-only — no external SDKs. The conftest autouse keychain +
kill_switch mocks are no-ops for this module (state_io imports neither);
they're harmless to inherit.

Run with: pytest -q tests/test_state_io.py
"""
from __future__ import annotations

import fcntl
import json
import threading
import time
from pathlib import Path

import pytest

import shared.state_io as state_io

# ---- atomic_write_json --------------------------------------------------


def test_atomic_write_json_writes_valid_json(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_io.atomic_write_json(state_path, {"a": 1, "b": [2, 3]})

    assert json.loads(state_path.read_text()) == {"a": 1, "b": [2, 3]}


def test_atomic_write_json_overwrites_existing(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"old": True}))

    state_io.atomic_write_json(state_path, {"new": True})

    assert json.loads(state_path.read_text()) == {"new": True}


def test_atomic_write_json_uses_sort_keys_and_indent(tmp_path: Path) -> None:
    """sort_keys + indent=2 deterministic output — load-bearing for clean diffs."""
    state_path = tmp_path / "state.json"
    state_io.atomic_write_json(state_path, {"b": 2, "a": 1})

    text = state_path.read_text()
    assert text == '{\n  "a": 1,\n  "b": 2\n}'


def test_atomic_write_json_creates_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "nested" / "dir" / "state.json"
    state_io.atomic_write_json(state_path, {"a": 1})

    assert json.loads(state_path.read_text()) == {"a": 1}


def test_atomic_write_json_cleans_up_temp_on_serialization_failure(
    tmp_path: Path,
) -> None:
    """Serialization failure must not leak ``.tmp.<pid>.<rand>`` files."""
    state_path = tmp_path / "state.json"

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        state_io.atomic_write_json(state_path, {"bad": Unserializable()})

    leftover = list(tmp_path.glob("state.json.tmp.*"))
    assert leftover == [], f"unexpected temp files: {leftover}"


def test_atomic_write_json_concurrent_readers_never_see_torn_writes(
    tmp_path: Path,
) -> None:
    """Reader thread spinning on read_text + json.loads never observes a partial file."""
    state_path = tmp_path / "state.json"
    # Seed so the path always exists for the reader.
    state_io.atomic_write_json(state_path, {"i": 0})

    stop = threading.Event()
    errors: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(state_path.read_text())
            except (json.JSONDecodeError, FileNotFoundError) as exc:
                errors.append(repr(exc))
                return

    rt = threading.Thread(target=reader)
    rt.start()
    try:
        for i in range(200):
            state_io.atomic_write_json(state_path, {"i": i, "payload": "x" * 500})
    finally:
        stop.set()
        rt.join()

    assert errors == []


# ---- atomic_write_text --------------------------------------------------


def test_atomic_write_text_writes_raw_text(tmp_path: Path) -> None:
    state_path = tmp_path / "heartbeat.txt"
    state_io.atomic_write_text(state_path, "2026-05-25T14:00:00+00:00")

    assert state_path.read_text() == "2026-05-25T14:00:00+00:00"


def test_atomic_write_text_overwrites_existing(tmp_path: Path) -> None:
    state_path = tmp_path / "heartbeat.txt"
    state_path.write_text("old timestamp")

    state_io.atomic_write_text(state_path, "new timestamp")

    assert state_path.read_text() == "new timestamp"


def test_atomic_write_text_creates_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "nested" / "dir" / "heartbeat.txt"
    state_io.atomic_write_text(state_path, "hello")

    assert state_path.read_text() == "hello"


# ---- with_path_lock -----------------------------------------------------


def test_with_path_lock_acquires_and_releases(tmp_path: Path) -> None:
    """A fresh acquire-release cycle leaves the sidecar unlocked for re-acquire."""
    state_path = tmp_path / "state.json"

    with state_io.with_path_lock(state_path):
        pass

    # Re-acquire must succeed immediately — bounded retry never triggered.
    with state_io.with_path_lock(state_path):
        pass


def test_with_path_lock_creates_sidecar(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"

    with state_io.with_path_lock(state_path):
        assert (tmp_path / "state.json.lock").exists()


def test_with_path_lock_persists_sidecar_after_exit(tmp_path: Path) -> None:
    """Sidecar `.lock` is NOT removed on exit; its existence is not a failure signal."""
    state_path = tmp_path / "state.json"

    with state_io.with_path_lock(state_path):
        pass

    assert (tmp_path / "state.json.lock").exists()


def test_with_path_lock_times_out_when_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded retry exhausts → StateLockTimeoutError with diagnostic message."""
    # Speed up the test: 5×1ms ≈ 5ms instead of 5×50ms ≈ 250ms.
    monkeypatch.setattr(state_io, "_LOCK_RETRY_DELAY_SECONDS", 0.001)
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "state.json.lock"

    holder = lock_path.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(state_io.StateLockTimeoutError, match=r"after 5 retries"):
            with state_io.with_path_lock(state_path):
                pytest.fail("body should not run when lock cannot be acquired")
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_with_path_lock_releases_on_exception_in_body(tmp_path: Path) -> None:
    """Exception inside the body must still release the lock for the next caller."""
    state_path = tmp_path / "state.json"

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        with state_io.with_path_lock(state_path):
            raise BoomError()

    # Lock released — next acquire succeeds without retry exhaustion.
    with state_io.with_path_lock(state_path):
        pass


def test_sidecar_lock_survives_atomic_write_on_data_path(tmp_path: Path) -> None:
    """Regression: os.replace on the data file must NOT invalidate the sidecar lock.

    If the lock were held on the data file itself, os.replace would swap
    the inode, the held FD would point to an orphaned inode, and a
    concurrent writer could acquire `the same` lock and corrupt the
    read-modify-write triple. The sidecar lives at a separate path that
    atomic_write_json never replaces, so the kernel-side lock survives
    every atomic write inside the context.
    """
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "state.json.lock"

    with state_io.with_path_lock(state_path):
        # Trigger os.replace on state_path (the data file).
        state_io.atomic_write_json(state_path, {"hello": "world"})

        # A second FD on the sidecar must NOT acquire — if it did, the
        # os.replace would have broken the lock guarantee for the RMW.
        other_fh = lock_path.open("a+")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            other_fh.close()


# ---- F23 concurrent-writer regression -----------------------------------


def test_concurrent_writers_both_writes_land(tmp_path: Path) -> None:
    """F23 protection: two threads RMWing the same shared state file — no lost writes.

    Mirrors the daemon-shared `heartbeat_row_ids.json` pattern: each
    daemon reads the file, mutates its own key, writes back. Without the
    path lock, the second writer's read can predate the first's write,
    clobbering the first's key. With the lock, the second writer's RMW
    serializes after the first; both keys land.
    """
    state_path = tmp_path / "heartbeat_row_ids.json"

    def writer(daemon_key: str, row_id: int) -> None:
        with state_io.with_path_lock(state_path):
            current: dict[str, int] = {}
            if state_path.exists():
                try:
                    parsed = json.loads(state_path.read_text())
                    if isinstance(parsed, dict):
                        current = parsed
                except (OSError, json.JSONDecodeError):
                    current = {}
            current[daemon_key] = row_id
            state_io.atomic_write_json(state_path, current)

    threads = [
        threading.Thread(target=writer, args=("safety_reports.portal_poll", 111)),
        threading.Thread(target=writer, args=("safety_reports.weekly_send_poll", 222)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = json.loads(state_path.read_text())
    assert result == {
        "safety_reports.portal_poll": 111,
        "safety_reports.weekly_send_poll": 222,
    }


def test_concurrent_writers_lock_serializes_overlap(tmp_path: Path) -> None:
    """Two writers never hold the path lock at the same time.

    Asserts MUTUAL EXCLUSION directly — a shared occupancy counter that must never
    exceed 1 inside the critical section — rather than inferring it from wall-clock
    end times.

    The previous form staggered the second thread by 1ms and asserted
    ``second_end >= first_end``. Both halves were load-sensitive: on a loaded machine
    the 1ms stagger can invert, so the *second* thread takes the lock first, finishes
    immediately, and its end time legitimately precedes the first's — a green lock
    reported as a red test. Here the barrier makes the contended ordering
    deterministic instead of hoped-for: the contender does not even attempt the lock
    until the holder is provably inside it.

    The hold stays at 30ms against `state_io`'s 5×50ms (~250ms) bounded retry budget,
    so the contender is guaranteed to retry-and-win rather than raise
    StateLockTimeoutError.
    """
    state_path = tmp_path / "state.json"
    holder_is_inside = threading.Event()
    occupancy_lock = threading.Lock()
    occupancy = 0
    max_occupancy = 0
    errors: list[BaseException] = []

    def writer(name: str, is_holder: bool) -> None:
        nonlocal occupancy, max_occupancy
        try:
            if not is_holder:
                # Only attempt the lock once the holder is provably inside it.
                assert holder_is_inside.wait(timeout=5.0), "holder never entered the lock"
            with state_io.with_path_lock(state_path):
                with occupancy_lock:
                    occupancy += 1
                    max_occupancy = max(max_occupancy, occupancy)
                if is_holder:
                    holder_is_inside.set()
                    # Hold the lock across the contender's retry window.
                    time.sleep(0.03)
                state_io.atomic_write_json(state_path, {name: True})
                with occupancy_lock:
                    occupancy -= 1
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread below
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("first", True)),
        threading.Thread(target=writer, args=("second", False)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "a writer thread never finished"

    assert not errors, f"writer raised: {errors!r}"
    # THE lock contract: the critical section was never occupied by both writers.
    assert max_occupancy == 1, f"lock allowed {max_occupancy} concurrent writers"
    assert occupancy == 0
    # Both writes ran; the survivor is whichever released last.
    assert state_path.exists()
    assert json.loads(state_path.read_text()) in ({"first": True}, {"second": True})
