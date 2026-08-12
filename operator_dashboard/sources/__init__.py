"""The ordered panel registry.

Each panel is one registered `DataSource`. Local-files-first (cheap,
always-on) come before the TTL-cached Smartsheet read panels. Adding a panel
is one entry here — no route changes.
"""
from __future__ import annotations

from operator_dashboard.sources.base import DataSource
from operator_dashboard.sources.box_roots import BoxRootsSource
from operator_dashboard.sources.daemons import DaemonStatusSource
from operator_dashboard.sources.logs import LogTailSource
from operator_dashboard.sources.runtime_state import (
    CircuitBreakerSource,
    HeartbeatsSource,
    LocksSource,
)
from operator_dashboard.sources.smartsheet_panels import (
    AuditTrailSource,
    ErrorsRecentSource,
    OpenCriticalsSource,
    ReviewQueueDepthSource,
    SendQueueSource,
)
from operator_dashboard.sources.watchdog_checks import (
    WatchdogChecksSource,
    WatchdogSweepSource,
)

PANELS: list[DataSource] = [
    DaemonStatusSource(),
    WatchdogSweepSource(),
    WatchdogChecksSource(),
    CircuitBreakerSource(),
    HeartbeatsSource(),
    LocksSource(),
    LogTailSource(),
    OpenCriticalsSource(),
    ErrorsRecentSource(),
    ReviewQueueDepthSource(),
    SendQueueSource(),
    BoxRootsSource(),
    AuditTrailSource(),
]

PANELS_BY_ID: dict[str, DataSource] = {p.panel_id: p for p in PANELS}
