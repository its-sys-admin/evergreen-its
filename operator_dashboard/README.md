# Operator Dashboard — WS2 (read-only observability + PIN-gated ACT)

A loginless-by-default, **localhost-only** FastAPI app (Tailscale-exposed)
that gives the operator one screen of ITS runtime health, a live **system
map**, the docs corpus + troubleshooting tree, and a PIN-gated ACT surface.
It **observes** the live daemon tree at `~/its`; every read is fail-soft.

## Run

```bash
# from a worktree/venv that has the project installed (fastapi/uvicorn/jinja2):
python -m operator_dashboard          # serves http://127.0.0.1:8484
```

It also runs as a launchd KeepAlive service (`org.solutionsmith.its.dashboard`,
installed via `scripts/launchd/install.sh`). Expose to your other devices over
Tailscale only — never a public interface:

```bash
tailscale serve 8484
```

## Pages

| Page | What it is |
|------|------------|
| `/` | The status grid: pulse strip + the fail-soft panels (htmx, 15s refresh) |
| `/system` | The live system map — trust-gradient lanes, the two walls (untrusted ingress · External Send Gate), per-node live state, deep-linked from error rows / panels / the troubleshooting tree (`?focus=<node>`, `?wf=<workflow>`) |
| `/view/{panel}` | A panel's full drill-down (more rows; optional `?col=&eq=` display filter) |
| `/config` | The ACT surface: config editor + operational verbs (section rail + live filter, `?f=` prefill) |
| `/troubleshoot` | The troubleshooting tree (htmx drill-down; `?wf=&step=&fm=` deep links) |
| `/docs` + `/doc/{path}` | The docs corpus + allowlisted markdown viewer |

Panel sources: local-files-first (launchctl, watchdog markers, breaker state,
heartbeats, lock probe, log tail — always on, no credentials) plus TTL-cached
Smartsheet reads (ITS_Errors ×2, review queue, send queue, ACT audit). Every
panel is **fail-soft**: a missing file, dead read, or failed import degrades
that one card to "unavailable" — the page never crashes.

The system-map registry lives in `operator_dashboard/system_map.py`;
`tests/test_system_map.py` fails the build if a new daemon / plist / tracked
marker lands without a node (registry reconciliation, HOUSE_REFLEXES §1).

## The ACT surface

`/config` + the `POST /act/*` routes — the **only** mutating routes (the exact
set is locked by `tests/test_operator_dashboard.py`): the Class-A config
editor, Class-B elevated edits, interval edits, daemon control, the
dashboard's own restart verb (DASH-12 — the one sanctioned self-restart;
restart-only, never a deploy), circuit-breaker clear, the two error-log verbs,
the review-queue resolve verb (DASH-13), Class-C secret rotation, and
change-PIN. Every verb: PIN or elevated-confirm ceremony (fail-closed,
throttled), Origin-allowlisted, validated-then-written, audited to
`ITS_Errors`. Writes touch only internal SoR surfaces (`ITS_Config`,
`ITS_Errors` stamps, `ITS_Review_Queue` stamps, launchctl) — the dashboard
**never sends externally and never deploys** (the External Send Gate stays
with the two-process daemons; §50 deploys stay with the actuators). See the
runbook: `docs/runbooks/operator_dashboard_config_editor.md`.

## Safety posture

- **Adversarial input.** Smartsheet cells and raw log lines are untrusted:
  every displayed value is redacted (`shared.redact`) and HTML-escaped (Jinja
  `autoescape` forced on), so a `<script>`-shaped or secret-shaped value
  renders inert.
- **Fail-closed auth.** A missing/locked Keychain PIN denies; 5 failures →
  60s lockout + a CRITICAL page.
- **Capability-gated.** No `anthropic` / `graph_client.send_mail` / `resend`
  import anywhere in the package; the F02 network/subprocess allowlist in
  `tests/test_capability_gating.py` documents every sanctioned capability.

## If it misbehaves (Tier-2)

A single "unavailable" panel is expected when its underlying file/daemon is
absent. If the whole app is down: `launchctl kickstart -k
gui/$(id -u)/org.solutionsmith.its.dashboard` (or use the in-app restart verb
while it still serves). Symptoms + repairs:
`docs/runbooks/operator_dashboard_config_editor.md` §Symptoms.
