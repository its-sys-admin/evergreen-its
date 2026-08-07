"""REAL-child-process tests for po_materials/estimate_sandbox.py — the killable
rlimited isolation boundary for hostile-document parsing (ADR-0004 red-team #5).

NO MOCKS, deliberately: every test spawns the actual
`python -m po_materials.estimate_sandbox` child through `run_sandboxed` and proves
the reaping contract against a LIVE process — a mocked subprocess would prove
nothing about the boundary that keeps the daemon alive (prove-the-control-bites).
The hostile behaviors come from the documented test-support child fns
(`_test_spin` / `_test_alloc` / `_test_crash` / `_test_echo` in the module's
`__main__` dispatch); timeouts stay at 2–3s so the suite stays fast.

Contract pinned (delete the sandbox's kill/timeout/exit handling and these fail):
  * CPU-spinning child → reaped within timeout_s (+ slack) → None; parent alive.
  * allocation-bomb child → dies to RLIMIT_AS where the platform enforces it
    (Linux) OR is reaped by the CPU/wall-clock bound (Darwin rejects lowering
    RLIMIT_AS — the module docstring's honesty note); either way → None, parent
    alive, and the child's own _TEST_ALLOC_CAP_BYTES bound keeps host memory safe.
  * crashing child (nonzero exit) → None.
  * unknown fn name → None (refused parent-side, no child ever spawned).
  * happy path: stdin bytes → child → JSON on stdout, round-tripped intact.

Run with: pytest -q tests/test_estimate_sandbox.py
"""
from __future__ import annotations

import hashlib
import json
import time

from po_materials import estimate_sandbox

# Wall-clock slack on top of timeout_s before a reap counts as "too slow":
# generous for a loaded CI runner spawning a fresh interpreter, but decisively
# below "wedged forever" — an unreaped child would blow well past this.
REAP_SLACK_S = 10.0


def test_cpu_spinning_child_reaped_within_timeout_parent_survives():
    """(a) A CPU-spinning parse child is killed at the budget (RLIMIT_CPU or the
    parent subprocess timeout, whichever lands first) and run_sandboxed returns
    None — the parent (this test process) simply continues."""
    start = time.monotonic()
    out = estimate_sandbox.run_sandboxed("_test_spin", b"", timeout_s=2)
    elapsed = time.monotonic() - start
    assert out is None
    assert elapsed >= 0  # parent alive and measuring — the reap did not hang us
    assert elapsed < 2 + REAP_SLACK_S


def test_allocation_bomb_child_dies_to_rlimit_or_reap_parent_survives():
    """(b) A large-allocation child either dies to the lowered RLIMIT_AS (where
    the kernel enforces it) or allocates only its bounded cap and is reaped at
    the wall-clock/CPU budget — None either way, parent alive."""
    start = time.monotonic()
    out = estimate_sandbox.run_sandboxed(
        "_test_alloc", b"", timeout_s=3, rlimit_bytes=256 * 1024 * 1024
    )
    elapsed = time.monotonic() - start
    assert out is None
    assert elapsed < 3 + REAP_SLACK_S


def test_crashing_child_nonzero_exit_returns_none():
    """(c) A child that dies mid-parse (uncaught exception → nonzero exit) maps
    to None — the caller's degrade signal, never an exception in the parent."""
    assert estimate_sandbox.run_sandboxed("_test_crash", b"", timeout_s=5) is None


def test_unknown_fn_name_refused_returns_none():
    """(d) An fn name outside _ALLOWED_FNS is refused parent-side (no spawn)."""
    assert estimate_sandbox.run_sandboxed("no_such_fn", b"", timeout_s=5) is None


def test_happy_path_round_trip_through_real_child():
    """(e) The full transport works end-to-end: stdin bytes reach the child
    intact (sha256-proven) and the JSON-on-stdout contract returns cleanly."""
    payload = b"estimate-sandbox round trip \x00\xff\x01 bytes"
    out = estimate_sandbox.run_sandboxed("_test_echo", payload, timeout_s=15)
    assert out is not None
    doc = json.loads(out)
    assert doc == {
        "echo_len": len(payload),
        "echo_sha256": hashlib.sha256(payload).hexdigest(),
    }


# ---- extract_xlsx_rows (PR3b materials-manifest grid extraction) --------------------
#
# Same NO-MOCKS rule: every case below spawns a REAL child over a REAL workbook built
# in-process by openpyxl — the whole point of relocating openpyxl into the sandbox is
# that the parse happens in another process, and only a live child proves that.


def _xlsx_bytes(rows_by_sheet: dict[str, list[list[object]]]) -> bytes:
    """A real .xlsx in memory. openpyxl is imported HERE (test-side) deliberately —
    the module under test must only ever load it inside the child."""
    import io  # noqa: PLC0415 — test-local by design

    import openpyxl  # noqa: PLC0415 — test-local by design

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in rows_by_sheet.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _extract(data: bytes, sheets: str = "20") -> bytes | None:
    return estimate_sandbox.run_sandboxed(
        "extract_xlsx_rows", data, timeout_s=estimate_sandbox.XLSX_TIMEOUT_S, args=[sheets]
    )


def test_extract_xlsx_rows_round_trips_a_real_workbook_through_a_real_child():
    """(f) The new fn is DISPATCHED, not swallowed by the trailing `else:` — which
    falls through to the allocation bomb, so a missing branch would look like a
    plain timeout rather than an error."""
    out = _extract(_xlsx_bytes({"Export": [["Part Number", "Qty"], ["7006955", 4]]}))
    assert out is not None, "extract_xlsx_rows returned None — is its dispatch branch missing?"
    doc = json.loads(out)
    assert [s["name"] for s in doc["sheets"]] == ["Export"]
    assert doc["sheets"][0]["rows"][0] == ["Part Number", "Qty"]


def test_extract_xlsx_rows_preserves_numeric_type_so_part_numbers_survive():
    """THE fidelity control. A part number stored as a NUMBER must reach the parser as
    a number: manifest_parse.normalize_cell renders a float 7006955.0 as "7006955" but
    a str "7006955.0" as "7006955.0", which matches no part on earth. A child that
    str()'d every cell would pass the round-trip test above and still corrupt every
    numeric part number in the corpus."""
    from field_ops.manifest_parse import normalize_cell  # noqa: PLC0415 — test-local

    out = _extract(_xlsx_bytes({"Sheet1": [["Part Number", "Qty"], [7006955, 4]]}))
    assert out is not None
    cell = json.loads(out)["sheets"][0]["rows"][1][0]
    assert not isinstance(cell, str), f"part number arrived as str — numeric type lost: {cell!r}"
    assert normalize_cell(cell) == "7006955"


def test_extract_xlsx_rows_stringifies_datetimes_into_the_parsers_iso_form():
    """json.dumps cannot encode a datetime, so the child must stringify it — into the
    exact "YYYY-MM-DD 00:00:00" shape normalize_cell's regex collapses to a date. An
    un-stringified datetime would raise in the child and lose the whole workbook."""
    import datetime  # noqa: PLC0415 — test-local by design

    from field_ops.manifest_parse import normalize_cell  # noqa: PLC0415 — test-local

    out = _extract(_xlsx_bytes({"Sheet1": [["Ship Date"], [datetime.datetime(2026, 6, 26)]]}))
    assert out is not None
    cell = json.loads(out)["sheets"][0]["rows"][1][0]
    assert cell == "2026-06-26 00:00:00"
    assert normalize_cell(cell) == "2026-06-26"


def test_extract_xlsx_rows_bounds_rows_and_columns_in_the_child():
    """The caps are enforced AS THE GRID IS BUILT. MAX_CHILD_STDOUT_BYTES is a
    parent-side check that only fires once the child has already materialized the
    payload, so an over-declared workbook must be truncated child-side or it exhausts
    the child before the parent can refuse it."""
    wide = [[f"c{i}" for i in range(estimate_sandbox.MANIFEST_XLSX_MAX_COLS_PER_ROW + 25)]]
    tall = [[i] for i in range(estimate_sandbox.MANIFEST_XLSX_MAX_ROWS_PER_SHEET + 40)]
    out = _extract(_xlsx_bytes({"Wide": wide, "Tall": tall}))
    assert out is not None
    sheets = {s["name"]: s["rows"] for s in json.loads(out)["sheets"]}
    assert len(sheets["Wide"][0]) == estimate_sandbox.MANIFEST_XLSX_MAX_COLS_PER_ROW
    assert len(sheets["Tall"]) == estimate_sandbox.MANIFEST_XLSX_MAX_ROWS_PER_SHEET


def test_extract_xlsx_rows_bounds_a_single_hostile_cell():
    """One cell holding megabytes of text must not ride into D1 and then into the
    validate screen's grid."""
    out = _extract(_xlsx_bytes({"S": [["x" * (estimate_sandbox.MANIFEST_XLSX_MAX_CELL_CHARS + 500)]]}))
    assert out is not None
    assert len(json.loads(out)["sheets"][0]["rows"][0][0]) == estimate_sandbox.MANIFEST_XLSX_MAX_CELL_CHARS


def test_extract_xlsx_rows_max_sheets_arg_is_honoured():
    """The single int argv slot means max SHEETS for this fn (it means max PAGES for
    the PDF fns) — the dispatch reuses one parameter across differently-shaped fns."""
    out = _extract(_xlsx_bytes({"A": [[1]], "B": [[2]], "C": [[3]]}), sheets="2")
    assert out is not None
    assert [s["name"] for s in json.loads(out)["sheets"]] == ["A", "B"]


def test_extract_xlsx_rows_on_non_workbook_bytes_degrades_to_none():
    """A hostile / corrupt container raises in the child → nonzero exit → None. The
    caller refuses the manifest; the daemon never sees an exception."""
    assert _extract(b"not a workbook at all \x00\xff") is None


def test_openpyxl_is_never_imported_at_sandbox_module_level():
    """The whole point of the relocation: importing estimate_sandbox must NOT pull
    openpyxl into the daemon process. The import lives inside the child fn body, where
    only the child ever evaluates it."""
    import ast  # noqa: PLC0415 — test-local by design
    import pathlib  # noqa: PLC0415 — test-local by design

    tree = ast.parse(pathlib.Path(estimate_sandbox.__file__).read_text(encoding="utf-8"))
    for node in tree.body:  # module level ONLY — nested child-fn imports are the design
        if isinstance(node, ast.Import):
            assert all("openpyxl" not in a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert "openpyxl" not in (node.module or "")
