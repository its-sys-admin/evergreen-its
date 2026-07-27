---
type: operations
date: 2026-07-09
status: active
related_prs: []
workstream: null
tags: [aug7_delivery, delivery_day, cutover, training, acceptance]
---

# Aug-7 Delivery Runbook (on-site install · demo · training · acceptance)

The scripted delivery day at Evergreen's office, **Friday 2026-08-07** — the
§4.4 day-of runbook of `docs/2026-07-09_aug7_delivery_program.md`, expanded
to executable steps. By this morning the system is ALREADY in production
(cutover Aug 3, `cutover_checklist.md` walked, `verify_cutover` green, dress
rehearsals Aug 4–5 complete, HARD CODE FREEZE since Aug 5): the demo is the
real system on live tenants, not a staging act.

## Purpose

Move the production host to Evergreen's office without a silent-failure
window, prove the system live in front of the owner, put the operator drills
in THEIR hands, and leave with a signed Step-8 acceptance and a working
leave-behind package.

## Procedure

### T-1 — buffer day (Thu Aug 6, kept empty of build work)

- [ ] Pack list staged: MacBook Pro (production host) · charger · Ethernet
  cable + USB-C adapter · printed leave-behind set (below) · backup hotspot
  (phone tether verified) · the on-site binder (this runbook +
  `cutover_checklist.md` + `production_rollback.md`, printed).
- [ ] Print + charge everything.
- [ ] **Foreign-network Tailscale test:** from the hotspot (NOT the home/work
  LAN), `tailscale status` on Seth's laptop reaches the production host and
  an SSH-over-Tailscale session opens. This is the Tier-3 lifeline once the
  host lives behind Evergreen's router.
- [ ] Final go/no-go with the owner (meeting logistics + supplier-stand-in
  mailbox confirmed reachable).

### Transport window (Fri morning)

1. **Enter MAINTENANCE** — ITS_Config `system.state [global]` → `MAINTENANCE`
   (alerts suppressed-but-recorded; watchdog Check G defers summaries).
   Verify: `python -c "from shared import smartsheet_client as s; print(s.get_setting('system.state', workstream='global'))"` → `MAINTENANCE`.
2. **Healthchecks.io maintenance window** covering transport (dashboard →
   monitor → Maintenance Windows) so the dead-man ping doesn't page during
   the drive. Verify: window shows scheduled/active on the monitor.
3. **Graceful shutdown:** wait for the current portal_poll cycle to complete
   (`~/its/logs/launchd/portal_poll.out.log` goes quiet), then Apple-menu
   Shut Down. (Daemons are one-shot-per-interval; there is no long-running
   job to drain besides an in-flight cycle.)
4. Pack per the T-1 list. GO.

### On-site install gates (run IN ORDER; do not demo past a red gate)

| # | Gate | Verify |
|---|------|--------|
| 1 | Power + placement agreed (owner names the spot; wired power, no shared switch-controlled outlet) | visual |
| 2 | Network up — creds obtained BEFORE today; outbound 443 suffices (no inbound holes, no public SSH — Tailscale only) | `curl -sI https://api.smartsheet.com | head -1` returns a response |
| 3 | Boot + login (FileVault posture per `host_migration_runbook.md` A1 — login unlocks the keychain; LaunchAgents start) | `launchctl list \| grep -c solutionsmith` → 14 (po-send stays unloaded — send-gate) |
| 4 | Tailscale reverse-access from Seth's laptop **over the hotspot** (proves Tier-3 access survives Evergreen's NAT) | SSH session opens |
| 5 | Clear MAINTENANCE → `system.state` → `ACTIVE`; close the Healthchecks.io window | config read-back → `ACTIVE`; monitor green |
| 6 | **`verify_cutover` re-run on-site** | `python -m scripts.verify_cutover` exits 0 — paste output in the session log |
| 7 | Fresh Check-C markers post-boot | `ls -l ~/its/.watchdog/*.last_run` — mtimes advancing on the interval jobs within ~2 cycles |

### Demo arc (~40 min, the real system)

> **Friday 14:00 rule (rehearsed at both dress rehearsals):** Aug 7 is a
> Friday — `weekly_generate` fires at 14:00 Pacific. If the demo slot
> overlaps 14:00, either narrate the live fire (best) or **pre-empt it**: set
> the `Compile Now` checkbox on the target week sheet and let
> `compile-now-poll` (90 s) run the compile on demand during the demo. The
> skip-if-already-compiled-and-no-new-docs guard makes the 14:00 pass a
> clean no-op afterwards. Decide which BEFORE the demo starts; both paths
> were rehearsed Aug 4–5.

1. **① Field capture:** a field PM (or Seth on a PM account) submits a daily
   report + photos on the production portal from a phone. Show
   `portal_poll` pull within ~60 s and the filed PDF landing in Box
   (ROOT → job → week) live on screen.
2. **② PO built live:** portal PO builder — job select (auto-fill + tax
   badge) → vendor select → line items → terms picker → generate. Show the
   `PO_Pending_Review` row + the rendered PO PDF.
3. **③ Approval (F22):** a workspace-member approver checks Approve on the
   pending row — narrate that *membership IS authority* (workspace share
   list, cell-history-verified, fail-closed).
4. **④ Send:** the send poll dispatches; the PO email lands in the
   supplier-stand-in inbox ON SCREEN, from `procurement@evergreenrenewables.com`.
   (Two-process invariant: the thing that generated it cannot send; the
   thing that sent it has no AI.)
5. **⑤ Operator dashboard tour:** panels (daemon health, pending sends,
   errors, config-with-source), then one harmless §44 action (e.g. kickstart
   a daemon) to show the audited action path.
6. **⑥ Manuals handoff:** the printed branded-PDF set + where the same PDFs
   live in Box ("ITS Manuals").

### Training (~60 min) — drills executed BY the trainees, not shown to them

**PM track (~20 min, each PM):** log in on their own phone → submit a form
with a photo → find their filed report ("where did my report go" = Box path
+ the portal's filed view) → request a re-download.

**Owner/admin track (~40 min) — the Step-8 drills, each demonstrated BY the
owner/admin with Seth watching:**

| Drill | Done-when observable |
|-------|----------------------|
| Kill-switch flip + back (`system.state` → `PAUSED` → `ACTIVE`) | daemon logs show clean pause; dashboard shows state both ways |
| Approve a pending review row | row dispatches; `Sent At` stamps |
| Respond to a Sentry alert (open the issue from the email, read the correlation id, find the ITS_Errors row) | they navigate unaided |
| Trusted-contacts add + disable | sheet row edited; they can state what DISABLED does |
| `portal_admin` add-user / disable-user / list-users | new user logs in; disabled user is locked out (verify the lockout live) |

### Acceptance

Owner signs the **Step-8 acceptance** (handover-plan) **with the v10
amendment language** (decision D17 — read aloud before signing):

> Pre-cutover condition #5 (trained Tier-2 Successor-Operator) is
> deliberately amended, not waived: **Seth Smith remains the operator of
> record post-cutover.** Tier-2 operator training and per-category clearance
> is a named post-delivery milestone, scheduled for `<DATE — fill at
> signing>`. Until that milestone, all alerts route to Seth (Day-7 routing
> gate) and the §44 escalation boundary applies unchanged.

Then: session log `docs/session_logs/2026-08-07_production-cutover.md`
(include the on-site `verify_cutover` output verbatim) · Day-7 routing
confirmed armed (checklist CL-32) · calendar entry for the T+7 review
(CL-33).

### Leave-behind package (inventory — check each physically handed over)

- [ ] Printed branded-PDF manual set (the 12-PDF delivery-critical set, WS3).
- [ ] **One-page emergency card:** how to flip `system.state=PAUSED`, Seth's
  phone/email, the meta-002 escalation SLA.
- [ ] Signed acceptance copy (owner keeps one, Seth keeps one).
- [ ] Access/account inventory sheet: which accounts exist, who holds them,
  which are Seth-only (Keychain/secrets marked explicitly NOT for local
  administration).

## Validation

Delivery is done when: the seven install gates passed in order · the demo
arc completed on the production system · every training drill was executed
by the trainee with its done-when observed · the amended Step-8 acceptance
is signed with a named Tier-2 milestone date · the leave-behind inventory is
fully checked · the session log exists with the on-site `verify_cutover`
output pasted verbatim.

## Owner

`@solutionsmith`. Rehearsal deltas from Aug 4–5 get folded into this runbook
BEFORE the Aug-5 hard code freeze (docs-only edits stay allowed on the live
tree per worktree discipline).
