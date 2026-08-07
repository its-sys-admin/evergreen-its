---
type: session_log
date: 2026-07-26
status: closed
related_prs: []
workstream: infrastructure
tags: [session_log, infrastructure, host_migration, cutover, phase_1.5, alerting_gap, tailscale, operational]
---

# Session log — 2026-07-25 → 2026-07-26 · Production-host migration Phase 1: dev-box teardown + new production-host stand-up

Operational session. **Zero commits, zero PRs.** Seth migrated ITS off his daily-use
development MacBook onto a dedicated production MacBook that will run unattended in
Florida while he travels, controlled over Tailscale screen share from Boston. There is
no code diff to point at and nothing for `pr-landed-verifier` to check — every claim
below is proven by an operational artifact (a command output, a timestamp, a live
service response), not by CI. This log records those artifacts and the decisions made
while producing them.

## Purpose

Move the launchd daemon fleet, Keychain secrets, and repo checkouts off the
development MacBook and onto the dedicated production host, tear the dev box down
durably, and leave the new host in a verifiable pre-activation state ahead of the
travel window. Related: `docs/operations/host_migration_runbook.md` (the pre-existing
Phase A/B/C plan this session executed against and diverged from — see "Plan changes"
below).

## Pre-flight findings — recon surfaced a blocker before any teardown

Seth asked to begin "as long as there are no blockers." A 10-agent recon workflow ran
first and found several; the headline was that the system could not do the thing it
was being migrated *for*: **there is no working outbound alert path.**

- `system.heartbeat_url` in `ITS_Config` is still the literal string
  `PLACEHOLDER_uptimerobot_heartbeat_url` — the dead-man's switch
  (`shared/heartbeat_client.py`, wired 2026-05-28 per PR #114 / F16) has never had a
  real endpoint behind it, so watchdog silently skips its external ping every run. No
  dead-man's switch has ever actually existed.
- `shared/resend_client.DEFAULT_FROM` is still the Resend sandbox sender
  `onboarding@resend.dev` (`shared/resend_client.py:56`), which 403s to every recipient
  except one mirror address. This is a pre-existing, already-tracked gap
  (`docs/tech_debt.md` — "resend_client.DEFAULT_FROM swap — blocked on CL-10
  solutionsmith sender-domain verification [OPEN 2026-07-23]") — this session did not
  discover it new, but the recon reconfirmed it live and gave it new weight given the
  unattended-host context.
- Consequence observed: **38 CRITICALs went undeliverable on 2026-07-24.**

Seth's call: proceed with the migration and track the fix rather than block on it. The
production host will therefore run for the travel window with **no verified external
alert path** — see "Open items" below.

## Decisions (all operator-made)

1. **Both repos stay live.** Dev continues on `SolutionSmith-debug/its`; production runs
   `its-sys-admin/evergreen-its` — a new, public, non-fork mirror repo created
   2026-07-25.
2. **Old Mac fully out of service.** Durable teardown, not a pause — all 15 launchd
   daemons unloaded, not just stopped.
3. **FileVault OFF on the production host.** Unattended-reboot recovery over disk
   encryption — a machine alone in a house with no one to enter a FileVault password on
   boot must come back up on its own after a power event.
4. **Send lanes FULLY DARK for the travel window.** `VC-02` reads FAIL by design — this
   is the intended, verified state, not a defect to chase.
5. **Tenant posture migrated AS-IS.** `sheet_ids` production, `worker_base_url` mirror —
   the existing phase1-hybrid split carries over unchanged onto the new host.
6. **Blueprint cloned as a sibling but deliberately NOT mirrored to the Evergreen
   account.** `its-blueprint` is Solution Smith cross-customer IP; it lives on the
   production host as a local sibling checkout only, never pushed to a repo the
   customer-facing account could see.

## Plan changes made mid-session

Two deviations from the pre-existing `host_migration_runbook.md` plan, both
operator-directed:

1. **New repo, not a same-repo re-host.** The runbook as written assumed the same
   `SolutionSmith-debug/its` checkout moving to a new machine. Seth instead had the
   production host clone the new `its-sys-admin/evergreen-its` mirror (decision 1
   above) — the two-repo split is a plan change made in-session, not something the
   runbook anticipated.
2. **Documentation-consolidation pass requested alongside the migration.** Produced 172
   findings, not yet landed (see "Open items").

## Migration evidence (operational, not CI — quoted verbatim)

**Dev-box teardown**, 2026-07-25T15:53:15Z — all 15 loaded daemons taken down via
`install.sh unload`:

```
launchctl list | grep solutionsmith
→ (empty)

ls ~/Library/LaunchAgents/org.solutionsmith.its.*
→ (empty)

install.sh status
→ "no ITS jobs loaded"
```

Re-verified 2026-07-26T17:59:27Z, ~26 hours later — nothing had come back:
newest watchdog marker frozen at Jul 25 11:52.

**State transported over Tailscale**: 26 state files + 19 watchdog markers, with
`*.lock`, `heartbeat_row_ids.json`, and `box_oauth_last_refresh.json` deliberately
excluded (see "Non-obvious findings" below for why).

**Production host, verified state:**

- arch: arm64
- macOS 26.5.2
- TZ: America/New_York
- FileVault: Off
- `evergreen-its` @ `885d4a4`, clean tree
- blueprint symlinks intact
- venv: Python 3.13.14
- Keychain secrets: 20/20 present
- Box OAuth completed on the production host 2026-07-26T20:07:43Z
- launchd: empty (nothing loaded yet — Stages 10-13 not run, see "Open items")
- `wrangler d1 migrations list --remote` → "No migrations to apply"
- `evergreen-its` CI → SUCCESS

## Non-obvious findings worth recording

- **`heartbeat_row_ids.json` was deliberately NOT copied.** Letting the new host build
  its own cache from scratch makes `ITS_Daemon_Health`'s `Total Cycles` reset and climb
  from zero on the production host — the *only* positive evidence of takeover. Nothing
  in ITS records which physical host wrote a given row, and `VC-04` reads green whether
  takeover happened, never happened, or both hosts are writing at once. A fresh cache
  is the tell.
- **Secrets were transported by fingerprint, not length.** Verified by sha256
  first-8-chars, not by byte count. Nine portal secrets are all exactly 64 characters —
  a length check would not catch a transposition between them, and a swapped
  `ITS_PORTAL_HMAC_SECRET` makes every portal submission verify `False` and present as a
  portal bug, not a secrets-transport bug.
- **Two assistant claims during this session were WRONG and Seth caught both.**
  Recording honestly rather than smoothing over:
  - Claimed "branch protection is absent on evergreen-its" — the checking vantage point
    was wrong. GitHub returns 404 (not 403) for a branch-protection read by a
    non-admin, which is indistinguishable from "no protection configured" unless you
    know to check from an admin session.
  - Claimed "the `gh` token is invalid" — also a vantage-point artifact. A non-GUI SSH
    session cannot decrypt the macOS login keychain (`User interaction is not
    allowed`), so `gh` reports its keyring-stored token as invalid when it is not.
  - Neither was a real defect in the migration. Both are recorded so a future session
    doesn't re-diagnose the same false alarms from an SSH vantage point.

## PRs landed

**None.** Zero commits, zero PRs this session — there is nothing for the four-part
landing verify to check, and none is asserted. Every claim in this log is backed by an
operational artifact quoted above (command output, live-service response, or a
timestamp), not by `pytest`/`mypy`/`ruff`/CI. Per `docs/operations/pr_merge_discipline.md`
and Op Stds §55.3/§55.4, a landing claim requires the four-part verify — this session
makes no such claim because there is no PR to make it about.

## What was NOT touched / deliberately deferred

- No launchd plists loaded on the production host yet (Stages 10-13 outstanding, below).
- No send-lane activation — deliberate, per decision 4.
- No alerting fix (`heartbeat_url`, `resend` sender domain) — flagged, not built, this
  session.
- No doc-consolidation findings landed (172 outstanding, produced but not merged).
- The old Mac was torn down but not yet fully disarmed (see below) — a full Friday
  cycle must pass first.

## Open items handed off (next session / Seth)

1. **Stages 10-13 of the migration** — plist render + lint, load the 15 non-send
   daemons, point the operator dashboard at the production host's Tailscale origin, and
   capture acceptance evidence for the loaded fleet.
2. **Unattended-reboot keychain proof.** Must be exercised from a GUI session — a
   headless SSH session cannot prove the login-keychain unlock survives a reboot
   without a human present (same class of gap as the two corrected assistant claims
   above: SSH is not a faithful stand-in for the machine's real unattended state).
3. **Alerting.** Both gaps found in pre-flight recon remain open: `system.heartbeat_url`
   needs a real UptimeRobot (or equivalent) endpoint, and `resend_client.DEFAULT_FROM`
   swap remains blocked on CL-10 sender-domain verification
   (`docs/tech_debt.md`, entry dated 2026-07-23). The production host will run the
   travel window without a verified external alert path until one or both close.
4. **Old-Mac disarm.** Full teardown was done, but the disarm decision should wait for
   a full Friday cycle to pass clean on the production host before treating the old
   machine as permanently retired.
5. **172 unlanded doc-consolidation findings** from the mid-session request — not
   triaged or merged this session.
6. **Five procurement daemons froze on the old host on 2026-07-24** and were never
   diagnosed. Carried forward unexamined — worth a `diagnose`-skill pass before or
   during the burn-in window, independent of the migration itself.

## Cross-references

- `docs/operations/host_migration_runbook.md` — the pre-existing Phase A/B/C plan this
  session executed against; Plan-changes section above records where this session
  diverged from it (new-repo split, doc-consolidation add-on).
- `docs/operations/pr_merge_discipline.md` — the four-part landing-verify discipline
  this log explicitly does NOT invoke, because there is nothing to verify.
- `docs/tech_debt.md` — "resend_client.DEFAULT_FROM swap — blocked on CL-10
  solutionsmith sender-domain verification [OPEN 2026-07-23]"; the `heartbeat_url`
  placeholder gap is not yet a dedicated tech-debt entry (flagged here for
  session-close-maintainer to add one).
- CLAUDE.md "Maintenance & successor-operator model" — the production host now carries
  the Tier-1/Tier-2/Tier-3 self-heal model in practice; Tier-1 self-heal (watchdog,
  launchd re-invocation) is only as good as the alert path in item 3 above, which is
  currently unverified.
- `docs/HOUSE_REFLEXES.md` §3 (git/worktree/deploy discipline) and §7 (known platform
  gotchas) — the Keychain `security -w` TTY trap and the SSH-vs-GUI-session distinction
  found this session are both instances of the existing "known platform gotcha" class;
  candidates for a future §7 addition if the SSH/GUI distinction recurs.
- **Memory:** no existing auto-memory entry names this session yet. Candidate for
  `session-close-maintainer` to add: a `project_` or `reference_` entry capturing (a)
  the production-host migration state as of 2026-07-26 (Stages 1-9 done, 10-13
  outstanding) and (b) the alerting-gap-during-unattended-operation finding, since it
  bears directly on whether Tier-1 self-heal is actually observable while Seth travels.
