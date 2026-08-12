---
type: operations
date: 2026-07-13
status: active
related_prs: []
workstream: operator_dashboard
tags: [enablement, a8, operator-dashboard, config-editor, secret-rotation, pin, external-send-gate, tier-2]
---

# Enablement — The Operator Dashboard · Op Stds §6/A8

**Audience:** the **operator** (Seth or the trained Successor-Operator) who watches ITS's health and
occasionally changes a setting. No code knowledge assumed. This is the plain-language companion to the
successor-operator runbooks [`../runbooks/operator_dashboard_config_editor.md`](../runbooks/operator_dashboard_config_editor.md)
(the Class-A config editor) and [`../runbooks/operator_dashboard_sensitive_tier.md`](../runbooks/operator_dashboard_sensitive_tier.md)
(the sensitive tier — weighted edits + secret rotation).

## What it is

The Operator Dashboard is a single web page that shows you, at a glance, whether ITS is healthy — which
background workers are alive, what's erroring, what's waiting for review — and lets you make a small set
of approved changes to how ITS runs. It is a **convenience view**: it observes the live ITS on the office
Mac and changes nothing on its own.

It runs **only on the office Mac, on localhost** (`127.0.0.1:8484`) — it is not on the public internet.
To reach it from another of your own devices, you expose it over **Tailscale** (your private network),
never a public interface.

## How to start it

The dashboard runs as an **always-on background service** — a launchd job
(`org.solutionsmith.its.dashboard`) that keeps a small web server alive on the office Mac and restarts it
if it ever exits. Once installed it is simply there; you don't start it by hand each time. (Its own health
is watched by launchd's keep-alive plus a `/healthz` endpoint — not the marker-staleness the other workers
use.)

```bash
install.sh load org.solutionsmith.its.dashboard   # install + start the service (one-time)
```

To reach it from your phone or laptop over **Tailscale** (your private network — never a public interface):

```bash
tailscale serve --bg 8484
```

You can also start it **by hand** for a one-off look, without the service:

```bash
python -m operator_dashboard        # serves http://127.0.0.1:8484
```

## What you see — the health panels (read-only)

The main page is a set of cards, each reading one live source. Every card is **fail-soft**: if its
source isn't available, that one card says "unavailable" and the rest of the page still works. Nothing
here changes anything.

| Panel | What it tells you |
|---|---|
| **launchd daemons** | Which background workers are loaded and running. |
| **Watchdog sweep** | The last nightly sweep's per-check results — each check's letter, verdict, and one-line summary, so "did last night's sweep run, and what did it find" is one glance. A sweep older than a day warns even when every check passed. |
| **Watchdog markers** | The last-run timestamps the watchdog uses to spot a worker that's gone quiet. |
| **Circuit breaker** | Whether ITS has tripped its Smartsheet safety breaker (and is pausing to recover). |
| **Daemon liveness** | Each worker's heartbeat — proof it's actually cycling, not just loaded. |
| **State locks** | Which workers hold a working lock right now (a passive, non-disturbing check). |
| **Recent log tail** | The newest lines of today's log (secrets/PII redacted before display). |
| **ITS_Errors — recent** | The latest errors ITS recorded. |
| **ITS_Review_Queue — depth** | How many items are waiting for human review. |
| **Send queue** | Customer-send review rows waiting for approval or in flight (read-only — approving a send still happens on the review sheets, never here). |
| **Box roots** | The five top-level Box folders every filing path lands in (the four workstream trees + the archive). Each row proves the configured folder id still resolves under its expected name: red = missing/dead id (filings hold), amber = renamed or Box unreachable. |

Everything shown is treated as untrusted and is redacted and escaped before it reaches the screen, so a
malicious-looking value in a cell or log line renders as harmless text.

Above the panels, a one-glance **pulse strip** summarizes the six load-bearing surfaces — workers
loaded, watchdog freshness, the breaker, open CRITICALs, review-queue depth, and pending sends —
each chip linking to its full view.

## The system map — `/system`

The **System map** page draws the whole machine as one clickable schematic. Reading left to right is
the trust gradient: field input, the send-free cloud queue, the wall where every submission's
signature is re-verified, the Mac's generation workers, the record sheets where a **human approves**
each outgoing packet, the **External Send Gate** (drawn as a literal gold wall — nothing crosses it
without that approval), the send workers, and finally the outside world. Each node shows its live
state: a red count means open CRITICALs, `DARK` means that capability's switch is off, and the dot
shows whether its worker is running. Click any node for a plain-language explanation, its current
state, and direct links to its runbook and its troubleshooting entries. Every Smartsheet node
additionally carries an operator brief — what the sheet is, who writes it, and what you do with it
day-to-day — plus links to its deeper documentation and an "open in Smartsheet" link straight to
the live sheet. Error rows and worker names
elsewhere in the dashboard link straight to their node on the map — so "what is this thing and where
does it sit?" is always one click.

## The ACT surface — making a change (PIN-gated)

Below the read-only panels is the **config editor** — the only part of the dashboard that changes
anything. It edits settings in the **`ITS_Config`** sheet; a change takes effect on the affected
worker's **next cycle**. The page is organized for daily use: the everyday on/off switches come
first and the heavyweight sections last, each section opens with a one-line explanation of what it
means, live on/off values render as green/grey pills, and the send-gate section carries a gold
accent (the global brake a red one) so the weight of what you're touching is visible before you
touch it. There are three kinds of action, in increasing weight:

### Class A — everyday settings (PIN only)

Pause/resume gates, tuning knobs (thresholds, windows), and behavior settings. You pick the setting,
type the new value, and enter your **operator PIN**. The value is checked against a rule (a bad or
out-of-range value is refused with **nothing written**).

### Class B — weighted settings (the elevated-confirm ceremony)

Higher-stakes settings — the sent-from and read mailboxes, trust allowlists, the Worker endpoint URLs,
and the **global brake** (`system.state` = ACTIVE / PAUSED / MAINTENANCE) — require the **elevated-confirm
ceremony**: you **re-enter your PIN** *and* **type the exact name of the setting** you're changing. Both
must match, or nothing happens. This is a deliberate anti-fat-finger step for changes that affect trust
or identity.

> **Turning a send worker ON for the first time** (a "dark → live" activation) is **not something the
> dashboard applies for you.** A `false → true` edit on a send-poller gate is **routed to the escalate
> path — surfaced to Seth, never applied here** — because a first activation carries go-live preconditions
> and, for the **vendor send gate**, the permanent **External Send Gate**, a fixed high-capability action
> that belongs to Seth. **Pausing** any worker (turning a gate off) is always a plain operator action.
> (One privileged non-send gate — the code-deploy actuator — instead self-applies after the elevated
> ceremony *plus* an explicit "go-live preconditions met" attestation; the send gates never do.)

Beyond editing settings, the dashboard gives you three guarded **operational controls** — each behind the
same elevated-confirm ceremony (re-PIN + typed confirmation):

- **Restart / start / stop a worker** — kickstart a wedged daemon, or start/stop one (e.g. after a fix).
  Only the known ITS daemons are allowlisted, so it can never touch a non-ITS process.
- **Restart the dashboard itself** — the one sanctioned self-restart, so the console picks up newly
  landed code without a trip to the terminal. Restart only — it never pulls or deploys anything.
- **Change a worker's poll interval** — set how often an interval worker runs; the dashboard updates that
  worker's `ITS_Config` interval row **and reloads the worker** so the new cadence takes effect now.
- **Clear the circuit breaker** — reset the Smartsheet safety breaker to CLOSED (skip the cooldown) once
  the underlying issue is resolved.
- **Resolve review-queue backlog** — move a stale, understood class of PENDING review rows to a
  terminal status (with a stamped note; nothing deleted), once its cause is fixed or moot. A filter
  is required and a **preview** shows the count before anything is written.

### Class C — secret rotation (write-only)

You can rotate a fixed list of credentials (API tokens, Worker bearers) through the same elevated
ceremony. Rotation is **write-only by construction**: the dashboard **never displays, reads back, or logs
a secret value** — it only writes the new value to its destination (macOS Keychain, and for a Worker
bearer, the Worker plus its Keychain mirror). Only credentials on the fixed list are rotatable; anything
else is refused. The **Box refresh token is guided-only** — it is never pasted here; the dashboard walks
you through the separate quiesce → re-authorize → smoke flow instead (a Seth-run, high-class step).

Some settings are shown but **can never be edited** on any surface — most importantly the **External Send
Gate** itself. It is display-only, by design.

## The hard invariant — what it will NEVER do

Everything the dashboard can do is a **local, internal control** — edit an `ITS_Config` setting, start /
stop / restart a worker, change a poll interval, or clear the safety breaker. It has **no ability to send
email and no ability to deploy code** — it holds no send capability and no AI. The permanent **External Send
Gate** stays entirely with the daemons; approving a customer send is done on the review sheets, and queuing a
code/config deploy is the job of the §50 portal app, not this dashboard. Even in the worst case, the
dashboard cannot put a message on the wire or push code.

## It ships dark

Out of the box the ACT surface is **fail-closed**: until Seth provisions the **operator PIN**
(`ITS_OPERATOR_PIN` in the Mac Keychain), every attempted change is denied. The read-only health panels
work without it. Activating the ACT surface — setting a **strong** PIN and allowlisting the Tailscale
origin — is a one-time, secrets-class step done by Seth.

## Two things guard every change

1. **The operator PIN** — a shared secret checked in constant time, that **fails closed** (a missing or
   locked Keychain denies). Wrong guesses are throttled: after 5 failures the endpoint locks out for a
   minute and fires a CRITICAL alert. Use a **strong** PIN, not a 4-digit one — especially before exposing
   the page over Tailscale.
2. **An Origin allowlist** — the page only accepts changes coming from localhost or an approved Tailscale
   address, as extra protection on top of the PIN.

Every applied change, every denial, and every secret rotation writes a durable audit row to
**`ITS_Errors`** (naming the setting and who did it — never a secret value).

## If something looks wrong

| What you see | What it means / what to do |
|---|---|
| A health card says **"unavailable"** | Its source (a file or daemon) isn't present right now — normal for anything not yet running. |
| The page won't load or a page errors | It runs as a launchd service that self-restarts; check it with `install.sh status org.solutionsmith.its.dashboard` (or `/healthz`), and reload with `install.sh load org.solutionsmith.its.dashboard` if needed. No data is at risk. |
| **"operator PIN not provisioned"** | The fail-closed default — the PIN isn't set. This is a **secret**, so **Seth** provisions it (not a Tier-2 step). |
| **"keychain is locked"** | Common after a reboot. Run `security unlock-keychain` on the Mac, then retry (Tier-2). |
| **"incorrect PIN"** | Re-enter it. Attempts are audited. |
| **"too many failed attempts — locked out"** | Wait 60 seconds and retry with the correct PIN. If you did **not** cause it, treat the CRITICAL as a possible attack and **escalate to Seth**. |
| **"origin … is not allowed"** | The browser's address isn't allowlisted — set the exact Tailscale origin and restart the app (Tier-2). A stranger hitting this is the control working. |
| **"… is not an editable key"** | The setting is intentionally read-only, or has no seeded row. Seeding a missing row is Seth's. |
| A **send-worker activation** is blocked/escalated | By design — first dark→live activation and the send gate are Seth's. Pausing is always available to you. |
| **"rejected: must be …"** | A validator caught a bad value; nothing was written. Fix the value to match the stated rule and retry (Tier-2). |
| **"write failed …"** | Usually the Smartsheet circuit breaker is open — check the breaker panel; once it closes, retry. A persistent token error is high-class → Seth. |

**Always to Seth:** provisioning or rotating the PIN, any first send-worker activation, editing a Class-B
setting you're unsure about, seeding a missing config row, and anything touching code or secrets. These
are the fixed "call the developer" categories (Op Stds §44).

## Owner

`@solutionsmith`. Part of the §6 / A8 documentation program. This in-repo version is the source of truth
for its content; the polished distributable PDF is rendered from it.
