"""Panel: the five ITS Box ROOT folders — config-row + live-resolve validity.

Every filing path in ITS bottoms out on one of five top-level Box folders, each
named by an ITS_Config row: the four per-workstream portal roots (safety,
progress, purchase orders, subcontracts — the latter two their own trees since
the 2026-08-11/12 lane splits) and the Track 6 archive root. An unset row makes
a daemon HOLD its filings (loud), but a WRONG id is the quiet failure class this
panel exists for: filing proceeds into whatever tree the id names, and nothing
else compares the configured id to the folder it actually resolves to. VC-03
asserts the rows are non-empty; only this panel proves each id RESOLVES and
still carries its canonical name.

Read-only and TTL-cached (five Box folder GETs per refresh would otherwise ride
the htmx panel poll). Per-root fail-soft: one unreachable root renders as its
own row while the other four still report. Severity: ERROR when a row is
missing/empty or its id resolves to nothing; WARN when the folder resolves under
a NON-canonical name (a rename is legitimate but worth eyes) or Box is
unreachable; OK when all five resolve under their canonical names.

TOKEN ECONOMICS: the dashboard is the system's only LONG-LIVED process, and Box
refresh tokens are single-use — a daemon's Box touch rotates the Keychain token,
so a client singleton held across sweeps would spend a STALE token at its next
refresh and mint a `box_refresh_token_consumed_retry` WARN every time (turning
the #26 overlap signal into background noise). So the sweep RESETS the module
singleton when it finishes: nothing is held between sweeps, the next one
constructs fresh from the Keychain's current token, and the long TTL keeps that
to at most one exchange per hour with a tab open.

`CANONICAL_ROOTS` mirrors `standup.BOX_ROOT_CONFIG_ROWS` + the archive root —
parity-tested (tests/test_operator_dashboard.py) rather than imported, because
`scripts/migrations/` is not a package and a dashboard panel has no business
importing a tenant-lifecycle migration.
"""
from __future__ import annotations

from operator_dashboard.cache import cached
from operator_dashboard.sources.base import (
    SEV_ERROR,
    SEV_OK,
    SEV_WARN,
    DataSource,
    PanelResult,
    clean,
)

# (canonical folder name, ITS_Config Setting, Workstream cell). The build/seed
# source of truth is scripts/migrations/build_box_roots.py::ROOT_FOLDERS (and
# standup.BOX_ROOT_CONFIG_ROWS for the portal four) — the parity test keeps this
# copy honest.
CANONICAL_ROOTS: tuple[tuple[str, str, str], ...] = (
    ("ITS Safety Reports", "safety_reports.box.portal_root_folder_id", "safety_reports"),
    ("ITS Progress Reports", "progress_reports.box.portal_root_folder_id", "progress_reports"),
    ("ITS Purchase Orders", "po_materials.box.portal_root_folder_id", "po_materials"),
    ("ITS Subcontracts", "subcontracts.box.portal_root_folder_id", "subcontracts"),
    ("ITS Archive", "field_ops.box.archive_root_folder_id", "field_ops"),
)

# Root names change ~never; a long TTL keeps an open dashboard tab from turning
# the panel poll into a steady drip of Box API calls — and bounds the token
# exchanges the sweep-end client reset costs (see the module docstring).
BOX_ROOTS_TTL_SECONDS = 3600


def _resolve_roots() -> list[dict[str, str]]:
    """One row per canonical root: configured id + live resolve outcome."""
    # Imported at call time via the module objects (not from-imports) so tests
    # monkeypatching shared.smartsheet_client / shared.box_client attributes are
    # seen, and so importing the panel registry never touches the network.
    from shared import box_client as box
    from shared import smartsheet_client as ss

    rows: list[dict[str, str]] = []
    try:
        for canonical, setting, workstream in CANONICAL_ROOTS:
            row = {"root": canonical, "setting": setting, "configured id": "",
                   "live name": "", "status": ""}
            try:
                value = (ss.get_setting(setting, workstream=workstream) or "").strip()
            except ss.SmartsheetNotFoundError:
                # get_setting RAISES for a genuinely MISSING (Setting, Workstream)
                # row and returns None only for a present-but-blank Value — a
                # missing row is the invisible-off-switch class this panel exists
                # for (filings HOLD), NOT a reachability blip.
                row["status"] = "config row MISSING — filings HOLD"
                row["_sev"] = SEV_ERROR
                rows.append(row)
                continue
            except Exception as exc:  # noqa: BLE001 — per-root fail-soft; the sheet may be down
                row["status"] = clean(f"config unreadable: {type(exc).__name__}")
                row["_sev"] = SEV_WARN
                rows.append(row)
                continue
            if not value:
                row["status"] = "config row present but EMPTY — filings HOLD"
                row["_sev"] = SEV_ERROR
                rows.append(row)
                continue
            row["configured id"] = clean(value)
            try:
                live_name = box.get_folder_name(value)
            except box.BoxNotFoundError:
                row["status"] = "id resolves to NO folder — filings would fail"
                row["_sev"] = SEV_ERROR
                rows.append(row)
                continue
            except Exception as exc:  # noqa: BLE001 — Box outage/auth; not a config verdict
                row["status"] = clean(f"Box unreachable: {type(exc).__name__}")
                row["_sev"] = SEV_WARN
                rows.append(row)
                continue
            row["live name"] = clean(live_name)
            if live_name == canonical:
                row["status"] = "ok"
                row["_sev"] = SEV_OK
            else:
                row["status"] = f"renamed (expected {canonical!r}) — verify it is the right tree"
                row["_sev"] = SEV_WARN
            rows.append(row)
    finally:
        # Drop the process-wide Box client so this long-lived process never holds
        # a refresh token a daemon may rotate out from under it between sweeps
        # (the module-docstring token-economics note). Private seam on purpose:
        # _reset_client IS box_client's own consumed-token remedy, and there is
        # no public "close" — adding one for a single consumer would grow API
        # surface the daemons must then not misuse.
        box._reset_client()  # noqa: SLF001
    return rows


class BoxRootsSource(DataSource):
    panel_id = "box_roots"
    title = "Box roots"

    def _fetch(self, detail: bool = False) -> PanelResult:
        rows = cached("box_roots", BOX_ROOTS_TTL_SECONDS, _resolve_roots)
        # Default WARN, not OK: a future branch that forgets to stamp _sev must
        # never silently render green.
        sevs = [r.get("_sev", SEV_WARN) for r in rows]
        if SEV_ERROR in sevs:
            severity, summary = SEV_ERROR, "a Box root is missing or does not resolve"
        elif SEV_WARN in sevs:
            severity, summary = SEV_WARN, "a Box root is renamed or unreachable"
        else:
            severity, summary = SEV_OK, "all 5 roots resolve under their canonical names"
        # Rows keep their _sev key — the template tints each row from it (the
        # house convention; keys outside `columns` never render as cells).
        return PanelResult(
            panel_id=self.panel_id,
            title=self.title,
            summary=summary,
            severity=severity,
            columns=["root", "setting", "configured id", "live name", "status"],
            rows=rows,
        )
