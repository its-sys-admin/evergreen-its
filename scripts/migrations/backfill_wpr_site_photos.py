"""Backfill PRE-BRIDGE inline site photos into the WPR photo pool (0074, Track B-D).

The site-photos → pool bridge (PR #138) registers a daily report's inline photos at FILING
time — submissions filed BEFORE it went live (2026-08-14) have their photos in Box but no
`daily_photo_pool` rows, so the weekly-report picker cannot offer them. This one-shot walks
each named submission's Box folder (`<job>/<week>/ITS Photos/<submission_uuid>/`), downloads
the already-§34-screened originals, derives the picker thumbnails, and POSTs the SAME
idempotent register route the live bridge uses — a re-run registers 0 and skips N.

OPERATOR-RUN, NOT A DAEMON. Dry-run by default; `--apply` to execute. Per-submission fenced —
one unresolvable folder is reported and skipped, never an abort. No send capability, no LLM
(Invariant 1); the one network write is the bearer-gated register POST to OUR OWN Worker.

Usage (from ~/its on the dev Mac, live venv):

    .venv/bin/python scripts/migrations/backfill_wpr_site_photos.py \\
        --project-name "Deep Lake" \\
        --submission 42b4670c-811a-4a18-851c-f310166f8b7a 2026-08-11 \\
        [--submission <uuid> <work_date> ...] [--apply]

Find candidate uuids with a read-only D1 query:
    npx wrangler d1 execute its-safety-portal-db --remote --command \\
      "SELECT submission_uuid, work_date FROM submissions WHERE job_id='JOB-…' AND form_code LIKE 'daily-report%'"
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
from datetime import date

from safety_reports import intake, photo_screen
from shared import box_client, keychain, portal_client

DEFAULT_BASE_URL = "https://safety.evergreenmirror.com"
# The bridge files photos as 01.jpg … 08.jpg (photo_screen.MAX_PHOTOS_PER_SUBMISSION).
_PHOTO_NAME_RE = re.compile(r"^\d{2}\.jpg$")


def _collect_registrations(
    project_name: str, parent_form_code: str, submission_uuid: str, work_date: date
) -> list[dict[str, str]] | str:
    """Resolve the submission's Box photo folder and build the register payload.

    Returns the photos list, or a string reason when there is nothing to register
    (no folder = the submission filed no inline photos — a skip, not an error).
    """
    folder_id, note = intake._resolve_portal_box_folder(  # noqa: SLF001 — the one-shot reuses
        project_name, parent_form_code, work_date        # intake's exact resolution on purpose:
    )                                                    # a re-derivation here could diverge.
    if folder_id is None:
        return f"box root unresolved ({note})"
    photos_root = box_client.find_child_folder(folder_id, "ITS Photos")
    if photos_root is None:
        return "no 'ITS Photos' folder (submission filed no inline photos)"
    sub_folder = box_client.find_child_folder(photos_root, submission_uuid)
    if sub_folder is None:
        return "no per-submission photo folder (submission filed no inline photos)"
    out: list[dict[str, str]] = []
    for item in sorted(box_client.list_folder(sub_folder), key=lambda x: str(x.get("name"))):
        if item.get("type") != "file" or not _PHOTO_NAME_RE.match(str(item.get("name") or "")):
            continue
        jpeg = box_client.download_file(str(item["id"]))
        reg: dict[str, str] = {"box_file_id": str(item["id"])}
        thumb = photo_screen.make_thumbnail(jpeg)
        if thumb is not None:
            reg["thumb_b64"] = base64.b64encode(thumb).decode("ascii")
        out.append(reg)
    if not out:
        return "photo folder exists but holds no NN.jpg files"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project-name", required=True)
    ap.add_argument("--parent-form-code", default="daily-report")
    ap.add_argument(
        "--submission", nargs=2, action="append", required=True,
        metavar=("UUID", "WORK_DATE"), help="submission uuid + its YYYY-MM-DD work date (repeatable)",
    )
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--apply", action="store_true", help="actually POST; default is dry-run")
    args = ap.parse_args()

    bearer = keychain.get_secret("ITS_PORTAL_INTERNAL_TOKEN")
    exit_code = 0
    for uuid_arg, date_arg in args.submission:
        try:
            work_date = date.fromisoformat(date_arg)
        except ValueError:
            print(f"SKIP {uuid_arg}: bad work_date {date_arg!r}")
            exit_code = 1
            continue
        try:
            result = _collect_registrations(
                args.project_name, args.parent_form_code, uuid_arg, work_date
            )
        except Exception as exc:  # noqa: BLE001 — per-submission fence: report + continue
            print(f"SKIP {uuid_arg}: {type(exc).__name__}: {exc}")
            exit_code = 1
            continue
        if isinstance(result, str):
            print(f"SKIP {uuid_arg}: {result}")
            continue
        thumbs = sum(1 for r in result if "thumb_b64" in r)
        if not args.apply:
            print(f"DRY-RUN {uuid_arg}: would register {len(result)} photo(s) ({thumbs} with thumbnails)")
            continue
        resp = portal_client.post_daily_photos_register(
            args.base_url, bearer, submission_uuid=uuid_arg, photos=result
        )
        print(
            f"APPLIED {uuid_arg}: registered={resp.get('registered')} skipped={resp.get('skipped')} "
            f"({thumbs} with thumbnails)"
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
