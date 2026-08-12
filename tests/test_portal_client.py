"""Tests for shared/portal_client.py.

All HTTP is mocked — these tests never hit the network. Live coverage (a real
Worker round-trip) is the deploy-gated tests/test_portal_client_integration.py.

Run with: pytest -q tests/test_portal_client.py
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from shared import portal_client
from shared.portal_client import (
    PortalAuthError,
    PortalRateLimitError,
    PortalTransportError,
)

BASE = "https://portal.example.com"
TOKEN = "fake-bearer"


def _mock_response(*, status=200, json_body=None, headers=None, text=""):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = json_body if json_body is not None else {}
    response.headers = headers if headers is not None else {}
    response.text = text
    return response


def _patch_requests(mocker, responses):
    if not isinstance(responses, list):
        responses = [responses]
    return mocker.patch(
        "shared.portal_client.requests.request",
        side_effect=responses if len(responses) > 1 else responses * 100,
    )


# ---- get_pending ---------------------------------------------------------


def test_get_pending_returns_rows_and_sends_bearer_and_limit(mocker):
    rows = [
        {"submission_uuid": "u1", "job_id": "JOB-1", "form_code": "jha-v1",
         "work_date": "2026-06-05", "payload_json": "{}", "amends_uuid": None,
         "hmac": "abc", "created_at": 1},
    ]
    req = _patch_requests(mocker, _mock_response(json_body={"pending": rows}))

    out = portal_client.get_pending(BASE, TOKEN, limit=25)

    assert out == rows
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/internal/pending")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"
    assert kwargs["params"] == {"limit": 25}


def test_get_pending_empty_queue_returns_empty_list(mocker):
    _patch_requests(mocker, _mock_response(json_body={"pending": []}))
    assert portal_client.get_pending(BASE, TOKEN) == []


def test_get_pending_drops_non_dict_rows(mocker):
    _patch_requests(
        mocker,
        _mock_response(json_body={"pending": [{"submission_uuid": "u1"}, "garbage", 5]}),
    )
    out = portal_client.get_pending(BASE, TOKEN)
    assert out == [{"submission_uuid": "u1"}]


def test_get_pending_missing_pending_key_raises(mocker):
    _patch_requests(mocker, _mock_response(json_body={"unexpected": 1}))
    with pytest.raises(PortalTransportError, match="pending"):
        portal_client.get_pending(BASE, TOKEN)


def test_get_pending_401_raises_auth_error_without_retry(mocker):
    req = _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_pending(BASE, TOKEN)
    assert req.call_count == 1  # 401 is NOT retried


def test_get_pending_non_200_raises_transport_error(mocker):
    _patch_requests(mocker, _mock_response(status=500, text="boom"))
    with pytest.raises(PortalTransportError, match="500"):
        portal_client.get_pending(BASE, TOKEN)


def test_get_pending_non_json_body_raises(mocker):
    resp = _mock_response(status=200, text="<html>")
    resp.json.side_effect = ValueError
    _patch_requests(mocker, resp)
    with pytest.raises(PortalTransportError, match="non-JSON"):
        portal_client.get_pending(BASE, TOKEN)


# ---- mark_filed ----------------------------------------------------------


def test_mark_filed_posts_body_and_returns_found(mocker):
    req = _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": True}))

    found = portal_client.mark_filed(
        BASE, TOKEN, submission_uuid="u1", box_link="https://app.box.com/file/9"
    )

    assert found is True
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/internal/mark-filed")
    assert kwargs["json"] == {
        "submission_uuid": "u1",
        "box_link": "https://app.box.com/file/9",
        "box_file_id": None,
    }
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"


def test_mark_filed_found_false_when_worker_has_no_row(mocker):
    _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": False}))
    assert portal_client.mark_filed(BASE, TOKEN, submission_uuid="u1", box_link="x") is False


def test_mark_filed_carries_box_file_id_when_given(mocker):
    req = _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": True}))
    portal_client.mark_filed(
        BASE, TOKEN, submission_uuid="u1",
        box_link="https://app.box.com/file/42", box_file_id="42",
    )
    assert req.call_args.kwargs["json"] == {
        "submission_uuid": "u1",
        "box_link": "https://app.box.com/file/42",
        "box_file_id": "42",
    }


def test_mark_filed_401_raises_auth_error(mocker):
    _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.mark_filed(BASE, TOKEN, submission_uuid="u1", box_link="x")


# ---- push_jobs -----------------------------------------------------------


def test_push_jobs_posts_full_set_and_returns_summary(mocker):
    jobs = [
        {"job_id": "JOB-000001", "project_name": "Bradley 1", "active": 1},
        {"job_id": "JOB-000007", "project_name": "Atlantis", "active": 0},
    ]
    req = _patch_requests(
        mocker, _mock_response(json_body={"ok": True, "upserted": 2, "deactivated": 1})
    )

    out = portal_client.push_jobs(BASE, TOKEN, jobs)

    assert out == {"ok": True, "upserted": 2, "deactivated": 1}
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/internal/sync")
    assert kwargs["json"] == {"jobs": jobs}
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"


def test_push_jobs_401_raises_auth_error(mocker):
    _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.push_jobs(
            BASE, TOKEN, [{"job_id": "J", "project_name": "P", "active": 1}]
        )


def test_push_jobs_non_200_raises_transport_error(mocker):
    _patch_requests(mocker, _mock_response(status=500, text="boom"))
    with pytest.raises(PortalTransportError, match="500"):
        portal_client.push_jobs(
            BASE, TOKEN, [{"job_id": "J", "project_name": "P", "active": 1}]
        )


def test_push_jobs_503_then_success(mocker):
    mocker.patch("shared.portal_client.time.sleep")
    _patch_requests(
        mocker,
        [
            _mock_response(status=503),
            _mock_response(json_body={"ok": True, "upserted": 1, "deactivated": 0}),
        ],
    )
    out = portal_client.push_jobs(
        BASE, TOKEN, [{"job_id": "J", "project_name": "P", "active": 1}]
    )
    assert out["upserted"] == 1


# ---- get_pdf_requests (PR-4 Part A) --------------------------------------


def test_get_pdf_requests_returns_rows_and_sends_bearer_and_limit(mocker):
    rows = [
        {"submission_uuid": "u1", "box_file_id": "f9",
         "form_code": "jha-v1", "work_date": "2026-06-05"},
    ]
    req = _patch_requests(mocker, _mock_response(json_body={"pdf_requests": rows}))

    out = portal_client.get_pdf_requests(BASE, TOKEN, limit=25)

    assert out == rows
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/internal/pdf-requests")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"
    assert kwargs["params"] == {"limit": 25}


def test_get_pdf_requests_empty_returns_empty_list(mocker):
    _patch_requests(mocker, _mock_response(json_body={"pdf_requests": []}))
    assert portal_client.get_pdf_requests(BASE, TOKEN) == []


def test_get_pdf_requests_drops_non_dict_rows(mocker):
    _patch_requests(
        mocker,
        _mock_response(json_body={"pdf_requests": [{"submission_uuid": "u1"}, "x", 7]}),
    )
    assert portal_client.get_pdf_requests(BASE, TOKEN) == [{"submission_uuid": "u1"}]


@pytest.mark.parametrize(
    "body", [{"pdf_requests": {"x": 1}}, {"pdf_requests": "nope"}, {"pdf_requests": 5}, {}]
)
def test_get_pdf_requests_non_list_raises(mocker, body):
    _patch_requests(mocker, _mock_response(json_body=body))
    with pytest.raises(PortalTransportError, match="pdf_requests"):
        portal_client.get_pdf_requests(BASE, TOKEN)


def test_get_pdf_requests_401_raises_auth_without_retry(mocker):
    req = _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_pdf_requests(BASE, TOKEN)
    assert req.call_count == 1


def test_get_pdf_requests_non_200_raises_transport_error(mocker):
    _patch_requests(mocker, _mock_response(status=500, text="boom"))
    with pytest.raises(PortalTransportError, match="500"):
        portal_client.get_pdf_requests(BASE, TOKEN)


def test_get_pdf_requests_503_then_success(mocker):
    mocker.patch("shared.portal_client.time.sleep")
    _patch_requests(
        mocker,
        [_mock_response(status=503), _mock_response(json_body={"pdf_requests": []})],
    )
    assert portal_client.get_pdf_requests(BASE, TOKEN) == []


# ---- get_fieldops_material_incidents (M3 Slice 2) ------------------------


def test_get_fieldops_material_incidents_returns_rows_and_sends_bearer(mocker):
    rows = [
        {"submission_uuid": "sub-10", "job_id": "JOB-1", "project_name": "Job One",
         "issue": "Short", "line_uuid": "u-10", "line_status": "incident"},
    ]
    req = _patch_requests(mocker, _mock_response(json_body={"incidents": rows}))

    out = portal_client.get_fieldops_material_incidents(BASE, TOKEN)

    assert out == rows
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/internal/fieldops/material-incidents")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"


def test_get_fieldops_material_incidents_empty_returns_empty_list(mocker):
    _patch_requests(mocker, _mock_response(json_body={"incidents": []}))
    assert portal_client.get_fieldops_material_incidents(BASE, TOKEN) == []


def test_get_fieldops_material_incidents_drops_non_dict_rows(mocker):
    _patch_requests(
        mocker,
        _mock_response(json_body={"incidents": [{"submission_uuid": "sub-10"}, "x", 7]}),
    )
    assert portal_client.get_fieldops_material_incidents(BASE, TOKEN) == [
        {"submission_uuid": "sub-10"}
    ]


@pytest.mark.parametrize(
    "body", [{"incidents": {"x": 1}}, {"incidents": "nope"}, {"incidents": 5}, {}]
)
def test_get_fieldops_material_incidents_non_list_raises(mocker, body):
    _patch_requests(mocker, _mock_response(json_body=body))
    with pytest.raises(PortalTransportError, match="incidents"):
        portal_client.get_fieldops_material_incidents(BASE, TOKEN)


def test_get_fieldops_material_incidents_401_raises_auth_without_retry(mocker):
    req = _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_fieldops_material_incidents(BASE, TOKEN)
    assert req.call_count == 1


def test_get_fieldops_material_incidents_non_200_raises_transport_error(mocker):
    _patch_requests(mocker, _mock_response(status=500, text="boom"))
    with pytest.raises(PortalTransportError, match="500"):
        portal_client.get_fieldops_material_incidents(BASE, TOKEN)


# ---- upload_filed_pdf (PR-4 Part A) --------------------------------------


def test_upload_filed_pdf_posts_chunk_body_and_returns_ack(mocker):
    req = _patch_requests(
        mocker,
        _mock_response(json_body={"ok": True, "ready": False, "stored": True, "received": 1}),
    )

    out = portal_client.upload_filed_pdf(
        BASE, TOKEN, submission_uuid="u1",
        chunk_index=0, chunk_total=2, chunk_b64="QUJD",
    )

    assert out == {"ok": True, "ready": False, "stored": True, "received": 1}
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/internal/filed-pdf")
    assert kwargs["json"] == {
        "submission_uuid": "u1",
        "chunk_index": 0,
        "chunk_total": 2,
        "chunk_b64": "QUJD",
    }
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"


def test_upload_filed_pdf_401_raises_auth_error(mocker):
    _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.upload_filed_pdf(
            BASE, TOKEN, submission_uuid="u1",
            chunk_index=0, chunk_total=1, chunk_b64="QQ==",
        )


def test_upload_filed_pdf_invalid_chunk_400_raises_transport_error(mocker):
    # A bad chunk (400 invalid_chunk) must SURFACE as a transport error, not a silent
    # return — upload_filed_pdf delegates to the raise-on-non-200 `_request`.
    _patch_requests(mocker, _mock_response(status=400, text='{"error":"invalid_chunk"}'))
    with pytest.raises(PortalTransportError, match="400"):
        portal_client.upload_filed_pdf(
            BASE, TOKEN, submission_uuid="u1",
            chunk_index=0, chunk_total=1, chunk_b64="QQ==",
        )


def test_upload_filed_pdf_503_then_success(mocker):
    mocker.patch("shared.portal_client.time.sleep")
    _patch_requests(
        mocker,
        [
            _mock_response(status=503),
            _mock_response(json_body={"ok": True, "ready": True, "stored": True, "received": 1}),
        ],
    )
    out = portal_client.upload_filed_pdf(
        BASE, TOKEN, submission_uuid="u1",
        chunk_index=0, chunk_total=1, chunk_b64="QQ==",
    )
    assert out["ready"] is True


# ---- admin_request -------------------------------------------------------


def test_admin_request_returns_status_and_json(mocker):
    req = _patch_requests(
        mocker, _mock_response(status=201, json_body={"ok": True, "username": "a.b"})
    )
    status, data = portal_client.admin_request(
        BASE, TOKEN, "POST", "/api/internal/admin/users",
        json_body={"username": "a.b", "password": "x"},
    )
    assert status == 201 and data == {"ok": True, "username": "a.b"}
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/internal/admin/users")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"
    assert kwargs["json"] == {"username": "a.b", "password": "x"}


def test_admin_request_semantic_4xx_returned_not_raised(mocker):
    _patch_requests(mocker, _mock_response(status=409, json_body={"error": "exists"}))
    status, data = portal_client.admin_request(BASE, TOKEN, "POST", "/p")
    assert status == 409 and data == {"error": "exists"}


def test_admin_request_401_raises_auth(mocker):
    _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.admin_request(BASE, TOKEN, "GET", "/p")


def test_admin_request_503_then_success(mocker):
    mocker.patch("shared.portal_client.time.sleep")
    _patch_requests(
        mocker,
        [_mock_response(status=503), _mock_response(status=200, json_body={"users": []})],
    )
    status, data = portal_client.admin_request(BASE, TOKEN, "GET", "/p")
    assert status == 200 and data == {"users": []}


def test_admin_request_non_json_body_returns_empty_dict(mocker):
    resp = _mock_response(status=204)
    resp.json.side_effect = ValueError
    _patch_requests(mocker, resp)
    status, data = portal_client.admin_request(BASE, TOKEN, "POST", "/p")
    assert status == 204 and data == {}


# ---- retry / backoff -----------------------------------------------------


def test_503_then_success(mocker):
    sleeps = mocker.patch("shared.portal_client.time.sleep")
    _patch_requests(
        mocker,
        [_mock_response(status=503), _mock_response(json_body={"pending": []})],
    )
    assert portal_client.get_pending(BASE, TOKEN) == []
    sleeps.assert_called_once_with(1.0)  # 2**0 backoff (no Retry-After)


def test_429_honors_retry_after(mocker):
    sleeps = mocker.patch("shared.portal_client.time.sleep")
    _patch_requests(
        mocker,
        [
            _mock_response(status=429, headers={"Retry-After": "0.5"}),
            _mock_response(json_body={"pending": []}),
        ],
    )
    portal_client.get_pending(BASE, TOKEN)
    sleeps.assert_called_once_with(0.5)


def test_429_exhausted_raises_rate_limit(mocker):
    mocker.patch("shared.portal_client.time.sleep")
    _patch_requests(mocker, [_mock_response(status=429)] * 3)
    with pytest.raises(PortalRateLimitError):
        portal_client.get_pending(BASE, TOKEN)


def test_network_error_retried_then_raises(mocker):
    import requests as _r

    mocker.patch("shared.portal_client.time.sleep")
    mocker.patch(
        "shared.portal_client.requests.request",
        side_effect=_r.ConnectionError("no route"),
    )
    with pytest.raises(PortalTransportError, match="network failure"):
        portal_client.get_pending(BASE, TOKEN)


def test_network_error_then_success(mocker):
    import requests as _r

    mocker.patch("shared.portal_client.time.sleep")
    mocker.patch(
        "shared.portal_client.requests.request",
        side_effect=[_r.ConnectionError("blip"), _mock_response(json_body={"pending": []})],
    )
    assert portal_client.get_pending(BASE, TOKEN) == []


# ---- review-hardening: URL normalization, shape, truncation, backoff ------


@pytest.mark.parametrize("base", ["https://portal.example.com", "https://portal.example.com/"])
def test_base_url_trailing_slash_normalized(mocker, base):
    req = _patch_requests(mocker, _mock_response(json_body={"pending": []}))
    portal_client.get_pending(base, TOKEN)
    assert req.call_args.args[1] == "https://portal.example.com/api/internal/pending"


@pytest.mark.parametrize("body", [{"pending": {"x": 1}}, {"pending": "nope"}, {"pending": 5}, {}])
def test_get_pending_non_list_pending_raises(mocker, body):
    _patch_requests(mocker, _mock_response(json_body=body))
    with pytest.raises(PortalTransportError, match="pending"):
        portal_client.get_pending(BASE, TOKEN)


def test_network_error_backoff_sequence(mocker):
    import requests as _r

    sleeps = mocker.patch("shared.portal_client.time.sleep")
    mocker.patch("shared.portal_client.requests.request", side_effect=_r.ConnectionError("x"))
    with pytest.raises(PortalTransportError, match="network failure"):
        portal_client.get_pending(BASE, TOKEN)
    # MAX_RETRIES=3 → two backoff sleeps (2**0, 2**1) before the final raise.
    assert [c.args[0] for c in sleeps.call_args_list] == [1.0, 2.0]


def test_non_200_message_truncates_long_text(mocker):
    _patch_requests(mocker, _mock_response(status=500, text="x" * 500))
    with pytest.raises(PortalTransportError) as exc:
        portal_client.get_pending(BASE, TOKEN)
    msg = exc.value.args[0]
    assert "x" * 300 in msg          # the leading 300 chars are present
    assert "x" * 301 not in msg      # but the body was truncated, not dumped whole


def test_429_unparseable_retry_after_falls_back_to_backoff(mocker):
    sleeps = mocker.patch("shared.portal_client.time.sleep")
    _patch_requests(
        mocker,
        [
            _mock_response(status=429, headers={"Retry-After": "soon"}),
            _mock_response(json_body={"pending": []}),
        ],
    )
    portal_client.get_pending(BASE, TOKEN)
    sleeps.assert_called_once_with(1.0)  # 2**0 backoff when Retry-After is garbage


# ---- get_progress_rollup (P6) --------------------------------------------

_ROLLUP_OK = {
    "job_id": "JOB-1", "window": {"from": 100, "to": 200},
    "labor_hours": 42.5, "equipment": [{"name": "Skid Steer", "kind": "skid-steer"}],
    "open_tasks": 4, "materials": None, "generated_at": 199,
}


def test_get_progress_rollup_returns_dict_and_sends_params(mocker):
    req = _patch_requests(mocker, _mock_response(json_body=_ROLLUP_OK))
    out = portal_client.get_progress_rollup(
        BASE, TOKEN, job_id="JOB-1", week_from=100, week_to=200
    )
    assert out == _ROLLUP_OK
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/internal/progress-rollup")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"
    assert kwargs["params"] == {"job_id": "JOB-1", "from": 100, "to": 200}


def test_get_progress_rollup_graceful_zeros_pass_the_guard(mocker):
    # An activity-free week returns 0 / [] / 0 — a VALID body, not an error.
    _patch_requests(mocker, _mock_response(json_body={
        **_ROLLUP_OK, "labor_hours": 0, "equipment": [], "open_tasks": 0}))
    out = portal_client.get_progress_rollup(BASE, TOKEN, job_id="J", week_from=1, week_to=2)
    assert out["labor_hours"] == 0 and out["equipment"] == [] and out["open_tasks"] == 0


@pytest.mark.parametrize("bad,match", [
    ({**_ROLLUP_OK, "labor_hours": "nan"}, "labor_hours"),
    ({**_ROLLUP_OK, "labor_hours": True}, "labor_hours"),  # bool is not a number here
    ({k: v for k, v in _ROLLUP_OK.items() if k != "labor_hours"}, "labor_hours"),
    ({**_ROLLUP_OK, "equipment": "oops"}, "equipment"),
    ({k: v for k, v in _ROLLUP_OK.items() if k != "equipment"}, "equipment"),
    ({**_ROLLUP_OK, "open_tasks": "4"}, "open_tasks"),
    ({**_ROLLUP_OK, "open_tasks": False}, "open_tasks"),
])
def test_get_progress_rollup_malformed_body_raises(mocker, bad, match):
    _patch_requests(mocker, _mock_response(json_body=bad))
    with pytest.raises(PortalTransportError, match=match):
        portal_client.get_progress_rollup(BASE, TOKEN, job_id="J", week_from=1, week_to=2)


def test_get_progress_rollup_401_raises_auth_error_without_retry(mocker):
    req = _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_progress_rollup(BASE, TOKEN, job_id="J", week_from=1, week_to=2)
    assert req.call_count == 1  # 401 is NOT retried


# ---- get_prune_status (GS2) -----------------------------------------------


def test_get_prune_status_returns_meta_dict(mocker):
    meta = {
        "last_run_at": 1_780_000_000,
        "db_size_bytes": 4096,
        "size_warn": False,
        "counters": {"jobs": 1},
        "failed_stages": [],
    }
    req = _patch_requests(mocker, _mock_response(json_body={"prune": meta}))

    out = portal_client.get_prune_status(BASE, TOKEN)

    assert out == meta
    args, kwargs = req.call_args
    assert args[0] == "GET"
    assert args[1] == f"{BASE}/api/internal/prune-status"
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_get_prune_status_null_prune_returns_none(mocker):
    _patch_requests(mocker, _mock_response(json_body={"prune": None}))
    assert portal_client.get_prune_status(BASE, TOKEN) is None


def test_get_prune_status_non_dict_prune_raises(mocker):
    _patch_requests(mocker, _mock_response(json_body={"prune": [1, 2]}))
    with pytest.raises(PortalTransportError, match="prune"):
        portal_client.get_prune_status(BASE, TOKEN)


def test_get_prune_status_401_raises_auth(mocker):
    _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_prune_status(BASE, TOKEN)


# ---- get_item_photos_pending (G1 Slice 2) ---------------------------------


def test_get_item_photos_pending_returns_rows_and_sends_bearer_and_limit(mocker):
    rows = [
        {"id": 5, "item_state_id": 7, "photo_json": '{"data":"QUJD"}',
         "hmac": "abc", "created_at": 1},
    ]
    req = _patch_requests(mocker, _mock_response(json_body={"item_photos": rows}))

    out = portal_client.get_item_photos_pending(BASE, TOKEN, limit=25)

    assert out == rows
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/internal/item-photos/pending")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"
    assert kwargs["params"] == {"limit": 25}


def test_get_item_photos_pending_empty_returns_empty_list(mocker):
    _patch_requests(mocker, _mock_response(json_body={"item_photos": []}))
    assert portal_client.get_item_photos_pending(BASE, TOKEN) == []


def test_get_item_photos_pending_drops_non_dict_rows(mocker):
    _patch_requests(
        mocker,
        _mock_response(json_body={"item_photos": [{"id": 5}, "x", 7]}),
    )
    assert portal_client.get_item_photos_pending(BASE, TOKEN) == [{"id": 5}]


@pytest.mark.parametrize(
    "body", [{"item_photos": {"x": 1}}, {"item_photos": "nope"}, {"item_photos": 5}, {}]
)
def test_get_item_photos_pending_non_list_raises(mocker, body):
    _patch_requests(mocker, _mock_response(json_body=body))
    with pytest.raises(PortalTransportError, match="item_photos"):
        portal_client.get_item_photos_pending(BASE, TOKEN)


def test_get_item_photos_pending_401_raises_auth_without_retry(mocker):
    req = _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_item_photos_pending(BASE, TOKEN)
    assert req.call_count == 1


# ---- post_item_photo_result (G1 Slice 2) ----------------------------------


def test_post_item_photo_result_clean_posts_box_file_id_and_returns_found(mocker):
    req = _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": True}))

    found = portal_client.post_item_photo_result(
        BASE, TOKEN, photo_id=5, status="clean", box_file_id="box-9",
    )

    assert found is True
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/internal/item-photos/5/result")
    assert kwargs["json"] == {"status": "clean", "box_file_id": "box-9"}
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"


def test_post_item_photo_result_refused_posts_detail_only(mocker):
    req = _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": True}))

    found = portal_client.post_item_photo_result(
        BASE, TOKEN, photo_id=6, status="refused", detail="L1:magic_mismatch",
    )

    assert found is True
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/internal/item-photos/6/result")
    # box_file_id is OMITTED on refused (the Worker 400s a refused-with-box_file_id).
    assert kwargs["json"] == {"status": "refused", "detail": "L1:magic_mismatch"}


def test_post_item_photo_result_found_false_is_returned_not_raised(mocker):
    # Idempotent re-screen: an already-applied disposition returns found=False (benign).
    _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": False}))
    assert portal_client.post_item_photo_result(
        BASE, TOKEN, photo_id=5, status="clean", box_file_id="box-9",
    ) is False


def test_post_item_photo_result_invalid_400_raises_transport_error(mocker):
    # A contract violation (400 invalid_result) must SURFACE, never a silent return.
    _patch_requests(mocker, _mock_response(status=400, text='{"error":"invalid_result"}'))
    with pytest.raises(PortalTransportError, match="400"):
        portal_client.post_item_photo_result(BASE, TOKEN, photo_id=5, status="refused")


def test_post_item_photo_result_401_raises_auth_error(mocker):
    _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.post_item_photo_result(
            BASE, TOKEN, photo_id=5, status="clean", box_file_id="box-9",
        )


# ---- get_fieldops_equipment_snapshot (P7 Slice 2) ------------------------


def test_get_fieldops_equipment_snapshot_returns_both_arrays_and_sends_bearer(mocker):
    rows = [{"equipment_id": 10, "job_id": "J1", "name": "Unit Alpha"}]
    roster = [{"job_id": "J1", "project_name": "Job One"}]
    req = _patch_requests(
        mocker, _mock_response(json_body={"equipment": rows, "jobs_with_equipment": roster})
    )
    out = portal_client.get_fieldops_equipment_snapshot(BASE, TOKEN)
    assert out.equipment == rows
    assert out.jobs_with_equipment == roster
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/internal/fieldops/equipment-snapshot")
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_get_fieldops_equipment_snapshot_empty_returns_empty_lists(mocker):
    _patch_requests(mocker, _mock_response(json_body={"equipment": [], "jobs_with_equipment": []}))
    out = portal_client.get_fieldops_equipment_snapshot(BASE, TOKEN)
    assert out.equipment == [] and out.jobs_with_equipment == []


def test_get_fieldops_equipment_snapshot_drops_non_dict_rows_in_both(mocker):
    _patch_requests(
        mocker,
        _mock_response(
            json_body={
                "equipment": [{"equipment_id": 10}, "x", 7],
                "jobs_with_equipment": [{"job_id": "J1"}, 9, None],
            }
        ),
    )
    out = portal_client.get_fieldops_equipment_snapshot(BASE, TOKEN)
    assert out.equipment == [{"equipment_id": 10}]
    assert out.jobs_with_equipment == [{"job_id": "J1"}]


@pytest.mark.parametrize(
    "body",
    [
        {"equipment": {"x": 1}, "jobs_with_equipment": []},
        {"equipment": "nope", "jobs_with_equipment": []},
        {"jobs_with_equipment": []},  # equipment key missing entirely
    ],
)
def test_get_fieldops_equipment_snapshot_bad_equipment_raises(mocker, body):
    _patch_requests(mocker, _mock_response(json_body=body))
    with pytest.raises(PortalTransportError, match="equipment"):
        portal_client.get_fieldops_equipment_snapshot(BASE, TOKEN)


@pytest.mark.parametrize(
    "body",
    [
        {"equipment": [], "jobs_with_equipment": {"x": 1}},
        {"equipment": [], "jobs_with_equipment": "nope"},
        {"equipment": []},  # roster key missing entirely
    ],
)
def test_get_fieldops_equipment_snapshot_bad_roster_raises(mocker, body):
    _patch_requests(mocker, _mock_response(json_body=body))
    with pytest.raises(PortalTransportError, match="jobs_with_equipment"):
        portal_client.get_fieldops_equipment_snapshot(BASE, TOKEN)


def test_get_fieldops_equipment_snapshot_401_raises_auth_without_retry(mocker):
    req = _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_fieldops_equipment_snapshot(BASE, TOKEN)
    assert req.call_count == 1  # a 401 is NOT retried


# ---- Purchase-order internal tier (PO S4) ---------------------------------


def test_get_pending_pos_returns_rows_and_sends_bearer_and_limit(mocker):
    rows = [{"id": 7, "po_number": "2026.001.2.0.0", "hmac": "abc", "line_items": []}]
    req = _patch_requests(mocker, _mock_response(json_body={"pending": rows}))
    out = portal_client.get_pending_pos(BASE, TOKEN, limit=10)
    assert out == rows
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/po/internal/pending")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"
    assert kwargs["params"] == {"limit": 10}


def test_get_pending_pos_drops_non_dict_rows_and_raises_on_bad_shape(mocker):
    _patch_requests(
        mocker, _mock_response(json_body={"pending": [{"id": 1}, "junk", 3]})
    )
    assert portal_client.get_pending_pos(BASE, TOKEN) == [{"id": 1}]
    _patch_requests(mocker, _mock_response(json_body={"pending": "nope"}))
    with pytest.raises(PortalTransportError, match="pending"):
        portal_client.get_pending_pos(BASE, TOKEN)


def test_mark_po_filed_posts_receipt_and_returns_found(mocker):
    req = _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": True}))
    assert portal_client.mark_po_filed(BASE, TOKEN, po_id=7, box_file_id="f-9") is True
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/po/internal/mark-filed")
    assert kwargs["json"] == {"po_id": 7, "box_file_id": "f-9"}


def test_mark_po_filed_replay_returns_found_false(mocker):
    _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": False}))
    assert portal_client.mark_po_filed(BASE, TOKEN, po_id=7, box_file_id=None) is False


def test_po_status_sync_posts_ordered_updates(mocker):
    req = _patch_requests(mocker, _mock_response(json_body={"ok": True, "updated": 2}))
    updates = [{"po_id": 7, "status": "approved"}, {"po_id": 7, "status": "sent"}]
    out = portal_client.po_status_sync(BASE, TOKEN, updates)
    assert out == {"ok": True, "updated": 2}
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/po/internal/status-sync")
    assert kwargs["json"] == {"updates": updates}  # order preserved verbatim


def test_vendors_sync_posts_vendor_array(mocker):
    req = _patch_requests(
        mocker, _mock_response(json_body={"ok": True, "upserted": 1, "skipped_dirty": 0})
    )
    payload = [{"vendor_key": "VEN-000001", "vendor_name": "Chint", "active": 1}]
    out = portal_client.vendors_sync(BASE, TOKEN, payload)
    assert out["upserted"] == 1
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/po/internal/vendors/sync")
    assert kwargs["json"] == {"vendors": payload}


def test_get_pending_vendors_returns_rows_and_raises_on_bad_shape(mocker):
    rows = [{"vendor_key": "VEN-000002", "mirror_version": 3}]
    req = _patch_requests(mocker, _mock_response(json_body={"vendors": rows}))
    assert portal_client.get_pending_vendors(BASE, TOKEN) == rows
    args, _ = req.call_args
    assert args == ("GET", "https://portal.example.com/api/po/internal/vendors/pending")
    _patch_requests(mocker, _mock_response(json_body={"vendors": None}))
    with pytest.raises(PortalTransportError, match="vendors"):
        portal_client.get_pending_vendors(BASE, TOKEN)


def test_mark_vendors_mirrored_posts_watermarked_updates(mocker):
    req = _patch_requests(
        mocker, _mock_response(json_body={"ok": True, "flipped": 1, "stale": 0})
    )
    updates = [{"vendor_key": "VEN-000002", "mirrored_version": 3}]
    out = portal_client.mark_vendors_mirrored(BASE, TOKEN, updates)
    assert out["flipped"] == 1
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/po/internal/vendors/mark-mirrored")
    assert kwargs["json"] == {"updates": updates}


def test_po_tier_401_raises_auth_without_retry(mocker):
    """The PO bearer tier propagates a 401 as PortalAuthError with NO retry — the
    daemon's cycle-stop signal."""
    req = _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_pending_pos(BASE, TOKEN)
    assert req.call_count == 1


# ---- Config-editor queue (§50 — get/claim/stamp/stuck) -------------------


def test_get_config_pending_returns_rows_and_sends_bearer_and_limit(mocker):
    rows = [
        {"id": 1, "workstream": "po_materials", "artifact_key": "purchaser",
         "op": "edit", "target_version": None, "payload": "{}", "created_at": 1},
    ]
    req = _patch_requests(mocker, _mock_response(json_body={"pending": rows}))

    out = portal_client.get_config_pending(BASE, TOKEN, limit=25)

    assert out == rows
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/internal/config/pending")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"
    assert kwargs["params"] == {"limit": 25}


def test_get_config_pending_drops_non_dict_rows(mocker):
    _patch_requests(
        mocker,
        _mock_response(json_body={"pending": [{"id": 1}, "garbage", 5]}),
    )
    assert portal_client.get_config_pending(BASE, TOKEN) == [{"id": 1}]


def test_get_config_pending_missing_key_raises(mocker):
    _patch_requests(mocker, _mock_response(json_body={"unexpected": 1}))
    with pytest.raises(PortalTransportError, match="pending"):
        portal_client.get_config_pending(BASE, TOKEN)


def test_get_config_pending_401_raises_auth_error_without_retry(mocker):
    req = _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_config_pending(BASE, TOKEN)
    assert req.call_count == 1  # 401 is NOT retried


def test_claim_config_returns_row_when_claimed(mocker):
    row = {"id": 7, "workstream": "po_materials", "artifact_key": "tax",
           "op": "edit", "payload": "{}", "status": "queued"}
    req = _patch_requests(
        mocker, _mock_response(json_body={"ok": True, "claimed": True, "request": row})
    )

    out = portal_client.claim_config(BASE, TOKEN, request_id=7, lease_owner="mac1")

    assert out == row
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/internal/config/claim")
    assert kwargs["json"] == {"id": 7, "lease_owner": "mac1"}


def test_claim_config_returns_none_when_not_claimed(mocker):
    _patch_requests(mocker, _mock_response(json_body={"ok": True, "claimed": False}))
    assert portal_client.claim_config(BASE, TOKEN, request_id=7, lease_owner="mac1") is None


def test_claim_config_returns_none_when_request_is_non_dict(mocker):
    _patch_requests(mocker, _mock_response(json_body={"ok": True, "claimed": True, "request": "oops"}))
    assert portal_client.claim_config(BASE, TOKEN, request_id=7, lease_owner="mac1") is None


def test_stamp_config_posts_body_and_returns_found(mocker):
    req = _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": True}))

    found = portal_client.stamp_config(BASE, TOKEN, request_id=7, status="validated")

    assert found is True
    args, kwargs = req.call_args
    assert args == ("POST", "https://portal.example.com/api/internal/config/stamp")
    assert kwargs["json"] == {"id": 7, "status": "validated"}


def test_stamp_config_carries_failed_fields_only_when_given(mocker):
    req = _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": True}))
    portal_client.stamp_config(
        BASE, TOKEN, request_id=7, status="failed",
        failed_stage="deploy", failure_reason="wrangler boom",
    )
    assert req.call_args.kwargs["json"] == {
        "id": 7,
        "status": "failed",
        "failed_stage": "deploy",
        "failure_reason": "wrangler boom",
    }


def test_stamp_config_found_false_when_worker_rejects(mocker):
    _patch_requests(mocker, _mock_response(json_body={"ok": True, "found": False}))
    assert portal_client.stamp_config(BASE, TOKEN, request_id=7, status="live") is False


def test_get_config_stuck_returns_rows_and_sends_older_than(mocker):
    rows = [{"id": 3, "status": "validated", "workstream": "po_materials", "artifact_key": "terms"}]
    req = _patch_requests(mocker, _mock_response(json_body={"stuck": rows}))

    out = portal_client.get_config_stuck(BASE, TOKEN, older_than=2700)

    assert out == rows
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/internal/config/stuck")
    assert kwargs["params"] == {"older_than": 2700}


def test_get_config_stuck_missing_key_raises(mocker):
    _patch_requests(mocker, _mock_response(json_body={"unexpected": 1}))
    with pytest.raises(PortalTransportError, match="stuck"):
        portal_client.get_config_stuck(BASE, TOKEN, older_than=2700)


# ---- get_estimates_pending (the estimates-key wire contract, 2026-07-20) ---------


def test_get_estimates_pending_reads_the_estimates_key(mocker):
    """The estimate tier's pending route serves `{estimates: [...]}` — the ONE
    internal pending route whose key differs from `pending`. The client shipped
    expecting `pending`; the drift was invisible to mocks on both sides and
    surfaced live as every upload stuck at `pending` (629 failed cycles). This
    test pins the CLIENT side to the deployed Worker's real shape."""
    rows = [{"id": 1, "est_uuid": "u-1", "filename": "q.xlsx", "hmac": "h", "status": "pending"}]
    req = _patch_requests(mocker, _mock_response(json_body={"estimates": rows}))

    out = portal_client.get_estimates_pending(BASE, TOKEN, limit=10)

    assert out == rows
    args, kwargs = req.call_args
    assert args == ("GET", "https://portal.example.com/api/po/estimates/internal/pending")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer"


def test_get_estimates_pending_rejects_a_pending_keyed_body(mocker):
    """RED for the exact live failure: a `pending`-keyed body (the pre-fix client's
    own expectation) is malformed transport for THIS route — loud, never silent."""
    _patch_requests(mocker, _mock_response(json_body={"pending": []}))
    with pytest.raises(portal_client.PortalTransportError, match="'estimates' array"):
        portal_client.get_estimates_pending(BASE, TOKEN)


def test_estimates_pending_wire_key_parity():
    """CROSS-RUNTIME PIN: the Worker route and this client must agree on the
    response key. Reads BOTH sources so neither side can drift alone again —
    the failure class here (each side's mocks matching its own assumption) is
    exactly what unit tests structurally cannot catch without this pin."""
    import inspect
    import re
    from pathlib import Path

    worker_src = (
        Path(__file__).resolve().parent.parent
        / "safety_portal" / "worker" / "po_estimates.ts"
    ).read_text(encoding="utf-8")
    # The pending route's success body in the Worker source.
    route_block = worker_src.split("/api/po/estimates/internal/pending", 1)[1][:1200]
    m = re.search(r"c\.json\(\{\s*(\w+):", route_block)
    assert m is not None, "pending route success body not found in po_estimates.ts"
    worker_key = m.group(1)

    client_src = inspect.getsource(portal_client.get_estimates_pending)
    assert f'data.get("{worker_key}")' in client_src, (
        f"wire-key drift: worker serves {worker_key!r} but "
        f"get_estimates_pending does not read it"
    )


def test_manifests_pending_wire_key_parity():
    """CROSS-RUNTIME PIN for the materials-manifest lane: the Worker route and this
    client must agree on the response key. Reads BOTH sources so neither side can drift
    alone — the failure class here (each side's mocks matching its own assumption) is
    exactly what unit tests structurally cannot catch without this pin, and it is not
    hypothetical: `get_estimates_pending` once shipped reading "pending" while the
    deployed Worker served `{ estimates: … }`, and 629 cycles failed invisibly."""
    import inspect
    import re
    from pathlib import Path

    worker_src = (
        Path(__file__).resolve().parent.parent
        / "safety_portal" / "worker" / "fieldops_manifests.ts"
    ).read_text(encoding="utf-8")
    # The pending route's success body in the Worker source. The window is measured
    # from the route path's FIRST occurrence (its doc comment), so it must span the
    # comment + handler up to `c.json(` — 2000 covers the 2026-08-11 project_name
    # join commentary with room to grow.
    route_block = worker_src.split("/api/fieldops/manifests/internal/pending", 1)[1][:2000]
    m = re.search(r"c\.json\(\{\s*(\w+):", route_block)
    assert m is not None, "pending route success body not found in fieldops_manifests.ts"
    worker_key = m.group(1)

    client_src = inspect.getsource(portal_client.get_manifests_pending)
    assert f'data.get("{worker_key}")' in client_src, (
        f"wire-key drift: worker serves {worker_key!r} but "
        f"get_manifests_pending does not read it"
    )


def test_manifest_internal_paths_match_the_worker_routes():
    """Every manifest path constant must name a route the Worker actually registers.
    A path typo is the same silent class as a wire-key drift: the daemon 404s forever
    and both sides' mocks agree with themselves."""
    from pathlib import Path

    worker_src = (
        Path(__file__).resolve().parent.parent
        / "safety_portal" / "worker" / "fieldops_manifests.ts"
    ).read_text(encoding="utf-8")
    for const in (
        portal_client.MANIFEST_PENDING_PATH,
        portal_client.MANIFEST_CLAIM_PATH,
        portal_client.MANIFEST_CHUNKS_PATH,
        portal_client.MANIFEST_ROWS_PATH,
        portal_client.MANIFEST_PREVIEW_PATH,
        portal_client.MANIFEST_RESULT_PATH,
    ):
        # The client writes `{id}` where Hono writes `:id`.
        route = const.replace("{id}", ":id")
        assert f'"{route}"' in worker_src, f"no Worker route registered for {route}"


# ---- get_fieldops_archive_health (#25) -----------------------------------


def test_archive_health_returns_rows_and_sends_the_bearer(mocker):
    rows = [{
        "job_id": "JOB-000030", "project_name": "Production test", "job_no": "30",
        "archive_folder_key": "Production test", "archive_direction": "archive",
        "archive_state": "partial", "archive_attempts": 1, "archive_requested_at": 1786144888,
    }]
    req = _patch_requests(mocker, _mock_response(json_body={"jobs": rows}))

    out = portal_client.get_fieldops_archive_health(BASE, TOKEN)

    assert out == rows
    assert req.call_args.args[0] == "GET"
    assert req.call_args.args[1].endswith(portal_client.FIELDOPS_ARCHIVE_HEALTH_PATH)
    assert req.call_args.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_archive_health_drops_non_dict_rows(mocker):
    _patch_requests(mocker, _mock_response(json_body={"jobs": [{"job_id": "A"}, "junk", None]}))
    assert portal_client.get_fieldops_archive_health(BASE, TOKEN) == [{"job_id": "A"}]


def test_archive_health_missing_jobs_array_is_a_transport_error(mocker):
    """A shape change must fail LOUD, not read as 'no stalled archives'."""
    _patch_requests(mocker, _mock_response(json_body={"unexpected": 1}))
    with pytest.raises(PortalTransportError):
        portal_client.get_fieldops_archive_health(BASE, TOKEN)


def test_archive_health_401_raises_auth_error(mocker):
    _patch_requests(mocker, _mock_response(status=401))
    with pytest.raises(PortalAuthError):
        portal_client.get_fieldops_archive_health(BASE, TOKEN)


def _worker_index_src():
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "safety_portal" / "worker" / "index.ts"
    ).read_text(encoding="utf-8")


def test_archive_health_path_matches_a_real_worker_route():
    """Same wire-drift class the manifest tests guard: a path typo 404s forever while
    both sides' mocks agree with themselves."""
    assert f'"{portal_client.FIELDOPS_ARCHIVE_HEALTH_PATH}"' in _worker_index_src()


def test_archive_health_route_is_read_only_and_sees_the_terminal_states():
    """The two properties Check X depends on, asserted against the REAL Worker source.

    If archive-health ever narrows to the queue's state set it becomes a duplicate of
    archive-pending and Check X goes blind to `partial`/`failed` — the exact gap #25
    exists to close. And a health probe that mutated would be writing on the watchdog's
    schedule, on rows a human may be acting on.
    """
    src = _worker_index_src()
    start = src.index(f'app.get("{portal_client.FIELDOPS_ARCHIVE_HEALTH_PATH}"')
    handler = src[start:src.index("});", start)]

    for state in ("'requested'", "'in_progress'", "'partial'", "'failed'"):
        assert state in handler, f"archive-health no longer serves {state}"
    assert "requireFieldopsToken" in handler, "archive-health lost its bearer gate"
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DB.batch"):
        assert verb not in handler, (
            f"archive-health became a MUTATING route ({verb.strip()}) — it is a read-only probe"
        )
