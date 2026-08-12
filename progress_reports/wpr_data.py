"""Assemble the client-facing Weekly Production Report's data for one (job, week).

The bridge between the send-free Worker aggregate (`GET /api/internal/production-report`,
migration 0067) and `form_pdf.render_production_report`. It fetches the derived half, resolves
the two things the Worker deliberately does not (safety-meeting DISPLAY names from the git-owned
form definitions, and photo BYTES from Box), assembles the narrative deterministically, and
returns the renderer's render-ready dict.

**NO AI.** The narrative sections are built by rule from text the field already typed — the
office's own words, reordered, never a model's. This module is in `GATED_SCRIPTS` with
`anthropic` / `anthropic_client` on its forbid list, and the office edits the assembled text on
the weekly screen before anything is sent.

**NO SEND.** Every call here is a READ: our own send-free Worker (bearer, same privilege class as
the portal_poll pull) and Box (the ITS OAuth account). The report this produces is transmitted
only by `progress_send`, after a human approves the `WPR_human_review` row — a different process
(Invariant 1, the two-process model).

**The office's word wins.** Wherever the office has typed something on the weekly screen, that is
what renders; the deterministic assembly is only the SEED for an untouched field. A saved empty
string is a deliberate "nothing to report" and is preserved as such — it is not re-seeded, or the
office could never clear a section.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from safety_reports import form_pdf
from shared import box_client, portal_client
from shared.active_jobs import ActiveJob
from shared.safety_week import SafetyWeek

logger = logging.getLogger(__name__)

# Bound on how much assembled narrative we seed. The office edits this text in a Smartsheet-backed
# screen and reads it in a client document; an unbounded concatenation of a busy week's daily
# comments would be neither reviewable nor printable.
_MAX_SEED_ITEMS = 12
_MAX_SEED_CHARS = 1500

# A form_code is `<name>-v<N>`; the display name lives in the definition JSON. Falls back to a
# humanized code so an unresolvable definition still reads as English on the client's page.
_CODE_VERSION_RE = re.compile(r"-v\d+$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _squash(s: str) -> str:
    """Lowercase, alphanumerics only — for comparing two labels that may say the same thing
    with different punctuation."""
    return _NON_ALNUM_RE.sub("", s.lower())


def _display_form_name(form_code: str) -> str:
    """Resolve a safety-meeting form code to its human name.

    The Worker returns CODES on purpose: the display name lives in the git-owned form definitions,
    which this side already loads, and duplicating that mapping into TypeScript would be a second
    source of truth for a name that changes whenever a form is republished.
    """
    definition = form_pdf.load_definition(form_code)
    if isinstance(definition, dict):
        name = definition.get("form_name")
        variant = definition.get("variant_label")
        if isinstance(name, str) and name.strip():
            # Append the variant ONLY when it adds information the name does not already carry.
            # The containment test compares ALPHANUMERICS ONLY, because the two strings routinely
            # say the same thing with different punctuation. A live render put this on a client's
            # page: "Toolbox Talk — Severe Weather (Tornadoes, High Winds, Lightning and Flooding)
            # — Severe Weather — Tornadoes, High Winds, Lightning and Flooding" — a raw substring
            # check saw parentheses-vs-em-dash and concluded the variant was new information.
            if isinstance(variant, str) and variant.strip() and _squash(variant) not in _squash(name):
                return f"{name.strip()} — {variant.strip()}"
            return name.strip()
    # Fallback: "toolbox-talk-heat-hydration-v1" → "Toolbox Talk Heat Hydration".
    return _CODE_VERSION_RE.sub("", form_code).replace("-", " ").strip().title()


def _joined(items: list[str]) -> str:
    """Bounded newline-join of assembled narrative lines, de-duplicated in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        text = (raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= _MAX_SEED_ITEMS:
            break
    joined = "\n".join(out)
    return joined[:_MAX_SEED_CHARS]


def _assemble_critical_items(payload: dict[str, Any]) -> str:
    """Seed for "Critical Items / Delays" — deterministic, from what the field reported.

    Order is deliberate and reads worst-first: behind-schedule tasks (the Worker returns them
    oldest-slip-first), then material incidents (a delivery problem IS the delay), then the daily
    `comments` field, which is where a foreman records what went wrong.

    A slipped CONTRACT milestone is labelled as one. It is a different conversation from a slipped
    task, and a client reading the line should not have to know the schedule to tell them apart.
    """
    lines: list[str] = []
    schedule = payload.get("schedule") or {}
    for t in (schedule.get("behind") or []):
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        where = str(t.get("section") or "").strip()
        head = f"{where}: {name}" if where else name
        kind = "Contract milestone behind schedule" if t.get("is_contract_milestone") else "Behind schedule"
        lines.append(
            f"{kind} — {head} (due {t.get('finish_date', '')}, {int(t.get('percent') or 0)}% complete)"
        )
    for inc in payload.get("material_incidents") or []:
        if not isinstance(inc, dict):
            continue
        material = str(inc.get("material") or "").strip()
        issue = str(inc.get("issue") or "").strip()
        if material or issue:
            detail = str(inc.get("details") or "").strip()
            head = f"{material}: {issue}" if material and issue else (material or issue)
            lines.append(f"{head} — {detail}" if detail else head)
    for note in payload.get("daily_notes") or []:
        if isinstance(note, dict):
            lines.append(str(note.get("comments") or "").strip())
    return _joined(lines)


def _assemble_upcoming(payload: dict[str, Any]) -> str:
    """Seed for "Upcoming Activities" — the LAST filed "Tomorrow's Progress Goals".

    Only the last one: earlier days' goals describe work that has since happened, so concatenating
    the week would report completed work as upcoming. `daily_notes` arrives in work-date order.
    """
    goals = [
        str(n.get("tomorrows_goals") or "").strip()
        for n in (payload.get("daily_notes") or []) if isinstance(n, dict)
    ]
    latest = next((g for g in reversed(goals) if g), "")
    return latest[:_MAX_SEED_CHARS]


def _assemble_labor_rows(payload: dict[str, Any], office_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Labor Report rows: the office's table when they have filled one, otherwise a seed of
    the crews the field actually reported.

    Man-hours are left BLANK in the seed on purpose. `personnel` carries no employer column, so
    `time_entries.hours` cannot be attributed to a subcontractor; splitting the job total across
    crews by headcount would look authoritative and be a guess. The office types the real number,
    and the page-2 footnote carries the honest job-wide total meanwhile.
    """
    if office_rows:
        return office_rows
    seed: list[dict[str, Any]] = []
    for crew in (payload.get("labor") or {}).get("crews") or []:
        if not isinstance(crew, dict):
            continue
        company = str(crew.get("company") or "").strip()
        if not company:
            continue
        workers = crew.get("workers")
        seed.append({
            "company": company,
            "workers": "" if workers in (None, 0) else str(workers),
            "man_hours": "",
        })
    return seed


def _resolve_photos(payload: dict[str, Any], correlation: str) -> list[tuple[str, bytes]]:
    """Download each selected photo from Box by `box_file_id` → (caption, jpeg_bytes).

    Box is the permanent record; D1 holds bytes only until the Mac's §34 screen dispositions them
    (`daily_photo_pool.photo_json` is NULLed on screen). A pool row earns a `box_file_id` ONLY on
    a CLEAN disposition, so downloading by that id is what makes it structurally impossible for an
    unscreened or refused photo to reach a client report.

    Per-photo fenced: one download failure drops that photo with a WARN and the report still
    renders. A photo page missing one image is a far better outcome than no weekly report.
    """
    out: list[tuple[str, bytes]] = []
    for pick in (payload.get("photos") or {}).get("selected") or []:
        if not isinstance(pick, dict):
            continue
        file_id = str(pick.get("box_file_id") or "").strip()
        if not file_id:
            continue
        caption = str(pick.get("caption") or "").strip()
        try:
            out.append((caption, box_client.download_file(file_id)))
        except Exception:  # noqa: BLE001 — one photo must never cost the week its report
            logger.warning(
                "wpr_data: progress photo skipped (box_file_id=%s, work_date=%s) — download failed [%s]",
                file_id, pick.get("work_date"), correlation,
            )
    return out


def _office_or_seed(office_value: Any, saved: bool, seed: str) -> str:
    """The office's text when this week's row was SAVED, else the deterministic seed.

    `saved` is load-bearing: a saved empty string means the office deliberately cleared the
    section, and re-seeding it would make that impossible. An UNSAVED week (nothing written, or
    values carried forward from an earlier week) still gets the seed, because carried narrative
    describes last week's job.
    """
    if saved and isinstance(office_value, str):
        return office_value
    return seed


def build(job: ActiveJob, week: SafetyWeek, *, base_url: str, bearer: str,
          correlation_id: str = "") -> dict[str, Any]:
    """Fetch and assemble the render-ready dict for `form_pdf.render_production_report`.

    Raises whatever `portal_client` raises (`PortalAuthError` / `PortalRateLimitError` /
    `PortalTransportError`) — the compile fences this call, so a broken-but-wired report degrades
    to filing the field-records packet alone and is LOUD about it, never silently absent.
    """
    from progress_reports.progress_weekly_generate import _week_epoch_window  # local: avoid a cycle

    week_from, week_to = _week_epoch_window(week)
    payload = portal_client.get_production_report(
        base_url, bearer, job_id=job.job_id,
        week_start=week.start.isoformat(), week_end=week.end.isoformat(),
        week_from=week_from, week_to=week_to,
    )

    office = payload.get("office") or {}
    saved = bool(office.get("saved"))
    o_header = office.get("header") or {}
    o_weather = office.get("weather") or {}
    o_narrative = office.get("narrative") or {}
    o_labor = (office.get("labor") or {}).get("rows") or []
    job_row = payload.get("job") or {}

    # Site location: the office's typed value wins (it is the client-facing place name, e.g.
    # "Klamath Falls, OR"); the job's structured city/state is the fallback so a never-filled
    # screen still labels the document.
    location = str(o_header.get("site_location") or "").strip()
    if not location:
        city = str(job_row.get("address_city") or "").strip()
        state = str(job_row.get("address_state") or "").strip()
        location = ", ".join(p for p in (city, state) if p)

    # Safety-meeting topics: the office's list when saved, else the week's filed toolbox
    # talks / JHAs resolved to their display names.
    topics = o_narrative.get("hazard_topics") or []
    if not (saved and topics):
        topics = [_display_form_name(c) for c in (payload.get("hazard_form_codes") or [])]

    weather = payload.get("weather") or {}
    labor = payload.get("labor") or {}

    return {
        "job_name": job.project_name,
        "site_location": location,
        "ess_management": o_header.get("ess_management") or "",
        "mobilization_date": o_header.get("mobilization_date") or "",
        "week_label": week.label,
        "report_submitted": week.end.isoformat(),
        "prepared_by": o_header.get("prepared_by") or "",
        "subcontractors": o_header.get("subcontractors") or [],
        "safety": office.get("safety") or {},
        "hazard_topics": topics,
        "weather": {
            "days": [
                {
                    "date": d.get("work_date", ""),
                    "conditions": d.get("conditions", ""),
                    "avg_temp": d.get("avg_temp", ""),
                    "inclement": bool(d.get("inclement")),
                }
                for d in (weather.get("days") or []) if isinstance(d, dict)
            ],
            "week": weather.get("weather_days_week", 0),
            "to_date": o_weather.get("weather_days_to_date", 0),
        },
        "labor": {
            "rows": _assemble_labor_rows(payload, o_labor),
            "total_hours": labor.get("total_hours", 0),
        },
        "progress": {
            # PR-5 fills `sections` from the ADR-0006 job_schedule_tasks read. Until then the
            # Worker returns schedule: null and the renderer prints its honest empty state.
            "sections": (payload.get("schedule") or {}).get("sections") or [],
            "critical_items": _office_or_seed(
                o_narrative.get("critical_items"), saved, _assemble_critical_items(payload)),
            "upcoming_activities": _office_or_seed(
                o_narrative.get("upcoming_activities"), saved, _assemble_upcoming(payload)),
        },
        "photos": _resolve_photos(payload, correlation_id),
        "materials": {
            "deliveries": [
                {
                    "item": d.get("item", "") or d.get("part_number", ""),
                    "vendor": d.get("vendor", ""),
                    "qty": d.get("qty", ""),
                    "delivered": d.get("event_date", ""),
                }
                for d in (payload.get("deliveries") or []) if isinstance(d, dict)
            ],
        },
        "pending": office.get("pending") or {},
    }
