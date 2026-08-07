"""Safety Portal internal-transport client — the Mac-side HTTP leg of the pull model.

Purpose
-------
    Thin, audited HTTP transport for the two internal Worker endpoints the
    `safety_reports/portal_poll.py` daemon drives (decision_phase5-portal-transport):

      GET  /api/internal/pending     → the queue drain (box_verified=0, oldest-first)
      POST /api/internal/mark-filed  → the receipt (flips box_verified=1)

    The Cloudflare Worker (`safety_portal/worker/index.ts`) signs + queues each
    submission send-free in D1; this module is the ONLY Python egress to that
    Worker. Keeping the HTTP here (not inline in `portal_poll`) is what lets the
    daemon import a network capability *through an audited shared/*_client.py*
    rather than acquiring `requests` itself — see the F02 NETWORK_LIB_ALLOWLIST
    note in `tests/test_capability_gating.py`. The puller therefore stays inside
    the capability gate; the Worker (TS) was outside it.

Trust boundary
--------------
    This module is TRANSPORT ONLY. It does NOT verify the per-row HMAC — that is
    the caller's job (`portal_poll` recomputes via `shared.portal_hmac` and
    constant-time-compares each pulled row's `hmac` field BEFORE handing it to
    intake). A row returned by `get_pending` is UNTRUSTED until the caller
    verifies it. `mark_filed` is a control-plane receipt to our own Worker, NOT a
    customer-facing send — it is outside the External Send Gate (Invariant 1).

Credentials
-----------
    `base_url` (the Worker origin) and `token` (the bearer) are passed IN by the
    caller — this module reads no Keychain / ITS_Config, so it stays trivially
    testable and the fail-closed credential check lives in one place
    (`portal_poll`). The bearer mirrors the Worker's `PORTAL_INTERNAL_API_TOKEN`;
    on the Mac it is Keychain `ITS_PORTAL_INTERNAL_TOKEN` (resolved by the caller).

Failure modes
-------------
    Every failure raises a typed exception under `PortalTransportError`; this
    module never swallows. A 401 is `PortalAuthError` (bad/missing bearer). 429
    and 503 are retried (cap `MAX_RETRIES`, Retry-After honored) then surface as
    `PortalTransportError`. The caller logs + skips the cycle (the submission
    stays box_verified=0 and re-pulls next cycle — no silent loss).
"""
from __future__ import annotations

import time
from typing import Any, NamedTuple

import requests  # type: ignore[import-untyped]

# Network timeouts (connect, read) in seconds. A hung Worker must not wedge the
# 60 s-cadence daemon — fail fast and let the next cycle retry.
TIMEOUT = (10.0, 30.0)
MAX_RETRIES = 3

PENDING_PATH = "/api/internal/pending"
MARK_FILED_PATH = "/api/internal/mark-filed"
MARK_REJECTED_PATH = "/api/internal/mark-rejected"
SYNC_PATH = "/api/internal/sync"
PDF_REQUESTS_PATH = "/api/internal/pdf-requests"
FILED_PDF_PATH = "/api/internal/filed-pdf"
ITEM_PHOTOS_PENDING_PATH = "/api/internal/item-photos/pending"
ITEM_PHOTO_RESULT_PATH_TEMPLATE = "/api/internal/item-photos/{photo_id}/result"
DAILY_PHOTOS_PENDING_PATH = "/api/internal/daily-photos/pending"
DAILY_PHOTO_RESULT_PATH_TEMPLATE = "/api/internal/daily-photos/{photo_id}/result"
PUBLISH_PENDING_PATH = "/api/internal/publish/pending"
PUBLISH_CLAIM_PATH = "/api/internal/publish/claim"
PUBLISH_STAMP_PATH = "/api/internal/publish/stamp"
PUBLISH_STUCK_PATH = "/api/internal/publish/stuck"
CONFIG_PENDING_PATH = "/api/internal/config/pending"
CONFIG_CLAIM_PATH = "/api/internal/config/claim"
CONFIG_STAMP_PATH = "/api/internal/config/stamp"
CONFIG_STUCK_PATH = "/api/internal/config/stuck"
FIELDOPS_PENDING_JOBS_PATH = "/api/internal/fieldops/pending-jobs"
FIELDOPS_JOBS_MARK_MIRRORED_PATH = "/api/internal/fieldops/jobs-mark-mirrored"
FIELDOPS_HOURS_PENDING_PATH = "/api/internal/fieldops/hours-pending"
FIELDOPS_ARCHIVE_PENDING_PATH = "/api/internal/fieldops/archive-pending"
FIELDOPS_ARCHIVE_PROGRESS_PATH = "/api/internal/fieldops/job-archive-progress"
FIELDOPS_HOURS_MARK_MIRRORED_PATH = "/api/internal/fieldops/hours-mark-mirrored"
FIELDOPS_EQUIPMENT_SNAPSHOT_PATH = "/api/internal/fieldops/equipment-snapshot"
FIELDOPS_MATERIAL_LIST_SNAPSHOT_PATH = "/api/internal/fieldops/material-list-snapshot"
FIELDOPS_MATERIAL_INCIDENTS_PATH = "/api/internal/fieldops/material-incidents"
PROGRESS_ROLLUP_PATH = "/api/internal/progress-rollup"
PRUNE_STATUS_PATH = "/api/internal/prune-status"
PO_PENDING_PATH = "/api/po/internal/pending"
PO_MARK_FILED_PATH = "/api/po/internal/mark-filed"
PO_STATUS_SYNC_PATH = "/api/po/internal/status-sync"
PO_VENDORS_SYNC_PATH = "/api/po/internal/vendors/sync"
PO_VENDORS_PENDING_PATH = "/api/po/internal/vendors/pending"
PO_VENDORS_MARK_MIRRORED_PATH = "/api/po/internal/vendors/mark-mirrored"
PO_ATTACHMENTS_PENDING_PATH = "/api/po/internal/attachments/pending"
PO_ATTACHMENT_CLAIM_PATH = "/api/po/internal/attachments/{id}/claim"
PO_ATTACHMENT_CHUNKS_PATH = "/api/po/internal/attachments/{id}/chunks"
PO_ATTACHMENT_RESULT_PATH = "/api/po/internal/attachments/{id}/result"
EST_PENDING_PATH = "/api/po/estimates/internal/pending"
EST_CLAIM_PATH = "/api/po/estimates/internal/{id}/claim"
EST_CHUNKS_PATH = "/api/po/estimates/internal/{id}/chunks"
EST_RESULT_PATH = "/api/po/estimates/internal/result"
EST_PREVIEW_PATH = "/api/po/estimates/internal/{id}/preview"
RFQ_PENDING_PATH = "/api/po/rfqs/internal/pending"
RFQ_MARK_FILED_PATH = "/api/po/rfqs/internal/mark-filed"
RFQ_STATUS_SYNC_PATH = "/api/po/rfqs/internal/status-sync"
SUB_PENDING_PATH = "/api/subcontracts/internal/pending"
SUB_MARK_FILED_PATH = "/api/subcontracts/internal/mark-filed"
SUB_STATUS_SYNC_PATH = "/api/subcontracts/internal/status-sync"
SUB_SUBCONTRACTORS_SYNC_PATH = "/api/subcontracts/internal/subcontractors/sync"
SUB_SUBCONTRACTORS_PENDING_PATH = "/api/subcontracts/internal/subcontractors/pending"
SUB_SUBCONTRACTORS_MARK_MIRRORED_PATH = "/api/subcontracts/internal/subcontractors/mark-mirrored"
MANIFEST_PENDING_PATH = "/api/fieldops/manifests/internal/pending"
MANIFEST_CLAIM_PATH = "/api/fieldops/manifests/internal/{id}/claim"
MANIFEST_CHUNKS_PATH = "/api/fieldops/manifests/internal/{id}/chunks"
MANIFEST_ROWS_PATH = "/api/fieldops/manifests/internal/{id}/rows"
MANIFEST_PREVIEW_PATH = "/api/fieldops/manifests/internal/{id}/preview"
MANIFEST_RESULT_PATH = "/api/fieldops/manifests/internal/result"


# ---- Typed exceptions ----------------------------------------------------


class PortalTransportError(Exception):
    """Base exception for all portal-transport failures."""


class PortalAuthError(PortalTransportError):
    """Bearer token rejected (HTTP 401) — bad/missing/rotated token."""


class PortalRateLimitError(PortalTransportError):
    """HTTP 429/503 after the retry budget was exhausted."""


# ---- Internals -----------------------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After as seconds. None on unparseable / HTTP-date form."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _request(
    method: str,
    base_url: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue one authenticated request with retry on 429/503; return parsed JSON.

    Retries transient 429/503 (Retry-After honored, exponential backoff
    fallback) and connection errors up to `MAX_RETRIES`. Translates the final
    outcome to the typed hierarchy. A 401 is NOT retried (the token is bad).
    """
    url = base_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    last_detail = ""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.request(
                method, url, params=params, json=json_body,
                headers=headers, timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            # Network-layer failure (DNS / connect / read timeout). Retry a
            # bounded number of times, then surface.
            last_detail = f"{type(exc).__name__}: {exc}"
            if attempt == MAX_RETRIES - 1:
                raise PortalTransportError(
                    f"{method} {path} network failure after {MAX_RETRIES} attempts: {last_detail}"
                ) from exc
            time.sleep(float(2**attempt))
            continue

        if response.status_code == 401:
            raise PortalAuthError(
                f"{method} {path} unauthorized (401) — bearer token rejected"
            )
        if response.status_code in (429, 503):
            last_detail = f"HTTP {response.status_code}"
            if attempt == MAX_RETRIES - 1:
                raise PortalRateLimitError(
                    f"{method} {path} throttled/unavailable after {MAX_RETRIES} attempts ({last_detail})"
                )
            delay = _parse_retry_after(response.headers.get("Retry-After"))
            time.sleep(delay if delay is not None else float(2**attempt))
            continue
        if response.status_code != 200:
            raise PortalTransportError(
                f"{method} {path} unexpected status {response.status_code}: "
                f"{response.text[:300]!r}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise PortalTransportError(
                f"{method} {path} returned non-JSON body: {response.text[:300]!r}"
            ) from exc
        if not isinstance(data, dict):
            # Type name only — a hostile/broken Worker could return a huge JSON
            # value, and repr()-ing it into the exception would be an unbounded
            # allocation in the daemon. (Same posture as the text[:300] truncation.)
            raise PortalTransportError(
                f"{method} {path} returned non-object JSON (got {type(data).__name__})"
            )
        return data
    # Unreachable: every loop branch either returns or raises on the last attempt.
    raise PortalTransportError(f"{method} {path} exhausted retries: {last_detail}")


# ---- Public API ----------------------------------------------------------


def get_pending(base_url: str, token: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Drain the pending queue: GET /api/internal/pending (oldest-first).

    Returns the `pending` list verbatim — each row a dict with
    `submission_uuid, job_id, form_code, work_date, payload_json, amends_uuid,
    hmac, created_at`. The Worker caps `limit` at 200. Rows are UNTRUSTED until
    the caller verifies each row's `hmac` (see module docstring trust boundary).

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure).
    """
    data = _request("GET", base_url, PENDING_PATH, token, params={"limit": limit})
    pending = data.get("pending")
    if not isinstance(pending, list):
        raise PortalTransportError(
            f"GET {PENDING_PATH} missing/invalid 'pending' array (got {type(pending).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in pending if isinstance(row, dict)]


def mark_filed(
    base_url: str, token: str, *, submission_uuid: str, box_link: str,
    box_file_id: str | None = None,
) -> bool:
    """Post the receipt: POST /api/internal/mark-filed → returns `found`.

    Called ONLY after intake has filed the submission to Box + Smartsheet
    (box_verified flips to 1 so the Worker stops serving the row). Idempotent —
    a second call for an already-filed UUID returns `found=True` with no effect.
    `found=False` means the Worker has no row for that UUID (already drained by a
    concurrent actor, or an unknown UUID); the caller treats it as benign.

    `box_file_id` (PR-4 Part A) is the filed Box file id — the Worker stores it so
    the request-driven PDF-cache servicing pass (get_pdf_requests → download from
    Box → upload_filed_pdf) knows which Box file to fetch. `None` leaves the
    Worker's box_file_id column untouched on that path (it sends `box_file_id: null`).

    Raises `PortalAuthError` / `PortalRateLimitError` / `PortalTransportError`.
    """
    data = _request(
        "POST", base_url, MARK_FILED_PATH, token,
        json_body={
            "submission_uuid": submission_uuid,
            "box_link": box_link,
            "box_file_id": box_file_id,
        },
    )
    return bool(data.get("found"))


def mark_rejected(base_url: str, token: str, *, submission_uuid: str, reason: str) -> bool:
    """Post the terminal-reject receipt (M4): POST /api/internal/mark-rejected → returns `found`.

    Called by portal_poll after an HMAC failure so the bad row is flipped box_verified=-1 and
    stops being re-served by /pending every cycle. Control-plane write to OUR OWN Worker (outside
    the External Send Gate, like mark_filed). Idempotent. Raises the typed PortalTransportError
    hierarchy."""
    data = _request(
        "POST", base_url, MARK_REJECTED_PATH, token,
        json_body={"submission_uuid": submission_uuid, "reason": reason},
    )
    return bool(data.get("found"))


def push_jobs(base_url: str, token: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Full-replace job sync: POST /api/internal/sync → {ok, upserted, deactivated}.

    `jobs` is the COMPLETE ITS_Active_Jobs set, each row
    `{job_id, project_name, active, address}` (active 1/0). The Worker upserts each and
    deactivates any job_id absent from the set — so this is a full-replace sync,
    NOT an incremental add. The caller MUST refuse to push an empty list (an empty
    set would deactivate the whole dropdown); the Worker also rejects it (400
    empty_jobs). Idempotent: re-pushing the same set is a no-op, so a missed cycle
    self-heals.

    Like `mark_filed`, this is a control-plane receipt/write to our OWN Worker
    (D1 dropdown cache), NOT a customer-facing send — it is outside the External
    Send Gate (Invariant 1).

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure).
    """
    return _request("POST", base_url, SYNC_PATH, token, json_body={"jobs": jobs})


# ---- Field-Ops job up-sync (P2.5 Slice 5 — the portal-as-writer mirror I/O) ----


def get_fieldops_pending_jobs(base_url: str, token: str) -> list[dict[str, Any]]:
    """Pull dirty portal jobs to mirror UP: GET /api/internal/fieldops/pending-jobs.

    Returns the `jobs` list verbatim — each a dict with the full SoR payload + version
    vector the `field_ops.fieldops_sync` daemon needs to find-or-create a row in BOTH
    Active-Jobs sheets: `job_id, project_name, lifecycle, address, stakeholder_name/email/
    phone, safety_contact_name/email, safety_cc (list), progress_contact_name/email,
    progress_cc (list), mirror_version, safety_mirrored_version, progress_mirrored_version,
    safety_row_id, progress_row_id, canonical_job_id`. The Worker caps the page at 200 rows
    server-side (no client limit param); the daemon drains across cycles.

    A control-plane read of OUR OWN Worker (bearer = the SEPARATE field-ops token
    `PORTAL_FIELDOPS_API_TOKEN`; privilege-separated from the poller's internal token), NOT a
    customer-facing send. Same typed-error contract as `get_pending` — `PortalAuthError`
    (401) / `PortalRateLimitError` (429/503 exhausted) / `PortalTransportError` (any other,
    incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, FIELDOPS_PENDING_JOBS_PATH, token)
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise PortalTransportError(
            f"GET {FIELDOPS_PENDING_JOBS_PATH} missing/invalid 'jobs' array "
            f"(got {type(jobs).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in jobs if isinstance(row, dict)]


def mark_fieldops_jobs_mirrored(
    base_url: str, token: str, updates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per-sheet mirror commit point: POST /api/internal/fieldops/jobs-mark-mirrored.

    `updates` is a non-empty list of `{job_id, sheet: 'safety'|'progress', mirrored_version,
    row_id, canonical_job_id?}`. The Worker MONOTONICALLY advances only that sheet's
    watermark (MAX), caches the row_id, writes back `canonical_job_id` (SAFETY sheet only),
    and flips `sync_state` to `synced` once BOTH watermarks reach `mirror_version`. The
    daemon calls this ONCE PER SHEET (after that sheet's upsert confirms) so a progress
    failure leaves the job dirty with only the safety watermark advanced — the version-vector
    self-heal. The Worker rejects an EMPTY list (400) — the caller must never send one.

    Like `push_jobs`/`mark_filed`, a control-plane write to OUR OWN Worker (outside the
    External Send Gate, Invariant 1). Returns the Worker's `{ok, updated}` dict. Raises the
    typed `PortalTransportError` hierarchy on failure (a 400 invalid/empty body surfaces as
    `PortalTransportError`, never a silent return).
    """
    return _request(
        "POST", base_url, FIELDOPS_JOBS_MARK_MIRRORED_PATH, token,
        json_body={"updates": updates},
    )


def get_fieldops_pending_hours(base_url: str, token: str) -> list[dict[str, Any]]:
    """Pull unmirrored crew time entries to mirror UP: GET /api/internal/fieldops/hours-pending.

    Returns the `entries` list verbatim — each a dict with `uuid, job_id, project_name,
    hours, notes, task, amends_uuid, created_at, personnel_name` (`personnel_name` is the DISPLAY
    name, never a username; `task` is task_assignments.description via time_entries.task_id, empty
    when the entry references no task — it replaced the always-empty work_started_at/_ended_at
    wall-clock fields, 2026-07-05). The Worker caps the page at 200 server-side (no client limit
    param); the daemon's hours pass drains across cycles.

    A control-plane read of OUR OWN Worker (bearer = the SEPARATE field-ops token
    `PORTAL_FIELDOPS_API_TOKEN`, same as `get_fieldops_pending_jobs`), NOT a customer send. Same
    typed-error contract: `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, FIELDOPS_HOURS_PENDING_PATH, token)
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise PortalTransportError(
            f"GET {FIELDOPS_HOURS_PENDING_PATH} missing/invalid 'entries' array "
            f"(got {type(entries).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in entries if isinstance(row, dict)]


def mark_fieldops_hours_mirrored(
    base_url: str, token: str, uuids: list[str]
) -> dict[str, Any]:
    """Hours-pass commit point: POST /api/internal/fieldops/hours-mark-mirrored.

    `uuids` is a non-empty list of the `time_entries.uuid`s whose per-job Hours Log row the daemon
    confirmed this cycle. The Worker stamps `mirrored_at = unixepoch()` for each IFF still NULL
    (idempotent — a replay/re-mirror is a no-op, never a regress) in one atomic batch + a summary
    audit row. The Worker rejects an EMPTY list (400) — the caller must never send one.

    Like `mark_fieldops_jobs_mirrored`, a control-plane write to OUR OWN Worker (outside the
    External Send Gate, Invariant 1). Returns the Worker's `{ok, updated}` dict; raises the typed
    `PortalTransportError` hierarchy on failure.
    """
    return _request(
        "POST", base_url, FIELDOPS_HOURS_MARK_MIRRORED_PATH, token,
        json_body={"uuids": uuids},
    )


def get_fieldops_pending_archives(base_url: str, token: str) -> list[dict[str, Any]]:
    """Pull jobs awaiting relocation: GET /api/internal/fieldops/archive-pending.

    Returns the `jobs` list verbatim — each a dict with `job_id, project_name, job_no,
    archive_folder_key, archive_direction, archive_state, archive_attempts,
    archive_requested_at`.

    Resolve containers against `archive_folder_key`, NEVER against `project_name`. The key is
    snapshotted when the operator raises the request; `project_name` is editable afterwards
    (`POST /api/fieldops/job/:id/contacts`), and every per-job folder was created under the key's
    value. Using the live name would send the pass after a folder that does not exist.

    The Worker caps the page at 25 server-side — far below the other queues' 200, because each row
    costs six external API sequences across two systems. The pass drains across cycles.

    A control-plane read of OUR OWN Worker (bearer = `PORTAL_FIELDOPS_API_TOKEN`, the same token
    as the job and hours queues), NOT a customer send. Typed-error contract as elsewhere:
    `PortalAuthError` (401) / `PortalRateLimitError` / `PortalTransportError`.
    """
    data = _request("GET", base_url, FIELDOPS_ARCHIVE_PENDING_PATH, token)
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise PortalTransportError(
            f"GET {FIELDOPS_ARCHIVE_PENDING_PATH} missing/invalid 'jobs' array "
            f"(got {type(jobs).__name__})"
        )
    return [row for row in jobs if isinstance(row, dict)]


def post_fieldops_archive_progress(
    base_url: str, token: str, updates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Archive-pass commit point: POST /api/internal/fieldops/job-archive-progress.

    Each update is `{job_id, direction, state, containers?}` where `state` is one of
    in_progress | complete | partial | failed. `requested` is REFUSED by the Worker — the pass may
    ADVANCE a request, only a human may raise one.

    The Worker's UPDATE is forward-only (`archive_state IN ('requested','in_progress') AND
    archive_direction = ?`), so a late or replayed post cannot resurrect a completed archive nor
    apply an archive result to a job the operator has since flipped to un-archive. Rows that no
    longer match come back in `skipped` rather than failing the batch — one stale member must not
    discard the rest. **Callers should treat a job appearing in `skipped` as "the operator moved
    this out from under me", not as an error to retry.**

    Empty lists are rejected (400) — never send one. Cap is 25, matching the queue page.

    A control-plane write to OUR OWN Worker (outside the External Send Gate, Invariant 1). Returns
    the Worker's `{ok, updated, skipped}` dict.
    """
    return _request(
        "POST", base_url, FIELDOPS_ARCHIVE_PROGRESS_PATH, token,
        json_body={"updates": updates},
    )


class FieldopsEquipmentSnapshot(NamedTuple):
    """The two arrays the equipment-snapshot route returns.

    `equipment` — the CURRENT on-active-job equipment (one row per equipment whose latest
    location sits on an active job). `jobs_with_equipment` — the RECONCILE ROSTER: every active
    job that has ANY equipment_location history (regardless of current count). The daemon iterates
    the roster so a job whose current equipment dropped to ZERO is still revisited and its stale
    `On Job=Active` rows retired (the count-drops-to-zero silent gap).
    """

    equipment: list[dict[str, Any]]
    jobs_with_equipment: list[dict[str, Any]]


def get_fieldops_equipment_snapshot(base_url: str, token: str) -> FieldopsEquipmentSnapshot:
    """Pull the CURRENT on-active-job equipment snapshot + reconcile roster: GET
    /api/internal/fieldops/equipment-snapshot (P7 Slice 2, Equipment Status & Location).

    Returns a `FieldopsEquipmentSnapshot(equipment, jobs_with_equipment)`:
      - `equipment` — dicts with `equipment_id, job_id, project_name, name, kind, identifier,
        status, status_note, status_changed_at, location_label, lat, lon, read_at, recorded_at`.
        A SNAPSHOT (the live on-active-job state re-projected every cycle), NOT an event drain:
        no watermark, no mark-mirrored companion. The Worker returns the complete set (uncapped —
        the daemon needs the full snapshot to compute retire-off-job).
      - `jobs_with_equipment` — dicts `{job_id, project_name}` for every active job with ANY
        equipment_location history. The reconcile set the daemon iterates so a job whose current
        equipment dropped to zero still retires its stale sheet rows.

    A control-plane read of OUR OWN Worker (bearer = the SEPARATE field-ops token
    `PORTAL_FIELDOPS_API_TOKEN`, same as `get_fieldops_pending_hours`), NOT a customer send.
    Same typed-error contract: `PortalAuthError` (401) / `PortalRateLimitError` (429/503
    exhausted) / `PortalTransportError` (any other, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, FIELDOPS_EQUIPMENT_SNAPSHOT_PATH, token)
    equipment = data.get("equipment")
    if not isinstance(equipment, list):
        raise PortalTransportError(
            f"GET {FIELDOPS_EQUIPMENT_SNAPSHOT_PATH} missing/invalid 'equipment' array "
            f"(got {type(equipment).__name__})"
        )
    roster = data.get("jobs_with_equipment")
    if not isinstance(roster, list):
        raise PortalTransportError(
            f"GET {FIELDOPS_EQUIPMENT_SNAPSHOT_PATH} missing/invalid 'jobs_with_equipment' array "
            f"(got {type(roster).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return FieldopsEquipmentSnapshot(
        equipment=[row for row in equipment if isinstance(row, dict)],
        jobs_with_equipment=[row for row in roster if isinstance(row, dict)],
    )


class FieldopsMaterialListSnapshot(NamedTuple):
    """The two arrays the material-list-snapshot route returns.

    `lines` — the CURRENT per-job Material List (one row per ACTIVE `job_expected_materials` line on
    an active job). `jobs_with_materials` — the RECONCILE ROSTER: every active job that has ANY
    `job_expected_materials` row (active OR deactivated). The daemon iterates the roster so a job
    whose lines were ALL deactivated is still revisited and its stale `On List=Active` rows marked
    Removed (the count-drops-to-zero silent gap).
    """

    lines: list[dict[str, Any]]
    jobs_with_materials: list[dict[str, Any]]


def get_fieldops_material_list_snapshot(base_url: str, token: str) -> FieldopsMaterialListSnapshot:
    """Pull the CURRENT per-job Material List snapshot + reconcile roster: GET
    /api/internal/fieldops/material-list-snapshot (P7 Material List up-sync, M2).

    Returns a `FieldopsMaterialListSnapshot(lines, jobs_with_materials)`:
      - `lines` — dicts with `line_uuid, job_id, project_name, material_id, catalog_name,
        description, qty, unit, expected_date, status, received_at, qty_received,
        received_by_display, note, unplanned, seq`. `catalog_name` is the resolved catalog model_id
        (NULL for a free-text line); `received_by_display` is the DISPLAY name only (never a
        username — House Reflex §5). A SNAPSHOT (the live per-job list re-projected every cycle),
        NOT an event drain: no watermark, no mark-mirrored companion. The Worker returns the
        complete set (uncapped — the daemon needs the full list to compute retire-removed).
      - `jobs_with_materials` — dicts `{job_id, project_name}` for every active job with ANY
        `job_expected_materials` row (active or deactivated). The reconcile set the daemon iterates
        so a job whose lines were all deactivated still retires its stale sheet rows.

    A control-plane read of OUR OWN Worker (bearer = the SEPARATE field-ops token
    `PORTAL_FIELDOPS_API_TOKEN`, same as `get_fieldops_equipment_snapshot`), NOT a customer send.
    Same typed-error contract: `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted)
    / `PortalTransportError` (any other, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, FIELDOPS_MATERIAL_LIST_SNAPSHOT_PATH, token)
    lines = data.get("lines")
    if not isinstance(lines, list):
        raise PortalTransportError(
            f"GET {FIELDOPS_MATERIAL_LIST_SNAPSHOT_PATH} missing/invalid 'lines' array "
            f"(got {type(lines).__name__})"
        )
    roster = data.get("jobs_with_materials")
    if not isinstance(roster, list):
        raise PortalTransportError(
            f"GET {FIELDOPS_MATERIAL_LIST_SNAPSHOT_PATH} missing/invalid 'jobs_with_materials' "
            f"array (got {type(roster).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return FieldopsMaterialListSnapshot(
        lines=[row for row in lines if isinstance(row, dict)],
        jobs_with_materials=[row for row in roster if isinstance(row, dict)],
    )


def get_fieldops_material_incidents(base_url: str, token: str) -> list[dict[str, Any]]:
    """Pull the filed material-incident ledger for active jobs: GET
    /api/internal/fieldops/material-incidents (P7 Material Incidents up-sync, M3 Slice 2).

    Each dict carries `submission_uuid, job_id, project_name, work_date, created_at, box_link,
    material_description, delivery_ref, qty_expected, qty_received, issue, details, action_taken,
    line_uuid, reported_by_display, line_status`. `reported_by_display` is the DISPLAY name only
    (never a username — House Reflex §5); `line_status` is the referenced expected-materials line's
    CURRENT status (M3 Slice 1 line_uuid join) or None when unlinked/since-deleted.

    Unlike `get_fieldops_material_list_snapshot` this is an APPEND-ONLY EVENT LEDGER, not a
    re-projected snapshot: each element is an immutable FILED (box_verified=1), §34-screened
    incident. There is deliberately NO reconcile roster and the daemon NEVER retires a row — so no
    NamedTuple, just the incident list. The Worker returns the complete set for active jobs (uncapped;
    a per-job incident count is small and the active-job filter bounds the working set).

    A control-plane read of OUR OWN Worker (bearer = the SEPARATE field-ops token
    `PORTAL_FIELDOPS_API_TOKEN`, same as `get_fieldops_material_list_snapshot`), NOT a customer send.
    Same typed-error contract: `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, FIELDOPS_MATERIAL_INCIDENTS_PATH, token)
    incidents = data.get("incidents")
    if not isinstance(incidents, list):
        raise PortalTransportError(
            f"GET {FIELDOPS_MATERIAL_INCIDENTS_PATH} missing/invalid 'incidents' array "
            f"(got {type(incidents).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in incidents if isinstance(row, dict)]


# ---- Request-driven PDF cache (PR-4 Part A — the Mac PDF-servicing pass I/O) ----


def get_pdf_requests(
    base_url: str, token: str, *, limit: int = 25
) -> list[dict[str, Any]]:
    """Pull serviceable PDF-cache requests: GET /api/internal/pdf-requests.

    Each row is a dict `{submission_uuid, box_file_id, form_code, work_date}` for a
    submission the user asked to "make available for download" that is filed
    (box_file_id set) but not yet cached. The Mac pass downloads the Box file by
    `box_file_id`, base64-chunks it, and POSTs each chunk via `upload_filed_pdf`.

    Returns the `pdf_requests` list verbatim (dict rows only). Rows are control-plane
    reads of OUR OWN Worker. Same typed-error contract as `get_pending` —
    `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request(
        "GET", base_url, PDF_REQUESTS_PATH, token, params={"limit": limit}
    )
    pdf_requests = data.get("pdf_requests")
    if not isinstance(pdf_requests, list):
        raise PortalTransportError(
            f"GET {PDF_REQUESTS_PATH} missing/invalid 'pdf_requests' array "
            f"(got {type(pdf_requests).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in pdf_requests if isinstance(row, dict)]


def upload_filed_pdf(
    base_url: str, token: str, *, submission_uuid: str,
    chunk_index: int, chunk_total: int, chunk_b64: str,
) -> dict[str, Any]:
    """Upload one base64 PDF chunk: POST /api/internal/filed-pdf → the ack dict.

    The compiled PDF rides as base64 text inside the JSON body (mirroring the photo
    wire) because `_request` is JSON-only — there is NO raw-binary/multipart path.
    Chunked because a full PDF + base64 inflation can exceed D1's per-row ceiling;
    the Worker reassembles by (submission_uuid, chunk_index) and flips the row to
    ready once `chunk_total` chunks have arrived. Idempotent per chunk
    (INSERT OR REPLACE), so a re-serviced row after a lost ack is a no-op.

    Returns the Worker's ack dict (e.g. `{ok, ready, stored, received}`) verbatim —
    NEVER interpolate `chunk_b64` into a log or error (never log PDF bytes).

    A control-plane write to OUR OWN Worker (outside the External Send Gate,
    Invariant 1 — like `mark_filed`). Raises the typed `PortalTransportError`
    hierarchy on failure (an invalid-chunk 400 surfaces as `PortalTransportError`,
    not a silent return).
    """
    return _request(
        "POST", base_url, FILED_PDF_PATH, token,
        json_body={
            "submission_uuid": submission_uuid,
            "chunk_index": chunk_index,
            "chunk_total": chunk_total,
            "chunk_b64": chunk_b64,
        },
    )


# ---- Checklist item-photo screening queue (G1 Slice 2 — the Mac screening-pass I/O) ----


def get_item_photos_pending(
    base_url: str, token: str, *, limit: int = 25
) -> list[dict[str, Any]]:
    """Pull the unscreened checklist item-photo queue: GET /api/internal/item-photos/pending.

    Each row is a dict `{id, item_state_id, photo_json, hmac, created_at}` — one
    `item_photos` row (migration 0036) at `status='pending'`, oldest-first. Rows are
    UNTRUSTED until the caller verifies each row's `hmac` against the item-photo
    canonical string (`shared.portal_hmac.verify_item_photo` — the same
    verify-before-anything contract as `get_pending`); `photo_json` is the VERBATIM
    HMAC-covered string and must never be re-serialized before verification.

    A control-plane read of OUR OWN Worker (bearer = the poller's
    `PORTAL_INTERNAL_API_TOKEN` tier — same privilege class as `get_pending`), NOT a
    customer-facing send. Same typed-error contract as `get_pending` —
    `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request(
        "GET", base_url, ITEM_PHOTOS_PENDING_PATH, token, params={"limit": limit}
    )
    item_photos = data.get("item_photos")
    if not isinstance(item_photos, list):
        raise PortalTransportError(
            f"GET {ITEM_PHOTOS_PENDING_PATH} missing/invalid 'item_photos' array "
            f"(got {type(item_photos).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in item_photos if isinstance(row, dict)]


def post_item_photo_result(
    base_url: str, token: str, *, photo_id: int, status: str,
    box_file_id: str | None = None, detail: str | None = None,
) -> bool:
    """Post one screening disposition: POST /api/internal/item-photos/:id/result → `found`.

    `status` is `'clean'` (MUST carry `box_file_id` — the Box record already exists;
    the Worker 400s a clean result without it) or `'refused'` (MUST NOT carry
    `box_file_id`; optional `detail` rides the audit row — the machine reason, NEVER
    photo bytes). The Worker applies the disposition in ONE atomic batch (W4):
    `item_photos.status` flip + **photo_json NULLed (delete-on-screen — the bytes
    leave D1)** + `checklist_item_states.photo_ref` → `'<status>:<id>'` + audit row.

    Idempotent: `found=False` means the row was already screened (a re-post after a
    lost ack) or no longer exists — the caller treats it as benign, exactly like
    `mark_filed`. A control-plane write to OUR OWN Worker (outside the External Send
    Gate, Invariant 1). Raises the typed `PortalTransportError` hierarchy on failure
    (an invalid-result 400 surfaces as `PortalTransportError`, never a silent return).
    """
    body: dict[str, Any] = {"status": status}
    if box_file_id is not None:
        body["box_file_id"] = box_file_id
    if detail is not None:
        body["detail"] = detail
    data = _request(
        "POST", base_url,
        ITEM_PHOTO_RESULT_PATH_TEMPLATE.format(photo_id=int(photo_id)), token,
        json_body=body,
    )
    return bool(data.get("found"))


# ---- Daily-pool photo screening queue (DR-photo-pool Slice 2 — the Mac pass I/O) ----


def get_daily_photos_pending(
    base_url: str, token: str, *, limit: int = 25
) -> list[dict[str, Any]]:
    """Pull the unscreened daily-pool photo queue: GET /api/internal/daily-photos/pending.

    Each row is a dict `{id, job_id, work_date, photo_json, hmac, created_at}` — one
    `daily_photo_pool` row (migration 0037) at `status='pending'`, oldest-first
    (claimed AND unclaimed alike — a claim changes ownership, not screening need).
    Rows are UNTRUSTED until the caller verifies each row's `hmac` against the
    daily-photo canonical string (`shared.portal_hmac.verify_daily_photo` — the same
    verify-before-anything contract as `get_pending`); `photo_json` is the VERBATIM
    HMAC-covered string and must never be re-serialized before verification.

    A control-plane read of OUR OWN Worker (bearer = the poller's
    `PORTAL_INTERNAL_API_TOKEN` tier — same privilege class as `get_pending`), NOT a
    customer-facing send. Same typed-error contract as `get_pending` —
    `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request(
        "GET", base_url, DAILY_PHOTOS_PENDING_PATH, token, params={"limit": limit}
    )
    daily_photos = data.get("daily_photos")
    if not isinstance(daily_photos, list):
        raise PortalTransportError(
            f"GET {DAILY_PHOTOS_PENDING_PATH} missing/invalid 'daily_photos' array "
            f"(got {type(daily_photos).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in daily_photos if isinstance(row, dict)]


def post_daily_photo_result(
    base_url: str, token: str, *, photo_id: int, status: str,
    box_file_id: str | None = None, detail: str | None = None,
) -> bool:
    """Post one screening disposition: POST /api/internal/daily-photos/:id/result → `found`.

    `status` is `'clean'` (MUST carry `box_file_id` — the Box record already exists;
    the Worker 400s a clean result without it) or `'refused'` (MUST NOT carry
    `box_file_id`; optional `detail` rides the audit row — the machine reason, NEVER
    photo bytes). The Worker applies the disposition in ONE atomic batch (W4):
    `daily_photo_pool.status` flip + **photo_json NULLed (delete-on-screen — the
    bytes leave D1)** + `box_file_id` + `screened_at` + the changes()-gated audit
    row. Unlike the item-photo twin there is NO sibling ref flip — pool rows
    self-describe their status (the SPA chips + the /pending claim manifest read it).

    Idempotent: `found=False` means the row was already screened (a re-post after a
    lost ack) or no longer exists — the caller treats it as benign, exactly like
    `mark_filed`. A control-plane write to OUR OWN Worker (outside the External Send
    Gate, Invariant 1). Raises the typed `PortalTransportError` hierarchy on failure
    (an invalid-result 400 surfaces as `PortalTransportError`, never a silent return).
    """
    body: dict[str, Any] = {"status": status}
    if box_file_id is not None:
        body["box_file_id"] = box_file_id
    if detail is not None:
        body["detail"] = detail
    data = _request(
        "POST", base_url,
        DAILY_PHOTO_RESULT_PATH_TEMPLATE.format(photo_id=int(photo_id)), token,
        json_body=body,
    )
    return bool(data.get("found"))


# ---- Progress rollup numbers (P6 — the progress weekly-compile's read I/O) ----


def get_progress_rollup(
    base_url: str, token: str, *, job_id: str, week_from: int, week_to: int
) -> dict[str, Any]:
    """Fetch the field-ops rollup aggregate for one job-week: GET /api/internal/progress-rollup.

    Reads the send-free Worker route (P6) that aggregates the structured field-ops D1 tables
    for `job_id` over the Sat→Fri epoch window `[week_from, week_to)`: labor hours
    (`SUM(time_entries.hours)`, amend-collapsed), the DISTINCT equipment on site
    (`equipment_location`), and the open-tasks count (`task_assignments status != 'done'`).
    Returns the Worker's JSON dict verbatim — `{job_id, window:{from,to}, labor_hours,
    equipment:[{name,kind}], open_tasks, materials, generated_at}` — for
    `form_pdf.render_progress_rollup` to lay out. There is NO progress-% (operator decision
    2026-06-30); `materials` is a null M2 placeholder.

    A control-plane READ of OUR OWN Worker (bearer = the poller's `PORTAL_INTERNAL_API_TOKEN`;
    same privilege class as `get_pending`), NOT a customer-facing send — outside the External
    Send Gate (Invariant 1). The Worker computes graceful zeros on empty data, so an
    activity-free week returns `labor_hours=0` / `equipment=[]` / `open_tasks=0`, never an error.

    Typed-shape guard: a malformed body (missing/invalid `labor_hours` / `equipment` /
    `open_tasks`) raises `PortalTransportError` — the daemon's rollup fence then falls back to
    a no-rollup packet (never a wrong number). Same typed-error contract as `get_pending` —
    `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) / `PortalTransportError`
    (any other failure).
    """
    data = _request(
        "GET", base_url, PROGRESS_ROLLUP_PATH, token,
        params={"job_id": job_id, "from": week_from, "to": week_to},
    )
    labor_hours = data.get("labor_hours")
    # bool is an int subclass — exclude it so a stray `true`/`false` is not read as a number.
    if not isinstance(labor_hours, (int, float)) or isinstance(labor_hours, bool):
        raise PortalTransportError(
            f"GET {PROGRESS_ROLLUP_PATH} missing/invalid 'labor_hours' "
            f"(got {type(labor_hours).__name__})"
        )
    if not isinstance(data.get("equipment"), list):
        raise PortalTransportError(
            f"GET {PROGRESS_ROLLUP_PATH} missing/invalid 'equipment' array "
            f"(got {type(data.get('equipment')).__name__})"
        )
    open_tasks = data.get("open_tasks")
    if not isinstance(open_tasks, int) or isinstance(open_tasks, bool):
        raise PortalTransportError(
            f"GET {PROGRESS_ROLLUP_PATH} missing/invalid 'open_tasks' "
            f"(got {type(open_tasks).__name__})"
        )
    return data


# ---- D1 prune observability (GS2 — the watchdog Check V read I/O) ----


def get_prune_status(base_url: str, token: str) -> dict[str, Any] | None:
    """Fetch the D1 prune heartbeat: GET /api/internal/prune-status (GS2).

    Reads the one-row `prune_meta` record the Worker's scheduled daily prune UPSERTs
    after every run (migration 0033) — `{last_run_at, db_size_bytes, size_warn,
    counters, failed_stages}`. Returns the `prune` dict verbatim, or ``None`` when the
    Worker reports no record yet (`prune: null` — the prune has never run since the
    migration; the caller treats that as its own signal, NOT as healthy).

    A control-plane READ of OUR OWN Worker (bearer = the poller's
    `PORTAL_INTERNAL_API_TOKEN` tier, Keychain `ITS_PORTAL_INTERNAL_TOKEN` — same
    privilege class as `get_pending`), NOT a customer-facing send — outside the
    External Send Gate (Invariant 1). Read-only and bounded (single row by schema).

    Consumed by `scripts/watchdog.py` Check V: WARN when `last_run_at` is >48h stale,
    CRITICAL on `failed_stages` non-empty or `db_size_bytes` over the 6 GB threshold.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object `prune` value).
    """
    data = _request("GET", base_url, PRUNE_STATUS_PATH, token)
    prune = data.get("prune")
    if prune is None:
        return None
    if not isinstance(prune, dict):
        raise PortalTransportError(
            f"GET {PRUNE_STATUS_PATH} missing/invalid 'prune' object "
            f"(got {type(prune).__name__})"
        )
    return prune


# ---- Purchase-order internal tier (PO S4 — the po_poll daemon's queue I/O) ----------
#
# All six calls ride the SEPARATE PO bearer tier (`requirePoToken`, Worker
# PORTAL_PO_API_TOKEN / Mac Keychain ITS_PORTAL_PO_TOKEN) — privilege-separated from the
# poller / field-ops / admin tokens; none of the sibling bearers is accepted on these
# routes (pinned by safety_portal/test/po.test.ts). Every one is a control-plane
# read/write of OUR OWN Worker, NOT a customer-facing send — outside the External Send
# Gate (Invariant 1). Same typed-error contract as `get_pending`.


def get_pending_pos(base_url: str, token: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Drain the queued-PO queue: GET /api/po/internal/pending (oldest-first).

    Returns the `pending` list verbatim — each row a full `purchase_orders` D1 row
    (id, po_number, the canonical header fields, status='queued', hmac, ...) with its
    `line_items` array attached. The Worker caps `limit` at 50. Rows are UNTRUSTED
    until the caller recomputes the po:v1 canonical HMAC (`shared.portal_hmac.verify_po`
    — the same verify-before-anything contract as `get_pending`); the canonical JSON is
    REBUILT from these values, so they must be handed to `po_canonical_json` VERBATIM.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, PO_PENDING_PATH, token, params={"limit": limit})
    pending = data.get("pending")
    if not isinstance(pending, list):
        raise PortalTransportError(
            f"GET {PO_PENDING_PATH} missing/invalid 'pending' array "
            f"(got {type(pending).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in pending if isinstance(row, dict)]


def mark_po_filed(
    base_url: str, token: str, *, po_id: int, box_file_id: str | None
) -> bool:
    """Post the filing receipt: POST /api/po/internal/mark-filed → returns `found`.

    Called ONLY after the daemon has verified the HMAC, re-asserted the totals,
    rendered, filed to Box, appended PO_Log, and added the PO_Pending_Review row —
    the Worker flips queued→pending_review + stores `box_file_id`. Idempotent: a
    replay (already pending_review) returns `found=False`, which the caller treats
    as benign (exactly like `mark_filed`).

    Raises the typed `PortalTransportError` hierarchy on failure.
    """
    data = _request(
        "POST", base_url, PO_MARK_FILED_PATH, token,
        json_body={"po_id": po_id, "box_file_id": box_file_id},
    )
    return bool(data.get("found"))


def po_status_sync(
    base_url: str, token: str, updates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Report Mac-side status-machine outcomes: POST /api/po/internal/status-sync.

    `updates` is a non-empty list of `{po_id, status}` with status ∈
    {approved, sent, superseded} (the Worker's SYNCABLE_STATUSES — draft/queued/
    pending_review/canceled are Worker-owned transitions). ORDER MATTERS: the Worker
    executes the updates as ordered statements in one batch and each is guarded on the
    predecessor state (approved only from pending_review; sent only from approved), so
    a row going straight to SENT must send its `approved` update BEFORE its `sent`
    update in the SAME call. A `sent` PO carrying `supersedes_po_id` also flips its
    predecessor to superseded in the same Worker batch. D1 status is a display CACHE
    of the Mac/Smartsheet-side authoritative state; the guards prevent regression, not
    approval — F22 stays Mac-side. Worker cap: 200 updates. Empty list is a Worker 400
    — the caller must never send one.

    Returns the Worker's `{ok, updated}` dict; raises the typed hierarchy on failure.
    """
    return _request(
        "POST", base_url, PO_STATUS_SYNC_PATH, token, json_body={"updates": updates}
    )


def vendors_sync(
    base_url: str, token: str, vendors: list[dict[str, Any]]
) -> dict[str, Any]:
    """Full-replace vendor down-sync: POST /api/po/internal/vendors/sync (§51 rider).

    `vendors` is the COMPLETE ITS_Vendors SoR projection — each row the Worker's
    vendor shape (`vendor_key` VEN-######, `vendor_name`, `address`, `contact_name`,
    `contact_email`, `contact_phone`, `region`, `supply_categories` as a LIST,
    `default_terms_profile`, `gtc_reference`, `active` 0/1, `notes`). The Worker
    upserts every row EXCEPT dirty ones (`sync_state='pending'` — THE dirty-row
    fence: an un-mirrored portal edit is never clobbered), rejects an EMPTY payload
    (a Smartsheet read-miss must never wipe the cache — the caller must also refuse
    to send one), rejects the WHOLE batch on any malformed row, and NEVER deletes —
    a sheet-retired vendor arrives with active=0.

    Returns the Worker's `{ok, upserted, skipped_dirty}` dict; raises the typed
    hierarchy on failure.
    """
    return _request(
        "POST", base_url, PO_VENDORS_SYNC_PATH, token, json_body={"vendors": vendors}
    )


def get_pending_vendors(base_url: str, token: str) -> list[dict[str, Any]]:
    """Pull portal-edited (dirty) vendors to mirror UP: GET /api/po/internal/vendors/pending.

    Returns the `vendors` list verbatim — each a dict with the full vendor payload
    (`supply_categories` already parsed to a list by the Worker) + the version vector
    (`mirror_version`, `mirrored_version`). The daemon bridge-key find-or-creates the
    ITS_Vendors row by `vendor_key`, then commits via `mark_vendors_mirrored`. The
    Worker caps the page at 200 rows server-side; the daemon drains across cycles.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, PO_VENDORS_PENDING_PATH, token)
    vendors = data.get("vendors")
    if not isinstance(vendors, list):
        raise PortalTransportError(
            f"GET {PO_VENDORS_PENDING_PATH} missing/invalid 'vendors' array "
            f"(got {type(vendors).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in vendors if isinstance(row, dict)]


def mark_vendors_mirrored(
    base_url: str, token: str, updates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Vendor up-sync commit point: POST /api/po/internal/vendors/mark-mirrored.

    `updates` is a non-empty list of `{vendor_key, mirrored_version}` where
    `mirrored_version` is the `mirror_version` the daemon READ on the pending pull.
    The Worker flips pending→synced ONLY IF `mirror_version` is UNCHANGED (the
    watermark guard, bound in-WHERE): a portal edit racing the mirror bumps the
    version, the guard fails, the row STAYS pending and re-up-syncs next cycle.
    Idempotent — a replay of a satisfied update is a no-op (`stale`). The Worker
    rejects an EMPTY list (400) — the caller must never send one.

    Returns the Worker's `{ok, flipped, stale}` dict; raises the typed hierarchy.
    """
    return _request(
        "POST", base_url, PO_VENDORS_MARK_MIRRORED_PATH, token,
        json_body={"updates": updates},
    )


def get_po_attachments_pending(
    base_url: str, token: str, *, limit: int = 25
) -> list[dict[str, Any]]:
    """Pull serviceable PO attachments: GET /api/po/internal/attachments/pending.

    Each row is one `po_attachments` D1 row (Feature B, migration 0053) at
    status pending|claimed on a FILED parent PO (pending_review+), oldest-first —
    `{id, att_uuid, po_id, po_number, job_name, filename, declared_mime, size_bytes,
    sha256, status, hmac, uploaded_by, created_at}`. Rows are UNTRUSTED until the
    caller recomputes the po-att:v1 canonical HMAC (`shared.portal_hmac.
    verify_po_attachment`) AND re-derives sha256 over the reassembled chunk bytes —
    the verify-before-anything contract, extended to content. Claimed rows re-serve
    (crash recovery); a disposed (filed/refused) row never returns.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request(
        "GET", base_url, PO_ATTACHMENTS_PENDING_PATH, token, params={"limit": limit}
    )
    attachments = data.get("attachments")
    if not isinstance(attachments, list):
        raise PortalTransportError(
            f"GET {PO_ATTACHMENTS_PENDING_PATH} missing/invalid 'attachments' array "
            f"(got {type(attachments).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in attachments if isinstance(row, dict)]


def claim_po_attachment(base_url: str, token: str, *, attachment_id: int) -> bool:
    """Claim-first marker: POST /api/po/internal/attachments/:id/claim → `found`.

    Flips pending→claimed (the photo-pool claim semantics) so a crash mid-service
    leaves an observable claimed row that re-serves next cycle. Idempotent:
    `found=False` means already claimed/disposed — the caller proceeds (its own read
    saw the row as serviceable). A control-plane write to OUR OWN Worker (outside
    the External Send Gate, Invariant 1). Raises the typed hierarchy on failure.
    """
    data = _request(
        "POST", base_url,
        PO_ATTACHMENT_CLAIM_PATH.format(id=attachment_id), token, json_body={},
    )
    return bool(data.get("found"))


def get_po_attachment_chunks(
    base_url: str, token: str, *, attachment_id: int
) -> list[dict[str, Any]]:
    """Pull one attachment's bytes: GET /api/po/internal/attachments/:id/chunks.

    Returns the `chunks` list verbatim — `{chunk_index, chunk_total, chunk_b64}`
    rows, index-ordered (the filed_pdfs chunk shape; each chunk decodes
    independently, decoded bytes concatenate to the original file). The ONLY route
    that ever serves attachment bytes, and only Mac-ward over this bearer (Option
    D). NEVER interpolate `chunk_b64` into a log or error. The caller MUST verify
    the po-att:v1 HMAC + sha256 over the reassembled bytes before screening.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a 404 for a disposed row).
    """
    data = _request(
        "GET", base_url, PO_ATTACHMENT_CHUNKS_PATH.format(id=attachment_id), token
    )
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        raise PortalTransportError(
            f"GET {PO_ATTACHMENT_CHUNKS_PATH} missing/invalid 'chunks' array "
            f"(got {type(chunks).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in chunks if isinstance(row, dict)]


def post_po_attachment_result(
    base_url: str, token: str, *, attachment_id: int, status: str,
    box_file_id: str | None = None, detail: str | None = None,
) -> bool:
    """Post one screening disposition: POST /api/po/internal/attachments/:id/result.

    `status` is `'filed'` (MUST carry `box_file_id` — the Box record already exists;
    the Worker 400s a filed result without it) or `'refused'` (MUST NOT carry one;
    optional `detail` rides the audit row — the machine reason, NEVER file bytes).
    The Worker applies the disposition in ONE atomic batch (W4): status flip +
    **chunk DELETE (delete-on-disposition — the bytes leave D1)** + audit row.

    Idempotent: `found=False` means the row was already disposed (a re-post after a
    lost ack) — benign, exactly like `mark_filed`. A control-plane write to OUR OWN
    Worker (outside the External Send Gate, Invariant 1). Raises the typed
    `PortalTransportError` hierarchy on failure (an invalid-result 400 surfaces as
    `PortalTransportError`, never a silent return).
    """
    body: dict[str, Any] = {"status": status}
    if box_file_id is not None:
        body["box_file_id"] = box_file_id
    if detail is not None:
        body["detail"] = detail
    data = _request(
        "POST", base_url,
        PO_ATTACHMENT_RESULT_PATH.format(id=attachment_id), token, json_body=body,
    )
    return bool(data.get("found"))


# ---- Vendor-estimate internal tier (ADR-0004 E1/E2 — the estimate_poll daemon's I/O) ----
#
# Faithful mirror of the PO-attachment pool client (above) — same typed-error contract,
# same control-plane read/write semantics — against the estimate pool routes. All five
# calls ride the SEPARATE estimate bearer tier (`requireEstimateToken`, Worker
# PORTAL_ESTIMATE_API_TOKEN / Mac Keychain ITS_PORTAL_ESTIMATE_TOKEN — resolved by the
# caller, `po_materials.estimate_poll`; this module stays stateless). Red-team finding 1
# (ADR-0004 decision 4): the estimate lane holds the highest-exposure process (it decodes
# hostile PDF bytes), so its bearer scopes ONLY /api/po/estimates/internal/* — none of
# the sibling bearers is accepted on these routes and this bearer opens no other tier.
# Every call is a control-plane read/write of OUR OWN Worker, NOT a customer-facing send
# — outside the External Send Gate (Invariant 1).


def get_estimates_pending(
    base_url: str, token: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Pull serviceable vendor estimates: GET /api/po/estimates/internal/pending.

    Each row is one `po_estimates` D1 row (migration 0054) at status pending|claimed,
    oldest-first — `{id, est_uuid, job_no, job_name, filename, declared_mime,
    size_bytes, sha256, status, hmac, uploaded_by, created_at}`. Rows are UNTRUSTED
    until the caller recomputes the est:v1 canonical HMAC
    (`shared.portal_hmac.verify_po_estimate`) AND re-derives sha256 over the
    reassembled chunk bytes — the verify-before-anything contract, extended to
    content. Claimed rows re-serve (crash recovery); a disposed row never returns.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).

    WIRE SHAPE: the response array rides under the key **`estimates`** — the ONE
    internal pending route that differs from the `pending` key the rfq/subcontract/
    safety tiers use (worker/po_estimates.ts:352 `c.json({ estimates: ... })`). This
    client shipped expecting `pending` and the drift was invisible to both sides'
    mocks — it surfaced only live, as 629 `estimate_pending_fetch_failed` cycles with
    every upload stuck at `pending` (2026-07-19→20). The deployed Worker is the
    contract; `tests/test_portal_client.py::test_estimates_pending_wire_key_parity`
    now pins BOTH sides so neither can drift alone again.
    """
    data = _request("GET", base_url, EST_PENDING_PATH, token, params={"limit": limit})
    estimates = data.get("estimates")
    if not isinstance(estimates, list):
        raise PortalTransportError(
            f"GET {EST_PENDING_PATH} missing/invalid 'estimates' array "
            f"(got {type(estimates).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in estimates if isinstance(row, dict)]


def claim_estimate(base_url: str, token: str, *, estimate_id: int) -> bool:
    """Claim-first marker: POST /api/po/estimates/internal/:id/claim → `found`.

    Flips pending→claimed (the attachment-pool claim semantics) so a crash
    mid-service leaves an observable claimed row that re-serves next cycle.
    Idempotent: `found=False` means already claimed/disposed — the caller proceeds
    (its own read saw the row as serviceable). A control-plane write to OUR OWN
    Worker (outside the External Send Gate, Invariant 1). Raises the typed
    hierarchy on failure.
    """
    data = _request(
        "POST", base_url, EST_CLAIM_PATH.format(id=estimate_id), token, json_body={}
    )
    return bool(data.get("found"))


def get_estimate_chunks(
    base_url: str, token: str, *, estimate_id: int
) -> list[dict[str, Any]]:
    """Pull one estimate's bytes: GET /api/po/estimates/internal/:id/chunks.

    Returns the `chunks` list verbatim — `{chunk_index, chunk_total, chunk_b64}`
    rows, index-ordered (the po_attachments chunk shape). The ONLY route that ever
    serves estimate bytes, and only Mac-ward over this bearer. NEVER interpolate
    `chunk_b64` into a log or error. The caller MUST verify the est:v1 HMAC +
    sha256 over the reassembled bytes before screening or parsing.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a 404 for a disposed row).
    """
    data = _request(
        "GET", base_url, EST_CHUNKS_PATH.format(id=estimate_id), token
    )
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        raise PortalTransportError(
            f"GET {EST_CHUNKS_PATH} missing/invalid 'chunks' array "
            f"(got {type(chunks).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in chunks if isinstance(row, dict)]


def post_estimate_result(
    base_url: str, token: str, *, estimate_id: int, status: str,
    detail: str | None = None, box_file_id: str | None = None,
    extraction: dict[str, Any] | None = None,
    rfq_number: str | None = None, rfq_vendor_key: str | None = None,
) -> bool:
    """Post one servicing disposition: POST /api/po/estimates/internal/result.

    `status` is `'refused'` (screen/doc-type rejection; `detail` carries the machine
    reason, e.g. `wrong_doc_type:invoice` — NEVER file bytes), `'needs_review'`
    (filed to Box awaiting the disposition screen — carries `box_file_id`), or
    `'extracted'` (PR-B: carries the tiered `extraction` payload — tier /
    schema_version / doc_type / header cents fields / `lines`; PR-A posts only
    refused/needs_review with NO extraction, but the shape is accepted here so the
    PR-B daemon change is additive). The Worker applies the disposition in ONE
    atomic batch (W4): status flip + chunk handling (refused → chunk DELETE) +
    audit row.

    Idempotent: `found=False` means the row was already disposed (a re-post after a
    lost ack) — benign, exactly like `mark_filed`. A control-plane write to OUR OWN
    Worker (outside the External Send Gate, Invariant 1). Raises the typed
    `PortalTransportError` hierarchy on failure (an invalid-result 400 surfaces as
    `PortalTransportError`, never a silent return).
    """
    body: dict[str, Any] = {"estimate_id": estimate_id, "status": status}
    if detail is not None:
        body["detail"] = detail
    if box_file_id is not None:
        body["box_file_id"] = box_file_id
    if extraction is not None:
        body["extraction"] = extraction
    # R4 round-trip auto-bind (additive): the verified Tier-0 rfq-form:v1 binding. The
    # Worker binds po_estimates.rfq_id/rfq_vendor_key + flips the rfq_vendors row to
    # responded ONLY when BOTH resolve; a shape-bad / unknown value is IGNORED there (never
    # 400s the whole result), so passing them is always safe.
    if rfq_number is not None:
        body["rfq_number"] = rfq_number
    if rfq_vendor_key is not None:
        body["rfq_vendor_key"] = rfq_vendor_key
    data = _request("POST", base_url, EST_RESULT_PATH, token, json_body=body)
    return bool(data.get("found"))


def post_estimate_preview(
    base_url: str, token: str, *, estimate_id: int, page: int, png_b64: str
) -> bool:
    """Upsert one page preview: POST /api/po/estimates/internal/:id/preview.

    `page` is 1-based; `png_b64` is the base64 of one Pillow-re-encoded PNG the
    Worker decodes into `estimate_previews` (decoded cap 1_000_000 bytes — the
    caller keeps each preview under it; an oversize preview is a Worker 400).
    Previews are the fidelity control's source panel (ADR-0004 decision 3): the
    disposition screen blocks accept until the page's preview is loaded. Idempotent
    per (estimate, page) — the Worker upserts. Raises the typed hierarchy on
    failure.
    """
    data = _request(
        "POST", base_url, EST_PREVIEW_PATH.format(id=estimate_id), token,
        json_body={"page": page, "png_b64": png_b64},
    )
    return bool(data.get("found"))


# ---- Subcontract internal tier (SC-S3c — the subcontract_poll daemon's queue I/O) ----
#
# Faithful mirror of the PO internal tier (above) — same typed-error contract, same
# control-plane-read/write semantics. All six calls ride the SEPARATE subcontract bearer
# tier (`requireSubToken`, Worker PORTAL_SUB_API_TOKEN / Mac Keychain ITS_PORTAL_SUB_TOKEN)
# — privilege-separated from the poller / field-ops / admin / PO tokens; none of the
# sibling bearers is accepted on these routes (pinned by safety_portal/test/subcontract.test.ts).
# Every one is a control-plane read/write of OUR OWN Worker, NOT a customer-facing send —
# outside the External Send Gate (Invariant 1). Same typed-error contract as get_pending.


def get_pending_subcontracts(
    base_url: str, token: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Drain the queued-subcontract queue: GET /api/subcontracts/internal/pending (oldest-first).

    Returns the `pending` list verbatim — each row a full `subcontracts` D1 row
    (id, sc_number, the canonical header fields, status='queued', hmac, ...) with its
    `sov_lines` array attached. The Worker caps `limit` at 50. Rows are UNTRUSTED
    until the caller recomputes the sub:v1 canonical HMAC (`shared.portal_hmac.verify_sub`
    — the same verify-before-anything contract as `get_pending`); the canonical JSON is
    REBUILT from these values, so they must be handed to `sub_canonical_json` VERBATIM.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, SUB_PENDING_PATH, token, params={"limit": limit})
    pending = data.get("pending")
    if not isinstance(pending, list):
        raise PortalTransportError(
            f"GET {SUB_PENDING_PATH} missing/invalid 'pending' array "
            f"(got {type(pending).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in pending if isinstance(row, dict)]


def mark_subcontract_filed(
    base_url: str, token: str, *, sc_id: int, box_file_id: str | None
) -> bool:
    """Post the filing receipt: POST /api/subcontracts/internal/mark-filed → returns `found`.

    Called ONLY after the daemon has verified the HMAC, rendered, filed to Box, appended
    Subcontract_Log, and added the Subcontract_Pending_Review row — the Worker flips
    queued→pending_review + stores `box_file_id`. Idempotent: a replay (already
    pending_review) returns `found=False`, which the caller treats as benign (exactly
    like `mark_filed`).

    Raises the typed `PortalTransportError` hierarchy on failure.
    """
    data = _request(
        "POST", base_url, SUB_MARK_FILED_PATH, token,
        json_body={"sc_id": sc_id, "box_file_id": box_file_id},
    )
    return bool(data.get("found"))


def subcontract_status_sync(
    base_url: str, token: str, updates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Report Mac-side status-machine outcomes: POST /api/subcontracts/internal/status-sync.

    `updates` is a non-empty list of `{sc_id, status}` with status ∈
    {approved, sent, executed, superseded} (the Worker's SYNCABLE_SUB_STATUSES —
    draft/queued/pending_review/canceled are Worker-owned transitions). ORDER MATTERS:
    the Worker executes the updates as ordered statements in one batch and each is
    guarded on the predecessor state (approved only from pending_review; sent only from
    approved; executed only from sent), so a row going straight to SENT must send its
    `approved` update BEFORE its `sent` update in the SAME call. A `sent` subcontract
    carrying `supersedes_sc_id` also flips its predecessor to superseded in the same
    Worker batch (the predecessor guard admits status IN ('sent','executed')). D1 status
    is a display CACHE of the Mac/Smartsheet-side authoritative state; the guards prevent
    regression, not approval — F22 stays Mac-side. Worker cap: 200 updates. Empty list is
    a Worker 400 — the caller must never send one.

    Returns the Worker's `{ok, updated}` dict; raises the typed hierarchy on failure.
    """
    return _request(
        "POST", base_url, SUB_STATUS_SYNC_PATH, token, json_body={"updates": updates}
    )


def subcontractors_sync(
    base_url: str, token: str, subcontractors: list[dict[str, Any]]
) -> dict[str, Any]:
    """Full-replace subcontractor down-sync: POST /api/subcontracts/internal/subcontractors/sync.

    `subcontractors` is the COMPLETE ITS_Subcontractors SoR projection — each row the
    Worker's subcontractor shape (`sub_key` SUB-######, `sub_name`, `address`,
    `contact_name`, `contact_email`, `contact_phone`, `state`, `trades` as a LIST,
    `default_terms_profile`, `msa_reference`, `coi_reference`, `license_number`,
    `active` 0/1, `notes`). The Worker upserts every row EXCEPT dirty ones
    (`sync_state='pending'` — THE dirty-row fence: an un-mirrored portal edit is never
    clobbered), rejects an EMPTY payload (a Smartsheet read-miss must never wipe the
    cache — the caller must also refuse to send one), rejects the WHOLE batch on any
    malformed row, and NEVER deletes — a sheet-retired subcontractor arrives with active=0.

    Returns the Worker's `{ok, upserted, skipped_dirty}` dict; raises the typed
    hierarchy on failure.
    """
    return _request(
        "POST", base_url, SUB_SUBCONTRACTORS_SYNC_PATH, token,
        json_body={"subcontractors": subcontractors},
    )


def get_pending_subcontractors(base_url: str, token: str) -> list[dict[str, Any]]:
    """Pull portal-edited (dirty) subcontractors to mirror UP: GET /api/subcontracts/internal/subcontractors/pending.

    Returns the `subcontractors` list verbatim — each a dict with the full subcontractor
    payload (`trades` already parsed to a list by the Worker) + the version vector
    (`mirror_version`, `mirrored_version`). The daemon bridge-key find-or-creates the
    ITS_Subcontractors row by `sub_key`, then commits via `mark_subcontractors_mirrored`.
    The Worker caps the page at 200 rows server-side; the daemon drains across cycles.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, SUB_SUBCONTRACTORS_PENDING_PATH, token)
    subcontractors = data.get("subcontractors")
    if not isinstance(subcontractors, list):
        raise PortalTransportError(
            f"GET {SUB_SUBCONTRACTORS_PENDING_PATH} missing/invalid 'subcontractors' array "
            f"(got {type(subcontractors).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in subcontractors if isinstance(row, dict)]


def mark_subcontractors_mirrored(
    base_url: str, token: str, updates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Subcontractor up-sync commit point: POST /api/subcontracts/internal/subcontractors/mark-mirrored.

    `updates` is a non-empty list of `{sub_key, mirrored_version}` where
    `mirrored_version` is the `mirror_version` the daemon READ on the pending pull.
    The Worker flips pending→synced ONLY IF `mirror_version` is UNCHANGED (the
    watermark guard, bound in-WHERE): a portal edit racing the mirror bumps the
    version, the guard fails, the row STAYS pending and re-up-syncs next cycle.
    Idempotent — a replay of a satisfied update is a no-op (`stale`). The Worker
    rejects an EMPTY list (400) — the caller must never send one.

    Returns the Worker's `{ok, flipped, stale}` dict; raises the typed hierarchy.
    """
    return _request(
        "POST", base_url, SUB_SUBCONTRACTORS_MARK_MIRRORED_PATH, token,
        json_body={"updates": updates},
    )


# ---- Form-editor publish pipeline (slice 3b — the Mac publish daemon's queue I/O) ----


def get_publish_pending(base_url: str, token: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Claimable publish requests: GET /api/internal/publish/pending (queued + unleased,
    oldest-first), each row a dict incl. `definition_json`. Same typed-error contract as
    get_pending."""
    data = _request("GET", base_url, PUBLISH_PENDING_PATH, token, params={"limit": limit})
    pending = data.get("pending")
    if not isinstance(pending, list):
        raise PortalTransportError(
            f"GET {PUBLISH_PENDING_PATH} missing/invalid 'pending' (got {type(pending).__name__})"
        )
    return [row for row in pending if isinstance(row, dict)]


def claim_publish(
    base_url: str, token: str, *, request_id: int, lease_owner: str
) -> dict[str, Any] | None:
    """Atomically lease a publish request: POST /api/internal/publish/claim.

    Returns the claimed row (incl. `definition_json`) on success, or None if it was
    already leased / no longer queued (`claimed=false`) — a benign concurrent-claim
    outcome the daemon skips."""
    data = _request(
        "POST", base_url, PUBLISH_CLAIM_PATH, token,
        json_body={"id": request_id, "lease_owner": lease_owner},
    )
    if not data.get("claimed"):
        return None
    request = data.get("request")
    return request if isinstance(request, dict) else None


def stamp_publish(
    base_url: str, token: str, *, request_id: int, status: str,
    failed_stage: str | None = None, failure_reason: str | None = None,
) -> bool:
    """Advance a publish request's state machine: POST /api/internal/publish/stamp.

    Returns `found`. failed_stage/failure_reason are sent only for status='failed'
    (the Worker ignores them otherwise)."""
    body: dict[str, Any] = {"id": request_id, "status": status}
    if failed_stage is not None:
        body["failed_stage"] = failed_stage
    if failure_reason is not None:
        body["failure_reason"] = failure_reason
    data = _request("POST", base_url, PUBLISH_STAMP_PATH, token, json_body=body)
    return bool(data.get("found"))


def get_publish_stuck(base_url: str, token: str, *, older_than: int) -> list[dict[str, Any]]:
    """Non-terminal publish requests whose updated_at is older than `older_than` seconds — the
    stale-row sweep input (a daemon that claimed-then-died, or a stalled stage). Same typed-error
    contract as get_publish_pending; rows are control-plane reads of OUR OWN Worker."""
    data = _request("GET", base_url, PUBLISH_STUCK_PATH, token, params={"older_than": older_than})
    stuck = data.get("stuck")
    if not isinstance(stuck, list):
        raise PortalTransportError(
            f"GET {PUBLISH_STUCK_PATH} missing/invalid 'stuck' (got {type(stuck).__name__})"
        )
    return [row for row in stuck if isinstance(row, dict)]


# ---- Config-editor queue (§50 — the Mac config daemon's queue I/O, built LATER) ----
# Clones the publish-pipeline transport one-for-one against /api/internal/config/*. Stateless:
# base_url + token are passed IN by the caller (this module reads no Keychain / ITS_Config).


def get_config_pending(base_url: str, token: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Claimable config requests: GET /api/internal/config/pending (queued + unleased,
    oldest-first), each row a dict incl. `payload`. Same typed-error contract as get_pending."""
    data = _request("GET", base_url, CONFIG_PENDING_PATH, token, params={"limit": limit})
    pending = data.get("pending")
    if not isinstance(pending, list):
        raise PortalTransportError(
            f"GET {CONFIG_PENDING_PATH} missing/invalid 'pending' (got {type(pending).__name__})"
        )
    return [row for row in pending if isinstance(row, dict)]


def claim_config(
    base_url: str, token: str, *, request_id: int, lease_owner: str
) -> dict[str, Any] | None:
    """Atomically lease a config request: POST /api/internal/config/claim.

    Returns the claimed row (incl. `payload`) on success, or None if it was already leased /
    no longer queued (`claimed=false`) — a benign concurrent-claim outcome the daemon skips."""
    data = _request(
        "POST", base_url, CONFIG_CLAIM_PATH, token,
        json_body={"id": request_id, "lease_owner": lease_owner},
    )
    if not data.get("claimed"):
        return None
    request = data.get("request")
    return request if isinstance(request, dict) else None


def stamp_config(
    base_url: str, token: str, *, request_id: int, status: str,
    failed_stage: str | None = None, failure_reason: str | None = None,
) -> bool:
    """Advance a config request's state machine: POST /api/internal/config/stamp.

    Returns `found`. failed_stage/failure_reason are sent only for status='failed'
    (the Worker ignores them otherwise)."""
    body: dict[str, Any] = {"id": request_id, "status": status}
    if failed_stage is not None:
        body["failed_stage"] = failed_stage
    if failure_reason is not None:
        body["failure_reason"] = failure_reason
    data = _request("POST", base_url, CONFIG_STAMP_PATH, token, json_body=body)
    return bool(data.get("found"))


def get_config_stuck(base_url: str, token: str, *, older_than: int) -> list[dict[str, Any]]:
    """Non-terminal config requests whose updated_at is older than `older_than` seconds — the
    stale-row sweep input (a daemon that claimed-then-died, or a stalled stage). Same typed-error
    contract as get_config_pending; rows are control-plane reads of OUR OWN Worker."""
    data = _request("GET", base_url, CONFIG_STUCK_PATH, token, params={"older_than": older_than})
    stuck = data.get("stuck")
    if not isinstance(stuck, list):
        raise PortalTransportError(
            f"GET {CONFIG_STUCK_PATH} missing/invalid 'stuck' (got {type(stuck).__name__})"
        )
    return [row for row in stuck if isinstance(row, dict)]


def admin_request(
    base_url: str, token: str, method: str, path: str, *,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Issue one bearer-authed admin request to `/api/internal/admin/*` → (status, json).

    The operator-only control-plane leg (user provision / reset / disable / enable /
    list) to OUR OWN Worker — NOT a customer-facing send (outside the External Send
    Gate, Invariant 1). The bearer is the Mac Keychain `ITS_PORTAL_ADMIN_TOKEN`,
    mirroring the Worker's `PORTAL_ADMIN_API_TOKEN` (SEPARATE from the poller's
    internal token — privilege separation). Retries 429/503 + network failures like
    the other portal_client calls.

    A 401 raises `PortalAuthError` (the admin bearer is wrong/missing — a real
    misconfig). Application statuses (200/201/400/404/409) are RETURNED, not raised,
    so the CLI maps them to operator-readable outcomes (created / exists / not_found
    / invalid). A non-JSON body yields `{}` — the caller treats the status as truth.
    """
    url = base_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    last_detail = ""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.request(
                method, url, json=json_body, headers=headers, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
            if attempt == MAX_RETRIES - 1:
                raise PortalTransportError(
                    f"{method} {path} network failure after {MAX_RETRIES} attempts: {last_detail}"
                ) from exc
            time.sleep(float(2**attempt))
            continue
        if response.status_code == 401:
            raise PortalAuthError(f"{method} {path} unauthorized (401) — admin bearer rejected")
        if response.status_code in (429, 503):
            if attempt == MAX_RETRIES - 1:
                raise PortalRateLimitError(
                    f"{method} {path} throttled/unavailable after {MAX_RETRIES} attempts"
                )
            delay = _parse_retry_after(response.headers.get("Retry-After"))
            time.sleep(delay if delay is not None else float(2**attempt))
            continue
        try:
            data = response.json()
        except ValueError:
            data = {}
        return response.status_code, data if isinstance(data, dict) else {}
    # Unreachable: every loop branch returns or raises on the last attempt.
    raise PortalTransportError(f"{method} {path} exhausted retries: {last_detail}")


# ---- RFQ internal tier (ADR-0004 R2 — the rfq_poll daemon's I/O) --------------------
#
# Faithful mirror of the PO internal-tier client (get_pending_pos / mark_po_filed /
# po_status_sync) — same typed-error contract, same control-plane read/write
# semantics — against the RFQ routes. All three calls ride the SEPARATE RFQ bearer
# tier (`requireRfqToken`, Worker PORTAL_RFQ_API_TOKEN / Mac Keychain
# ITS_PORTAL_RFQ_TOKEN — resolved by the caller, `po_materials.rfq_poll`; this module
# stays stateless). Red-team finding 1 (ADR-0004 decision 4): the RFQ lane's bearer is
# DELIBERATELY separate from the estimate lane's (the highest-exposure hostile-PDF
# decoder must not reach the RFQ send-lane control surface) and from the PO tier —
# it scopes ONLY /api/po/rfqs/internal/*. Every call is a control-plane read/write of
# OUR OWN Worker, NOT a customer-facing send — outside the External Send Gate
# (Invariant 1); the actual vendor send is PR-D's rfq_send half.


def get_rfqs_pending(base_url: str, token: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Pull queued RFQs: GET /api/po/rfqs/internal/pending.

    Each row is one `rfqs` D1 row (migration 0056) at status queued, oldest-first —
    the full row (`id, rfq_uuid, rfq_number, job_no, job_name, ship_to_*,
    delivery_contact_*, scope_text, due_date, status, hmac, …`) plus the Worker-
    joined `line_items` (the PRICE-FREE position-ordered grid, `line_note` per
    line) and `vendors` (the per-vendor `rfq_vendors` rows — `{vendor_key, status,
    box_pdf_file_id, box_form_file_id, review_row_id, …}`; the fan-out list the
    caller derives `vendor_keys` from). Rows are UNTRUSTED until the caller
    rebuilds the rfq:v1 canonical (`shared.portal_hmac.rfq_canonical_json` — sorted
    vendor_keys) and constant-time-verifies the HMAC (`verify_rfq`) — the
    verify-before-anything contract. A generated (all-filed) row never returns.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, RFQ_PENDING_PATH, token, params={"limit": limit})
    pending = data.get("pending")
    if not isinstance(pending, list):
        raise PortalTransportError(
            f"GET {RFQ_PENDING_PATH} missing/invalid 'pending' array "
            f"(got {type(pending).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in pending if isinstance(row, dict)]


def post_rfq_mark_filed(
    base_url: str, token: str, *, rfq_id: int, vendor_results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Post the filing receipt ONCE per RFQ: POST /api/po/rfqs/internal/mark-filed.

    Called ONLY after pass ① has HMAC-verified the row and, per vendor, rendered the
    price-free RFQ PDF, filed it to Box, appended the RFQ_Log row, and added the
    RFQ_Pending_Review row. `vendor_results` is the collected per-vendor outcome
    list — `{vendor_key, box_pdf_file_id, review_row_id[, box_form_file_id]}`
    (string ids; the form file is R3's xlsx quote form, absent in R2). The Worker
    applies it as ONE batch (W4): each vendor row flips pending→filed in-WHERE, then
    the rfq flips queued→generated ONLY when no pending vendor row remains — so a
    partial fan-out (some vendors fenced) leaves the rfq queued and a re-served pass
    finishes idempotently; a replayed vendor result no-ops in-WHERE.

    Returns the Worker's `{ok, filed, replayed, all_filed}` dict; raises the typed
    `PortalTransportError` hierarchy on failure.
    """
    return _request(
        "POST", base_url, RFQ_MARK_FILED_PATH, token,
        json_body={"rfq_id": rfq_id, "vendor_results": vendor_results},
    )


def post_rfq_status_sync(
    base_url: str, token: str, *, rfq_id: int, vendor_key: str, status: str,
    responded_estimate_id: int | None = None,
) -> dict[str, Any]:
    """Report ONE (rfq, vendor) send outcome: POST /api/po/rfqs/internal/status-sync.

    Body is a SINGLE update `{rfq_id, vendor_key, status}` (NOT a batch — the Worker
    contract) with status ∈ {sent, responded}; `responded_estimate_id` rides only
    with `responded` (the R4 round-trip close). FORWARD-ONLY, in-WHERE guarded:
    `sent` only from `filed`, `responded` only from `sent` — a replay or regression
    no-ops (`found=False`, benign). The Worker derives the rfq's own sent/
    partially_sent status in the same batch. D1 status is a display CACHE of the
    Mac/Smartsheet-side authoritative state; F22 approval verification stays with
    the PR-D send poller — this call reports, it does not authorize.

    Returns the Worker's `{ok, found, rfq_status}` dict; raises the typed hierarchy
    on failure.
    """
    body: dict[str, Any] = {"rfq_id": rfq_id, "vendor_key": vendor_key, "status": status}
    if responded_estimate_id is not None:
        body["responded_estimate_id"] = responded_estimate_id
    return _request("POST", base_url, RFQ_STATUS_SYNC_PATH, token, json_body=body)


# ---- Materials-manifest internal tier (PR3b — the manifest_poll daemon's I/O) ----
#
# Faithful mirror of the vendor-estimate pool client (above) — same typed-error contract,
# same control-plane read/write semantics — against the manifest pool routes. All six
# calls ride the SEPARATE manifest bearer tier (`requireManifestToken`, Worker
# PORTAL_MANIFEST_API_TOKEN / Mac Keychain ITS_PORTAL_MANIFEST_TOKEN — resolved by the
# caller, `field_ops.manifest_poll`; this module stays stateless). Same ADR-0004
# decision-4 reasoning as the estimate lane: this process decodes hostile PDF/XLSX bytes,
# so its bearer scopes ONLY /api/fieldops/manifests/internal/* — none of the sibling
# bearers is accepted on these routes and this bearer opens no other tier. Every call is
# a control-plane read/write of OUR OWN Worker, NOT a customer-facing send — outside the
# External Send Gate (Invariant 1).


def get_manifests_pending(
    base_url: str, token: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Pull serviceable materials manifests: GET /api/fieldops/manifests/internal/pending.

    Each row is one `job_manifests` D1 row (migration 0060) at status pending|claimed,
    oldest-first — `{id, manifest_uuid, job_id, filename, declared_mime, size_bytes,
    sha256, hmac, uploaded_by, status, created_at}`. Rows are UNTRUSTED until the
    caller recomputes the manifest:v1 canonical HMAC
    (`shared.portal_hmac.verify_manifest`) AND separately re-derives length + sha256
    over the reassembled chunk bytes — the HMAC covers only the CLAIMS about the bytes.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a non-object / missing-array body).
    """
    data = _request("GET", base_url, MANIFEST_PENDING_PATH, token, params={"limit": limit})
    manifests = data.get("manifests")
    if not isinstance(manifests, list):
        raise PortalTransportError(
            f"GET {MANIFEST_PENDING_PATH} missing/invalid 'manifests' array "
            f"(got {type(manifests).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in manifests if isinstance(row, dict)]


def claim_manifest(base_url: str, token: str, *, manifest_id: int) -> bool:
    """Mark one manifest claimed BEFORE pulling its bytes: POST
    /api/fieldops/manifests/internal/:id/claim.

    Claim-FIRST is the crash-visibility contract: a daemon that dies mid-parse leaves
    an observably `claimed` row rather than a `pending` one that silently re-services
    a hostile document every cycle. `found=False` means the row was already claimed or
    disposed by a previous cycle — BENIGN, and the caller proceeds (its own pending read
    saw the row serviceable). A control-plane write to OUR OWN Worker (outside the
    External Send Gate, Invariant 1).
    """
    data = _request(
        "POST", base_url, MANIFEST_CLAIM_PATH.format(id=manifest_id), token, json_body={}
    )
    return bool(data.get("found"))


def get_manifest_chunks(
    base_url: str, token: str, *, manifest_id: int
) -> list[dict[str, Any]]:
    """Pull one manifest's bytes: GET /api/fieldops/manifests/internal/:id/chunks.

    Returns the `chunks` list verbatim — `{chunk_index, chunk_total, chunk_b64}` rows,
    index-ordered (the po_attachments / po_estimates chunk shape). The ONLY route that
    ever serves manifest bytes, and only Mac-ward over this bearer. NEVER interpolate
    `chunk_b64` into a log or error. The caller MUST verify the manifest:v1 HMAC + the
    length/sha256 recompute over the reassembled bytes before screening or extracting.

    Raises `PortalAuthError` (401) / `PortalRateLimitError` (429/503 exhausted) /
    `PortalTransportError` (any other failure, incl. a 404 for a disposed row).
    """
    data = _request("GET", base_url, MANIFEST_CHUNKS_PATH.format(id=manifest_id), token)
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        raise PortalTransportError(
            f"GET {MANIFEST_CHUNKS_PATH} missing/invalid 'chunks' array "
            f"(got {type(chunks).__name__})"
        )
    # Defensive: keep only dict rows; a non-dict element is malformed transport.
    return [row for row in chunks if isinstance(row, dict)]


def post_manifest_rows(
    base_url: str, token: str, *, manifest_id: int, rows: list[dict[str, Any]]
) -> int:
    """Post ONE PAGE of the parsed grid: POST /api/fieldops/manifests/internal/:id/rows.

    `rows` are `manifest_parse.ParsedRow` projections —
    `{row_index, source_page, kind, cells, flags}` — where `cells` is the row's cell
    list VERBATIM (the validate screen edits against these, so nothing is pre-collapsed
    and duplicate part numbers survive to get a per-row human decision). The Worker
    upserts on `(manifest_id, row_index)`, so a re-posted page is a no-op and a crashed
    daemon simply re-posts from the start next cycle.

    Paged by the caller (a 900-row master BOM cannot ride one request). Returns the
    Worker's accepted-row count. A control-plane write to OUR OWN Worker (outside the
    External Send Gate, Invariant 1); raises the typed `PortalTransportError` hierarchy
    on failure.
    """
    data = _request(
        "POST",
        base_url,
        MANIFEST_ROWS_PATH.format(id=manifest_id),
        token,
        json_body={"rows": rows},
    )
    written = data.get("written")
    return written if isinstance(written, int) and not isinstance(written, bool) else 0


def post_manifest_preview(
    base_url: str, token: str, *, manifest_id: int, page: int, png_b64: str
) -> bool:
    """Upsert one source-page preview: POST /api/fieldops/manifests/internal/:id/preview.

    `page` is 1-based; `png_b64` is the base64 of one rendered PNG the Worker decodes
    into `job_manifest_previews` (decoded cap 1_000_000 bytes — the caller keeps each
    preview under it; an oversize preview is a Worker 400). The previews are what let
    the validate screen show the SOURCE beside the editable grid without ever serving
    the original untrusted bytes to a browser — the same fidelity control the estimate
    lane's disposition screen uses.
    """
    data = _request(
        "POST",
        base_url,
        MANIFEST_PREVIEW_PATH.format(id=manifest_id),
        token,
        json_body={"page": page, "png_b64": png_b64},
    )
    return bool(data.get("found"))


def post_manifest_result(
    base_url: str,
    token: str,
    *,
    manifest_id: int,
    status: str,
    detail: str | None = None,
    box_file_id: str | None = None,
    profile: str | None = None,
    row_count: int | None = None,
    column_map: dict[str, Any] | None = None,
    header_meta: dict[str, str] | None = None,
    parse_notes: str | None = None,
) -> bool:
    """Post one servicing disposition: POST /api/fieldops/manifests/internal/result.

    `status` is `'refused'` (§34 screen rejection, an unreadable container, or an
    unusable parse; `detail` carries the machine reason — NEVER file bytes) or
    `'parsed'` (filed to Box and awaiting the validate screen — carries `box_file_id`,
    the `profile`, the posted `row_count`, the proposed `column_map`, the header `meta`
    block and the parser's `notes`). The Worker applies the disposition in ONE atomic
    batch (W4): status flip + chunk DELETE (the bytes are no longer needed once the
    grid exists) + audit row.

    Posted LAST, after the rows and previews are in: a crash before this leaves the row
    `claimed` and every prior step is idempotent, so the next cycle simply redoes them.
    Idempotent: `found=False` means the row was already disposed (a re-post after a lost
    ack) — benign. A control-plane write to OUR OWN Worker (outside the External Send
    Gate, Invariant 1); raises the typed `PortalTransportError` hierarchy on failure.
    """
    body: dict[str, Any] = {"manifest_id": manifest_id, "status": status}
    if detail is not None:
        body["detail"] = detail
    if box_file_id is not None:
        body["box_file_id"] = box_file_id
    if profile is not None:
        body["profile"] = profile
    if row_count is not None:
        body["row_count"] = row_count
    if column_map is not None:
        body["column_map"] = column_map
    if header_meta is not None:
        body["header_meta"] = header_meta
    if parse_notes is not None:
        body["parse_notes"] = parse_notes
    data = _request("POST", base_url, MANIFEST_RESULT_PATH, token, json_body=body)
    return bool(data.get("found"))
