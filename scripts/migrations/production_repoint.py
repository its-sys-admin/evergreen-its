"""Apply the CL-12 mirror -> production ITS_Config repoint from the reviewed value map.

THE CUTOVER-DAY REPOINT ACTUATOR (Seth-attended). It applies
docs/operations/production_repoint_changeset.md sections A-D — the reviewed
mirror -> production ITS_Config value sweep — from a declarative data file,
scripts/migrations/production_repoint_map.json, writing each reviewed value
VERBATIM. Rationale (2026-07-23 review): VC-03's sandbox scan proves
absence-of-mirror, not correctness-of-production — a typo'd production value
("evergreenrenewable.com", missing 's') is non-empty, mirror-free, and PASSES
VC-03 while silently mispointing a send mailbox. A tool that writes the
reviewed value verbatim closes the typo class; hand-transcription cannot.

Why the values live in a JSON data file, not Python literals: the CI `secrets`
job's production-identity guard (.gitleaks-identity.toml) blocks any
@evergreenrenewables.com email from re-entering .py/.ts/.tsx source. The
reviewed production identities therefore live in production_repoint_map.json —
a data artifact the guard deliberately scopes out — and this module contains
zero production-identity literals.

Guards (each fails CLOSED, each is separately tested)
    1. STRUCTURAL SECTION-E EXCLUSION + A-D ALLOWLIST. load_map() REFUSES any
       map containing a section-E send-scope setting (``*_enabled``,
       ``*.polling_enabled``, ``scheduled_send_local``) AND refuses any setting
       outside the reviewed section A-D name classes (the ALLOWED_SETTING_*
       allowlist) — so no edit to the JSON can smuggle a send-scope write
       through this tool, including future send-scope names no blocklist could
       anticipate. Gate flips stay CL-13 manual + Seth — a FIXED External-
       Send-Gate high-capability class.
    2. MIRROR-VALUE REFUSAL. load_map() REFUSES any ``to_production`` containing
       the sandbox domain marker (imported from scripts.verify_cutover — single
       source): a repoint map that points AT the mirror is a corrupted map.
    3. PLAN MODE IS THE DEFAULT and never writes. It fetches each row live
       (Setting+Workstream), classifies it, and prints a per-row diff table.
    4. DRIFT REFUSAL. A row whose current value matches NEITHER from_mirror NOR
       to_production is DRIFTED — someone edited the tenant outside the reviewed
       map — and the WHOLE run refuses to commit (investigate first). A missing
       row likewise refuses (this tool repoints existing rows; it never seeds).
       Any refusal aborts before the FIRST write.
    5. DAEMON-DOWN PRECONDITION (--commit). Refuses while any
       org.solutionsmith.its.* launchd job is loaded (the wipe_tenant pattern —
       a live fleet would read half-repointed config mid-sweep).
       --allow-loaded-daemons overrides for an attended MAINTENANCE-window run,
       WARN-loud.
    6. TYPED-PHRASE GATE (--commit). Writing requires typing the exact phrase
       REPOINT TO PRODUCTION at the prompt (the prompt IS the control — there is
       deliberately no flag that bypasses it; EOF declines).

Section-specific mechanics
    A/B/C rows carry ``from_mirror``/``to_production`` verbatim from the
    changeset tables. Section D (Box root folder ids) is resolved LIVE at commit
    via box_client.list_folder("0") exact-name match — refusing ambiguity or
    absence (the standup.py _stage_box_roots pattern) — because a production
    folder id must never be typed by hand. ``system.heartbeat_url`` is genuinely
    unknowable until UptimeRobot provisioning, so it is modeled
    ``prompt_operator: true``: the attended operator is prompted for the value
    at commit time (validated ``https://``); an empty/EOF answer SKIPS the row
    loudly rather than aborting — VC-09 remains the gate that catches an unset
    heartbeat URL, and holding the whole identity sweep hostage to UptimeRobot
    provisioning order would be the wrong coupling.

``--skip-profile phase1-hybrid`` skips exactly the row set named by
scripts.verify_cutover.PROFILES (imported — single source, never duplicated
here, never hand-edited out of the map): the worker_base_url trio that stays on
the mirror Worker during the phase-gated leg. Skipped rows print as
``[skip profile]`` and are never written.

This is a CONFIG-WRITE-ONLY tool: no anthropic / graph / resend / smtplib
anywhere — it has no send capability and no AI step by construction.

Auth: ITS_SMARTSHEET_TOKEN (Keychain) for Smartsheet; the shared Box OAuth
identity via shared/box_client for section-D resolution (commit only).

Run from ~/its (or a worktree):
    python3 scripts/migrations/production_repoint.py                 # read-only plan
    python3 scripts/migrations/production_repoint.py --skip-profile phase1-hybrid
    python3 scripts/migrations/production_repoint.py --commit        # guarded write

Afterward the operator runs (never run automatically by this tool):
    python -m scripts.verify_cutover --only config
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# scripts/ is not a Python package; the same sys.path-insert idiom as
# tests/test_verify_cutover.py imports it as the TOP-LEVEL `verify_cutover`
# (a `from scripts import …` would make mypy see the file under two module
# names — "found twice").
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_cutover  # noqa: E402  — sys.path-driven import

# Family-lib sibling (this dir is sys.path[0] when run as a script; tests insert
# it explicitly). Bounded transient retry for the cutover-day sweep — a 429
# mid-sweep must retry, and exhaustion PROPAGATES (a partial sweep re-runs
# safely: already-production rows classify as no-write).
from _rest_retry import request_with_retry  # noqa: E402

from shared import keychain, sheet_ids  # noqa: E402

# Single sources from verify_cutover — aliased, never duplicated: the named
# profile row-sets and the sandbox-domain marker MUST stay in lock-step with
# the VC-03 gate that later verifies this sweep.
PROFILES = verify_cutover.PROFILES
SANDBOX_DOMAIN_MARKER = verify_cutover.SANDBOX_DOMAIN_MARKER

BASE = "https://api.smartsheet.com/2.0"
CONFIRM_PHRASE = "REPOINT TO PRODUCTION"
MAP_PATH = Path(__file__).with_name("production_repoint_map.json")

VALID_CATEGORIES = frozenset({"A", "B", "C", "D"})

# Guard 1 ALLOWLIST — only the reviewed section A-D setting-name classes may
# load. A blocklist under-approximates (adversarial review 2026-07-23: the
# original *_enabled/polling_enabled blocklist missed scheduled_send_local, and
# a future send-scope name like `send_window_local` would slip through any
# blocklist). Anything not matching refuses — adding a NEW repoint class is
# deliberately a code+data change, never a data-only map edit.
ALLOWED_SETTING_SUFFIXES: tuple[str, ...] = (
    ".worker_base_url",          # section A
    ".from_mailbox",             # section B
    ".portal_root_folder_id",    # section D (resolve_box_root rows)
    ".archive_root_folder_id",   # section D — the Track 6 Box archive root
)
ALLOWED_SETTINGS_EXACT: frozenset[str] = frozenset({
    "system.operator_email",     # section C
    "system.heartbeat_url",      # section C (prompt_operator row)
})

# ---- classifications ------------------------------------------------------

CLASS_ALREADY = "already-production"
CLASS_REPOINT = "will-repoint"
CLASS_DRIFTED = "DRIFTED — REFUSE"
CLASS_MISSING = "row-missing — REFUSE"
CLASS_SKIP = "[skip profile]"
CLASS_BOX = "box-resolve-at-commit"
CLASS_PROMPT = "prompt-at-commit"
CLASS_PROMPT_DECLINED = "skipped — operator declined prompt"

REFUSING_CLASSES = frozenset({CLASS_DRIFTED, CLASS_MISSING})


class RepointRefusedError(RuntimeError):
    """A guard refused the repoint. Nothing (further) was written."""


class MapValidationError(RepointRefusedError):
    """The value map failed schema/structural validation. Nothing was written."""


# ---- map loading + validation ---------------------------------------------


@dataclass(frozen=True)
class RowSpec:
    """One reviewed repoint row from production_repoint_map.json."""

    setting: str
    workstream: str
    from_mirror: str | None
    to_production: str | None
    category: str
    notes: str
    resolve_box_root: str | None = None
    prompt_operator: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.setting, self.workstream)


def _validate_row(raw: dict[str, Any], index: int) -> RowSpec:
    """Validate one raw map row -> RowSpec. Raises MapValidationError on any defect."""
    where = f"map row {index}"
    for req in ("setting", "workstream", "category", "notes"):
        if not isinstance(raw.get(req), str) or not raw[req].strip():
            raise MapValidationError(f"{where}: {req!r} missing or not a non-empty string")
    setting = raw["setting"]
    where = f"map row {index} ({setting!r})"

    # Guard 1 — structural section-E exclusion. Gate flips are CL-13 manual + Seth
    # (FIXED External-Send-Gate class); this tool must be INCAPABLE of carrying one.
    # The changeset's section E names THREE classes: *_enabled, *.polling_enabled,
    # AND scheduled_send_local — all refused by name for a precise message...
    if (setting.endswith("_enabled") or ".polling_enabled" in setting
            or "scheduled_send_local" in setting):
        raise MapValidationError(
            f"{where}: section-E send-scope settings (*_enabled / *.polling_enabled / "
            "scheduled_send_local) are structurally EXCLUDED from this tool — they stay "
            "CL-13 manual + Seth."
        )
    # ...and the ALLOWLIST is the catch-all: only the reviewed A-D name classes
    # load at all, so no future send-scope name can slip through a blocklist.
    if not (setting in ALLOWED_SETTINGS_EXACT
            or any(setting.endswith(suffix) for suffix in ALLOWED_SETTING_SUFFIXES)):
        raise MapValidationError(
            f"{where}: setting {setting!r} is not in the reviewed section A-D allowlist "
            "(worker_base_url / from_mailbox / portal_root_folder_id suffixes, "
            "system.operator_email, system.heartbeat_url) — adding a new repoint class "
            "is a code+data change, never a data-only map edit."
        )

    if raw["category"] not in VALID_CATEGORIES:
        raise MapValidationError(
            f"{where}: category {raw['category']!r} not in {sorted(VALID_CATEGORIES)}")

    from_mirror = raw.get("from_mirror")
    to_production = raw.get("to_production")
    resolve_box_root = raw.get("resolve_box_root")
    prompt_operator = raw.get("prompt_operator", False)
    if from_mirror is not None and not isinstance(from_mirror, str):
        raise MapValidationError(f"{where}: from_mirror must be a string or null")
    if to_production is not None and not isinstance(to_production, str):
        raise MapValidationError(f"{where}: to_production must be a string or null")
    if resolve_box_root is not None and (
            not isinstance(resolve_box_root, str) or not resolve_box_root.strip()):
        raise MapValidationError(f"{where}: resolve_box_root must be a non-empty string or absent")
    if not isinstance(prompt_operator, bool):
        raise MapValidationError(f"{where}: prompt_operator must be a boolean")

    # Guard 2 — a repoint map pointing AT the mirror is corrupted.
    if to_production is not None and SANDBOX_DOMAIN_MARKER in to_production:
        raise MapValidationError(
            f"{where}: to_production contains the sandbox domain marker "
            f"{SANDBOX_DOMAIN_MARKER!r} — a production repoint must never point at the mirror.")

    if to_production is not None:
        if resolve_box_root is not None or prompt_operator:
            raise MapValidationError(
                f"{where}: a row with a literal to_production must not also carry "
                "resolve_box_root / prompt_operator")
        if from_mirror is None:
            raise MapValidationError(
                f"{where}: a literal-value row needs from_mirror (drift detection depends on it)")
    else:
        if bool(resolve_box_root) == bool(prompt_operator):
            raise MapValidationError(
                f"{where}: to_production null requires EXACTLY ONE of "
                "resolve_box_root / prompt_operator")
    if resolve_box_root is not None and raw["category"] != "D":
        raise MapValidationError(f"{where}: resolve_box_root is a section-D mechanism only")

    return RowSpec(
        setting=setting,
        workstream=raw["workstream"],
        from_mirror=from_mirror,
        to_production=to_production,
        category=raw["category"],
        notes=raw["notes"],
        resolve_box_root=resolve_box_root,
        prompt_operator=prompt_operator,
    )


def load_map(path: Path = MAP_PATH) -> list[RowSpec]:
    """Load + validate the value map. Any defect refuses the WHOLE load."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapValidationError(f"cannot read value map {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise MapValidationError(f"{path}: expected a version-1 map object")
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise MapValidationError(f"{path}: 'rows' must be a non-empty list")

    specs: list[RowSpec] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise MapValidationError(f"map row {index}: not an object")
        spec = _validate_row(raw, index)
        if spec.key in seen:
            raise MapValidationError(
                f"map row {index}: duplicate (setting, workstream) pair {spec.key!r}")
        seen.add(spec.key)
        specs.append(spec)
    return specs


# ---- Smartsheet plumbing (raw REST, the seed_daemon_gate_config pattern) ---


def _headers() -> dict[str, str]:
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_json(path: str) -> dict[str, Any]:
    r = request_with_retry("get", BASE + path, headers=_headers(), timeout=60)
    json_body: dict[str, Any] = r.json()
    return json_body


def _put_json(path: str, body: Any) -> dict[str, Any]:
    r = request_with_retry("put", BASE + path, headers=_headers(), json=body, timeout=60)
    json_body: dict[str, Any] = r.json()
    return json_body


def _get_sheet() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch ITS_Config (columns + rows). The single live-read seam; tests mock it."""
    sheet = _get_json(f"/sheets/{sheet_ids.SHEET_CONFIG}?include=columns")
    return sheet["columns"], sheet["rows"]


def _find_config_row(
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    setting: str,
    workstream: str,
) -> dict[str, Any] | None:
    col_id_by_title = {c["title"]: c["id"] for c in columns}
    setting_col = col_id_by_title["Setting"]
    workstream_col = col_id_by_title["Workstream"]
    for row in rows:
        s = w = None
        for cell in row.get("cells", []):
            if cell.get("columnId") == setting_col:
                s = cell.get("value")
            elif cell.get("columnId") == workstream_col:
                w = cell.get("value")
        if s == setting and w == workstream:
            return row
    return None


def _row_value(row: dict[str, Any], columns: list[dict[str, Any]]) -> Any:
    value_col = next(c["id"] for c in columns if c["title"] == "Value")
    for cell in row.get("cells", []):
        if cell.get("columnId") == value_col:
            return cell.get("value")
    return None


def _write_value(row_id: int, value_column_id: int, value: str) -> None:
    """THE write seam — the only call that mutates the tenant. Tests boom-patch it."""
    _put_json(
        f"/sheets/{sheet_ids.SHEET_CONFIG}/rows",
        [{"id": row_id, "cells": [{"columnId": value_column_id, "value": value}]}],
    )


# ---- classification --------------------------------------------------------


@dataclass
class RowState:
    """A map row joined to its live ITS_Config row and classified."""

    spec: RowSpec
    row_id: int | None
    current: str | None
    classification: str
    planned: str | None  # the value a commit would write (None = no write)


def classify_row(
    spec: RowSpec,
    current: str | None,
    skip_keys: frozenset[tuple[str, str]],
) -> str:
    """Classify one row. Pure — the whole refusal matrix hangs off this.

    A BLANK current value ("") deliberately classifies DRIFTED, not missing: the
    row exists but someone half-wrote it — investigate, don't overwrite blind.
    """
    if spec.key in skip_keys:
        return CLASS_SKIP
    if current is None:
        return CLASS_MISSING
    if spec.resolve_box_root is not None:
        return CLASS_BOX
    if spec.prompt_operator:
        return CLASS_PROMPT
    if current == spec.to_production:
        return CLASS_ALREADY
    if current == spec.from_mirror:
        return CLASS_REPOINT
    return CLASS_DRIFTED


def gather(
    specs: list[RowSpec],
    skip_keys: frozenset[tuple[str, str]],
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[RowState]:
    states: list[RowState] = []
    for spec in specs:
        row = _find_config_row(rows, columns, spec.setting, spec.workstream)
        current: str | None
        row_id: int | None
        if row is None:
            current, row_id = None, None
        else:
            raw_value = _row_value(row, columns)
            current = "" if raw_value is None else str(raw_value)
            row_id = row["id"]
        classification = classify_row(spec, current, skip_keys)
        planned = spec.to_production if classification == CLASS_REPOINT else None
        states.append(RowState(spec=spec, row_id=row_id, current=current,
                               classification=classification, planned=planned))
    return states


def _print_table(states: list[RowState], heading: str) -> None:
    print(f"\n{heading}")
    print(f"  {'§':<2} {'Setting [Workstream]':<62} {'classification':<28} current -> planned")
    for st in states:
        label = f"{st.spec.setting} [{st.spec.workstream}]"
        planned = st.planned if st.planned is not None else "-"
        print(f"  {st.spec.category:<2} {label:<62} {st.classification:<28} "
              f"{st.current!r} -> {planned!r}")
        if st.classification == CLASS_SKIP and st.current is None:
            print(f"     [WARN] {label} is profile-skipped but MISSING on the tenant — "
                  "not this tool's to fix, but verify_cutover will flag it.")


# ---- commit-time guards + resolvers ----------------------------------------


def _loaded_its_daemons() -> list[str]:
    """Labels of loaded org.solutionsmith.its.* launchd jobs (wipe_tenant pattern)."""
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    return sorted(
        line.split()[-1] for line in out.splitlines()
        if "org.solutionsmith.its." in line
    )


def _require_daemons_down(allow_loaded_daemons: bool) -> None:
    loaded = _loaded_its_daemons()
    if not loaded:
        return
    if allow_loaded_daemons:
        print(f"[WARN] --allow-loaded-daemons: {len(loaded)} org.solutionsmith.its.* job(s) "
              "are STILL LOADED. A daemon cycling mid-sweep will read half-repointed config. "
              "Proceeding only because this is an attended MAINTENANCE-window run:")
        for label in loaded:
            print(f"    [WARN]   {label}")
        return
    print(f"[abort] daemons_loaded: {len(loaded)} org.solutionsmith.its.* job(s) are still "
          "loaded — a live fleet would read half-repointed config mid-sweep. Unload them "
          "first (or, for an attended MAINTENANCE-window run, pass --allow-loaded-daemons):")
    for label in loaded:
        print(f"    launchctl bootout gui/$(id -u)/{label}")
    raise RepointRefusedError("daemons loaded")


def _confirm_phrase() -> bool:
    """The typed-phrase gate. Tests monkeypatch this seam. EOF = decline."""
    print(f'\nType exactly "{CONFIRM_PHRASE}" to write the values above.')
    try:
        return input("> ").strip() == CONFIRM_PHRASE
    except EOFError:
        return False


def _resolve_box_root(folder_name: str) -> str:
    """Resolve a Box ROOT folder id by exact name (standup._stage_box_roots pattern).

    Refuses ambiguity and absence — a production folder id is never guessed.
    """
    from shared import box_client  # lazy: Box auth only when a commit reaches section D

    all_roots = [i for i in box_client.list_folder("0", limit=1000)
                 if i.get("type") == "folder"]
    matches = [str(i.get("id")) for i in all_roots if i.get("name") == folder_name]
    if len(matches) > 1:
        raise RepointRefusedError(
            f"Box root {folder_name!r} is AMBIGUOUS ({len(matches)} folders: {matches}) — "
            "reconcile before the repoint")
    if not matches:
        raise RepointRefusedError(f"Box root {folder_name!r} not found on this Box account")
    return matches[0]


def _prompt_operator_value(spec: RowSpec) -> str | None:
    """Prompt the attended operator for a value unknowable at map-authoring time.

    Currently only system.heartbeat_url uses this. Validates https://; loops on
    an invalid answer; empty answer or EOF returns None (= skip the row loudly).
    """
    print(f"\n{spec.setting} [{spec.workstream}]: {spec.notes}")
    while True:
        try:
            answer = input("Enter the production value (https://…), or blank to skip: ").strip()
        except EOFError:
            return None
        if not answer:
            return None
        if not answer.startswith("https://"):
            print("[WARN] value must start with https:// — try again (blank to skip).")
            continue
        return answer


# ---- plan / commit ---------------------------------------------------------


def _print_verify_instruction() -> None:
    print("\nNext (operator-gated — this tool never runs it for you):")
    print("    python -m scripts.verify_cutover --only config")


def run_plan(specs: list[RowSpec], skip_keys: frozenset[tuple[str, str]]) -> int:
    """PLAN mode (default): fetch + classify + print. Never writes."""
    columns, rows = _get_sheet()
    states = gather(specs, skip_keys, columns, rows)
    _print_table(states, "PLAN (read-only — nothing was written):")
    refusals = [st for st in states if st.classification in REFUSING_CLASSES]
    if refusals:
        print(f"\n[abort-preview] {len(refusals)} row(s) would REFUSE a commit "
              "(drifted/missing — investigate before the sweep):")
        for st in refusals:
            print(f"    {st.spec.setting} [{st.spec.workstream}]: {st.classification}")
        return 1
    print("\nPlan is clean: a --commit run would proceed to the typed-phrase gate.")
    _print_verify_instruction()
    return 0


def run_commit(
    specs: list[RowSpec],
    skip_keys: frozenset[tuple[str, str]],
    allow_loaded_daemons: bool,
) -> int:
    """COMMIT mode: guards -> classify -> resolve/prompt -> phrase -> write."""
    _require_daemons_down(allow_loaded_daemons)

    columns, rows = _get_sheet()
    states = gather(specs, skip_keys, columns, rows)
    _print_table(states, "COMMIT plan:")

    # Guard 4 — zero drifted/missing rows, checked BEFORE the first write (and
    # before Box/prompt resolution: a drifted tenant means investigate, full stop).
    refusals = [st for st in states if st.classification in REFUSING_CLASSES]
    if refusals:
        print(f"\n[abort] {len(refusals)} row(s) refuse the sweep — NOTHING was written:")
        for st in refusals:
            print(f"    {st.spec.setting} [{st.spec.workstream}]: {st.classification} "
                  f"(current={st.current!r})")
        raise RepointRefusedError("drifted or missing rows")

    # Section D: resolve each Box root live — refuse ambiguity/absence (pre-write).
    for st in states:
        if st.classification != CLASS_BOX:
            continue
        resolved = _resolve_box_root(st.spec.resolve_box_root or "")
        if st.current == resolved:
            st.classification = CLASS_ALREADY
            st.planned = None
        else:
            st.planned = resolved

    # Prompted rows (system.heartbeat_url): attended operator supplies the value.
    for st in states:
        if st.classification != CLASS_PROMPT:
            continue
        answer = _prompt_operator_value(st.spec)
        if answer is None:
            st.classification = CLASS_PROMPT_DECLINED
            st.planned = None
            print(f"[WARN] {st.spec.setting} [{st.spec.workstream}] SKIPPED — no value "
                  "provided. The row keeps its current value; VC-09 remains the gate.")
        elif st.current == answer:
            st.classification = CLASS_ALREADY
            st.planned = None
        else:
            st.planned = answer

    writes = [st for st in states if st.planned is not None]
    _print_table(states, "FINAL write set (rows with a planned value will be written):")
    if not writes:
        print("\nNothing to write — every row is already at its production value or skipped.")
        _print_verify_instruction()
        return 0

    # Guard 6 — the typed phrase is the LAST control before the first write.
    if not _confirm_phrase():
        print("[abort] phrase not confirmed — NOTHING was written.")
        return 1

    value_column_id = next(c["id"] for c in columns if c["title"] == "Value")
    for st in writes:
        assert st.row_id is not None and st.planned is not None  # refusals ran above
        _write_value(st.row_id, value_column_id, st.planned)
        print(f"[ok] {st.spec.setting} [{st.spec.workstream}] -> {st.planned!r}")

    print(f"\n[done] {len(writes)} row(s) repointed.")
    _print_verify_instruction()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the CL-12 mirror->production ITS_Config repoint "
                    "(plan by default; --commit is phrase-gated).")
    parser.add_argument("--commit", action="store_true",
                        help="write the repoint (guards: daemons-down, zero drift, typed phrase)")
    parser.add_argument("--skip-profile", choices=sorted(PROFILES), default=None,
                        help="skip exactly the named scripts.verify_cutover.PROFILES row set "
                             "(e.g. phase1-hybrid: the worker_base_url trio staying mirror)")
    parser.add_argument("--allow-loaded-daemons", action="store_true",
                        help="override the daemons-down guard for an attended "
                             "MAINTENANCE-window run (WARN-loud)")
    parser.add_argument("--map", type=Path, default=MAP_PATH,
                        help="value-map path (default: the reviewed repo map; the same "
                             "validation applies to any map)")
    args = parser.parse_args(argv)

    skip_keys: frozenset[tuple[str, str]] = (
        PROFILES[args.skip_profile] if args.skip_profile else frozenset())

    try:
        specs = load_map(args.map)
        print(f"[info] value map: {args.map} ({len(specs)} rows) — "
              f"ITS_Config sheet {sheet_ids.SHEET_CONFIG}")
        if args.skip_profile:
            print(f"[info] --skip-profile {args.skip_profile}: "
                  f"{len(skip_keys)} row(s) exempt (scripts.verify_cutover.PROFILES)")
        if args.commit:
            return run_commit(specs, skip_keys, args.allow_loaded_daemons)
        return run_plan(specs, skip_keys)
    except RepointRefusedError as exc:
        print(f"[abort] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
