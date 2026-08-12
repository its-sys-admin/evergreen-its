"""Weekly-compile CORE — the deterministic packet + review-row dual-write, parameterized.

This is the shared engine behind BOTH the safety weekly compile (`weekly_generate`) and the
progress weekly compile (`progress_weekly_generate`) — the GENERATION half of the External
Send Gate two-process model (Foundation Mission v11 Invariant 1). **Zero send capability.
Zero AI.** Capability gating is enforced BOTH on this module AND on the thin per-workstream
entry-point modules that instantiate it — all are registered in
`tests/test_capability_gating.py::GATED_SCRIPTS` (belt-and-suspenders).

Parameterize-not-clone (Op Stds §14). Every workstream-specific binding — which Active-Jobs
sheet to iterate, which week-sheet workspace, which review sheet + row-writer, which Box root,
the config-key prefix, the compile-mutex role, the watchdog slug — is carried in a required
`GenerateConfig`. The two canonical configs live next to their entry points
(`weekly_generate.SAFETY_GENERATE_CONFIG`, `progress_weekly_generate.PROGRESS_GENERATE_CONFIG`).
Because the safety entry point binds the EXACT prior values, the safety compile is byte-identical
to its pre-extraction self (guarded by `tests/test_weekly_generate.py`).

Per (job, week): gather the week sheet's per-submission PDFs → merge into one packet
(`form_pdf.merge_pdfs`, branded cover + date index) → file the packet to the workstream's Box
week folder as a DISTINCT, version-numbered file (append-only; recompiles bump `_v2`/`_v3`…) →
APPEND a Rollup snapshot row + a review-sheet row (PENDING). A recompile NEVER overwrites a prior
packet / Rollup / review row. Empty week → STILL appends a Rollup + review row (a silent skip
looks like daemon failure); the review row carries no packet, so the send HELDs it.

No-mix-up (operator requirement): the workstream binding is the ONE `GenerateConfig` — the
review row is written to `config.review_sheet_id` with `config.add_review_row` (which tags the
row's `Workstream`), recipients come only from `config.active_jobs_config`'s sheet, and the
packet files under `config.box_root_setting_key`. A progress compile can never write a safety
review row or resolve a safety recipient, and vice-versa.

Adversarial Input Handling (Invariant 2): the compile is deterministic over already-rendered,
already-HMAC-verified PDFs + typed Smartsheet cells — there is NO LLM step (Layer-2 N/A).
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from safety_reports import compile_core, form_pdf, safety_naming, week_sheet
from shared import (
    active_jobs,
    box_client,
    compile_mutex,
    error_log,
    project_routing,
    review_queue,
    safety_week,
    smartsheet_client,
)
from shared.active_jobs import ActiveJob, ActiveJobsConfig
from shared.error_log import Severity

DEFAULT_TZ = "America/Los_Angeles"  # everything Pacific (Brief v6.1)

# Box link shape produced by intake (`https://app.box.com/file/<id>`).
_BOX_FILE_LINK_RE = re.compile(r"/file/(\d+)")
_MAX_PACKET_VERSIONS = 200  # bound the recompile _vN probe; an absurd ceiling (real weeks ≤ a few)

# Type of the uniform review-row writer a config binds. Safety binds a partial of
# wsr_review.add_wsr_row (sheet+workstream baked in); progress binds wpr_review.add_wpr_row.
AddReviewRow = Callable[..., int]
EmailBodyTemplate = Callable[..., str]


@dataclass(frozen=True)
class GenerateConfig:
    """Every workstream-specific binding for one weekly compile. Required, no defaults — the
    caller (safety or progress entry point) binds explicitly (parameterize-not-clone, §14)."""

    script_name: str          # SCRIPT_NAME for error_log (e.g. "safety_reports.weekly_generate")
    workstream: str           # review_queue workstream tag (e.g. "safety_reports")
    week_sheet_config: week_sheet.WeekSheetConfig
    active_jobs_config: ActiveJobsConfig
    review_sheet_id: int      # the WSR/WPR sheet the review row + inline attach land on
    add_review_row: AddReviewRow      # uniform: (*, job_project, job_id, week_of, …) -> row_id
    email_body_template: EmailBodyTemplate
    box_root_setting_key: str         # ITS_Config key for the portal Box root
    box_legacy_fallback: bool         # safety: fall back to project_routing tree; progress: no
    compile_mutex_role: str           # "safety" / "progress"
    watchdog_slug: str                # Check-C marker slug
    sla_tier: review_queue.SlaTier
    cfg_job_timeout: str
    default_job_timeout: int
    cfg_memory_ceiling: str
    default_memory_ceiling: int
    cfg_evergreen_contact: str
    default_evergreen_contact: str

    # ── Optional trailing hooks (§14: byte-identical when unset) ──────────────────────
    # A workstream may bind an OPTIONAL rollup-numbers page provider (P6). When set, the
    # compile splices its returned page into the packet AFTER the cover, BEFORE the index
    # (FENCED — see `_maybe_rollup_page` / `_build_weekly_packet`). The SAFETY config binds
    # NOTHING (default None) → the provider is never called, the merge list is unchanged, and
    # the safety packet stays byte-identical (the preservation invariant pinned by
    # tests/test_weekly_generate.py). The provider is a `(job, week) -> bytes | None` closure;
    # PROGRESS binds one in `progress_weekly_generate` that fetches the send-free Worker rollup
    # route and renders it — so `generate_core` itself gains NO new import (no portal_client, no
    # keychain, no form_pdf render coupling) and stays a pure engine (§42: the fetch closure
    # lives in the progress module, not here, to keep the shared engine import-pure + gate-clean).
    rollup_page_provider: (
        Callable[[ActiveJob, safety_week.SafetyWeek], bytes | None] | None
    ) = None
    # The cover page's report title (2026-07-23). The old hardcoded cover title
    # mislabeled every PROGRESS packet cover "WEEKLY SAFETY REPORT"; each binding now
    # names its packet. Default preserves the safety wording (§14: unset = unchanged).
    cover_title: str = "WEEKLY SAFETY REPORT"
    # ── the client-facing report seam (0067) ──────────────────────────────────────────────
    # A workstream may produce a SECOND artifact from the same compile: a synthesized document
    # for the CLIENT, distinct from the packet of filed field records. When bound, the returned
    # bytes are filed to the same Box week folder under `client_report_suffix` and the REVIEW
    # ROW's Compiled PDF points at THAT file — so the send attaches the report, not the packet.
    # The packet is still built, still filed, still attached to the week-sheet Rollup row, and
    # its link rides the review row's Notes: it remains the internal record, it just stops being
    # what the client receives.
    #
    # Same shape and the same reasons as `rollup_page_provider` above: a `(job, week) -> bytes |
    # None` closure bound in the workstream module, so `generate_core` gains no portal_client /
    # keychain / renderer import and stays a pure engine (§42). Unbound (safety) → the compile is
    # byte-identical to before.
    client_report_provider: (
        Callable[[ActiveJob, safety_week.SafetyWeek], bytes | None] | None
    ) = None
    client_report_suffix: str = "WPR"
    # The FIELD-RECORDS packet's Box filename suffix. Hardcoded "WSR" until now, so every PROGRESS
    # packet filed as `<Job>_week of <Sat>_WSR.pdf` — safety's initials on a progress artifact,
    # the exact class `cover_title` was parameterized to fix and the same pass missed. Default
    # preserves safety's historical name byte-for-byte (§14: unset = unchanged).
    packet_suffix: str = "WSR"
    # A week with NO submissions currently writes a PENDING review row with an empty packet link,
    # which the send later HELDs for a missing PDF. When a workstream's artifact is CLIENT-FACING
    # that is too late and too quiet: the office should consciously decide whether a no-activity
    # week gets a note, a skip, or an investigation. Bound True (progress) the empty-week row is
    # written HELD with the reason in Notes plus a Review-Queue item. Default False keeps safety
    # exactly as it was.
    empty_week_hold: bool = False

    @property
    def watchdog_marker_dir(self) -> Path:
        return Path.home() / "its" / ".watchdog"


@dataclass
class RunSummary:
    """Per-run counters returned from run_generate(); logged via @its_error_log on the caller."""
    jobs_processed: int = 0
    packets_compiled: int = 0
    client_reports_compiled: int = 0
    skipped_no_change: int = 0
    empty_weeks: int = 0
    wsr_written: int = 0
    review_queue_entries: int = 0
    timed_out: int = 0
    download_errors: int = 0
    errors_per_job: dict[str, str] = field(default_factory=dict)


# ---- Config readers ------------------------------------------------------


def _read_str_setting(config: GenerateConfig, key: str, fallback: str) -> str:
    try:
        raw = smartsheet_client.get_setting(key, workstream=config.workstream)
    except smartsheet_client.SmartsheetNotFoundError:
        return fallback
    except smartsheet_client.SmartsheetCircuitOpenError:
        return fallback
    except smartsheet_client.SmartsheetError as exc:
        # Transient read failure (timeout / 5xx) — a single-cycle blip must not
        # escape to @its_error_log as a spurious CRITICAL. WARN + fall open to
        # the fallback, same disposition as the circuit-open branch above.
        error_log.log(
            Severity.WARN, config.script_name,
            f"config read failed for {key}: {exc!r} — using fallback {fallback!r}",
            error_code="config_read_error",
        )
        return fallback
    return raw if isinstance(raw, str) and raw else fallback


def _read_int_setting(config: GenerateConfig, key: str, fallback: int) -> int:
    """Defensive int ITS_Config read, LAYERED ON _read_str_setting (the one config seam the
    tests mock) — missing row / circuit-open / non-int all resolve to fallback, never raising
    into the compile. Matches the pre-P4 weekly_generate behavior."""
    try:
        return int(_read_str_setting(config, key, str(fallback)).strip())
    except (TypeError, ValueError):
        return fallback


# ---- Box helpers ---------------------------------------------------------


def _box_file_id(link: str) -> str | None:
    m = _BOX_FILE_LINK_RE.search(link or "")
    return m.group(1) if m else None


def _its_week_folder_name(week: safety_week.SafetyWeek) -> str:
    """ITS-prefixed Box folder for the week's compiled packet (legacy project_routing path)."""
    return f"ITS Week of {week.start.isoformat()} to {week.end.isoformat()}"


def _packet_basename(config: GenerateConfig, project_name: str,
                    week: safety_week.SafetyWeek) -> str:
    """Clean per-(job, week) packet base name: `<Job>_week of <Sat>_<suffix>` (no version/ext).

    The suffix is the workstream's (`GenerateConfig.packet_suffix`); safety's default keeps its
    historical `_WSR`. A workstream that changes it starts a FRESH `_vN` sequence for that week —
    the append-only probe looks for the new basename and finds nothing — which is harmless (a
    recompile appends rather than overwrites either way) but means one week can hold a packet
    under both names if the change lands mid-week."""
    return (
        f"{safety_naming.job_folder_name(project_name)}_"
        f"{safety_naming.week_label(week.start)}_{config.packet_suffix}"
    )


def _upload_packet(
    folder_id: str, basename: str, compiled: bytes, stamp: str
) -> tuple[str, str]:
    """Upload the packet as a DISTINCT file with a clean version-numbered name; return
    (filename, box_file_id). First compile → `<basename>.pdf`; recompiles → `_v2`/`_v3`… (the
    next name not already in the folder, by catching the 409). APPEND-ONLY — never overwrites."""
    for n in range(1, _MAX_PACKET_VERSIONS + 1):
        name = f"{basename}.pdf" if n == 1 else f"{basename}_v{n}.pdf"
        try:
            meta = box_client.upload_bytes(folder_id, name, compiled)
            return name, str(meta["id"])
        except box_client.BoxConflictError:
            continue
    name = f"{basename}_{stamp}.pdf"  # ceiling hit (never expected) — never lose the packet
    meta = box_client.upload_bytes(folder_id, name, compiled)
    return name, str(meta["id"])


def _portal_box_root(config: GenerateConfig) -> str:
    """The workstream's Box portal-root folder ID (ITS_Config, config-GATED). Blank/unset → the
    mirror tree is OFF; safety falls back to the legacy project_routing tree, progress (no legacy
    fallback) surfaces a config gap to the per-job fence."""
    return _read_str_setting(config, config.box_root_setting_key, "").strip()


def _ensure_box_week_folder(
    config: GenerateConfig, project_name: str, week: safety_week.SafetyWeek, correlation_id: str
) -> str:
    """Resolve the Box folder the compiled packet files into (ROOT → job → week)."""
    portal_root = _portal_box_root(config)
    if portal_root:
        job_folder = box_client.get_or_create_folder(
            portal_root, safety_naming.job_folder_name(project_name)
        )
        return box_client.get_or_create_folder(
            job_folder, safety_naming.week_label(week.start)
        )
    if not config.box_legacy_fallback:
        raise box_client.BoxError(
            f"no Box portal root for {config.workstream} (ITS_Config "
            f"{config.box_root_setting_key!r} unset) and no legacy fallback"
        )
    root = project_routing.get_folder_id(project_name)
    if not root:
        raise box_client.BoxError(
            f"no Box root for project {project_name!r} (project_routing unresolved)"
        )
    return box_client.get_or_create_folder(root, _its_week_folder_name(week))


# ---- Packet front matter (branded cover + date-grouped index) ------------


def _form_display_name(form_code: str) -> str:
    try:
        definition = form_pdf.load_definition(form_code)
    except Exception:  # noqa: BLE001 — index naming must never break the compile
        definition = None
    if not definition:
        return form_code
    name = definition.get("form_name") or definition.get("branding", {}).get("title") or form_code
    return str(name)


def _work_date_display(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%a, %b %-d, %Y")
    except (ValueError, TypeError):
        return iso or "—"


def _gather_submission_pdfs(
    config: GenerateConfig, submission_rows: list[dict[str, Any]], summary: RunSummary,
    correlation_id: str,
) -> tuple[list[bytes], list[str], list[dict[str, Any]]]:
    """Download each submission's per-submission PDF from Box (by its sheet-recorded link).
    A row with no link / a failed download is SKIPPED + announced (never silently dropped)."""
    pdfs: list[bytes] = []
    manifest: list[str] = []
    metas: list[dict[str, Any]] = []
    for row in submission_rows:
        form_code = str(row.get(week_sheet.COL_FORM_CODE) or "?")
        link = str(row.get(week_sheet.COL_SUBMISSION_PDF) or "")
        file_id = _box_file_id(link)
        if not file_id:
            summary.download_errors += 1
            manifest.append(f"{form_code}[no-box-link]")
            error_log.log(
                Severity.WARN, config.script_name,
                f"compile: submission row has no Box link (form={form_code}); excluded from packet",
                error_code=f"{config.script_name.split('.')[-1]}.submission_no_link",
                correlation_id=correlation_id,
            )
            continue
        try:
            pdfs.append(box_client.download_file(file_id))
            manifest.append(form_code)
            metas.append({
                "date_display": _work_date_display(str(row.get(week_sheet.COL_WORK_DATE) or "")),
                "form_name": _form_display_name(form_code),
            })
        except box_client.BoxError as exc:
            summary.download_errors += 1
            manifest.append(f"{form_code}[download-failed]")
            error_log.log(
                Severity.WARN, config.script_name,
                f"compile: Box download failed for file {file_id} (form={form_code}): {exc!r}; excluded",
                error_code=f"{config.script_name.split('.')[-1]}.submission_download_failed",
                correlation_id=correlation_id,
            )
    return pdfs, manifest, metas


def _week_label(week: safety_week.SafetyWeek) -> str:
    """'Week of Jun 7 – 13, 2026' (collapsed when start/end share a month/year)."""
    s, e = week.start, week.end
    if s.year == e.year and s.month == e.month:
        return f"Week of {s.strftime('%b %-d')} – {e.strftime('%-d, %Y')}"
    if s.year == e.year:
        return f"Week of {s.strftime('%b %-d')} – {e.strftime('%b %-d, %Y')}"
    return f"Week of {s.strftime('%b %-d, %Y')} – {e.strftime('%b %-d, %Y')}"


def _maybe_rollup_page(
    config: GenerateConfig, job: ActiveJob, week: safety_week.SafetyWeek, correlation_id: str,
) -> bytes | None:
    """Build the optional rollup-numbers page (P6) via `config.rollup_page_provider`, FENCED.

    SAFETY (provider None) → returns None immediately with ZERO side effect, so the packet is
    byte-identical (§14). PROGRESS binds a provider that fetches the send-free Worker rollup
    route + renders it. This fence is DISTINCT from (and nested inside) the front-matter fence:
    any provider/fetch/render exception here logs `<slug>.rollup_page_failed` (WARN) and returns
    None → the packet falls back to NO rollup page but KEEPS its cover + index. A rollup failure
    (Worker down, creds unset, transport error) must NEVER break the compile — the numbers page
    is an enhancement, not a gate."""
    provider = config.rollup_page_provider
    if provider is None:
        return None
    try:
        return provider(job, week)
    except Exception as exc:  # noqa: BLE001 — the rollup page must never break the compile
        error_log.log(
            Severity.WARN, config.script_name,
            f"compile: rollup-numbers page failed for {job.project_name} week {week.start} "
            f"({exc!r}); packet compiled WITHOUT the rollup page",
            error_code=f"{config.script_name.split('.')[-1]}.rollup_page_failed",
            correlation_id=correlation_id,
        )
        return None


def _build_weekly_packet(
    config: GenerateConfig, job: ActiveJob, project_name: str, week: safety_week.SafetyWeek,
    pdfs: list[bytes], metas: list[dict[str, Any]], compiled_dt: datetime, correlation_id: str,
) -> bytes:
    """Branded COVER + (optional P6 rollup page) + date-grouped CONTENTS index + the
    per-submission PDFs. FENCED: any front-matter failure degrades to a plain forms-only
    `merge_pdfs(pdfs)`. The optional rollup page (progress only) is spliced AFTER the cover and
    BEFORE the index; its page count is absorbed by the index-pagination convergence loop below
    so the index's absolute page numbers stay correct. Safety binds no provider → `rollup_page`
    is None → the merge list + page math are UNCHANGED (byte-identical, §14)."""
    try:
        week_label = _week_label(week)
        compiled_display = compiled_dt.strftime("%b %-d, %Y %-I:%M %p %Z").strip()
        cover = form_pdf.render_weekly_cover(
            project_name, week_label, len(pdfs), compiled_display=compiled_display,
            title=config.cover_title,
        )
        cover_pages = form_pdf.page_count(cover)
        # P6: optional rollup-numbers page after the cover, before the index (progress only).
        rollup_page = _maybe_rollup_page(config, job, week, correlation_id)
        rollup_pages = form_pdf.page_count(rollup_page) if rollup_page is not None else 0
        counts = [form_pdf.page_count(b) for b in pdfs]

        def make_index(index_pages: int) -> bytes:
            cur = cover_pages + rollup_pages + index_pages + 1
            entries: list[dict[str, Any]] = []
            for m, c in zip(metas, counts, strict=True):
                entries.append({"date_display": m.get("date_display", ""),
                                "form_name": m.get("form_name", ""), "start_page": cur})
                cur += c
            return form_pdf.render_weekly_index(project_name, week_label, entries)

        index_pages = 1
        index_pdf = make_index(index_pages)
        for _ in range(4):
            actual = form_pdf.page_count(index_pdf)
            if actual == index_pages:
                break
            index_pages = actual
            index_pdf = make_index(index_pages)
        else:
            raise RuntimeError("weekly index pagination did not converge")
        front = [cover] if rollup_page is None else [cover, rollup_page]
        return form_pdf.merge_pdfs([*front, index_pdf, *pdfs])
    except Exception as exc:  # noqa: BLE001 — front matter must never break the compile
        error_log.log(
            Severity.WARN, config.script_name,
            f"compile: weekly cover/index build failed ({exc!r}); packet falls back to forms-only",
            error_code=f"{config.script_name.split('.')[-1]}.front_matter_failed",
            correlation_id=correlation_id,
        )
        return form_pdf.merge_pdfs(pdfs)


# ---- Recipient display + review-row write --------------------------------


def _recipient_display(job: ActiveJob) -> tuple[str, str]:
    """(TO display, CC display) — DISPLAY only; the send re-resolves authoritatively at send
    time from the workstream's Active-Jobs sheet. Uses the workstream-neutral contact alias."""
    return job.reports_contact_email, ", ".join(job.cc_emails)


def _write_review_row(
    config: GenerateConfig, job: ActiveJob, week: safety_week.SafetyWeek, *,
    packet_link: str, manifest: str, summary: RunSummary, correlation_id: str,
) -> int:
    """Dual-write (b): APPEND the review-sheet row for (job, week); return its row ID."""
    to_display, cc_display = _recipient_display(job)
    evergreen = _read_str_setting(config, config.cfg_evergreen_contact, config.default_evergreen_contact)
    body = config.email_body_template(
        contact_name=job.reports_contact_name,
        week_label=week.label,
        job_name=job.project_name,
        evergreen_contact=evergreen,
    )
    row_id = config.add_review_row(
        job_project=job.project_name,
        job_id=job.job_id,
        week_of=week.start,
        compiled_pdf_link=packet_link,
        recipient_to=to_display,
        cc_display=cc_display,
        email_body=body,
        notes=manifest,
    )
    summary.wsr_written += 1
    if not to_display:
        # No reports contact on the job → the send will HELD it. Surface now.
        review_queue.add(
            workstream=config.workstream,
            summary=f"weekly compile: job {job.job_id} ({job.project_name}) has no reports contact (TO) for week {week.start}",
            payload={"job_id": job.job_id, "project": job.project_name, "week": week.start.isoformat()},
            sla_tier=config.sla_tier,
            reason=review_queue.ReviewReason.OTHER,
            severity=Severity.WARN,
            source_file=f"{job.job_id}-{week.start.isoformat()}",
        )
        summary.review_queue_entries += 1
    return row_id


def _hold_empty_week(
    config: GenerateConfig, job: ActiveJob, week: safety_week.SafetyWeek,
    review_row_id: int, summary: RunSummary, correlation_id: str,
) -> None:
    """Mark an empty-week review row HELD and raise a Review-Queue item (0067, opt-in).

    `Send Status` takes the UPPERCASE `HELD` already in the picklist registry — the lowercase
    `held_*` strings are RESULT codes that live in Notes (the `weekly_send._mark_held` contract).
    Writing `held_no_activity` into the cell would raise `PicklistViolationError`.

    Best-effort by design: if the status write fails the row simply stays PENDING and the send
    HELDs it later for a missing PDF, which is the pre-0067 behaviour — a degraded outcome, not a
    lost one, so it must never abort a compile that has already written its rows.
    """
    try:
        smartsheet_client.update_rows(
            config.review_sheet_id,
            [{"_row_id": review_row_id, "Send Status": "HELD"}],
        )
    except Exception:  # noqa: BLE001 — a status write must never abort a written-through compile
        error_log.log(
            Severity.WARN, config.script_name,
            f"compile: could not mark the empty-week review row HELD for {job.project_name} "
            f"week {week.start} — it stays PENDING and the send will HELD it for a missing PDF",
            error_code=f"{config.script_name.split(chr(46))[-1]}.empty_week_hold_failed", correlation_id=correlation_id,
        )
    review_queue.add(
        workstream=config.workstream,
        summary=(
            f"weekly compile: job {job.job_id} ({job.project_name}) filed NO daily reports for "
            f"week {week.start} — no client report compiled; the row is HELD for a decision"
        ),
        payload={"job_id": job.job_id, "project": job.project_name,
                 "week": week.start.isoformat(), "result": "held_no_activity"},
        sla_tier=config.sla_tier,
        reason=review_queue.ReviewReason.OTHER,
        severity=Severity.WARN,
        source_file=f"{job.job_id}-{week.start.isoformat()}",
    )
    summary.review_queue_entries += 1


def _maybe_client_report(
    config: GenerateConfig, job: ActiveJob, project_name: str,
    week: safety_week.SafetyWeek, stamp: str, summary: RunSummary, correlation_id: str,
) -> str:
    """Build + file the workstream's client-facing report; return its Box link, or "" .

    FENCED like `_maybe_rollup_page`: any failure — provider raise, Box outage, renderer fault —
    WARNs and returns "", and the caller falls back to putting the field-records packet in the
    review row. A weekly cadence that quietly stops is worse than one that sends the wrong-shaped
    document once with a WARN sitting in ITS_Errors.
    """
    provider = config.client_report_provider
    if provider is None:
        return ""
    try:
        report = provider(job, week)
        if not report:
            return ""
        folder_id = _ensure_box_week_folder(config, project_name, week, correlation_id)
        basename = (
            f"{safety_naming.job_folder_name(project_name)}_"
            f"{safety_naming.week_label(week.start)}_{config.client_report_suffix}"
        )
        _name, file_id = _upload_packet(folder_id, basename, report, stamp)
        summary.client_reports_compiled += 1
        return f"https://app.box.com/file/{file_id}"
    except Exception:  # noqa: BLE001 — the packet fallback keeps the week deliverable
        error_log.log(
            Severity.WARN, config.script_name,
            f"compile: client report FAILED for {project_name} week {week.start} — the review row "
            f"falls back to the field-records packet; the client receives the packet this week",
            error_code=f"{config.script_name.split(chr(46))[-1]}.client_report_failed", correlation_id=correlation_id,
        )
        return ""


def _attach_pdf_best_effort(
    config: GenerateConfig, sheet_id: int, row_id: int, filename: str, pdf_bytes: bytes,
    correlation_id: str,
) -> None:
    """Attach the compiled packet inline on a Smartsheet row, BEST-EFFORT (Box is the SoR)."""
    try:
        smartsheet_client.attach_pdf_to_row(sheet_id, row_id, filename, pdf_bytes)
    except Exception as exc:  # noqa: BLE001 — supplementary inline copy; Box is the SoR
        error_log.log(
            Severity.WARN, config.script_name,
            f"row PDF attach failed (row {row_id}, {filename!r}): {type(exc).__name__}: {exc!r}",
            error_code="row_pdf_attach_failed", correlation_id=correlation_id,
        )


def _write_watchdog_marker(config: GenerateConfig) -> None:
    """Touch the Check C freshness marker for this run."""
    try:
        config.watchdog_marker_dir.mkdir(parents=True, exist_ok=True)
        marker = config.watchdog_marker_dir / f"{config.watchdog_slug}.last_run"
        marker.write_text(datetime.now(UTC).isoformat())
    except OSError as exc:
        error_log.log(
            Severity.WARN, config.script_name,
            f"watchdog marker write failed: {exc!r}",
            error_code="watchdog_marker_failed",
        )


# ---- Per-(job, week) compile --------------------------------------------


def _compile_job_week(
    config: GenerateConfig, job: ActiveJob, week: safety_week.SafetyWeek,
    summary: RunSummary, correlation_id: str, *, selection: set[int] | None = None,
    memory_ceiling: int = 0,
) -> None:
    """Compile one (job, week): merge → file → dual-write Rollup + review row. Raises
    SmartsheetError / BoxError on transient infra failure (the per-job fence catches + routes
    to the Review Queue).

    `selection` (Compile-Now Part-B / on-demand) narrows the PACKET to the given submission
    row IDs; None (the Friday run + a default Compile Now) = the full week. The narrowing
    applies to the merged packet only — the no-new-docs skip below still reads the FULL set."""
    project_name = job.project_name
    sheet_id = week_sheet.ensure_week_sheet(
        config.week_sheet_config, project_name, week.start
    )
    rollup_rows = week_sheet.list_rollup_rows(sheet_id)
    rollup = rollup_rows[-1] if rollup_rows else None
    submissions = week_sheet.list_submission_rows(sheet_id, active_only=True)
    force = week_sheet.any_compile_now_requested(rollup_rows)

    short = config.script_name.split(".")[-1]
    if rollup is not None and not force:
        prior_compiled_at = str(rollup.get(week_sheet.COL_SUBMITTED_AT) or "")
        newest = week_sheet.latest_submitted_at(submissions)
        if not newest and submissions:
            error_log.log(
                Severity.WARN, config.script_name,
                f"compile: {project_name} week {week.start} has submissions with missing/blank "
                f"Submitted At — forcing recompile (cannot prove no-new-docs)",
                error_code=f"{short}.missing_submitted_at", correlation_id=correlation_id,
            )
        elif newest <= prior_compiled_at:
            summary.skipped_no_change += 1
            error_log.log(
                Severity.INFO, config.script_name,
                f"compile: {project_name} week {week.start} already compiled, no new docs — skip",
                error_code=f"{short}.skip_no_change", correlation_id=correlation_id,
            )
            return

    # Part B: narrow the packet to the explicit selection (default-all when None). Placed
    # AFTER the skip check, which intentionally read the full set.
    if selection is not None:
        submissions = [s for s in submissions if int(s.get("_row_id") or 0) in selection]

    compiled_dt = datetime.now(ZoneInfo(DEFAULT_TZ))
    compiled_at = compiled_dt.isoformat()
    stamp = f"{compiled_dt.strftime('%Y%m%d-%H%M%S')}-{correlation_id[:6]}"

    if not submissions:
        summary.empty_weeks += 1
        empty_note = "no submissions this week"
        if config.empty_week_hold:
            # A client-facing lane: do NOT leave a PENDING row that only fails at send time for a
            # missing PDF. Nothing was filed this week — crew demobilized, weather shutdown, or
            # nobody filed — and only a human can tell those apart, so the row is HELD with the
            # reason visible and a Review-Queue item raised. `progress_send_poll` dispatches only
            # {PENDING, FAILED}, so a HELD row can never go out on its own.
            empty_note = (
                "held_no_activity: no daily reports were filed for this week — no client report "
                "was compiled. Decide whether to send a no-activity note, skip the week, or find "
                "out why nothing was filed."
            )
        review_row_id = _write_review_row(
            config, job, week, packet_link="", manifest=empty_note,
            summary=summary, correlation_id=correlation_id,
        )
        if config.empty_week_hold:
            _hold_empty_week(config, job, week, review_row_id, summary, correlation_id)
        week_sheet.append_rollup_row(
            sheet_id, packet_link="", compiled_at=compiled_at,
            manifest_note="0 submissions this week (empty-week placeholder)",
        )
        week_sheet.clear_compile_now_on_rollups(sheet_id, rollup_rows)
        return

    pdfs, manifest_parts, metas = _gather_submission_pdfs(config, submissions, summary, correlation_id)
    compile_core.enforce_memory_budget((len(p) for p in pdfs), memory_ceiling)
    packet_link = ""
    packet_name = ""
    compiled: bytes | None = None
    if pdfs:
        compiled = _build_weekly_packet(config, job, project_name, week, pdfs, metas, compiled_dt,
                                        correlation_id)
        folder_id = _ensure_box_week_folder(config, project_name, week, correlation_id)
        packet_name, file_id = _upload_packet(
            folder_id, _packet_basename(config, project_name, week), compiled, stamp
        )
        packet_link = f"https://app.box.com/file/{file_id}"
        summary.packets_compiled += 1
    else:
        error_log.log(
            Severity.ERROR, config.script_name,
            f"compile: {project_name} week {week.start} had {len(submissions)} submissions "
            f"but ZERO downloadable PDFs — review row written without a packet",
            error_code=f"{short}.no_downloadable_pdfs", correlation_id=correlation_id,
        )

    manifest_note = (
        f"{len(submissions)} submissions ({len(pdfs)} in packet): "
        f"{', '.join(manifest_parts)}; compiled {compiled_at}"
    )

    # The client-facing report (0067), when the workstream binds one. Fenced: a failure here
    # degrades to today's behaviour — the field-records packet in Compiled PDF — and is LOUD.
    # Sending the packet is not ideal, but it is a document the client can read; sending nothing
    # would silently break the weekly cadence.
    report_link = ""
    if config.client_report_provider is not None:
        report_link = _maybe_client_report(config, job, project_name, week, stamp,
                                           summary, correlation_id)
    if report_link:
        # The packet stops being the client's attachment and becomes the internal record. Its
        # link rides Notes so the operator can still reach it from the same row in one click.
        manifest_note = f"{manifest_note}; field-records packet: {packet_link or '(none)'}"

    # Commit-point ordering (A6 resumable watermark, §42): review row FIRST, Rollup LAST.
    review_row_id = _write_review_row(config, job, week, packet_link=report_link or packet_link,
                                      manifest=manifest_note, summary=summary,
                                      correlation_id=correlation_id)
    rollup_row_id = week_sheet.append_rollup_row(
        sheet_id, packet_link=packet_link, compiled_at=compiled_at, manifest_note=manifest_note,
    )
    week_sheet.clear_compile_now_on_rollups(sheet_id, rollup_rows)

    # Inline-attach the packet to BOTH surfaces: the week-sheet ROLLUP row (rollup_row_id) and
    # the review-sheet review row (review_row_id) — two DISTINCT rows on two DISTINCT sheets.
    if compiled is not None:
        _attach_pdf_best_effort(config, sheet_id, rollup_row_id, packet_name, compiled, correlation_id)
        _attach_pdf_best_effort(config, config.review_sheet_id, review_row_id, packet_name,
                                compiled, correlation_id)


def _safe_review_queue(
    config: GenerateConfig, job: ActiveJob, week: safety_week.SafetyWeek, error_class: str,
    correlation_id: str, summary: RunSummary,
) -> None:
    """Surface a per-job compile failure to the Review Queue (never silent).

    DEDUPED per (job, week): a stuck Compile-Now retrying every ~90 s during a Smartsheet
    outage once appended a NEW PENDING row per attempt (189 rows for one job in one day) —
    if a PENDING row already covers this job-week's compile failure, the add is skipped
    with a WARN (never silent). FAIL-OPEN: a dedupe-read failure appends anyway — never
    lose the first signal.
    """
    short = config.script_name.split(".")[-1]
    # The summary is built from this prefix so the dedupe match can never drift from the
    # written row. error_class is OUTSIDE the prefix — a repeat failure of the same
    # job-week is a duplicate even if the exception class differs between attempts.
    dedupe_prefix = (
        f"weekly compile failed for {job.project_name} (job {job.job_id}) week {week.start}"
    )
    try:
        pending = review_queue.get_pending(config.workstream)
        if any(str(row.get("Summary", "")).startswith(dedupe_prefix) for row in pending):
            error_log.log(
                Severity.WARN, config.script_name,
                f"suppressed duplicate Review-Queue entry for {job.project_name} "
                f"(job {job.job_id}) week {week.start} ({error_class}) — a PENDING row "
                f"already covers this compile failure",
                error_code=f"{short}.review_queue_deduped", correlation_id=correlation_id,
            )
            return
    except Exception as exc:  # noqa: BLE001 — FAIL-OPEN: dedupe is best-effort, the add is not
        error_log.log(
            Severity.WARN, config.script_name,
            f"review-queue dedupe read failed for {job.project_name}: {exc!r} — "
            f"appending anyway (fail-open)",
            error_code=f"{short}.review_queue_dedupe_read_failed", correlation_id=correlation_id,
        )
    try:
        review_queue.add(
            workstream=config.workstream,
            summary=f"{dedupe_prefix} ({error_class})",
            payload={
                "job_id": job.job_id, "project": job.project_name,
                "week": week.start.isoformat(), "error_class": error_class,
                "correlation_id": correlation_id,
            },
            sla_tier=config.sla_tier,
            reason=review_queue.ReviewReason.OTHER,
            severity=Severity.ERROR,
            source_file=f"{job.job_id}-{week.start.isoformat()}",
        )
        summary.review_queue_entries += 1
    except Exception as exc:  # noqa: BLE001 — defensive outer catch
        error_log.log(
            Severity.ERROR, config.script_name,
            f"failed to write Review-Queue entry for {job.project_name}: {exc!r}",
            error_code=f"{short}.review_queue_failed", correlation_id=correlation_id,
        )


# ---- Pipeline ------------------------------------------------------------


def run_generate(config: GenerateConfig, *,
                 week_start_override: date | None) -> dict[str, Any]:
    """Compile weekly packets + dual-write Rollup/review-row for each Active job of `config`'s
    workstream. Returns the RunSummary dict + week bounds + correlation id."""
    correlation_id = uuid.uuid4().hex[:12]
    summary = RunSummary()

    anchor = week_start_override if week_start_override is not None else datetime.now(
        ZoneInfo(DEFAULT_TZ)
    ).date()
    week = safety_week.week_bounds(anchor)

    job_timeout = _read_int_setting(config, config.cfg_job_timeout, config.default_job_timeout)
    memory_ceiling = _read_int_setting(config, config.cfg_memory_ceiling, config.default_memory_ceiling)
    short = config.script_name.split(".")[-1]

    def _start(job: ActiveJob) -> None:
        summary.jobs_processed += 1

    def _record(job: ActiveJob, error_class: str, exc: BaseException) -> None:
        summary.errors_per_job[job.project_name] = f"{error_class}: {exc!r}"

    def _on_timeout(job: ActiveJob, error_class: str, exc: BaseException) -> None:
        summary.timed_out += 1
        _record(job, error_class, exc)
        error_log.log(
            Severity.ERROR, config.script_name,
            f"compile timed out for {job.project_name} (job {job.job_id}) week {week.start}: {exc!r}",
            error_code=f"{short}.compile_timeout", correlation_id=correlation_id,
        )
        _safe_review_queue(config, job, week, error_class, correlation_id, summary)

    def _on_infra(job: ActiveJob, error_class: str, exc: BaseException) -> None:
        _record(job, error_class, exc)
        error_log.log(
            Severity.ERROR, config.script_name,
            f"compile failed for {job.project_name} (job {job.job_id}) week {week.start}: {exc!r}",
            error_code=f"{short}.compile_failed", correlation_id=correlation_id,
        )
        _safe_review_queue(config, job, week, error_class, correlation_id, summary)

    def _on_unexpected(job: ActiveJob, error_class: str, exc: BaseException) -> None:
        _record(job, error_class, exc)
        error_log.log(
            Severity.ERROR, config.script_name,
            f"unexpected compile error for {job.project_name}: {exc!r}",
            error_code=f"{short}.compile_unexpected", correlation_id=correlation_id,
        )
        _safe_review_queue(config, job, week, error_class, correlation_id, summary)

    # Serialize against any concurrent compile on the host mutex (P4-core); FAIL-OPEN — run
    # REGARDLESS, a single WARN on contention (blocking the live compile is worse).
    with compile_mutex.hold(role=config.compile_mutex_role):
        compile_core.run_per_job(
            active_jobs.list_active_jobs(config.active_jobs_config),
            lambda job: _compile_job_week(
                config, job, week, summary, correlation_id, memory_ceiling=memory_ceiling
            ),
            fences=compile_core.JobFences(
                on_timeout=_on_timeout,
                on_infra_error=_on_infra,
                on_unexpected=_on_unexpected,
                infra_errors=(smartsheet_client.SmartsheetError, box_client.BoxError),
            ),
            job_timeout_seconds=job_timeout,
            on_job_start=_start,
        )

    _write_watchdog_marker(config)
    return {
        **summary.__dict__,
        "week_start": week.start.isoformat(),
        "week_end": week.end.isoformat(),
        "correlation_id": correlation_id,
    }
