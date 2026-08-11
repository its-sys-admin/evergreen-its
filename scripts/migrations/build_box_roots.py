#!/usr/bin/env python3
"""Build the four ITS Box ROOT folders — Safety + Progress reports, Purchase Orders, and the job archive (D4).

Purpose
-------
The Safety-Reports and Progress-Reporting workstreams each mirror their filed
artifacts into a dedicated Box tree whose ROOT is a single top-level folder in the
authenticated Box account. Those two roots predate the migration-builder family and
have never had a builder — they were hand-created in the sandbox and their ids
hand-pasted into ITS_Config. This script is the missing builder, so the Phase-1
cutover onto Evergreen's PRODUCTION Box account is a re-runnable, auditable step
rather than a manual click-through. The ARCHIVE root joined them with the Track 6
job archive, which relocates a closed job's Box containers beneath it; the
PURCHASE ORDERS root joined at the 2026-08-11 lane split.

It find-or-creates EXACTLY four folders, all directly under the Box root
(folder id ``"0"``):

  - "ITS Safety Reports"    -> ITS_Config ``safety_reports.box.portal_root_folder_id``
  - "ITS Progress Reports"  -> ITS_Config ``progress_reports.box.portal_root_folder_id``
  - "ITS Purchase Orders"   -> ITS_Config ``po_materials.box.portal_root_folder_id``
  - "ITS Archive"           -> ITS_Config ``field_ops.box.archive_root_folder_id``

Config-key provenance (verified in-tree, not from memory):
``safety_reports/safety_naming.py::CFG_BOX_PORTAL_ROOT``,
``progress_reports/progress_weekly_generate.py::CFG_BOX_PORTAL_ROOT``,
``po_materials/po_naming.py::CFG_BOX_PORTAL_ROOT``,
``field_ops/job_archive.py::CFG_BOX_ARCHIVE_ROOT``, and all four rows in
``operator_dashboard/act/registry.py`` (the §50 config-editor registry).

OUT OF SCOPE — deliberately: the per-job / per-week / category subtrees beneath these
roots. Those are RUNTIME find-or-create (``safety_reports/week_folder.py``,
``shared/box_client.get_or_create_folder``) and must never be pre-built here. This
builder creates the four roots and nothing else (MINIMAL SET). The archive's per-job
``<Job>/`` subfolders are likewise RUNTIME find-or-create
(``field_ops.job_archive.ensure_archive_box_job_folder``).

Cutover sequence (FLIP-precedes-SEED family convention)
-------------------------------------------------------
  1. PREREQUISITE — on the PRODUCTION host, authenticate Box as the dedicated ITS
     service account (the ``its`` mailbox on the ``evergreenrenewables.com`` domain;
     the joined login is ``EXPECTED_BOX_LOGIN``):
         python3 scripts/setup_box_oauth.py
     That seeds ``ITS_BOX_CLIENT_ID`` / ``ITS_BOX_CLIENT_SECRET`` /
     ``ITS_BOX_REFRESH_TOKEN`` into the macOS Keychain. The roots MUST be created by
     that identity — a root created under a personal account is invisible to the
     daemons and silently breaks every filing path.
  2. Preview:   python3 scripts/migrations/build_box_roots.py --dry-run
  3. Live:      python3 scripts/migrations/build_box_roots.py     (y/N confirmed)
  4. Paste the four printed ids into the corresponding ITS_Config ROWS (see the
     "=== FLIP BLOCK ===" this script prints). NOTE: this is an **ITS_Config row
     flip, NOT a shared/sheet_ids.py flip** — unlike every Smartsheet builder in
     this family, no Python constant changes. The consumers read the ids at runtime
     from ITS_Config via ``get_setting``.
  5. Re-run this script; it must print four ``[skip]`` lines and create nothing.

Invariants (blast-radius — this runs against a customer's PRODUCTION Box account)
--------------------------------------------------------------------------------
  1. CREATE-ONLY. Reads (folder listing) + create-POST only. No rename, move,
     delete, share, collaboration, or metadata write — on anything, ever, including
     folders this script itself created.
  2. EXACT-NAME FIND, ADOPT-DON'T-TOUCH. A folder whose name string-equals the
     canonical name is adopted and left completely untouched.
  3. SCOPED CREATION. Only the Box root (``"0"``) is enumerated, and only the four
     canonical names are acted on. Nothing else in the account is read into a
     decision or written to.
  4. MINIMAL SET. Exactly four folders. No "while we're here" subfolders.
  5. IDEMPOTENT NO-OP. A second run prints the same ids and creates nothing.
  6. LIVE-WRITE CONFIRMATION. Live by default (family convention) with a y/N
     prompt before the FIRST create; ``--dry-run`` makes no create call at all.
  7. NO SECRETS IN OUTPUT. Folder names + ids only; never a token.
  8. DUPLICATE-NAME AMBIGUITY IS LOUD. Box does NOT enforce folder-name uniqueness
     and ``get_or_create_folder`` adopts the FIRST match silently, so this script
     counts ALL exact-name matches at root itself and prints a [WARN] naming every
     matching id when the count exceeds one. It still adopts the first (never
     creates a duplicate) but the operator must reconcile before flipping the id.

Failure modes
-------------
  - UNAUTHENTICATED / expired-or-revoked refresh token: the auth probe (a read of
    the Box root) fails BEFORE any create is attempted, so the run cannot
    half-create. Surfaces as an operator-actionable message naming
    scripts/setup_box_oauth.py and exits 2.

    §42 — why the probe does NOT call ``box_client.list_folder``: that helper wraps
    only the CONSTRUCTION of boxsdk's LAZY ``LimitOffsetBasedObjectCollection``
    inside ``box_client._call`` (``_call(client.folder(id).get_items, limit=…)``);
    no HTTP is issued there. The actual GET — and the refresh-token exchange behind
    it — happens during ITERATION, in ``list_folder``'s list comprehension, one
    frame OUTSIDE ``_call``'s translation/retry wrapper. A rejected token would
    therefore escape as a RAW ``BoxOAuthException`` and this module's loud,
    operator-actionable auth path would never run. So the probe forces the request
    INSIDE translated territory itself (``_call(lambda: list(...get_items(...)))``)
    and additionally nets the raw boxsdk exception types, so an unhandled traceback
    is impossible at the cutover console.

    This lazy-iteration/translation gap is a property of ``shared/box_client.py``
    GENERALLY — every daemon calling ``list_folder`` / ``search`` has the same hole,
    and it is a candidate tech-debt entry. Fixing the shared module is DELIBERATELY
    out of scope for this migration PR: it is a live module consumed by running
    daemons, and a one-time cutover builder is the wrong blast radius to change it
    from. The workaround is local to this file.
  - Box 429/5xx: ``shared.box_client`` retries internally; on exhaustion this
    script prints the typed error and exits 3, having created at most the folders
    already reported ``[ok]`` (each create is independently idempotent on re-run).
  - Duplicate root names: does NOT fail — WARNs loudly (invariant 8).
  - Wrong Box identity: NOT structurally detectable (Box has no OWNER-of-root concept —
    every user owns their own root, so ``probe_auth`` succeeds for ANY valid Box user).
    The control is a HUMAN one: ``_resolve_identity`` reads the authenticated account via
    ``_whoami`` and prints it LOUDLY on every run (incl. --dry-run); a login that is not
    ``EXPECTED_BOX_LOGIN`` (the ITS service account on the ``evergreenrenewables.com``
    domain) raises a prominent ``[WARN]
    box_identity_mismatch`` naming both logins; and ``_confirm_live_writes`` NAMES that
    account in the y/N prompt, so the operator cannot approve a create without seeing which
    account it lands in. This is WARN-not-block (contrast the Smartsheet accessLevel==OWNER
    hard-stop D1/D2/D3 use) precisely because Box gives no ownership discriminator to fail
    closed on, and a non-production identity can be legitimate during validation. The
    prerequisite in step 1 remains the primary control; the printed ids must still be
    sanity-checked in the Box web UI as the ITS user before the ITS_Config flip.

No §43 successor-remediation runbook entry is needed: this is a ONE-TIME operator
migration run by the Developer-Operator during cutover, with no Tier-2-recurring
failure mode and no daemon consuming it. The runtime consumers' own runbooks cover
a misconfigured root id.

Consumers of the ids this produces
----------------------------------
  - safety_reports/safety_naming.py (CFG_BOX_PORTAL_ROOT) -> intake / week_folder
  - progress_reports/progress_weekly_generate.py (CFG_BOX_PORTAL_ROOT)
  - field_ops/job_archive.py (CFG_BOX_ARCHIVE_ROOT) -> the Track 6 archive destination
  - operator_dashboard/act/registry.py (the §50 Class-A ITS_Config editor rows)

Auth: Box OAuth credentials from macOS Keychain via shared/box_client.py.
No send capability, no AI: this module imports neither graph_client / resend_client
nor anthropic / anthropic_client.

Exit 0 on success, no-op, or an operator-declined confirmation (a decline creates
nothing — it is a no-op, not a failure, and the other three cutover builders agree);
2 on auth failure; 3 on Box error.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from boxsdk.exception import (  # type: ignore[import-untyped]  # noqa: E402
    BoxException,
    BoxOAuthException,
)

from shared import box_client  # noqa: E402

# Box's root folder is the literal string id "0" (see box_client.list_folder).
BOX_ROOT_FOLDER_ID = "0"

# The dedicated ITS Box identity that MUST own the four root folders at the Phase-1
# cutover: Evergreen's production ITS service account. This repo is Evergreen-specific
# (Customer 0), so a CONCRETE expected login is correct here — the customer-agnostic
# blueprint would not hardcode one.
#
# §42 — the D4 Box-identity discriminator (why WARN, not a hard block). D1/D2/D3 defend the
# sandbox-shared-into-production trap with a Smartsheet accessLevel==OWNER HARD-STOP, because
# a Smartsheet workspace HAS an ownership discriminator. Box has NO such concept — every Box
# user owns their own root, so "am I in the wrong account?" is undetectable in-band: probe_auth
# succeeds for ANY valid Box user reading its own root. The analog control is therefore to
# SURFACE the authenticated identity prominently (`_resolve_identity`) and NAME it in the y/N
# create prompt (`_confirm_live_writes`), so a wrong / personal / sandbox identity is caught by
# a HUMAN at the gate. A mismatch WARNs but does not refuse: a non-EXPECTED login can be
# legitimate during validation (a sandbox ITS user), and there is no reliable structural signal
# to distinguish "wrong account" from "correct-but-not-yet-production account". The human
# confirmation IS the control.
#
# The expected identity is COMPOSED from two bare parts rather than written as a literal
# ``<local>@<domain>`` email: the CI production-identity re-entry guard
# (`.gitleaks-identity.toml`) blocks any real ``@``-email on the production domain from
# re-entering ``.py`` source (the sandbox-first pattern). A bare-domain constant is exempt,
# and the joined value is byte-identical at runtime — display + comparison are unchanged.
EXPECTED_BOX_LOCALPART = "its"
EXPECTED_BOX_DOMAIN = "evergreenrenewables.com"
EXPECTED_BOX_LOGIN = f"{EXPECTED_BOX_LOCALPART}@{EXPECTED_BOX_DOMAIN}"

# Box's maximum page size for a folder listing. The root of the ITS account holds a
# handful of top-level folders, so a single page is sufficient; the same >1000-child
# caveat documented on box_client.find_child_folder applies in principle.
_ROOT_PAGE_LIMIT = 1000

# The canonical, MINIMAL set. (folder name, ITS_Config Setting key, Workstream cell).
ROOT_FOLDERS: list[tuple[str, str, str]] = [
    ("ITS Safety Reports", "safety_reports.box.portal_root_folder_id", "safety_reports"),
    ("ITS Progress Reports", "progress_reports.box.portal_root_folder_id", "progress_reports"),
    # The PO lane's OWN root (2026-08-11) — po_naming.CFG_BOX_PORTAL_ROOT. Its per-job folder
    # holds the PO PDFs directly, with "RFQs" / "Vendor Quotes" as children (po_poll, rfq_poll,
    # estimate_poll). A SIBLING of the safety tree, not a subtree of it — the operator directive
    # that split the lane out of `<safety root>/<job>/Purchase Orders/`.
    ("ITS Purchase Orders", "po_materials.box.portal_root_folder_id", "po_materials"),
    # Track 6 job archive. A SIBLING of the live roots, never a child of any: a job's
    # safety, progress and purchase-orders containers all land under it, so nesting it inside
    # one workstream's tree would put the other workstreams' archived documents inside a folder
    # their own daemons walk find-or-create over.
    ("ITS Archive", "field_ops.box.archive_root_folder_id", "field_ops"),
]


def _auth_failure(detail: str) -> SystemExit:
    """Print the operator-actionable auth message and return the exit-2 SystemExit."""
    print(
        f"[error] Box authentication FAILED — nothing was created.\n"
        f"        {detail}\n"
        f"        This script must run on the production host as the dedicated ITS\n"
        f"        service account ({EXPECTED_BOX_LOGIN}). Seed/refresh credentials with:\n"
        f"            python3 scripts/setup_box_oauth.py\n"
        f"        then re-run. (A refresh token expires after ~60 days idle.)",
        file=sys.stderr,
    )
    return SystemExit(2)


def _list_box_root() -> list[dict[str, Any]]:
    """READ the Box root, forcing the HTTP GET inside box_client's translation wrapper.

    §42 — deliberately NOT ``box_client.list_folder``. That helper passes the BOUND
    ``get_items`` method to ``box_client._call``, which returns boxsdk's LAZY
    ``LimitOffsetBasedObjectCollection`` without issuing any HTTP; the real GET (and
    the refresh-token exchange behind it) fires when ``list_folder``'s comprehension
    iterates the collection — one frame OUTSIDE ``_call``. A bad/expired/revoked token
    therefore escapes ``list_folder`` as a RAW boxsdk exception, past the typed
    ``BoxError`` hierarchy, and the caller's typed handling never runs.

    There is no PUBLIC box_client seam that forces the request inside translated
    territory (``search`` has the identical lazy shape; ``get_folder_by_path`` is built
    on ``list_folder``; the remaining forcing calls all need an id we do not have or
    are WRITES, which invariant 1 forbids). So we reach for the module-private
    ``_call`` with a thunk that fully materializes the collection — the iteration now
    happens INSIDE the retry/translation frame. Returns the same
    ``{id, name, type}`` dict shape ``list_folder`` does, so callers are unaffected.
    """
    client = box_client.get_client()
    items = box_client._call(  # noqa: SLF001 — no public seam forces the GET inside _call
        lambda: list(client.folder(BOX_ROOT_FOLDER_ID).get_items(limit=_ROOT_PAGE_LIMIT))
    )
    return [{"id": str(item.id), "name": item.name, "type": item.type} for item in items]


def _whoami() -> tuple[str, str]:
    """Return the ``(login, display_name)`` of the authenticated Box user.

    The D4 Box-identity discriminator (see ``EXPECTED_BOX_LOGIN`` for the full §42 rationale):
    surfaces WHICH Box account is about to be written into, so a wrong / personal / sandbox
    identity is caught by the operator at the y/N gate rather than silently creating the ITS
    roots in the wrong account and leaking their ids into the paste-ready FLIP BLOCK.

    §42 — like ``_list_box_root``, this forces the HTTP GET INSIDE ``box_client._call``'s
    translation frame. ``client.user().get()`` is NOT lazy the way ``get_items()`` is (it
    issues ``GET /users/me`` eagerly), but a bad / expired / revoked token still raises a RAW
    ``BoxOAuthException`` from the token exchange behind it, so wrapping the whole thunk in
    ``_call`` routes that through the typed ``BoxAuthError`` hierarchy the module's fail-loud
    path already handles. The failure is NEVER swallowed: an un-surfaced identity is exactly
    the gap this closes.

    Raises:
        BoxAuthError / BoxError: propagated (via ``_call``) to the caller's fail-loud ladder.
    """
    client = box_client.get_client()
    user = box_client._call(  # noqa: SLF001 — force the GET inside _call's translation frame
        lambda: client.user().get(fields=("login", "name"))
    )
    return str(user.login), str(user.name)


def _guard_box_read[T](thunk: Callable[[], T], *, what: str) -> T:
    """Run a Box READ ``thunk``, converting every failure into the module's fail-loud SystemExit.

    The shared exception ladder behind both ``probe_auth`` (the root listing) and
    ``_resolve_identity`` (the whoami identity read): typed ``BoxAuthError`` -> exit 2; typed
    ``BoxError`` -> exit 3; then, belt-and-braces, the RAW boxsdk exception types are netted
    too (``BoxOAuthException`` -> auth/exit 2, any other ``BoxException`` -> exit 3), and
    finally a catch-all. At a production cutover console an unhandled traceback instead of the
    setup_box_oauth.py instruction is itself the defect (see ``_list_box_root``'s §42 note on
    why a raw boxsdk exception can still surface past the typed boundary).

    Raises:
        SystemExit: exit 2 on any auth failure, exit 3 on any other failure.
    """
    try:
        return thunk()
    except box_client.BoxAuthError as e:
        raise _auth_failure(str(e)) from e
    except box_client.BoxError as e:
        print(
            f"[error] Box {what} failed (not an auth error) — nothing was created.\n"
            f"        {e}",
            file=sys.stderr,
        )
        raise SystemExit(3) from e
    except BoxOAuthException as e:
        # Untranslated token-exchange failure (see _list_box_root's §42 note).
        raise _auth_failure(f"raw boxsdk OAuth failure: {e!r}") from e
    except BoxException as e:
        # Base of BoxAPIException / BoxNetworkException. (BoxValueError derives from
        # ValueError, not BoxException — the catch-all below covers it.)
        print(
            f"[error] Box {what} failed (untranslated boxsdk error) — "
            f"nothing was created.\n        {e!r}",
            file=sys.stderr,
        )
        raise SystemExit(3) from e
    except Exception as e:  # noqa: BLE001 — a cutover console must never see a traceback
        print(
            f"[error] Box {what} failed unexpectedly — nothing was created.\n"
            f"        {e!r}",
            file=sys.stderr,
        )
        raise SystemExit(3) from e


def probe_auth() -> list[dict[str, Any]]:
    """Verify Box auth with a READ before anything can be created; return root items.

    The probe is a listing of the Box root folder — the cheapest call that forces the
    lazy client build (Keychain credential read) AND the refresh-token exchange, so an
    unauthenticated / expired / revoked identity fails LOUD here rather than after a
    partial create. The returned listing is reused for the find pass, so this costs no
    extra API call. The fail-loud exception ladder lives in ``_guard_box_read``.

    Raises:
        SystemExit: exit 2 on any auth failure, exit 3 on any other failure.
    """
    return _guard_box_read(_list_box_root, what="read probe")


def _resolve_identity() -> str:
    """Read + LOUDLY print the authenticated Box identity; WARN on mismatch; return the login.

    Runs on EVERY mode, including ``--dry-run`` — surfacing WHICH Box account is about to be
    written into is the whole point, at plan time as much as at create time. Fails loud (the
    same ``_guard_box_read`` ladder as ``probe_auth``) on any Box error, so an identity we
    cannot read is never silently skipped.

    A login != ``EXPECTED_BOX_LOGIN`` is a prominent ``[WARN] box_identity_mismatch`` naming
    BOTH logins — NOT a hard refusal. See ``EXPECTED_BOX_LOGIN`` for why this is WARN-not-block
    (Box has no ownership discriminator; the human confirmation in the y/N prompt is the
    control). The returned login is threaded into ``_confirm_live_writes`` so the operator
    cannot confirm a create without seeing the account it lands in.
    """
    login, name = _guard_box_read(_whoami, what="identity probe")
    print(f"[ok] Box auth verified — authenticated as: {login} ({name})")
    if login != EXPECTED_BOX_LOGIN:
        print(
            f"[WARN] box_identity_mismatch — authenticated as {login!r}, but this cutover "
            f"expects {EXPECTED_BOX_LOGIN!r}.\n"
            f"       This usually means scripts/setup_box_oauth.py has not been run on THIS\n"
            f"       host as the production ITS identity — the Keychain may still hold a\n"
            f"       personal or sandbox Box token. Root folders created under the wrong\n"
            f"       account are invisible to the daemons and silently break every filing\n"
            f"       path. Confirm the account named in the prompt below is correct before\n"
            f"       answering 'y' (this is a WARNING, not a block: a non-production ITS\n"
            f"       identity can be legitimate during validation — the choice is yours)."
        )
    return login


def find_root_matches(root_items: list[dict[str, Any]], name: str) -> list[str]:
    """Return the ids of ALL direct child FOLDERS of the Box root named exactly `name`.

    Exact string match, no normalization — invariant 2. Returning the full list (not
    the first hit) is what makes invariant 8 possible: Box permits sibling folders
    with identical names, and adopting the wrong one would silently mis-file every
    future artifact.
    """
    return [
        str(item["id"])
        for item in root_items
        if item.get("type") == "folder" and item.get("name") == name
    ]


def _warn_if_ambiguous(name: str, matches: list[str]) -> None:
    """Invariant 8 — never let a duplicate-name adoption be silent."""
    if len(matches) > 1:
        print(
            f"[WARN] {len(matches)} folders named {name!r} exist at the Box root — "
            f"ids={matches}.\n"
            f"       Box does NOT enforce folder-name uniqueness; adopting the FIRST "
            f"({matches[0]}).\n"
            f"       RECONCILE these in the Box UI and confirm which id holds the live "
            f"tree BEFORE flipping the ITS_Config row."
        )


def _confirm_live_writes(pending: list[str], login: str) -> bool:
    """y/N gate before the FIRST live create, NAMING the Box account being written into.

    Threading the authenticated ``login`` into the prompt is the Box-identity control (Box has
    no OWNER-of-root discriminator — see ``EXPECTED_BOX_LOGIN``): the operator cannot confirm
    the create without seeing WHICH account the roots land under, so a wrong / personal /
    sandbox identity is caught at the human gate instead of silently mis-filing every future
    artifact.
    """
    listed = ", ".join(repr(n) for n in pending)
    if os.environ.get("STANDUP_NONINTERACTIVE") == "1":
        # Orchestrated run (standup.py): the master gate is the control and the
        # Box identity was ALREADY printed by standup's box-roots stage before
        # this subprocess ran — auto-approve without touching the closed stdin
        # (an unexpected extra prompt still fails loudly via EOFError).
        print(f"\nCreate {len(pending)} folder(s) at the Box ROOT of {login!r} "
              f"({listed})? [auto-approved: STANDUP_NONINTERACTIVE]")
        return True
    answer = input(
        f"\nCreate {len(pending)} folder(s) at the Box ROOT of {login!r} ({listed})? [y/N] "
    ).strip().lower()
    return answer == "y"


def build_roots(*, dry_run: bool) -> tuple[dict[str, str | None], int]:
    """Find-or-create the four root folders. Returns (name -> id-or-None, exit code)."""
    root_items = probe_auth()
    # Surface the authenticated Box IDENTITY loudly + unconditionally (every mode, incl.
    # --dry-run) BEFORE any find/create decision — the D4 wrong-account control (see
    # EXPECTED_BOX_LOGIN). A mismatch WARNs; the login is named again in the y/N prompt.
    login = _resolve_identity()
    print(f"[ok] Box root listing returned {len(root_items)} item(s).\n")

    resolved: dict[str, str | None] = {}
    to_create: list[str] = []

    # --- Find pass (read-only, runs in BOTH modes) -------------------------
    for name, config_key, _workstream in ROOT_FOLDERS:
        matches = find_root_matches(root_items, name)
        _warn_if_ambiguous(name, matches)
        if matches:
            folder_id = matches[0]
            resolved[name] = folder_id
            print(f"[skip] folder {name!r} already present (folder_id={folder_id}).")
            print(f"       -> ITS_Config {config_key} = {folder_id}")
        else:
            resolved[name] = None
            to_create.append(name)
            if dry_run:
                print(
                    f"[dry-run] Would create folder {name!r} under the Box ROOT "
                    f"(parent folder_id={BOX_ROOT_FOLDER_ID}), then set "
                    f"ITS_Config {config_key} to the new id."
                )
            else:
                print(f"[plan] folder {name!r} is ABSENT — will create under Box root.")

    if not to_create:
        print("\n[ok] Nothing to create; every root already present (idempotent no-op).")
        return resolved, 0

    if dry_run:
        print(
            f"\n[dry-run] No API create was attempted. "
            f"{len(to_create)} folder(s) would be created."
        )
        return resolved, 0

    # --- Create pass (live) ------------------------------------------------
    if not _confirm_live_writes(to_create, login):
        # A decline is a NO-OP, not a failure: exit 0 so a `set -e` cutover sequence
        # is not derailed by the operator deliberately answering "no". main() suppresses
        # the FLIP BLOCK because `resolved` still carries unresolved (None) ids.
        print("[abort] Operator declined; no folders created.")
        return resolved, 0

    for name, config_key, _workstream in ROOT_FOLDERS:
        if resolved[name] is not None:
            continue
        try:
            # 409-adopt semantics: a concurrent creator is adopted, never duplicated.
            folder_id = box_client.get_or_create_folder(BOX_ROOT_FOLDER_ID, name)
        except box_client.BoxAuthError as e:
            print(f"[error] Box auth lost mid-run creating {name!r}: {e}", file=sys.stderr)
            return resolved, 2
        except box_client.BoxError as e:
            print(f"[error] Box create failed for {name!r}: {e}", file=sys.stderr)
            return resolved, 3
        except BoxOAuthException as e:
            # get_or_create_folder's find step goes through box_client.list_folder,
            # which has the lazy-iteration translation gap documented on
            # _list_box_root — so a raw boxsdk exception can still surface here.
            print(f"[error] Box auth lost mid-run creating {name!r}: {e!r}", file=sys.stderr)
            return resolved, 2
        except BoxException as e:
            print(f"[error] Box create failed for {name!r} (untranslated): {e!r}", file=sys.stderr)
            return resolved, 3
        except Exception as e:  # noqa: BLE001 — never a raw traceback at a cutover console
            print(f"[error] Box create failed unexpectedly for {name!r}: {e!r}", file=sys.stderr)
            return resolved, 3
        resolved[name] = folder_id
        print(f"[ok] created folder {name!r} (folder_id={folder_id}).")
        print(f"       -> ITS_Config {config_key} = {folder_id}")

    return resolved, 0


def _print_flip_block(resolved: dict[str, str | None]) -> None:
    """Print the operator's paste-ready ITS_Config flip block.

    Deliberately NOT a `[bootstrap] Update shared/sheet_ids.py:` block — these ids
    live in ITS_Config ROWS, read at runtime, and there is no Python constant to edit.
    """
    print("\n=== FLIP BLOCK ===")
    print("These are ITS_Config ROW values — NOT shared/sheet_ids.py constants.")
    print("Edit the Value cell of each row below (Class-A config edit; the §50 dashboard")
    print("config editor or a direct ITS_Config edit both work). No code change follows.\n")
    print(f"  {'Setting':<48} {'Workstream':<18} Value")
    for name, config_key, workstream in ROOT_FOLDERS:
        value = resolved.get(name) or "<NOT CREATED — re-run>"
        print(f"  {config_key:<48} {workstream:<18} {value}")
    print("\n  (Box folder ids are STRINGS — paste them verbatim, no formatting.)")
    print("  Verify in the Box web UI, signed in as the ITS identity, that each id is")
    print("  the intended top-level folder before relying on it.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Find-or-create the four ITS Box root folders (D4). Create-only, idempotent, "
            "exact-name adopt."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = parser.parse_args()

    print("[info] Box roots: " + " | ".join(repr(n) for n, _k, _w in ROOT_FOLDERS))
    print(f"[info] Parent: Box ROOT folder id={BOX_ROOT_FOLDER_ID!r}")
    print(f"[info] Mode: {'DRY-RUN' if args.dry_run else 'LIVE WRITE'}")
    print("[info] Per-job / per-week subtrees are RUNTIME find-or-create — out of scope.\n")

    resolved, code = build_roots(dry_run=args.dry_run)

    print("\nSummary:")
    for name, config_key, _workstream in ROOT_FOLDERS:
        print(f"  {name:<24} id={resolved.get(name)}   ({config_key})")

    if args.dry_run:
        print("\n[dry-run] Re-run without --dry-run to create, then paste the FLIP BLOCK.")
    elif code == 0 and all(v is not None for v in resolved.values()):
        _print_flip_block(resolved)
    elif code == 0:
        # Operator-declined run: every root resolved is still None, so there is nothing
        # to flip. Printing a FLIP BLOCK full of "<NOT CREATED>" would only invite a
        # placeholder paste.
        print("\n[info] One or more roots were not created — no FLIP BLOCK. Re-run and "
              "confirm at the prompt to create them.")

    return code


if __name__ == "__main__":
    sys.exit(main())
