# ITS launchd plists

How ITS scheduled scripts get scheduled on macOS. Per **Operational Standards v8 §2**: launchd is the scheduling layer. `StartCalendarInterval` catches up on wake, so a Friday 14:00 compile still fires when the laptop wakes at 16:00. (The watchdog itself moved OFF calendar to `StartInterval 3600` on 2026-08-07 — see `org.solutionsmith.its.watchdog.plist`.)

## What's here

| File | Purpose |
| --- | --- |
| `template.plist` | Generic starter — copy, rename, fill in placeholders |
| `org.solutionsmith.its.watchdog.plist` | Concrete plist for `scripts/watchdog.py`, daily 7:00 AM |
| `org.solutionsmith.its.estimate-poll.plist` | Vendor-estimate pull daemon (`po_materials/estimate_poll.py`), `StartInterval` 120s (via `__POLL_INTERVAL_SECONDS__` / `po_materials.estimate_poll.poll_interval_seconds`); ships dark — load + gate-flip together |
| `org.solutionsmith.its.rfq-poll.plist` | Outbound-RFQ generation daemon (`po_materials/rfq_poll.py`), `StartInterval` 120s (via `__POLL_INTERVAL_SECONDS__` / `po_materials.rfq_poll.poll_interval_seconds`); ships dark — load + gate-flip together |
| `org.solutionsmith.its.rfq-send.plist` | RFQ SEND daemon (`po_materials/rfq_send_poll.py`), `StartInterval` 900s (via `__POLL_INTERVAL_SECONDS__` / `po_materials.rfq_send.poll_interval_seconds`); ships dark — go-live is a FIXED External-Send-Gate flip + load (Seth) |
| `install.sh` | Load / unload / status / dry-run helper |
| `README.md` | This file |

## How install works

The plists in this directory keep `__ITS_HOME__` as a literal placeholder (launchd does not expand `~` or `$HOME` in paths). `install.sh load` substitutes `__ITS_HOME__` → `$HOME/its` when copying into `~/Library/LaunchAgents/`. The repo stays portable; the installed copy has real paths.

## Daily commands

```bash
# Load the watchdog
./install.sh load org.solutionsmith.its.watchdog

# See what ITS jobs are loaded
./install.sh status

# Detailed state for one job
./install.sh status org.solutionsmith.its.watchdog

# Preview the resolved plist before loading
./install.sh dry-run org.solutionsmith.its.watchdog

# Unload
./install.sh unload org.solutionsmith.its.watchdog
```

`install.sh load` runs `plutil -lint` before bootstrapping, so XML or type errors get caught before the plist ever reaches launchd.

## Conventions

**Label prefix.** Build phase: `org.solutionsmith.its.<name>`. Post-handover (Phase 1.5), all labels become `com.evergreenrenewables.its.<name>` and plists get regenerated. The `<name>` part stays the same.

**Agent vs Daemon.** Always LaunchAgent (per-user, `~/Library/LaunchAgents/`), never LaunchDaemon. ITS scripts need user-context Keychain access; daemons run before login and cannot reach the login keychain.

**Schedule.** Three patterns, pick one per plist:
- `StartCalendarInterval` daily (watchdog at 7:00 AM)
- `StartCalendarInterval` weekly (weekly summary; `Weekday: 0=Sun..6=Sat`)
- `StartInterval` recurring seconds (email triage reclassify, hourly)

**`RunAtLoad` is `false`** — scheduled scripts should not fire on every `launchctl load`. Manually invoke for testing instead (see below).

**Logs** land at `~/its/logs/launchd/<basename>.out.log` and `.err.log`. Separate from the application error log (`shared/error_log.py` writes to ITS_Errors and `~/its/logs/`). These launchd logs catch crashes that happen before the `@its_error_log` decorator runs.

## Testing a script without waiting for the schedule

```bash
launchctl kickstart -k gui/$(id -u)/org.solutionsmith.its.watchdog
```

`-k` kills any running instance first, then starts a fresh one. Output goes to the log files above.

## Adding a new scheduled script

1. `cp template.plist org.solutionsmith.its.<name>.plist`
2. Replace placeholders: `__LABEL__`, `__SCRIPT_PATH__`, `__SCRIPT_BASENAME__`
3. Keep `__ITS_HOME__` as-is (install.sh handles it)
4. Pick one schedule block, delete the others
5. `./install.sh dry-run <name>` to inspect
6. `./install.sh load <name>` to enable
7. Commit the plist to the repo

## Caveats

- **Login keychain must be unlocked.** Cleared by logging in. After a reboot, scripts run only once the user has logged in at least once. Acceptable per the best-effort reliability framing.
- **Sleep behavior.** Closing the lid puts the Mac to sleep; scheduled jobs do not fire until wake. The catch-up-on-wake semantics mean a missed 7:00 AM watchdog runs at the next wake, not the next day.
- **Power Nap.** If enabled (System Settings → Battery), `StartCalendarInterval` can fire during sleep on AC power. Off by default on most Macs; not required.
