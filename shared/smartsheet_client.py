"""Smartsheet SDK wrapper for ITS.

Wraps `smartsheet-python-sdk` so callers work in column-title terms instead of
column IDs, and so SDK exceptions don't leak into business code. Mirrors the
shape of `shared.graph_client` (lazy singleton from Keychain, typed exception
hierarchy, thin operation helpers) but delegates HTTP retry / rate-limit
backoff to the SDK rather than re-implementing those with `requests`.

Token: ITS_SMARTSHEET_TOKEN in macOS Keychain.

Column-name cache:
    Title → column-ID is cached per-sheet at module level. On a title that
    isn't in the cache, we refetch the sheet's columns once before giving
    up — that recovers when a column was *added* after the cache was built.
    It does NOT recover from a *rename*: callers using the old title will
    keep raising KeyError because the refreshed map won't contain it either.
    That is deliberate — silently writing into the wrong column is far worse
    than fast-failing on a stale title. Long-lived processes that need to
    survive a rename must restart or call `invalidate_column_cache()`.

External Send Gate (Foundation Mission v8, Invariant 1):
    Smartsheet writes are not external sends. This module is freely
    importable by both generation and send scripts.

SDK 404 noise:
    The Smartsheet SDK's central request/response logger emits the full
    response body at ERROR on the `smartsheet.smartsheet` logger for every
    non-2xx response, before our `_translate` raises a typed exception. We
    suppress that emission for 404 only — see `_Suppress404JSON` at the
    bottom of this module — because 404 covers the *expected* "row not yet
    seeded" case via `get_setting`. Other status codes (401 / 403 / 429 /
    500) are NOT suppressed; an operator should see them on stderr.
"""
from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any

import requests  # type: ignore[import-untyped]
import smartsheet  # type: ignore[import-untyped]
import smartsheet.exceptions as sdk_exc  # type: ignore[import-untyped]

from . import circuit_breaker, defaults, keychain, sheet_ids

SDK_LOGGER_NAME = "smartsheet.smartsheet"


class SmartsheetError(Exception):
    """Base exception for all Smartsheet failures."""


class SmartsheetAuthError(SmartsheetError):
    """Token rejected (HTTP 401)."""


class SmartsheetPermissionError(SmartsheetError):
    """Access denied for this sheet/resource (HTTP 403)."""


class SmartsheetNotFoundError(SmartsheetError):
    """Sheet, row, column, or config setting missing (HTTP 404)."""


class SmartsheetRateLimitError(SmartsheetError):
    """SDK retry budget exhausted (HTTP 429)."""


class SmartsheetValidationError(SmartsheetError):
    """Request rejected as malformed/invalid by Smartsheet (HTTP 400).

    PERMANENT by definition — the API returns ``shouldRetry: false`` (e.g.
    errorCode 1041 "sheet.name must be 50 characters or less"). A subclass of
    ``SmartsheetError`` so every existing ``except SmartsheetError`` still catches
    it unchanged; the distinct type lets a caller that retries-on-transient (the
    portal intake drain) recognize "re-pulling will NEVER fix this" and route the
    submission to the Review Queue instead of looping forever (see
    ``safety_reports/intake.process_portal_submission``). NOTE: a 400 is a
    client-side error, not a Smartsheet-health signal, so it still counts toward
    the circuit breaker exactly as the base error did (behavior unchanged); only
    the portal drain branches on the new type."""


class SmartsheetTransientError(SmartsheetError):
    """A Smartsheet failure expected to self-heal on a re-issue of the SAME call —
    an HTTP 5xx, or a `requests`-level timeout / connection drop.

    §42 — WHY this type exists (the SDK gap it covers). This module's docstring says we
    "delegate HTTP retry / rate-limit backoff to the SDK". That is only PARTLY true, and
    the two holes are exactly the failures observed live on 2026-07-21:

      * ``smartsheet-python-sdk`` 3.9.0 retries a response ONLY when its JSON body
        carries errorCode 4001/4002/4003/4004 (its ``should_retry`` lookup). An HTTP
        500 whose body carries errorCode **4000** is not in that lookup → ZERO SDK
        retries.
      * A ``requests.RequestException`` (ReadTimeout at our 30 s adapter default,
        ConnectionError) raises ``UnexpectedRequestError`` out of the SDK's ``_request``
        **before** the retry loop ever evaluates ``should_retry`` → ZERO SDK retries.

    Both classes therefore reached ITS as a raw exception, escaped the daemon's pass,
    and were stamped ``uncaught_exception`` CRITICAL by ``@its_error_log`` — a page for
    a blip that had already healed by the next cycle. This type is what lets
    ``_transient_retry`` (bounded, reads-only) and ``sustained_failure.TransientFence``
    (pass-boundary severity) recognise that precise class WITHOUT softening genuinely
    deterministic failures.

    A subclass of ``SmartsheetError`` so every existing ``except SmartsheetError``
    consumer — and the breaker's ``count=SmartsheetError`` — is unchanged. Deliberately
    NOT raised for 429: ``SmartsheetRateLimitError`` keeps its own type because the SDK
    HAS already spent its full retry window on 4003, so re-hammering is the wrong move.
    """


class SmartsheetCircuitOpenError(SmartsheetError):
    """Circuit breaker is OPEN — short-circuiting to spare a sustained-degraded
    Smartsheet API (F08).

    A subclass of ``SmartsheetError`` BY DESIGN: every existing consumer that
    catches ``SmartsheetError`` (kill_switch, portal_poll, weekly_send_poll,
    weekly_generate, picklist_sync) handles it unchanged, and
    ``weekly_generate``'s NotFound-only retry deliberately excludes it (so a
    short-circuit never triggers a retry-hammer). Raised by the
    ``circuit_breaker.guard`` wrappers below — never by the SDK-translation path.
    """


class SmartsheetWriteCapabilityError(SmartsheetError):
    """The token can READ but cannot WRITE (B2 — startup write-capability probe).

    Raised by ``verify_write_capability`` when a probe write is rejected with an
    auth/permission error (401/403) — i.e. a read-only or mis-scoped
    ``ITS_SMARTSHEET_TOKEN``. A subclass of ``SmartsheetError`` so generic
    callers still catch it, but distinct so a boot/watchdog caller can fail LOUD
    on it specifically: the alternative is the token passing every read and only
    failing at the first real daemon write (a mid-cycle 401 that is hard to
    trace — the keychain-stub session burned ~2 h on exactly that signature)."""


_client: smartsheet.Smartsheet | None = None
_column_maps: dict[int, dict[str, int]] = {}


# ---- Client + error translation -----------------------------------------


# Default network timeout (seconds) for SDK-method calls. The direct-REST helpers
# elsewhere in this module already pass `timeout=30` explicitly; the SDK methods
# (get_sheet/get_rows/add_rows/...) go through the SDK's `requests.Session`, which
# has NO default timeout — a stalled call would hang a daemon indefinitely (eval A2
# `host-daemon-no-timeout`). We mount a default-timeout adapter on that session.
SDK_NETWORK_TIMEOUT = 30


class _TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    """A `requests` HTTPAdapter that injects a DEFAULT timeout when the caller
    omits one — the standard way to bound every call on a `requests.Session`
    (the SDK never passes a per-call timeout, so this default always applies)."""

    def __init__(self, *args: Any, timeout: float, **kwargs: Any) -> None:
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


def get_client() -> smartsheet.Smartsheet:
    """Return a process-wide Smartsheet SDK client, building it on first use.

    The SDK is configured with `errors_as_exceptions=True` so non-2xx
    responses surface as `smartsheet.exceptions.ApiError`, which we translate
    in `_translate` below. A default-timeout adapter is mounted on the SDK's
    `requests` session so every SDK HTTP call is bounded (eval A2).
    """
    global _client
    if _client is None:
        token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
        client = smartsheet.Smartsheet(token, user_agent="its")
        client.errors_as_exceptions(True)
        # Bound every SDK HTTP call — the SDK session has no default timeout.
        adapter = _TimeoutHTTPAdapter(timeout=SDK_NETWORK_TIMEOUT)
        client._session.mount("https://", adapter)  # noqa: SLF001 — SDK exposes no public session/timeout hook
        client._session.mount("http://", adapter)  # noqa: SLF001
        _client = client
    return _client


#: Appended to a 5xx/timeout raised out of a NON-IDEMPOTENT call. The wording is the whole
#: point: the operator — and any future retry consumer — must be told the outcome is
#: genuinely UNKNOWN, not merely that the call "failed".
_NON_IDEMPOTENT_SUFFIX = (
    " — NON-IDEMPOTENT operation: the request left this process, so it is UNKNOWN whether "
    "the write COMMITTED. Smartsheet has no idempotency key. Do NOT auto-retry; re-read "
    "the sheet to establish the truth, then re-issue only if it did not commit."
)


def _translate(exc: sdk_exc.SmartsheetException, *, idempotent: bool) -> SmartsheetError:
    """Map an SDK exception onto our typed hierarchy.

    ``idempotent`` — REQUIRED and keyword-only, the exact contract
    ``_translate_smartsheet_error`` already carries for the raw-REST helpers, so a new SDK
    helper must DECIDE rather than inherit retry-safety from whichever branch it lands in.
    It gates the self-healing classification ONLY (5xx, non-JSON 5xx, and the
    ``UnexpectedRequestError`` timeout/connection fallthrough):

      * ``True``  (a GET / lookup) → ``SmartsheetTransientError``, which
        ``is_transient_error()`` reports as retry-safe and ``_transient_retry`` re-issues.
      * ``False`` (add_rows / update_rows / delete_rows / add_columns / update_column /
        attach / create / move / delete sheet) → base ``SmartsheetError`` carrying
        ``_NON_IDEMPOTENT_SUFFIX``.

    WHY (the round-4 finding this closes, 2026-07-21). Round 3 threaded ``idempotent``
    through ``_translate_smartsheet_error``, which fixed the four raw-REST helpers — but
    THIS path still classified by STATUS ALONE, so an ``add_rows`` that 500s or times out
    yielded ``SmartsheetTransientError`` and ``is_transient_error()`` answered True for it.
    That is a PUBLIC predicate contradicting its own contract, and it is load-bearing:
    ``sustained_failure.TransientFence`` consults exactly this predicate, so a failed WRITE
    reaching a fenced pass boundary was softened from an immediate CRITICAL to a counted
    ERROR as though it were a safe-to-reissue read. A timed-out ``add_rows`` may well have
    COMMITTED; labelling that "transient" is a claim of retry-safety no caller can honour.
    Narrowed at the raise site — the one place the read/write fact is actually known —
    rather than asking every future consumer to know which helpers are writes.
    """
    if isinstance(exc, sdk_exc.ApiError):
        result = exc.error.result
        status = result.status_code
        message = result.message or "Smartsheet API error"
        detail = f"HTTP {status} (code {result.code}): {message}"
        if status == 400:
            return SmartsheetValidationError(detail)
        if status == 401:
            return SmartsheetAuthError(detail)
        if status == 403:
            return SmartsheetPermissionError(detail)
        if status == 404:
            return SmartsheetNotFoundError(detail)
        if status == 429:
            # NOT transient: the SDK already spent its full retry budget on 4003.
            return SmartsheetRateLimitError(detail)
        if status >= 500:
            if idempotent:
                return SmartsheetTransientError(detail)
            return SmartsheetError(detail + _NON_IDEMPOTENT_SUFFIX)
        return SmartsheetError(detail)
    if isinstance(exc, sdk_exc.HttpError):
        # Non-JSON error body. GATE ON STATUS exactly like the ApiError branch above —
        # "the body was not JSON" is not by itself evidence of a self-healing fault. A
        # captive portal / corporate proxy / Cloudflare challenge answers 401/403/407
        # with an HTML page; retrying that 3× and then softening it to an ERROR would
        # hide a real, deterministic access problem behind a "blip" label.
        http_detail = f"HTTP {exc.status_code}: {exc.body!r}"
        if isinstance(exc.status_code, int) and exc.status_code >= 500:
            if idempotent:
                return SmartsheetTransientError(http_detail)
            return SmartsheetError(http_detail + _NON_IDEMPOTENT_SUFFIX)
        return SmartsheetError(http_detail)
    # Fallthrough is dominated by UnexpectedRequestError, which the SDK raises from
    # `_request` for every `requests` exception (ReadTimeout / ConnectionError) BEFORE
    # its retry loop can see it — see SmartsheetTransientError's docstring. A timeout is
    # the WORST case for a write: the request was fully transmitted and only the RESPONSE
    # was lost, so "it may have committed" is not hypothetical.
    if idempotent:
        return SmartsheetTransientError(str(exc))
    return SmartsheetError(str(exc) + _NON_IDEMPOTENT_SUFFIX)


# ---- Circuit breaker wiring (F08) ---------------------------------------
#
# This wrapper is the canonical Smartsheet network boundary, so it hosts the
# breaker. The breaker mechanism itself is domain-agnostic
# (shared/circuit_breaker.py); the Smartsheet specifics — which exceptions
# count, which are ignored, and how config is read — are injected here.

_circuit_config_cache: circuit_breaker.CircuitConfig | None = None


def _coerce_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def _coerce_int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw is not None else default
    except (ValueError, TypeError):
        return default


def _read_global_setting(key: str) -> str | None:
    """Read one global ITS_Config setting; None if the row is missing or the
    read fails. Called only inside ``_load_circuit_config`` (under bypass)."""
    try:
        return get_setting(key, workstream="global")
    except SmartsheetError:
        return None


def _load_circuit_config() -> circuit_breaker.CircuitConfig:
    """Resolve ``circuit_breaker.*`` from ITS_Config, under
    ``circuit_breaker.bypass()`` so an OPEN breaker cannot block the read of its
    own ``enabled=false`` kill flag (escape-hatch layer 3). Cached for the
    process lifetime: launchd runs each daemon as a fresh process per cycle, so
    a per-process read picks up operator changes on the next cycle at the cost
    of at most one extra config round-trip per process. Any unreadable value
    falls back to ``defaults.py`` (→ ENABLED — a degraded Smartsheet still trips).
    """
    global _circuit_config_cache
    if _circuit_config_cache is not None:
        return _circuit_config_cache
    with circuit_breaker.bypass():
        enabled_raw = _read_global_setting("circuit_breaker.enabled")
        threshold_raw = _read_global_setting("circuit_breaker.failure_threshold")
        cooldown_raw = _read_global_setting("circuit_breaker.cooldown_seconds")
    cfg = circuit_breaker.CircuitConfig(
        enabled=_coerce_bool(enabled_raw, defaults.CIRCUIT_BREAKER_ENABLED),
        failure_threshold=_coerce_int(
            threshold_raw, defaults.CIRCUIT_BREAKER_FAILURE_THRESHOLD
        ),
        cooldown_seconds=_coerce_int(
            cooldown_raw, defaults.CIRCUIT_BREAKER_COOLDOWN_SECONDS
        ),
    )
    _circuit_config_cache = cfg
    return cfg


# Applied to every network-issuing method below. Reads + writes both count
# toward tripping; 401/403/404 are ignored (deterministic / routine — must
# surface as themselves, never as a degraded-service signal). NOTE:
# get_setting / get_settings_with_prefix are deliberately LEFT UNDECORATED —
# they delegate to the decorated get_rows, so guarding them too would nest the
# breaker and double-count a single failure.
_breaker_guard = circuit_breaker.guard(
    open_exc=SmartsheetCircuitOpenError,
    count=SmartsheetError,
    ignore=(SmartsheetAuthError, SmartsheetPermissionError, SmartsheetNotFoundError),
    config_loader=_load_circuit_config,
)


# ---- Bounded transient retry (reads only) --------------------------------
#
# The layer the SDK does NOT provide (see SmartsheetTransientError): ONE bounded
# in-process re-issue sequence for a 5xx / timeout, on IDEMPOTENT READS ONLY.
#
# A write is NEVER enrolled and never will be: Smartsheet has no idempotency key, so a
# timed-out add_rows may well have COMMITTED — a blind re-issue would duplicate the row.
# `_TRANSIENT_RETRY_ENROLLED` + the structural AST guard in tests/test_smartsheet_retry.py
# bind that for FUTURE helpers too, not just today's list.


@dataclass(frozen=True)
class RetryConfig:
    """Resolved ``smartsheet.retry.*`` settings for this process."""

    enabled: bool
    max_extra_attempts: int
    backoff_seconds: tuple[float, ...]
    #: Per-key "<key>=<ITS_Config|default>" summary — the observable-config-resolution
    #: standard (never resolve a setting without being able to say where it came from).
    source_summary: str


_retry_config_cache: RetryConfig | None = None
# Re-entrancy guard. COLD START without it: the first guarded call resolves the circuit
# config BEFORE calling fn; that read runs under `circuit_breaker.bypass()`, which
# short-circuits only the GUARD — the call still descends into get_rows' _transient_retry
# wrapper, which loads ITS retry config, which reads get_setting → get_rows → … forever.
# While a config read is in flight the retry decorator is a straight pass-through.
_loading_retry_config = False

_RETRY_BACKOFF_SEPARATORS = ","


def _coerce_backoff(raw: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    if raw is None:
        return default
    try:
        parsed = tuple(
            float(part.strip())
            for part in raw.split(_RETRY_BACKOFF_SEPARATORS)
            if part.strip()
        )
    except (ValueError, TypeError):
        return default
    return parsed or default


def _clamp_attempts(value: int) -> int:
    """Bound ``max_extra_attempts`` to [0, ceiling], WARNing when a row is out of range.

    An unbounded knob is an outage surface: a typo'd ``200`` would hold every daemon on
    one failing read for ~100 min. The dashboard's ``v_int`` validator rejects
    out-of-range up front; this clamp covers the value that got in by another path (a
    hand-edited ITS_Config cell), because the client — not the editor — is where the
    number actually costs wall-clock.
    """
    ceiling = defaults.SMARTSHEET_RETRY_MAX_ATTEMPTS_CEILING
    clamped = max(0, min(value, ceiling))
    if clamped != value:
        _local_warn(
            f"smartsheet.retry.max_extra_attempts={value} is out of range — "
            f"CLAMPED to {clamped} (allowed 0-{ceiling}). Fix the ITS_Config row."
        )
    return clamped


def _clamp_backoff(values: tuple[float, ...]) -> tuple[float, ...]:
    """Drop negatives and cap the SUMMED backoff of one sequence, WARNing when it bites.

    Capping the total (rather than each entry) is what actually bounds the cost: three
    entries of 20 s each are individually plausible and collectively a minute of sleep.
    Entries past the cap are truncated, so the sequence gets shorter, never longer.
    """
    cap = defaults.SMARTSHEET_RETRY_MAX_TOTAL_BACKOFF_SECS
    kept: list[float] = []
    total = 0.0
    for value in values:
        step = max(0.0, value)
        if total + step > cap:
            break
        kept.append(step)
        total += step
    out = tuple(kept)
    if out != values:
        _local_warn(
            f"smartsheet.retry.backoff_seconds={values} exceeds the {cap:g}s total cap "
            f"(or contains a negative) — TRUNCATED to {out}. Fix the ITS_Config row."
        )
    return out


def _defaults_retry_config() -> RetryConfig:
    return RetryConfig(
        enabled=defaults.SMARTSHEET_RETRY_ENABLED,
        max_extra_attempts=defaults.SMARTSHEET_RETRY_MAX_EXTRA_ATTEMPTS,
        backoff_seconds=defaults.SMARTSHEET_RETRY_BACKOFF_SECONDS,
        source_summary="enabled=default max_extra_attempts=default backoff_seconds=default",
    )


def _load_retry_config() -> RetryConfig:
    """Resolve ``smartsheet.retry.*`` from ITS_Config; ``defaults.py`` on any gap.

    Mirrors ``_load_circuit_config`` exactly (bypass-wrapped reads, process-lifetime
    cache — launchd gives each daemon a fresh process per cycle, so an operator change
    lands next cycle at the cost of one extra round-trip per process). Re-entrant calls
    (see ``_loading_retry_config``) get defaults WITHOUT caching them, so the real read
    still wins once the outer load completes.

    TOTAL by construction, like ``circuit_breaker._resolve_config``: resolving config must
    never be the thing that breaks the call it is configuring.
    """
    global _retry_config_cache, _loading_retry_config
    if _retry_config_cache is not None:
        return _retry_config_cache
    if _loading_retry_config:
        return _defaults_retry_config()
    _loading_retry_config = True
    try:
        with circuit_breaker.bypass():
            # ONE round-trip for all three knobs. `_load_circuit_config` above reads its
            # three keys one at a time; doing the same here DOUBLED the per-process
            # ITS_Config traffic (3 → 6) across ~12 daemons firing every 60-120 s, for
            # rows that share a prefix and live on the same sheet. `get_rows` is the
            # actual cost unit, and the prefix read spends exactly one of them.
            rows = get_settings_with_prefix("smartsheet.retry.", workstream="global")
    except Exception:  # noqa: BLE001 — a config read must never wedge the call it configures
        return _defaults_retry_config()
    finally:
        _loading_retry_config = False
    enabled_raw = rows.get("smartsheet.retry.enabled")
    attempts_raw = rows.get("smartsheet.retry.max_extra_attempts")
    backoff_raw = rows.get("smartsheet.retry.backoff_seconds")
    sources = " ".join(
        f"{name}={'ITS_Config' if raw is not None else 'default'}"
        for name, raw in (
            ("enabled", enabled_raw),
            ("max_extra_attempts", attempts_raw),
            ("backoff_seconds", backoff_raw),
        )
    )
    cfg = RetryConfig(
        enabled=_coerce_bool(enabled_raw, defaults.SMARTSHEET_RETRY_ENABLED),
        max_extra_attempts=_clamp_attempts(
            _coerce_int(attempts_raw, defaults.SMARTSHEET_RETRY_MAX_EXTRA_ATTEMPTS)
        ),
        backoff_seconds=_clamp_backoff(
            _coerce_backoff(backoff_raw, defaults.SMARTSHEET_RETRY_BACKOFF_SECONDS)
        ),
        source_summary=sources,
    )
    _retry_config_cache = cfg
    return cfg


# ---- Recovery visibility (D3) --------------------------------------------
#
# A retry that SUCCEEDS is invisible by construction — nothing raises, nothing is logged.
# A chronically flaky sheet would then be silently absorbed, which is the "never silent"
# invariant inverted. So every recovered sequence emits ONE local WARN line, AND is
# accumulated here for a pass-boundary summary row (drained by
# `sustained_failure.flush_retry_recovery`). A process NOT enrolled simply discards its
# accumulator at exit — the local WARN line still went to the on-disk log, so nothing is
# silent; only the ITS_Errors summary is skipped.
#
# WHICH PROCESSES FLUSH (verified 2026-07-21): every one-shot launchd pass with a clean
# exit point — the six intake daemons, the five bound send pollers via `send_poll_core`,
# `publish_daemon`, and `scripts/watchdog.py`. DELIBERATELY NOT `operator_dashboard`: it
# is a long-lived multi-THREADED FastAPI server with no pass boundary at all, so the only
# candidate site is per-HTTP-request — which would both race on this process-global dict
# across worker threads and mint an ITS_Errors row per browser poll of a read-only
# observation surface. Its recoveries stay local-log-only, by decision not by omission.
# `safety_reports/compile_now_poll.py` is owned by a parallel change and is untouched here.
_RETRY_RECOVERY_MAX_KEYS = 32
_RETRY_RECOVERY_OVERFLOW_KEY = "(other)"
_retry_recoveries: dict[str, dict[str, Any]] = {}


def _local_warn(message: str) -> None:
    """Best-effort local WARN via a lazy ``error_log`` import (error_log imports THIS
    module at top level — hence lazy). Never raises."""
    try:
        from . import error_log

        error_log.local_log(error_log.Severity.WARN, "shared.smartsheet_client", message)
    except Exception:  # noqa: BLE001 — logging must never break a recovered call
        pass


def _note_retry_recovery(
    call: str, attempts: int, elapsed: float, last_error: BaseException, cfg: RetryConfig
) -> None:
    _local_warn(
        f"transient Smartsheet failure RECOVERED on retry: call={call} "
        f"extra_attempts={attempts} elapsed={elapsed:.2f}s "
        f"last_error={type(last_error).__name__}: {last_error} "
        f"[retry config: {cfg.source_summary}]"
    )
    key = call
    if key not in _retry_recoveries and len(_retry_recoveries) >= _RETRY_RECOVERY_MAX_KEYS:
        key = _RETRY_RECOVERY_OVERFLOW_KEY
    entry = _retry_recoveries.setdefault(key, {"sequences": 0, "attempts": 0})
    entry["sequences"] += 1
    entry["attempts"] += attempts


def drain_retry_recovery() -> dict[str, dict[str, Any]]:
    """Return the accumulated recovered-retry summary and CLEAR it.

    Public because the pass-boundary fence (`shared.sustained_failure.TransientFence`)
    is in another module: it drains at end of pass and writes ONE summarized WARN
    ``ITS_Errors`` row, so a chronically flaky sheet stays visible on the dashboard
    instead of only in the local log.
    """
    drained = dict(_retry_recoveries)
    _retry_recoveries.clear()
    return drained


def _transient_retry[F: Callable[..., Any]](fn: F) -> F:
    """Re-issue ``fn`` up to ``max_extra_attempts`` times on SmartsheetTransientError.

    Retries NOTHING else — CircuitOpen / Validation / Auth / Permission / NotFound /
    RateLimit all propagate on the first raise, and the final transient failure is
    re-raised with its type UNCHANGED so callers keep their existing `except` clauses.

    PLACEMENT IS LOAD-BEARING — this must sit INSIDE `_breaker_guard`::

        @_breaker_guard
        @_transient_retry      # applied first (bottom-up) ⇒ runs INSIDE the guard
        def get_rows(...): ...

    The guard records at most one failure/success per wrapped call, so an exhausted
    3-attempt sequence is exactly ONE breaker failure and the breaker's
    consecutive-failure semantics are preserved. Retry OUTSIDE the guard would (a)
    triple the failure-count rate, tripping the breaker 3× sooner than configured, and
    (b) catch-and-sleep on SmartsheetCircuitOpenError — hammering the very
    short-circuit the breaker exists to provide.

    CONTROL-PLANE READS GET EXACTLY ONE ATTEMPT. A call made under
    ``circuit_breaker.bypass()`` is the breaker's / error_log's / the heartbeat's own
    plumbing, not the daemon's work: multiplying it turned a cold config bootstrap on a
    failing backend into 12 SDK calls and ~21 s of sleeps, on the very path that exists
    so an OPEN breaker cannot block it. Same shape as the ``_loading_retry_config``
    short-circuit below, one level out.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _loading_retry_config or circuit_breaker.in_bypass():
            return fn(*args, **kwargs)
        cfg = _load_retry_config()
        if not cfg.enabled or cfg.max_extra_attempts <= 0:
            return fn(*args, **kwargs)
        started = time.monotonic()
        extra = 0
        last_error: BaseException | None = None
        while True:
            try:
                result = fn(*args, **kwargs)
            except SmartsheetTransientError as exc:
                if extra >= cfg.max_extra_attempts:
                    raise
                last_error = exc
                if cfg.backoff_seconds:
                    time.sleep(cfg.backoff_seconds[min(extra, len(cfg.backoff_seconds) - 1)])
                extra += 1
                continue
            if extra and last_error is not None:
                _note_retry_recovery(fn.__name__, extra, time.monotonic() - started, last_error, cfg)
            return result

    wrapper.__its_transient_retry__ = True  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]


#: Every helper carrying `_transient_retry`. READS / IDEMPOTENT LOOKUPS ONLY — a write is
#: never enrollable (no idempotency key ⇒ a timed-out write may have committed). Held as
#: data so `tests/test_smartsheet_retry.py` can assert SET EQUALITY against the approved
#: list: adding an enrollment forces a deliberate test edit, and the companion AST guard
#: independently proves no enrolled body reaches an SDK mutator or requests.post/put/delete.
#: `get_setting` / `get_settings_with_prefix` are deliberately absent — they delegate to the
#: enrolled `get_rows` and inherit the retry (same reason they carry no breaker guard). That
#: single fact is what covers both 2026-07-21 CRITICAL paths (publish_daemon's config read
#: and progress_send_poll's approver read).
_TRANSIENT_RETRY_ENROLLED: frozenset[str] = frozenset({
    "get_sheet",
    "get_row",
    "get_rows",
    "get_cell_history",
    "list_columns_with_options",
    "find_sheet_by_name_in_folder",
    "count_workspace_sheets",
    "find_folder_by_name_in_folder",
    "find_folder_by_name_in_workspace",
    "list_workspace_share_emails",
    "get_workspace_name",
    # Track 6 archive primitives — pure GETs. The three folder WRITES that accompany them
    # (move_folder_to_folder / move_folder_to_workspace / rename_folder) are deliberately
    # absent: an archive's correct retry is durable and cross-cycle, not in-process.
    "get_folder_name",
    "get_workspace_access_level",
})


def is_transient_error(exc: BaseException) -> bool:
    """True iff ``exc`` is the precisely-typed self-healing Smartsheet class.

    THE CONTRACT, stated as a promise the code keeps rather than an aspiration: True means
    "re-issuing the SAME call is safe". It is therefore NEVER True for a failed WRITE. Both
    translation paths enforce that at the raise site — ``_translate(…, idempotent=…)`` for
    the SDK helpers and ``_translate_smartsheet_error(…, idempotent=…)`` for the raw-REST
    ones — because only the raise site knows whether the call was a read. A 5xx or a
    timeout on ``add_rows`` / ``update_rows`` / ``delete_rows`` / an attach / a create
    carries NO information about whether it committed, and Smartsheet has no idempotency
    key to settle it, so those raise base ``SmartsheetError`` and this answers False.
    ``tests/test_smartsheet_client.py::test_no_sdk_write_helper_is_ever_classified_retry_safe``
    holds the line for helpers added later.

    Defined in terms of the TYPE, never as "not one of the deterministic subclasses":
    a future subclass must be classified deliberately at its definition site, not
    inherit transience by omission from one mechanism and determinism from another.

    SmartsheetCircuitOpenError and SmartsheetRateLimitError are deliberately EXCLUDED
    and handled explicitly by `sustained_failure.TransientFence` — see its docstring.

    WHY IT MATTERS. ``sustained_failure.TransientFence.handle`` consults this predicate to
    decide whether a pass-boundary failure is SOFTENED (halt + counted ERROR) or left to
    ``@its_error_log``'s immediate ``uncaught_exception`` CRITICAL. A write must never be
    softened: "cycle halted, retrying next cycle" is the wrong story for an operation that
    may already have committed.
    """
    return isinstance(exc, SmartsheetTransientError)


# Register the same loader so arg-free circuit_breaker.is_open() (the daemons'
# CIRCUIT_OPEN status surfacing) resolves live Smartsheet config too.
circuit_breaker.set_config_loader(_load_circuit_config)


def _close_fleet_circuit_open_window() -> None:
    """Breaker-recovery hook: a real call answered, so the fleet outage window is over.

    LOCAL import, deliberately: `sustained_failure` imports THIS module (for the exception
    types + `is_transient_error`), so a module-level import here is circular. Registering
    the hook from `smartsheet_client` — rather than from `sustained_failure` — is what
    makes it present in EVERY Smartsheet-touching process, including the daemons that own
    no fence; otherwise a window opened by a fenced daemon could only ever be closed by a
    fenced daemon.
    """
    from shared import sustained_failure

    sustained_failure.clear_circuit_open()


circuit_breaker.set_recovery_hook(_close_fleet_circuit_open_window)


# ---- Column-map cache ----------------------------------------------------


def _fetch_column_map(sheet_id: int) -> dict[str, int]:
    try:
        sheet = get_client().Sheets.get_sheet(sheet_id, include="columns")
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=True) from e
    return {col.title: col.id for col in sheet.columns}


def _column_map(sheet_id: int) -> dict[str, int]:
    cached = _column_maps.get(sheet_id)
    if cached is None:
        cached = _fetch_column_map(sheet_id)
        _column_maps[sheet_id] = cached
    return cached


def invalidate_column_cache(sheet_id: int | None = None) -> None:
    """Drop cached column maps. Call after a known schema change.

    Without `sheet_id`, drops every entry.
    """
    if sheet_id is None:
        _column_maps.clear()
    else:
        _column_maps.pop(sheet_id, None)


def _resolve_cells(sheet_id: int, values: dict[str, Any]) -> list[Any]:
    """Build SDK Cell objects for a row from a {title: value} dict.

    On any title that isn't in the cached column map, refetches the map once
    before giving up — see module docstring for the rename-breaks-cache
    failure mode.

    Optional per-cell formatting: a `_formats` meta key (`{title: format_str}`,
    where `format_str` is a Smartsheet format-descriptor string) attaches that
    format to the matching cell. `_`-prefixed keys are meta — never treated as
    column titles (so callers can pass `_formats` alongside the cell values).

    A list/tuple/set value targets a MULTI_PICKLIST column (PO S1 — ITS_Vendors
    Supply Categories is the first): the API rejects a plain `value` there, so the
    cell is built with a MULTI_PICKLIST `objectValue` instead. Elements are
    stringified; unordered collections are sorted for a deterministic payload.
    Picklist validation of the elements happens upstream in
    `picklist_validation.validate_cell` (list-aware since the same change).
    """
    formats: dict[str, str] = values.get("_formats") or {}
    values = {k: v for k, v in values.items() if not k.startswith("_")}
    columns = _column_map(sheet_id)
    if any(title not in columns for title in values):
        invalidate_column_cache(sheet_id)
        columns = _column_map(sheet_id)

    cells = []
    for title, value in values.items():
        if title not in columns:
            raise KeyError(
                f"Column {title!r} not found in sheet {sheet_id}. "
                f"Available: {sorted(columns)}"
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            ordered = list(value) if isinstance(value, (list, tuple)) else sorted(value, key=str)
            multi_cell = smartsheet.models.Cell()
            multi_cell.column_id = columns[title]
            multi_cell.object_value = smartsheet.models.MultiPicklistObjectValue(
                {"values": [str(v) for v in ordered]}
            )
            if title in formats:
                multi_cell.format = formats[title]
            cells.append(multi_cell)
            continue
        cell_dict: dict[str, Any] = {"column_id": columns[title], "value": value}
        if title in formats:
            cell_dict["format"] = formats[title]
        cells.append(smartsheet.models.Cell(cell_dict))
    return cells


# ---- Reads ---------------------------------------------------------------


@_breaker_guard
@_transient_retry
def get_sheet(sheet_id: int):
    """Fetch the full sheet object (SDK model). Most callers want get_rows()."""
    try:
        return get_client().Sheets.get_sheet(sheet_id)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=True) from e


@_breaker_guard
@_transient_retry
def get_row(sheet_id: int, row_id: int) -> dict[str, Any]:
    """Fetch one row by ID as a `{_row_id, <title>: value, ...}` dict.

    Raises `SmartsheetNotFoundError` if the row was deleted. Use this when
    the caller knows the row_id (e.g. a polling daemon dispatching to a
    per-event handler) and wants to avoid the full-sheet scan that
    `get_rows()` requires.
    """
    try:
        sheet = get_client().Sheets.get_sheet(sheet_id)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=True) from e
    title_by_id = {col.id: col.title for col in sheet.columns}
    for row in sheet.rows:
        if row.id != row_id:
            continue
        record: dict[str, Any] = {"_row_id": row.id}
        for cell in row.cells:
            title = title_by_id.get(cell.column_id)
            if title is not None:
                record[title] = cell.value
        return record
    raise SmartsheetNotFoundError(
        f"row_id={row_id} not found in sheet {sheet_id}"
    )


@_breaker_guard
@_transient_retry
def get_rows(
    sheet_id: int,
    *,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return rows as `{_row_id: int, <title>: value, ...}` dicts.

    `filters` is an equality-AND match applied client-side. Use only on
    sheets small enough to fetch in one round-trip (config, time-off, etc.);
    big logs should use Reports or scoped row queries.
    """
    try:
        sheet = get_client().Sheets.get_sheet(sheet_id)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=True) from e

    title_by_id = {col.id: col.title for col in sheet.columns}
    out: list[dict[str, Any]] = []
    for row in sheet.rows:
        record: dict[str, Any] = {"_row_id": row.id}
        for cell in row.cells:
            title = title_by_id.get(cell.column_id)
            if title is not None:
                record[title] = cell.value
        if filters and not all(record.get(k) == v for k, v in filters.items()):
            continue
        out.append(record)
    return out


def get_settings_with_prefix(
    prefix: str,
    *,
    workstream: str | None = None,
) -> dict[str, str]:
    """Return all ITS_Config rows whose `Setting` starts with `prefix`.

    Mirrors `get_setting`'s row-shape assumptions but iterates instead of
    raising. Returns a `{setting_key: value_str}` dict. Rows whose `Value`
    cell is not a string are skipped (matches `get_setting`'s contract).

    `workstream` narrows results to one workstream when set; default
    returns all matching rows across workstreams. Used by
    `scripts/watchdog.py` Check F to enumerate `mail_intake.*` rows
    without knowing the workstream slugs up front.
    """
    filters: dict[str, Any] = {}
    if workstream is not None:
        filters["Workstream"] = workstream
    rows = get_rows(sheet_ids.SHEET_CONFIG, filters=filters)
    out: dict[str, str] = {}
    for row in rows:
        setting = row.get("Setting")
        value = row.get("Value")
        if (
            isinstance(setting, str)
            and setting.startswith(prefix)
            and isinstance(value, str)
        ):
            out[setting] = value
    return out


def get_setting(key: str, *, workstream: str) -> str | None:
    """Read one Setting from ITS_Config, scoped to a workstream.

    Workstream is required — `ITS_Config` rows are keyed on (Setting,
    Workstream), and silently defaulting hides config misses.

    Returns the cell value as a string, or `None` if the row exists but
    the Value cell is empty / non-string. Raises `SmartsheetNotFoundError`
    if no row matches at all — callers distinguish "row missing" from
    "row found but blank Value" by which path triggers.
    """
    rows = get_rows(
        sheet_ids.SHEET_CONFIG,
        filters={"Setting": key, "Workstream": workstream},
    )
    if not rows:
        raise SmartsheetNotFoundError(
            f"ITS_Config has no row for Setting={key!r} Workstream={workstream!r}"
        )
    value = rows[0].get("Value")
    return value if isinstance(value, str) else None


# ---- Cell history --------------------------------------------------------


@dataclass(frozen=True)
class CellHistoryEvent:
    """One modification event from a cell's Smartsheet history.

    SDK-decoupled view of `smartsheet.models.CellHistory` so consumers
    (`shared.approval_verification`) need not import the SDK and the F02
    network allowlist stays honest — this module is the network boundary.

    Identity note: Smartsheet's cell-history `modifiedBy` returns only
    `{name, email}` — there is NO user ID in that payload (confirmed
    against the documented API shape). `actor_user_id` is therefore
    populated opportunistically (None today) for forensic logging and
    future-proofing, but `actor_email` is the only stable match key
    available to callers.
    """
    value: Any
    actor_email: str | None
    actor_name: str | None
    actor_user_id: int | None
    modified_at: datetime | None


@_breaker_guard
@_transient_retry
def get_cell_history(
    sheet_id: int, row_id: int, column_title: str
) -> list[CellHistoryEvent]:
    """Return the modification history of one cell as `CellHistoryEvent`s.

    Resolves `column_title` → column ID via the per-sheet title cache
    (same refresh-once-on-miss semantics as `_resolve_cells`), then calls
    the Smartsheet `GET /sheets/{id}/rows/{id}/columns/{id}/history`
    endpoint via the SDK with `include_all=True` (no pagination — a single
    cell's history is bounded).

    Ordering follows the Smartsheet API (reverse-chronological, newest
    first); callers that need a strict ordering should sort on
    `modified_at` rather than trust list position.

    Raises the typed `SmartsheetError` hierarchy on API failure (404 for a
    deleted row, 401/403 for auth/permission) and `KeyError` for an unknown
    column title — consistent with `_resolve_cells`. `shared.approval_verification`
    calls THIS, never `Cells.get_cell_history` directly, so the network
    egress stays inside the audited `*_client` boundary (audit F02).
    """
    columns = _column_map(sheet_id)
    if column_title not in columns:
        invalidate_column_cache(sheet_id)
        columns = _column_map(sheet_id)
    if column_title not in columns:
        raise KeyError(
            f"Column {column_title!r} not found in sheet {sheet_id}. "
            f"Available: {sorted(columns)}"
        )
    column_id = columns[column_title]

    try:
        result = get_client().Cells.get_cell_history(
            sheet_id, row_id, column_id, include_all=True
        )
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=True) from e

    events: list[CellHistoryEvent] = []
    for item in result.data:
        modified_by = getattr(item, "modified_by", None)
        events.append(
            CellHistoryEvent(
                value=item.value,
                actor_email=getattr(modified_by, "email", None),
                actor_name=getattr(modified_by, "name", None),
                # SDK property is `id_` (trailing underscore); None when the
                # cell-history payload omits it, which is always today.
                actor_user_id=getattr(modified_by, "id_", None),
                modified_at=getattr(item, "modified_at", None),
            )
        )
    return events


# ---- Writes --------------------------------------------------------------


@_breaker_guard
def add_rows(sheet_id: int, rows: list[dict[str, Any]]) -> list[int]:
    """Append rows to a sheet. Returns the new row IDs in input order.

    Each entry in `rows` is a `{column_title: value}` dict. Rows are
    appended to the bottom — change at the call site if a different
    position is needed.

    Pre-write picklist validation (Op Stds v11 §35): each row passes
    through `picklist_validation.validate_row` first. Unregistered
    (sheet, column) pairs pass-through; registered cells whose value
    is outside the allowed set raise `PicklistViolationError` BEFORE any
    Smartsheet API call. Late-import to avoid the
    picklist_validation → kill_switch → smartsheet_client cycle.
    """
    if not rows:
        return []
    from . import picklist_validation
    for row_dict in rows:
        picklist_validation.validate_row(sheet_id, row_dict)
    sdk_rows = []
    for values in rows:
        row = smartsheet.models.Row()
        row.to_bottom = True
        row.cells = _resolve_cells(sheet_id, values)
        sdk_rows.append(row)
    try:
        result = get_client().Sheets.add_rows(sheet_id, sdk_rows)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e
    return [r.id for r in result.result]


@_breaker_guard
def update_rows(sheet_id: int, updates: list[dict[str, Any]]) -> None:
    """Update existing rows. Each update is `{_row_id: int, <title>: value, ...}`.

    Cells whose column titles aren't supplied are left untouched.

    Pre-write picklist validation: same contract as `add_rows`.
    `_row_id` and any other `_`-prefixed meta keys are skipped during
    validation (they're not Smartsheet columns).
    """
    if not updates:
        return
    from . import picklist_validation
    for row_dict in updates:
        picklist_validation.validate_row(sheet_id, row_dict)
    sdk_rows = []
    for values in updates:
        row_id = values.get("_row_id")
        if row_id is None:
            raise ValueError("update_rows entry missing required '_row_id'")
        cells_payload = {k: v for k, v in values.items() if k != "_row_id"}
        row = smartsheet.models.Row()
        row.id = row_id
        row.cells = _resolve_cells(sheet_id, cells_payload)
        sdk_rows.append(row)
    try:
        get_client().Sheets.update_rows(sheet_id, sdk_rows)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e


@_breaker_guard
def delete_rows(sheet_id: int, row_ids: list[int]) -> None:
    """Delete rows by ID. Smartsheet caps at 450 IDs per call."""
    if not row_ids:
        return
    try:
        get_client().Sheets.delete_rows(sheet_id, row_ids)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e


@_breaker_guard
def find_row_by_primary(
    sheet_id: int,
    primary_column_id: int,
    value: Any,
) -> dict[str, Any] | None:
    """Return the first row whose primary column equals `value`, or None.

    Primary-key lookup by column ID (not title) so the call site is robust
    against column renames. Used by daemon-style consumers (PR #59.5
    heartbeat write) that maintain a row-id state-file cache and need a
    cheap one-shot lookup on first write or cache invalidation.

    Returns a `{_row_id, <title>: value, ...}` dict on match, or None when
    no row contains a matching cell. Iterates the full sheet client-side
    — only safe on sheets bounded in size (ITS_Daemon_Health is one row
    per daemon, ITS_Config is a couple dozen rows). Bigger sheets need a
    Reports-backed query path; this function is NOT that.
    """
    try:
        sheet = get_client().Sheets.get_sheet(sheet_id)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=True) from e

    title_by_id = {col.id: col.title for col in sheet.columns}
    for row in sheet.rows:
        for cell in row.cells:
            if cell.column_id == primary_column_id and cell.value == value:
                record: dict[str, Any] = {"_row_id": row.id}
                for c in row.cells:
                    title = title_by_id.get(c.column_id)
                    if title is not None:
                        record[title] = c.value
                return record
    return None


@_breaker_guard
def update_row_cells_by_id(
    sheet_id: int,
    row_id: int,
    cells_by_column_id: dict[int, Any],
) -> None:
    """Update one row's cells, keyed by column ID instead of column title.

    The title-based `update_rows` is the right call when the schema is
    column-rename-stable (most ITS sheets). For daemon heartbeat writes
    where the column IDs are committed in `sheet_ids.DAEMON_HEALTH_COLUMNS`
    and we want write paths that survive a title rename without code
    changes, this ID-based helper is the right shape. No title-cache
    lookup happens — the IDs are the authoritative reference.

    Raises `SmartsheetNotFoundError` if the row no longer exists (e.g.,
    the daemon-health row was re-seeded after a column reset); the
    caller's row-id cache should invalidate on this signal.
    """
    if not cells_by_column_id:
        return
    cells = [
        smartsheet.models.Cell({"column_id": col_id, "value": value})
        for col_id, value in cells_by_column_id.items()
    ]
    row = smartsheet.models.Row()
    row.id = row_id
    row.cells = cells
    try:
        get_client().Sheets.update_rows(sheet_id, [row])
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e


@_breaker_guard
def add_row_by_id(sheet_id: int, cells_by_column_id: dict[int, Any]) -> int:
    """Append one row, cells keyed by column ID instead of column title.

    The ID-keyed sibling of `update_row_cells_by_id`. Use it for the same
    reason that helper exists: daemon control-plane rows (e.g. a daemon
    self-provisioning its `ITS_Daemon_Health` visibility row) whose column
    IDs are committed in `sheet_ids.DAEMON_HEALTH_COLUMNS` and which want a
    create path that survives a column-title rename without a code change.
    No title-cache lookup happens — the IDs are the authoritative reference,
    so a value cannot land in the wrong column via a stale title map.

    Returns the new row ID. Unlike the title-keyed `add_rows`, this skips the
    `picklist_validation` pass: that registry is title-keyed, and the
    ID-keyed control-plane sheets this serves (ITS_Daemon_Health) are not
    registered there (validation would be a pass-through anyway). Callers
    must pass a non-empty payload that includes the sheet's primary-key cell.
    """
    cells = [
        smartsheet.models.Cell({"column_id": col_id, "value": value})
        for col_id, value in cells_by_column_id.items()
    ]
    row = smartsheet.models.Row()
    row.to_bottom = True
    row.cells = cells
    try:
        result = get_client().Sheets.add_rows(sheet_id, [row])
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e
    return result.result[0].id


# ---- Column + sheet helpers (PICKLIST sync) -----------------------------


@_breaker_guard
@_transient_retry
def list_columns_with_options(sheet_id: int) -> list[dict[str, Any]]:
    """Return one dict per column with `id`, `title`, `type`, and `options`.

    `options` is the picklist option list when the column is `PICKLIST` /
    `MULTI_PICKLIST`; an empty list otherwise. Used by
    `shared.picklist_sync` to read current downstream picklist state
    before computing a diff against the source master DB.

    Fetched at `level=2` so a `MULTI_PICKLIST` (multi-select dropdown) column reports its
    true type — by default the API downgrades it to `TEXT_NUMBER` (see the call-site note),
    which silently broke option management on live multi-select columns.

    Bypasses the column-title cache because picklist sync needs the
    `options` field (the cache only stores `{title: id}` for cell-write
    resolution). A direct `get_sheet` is the right shape here.

    `type` is returned as a plain string (e.g. `"PICKLIST"`), NOT as the
    SDK's `EnumeratedValue` wrapper. Callers feeding `type` back into a
    Column body for `update_column_options` need the string form — the
    SDK's deserializer can't set an EnumeratedValue field from another
    EnumeratedValue object and silently strips it, which produces a body
    without `type` and triggers errorCode 1090 on the API side.
    Surfaced live during the PR #48 re-smoke.
    """
    try:
        # level=2 is LOAD-BEARING: without it the API DOWNGRADES a MULTI_PICKLIST
        # (multi-select dropdown) column to TEXT_NUMBER in the response — options still
        # attached, but type mis-reported. That made `ensure_picklist_options` REFUSE to
        # manage a live multi-select column (it type-checks for PICKLIST/MULTI_PICKLIST) and
        # made `audit_picklist_drift` false-flag it as "needs a manual UI conversion". With
        # level=2 MULTI_PICKLIST reports its true type. (Confirmed live 2026-07-14:
        # ITS_Subcontractors.Trades + ITS_Vendors.Supply Categories read as MULTI_PICKLIST;
        # single-value CONTACT_LIST columns e.g. Approved By/Modified By are UNAFFECTED —
        # verified. level=2 also un-downgrades MULTI_CONTACT_LIST, but the ITS schema uses
        # none today, so that path is unexercised.)
        sheet = get_client().Sheets.get_sheet(sheet_id, include="columns", level=2)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=True) from e
    out: list[dict[str, Any]] = []
    for col in sheet.columns:
        opts = getattr(col, "options", None) or []
        # col.type is an EnumeratedValue wrapper; .value is the ColumnType
        # enum member; .name is the picklist-friendly string ("PICKLIST",
        # "TEXT_NUMBER", etc.). Defensively fall back to str() if the
        # SDK shape ever changes.
        col_type = col.type
        type_str: str
        if hasattr(col_type, "value") and hasattr(col_type.value, "name"):
            type_str = col_type.value.name
        else:
            type_str = str(col_type)
        out.append({
            "id": col.id,
            "title": col.title,
            "type": type_str,
            "options": list(opts),
        })
    return out


@_breaker_guard
def update_column_options(
    sheet_id: int, column_id: int, options: list[str], *, column_type: str
) -> None:
    """Replace a PICKLIST column's options list with `options`.

    Smartsheet's `PUT /sheets/{sheetId}/columns/{columnId}` accepts an
    `options` array; the server replaces the whole list. Pass an
    alphabetized list when stable order matters (R5 — Smartsheet does
    not guarantee API-side preservation otherwise).

    Body shape requirements discovered live (PR #47 → PR #48):
      - `id` must NOT appear in the body (errorCode 1032; the column ID
        lives in the URL path).
      - `type` IS required when changing `options` (errorCode 1090).
        Caller passes it explicitly because callers already have the
        column type from list_columns_with_options(); fetching it here
        would mean an extra round-trip per write.

    Expected `column_type` values: "PICKLIST" or "MULTI_PICKLIST". Other
    values are accepted but the API will reject any type that doesn't
    take an options array.

    Invalidates the column-title cache for the sheet because picklist
    edits don't change titles but the cache may be stale if titles were
    edited in the same UI session.
    """
    try:
        body = smartsheet.models.Column({
            "type": column_type,
            "options": list(options),
        })
        get_client().Sheets.update_column(sheet_id, column_id, body)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e
    invalidate_column_cache(sheet_id)


@dataclass(frozen=True)
class EnsureOptionsResult:
    """Outcome of an `ensure_picklist_options` call.

    `added` is empty on an idempotent no-op (every requested value already
    present) AND on the precondition where nothing was missing. `applied` is
    True only when an API write actually happened (False on dry-run and on a
    no-op). `final_options` is the option list after the call (== current when
    nothing changed).
    """

    column: str
    column_id: int
    already_present: tuple[str, ...]
    added: tuple[str, ...]
    final_options: tuple[str, ...]
    applied: bool


def ensure_picklist_options(
    sheet_id: int,
    column: str,
    values: Iterable[str],
    *,
    dry_run: bool = False,
) -> EnsureOptionsResult:
    """Additively ensure `values` are present on a PICKLIST column. Never removes.

    Purpose
        The additive complement to `update_column_options` (which is
        REPLACE-style: it overwrites the whole options array). Used to reconcile
        a live picklist UP TO a canonical superset — e.g. push the three
        `ReviewReason` enum values the live ITS_Review_Queue `Reason` picklist
        lacks (picklist-drift reconcile, the documented-but-undone operator step
        at `review_queue.py`) — without disturbing existing options or their
        order.

    Invariants
        - **Additive only.** The resulting list is `current + missing`, where
          `missing` are the requested values not already present, in the order
          they appear in `values`. Existing options are never removed and their
          relative order is preserved (the operator's dropdown doesn't reshuffle).
        - **Idempotent.** When every requested value is already present, NO API
          write is issued (`applied=False`, `added=()`); re-running is a no-op.
        - **Does not create columns.** A title that doesn't resolve to an
          existing PICKLIST/MULTI_PICKLIST column raises `ValueError` — adding a
          missing *column* is a separate, deliberate schema change, not a
          side effect of an option add.
        - **Preview-able.** `dry_run=True` computes `added`/`final_options`
          without writing, so a caller can log the proposed change set first.

    Failure modes
        - Column title absent, or present but not an option-bearing type →
          `ValueError` (fail loud; this is a caller/config error).
        - Underlying read/write raises the typed `SmartsheetError` hierarchy
          (incl. `SmartsheetCircuitOpenError` when the breaker is OPEN — this
          helper is not breaker-guarded itself; `list_columns_with_options` is
          not guarded but `update_column_options` is, so a circuit-open surfaces
          on apply, not preview).

    Consumers
        Picklist-drift remediation (`scripts/`-side apply + the §30 integration
        test). NOT wired into any daemon hot path — option edits are an
        operator/maintenance action, not per-cycle work.
    """
    cols = list_columns_with_options(sheet_id)
    target = next((c for c in cols if c["title"] == column), None)
    if target is None:
        raise ValueError(
            f"ensure_picklist_options: column {column!r} not found on sheet "
            f"{sheet_id} (this helper never creates columns)"
        )
    col_type = target["type"]
    if col_type not in ("PICKLIST", "MULTI_PICKLIST"):
        raise ValueError(
            f"ensure_picklist_options: column {column!r} is type {col_type!r}, "
            f"not an option-bearing PICKLIST/MULTI_PICKLIST"
        )

    current = list(target["options"])
    current_set = set(current)
    # `values` may be a one-shot iterable (e.g. a generator); classify each
    # requested value as missing-vs-already in a SINGLE pass so we never
    # re-iterate an exhausted iterable (which would silently empty
    # `already_present`). Dedup within the request and skip falsy values
    # consistently for BOTH buckets.
    missing: list[str] = []
    already_list: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not v or v in seen:
            continue
        seen.add(v)
        (already_list if v in current_set else missing).append(v)
    already = tuple(already_list)

    if not missing:
        return EnsureOptionsResult(
            column=column,
            column_id=int(target["id"]),
            already_present=already,
            added=(),
            final_options=tuple(current),
            applied=False,
        )

    final = current + missing
    if dry_run:
        return EnsureOptionsResult(
            column=column,
            column_id=int(target["id"]),
            already_present=already,
            added=tuple(missing),
            final_options=tuple(final),
            applied=False,
        )

    update_column_options(
        sheet_id, int(target["id"]), final, column_type=col_type
    )
    return EnsureOptionsResult(
        column=column,
        column_id=int(target["id"]),
        already_present=already,
        added=tuple(missing),
        final_options=tuple(final),
        applied=True,
    )


@_breaker_guard
def create_picklist_column(
    sheet_id: int,
    title: str,
    options: list[str],
    *,
    index: int | None = None,
    restrict_to_options: bool = False,
) -> int:
    """Add a NEW PICKLIST column to a sheet and return its column ID.

    Purpose
        The create-side complement to `update_column_options`/`ensure_picklist_options`
        (both of which *edit an existing* option-bearing column and raise if the
        column is absent). This adds a column that does not exist yet — the
        deliberate schema change behind the Phase 3a "add the dormant column"
        decision (e.g. ITS_Errors `Workstream`, ITS_Quarantine `Disposition`),
        seeded with its `picklist_validation.REGISTRY` allowed set so the
        `audit_picklist_drift` allowed-set check passes immediately rather than
        flipping a "NOT PRESENT" finding into an "allowed-set mismatch" one.

    Invariants
        - **Additive only.** Creates a new column; never edits or removes an
          existing column. Idempotency is the CALLER's job — use
          `list_columns_with_options` to skip when the title already exists
          (same contract as `create_sheet_in_folder`). Re-running blindly would
          create a duplicate-titled column.
        - **Append by default.** `index=None` appends after the last existing
          column (one extra read to count them — acceptable for a maintenance
          helper, not a hot path). Pass an explicit 0-based `index` to insert
          elsewhere; Smartsheet's `POST /columns` *requires* an index.
        - **Plain PICKLIST, restrict-off by default.** `restrict_to_options=False`
          mirrors the rest of the sandbox sheets (server-side "restrict to
          dropdown values only" is the separate hardening sweep,
          `docs/audits/picklist_hardening_audit.md`). Pass `restrict_to_options=True`
          to set `validation=True` at creation. NOTE: `list_columns_with_options`
          does not read back `validation`, so the restrict flag is covered only
          at the unit-body level, not the §30 live round-trip.

    Failure modes
        - Underlying SDK error surfaces as the typed `SmartsheetError` hierarchy
          (401→Auth, 403→Permission, etc.); `SmartsheetCircuitOpenError` when the
          breaker is OPEN (this helper IS `@_breaker_guard`-decorated — a column
          create is a write).
        - Invalidates the sheet's column-title cache on success so a later
          title→id lookup resolves the new column.

    Consumers
        `scripts/migrations/add_dormant_picklist_columns.py` (Phase 3a) + its §30
        integration test. NOT wired into any daemon hot path — adding a column is
        an operator/maintenance schema action, not per-cycle work.
    """
    if index is None:
        # Append after the last existing column. One extra read; a column add
        # is a one-shot maintenance action, so the round-trip cost is moot.
        index = len(list_columns_with_options(sheet_id))
    body = smartsheet.models.Column({
        "title": title,
        "type": "PICKLIST",
        "index": index,
        "options": list(options),
    })
    if restrict_to_options:
        body.validation = True
    try:
        result = get_client().Sheets.add_columns(sheet_id, [body])
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e
    invalidate_column_cache(sheet_id)
    added = result.result
    # `add_columns` returns the created Column(s) under `.result` (a list even
    # for a single add). Defensive: accept a bare object if the SDK shape drifts.
    first = added[0] if isinstance(added, (list, tuple)) else added
    return int(first.id)


def _translate_smartsheet_error(
    response: requests.Response, *, context: str, idempotent: bool
) -> None:
    """Raise a typed `SmartsheetError` for a non-2xx REST response.

    No-op on 2xx — callers continue. On 4xx/5xx, dispatch the status code
    onto the same typed-exception hierarchy used by `_translate` for SDK
    errors (401 → Auth, 403 → Permission, 404 → NotFound, 429 → RateLimit,
    everything else → base `SmartsheetError`).

    `idempotent` — REQUIRED, no default, so a new REST helper must decide rather than
    inherit. It gates the 5xx branch ONLY:

      * `True`  (a GET / lookup) → `SmartsheetTransientError`, which
        `is_transient_error()` reports as retry-safe.
      * `False` (a CREATE) → base `SmartsheetError`. A 5xx on a create carries NO
        information about whether the folder/sheet was committed before the server
        errored, and there is no idempotency key to settle it. Labelling that
        "transient" is a claim of retry-safety the caller cannot honour: nothing retries
        creates today, but `is_transient_error()` is a PUBLIC predicate, and a future
        fence/retry consumer trusting it would duplicate a created folder or sheet. The
        classification is narrowed at the raise site so the predicate cannot lie, rather
        than relying on every future consumer to know which helpers are writes.
        (Matches main's pre-2026-07-21 behaviour for these three creates exactly.)

    Internal helper for the REST-backed helpers below (`find_sheet_by_name_in_folder`,
    `find_folder_by_name_in_folder`, `create_folder_in_folder`,
    `create_sheet_in_folder_from_template`). Reached the §14 abstraction
    threshold at PR #54 (4 REST helpers sharing identical dispatch).

    `context` is prepended to the error message so operator-facing logs
    identify which REST operation failed without needing a stack trace —
    e.g. "creating folder in parent 12345: HTTP 500: ...".

    Internally drives off `response.raise_for_status()` rather than direct
    `response.ok` / `status_code` inspection so the existing
    `requests.HTTPError`-shaped mock fixtures in
    `tests/test_smartsheet_client.py` continue to exercise the dispatch
    without per-fixture `.ok` configuration.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        resp = e.response if e.response is not None else response
        status = resp.status_code if resp is not None else 0
        body_text = ((resp.text or "")[:200]) if resp is not None else str(e)
        if status == 400:
            raise SmartsheetValidationError(f"{context}: HTTP 400: {body_text}") from e
        if status == 401:
            raise SmartsheetAuthError(f"{context}: HTTP 401: {body_text}") from e
        if status == 403:
            raise SmartsheetPermissionError(f"{context}: HTTP 403: {body_text}") from e
        if status == 404:
            raise SmartsheetNotFoundError(f"{context}: HTTP 404: {body_text}") from e
        if status == 429:
            raise SmartsheetRateLimitError(f"{context}: HTTP 429: {body_text}") from e
        if status >= 500 and idempotent:
            raise SmartsheetTransientError(f"{context}: HTTP {status}: {body_text}") from e
        if status >= 500:
            raise SmartsheetError(
                f"{context}: HTTP {status}: {body_text} — NON-IDEMPOTENT operation: the "
                "server errored, so it is UNKNOWN whether this create committed. Do NOT "
                "auto-retry; re-run the find-or-create path, which is safe."
            ) from e
        raise SmartsheetError(f"{context}: HTTP {status}: {body_text}") from e


@_breaker_guard
@_transient_retry
def find_sheet_by_name_in_folder(folder_id: int, name: str) -> int | None:
    """Return the sheet ID with title `name` inside `folder_id`, or None.

    Used by migrations + `picklist_sync` to check "does this sheet
    already exist?" before issuing a `create_sheet_in_folder` POST — the
    idempotency pattern from the PR α migration generalizes here.

    Implemented via direct REST (`GET /folders/{id}`) rather than the
    SDK's `Folders.get_folder()` for two reasons surfaced live during
    the PR #50 integration-test run on 2026-05-21:

    1. `Folders.get_folder()` is deprecated upstream (emits
       DeprecationWarning).
    2. The deprecated method returns stale folder data within a single
       SDK client session — a sheet created via the SDK's
       `create_sheet_in_folder()` does NOT appear in a subsequent
       `get_folder()` from the same client. Direct REST sees the sheet
       immediately. Confirmed live: REST returned the freshly-created
       sheet, SDK did not.

    Matches on exact title equality (Smartsheet folder listings are
    case-sensitive; titles are unique within a folder by convention but
    not enforced by the API, so a duplicate returns the first match).
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/folders/{folder_id}"
    context = f"finding sheet {name!r} in folder {folder_id}"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    body = response.json()
    for sheet in body.get("sheets", []):
        if sheet.get("name") == name:
            return int(sheet["id"])
    return None


@_breaker_guard
@_transient_retry
def count_workspace_sheets(workspace_id: int) -> int:
    """Count every sheet in a workspace, recursing nested folders.

    Direct REST (`GET /workspaces/{id}?loadAll=true`) rather than the SDK's
    deprecated `Workspaces.get_workspace()` — same DeprecationWarning + within-
    session stale-read reasons documented on `find_sheet_by_name_in_folder`.

    Used by `shared.sheet_capacity` to gate find-or-create against the per-workspace
    sheet cap (Tier-A A1 / forensic scaling eval B1 — sheet proliferation).
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/workspaces/{workspace_id}?loadAll=true"
    context = f"counting sheets in workspace {workspace_id}"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    return _count_sheets_in_node(response.json())


def _count_sheets_in_node(node: dict[str, Any]) -> int:
    """Recursively count `sheets[]` across a workspace/folder JSON node."""
    total = len(node.get("sheets") or [])
    for folder in node.get("folders") or []:
        total += _count_sheets_in_node(folder)
    return total


@_breaker_guard
@_transient_retry
def get_workspace_name(workspace_id: int) -> str:
    """Live workspace name for ``workspace_id`` (`GET /workspaces/{id}`).

    Purpose: VC-10's stale-constant guard — before auditing a workspace's F22
    approver shares, the cutover gate proves the object a
    ``sheet_ids.WORKSPACE_*`` constant points at is still the workspace the
    manifest MEANS (adversarial review 2026-07-23: an ID-drifted constant
    would otherwise silently audit the WRONG object — false PASS or masked
    FAIL). Direct REST for the same DeprecationWarning / within-session
    stale-read reasons as `count_workspace_sheets`.

    Failure modes: `SmartsheetNotFoundError` on a dead ID (the caller names it
    per-workspace); transients ride `_transient_retry`. Consumers:
    `scripts/verify_cutover._check_approver_shares` (VC-10).
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/workspaces/{workspace_id}"
    context = f"fetching workspace {workspace_id} name"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    return str(response.json().get("name", ""))


@_breaker_guard
@_transient_retry
def find_folder_by_name_in_folder(parent_folder_id: int, name: str) -> int | None:
    """Return the sub-folder ID with title `name` inside `parent_folder_id`, or None.

    Sibling of `find_sheet_by_name_in_folder` for the folders[] response field.
    Used by `safety_reports.week_folder.ensure_current_week_folder` to check
    "does this week's folder already exist?" before issuing a folder-create
    POST — same find-or-create idempotency pattern.

    Implemented via direct REST (`GET /folders/{id}`) rather than the SDK's
    `Folders.get_folder()` for the same two reasons documented on
    `find_sheet_by_name_in_folder`:

    1. `Folders.get_folder()` is deprecated upstream (emits
       DeprecationWarning).
    2. The deprecated method returns stale folder data within a single
       SDK client session — a folder created via `create_folder_in_folder`
       does NOT appear in a subsequent `get_folder()` from the same
       client. Direct REST sees the folder immediately. Confirmed live
       during the PR #51 integration-test run.

    Matches on exact title equality (Smartsheet folder listings are
    case-sensitive; titles are unique within a folder by convention but
    not enforced by the API, so a duplicate returns the first match —
    callers that need duplicate-aware behavior must inspect the listing
    themselves).
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/folders/{parent_folder_id}"
    context = f"finding folder {name!r} in folder {parent_folder_id}"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    body = response.json()
    for folder in body.get("folders", []):
        if folder.get("name") == name:
            return int(folder["id"])
    return None


@_breaker_guard
def create_sheet_in_folder(
    folder_id: int,
    name: str,
    columns: list[dict[str, Any]],
) -> int:
    """Create a new sheet inside `folder_id` and return its sheet ID.

    `columns` is a list of `{title, type, primary?, options?, ...}` dicts
    matching the Smartsheet Column model. The first entry whose
    `primary=True` becomes the primary column (Smartsheet requires
    exactly one; TEXT_NUMBER per its constraints).

    Idempotency is the caller's job — use `find_sheet_by_name_in_folder`
    first if the create needs to be re-run-safe (PR α migration pattern).
    """
    column_models = [smartsheet.models.Column(c) for c in columns]
    sheet_model = smartsheet.models.Sheet({"name": name, "columns": column_models})
    try:
        result = get_client().Folders.create_sheet_in_folder(folder_id, sheet_model)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e
    return int(result.result.id)


@_breaker_guard
def apply_column_styles(sheet_id: int, styles: list[dict[str, Any]]) -> None:
    """Apply column WIDTH / default-FORMAT to columns post-create (cosmetic only).

    Smartsheet IGNORES `width` / `format` on sheet creation (the `POST` path) —
    they're honored only on a column UPDATE (`PUT`). So week-sheet styling is a
    two-step: create with `create_sheet_in_folder`, then style with THIS. Each
    `styles` entry is `{"title": str, "width"?: int, "format"?: str}` where
    `format` is a Smartsheet format-descriptor string (comma-separated positions:
    2=bold, 8=textColor, 9=backgroundColor — colors index the account palette from
    `GET /serverinfo`). Titles resolve to column id + index via the live column
    list (`index` is required on the update model). No-op for an empty list.

    Cosmetic-only: no data change, no external send. Raises the typed
    `SmartsheetError` hierarchy on failure (e.g. a 403 read-only-token probe).
    """
    if not styles:
        return
    try:
        cols = get_client().Sheets.get_columns(sheet_id, include_all=True).data
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=True) from e
    by_title = {c.title: c for c in cols}
    for style in styles:
        title = style["title"]
        col = by_title.get(title)
        if col is None:
            raise KeyError(f"Column {title!r} not found in sheet {sheet_id} for styling")
        # `format` must be set via the model ATTRIBUTE — the Column dict constructor
        # silently drops a `"format"` key (verified live; width is fine either way).
        model = smartsheet.models.Column()
        model.index = col.index
        if "width" in style:
            model.width = style["width"]
        if "format" in style:
            model.format = style["format"]
        try:
            get_client().Sheets.update_column(sheet_id, col.id, model)
        except sdk_exc.SmartsheetException as e:
            raise _translate(e, idempotent=False) from e


@_breaker_guard
def delete_sheet(sheet_id: int) -> None:
    """Delete a sheet by ID via the SDK (`Sheets.delete_sheet`).

    Raises the typed hierarchy on failure (404 if already gone; 401/403 on a
    read-only token). The B2 write-capability probe uses this to clean up its
    throwaway sheet.
    """
    try:
        get_client().Sheets.delete_sheet(sheet_id)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e


@_breaker_guard
def move_sheet_to_folder(sheet_id: int, folder_id: int) -> None:
    """MOVE (never delete) a sheet into `folder_id` via the SDK (`Sheets.move_sheet`).

    The §51 archive-on-closure path uses this to RELOCATE a closed job's standing
    tracker sheets — the enumeration lives with the sole caller,
    `field_ops.fieldops_sync._archive_closed_job_trackers` (four trackers as of P7
    M3 Slice 2) — from the per-job PROGRESS folder into the Archive workspace's
    "Closed Projects" folder. This is a pure
    relocation: the sheet, its rows, and its cell history are all preserved (contrast
    `delete_sheet`, which destroys the sheet — this NEVER deletes anything).

    Idempotency is the CALLER's concern — there is no find-or-create here. A caller
    that no longer finds the sheet in the SOURCE folder (because a prior cycle already
    moved it) simply skips the move; this helper always issues the move. Raises the
    typed hierarchy on failure (404 if the sheet is gone; 401/403 on a read-only token).
    """
    dest = smartsheet.models.ContainerDestination({
        "destination_type": "folder",
        "destination_id": folder_id,
    })
    try:
        get_client().Sheets.move_sheet(sheet_id, dest)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e


# ── Folder-level relocation (ROADMAP Track 6) ───────────────────────────────────────────
#
# A job owns a FOLDER per workstream, not a loose set of sheets, so archiving a job means
# moving folders. One `move_folder` relocates everything inside — sub-folders, sheets,
# reports, dashboards — in a single call, which is the whole reason these exist rather than
# a loop over `move_sheet_to_folder`.
#
# THE MOVE ENDPOINT CANNOT RENAME. `POST /folders/{id}/move` accepts only `destinationType`
# + `destinationId`; `newName` belongs to `POST /folders/{id}/copy`. The SDK's
# `ContainerDestination` model is SHARED between copy and move and therefore exposes a
# `new_name` field — set it on a move and it serializes, is ignored, and you get a silently
# un-renamed folder. Rename with `rename_folder` as a second call.
#
# PERMISSIONS. The move endpoint requires the `ADMIN_WORKSPACES` scope. ITS authenticates
# with a PAT, which carries the acting user's own permissions, so the token identity must
# hold ADMIN on BOTH the source and destination workspaces. Probe with
# `get_workspace_access_level` rather than discovering it as a runtime 403.
#
# SHARING. Moving a folder ACROSS workspaces changes who can READ its contents — Smartsheet
# sharing is inherited from the workspace. That is a real, silent consequence (Op Stds §46:
# workspace membership carries approval semantics), not a footnote.


@_breaker_guard
def move_folder_to_folder(folder_id: int, dest_folder_id: int) -> None:
    """MOVE (never delete) a folder — and everything inside it — into `dest_folder_id`.

    Pure relocation: the folder keeps its ID, and the sheets it contains keep their IDs,
    permalinks, and cell history (contrast `copy_folder`, which mints new objects and
    explicitly does NOT carry cell history).

    Does NOT rename — see the section note above. Idempotency is the CALLER's concern:
    this always issues the move, so a caller must decide "already moved?" itself — and must
    NOT decide it by name, because the find-or-create paths in `week_sheet` / `hours_log` /
    `job_sheet` can re-create that name in the source at any time. Key the decision off the
    recorded folder ID via `get_folder_name`.

    Raises the typed hierarchy: `SmartsheetNotFoundError` on a dead source or destination,
    `SmartsheetPermissionError` (403) when the token lacks ADMIN on either end.
    """
    dest = smartsheet.models.ContainerDestination({
        "destination_type": "folder",
        "destination_id": dest_folder_id,
    })
    try:
        get_client().Folders.move_folder(folder_id, dest)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e


@_breaker_guard
def move_folder_to_workspace(folder_id: int, workspace_id: int) -> None:
    """MOVE a folder to a WORKSPACE root (the `destination_type='workspace'` variant).

    Not speculative: the per-job Safety and Progress folders sit directly under their
    workspace, not inside a parent folder, so restoring one out of the archive needs this
    variant. Two named wrappers rather than one parameterized function, matching the
    existing `..._in_folder` / `..._in_workspace` idiom in this module.

    Same contract as `move_folder_to_folder` — see it and the section note above.
    """
    dest = smartsheet.models.ContainerDestination({
        "destination_type": "workspace",
        "destination_id": workspace_id,
    })
    try:
        get_client().Folders.move_folder(folder_id, dest)
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e


@_breaker_guard
def rename_folder(folder_id: int, new_name: str) -> None:
    """Rename a folder in place (`PUT /folders/{id}` via `Folders.update_folder`).

    Exists because the move endpoint cannot rename (section note above), so reaching
    `Archive / <Job> / Safety` is a two-call sequence: move, then rename. That sequence is
    NOT atomic — a crash between them leaves a folder that moved but kept its old name.
    This call is naturally idempotent (PUT the same name twice ⇒ same state), which is what
    makes that intermediate state safely resumable rather than a wedge.

    Ordering matters and belongs to the caller: move-then-rename on the way IN (renaming
    first would hide the folder from the live find-or-create paths, which would then create
    a duplicate beside it), rename-then-move on the way back OUT (so the folder already
    carries its live name the moment it lands where those paths look).
    """
    try:
        get_client().Folders.update_folder(
            folder_id, smartsheet.models.Folder({"name": new_name})
        )
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e


@_breaker_guard
@_transient_retry
def get_folder_name(folder_id: int) -> str:
    """Live folder name for ``folder_id`` (`GET /folders/{id}`).

    The archive resume probe. After a crash mid-sequence a container may have moved but not
    been renamed, so "did this already finish?" cannot be answered by looking for a name in
    the SOURCE (the live creators re-make that name on the next filing) — it is answered by
    reading the RECORDED folder id's current name and comparing it to the expected label.
    Also disambiguates the pathological case of a job literally named "Safety".

    Direct REST for the same reasons as `get_workspace_name` (no SDK DeprecationWarning, no
    within-session stale reads). Idempotent GET, so retry-enrolled.
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/folders/{folder_id}"
    context = f"fetching folder {folder_id} name"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    return str(response.json().get("name", ""))


@_breaker_guard
@_transient_retry
def get_workspace_access_level(workspace_id: int) -> str:
    """The TOKEN IDENTITY's access level on ``workspace_id`` (e.g. ``ADMIN`` / ``OWNER``).

    The pre-flight for folder moves, which need `ADMIN_WORKSPACES`. Without it a move fails
    403 only AFTER an operator has pressed Archive, and only for whichever containers got
    that far — a half-archived job caused purely by a permissions gap. Probing up front
    turns that into one loud, actionable refusal before anything moves.

    Precedent: `verify_write_capability` probes rather than assumes. The difference is that
    one tests transport CAPABILITY while this tests authorization POSTURE, so the policy
    decision (what to do when it is insufficient) belongs to the caller, not here.

    Returns '' when the API omits the field rather than guessing a level — '' is not ADMIN,
    so a caller comparing against an allowed set fails CLOSED.
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/workspaces/{workspace_id}"
    context = f"fetching workspace {workspace_id} access level"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    return str(response.json().get("accessLevel", ""))


def delete_sheet_settling(
    sheet_id: int, *, attempts: int = 3, backoff_seconds: float = 1.0
) -> None:
    """Delete a sheet, retrying the create→delete eventual-consistency window.

    Smartsheet is eventually consistent: a delete issued IMMEDIATELY after a
    create can 404 (errorCode 1006) or return errorCode 5036 ("not yet
    propagated") because the new sheet has not replicated across read/write
    replicas yet — a settle window of several seconds. (Same flake class as the
    `docs/tech_debt.md` entry "Smartsheet integration tests flake on
    create→read/write eventual consistency"; surfaced here on the B2
    write-capability probe's immediate cleanup.) This retries `delete_sheet` on
    that transient not-found a few times with a short backoff.

    DISTINCT from `delete_sheet` BY DESIGN: a genuine missing-sheet delete
    elsewhere should fail FAST, so ONLY the probe-cleanup path
    (`verify_write_capability` / watchdog Check L) uses this. Re-raises the last
    not-found error if all attempts are exhausted; a non-not-found error
    (including `SmartsheetCircuitOpenError`) fails fast on the first attempt.
    """
    last_exc: SmartsheetError | None = None
    for attempt in range(attempts):
        try:
            delete_sheet(sheet_id)
            return
        except SmartsheetNotFoundError as exc:
            last_exc = exc  # 404 / errorCode 1006 — likely not-yet-propagated.
        except SmartsheetError as exc:
            # errorCode 5036 can surface with a non-404 status but is still an
            # eventual-consistency not-found; retry it. Anything else fails fast.
            if "5036" not in str(exc):
                raise
            last_exc = exc
        if attempt < attempts - 1:
            time.sleep(backoff_seconds)
    assert last_exc is not None  # loop ran >=1 time and never returned
    raise last_exc


def verify_write_capability(folder_id: int = sheet_ids.FOLDER_SYSTEM_CONFIG) -> int:
    """Probe that ITS_SMARTSHEET_TOKEN can WRITE, not just read (B2).

    Creates a throwaway one-column sheet in `folder_id` and returns its id. The
    CALLER must `delete_sheet` it — cleanup is kept separate so a *delete*
    failure is the caller's WARN, not a false "cannot write" verdict (the create
    already proved write capability). A read-only or mis-scoped token fails the
    CREATE with 401/403, re-raised here as `SmartsheetWriteCapabilityError` so a
    boot/watchdog caller can fail LOUD — instead of the token passing every read
    and only failing at the first real daemon write (a mid-cycle 401 that is hard
    to trace; the keychain-stub session lost ~2 h to exactly that signature).

    NOT `@_breaker_guard`-decorated: it composes the already-guarded
    `create_sheet_in_folder`, so a `SmartsheetCircuitOpenError` (Smartsheet
    OUTAGE, not a token problem) propagates unchanged for the caller to treat as
    inconclusive rather than as a write-capability verdict.

    Raises:
        SmartsheetWriteCapabilityError: create rejected 401/403 → cannot write.
        SmartsheetCircuitOpenError / other SmartsheetError: transient/outage —
            propagates unchanged (NOT a capability verdict).
    """
    probe_name = f"_its_write_probe_{datetime.now():%H%M%S%f}"  # <= 50 chars (1041)
    try:
        return create_sheet_in_folder(
            folder_id,
            probe_name,
            [{"title": "probe", "type": "TEXT_NUMBER", "primary": True}],
        )
    except (SmartsheetAuthError, SmartsheetPermissionError) as exc:
        raise SmartsheetWriteCapabilityError(
            f"ITS_SMARTSHEET_TOKEN cannot create a sheet in folder {folder_id} "
            f"(read-only or mis-scoped token?): {exc}"
        ) from exc


@_breaker_guard
def create_folder_in_folder(parent_folder_id: int, name: str) -> int:
    """Create a sub-folder inside `parent_folder_id` and return its folder ID.

    Implemented via direct REST (`POST /folders/{id}/folders`) for symmetry
    with `find_folder_by_name_in_folder` — both legs of the find-or-create
    idempotency pattern in `safety_reports.week_folder` share the REST
    transport so the same-session cache bug (PR #51) cannot bite a
    later refactor.

    Idempotency is the caller's job — use `find_folder_by_name_in_folder`
    first if the create needs to be re-run-safe.
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/folders/{parent_folder_id}/folders"
    context = f"creating folder {name!r} in folder {parent_folder_id}"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"name": name},
            timeout=30,
        )
    except requests.RequestException as e:
        # A timeout/connection drop on a POST is the SAME unknown-commit case the 5xx arm
        # below handles — the request was transmitted and only the response was lost. It
        # must NOT wear the retry-safe type (round-4 finding, 2026-07-21).
        raise SmartsheetError(f"{context}: {e!r}" + _NON_IDEMPOTENT_SUFFIX) from e
    _translate_smartsheet_error(response, context=context, idempotent=False)
    body = response.json()
    return int(body["result"]["id"])


@_breaker_guard
@_transient_retry
def find_folder_by_name_in_workspace(workspace_id: int, name: str) -> int | None:
    """Return the top-level folder ID named `name` in `workspace_id`, or None.

    Purpose
        The workspace-level sibling of `find_folder_by_name_in_folder`. Lets a
        migration find-or-create a folder directly under a workspace (e.g. the
        "Safety Portal" config folder under ITS — Operations) idempotently.

    Invariants
        - Direct REST (`GET /workspaces/{id}`) rather than the SDK's
          `Workspaces.get_workspace()`, for the same reason its folder-level
          twin avoids `Folders.get_folder()`: the SDK getter returns stale
          within-session data after a sibling create (PR #51). Direct REST sees
          a just-created folder immediately.
        - Exact, case-sensitive title match (Smartsheet does not enforce unique
          folder titles; the FIRST match wins — a caller needing duplicate-aware
          behaviour must inspect the listing itself). Read-only; never creates.

    Failure modes
        - Non-2xx surfaces as the typed `SmartsheetError` hierarchy via
          `_translate_smartsheet_error`; `SmartsheetCircuitOpenError` when the
          breaker is OPEN.

    Consumers
        `scripts/migrations/build_its_active_jobs_sheet.py` +
        `build_its_forms_catalog_sheet.py` (find-or-create the shared
        "Safety Portal" folder under WORKSPACE_OPERATIONS).
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/workspaces/{workspace_id}"
    context = f"finding folder {name!r} in workspace {workspace_id}"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    body = response.json()
    for folder in body.get("folders", []):
        if folder.get("name") == name:
            return int(folder["id"])
    return None


@_breaker_guard
def create_folder_in_workspace(workspace_id: int, name: str) -> int:
    """Create a top-level folder named `name` in `workspace_id`; return its ID.

    Purpose
        Workspace-level sibling of `create_folder_in_folder`. Stands up a config
        folder directly under a workspace (the "Safety Portal" folder under
        ITS — Operations).

    Invariants
        - Direct REST (`POST /workspaces/{id}/folders`) for transport symmetry
          with `find_folder_by_name_in_workspace` — keeps the find-or-create
          loop on one transport so a later refactor can't reintroduce the SDK
          same-session cache bug (PR #51).
        - Idempotency is the CALLER's job — call
          `find_folder_by_name_in_workspace` first if the create must be
          re-run-safe. Re-running blindly creates a duplicate-titled folder.

    Failure modes
        - Non-2xx surfaces as the typed `SmartsheetError` hierarchy;
          `SmartsheetCircuitOpenError` when the breaker is OPEN (a create is a
          write).

    Consumers
        Same as `find_folder_by_name_in_workspace`.
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/workspaces/{workspace_id}/folders"
    context = f"creating folder {name!r} in workspace {workspace_id}"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"name": name},
            timeout=30,
        )
    except requests.RequestException as e:
        # A timeout/connection drop on a POST is the SAME unknown-commit case the 5xx arm
        # below handles — the request was transmitted and only the response was lost. It
        # must NOT wear the retry-safe type (round-4 finding, 2026-07-21).
        raise SmartsheetError(f"{context}: {e!r}" + _NON_IDEMPOTENT_SUFFIX) from e
    _translate_smartsheet_error(response, context=context, idempotent=False)
    body = response.json()
    return int(body["result"]["id"])


@_breaker_guard
@_transient_retry
def list_workspace_share_emails(workspace_id: int) -> frozenset[str]:
    """Return the lowercased member emails the workspace is directly shared with.

    Purpose
        The F22 approval authority for the Safety Portal send leg: an approver is
        authorized iff they are a member of the ITS — Safety Portal workspace's
        share list. Sharing the workspace IS granting approval authority (Evergreen
        controls who can trigger rollup + send by who the workspace is shared with),
        replacing the former `safety_reports.authorized_approvers` ITS_Config
        allowlist.

    Invariants
        - Direct REST (`GET /workspaces/{id}/shares?includeAll=true`) for transport
          symmetry with the workspace folder helpers (one transport, no SDK
          same-session cache surprises — PR #51).
        - Only USER (individual) shares carry an `email`; GROUP shares have no email
          and are excluded (group-membership expansion is a documented follow-up).
          Emails are lowercased + stripped + deduped.
        - Read-only. An EMPTY result is a valid return the F22 gate treats as
          fail-closed (EMPTY_ALLOWLIST → block all sends), NEVER fail-open.

    Failure modes
        - Non-2xx → typed `SmartsheetError` hierarchy via
          `_translate_smartsheet_error`; `SmartsheetCircuitOpenError` when the
          breaker is OPEN.

    Consumers
        `safety_reports/weekly_send_poll._load_authorized_approvers` (the F22 gate).
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = (
        f"https://api.smartsheet.com/2.0/workspaces/{workspace_id}/shares"
        "?includeAll=true"
    )
    context = f"listing shares for workspace {workspace_id}"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    body = response.json()
    return frozenset(
        str(share["email"]).strip().lower()
        for share in body.get("data", [])
        if isinstance(share, dict) and share.get("email")
    )


@_breaker_guard
def list_workspace_shares(workspace_id: int) -> tuple[dict[str, Any], ...]:
    """Return the workspace's RAW share records — USER and GROUP shares alike.

    Purpose
        The cutover gate's F22 share audit (``scripts/verify_cutover.py`` VC-10)
        needs more than ``list_workspace_share_emails`` exposes: a GROUP share
        carries NO ``email``, so the email view cannot distinguish "no group
        shares" from "a group share that silently does not count toward F22".
        This helper returns the raw share dicts (``type``, ``email``, ``name``,
        ``accessLevel``, …) so a caller can flag group shares and mirror-account
        residue by inspection.

    Invariants
        - Same transport as ``list_workspace_share_emails`` (direct REST
          ``GET /workspaces/{id}/shares?includeAll=true``). The small transport
          duplication is deliberate, not an oversight: that helper's body and
          decorator stack are locked by the retry-enrollment AST tests
          (``tests/test_smartsheet_retry.py``), so neither delegating it here
          nor refactoring it onto this helper is in scope (§14 preservation).
        - Records are returned verbatim (no lowercasing/dedup — the caller
          normalizes for its own comparison). Read-only.
        - NOT ``_transient_retry``-enrolled: the approved-enrollment list is
          set-equality-locked in ``tests/test_smartsheet_retry.py``; enrolling
          this read is a deliberate follow-up edit there, never a side effect
          of the helper landing.

    Failure modes
        Non-2xx → typed ``SmartsheetError`` hierarchy via
        ``_translate_smartsheet_error``; ``SmartsheetCircuitOpenError`` when the
        breaker is OPEN.

    Consumers
        ``scripts/verify_cutover.py`` ``_check_approver_shares`` (VC-10).
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = (
        f"https://api.smartsheet.com/2.0/workspaces/{workspace_id}/shares"
        "?includeAll=true"
    )
    context = f"listing raw shares for workspace {workspace_id}"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as e:
        raise SmartsheetTransientError(f"{context}: {e!r}") from e
    _translate_smartsheet_error(response, context=context, idempotent=True)
    body = response.json()
    return tuple(share for share in body.get("data", []) if isinstance(share, dict))


@_breaker_guard
def attach_pdf_to_row(
    sheet_id: int,
    row_id: int,
    filename: str,
    pdf_bytes: bytes,
    *,
    replace: bool = True,
    content_type: str = "application/pdf",
) -> int:
    """Attach `pdf_bytes` as `filename` to a row; return the attachment ID.

    Purpose
        Put the rendered document INLINE on a Smartsheet row — the per-submission
        PDF on its week-sheet Submission row, and the compiled weekly packet on the
        week-sheet Rollup row + the WSR_human_review row — so a reviewer sees it
        without a Box round-trip.

        `content_type` defaults to ``application/pdf`` (the historical hardcode —
        every pre-existing caller attaches a rendered PDF and is unchanged). Callers
        attaching non-PDF bytes (subcontract .docx/.xlsx packages, PO document
        attachments) pass the correct MIME so the Smartsheet attachment is not
        mislabeled (closes the caveat documented at
        ``subcontracts.subcontract_poll._attach_files_best_effort``).

    Invariants
        - Box stays the System of Record: the row's Box-link cell is UNCHANGED and
          the weekly compile still reads per-submission PDFs from Box, not from
          these attachments. This inline copy is purely supplementary.
        - A Smartsheet attachment write is NOT an external send (Invariant 1 safe)
          and has no AI step — the generation/send-gate separation is untouched.
        - `replace=True` (default) deletes any prior attachment on the row with the
          SAME `filename` first, so a re-pull / recompile leaves exactly one current
          copy instead of accumulating duplicates (the per-submission + packet
          filenames are deterministic per row).

    Failure modes
        SDK / non-2xx → the typed `SmartsheetError` hierarchy via `_translate`;
        `SmartsheetCircuitOpenError` when the breaker is OPEN. Callers treat this
        BEST-EFFORT (Box is the SoR) — an attach failure must NOT fail the
        filing/compile that produced the row.

    Consumers
        `safety_reports/intake._attach_pdf_best_effort` (the per-submission PDF on
        the Submission row); `safety_reports/weekly_generate._attach_pdf_best_effort`
        (the compiled packet on the Rollup row + the WSR_human_review row).
    """
    client = get_client()
    if replace:
        try:
            existing = client.Attachments.list_row_attachments(sheet_id, row_id)
            for att in existing.data:
                if att.name == filename:
                    client.Attachments.delete_attachment(sheet_id, att.id)
        except sdk_exc.SmartsheetException as e:
            raise _translate(e, idempotent=False) from e
    try:
        result = client.Attachments.attach_file_to_row(
            sheet_id, row_id, (filename, io.BytesIO(pdf_bytes), content_type)
        )
    except sdk_exc.SmartsheetException as e:
        raise _translate(e, idempotent=False) from e
    return int(result.result.id)


@_breaker_guard
def create_sheet_in_folder_from_template(
    folder_id: int,
    name: str,
    template_sheet_id: int,
    *,
    include: list[str] | None = None,
) -> int:
    """Clone `template_sheet_id` into `folder_id` with name `name`.

    `include` controls which parts of the template are copied. Empty list
    (or None — the default) clones structure only: columns, formatting,
    column descriptions. Pass `["data"]` to also copy row contents, or
    `["data", "attachments", "discussions"]` for a fuller clone. Values
    match Smartsheet's `POST /sheets/{id}/copy?include=...` query param.

    Used by `safety_reports.week_folder.ensure_current_week_folder` to
    clone the Bradley 1 / Week of 2026-03-09 templates forward into
    each new week. Empty include is the right default — we want the
    template's schema (column titles, picklists, descriptions) but not
    the template week's residual rows.

    Implemented via direct REST (`POST /sheets/{id}/copy`) for symmetry
    with `find_folder_by_name_in_folder` and `create_folder_in_folder`
    — keeps the create-then-find loop in `ensure_current_week_folder`
    on a single transport.

    Body shape requirement discovered live during this PR's integration
    test: Copy Sheet expects `destinationType` + `destinationId` as
    flat top-level keys, NOT a nested `destination: {type, id}` object.
    The nested form returns HTTP 400 errorCode 1008 ("Unknown attribute
    'destination'"). Smartsheet's other endpoints (Move Sheet, etc.) use
    the same flat shape — pattern is consistent once you know it.
    """
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    url = f"https://api.smartsheet.com/2.0/sheets/{template_sheet_id}/copy"
    include_csv = ",".join(include) if include else ""
    if include_csv:
        url += f"?include={include_csv}"
    context = (
        f"copying sheet {template_sheet_id} into folder {folder_id} as {name!r}"
    )
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "destinationType": "folder",
                "destinationId": folder_id,
                "newName": name,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        # A timeout/connection drop on a POST is the SAME unknown-commit case the 5xx arm
        # below handles — the request was transmitted and only the response was lost. It
        # must NOT wear the retry-safe type (round-4 finding, 2026-07-21).
        raise SmartsheetError(f"{context}: {e!r}" + _NON_IDEMPOTENT_SUFFIX) from e
    _translate_smartsheet_error(response, context=context, idempotent=False)
    body = response.json()
    return int(body["result"]["id"])


# ---- SDK 404 noise suppression ------------------------------------------


class _Suppress404JSON(logging.Filter):
    """Drop the SDK's ERROR-level emission of the raw 404 response body.

    Inspects `record.args` (unformatted; first positional is the status
    code passed to the SDK's `_log_request` ERROR call) so the filter
    survives format-string changes in future SDK versions. Non-tuple or
    empty args, or any non-ERROR record, passes through untouched. The
    set is parameterized so additional status codes can be silenced later
    without re-architecting the filter.
    """

    _QUIET_STATUS_CODES = frozenset({404})

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.ERROR:
            return True
        args = record.args
        if not isinstance(args, tuple) or not args:
            return True
        return args[0] not in self._QUIET_STATUS_CODES


logging.getLogger(SDK_LOGGER_NAME).addFilter(_Suppress404JSON())
