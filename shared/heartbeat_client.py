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
- Blank / placeholder / missing URL is decided by ``is_configured()`` here
  and ACTED ON by the caller — ``scripts/watchdog.py`` ``main()`` skips the
  ping, watchdog Check Z / VC-09 go red. The predicate lives here so those
  three cannot disagree about what "configured" means.

Consumers
---------
- ``scripts/watchdog.py`` ``main()`` — the sole ``ping()`` caller, fires one
  ping per run after all checks complete, gated on ``is_configured()``.
- ``scripts/watchdog.py`` ``_check_heartbeat_armed`` (Check Z) — pages when
  the beacon is unconfigured.
- ``scripts/verify_cutover.py`` ``_check_heartbeat_url`` (VC-09) — same
  verdict, via the same predicate.
- ``scripts/seed_its_config.py`` — seeds ``PLACEHOLDER_URL`` verbatim.

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

from typing import TypeGuard

import requests  # type: ignore[import-untyped]

from .error_log import Severity, log

_SCRIPT = "shared.heartbeat_client"

# Default GET timeout. A heartbeat is fire-and-forget; we never want it to
# hang the watchdog waiting on a slow monitor. 10s is generous for a single
# GET to Healthchecks.io and well under the watchdog's launchd cadence.
_DEFAULT_TIMEOUT = 10.0

# The seed `Value` for ITS_Config `system.heartbeat_url` on an unprovisioned
# tenant. FROZEN TOKEN — `scripts/seed_its_config.py` writes exactly this
# string and every consumer must compare against exactly this string.
#
# It lived as a bare literal in `scripts/watchdog.py` main() until 2026-08-17.
# That was the narrated-not-enforced (§52) shape of the correspondence
# documented in docs/references/integration_reference.md: "must stay
# char-for-char equal to the watchdog guard token" was a sentence, not a
# mechanism. It is a shared constant now, so the seed, the ping guard and the
# VC-09 check cannot drift apart.
#
# Historical note: the token says "uptimerobot" but the provisioned vendor is
# Healthchecks.io (the free UptimeRobot tier gates heartbeat monitoring behind
# Pro and restricts commercial use). The token is deliberately NOT renamed —
# renaming it would require a lockstep edit of the seed and every guard for no
# behavioural gain. See docs/session_logs/2026-05-28_f16-heartbeat-ping.md.
PLACEHOLDER_URL = "PLACEHOLDER_uptimerobot_heartbeat_url"


def is_configured(url: str | None) -> TypeGuard[str]:
    """True when ``url`` is a real, pingable beacon rather than the unset seed.

    Declared as a ``TypeGuard[str]`` rather than a plain ``bool`` so the caller's
    ``str | None`` narrows to ``str`` inside the guarded branch — otherwise every
    call site needs a redundant second truthiness test to satisfy mypy, and a
    redundant test is a place where the two conditions can drift apart.

    THE correspondence predicate: `scripts/watchdog.py` main() skips the ping
    when this is False, and `scripts/verify_cutover.py` VC-09 / watchdog Check Z
    go red when this is False. One predicate, three consumers — so "the check is
    green" and "the ping actually fires" cannot disagree, which is the only
    property that makes the dead-man's switch trustworthy.

    False for: None, blank/whitespace, the frozen ``PLACEHOLDER_URL`` seed, and
    anything that is not an ``https://`` URL. The https requirement is not
    cosmetic — a ping URL carries the host-liveness claim and must not be
    interceptable in cleartext.
    """
    text = (url or "").strip()
    if not text or text == PLACEHOLDER_URL:
        return False
    return text.startswith("https://")


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
