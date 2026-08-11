"""Box SDK wrapper — OAuth 2.0 User Authentication.

Auth: OAuth 2.0 Authorization Code Grant against a Box Custom App configured
as a User Authentication app. The first-time browser flow is handled by
`scripts/setup_box_oauth.py`; this module wires the steady-state flow that
every ITS process invocation uses.

**Pivot context (2026-05-20):** ITS originally targeted Box JWT/server-auth.
That path requires the paid Box Platform add-on, which Evergreen's Box
Enterprise tier does not include. OAuth User Authentication works on
standard Enterprise with no add-on; the tradeoff is that ITS authenticates
as a real Box user (operator account for now, dedicated ITS user at
Phase 1.5 cutover) rather than as a service account. Audit trail and file
ownership attribute to that user. Acceptable for Customer 0 sandbox phase.

Credentials live in macOS Keychain:
    ITS_BOX_CLIENT_ID         # operator-seeded once, never rotates here
    ITS_BOX_CLIENT_SECRET     # operator-seeded once, manually rotated in Box console
    ITS_BOX_REFRESH_TOKEN     # written by setup_box_oauth.py; ROTATED on every use

**Critical invariant — refresh-token rotation MUST persist.** Box rotates
refresh tokens on every token exchange. The old token becomes invalid the
moment a new one is issued. If ITS reads the refresh token from Keychain,
exchanges it, gets a new one, then crashes before persisting the new one,
the next ITS invocation reads the old (now invalid) token and fails
authentication. Recovery requires re-running `setup_box_oauth.py`. The
`_store_tokens` callback wired into `boxsdk.OAuth2` MUST write the new
refresh token to Keychain synchronously on every rotation. The test suite
asserts this explicitly.

Ship-and-leave window: refresh tokens are valid for 60 days from last use.
ITS in steady-state runs daily workstreams → token is exchanged daily →
no concern. If ITS goes dark for >60 days, the refresh token expires and
the operator must re-run `setup_box_oauth.py`. **Watchdog Check P
(`scripts/watchdog._check_box_token_freshness`) covers this today** — it reads
the `BOX_TOKEN_REFRESH_MARKER` age (WARN at 50 days idle / CRITICAL at 58) AND
makes a real authenticated Box read, so a token that is dead RIGHT NOW is caught
even while the marker still looks recent. (The marker-only version of that check
reported "fresh (idle 2d)" straight through a live `invalid_grant` on 2026-08-10;
issue #26.)

**Single-use refresh tokens + concurrent processes = a losable race.** Box
invalidates a refresh token the instant it is exchanged. `_store_tokens`'s lock
serializes the PERSIST, not the HTTP exchange (boxsdk owns that), so two ITS
processes can both read token R from Keychain and both try to spend it. The
loser is rejected with `invalid_grant` — for which Box uses the SAME wording it
uses for a genuinely aged-out token, making a transient race look like a 60-day
expiry. `_retry_once_on_rejected_refresh_token` absorbs that: it drops the cached
client (so the next `get_client()` re-reads Keychain, where the WINNER has by then
persisted the newer token) and retries the operation exactly once. See
`BoxRefreshTokenRejectedError`.

Capabilities exposed:
    get_client(), upload_file(), upload_bytes(), download_file(), list_folder(),
    get_folder_by_path(), get_or_create_folder(), search(), get_file_metadata(),
    canonical_job_path()

Error model:
    Every failure raises a typed exception under `BoxError`. Callers
    decide whether to log, retry, or surface — this module does not
    swallow. Mirrors the shape of `shared.graph_client` and
    `shared.resend_client`.

Retry: 429 and 503 with Retry-After header honored, exponential backoff
fallback, cap `MAX_RETRIES=3`. Same pattern as resend_client.
"""
from __future__ import annotations

import functools
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from boxsdk import Client, OAuth2  # type: ignore[import-untyped]
from boxsdk.exception import (  # type: ignore[import-untyped]
    BoxAPIException,
    BoxOAuthException,
)
from boxsdk.session.session import AuthorizedSession  # type: ignore[import-untyped]

from . import keychain, state_io

OAUTH_TOKEN_URL = "https://api.box.com/oauth2/token"  # noqa: S105 — public OAuth endpoint
OAUTH_AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"

MAX_RETRIES = 3

# (connect, read) network timeout in seconds for every Box API call. boxsdk has
# NO default network timeout (eval A2 `host-daemon-no-timeout` /
# `box-rate-limit-no-instrumentation`) — a stalled Box call would hang a daemon
# indefinitely. Wired via the AuthorizedSession's `default_network_request_kwargs`
# (the legacy-boxsdk timeout knob; `DefaultNetwork.request(**kwargs)` forwards it
# to `requests`).
BOX_NETWORK_TIMEOUT = (10, 30)

# Keychain entry names. Single source of truth — also used by
# setup_box_oauth.py and smoke_test_box.py.
KC_CLIENT_ID = "ITS_BOX_CLIENT_ID"
KC_CLIENT_SECRET = "ITS_BOX_CLIENT_SECRET"  # noqa: S105 — Keychain entry NAME, not the secret itself
KC_REFRESH_TOKEN = "ITS_BOX_REFRESH_TOKEN"  # noqa: S105 — Keychain entry NAME, not the secret itself

# A3 — cross-process refresh-token coordination + freshness marker.
# STATE_DIR mirrors the convention in shared/alert_dedupe.py + the daemons:
# all daemon-managed state lives under ~/its/state/ (absolute, NOT cwd-relative),
# so every process — wherever launched — coordinates on the same files.
STATE_DIR = Path.home() / "its" / "state"
# Anchor for the cross-process refresh-lock. `with_path_lock()` flocks
# "{path}.lock", so the sidecar lives at ~/its/state/box_oauth_refresh.lock.
_BOX_OAUTH_REFRESH_LOCK_ANCHOR = STATE_DIR / "box_oauth_refresh"
# Freshness marker, written on every successful refresh-token persist; read by
# watchdog Check P to WARN at 50d idle / CRITICAL at 58d (ahead of the 60d expiry).
BOX_TOKEN_REFRESH_MARKER = STATE_DIR / "box_oauth_last_refresh.json"


# ---- Typed exceptions ----------------------------------------------------


class BoxError(Exception):
    """Base exception for all Box failures."""


class BoxAuthError(BoxError):
    """Token rejected or insufficient scope (HTTP 401/403)."""


class BoxRefreshTokenRejectedError(BoxAuthError):
    """Box rejected the REFRESH token itself (`invalid_grant`).

    §42: this is deliberately a distinct type rather than a message flavour, because
    exactly one recovery is safe to automate for it and it must not be applied to the
    other auth failures. `invalid_grant` has TWO causes that Box reports with identical
    wording ("Refresh token has expired"):

      1. **CONSUMED** — another ITS process exchanged the token moments ago. Refresh
         tokens are single-use, and the `_store_tokens` lock covers the persist, not the
         exchange, so this race is reachable whenever two Box-touching processes overlap
         (portal_poll 60 s, fieldops_sync / po_poll 90 s). Self-healing: the winner has
         already persisted a NEWER valid token to Keychain.
      2. **AGED OUT** — nothing exchanged the token for 60 days. Fatal until a human
         re-runs `scripts/setup_box_oauth.py`.

    Nothing in the response distinguishes them, so this module NEVER reports a rejection
    as "expired" on its own authority. `_rejection_message` states both possibilities and
    quotes the freshness marker, which is the only local evidence that separates them: a
    marker stamped minutes ago means (1); a marker ~60 days old or absent means (2).
    """


class BoxNotFoundError(BoxError):
    """File, folder, or resource missing (HTTP 404)."""


class BoxConflictError(BoxError):
    """Conflict (HTTP 409) — typically duplicate filename in a folder."""


class BoxRateLimitError(BoxError):
    """HTTP 429 after the retry budget was exhausted."""


# ---- Lazy-singleton client + token rotation ------------------------------


_client: Client | None = None


def _record_token_refresh() -> None:
    """Best-effort: stamp the Box refresh-token freshness marker.

    §42: Box refresh tokens expire 60 days from last use. The daily-workstream
    cadence exercises the token well inside that window, but a multi-day host
    outage erodes the margin invisibly. This marker records the last successful
    persist so watchdog Check P can WARN at 50d / CRITICAL at 58d *before* the
    token dies (recovery = re-run `setup_box_oauth.py`, a Developer-Operator-only
    credential operation). **Best-effort by design:** the Keychain write in
    `_store_tokens` is the critical path, so a failed marker write must never
    break token persistence — every failure here is swallowed (logged WARN).
    """
    try:
        # Single atomic write of last_refresh_utc ONLY — no read-modify-write, so no
        # state-file lock is needed (a refresh_count RMW would have required
        # with_path_lock on every call, incl. _store_tokens' fail-open path which runs
        # outside the refresh-lock anchor). The marker is freshness-only: watchdog
        # Check P reads last_refresh_utc and nothing else.
        state_io.atomic_write_json(
            BOX_TOKEN_REFRESH_MARKER,
            {"last_refresh_utc": datetime.now(UTC).isoformat()},
        )
    except Exception as exc:  # noqa: BLE001 — marker is best-effort, never break persistence
        from .error_log import Severity, log
        log(
            Severity.WARN,
            "shared.box_client",
            f"Box token freshness marker write failed (non-fatal): {exc!r}",
            error_code="box_token_marker_write_failed",
        )


def _store_tokens(access_token: str, refresh_token: str) -> None:
    """OAuth2 store_tokens callback — persists the rotated refresh token.

    boxsdk calls this on EVERY token exchange (initial + every refresh).
    Box rotates the refresh token on each exchange; if we don't persist
    the new one, the next ITS process invocation reads the old (now
    invalid) token from Keychain and fails authentication. See module
    docstring "Critical invariant."

    Access tokens are NOT persisted — they have a 60-minute TTL and
    boxsdk re-fetches them on demand within the process.

    A3: the persist + freshness-marker write run under a cross-process sidecar
    lock (`box_oauth_refresh.lock`) so two ITS daemons refreshing within the same
    window cannot interleave their Keychain writes and persist a token the other
    already invalidated (the `box-token-refresh-race`). boxsdk owns the token
    *exchange* itself, so this serializes the persist seam — the realistic
    cross-process coordination point — not the HTTP exchange. **FAIL-OPEN:** a
    lock-acquire timeout logs WARN and persists anyway, because an un-persisted
    rotated token GUARANTEES a 60-day death whereas a lost lock is merely a rare
    race window.
    """
    try:
        with state_io.with_path_lock(_BOX_OAUTH_REFRESH_LOCK_ANCHOR):
            keychain.set_secret(KC_REFRESH_TOKEN, refresh_token)
            _record_token_refresh()
    except state_io.StateLockTimeoutError:
        from .error_log import Severity, log
        log(
            Severity.WARN,
            "shared.box_client",
            "Box refresh-lock timeout — persisting rotated token UNLOCKED "
            "(fail-open: an un-persisted token is fatal; a lost lock is not).",
            error_code="box_oauth_refresh_lock_timeout",
        )
        keychain.set_secret(KC_REFRESH_TOKEN, refresh_token)
        _record_token_refresh()


def get_client() -> Client:
    """Return a process-wide Box OAuth client, building it on first use.

    Reads `ITS_BOX_CLIENT_ID`, `ITS_BOX_CLIENT_SECRET`, and
    `ITS_BOX_REFRESH_TOKEN` from Keychain. Constructs `boxsdk.OAuth2`
    with `_store_tokens` wired so refresh-token rotations persist. Wraps
    in a `boxsdk.Client` which handles access-token acquisition + rotation
    transparently inside the process.

    Subsequent calls within the same process return the cached client.

    Raises:
        BoxAuthError: If credential reads fail or the initial token
            exchange is rejected (typical cause: refresh token expired
            after >60 days idle, or revoked from the Box console).
    """
    global _client
    if _client is None:
        try:
            client_id = keychain.get_secret(KC_CLIENT_ID)
            client_secret = keychain.get_secret(KC_CLIENT_SECRET)
            refresh_token = keychain.get_secret(KC_REFRESH_TOKEN)
        except keychain.KeychainError as e:
            raise BoxAuthError(
                f"Box credentials missing from Keychain: {e}. "
                f"Run scripts/setup_box_oauth.py to seed."
            ) from e

        oauth = OAuth2(
            client_id=client_id,
            client_secret=client_secret,
            access_token=None,  # forces refresh-token exchange on first call
            refresh_token=refresh_token,
            store_tokens=_store_tokens,
        )
        # Bound every Box API call (boxsdk has no default network timeout) via the
        # AuthorizedSession's default_network_request_kwargs — see BOX_NETWORK_TIMEOUT.
        session = AuthorizedSession(
            oauth, default_network_request_kwargs={"timeout": BOX_NETWORK_TIMEOUT}
        )
        _client = Client(oauth, session=session)
    return _client


def _reset_client() -> None:
    """Drop the cached client so the next `get_client()` re-reads Keychain.

    §42 — this is the load-bearing half of the `invalid_grant` retry, and the reason a
    naive "just call the operation again" retry would NOT have worked. `get_client()` is
    a process-wide lazy singleton, and the `boxsdk.OAuth2` object it builds holds the
    refresh token it read at construction time **in memory**. After a lost rotation race
    the newer valid token is in Keychain, but the cached OAuth2 still holds the consumed
    one — so re-running the same operation against the cached client re-spends the dead
    token and fails identically. Clearing the singleton is what makes the retry read the
    winner's token.

    Note this also invalidates every boxsdk resource object already bound to the old
    session (`client.folder(...)` etc.), which is why the retry seam is the PUBLIC
    function (each of which re-derives its resource from a fresh `get_client()`) and not
    `_call` — `_call` receives an ALREADY-BOUND operation and could only ever re-run it
    against the stale session.
    """
    global _client
    _client = None


def _is_invalid_grant(exc: BoxOAuthException) -> bool:
    """True when a boxsdk auth failure is specifically an `invalid_grant` rejection.

    boxsdk's `_oauth_exception` sets `code = json['code'] or json['error']`, so a Box
    OAuth error body `{"error":"invalid_grant", ...}` arrives as `code='invalid_grant'`
    — a structured field, checked first. The message fallback covers a non-JSON or
    shape-changed error body, where boxsdk keeps the raw text.
    """
    if getattr(exc, "code", None) == "invalid_grant":
        return True
    return "invalid_grant" in f"{getattr(exc, 'message', '') or ''}".lower()


def _last_refresh_note() -> str:
    """Operator-facing evidence for a rejection: when the token last rotated HERE.

    Best-effort and never raises — this only ever decorates an error message, and an
    unreadable marker must not mask the auth failure being reported.
    """
    try:
        if not BOX_TOKEN_REFRESH_MARKER.exists():
            return "no local rotation on record (marker absent)"
        raw = json.loads(BOX_TOKEN_REFRESH_MARKER.read_text())
        stamped = datetime.fromisoformat(raw["last_refresh_utc"])
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=UTC)
        age = datetime.now(UTC) - stamped
        if age.total_seconds() < 3600:
            human = f"{age.total_seconds() / 60:.0f} min ago"
        elif age.days < 1:
            human = f"{age.total_seconds() / 3600:.1f} h ago"
        else:
            human = f"{age.days}d ago"
        return f"last local rotation {stamped.isoformat()} ({human})"
    except Exception:  # noqa: BLE001 — decoration only; never mask the auth failure
        return "local rotation time unreadable"


def _rejection_message(exc: BoxOAuthException) -> str:
    """The operator-facing text for an `invalid_grant`. Deliberately NOT 'expired'.

    Box returns the same `error_description` ("Refresh token has expired") whether the
    token was CONSUMED by a concurrent ITS process seconds ago or genuinely AGED OUT
    after 60 idle days. Asserting either one would be a guess, and guessing "expired"
    is the expensive direction: it points a Tier-2 operator at a full re-auth (a fixed
    high-capability-class escalation to Seth) for what is usually a self-healing race.
    So state both, and hand over the one piece of local evidence that separates them.
    """
    return (
        f"Box REJECTED the refresh token (invalid_grant): {exc}. Box uses identical "
        "wording for a token CONSUMED by a concurrent ITS process (single-use tokens; "
        "common and self-healing) and one genuinely AGED OUT after 60 idle days — the "
        f"response cannot tell them apart. Local evidence: {_last_refresh_note()}. "
        "A recent rotation means a lost race (ITS retries once automatically); a ~60-day-"
        "old or absent one means re-auth via scripts/setup_box_oauth.py is required "
        "(escalate to Seth — secrets/auth is a fixed high-capability class)."
    )


def _oauth_error(exc: BoxOAuthException) -> BoxAuthError:
    """Translate a boxsdk auth-layer failure onto the typed hierarchy.

    The ONE place `BoxOAuthException` becomes ours, so the `invalid_grant` discrimination
    (and its retry eligibility) cannot be forgotten at one of the three raise sites.
    """
    if _is_invalid_grant(exc):
        return BoxRefreshTokenRejectedError(_rejection_message(exc))
    return BoxAuthError(f"OAuth exchange failed: {exc}")


# Re-entrancy guard for the retry decorator. Thread-local because ITS runs several
# Box-touching daemons and nothing forbids a threaded caller; the flag must not leak
# across threads and suppress a genuine retry.
_retry_guard = threading.local()

def _retry_once_on_rejected_refresh_token[F: Callable[..., Any]](fn: F) -> F:
    """Retry a Box operation ONCE after an `invalid_grant`, on a freshly-read token.

    §42 — why this exists and why it is shaped this way.

    The failure it absorbs (issue #26, observed live 2026-08-10, three times in one day,
    self-healing on retry every time): refresh tokens are single-use and the persist lock
    does not cover the exchange, so two overlapping ITS processes can both spend token R.
    The loser is rejected. By the time it is rejected, the WINNER has already persisted a
    newer valid token to Keychain — which is precisely why one retry is sufficient and why
    `_reset_client()` (not a bare re-call) is the operative step: the retry must go back to
    Keychain rather than reuse the cached OAuth2's in-memory copy of the dead token.

    Applied ONLY to the public functions that call `get_client()` directly. Composite
    helpers (`get_folder_by_path`, `find_child_folder`, `canonical_job_path`) reach Box
    exclusively through those, so they inherit the behaviour without stacking a second
    retry layer.

    ONE retry, enforced two ways: a second rejection re-raises (a genuinely dead token
    must not be retried into a loop against Box's auth endpoint), and the thread-local
    re-entrancy guard makes a nested decorated call pass straight through — without it,
    `get_or_create_folder` → `list_folder` would give a single lost race 2 retries, and a
    deeper chain 2ⁿ, turning one rejection into a burst of token exchanges that would
    WORSEN the very race being fixed.

    Retrying the whole operation is safe: `invalid_grant` is raised during the token
    exchange, i.e. BEFORE the wrapped API request is accepted by Box (the exchange either
    precedes the first request of the process, or follows a 401 that Box already refused),
    so no partial side effect can exist at this point. Bodies are re-entrant by
    construction — `upload_bytes` builds its `BytesIO` inside the call, not outside.
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if getattr(_retry_guard, "active", False):
            # Nested inside an already-guarded call: the OUTERMOST frame owns the single
            # retry. See the 2ⁿ note above.
            return fn(*args, **kwargs)
        _retry_guard.active = True
        try:
            try:
                return fn(*args, **kwargs)
            except BoxRefreshTokenRejectedError:
                _reset_client()
                result = fn(*args, **kwargs)
        finally:
            _retry_guard.active = False
        # Reached only when the retry SUCCEEDED, which retroactively proves the first
        # rejection was a CONSUMED token and not an aged-out one — the one moment the
        # ambiguity in `_rejection_message` is actually resolvable. Record it (never
        # silent) so a rising race rate is visible instead of being absorbed invisibly.
        from .error_log import Severity, log
        log(
            Severity.WARN,
            "shared.box_client",
            f"Box refresh token was CONSUMED by a concurrent ITS process during "
            f"{fn.__name__}() — NOT expired. Re-read the rotated token from Keychain and "
            f"the retry SUCCEEDED. Self-healed; no action needed. Repeated occurrences "
            f"mean Box-touching daemons are overlapping more often.",
            error_code="box_refresh_token_consumed_retry",
        )
        return result

    return wrapper  # type: ignore[return-value]


# ---- Retry / error translation -------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After as seconds. None on unparseable input."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        # HTTP-date form is legal but Box returns seconds; fall back to backoff.
        return None


def _translate(exc: BoxAPIException) -> BoxError:
    """Map a BoxAPIException onto our typed hierarchy."""
    status = exc.status
    message = exc.message or "Box API error"
    detail = f"HTTP {status}: {message}"
    if status in (401, 403):
        return BoxAuthError(detail)
    if status == 404:
        return BoxNotFoundError(detail)
    if status == 409:
        return BoxConflictError(detail)
    if status == 429:
        return BoxRateLimitError(detail)
    return BoxError(detail)


def _call(operation, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Execute a boxsdk operation with retry on 429/503.

    `operation` is a callable (typically a method bound to a boxsdk
    resource — e.g., `client.folder("0").get_items`). Args/kwargs are
    forwarded. On 429 or 503, retries up to `MAX_RETRIES` with
    Retry-After honored (Box uses seconds); falls back to exponential
    backoff when the header is absent.

    Translates `BoxAPIException` to the typed `BoxError` hierarchy.
    `BoxOAuthException` (auth-layer failures during token exchange)
    surfaces as `BoxAuthError` regardless of status.
    """
    last_exc: BoxAPIException | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return operation(*args, **kwargs)
        except BoxOAuthException as e:
            # Auth-layer failure — the token exchange itself failed. NOT retried here:
            # the operation is already BOUND to the stale client's session, so re-running
            # it would re-spend the same dead token. An `invalid_grant` becomes a
            # BoxRefreshTokenRejectedError, which the public function's
            # `_retry_once_on_rejected_refresh_token` wrapper retries after dropping the
            # cached client — the only layer where a fresh Keychain read is possible.
            raise _oauth_error(e) from e
        except BoxAPIException as e:
            if e.status not in (429, 503):
                raise _translate(e) from e
            last_exc = e
            if attempt == MAX_RETRIES - 1:
                break
            headers = getattr(e, "headers", None) or {}
            delay = _parse_retry_after(headers.get("Retry-After"))
            if delay is None:
                delay = float(2**attempt)
            time.sleep(delay)
    # Exhausted retries on 429/503.
    assert last_exc is not None
    raise _translate(last_exc) from last_exc


# ---- Public API ----------------------------------------------------------


@_retry_once_on_rejected_refresh_token
def upload_file(
    folder_id: str,
    file_path: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Upload a local file to a Box folder. Returns minimal file metadata.

    Args:
        folder_id: Box folder ID. "0" is the user's root folder.
        file_path: Local filesystem path to the file to upload.
        name: Optional override for the uploaded file name. Defaults to
            the basename of `file_path`.

    Returns:
        Dict with `id`, `name`, `size` for the uploaded file. Box's
        full file object has many more fields; we expose the minimal
        set callers actually need and force a fresh API call if more
        is required (avoids surprises from boxsdk lazy-loading attrs).

    Raises:
        BoxConflictError: HTTP 409 — a file with the same name already
            exists in the destination folder.
        BoxAuthError / BoxNotFoundError / BoxRateLimitError / BoxError:
            other failure modes per the typed hierarchy.
    """
    client = get_client()
    folder = client.folder(folder_id)
    uploaded = _call(folder.upload, file_path, file_name=name)
    return {"id": uploaded.id, "name": uploaded.name, "size": uploaded.size}


@_retry_once_on_rejected_refresh_token
def upload_bytes(folder_id: str, name: str, content: bytes) -> dict[str, Any]:
    """Upload in-memory bytes as a Box file. Returns minimal file metadata.

    The in-memory sibling of `upload_file` — for content produced at runtime
    (e.g. `form_pdf.render_submission_pdf` → PDF bytes) that never touches the
    local filesystem. Uses the boxsdk byte-stream upload path.

    Deliberately NOT routed through `_call`'s 429/503 retry: a `BytesIO` stream
    is consumed on the first attempt, so a naive retry would re-send from EOF and
    upload an empty file. Upload is not safely idempotent to retry anyway. We
    translate exceptions to the typed hierarchy and let the caller decide (the
    portal path suffixes the name + re-uploads on `BoxConflictError` to keep both
    versions of an amended submission).

    Raises:
        BoxConflictError: HTTP 409 — a file named `name` already exists here.
        BoxAuthError / BoxNotFoundError / BoxRateLimitError / BoxError: per the
            typed hierarchy.
    """
    import io
    client = get_client()
    try:
        uploaded = client.folder(folder_id).upload_stream(io.BytesIO(content), name)
    except BoxOAuthException as exc:
        raise _oauth_error(exc) from exc
    except BoxAPIException as exc:
        raise _translate(exc) from exc
    except Exception as exc:  # noqa: BLE001 — honor the module's "every failure → BoxError" contract
        # boxsdk usually raises its own types, but anything else (e.g. an OSError
        # mid-stream) must not escape untranslated past the typed boundary.
        raise BoxError(f"Box upload of {name!r} failed: {exc!r}") from exc
    return {"id": str(uploaded.id), "name": uploaded.name, "size": uploaded.size}


def _find_child_file(parent_folder_id: str, name: str) -> str | None:
    """Return the ID of the direct child FILE named `name`, or None.

    File sibling of `find_child_folder` (same 1000-child page caveat). Used by
    `upload_bytes_or_new_version` to resolve the existing file to version-update.
    """
    for item in list_folder(parent_folder_id, limit=1000):
        if item["type"] == "file" and item["name"] == name:
            return str(item["id"])
    return None


@_retry_once_on_rejected_refresh_token
def upload_bytes_or_new_version(folder_id: str, name: str, content: bytes) -> dict[str, Any]:
    """Upload bytes as `name`; if a same-named file already exists, upload a NEW
    Box VERSION of it instead of failing.

    The version-on-conflict sibling of `upload_bytes`, for content RE-generated under
    a DETERMINISTIC name — the weekly packet: a recompile (Compile Now / a late
    submission) produces the same filename. `upload_bytes` 409s on the same name,
    which routed the recompile to the Review Queue and broke Compile Now. Here a 409
    instead resolves the existing file and `update_contents` it — preserving Box's
    file-version HISTORY (the System of Record) rather than 409ing or accumulating
    suffixed copies. (The per-SUBMISSION path keeps `upload_bytes`'s suffix strategy:
    each amend is a genuinely distinct document, not a new version of one.)

    Idempotent on recompile: the returned file id is STABLE across versions.

    Raises the typed `BoxError` hierarchy. A 409 whose conflicting file then can't be
    found (it vanished between the upload and the find — a race) re-raises
    `BoxConflictError` rather than silently swallowing.
    """
    import io
    try:
        return upload_bytes(folder_id, name, content)
    except BoxConflictError:
        existing_id = _find_child_file(folder_id, name)
        if existing_id is None:
            raise  # the conflicting file vanished between upload + find — surface it
        client = get_client()
        try:
            updated = client.file(existing_id).update_contents_with_stream(io.BytesIO(content))
        except BoxOAuthException as exc:
            raise _oauth_error(exc) from exc
        except BoxAPIException as exc:
            raise _translate(exc) from exc
        except Exception as exc:  # noqa: BLE001 — honor "every failure → BoxError"
            raise BoxError(f"Box version-update of {name!r} failed: {exc!r}") from exc
        return {"id": str(updated.id), "name": updated.name, "size": updated.size}


@_retry_once_on_rejected_refresh_token
def download_file(file_id: str) -> bytes:
    """Return the raw bytes of a Box file."""
    client = get_client()
    return _call(client.file(file_id).content)


@_retry_once_on_rejected_refresh_token
def list_folder(folder_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """List items (files + folders) in a Box folder.

    Returns a list of dicts each containing `id`, `name`, and `type`
    (`'file'` or `'folder'`). Use `folder_id="0"` for the root folder.
    """
    client = get_client()
    # MATERIALIZE INSIDE `_call`. `get_items` issues ZERO HTTP — it returns a lazy
    # `LimitOffsetBasedObjectCollection`, and the request fires on ITERATION. Passing the bound
    # method to `_call` guarded only the CONSTRUCTION: the real call happened one frame OUTSIDE
    # the translation/retry wrapper, so a rejected token escaped as a raw `BoxOAuthException`,
    # was never mapped to `BoxRefreshTokenRejectedError`, and the retry could not match it —
    # Check P's liveness probe then reported "skipped" on a dead credential and fell back to
    # "marker fresh". That is the #26 bug reproduced by the #26 fix.
    #
    # `lambda: list(...)` drags the iteration inside the guarded frame. This also restores the
    # 429/503 retry loop for this read, which had the same escape. Tests must mock `get_items`
    # with a GENERATOR that raises on iteration — a plain list evaluates eagerly and hides it.
    items = _call(lambda: list(client.folder(folder_id).get_items(limit=limit)))
    return [{"id": item.id, "name": item.name, "type": item.type} for item in items]


def get_folder_by_path(path: str) -> dict[str, Any]:
    """Resolve a slash-separated path under the user's root to a folder.

    `path` is a forward-slash-delimited path under root (e.g.,
    `"Customer/2024.335 — Forefront/2026/"`). Leading and trailing
    slashes are tolerated. Walks the path segment-by-segment from
    root using `list_folder`; raises `BoxNotFoundError` on the first
    segment that doesn't resolve.

    Returns a dict with `id`, `name`, `type='folder'` matching the
    final segment.

    Note: walks via list_folder which is paginated at the default
    limit. Folders containing more than `limit` children with the
    target name buried past that point will fail to resolve. Bump
    `limit` upstream if this becomes a real problem.
    """
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        # Empty/root request — return root folder shape.
        return {"id": "0", "name": "All Files", "type": "folder"}

    current_id = "0"
    current_name = "All Files"
    for segment in segments:
        items = list_folder(current_id)
        match = next(
            (it for it in items if it["type"] == "folder" and it["name"] == segment),
            None,
        )
        if match is None:
            raise BoxNotFoundError(
                f"path segment {segment!r} not found under folder "
                f"{current_id} ({current_name!r}); full path={path!r}"
            )
        current_id = match["id"]
        current_name = match["name"]
    return {"id": current_id, "name": current_name, "type": "folder"}


def find_child_folder(parent_folder_id: str, name: str) -> str | None:
    """Return the ID of the direct child folder named `name`, or None.

    Lists at a generous page limit (1000, Box's max page) — folders that hold
    more than 1000 same-level children with the target beyond that page won't
    resolve, same documented caveat as `get_folder_by_path`.

    PUBLIC because the Track 6 archive needs a FIND-ONLY folder lookup that this
    module's other resolvers cannot express: `get_or_create_folder` would create
    the very source container whose absence means "nothing to move", and
    `get_folder_by_path` walks from the account root rather than from a
    config-supplied root id. `field_ops.job_archive.archive_box_container` is the
    external caller; everything else here uses it internally.
    """
    for item in list_folder(parent_folder_id, limit=1000):
        if item["type"] == "folder" and item["name"] == name:
            return str(item["id"])
    return None


@_retry_once_on_rejected_refresh_token
def get_or_create_folder(parent_folder_id: str, name: str) -> str:
    """Find a direct child folder named `name` under `parent_folder_id`; create
    it if absent. Idempotent find-or-create. Returns the child folder ID.

    The ITS-auto-created-folder primitive for the Safety Portal Box mirror (the
    compiled-WSR week folder). Per the operator naming rule, callers prefix
    ITS-created folder names with ``ITS`` so the system's own folders are
    distinguishable from the existing job/category tree.

    Race-tolerant: Box does NOT enforce folder-name uniqueness, so two callers
    can both pass the find step and both create. On a create that returns 409
    (BoxConflictError), we re-find and adopt the existing folder if it is now
    visible. If the re-find STILL misses (the folder was concurrently deleted, or
    Box read-replica lag), we re-RAISE the 409 — loud, not silent — so the caller
    retries next cycle rather than proceeding with no folder. Bounded blast
    radius on the adopt path: at worst one extra empty folder for operator cleanup.
    """
    existing = find_child_folder(parent_folder_id, name)
    if existing is not None:
        return existing
    client = get_client()
    try:
        created = _call(client.folder(parent_folder_id).create_subfolder, name)
        return str(created.id)
    except BoxConflictError:
        refound = find_child_folder(parent_folder_id, name)
        if refound is not None:
            return refound
        raise


@_retry_once_on_rejected_refresh_token
def move_folder(
    folder_id: str, new_parent_folder_id: str, *, new_name: str | None = None
) -> dict[str, Any]:
    """MOVE a folder (and everything in it) under `new_parent_folder_id`, optionally
    renaming it in the same call. Returns ``{"id", "name", "parent_id"}``.

    The Box half of the Track 6 job archive. Only TWO Box containers move per job, not
    five: ``safety_reports.box.portal_root_folder_id`` is the SHARED root for safety AND
    purchase orders AND RFQs AND subcontracts (`po_poll._resolve_po_box_folder`,
    `rfq_poll._resolve_rfq_box_folder`, `subcontract_poll._resolve_subcontract_box_folder`
    all bottom out on it), so relocating ``<safety root>/<Job>`` carries
    ``Purchase Orders/``, ``RFQs/``, ``Vendor Quotes/`` and the subcontract files with it.
    Progress has its own root. A future reader will be tempted to "fix" this by adding four
    Box slots — don't.

    ATOMIC move+rename, unlike Smartsheet. boxsdk emits ONE ``PUT /folders/{id}`` carrying
    both ``parent.id`` and ``name``, so there is no crash window between relocating and
    labelling. The Smartsheet path needs a two-call move-then-rename with a resumable
    intermediate state; this side has none. That asymmetry is why the two systems get
    different resume logic, and it is not an accident of style.

    MOVE-ONLY by construction. There is deliberately no Box delete or rename wrapper in
    this module: the archive needs relocation, and ``move`` carries the rename. A "move
    failed → delete and re-upload" recovery would be catastrophic and irreversible, so the
    primitive that would enable it is simply absent.

    Conflict-adopt on 409, mirroring `get_or_create_folder`: a partially-completed prior
    run can leave ``<archive>/<Job>/Safety`` already present. If the existing child IS the
    folder we hold, the move already happened and this is a no-op — which is what lets the
    resume path re-issue it without checking first. If it is a DIFFERENT folder, re-raise
    loud: there is no merge primitive on either system, and silently colliding two job
    trees is unrecoverable.
    """
    client = get_client()
    try:
        moved = _call(
            client.folder(folder_id).move, client.folder(new_parent_folder_id), new_name
        )
    except BoxConflictError:
        target = new_name or ""
        existing = find_child_folder(new_parent_folder_id, target) if target else None
        if existing is not None and str(existing) == str(folder_id):
            # Already in place under the intended name — a replay, not a collision.
            return {
                "id": str(folder_id),
                "name": target,
                "parent_id": str(new_parent_folder_id),
            }
        raise
    parent = getattr(moved, "parent", None)
    return {
        "id": str(moved.id),
        "name": str(getattr(moved, "name", "") or ""),
        "parent_id": str(getattr(parent, "id", "") or "") if parent else "",
    }


@_retry_once_on_rejected_refresh_token
def search(
    query: str,
    *,
    type: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Search Box for items matching `query`.

    Args:
        query: Free-text search query.
        type: Optional `'file'` or `'folder'` to narrow results.
        limit: Max results.

    Returns:
        List of dicts each with `id`, `name`, `type`.
    """
    client = get_client()
    kwargs: dict[str, Any] = {"limit": limit}
    if type is not None:
        kwargs["result_type"] = type
    # Materialized inside `_call` for the same reason as `list_folder` above — `search().query`
    # is lazy too, so the HTTP fired outside the guarded frame and a rejected token escaped raw.
    results = _call(lambda: list(client.search().query(query, **kwargs)))
    return [{"id": item.id, "name": item.name, "type": item.type} for item in results]


@_retry_once_on_rejected_refresh_token
def get_file_metadata(file_id: str) -> dict[str, Any]:
    """Return basic file metadata (`id`, `name`, `size`, `modified_at`)."""
    client = get_client()
    info = _call(client.file(file_id).get)
    return {
        "id": info.id,
        "name": info.name,
        "size": info.size,
        "modified_at": getattr(info, "modified_at", None),
    }


def canonical_job_path(
    customer: str, job_number: str, job_name: str, year: int
) -> str:
    """Return the canonical Box folder path for a given job.

    Path pattern (per Safety Reports Mission v3 — still open question):
        /Customer/Job Number — Job Name/YYYY/

    Used as the WRITE path for new content. Recognition of pre-existing
    folders is handled by `box_migration/parse_job_v3.py` (which knows
    the many schema variants observed across the closed-archive corpus).
    This helper does not attempt to match those variants.

    TODO: confirm exact path pattern with owner. Kept stub-format from
    the pre-pivot box_client; all workstreams should call this helper
    rather than constructing paths inline so a single edit propagates.
    """
    return f"/{customer}/{job_number} — {job_name}/{year}/"
