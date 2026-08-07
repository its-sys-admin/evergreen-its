"""Healthchecks.io heartbeat client — external dead-man's-switch beacon.

Purpose
-------
Single outbound GET to a configured Healthchecks.io ping URL, fired once
per watchdog run (hourly since 2026-08-07) (``scripts/watchdog.py`` ``main()``). The external
monitor expects this ping within its configured period+grace; a missed
ping means "the watchdog (and almost certainly the whole MacBook) stopped
running" and Healthchecks.io alerts the operator out-of-band. This is the
only external detector for total-host failure (crash, disk-full, launchd
unload, user logout) — every in-tenant signal (Smartsheet rows, etc.) goes
silent in that scenario with nothing to raise the alarm.

Invariants
----------
- This is an OUTBOUND OBSERVABILITY BEACON, not a customer-facing send.
  It targets a fixed monitoring endpoint, carries no customer data, and is
  analogous to Sentry event capture — NOT subject to the External Send
  Gate (Foundation Mission v8 Invariant 1) and NOT in SEND_SCRIPTS /
  GATED_SCRIPTS. ``tests/test_capability_gating.py`` is intentionally
  unchanged by this module.
- ``ping()`` is fail-soft: it NEVER raises. A dead monitoring endpoint or
  network blip must not break the watchdog's real checks (Op Stds v13
  §3.1 — fail-open posture for observability writes).
- The ping URL is read from ITS_Config row ``system.heartbeat_url``
  (Workstream=``global``) by the CALLER, NOT from Keychain — it is
  low-sensitivity config (a write-only beacon URL), read the same way as
  kill-switch state and alerting windows.

Failure modes
-------------
- Network / timeout / non-2xx HTTP error → caught (every ``requests``
  failure derives from ``requests.RequestException``, including the
  ``HTTPError`` raised by ``raise_for_status()``), logged WARN under
  error_log category ``heartbeat_ping_failed``, returns ``None``. The next
  daily run retries. A non-2xx response is routed through this same WARN
  path on purpose: a mistyped URL (404) or a Healthchecks.io outage (5xx)
  is a real "the beacon isn't landing" signal worth a log line, not a
  silent success.
- Blank / placeholder / missing URL is the CALLER's guard (watchdog), not
  this module's — see ``scripts/watchdog.py`` ``main()``.

Consumers
---------
- ``scripts/watchdog.py`` ``main()`` — the sole caller, fires one ping per
  run after all checks complete.

Reference
---------
Audit F16 (``its-blueprint/audits/2026-05-25_forensic-audit.md`` §3).
Wrapped as a shared client (not an inline watchdog ``requests`` import) to
pre-comply with the F02 network-library allowlist — only ``shared/*_client``
modules may import ``requests`` once that lands. Mirrors the structure of
``shared/resend_client.py`` (a simpler sibling: no auth, no retry, no JSON
body — just a fire-and-forget GET with a timeout).
"""
from __future__ import annotations

import requests  # type: ignore[import-untyped]

from .error_log import Severity, log

_SCRIPT = "shared.heartbeat_client"

# Default GET timeout. A heartbeat is fire-and-forget; we never want it to
# hang the watchdog waiting on a slow monitor. 10s is generous for a single
# GET to Healthchecks.io and well under the watchdog's launchd cadence.
_DEFAULT_TIMEOUT = 10.0


class HeartbeatError(Exception):
    """Raised by heartbeat_client on a ping failure when a caller opts into
    propagation. ``ping()`` itself is fail-soft and does NOT raise — see its
    docstring. Defined for symmetry with the other ``shared/*_client`` error
    hierarchies and for future callers that need the failure surfaced."""


def ping(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
    """Notify the external monitor that the host is alive — fail-soft.

    Issues a single ``GET`` to ``url`` (a Healthchecks.io ping endpoint) and
    treats any failure — connection refused, timeout, or non-2xx response —
    as a logged WARN, never an exception. The heartbeat is an observability
    beacon: a dead *endpoint* must never break the watchdog's real work
    (Op Stds v13 §3.1). The fail-open rationale lives here, in one place,
    because the only caller would otherwise just swallow the error anyway.
    """
    try:
        response = requests.get(url, timeout=timeout)
        # raise_for_status routes a 4xx/5xx through the same WARN path as a
        # connection failure — a mistyped URL or a monitor outage is a real
        # "beacon not landing" signal, not a silent success. HTTPError is a
        # RequestException subclass, so the single except below catches it.
        response.raise_for_status()
    except requests.RequestException as exc:
        log(
            Severity.WARN,
            _SCRIPT,
            f"heartbeat ping failed: {exc!r}",
            error_code="heartbeat_ping_failed",
        )
