"""schedule_ocr — the parent-side wrapper's validation + degrade contract.

The sandbox call is mocked (the REAL child runs in tests/test_schedule_ocr_corpus.py,
operator-run); what CI proves here is the strict-shape boundary: a child that emits
anything but the declared contract degrades to None, never a crash and never a
coerced half-payload."""
from __future__ import annotations

import json
from unittest import mock

from field_ops import schedule_ocr

GOOD = {
    "pages": [[{"text": "Task Name", "conf": 1.0, "x": 0.1, "y": 0.9, "w": 0.08, "h": 0.02}]],
    "page_sizes": [[2400, 3105]],
    "rotations": [270],
}


def _with_sandbox(returns: bytes | None):
    return mock.patch.object(
        schedule_ocr.estimate_sandbox, "run_sandboxed", return_value=returns
    )


def test_happy_path_validates_and_types():
    with _with_sandbox(json.dumps(GOOD).encode()):
        got = schedule_ocr.ocr_schedule_pages(b"%PDF-fake")
    assert got is not None
    assert got.rotations == [270]
    assert got.page_sizes == [(2400, 3105)]
    assert got.pages[0][0]["text"] == "Task Name"


def test_sandbox_none_degrades_to_none():
    with _with_sandbox(None):
        assert schedule_ocr.ocr_schedule_pages(b"x") is None


def test_malformed_json_degrades_to_none():
    with _with_sandbox(b"not json {{{"):
        assert schedule_ocr.ocr_schedule_pages(b"x") is None


def test_shape_violations_degrade_to_none():
    cases = [
        {},  # empty
        {**GOOD, "rotations": [45]},  # rotation outside the ladder
        {**GOOD, "page_sizes": [[2400]]},  # malformed size pair
        {**GOOD, "pages": [[{"text": 5, "x": 0, "y": 0, "w": 0, "h": 0}]]},  # non-str text
        {**GOOD, "pages": [[{"text": "ok"}]]},  # missing coords
        {**GOOD, "rotations": []},  # length mismatch
        [],  # not a dict
    ]
    for case in cases:
        with _with_sandbox(json.dumps(case).encode()):
            assert schedule_ocr.ocr_schedule_pages(b"x") is None, case


def test_max_pages_rides_into_the_sandbox_argv():
    with _with_sandbox(json.dumps(GOOD).encode()) as run:
        schedule_ocr.ocr_schedule_pages(b"x", max_pages=3)
    assert run.call_args.kwargs["args"] == ["3"]
