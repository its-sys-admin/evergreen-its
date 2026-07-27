---
type: operations
date: 2026-07-10
status: active
related_prs: []
workstream: null
tags: [cutover, aug7_delivery, punchlist, operator, host_migration]
---

# Cutover operator punch-list — the on-site-binder walk (ordered by calendar)

## Purpose

The operator/Seth-gated items for the Aug-7 delivery, ordered by the master calendar, each with its
mechanical verify. Claude Code CANNOT perform these (Smartsheet shares, DNS, M365 app-reg, Cloudflare
deploy/plan/WAF, Box OAuth, Keychain secrets, human go/no-go) — they are surfaced here with the exact
verify so the binder can walk them. Code/doc work is landing separately (see
`docs/session_logs/2026-07-10_cutover-gap.md`).

Each row cites its checklist item (`CL-NN`) in `docs/operations/cutover_checklist.md` and, where
mechanical, its gate check (`VC-NN`). Runbooks: `host_migration_runbook.md`, `production_rollback.md`,
`aug7_delivery_runbook.md`.

---

## ⓪ PRE-EVERYTHING — the one NEW gap surfaced 2026-07-10

- [x] **CL-23 — DONE 2026-07-22: branch protection enabled on `main`** (required checks
  `test`+`portal`+`secrets`, strict up-to-date, `enforce_admins=true`, no required reviews —
  preserves autonomous slice-PR merging). **Bite-proven**: a direct push was server-rejected.
  Original item:
  `gh api repos/SolutionSmith-debug/its/branches/main/protection` → **404 "Branch not protected"**
  today. Add required status checks `test` + `portal` + `secrets` (+ require branch up-to-date).
  Repo-admin GitHub setting. Verify: the same `gh api …/protection --jq '.required_status_checks.contexts'`
  lists all three. *(CLAUDE.md already flags this as the open "server-side authoritative" follow-up.)*

## ① Jul 10 — Phase A provision (dev box stays live; additive/read-only)
Ref: `host_migration_runbook.md` Phase A (A1–A7).
- [ ] A1 macOS baseline + FileVault owner sign-off.
- [ ] A2 toolchain incl. **NET-NEW Tailscale + ClamAV** (ClamAV is the prerequisite for the CL live
  EICAR smoke below and for enabling `photo_screen.clamav_enabled`).
- [ ] A3 repos as `~/` siblings — `find -L ~ -maxdepth 2 -type l` prints nothing (symlink integrity).
- [ ] A4 venv + provision gate: `pytest` / `mypy` / `ruff` all green.
- [ ] A5 Keychain re-seed of the 11 non-Box secrets + PENDING `ITS_PORTAL_PO_TOKEN` (Box triplet
  DELIBERATELY absent in Phase A — single-consumer refresh token).
- [ ] A6 launchd render + **lint only, NO load** (`install.sh` has no install-without-load mode →
  Phase A does not install plists; runbook amendment overrides the program doc's "installed UNLOADED").
- [ ] A7 read-only service smokes (no sends/writes; Box intentionally not smoked).

Day-1 external requests to fire (program doc "Operator-side actions remaining"): M365 admin, DNS,
Teala recipient + vendor lists, the 7 approver accounts, Box production account, network details,
**Workers Paid-plan confirm** (drives CL-21), Aug-7 logistics + supplier-stand-in mailbox, mirror
`progress@` / `procurement@`, legal confirm of purchaser entity + 17-clause T&C, `gh issue close 338 340`.

## ② Jul 13 — Phase B one-way flip (~30-min ORDERED window; do NOT parallelize)
Ref: `host_migration_runbook.md` Phase B. #1 hazard = daemon double-run. **Must not slip past Jul 14.**
- [ ] (1) dev box: unload EVERY loaded `org.solutionsmith.its.*` daemon (the count is
  whatever `launchctl list | grep -c solutionsmith` says — don't trust a hardcoded number).
- [ ] (2) verify `launchctl list | grep solutionsmith` prints **nothing** on the dev box.
- [ ] (3) verify plists REMOVED from `~/Library/LaunchAgents`.
- [ ] (4) rsync `state/` + `.watchdog/` markers to the new host.
- [ ] (5) Box re-auth via a fresh `setup_box_oauth.py` on the **new host ONLY** — never run Box code
  on the dev box again.
- [ ] (6) `git pull origin main` on the new host, then load all daemons but `po-send` **and `rfq-send`** (both SEND daemons stay unloaded — send-gate; both in `DARK_UNLOADED_LABELS`). Their go-live is a FIXED External-Send-Gate flip + load done with Seth, never at cutover.
- [ ] (7) verification gate: labels loaded, fresh Check-C markers, ITS_Daemon_Health advancing, portal
  round-trip, **and the Healthchecks.io prove-it-bites**. Note the watchdog pings **once per day
  at 07:00** (`StartCalendarInterval`), so an unload-and-wait-35-min drill proves nothing — run the
  drill at the monitor instead (make the check overdue there, confirm the alert ARRIVES, restore
  period = 1 day, re-run the watchdog → green). **Prerequisite:** the beacon must be armed first —
  see CL-27; while `system.heartbeat_url` holds its seed placeholder the watchdog skips the ping
  entirely and this gate cannot pass.

## ③ Jul 14 → Aug 3 — Phase C burn-in + hardening gate
Ref: `host_migration_runbook.md` Phase C. Fri Jul 24 = CODE FREEZE on daemon paths; Jul 25-30 operator
away = the unattended Tier-1 live trial; **Fri Jul 31 = the host GO/NO-GO** (the hard prerequisite for
the cutover checklist — CL-01; no-go moves the date).
Hardening-gate items (Jul 14-16):
- [ ] **CL-21 Paid-plan-or-PBKDF2 verdict.** Login is **bcryptjs cost-10 only** today (no PBKDF2 fork).
  If Workers Paid is confirmed → no code (cost-10 `bcrypt.compare` fits the Paid CPU budget). If Paid is
  unavailable → the PBKDF2 swap (`crypto.subtle.deriveBits`, replacing `bcrypt.compare`/`bcrypt.hash` in
  `safety_portal/worker/auth.ts`) is a **secrets/auth high-class code change → Seth co-resolution**, and
  it MUST land **before** CL-20 account provisioning. Verify: real login on prod returns **200 (no Error
  1102)** + dashboard shows Worker on Paid.
- [ ] **CL-22 WAF `/api/login` rate-limit.** Not built in code (operator-gated Cloudflare dashboard,
  tech_debt #2455). Stage a rate-limit rule ~**5 req / 10s / IP** on `/api/login` + a blanket `/api/*`
  rule. Verify (bogus usernames only — do NOT lock a real account): 12 rapid bogus-credential POSTs from
  one IP → the tail returns **429**:
  `for i in $(seq 12); do curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<prod-domain>/api/login -H 'content-type: application/json' -d '{"username":"nobody","password":"x"}'; done`
- [ ] **ClamAV / EICAR portal-upload scanning.** Code is wired (`photo_screen._clamav_scan`, all portal
  upload paths call it) but ships **default-OFF** and the EICAR test is **mocked** (no live-clamd
  end-to-end). Operator: install clamd + pyclamd (Phase A2), then flip
  `safety_reports.photo_screen.clamav_enabled=true`. Prove-it-bites (HOUSE_REFLEXES §2): feed a
  runtime-constructed EICAR payload through `photo_screen.screen_photo(..., clamav_enabled=True)` on the
  host and confirm disposition = **malicious** (a live-clamd smoke; CI cannot run clamd).
- [ ] **CL-19 D1 production hygiene** (read-only wrangler on prod):
  `npx wrangler d1 execute its-safety-portal-db --remote --command "SELECT username, disabled FROM users"`
  → only real accounts (no `test.pm`); `… "SELECT COUNT(*) c FROM submissions"` → 0 / only real rows.

## ④ Aug 3 — CUTOVER DAY (verify_cutover green)
Ref: `cutover_checklist.md` + `production_rollback.md`. Gated by the Jul-31 go/no-go.
- [ ] **(Seth schedules) EARLY real production stand-up — days BEFORE Aug 3.** The one-step
  stand-up (`standup.py --no-restore`) can run against the production tenant early: objects
  build dark (every gate row seeds to its safe value; nothing sends), the regenerated
  ID-surface landing PR is simply HELD unmerged until cutover day, and Aug-3 shrinks to
  merge + `finish` + bridge. **Sequencing caveat:** the production `ITS_SMARTSHEET_TOKEN`
  swap must slot inside the phase1-hybrid window — never stand next to a mirror-pointed
  fleet with a production token in Keychain (host_migration_runbook.md hazards).
- [ ] **Production tenant STAND-UP (one step — rehearsal-proven 2026-07-23).** From a
  per-task worktree with its OWN venv (worktree_discipline.md), daemons down:
  `python3 scripts/migrations/standup.py --no-restore`
  (builders + auto-FLIP + seeds end-to-end; a failed stage prints its resume hint —
  `--resume` restarts at the first incomplete stage AND fetches+merges `origin/main`
  onto the run branch itself, so a mid-run fix-PR is just: land it on main, re-run
  `--resume` — conflicts surface and STOP, never auto-resolved). Then land the
  regenerated ID-surface PR (run-branch mode pushes the branch + prints the
  `gh pr create` command); after merge + `git -C ~/its pull`:
  `python3 scripts/migrations/standup.py finish`
  (state cleanup → DARK fleet reload [send-dispatch plists stay unloaded] → heartbeat
  wait → error sweep → the read-only gate-flip worksheet → dashboard restart LAST),
  **then the bridge step** — load `weekly-send`/`progress-send`/`subcontract-send`
  per-plist (the established lanes; see the checklist stand-up callout) before any
  VC-02-bearing gate. Runbook: `docs/runbooks/tenant_standup.md`.
- [ ] **CL-31 (ordering: FIRST) sealed mirror-secret backup** — `security find-generic-password … -w`
  each secret → offline medium (never repo/log/Smartsheet/cloud), BEFORE any secret is overwritten.
  Box caveat: the sealed token is valid only while UNUSED. Verify: `curl -sI https://safety.evergreenmirror.com/`
  → 200 (rollback target alive).
- [ ] CL-02 Keychain on prod host (`verify_cutover --only keychain` PASS).
- [ ] CL-05 prod Azure app-reg (`smoke_test_graph.py` exit 0).
- [ ] CL-06 EXO Application Access Policy (Granted for ITS mailbox, Denied for non-ITS).
- [ ] CL-07 prod mailboxes `safety@`/`progress@`/`procurement@` exist.
- [ ] CL-08 DKIM/SPF (`dig TXT`/`dig CNAME`).
- [ ] CL-09 portal prod DNS live + serves Worker (`curl -sI` 200 + asset content-type).
- [ ] CL-10 Resend sender domain Verified.
- [ ] **CL-11 workspace approver shares (mechanized)** — review/edit
  `scripts/migrations/production_shares_manifest.json`, then
  `seed_production_shares.py` PLAN → `--commit` (Seth; ADD-only — the 3 mirror-account
  unshares stay a manual UI step, named loudly by the plan). Verify:
  `verify_cutover --only approver-shares` → PASS (VC-10; the Ezra-typo class is
  refused mechanically at manifest load). Expect a ONE-TIME watchdog Check U
  approver-drift WARN after the seed + manual mirror unshares — confirm the +/- lists
  match the manifest edits (`state/approver_set_baseline.json` self-persists on the
  next sweep); an unexplained delta beyond the manifest is escalation evidence, not
  noise.
- [ ] **CL-12 ITS_Config production sweep (mechanized)** — apply
  `production_repoint_changeset.md` §A–D via `production_repoint.py` PLAN → `--commit`
  (Seth; typed phrase; DRIFTED rows refuse the whole sweep; §E gates structurally
  excluded — flips stay CL-13). Verify: `verify_cutover --only config` (NO
  `--allow-sandbox`) → PASS.
- [ ] CL-13 read each `*_enabled` row's Description before flipping (in-cell precondition = doctrine).
- [ ] CL-15 (Smartsheet half) ITS_Review_Queue Workstream picklist column ⊇ live workstreams
  (REGISTRY code half already green).
- [ ] CL-16 Box production identity (its@evergreenrenewables.com) + CL-17 Box roots reseeded.
- [ ] CL-18 remote D1 schema current (`verify_cutover --only d1-migrations` PASS). **Pull `~/its` to
  latest main BEFORE any `wrangler d1 migrations apply/list`** (stale-checkout lockout). Includes
  applying migration **0047** (`config_requests.cleared_at`, Feature 1) before the Worker deploy.
- [ ] CL-20 real PM accounts (`portal_admin add-user`), after CL-21 if PBKDF2 was needed.
- [ ] **Send paths LAST (CL-24 → CL-28, safest-first order):** CL-24 daemon-health, CL-25 review-queue,
  CL-26 alerting, CL-27 Healthchecks.io heartbeat — all `verify_cutover --only <check>` PASS. Then **CL-28 fail-closed
  send smoke**: a member-approved review row DISPATCHES, a non-member-approved row is BLOCKED +
  `approval_unverified` lands in ITS_Errors (proves F22 against prod identities BEFORE real recipients).
- [ ] **CL-29 real-recipient wiring** (Teala-coordinated) — ITS_Active_Jobs contact + CC columns carry
  production recipients; zero `evergreenmirror.com`, zero blanks on active rows.
- [ ] **CL-30 THE GATE** — `python -m scripts.verify_cutover` (full run, NO flags) exits **0** on the
  prod host; paste output into the cutover session log verbatim (§53).
- [ ] CL-32 Day-7 routing gate armed (alerts stay routed to Seth beyond Day 7; dated review date recorded).

## ⑤ Aug 7 — Delivery (system already in production since Aug 3)
Ref: `aug7_delivery_runbook.md`. Thu Aug 6 = T-1 buffer (no build work). HARD CODE FREEZE since Aug 5.
- [ ] Transport window: enter `system.state=MAINTENANCE` + Healthchecks.io maintenance window; graceful
  shutdown after portal_poll quiets.
- [ ] On-site: the **7 ordered install gates** (do not demo past a red gate) — power/placement, network
  (outbound 443), boot+login → `launchctl list | grep -c solutionsmith` matches the must-load count (the
  shipped plist set minus `po-send` + `rfq-send`, both dark-unloaded — send-gate; VC-02
  derives it, 18 at last count), Tailscale reverse
  access over hotspot, clear MAINTENANCE→ACTIVE, `verify_cutover` re-run on-site (exit 0, paste to log),
  fresh Check-C markers.
- [ ] Demo (~40 min, mind the Friday-14:00 `weekly_generate` rule — narrate it live or pre-empt via
  Compile Now), training (~60 min, drills BY trainees), acceptance (owner signs Step-8; D17 language read
  aloud), leave-behind package (printed 12-PDF manual set + emergency card + signed acceptance).

## ⑥ T+7 — Day-7 review
- [ ] **CL-33** — zero unexplained CRITICALs, Check-C markers continuous, dedupe summaries reviewed;
  THEN disable mirror portal users + optionally tear down the mirror Worker (the end of rollback
  capability). Record a dated session log.

## Owner
Developer-Operator (Seth). Rollback (any leg but R-global `system.state=PAUSED`) is §44 high-class →
Seth. The Successor-Operator's only cutover-window action is R-global (the PAUSED brake).
