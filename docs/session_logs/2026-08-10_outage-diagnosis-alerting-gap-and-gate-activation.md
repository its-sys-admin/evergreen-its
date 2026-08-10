---
type: session_log
date: 2026-08-10
status: closed
related_prs: [11, 12, 13, 14, 16]
workstream: infrastructure
tags: [session_log, infrastructure, watchdog, alerting_gap, external_send_gate, gate_activation, po_materials, subcontracts, estimates, operator_dashboard, outage_postmortem]
---

# Session log — 2026-08-06 → 2026-08-10 · Outage diagnosis, the second alerting-gap occurrence, External Send Gate activation across procurement, and five merged PRs

## Purpose

Started as a narrow ask — "diagnose the dashboard, make sure we're back online, clear stale
errors." Became a five-day arc: a 12.7-hour Smartsheet outage postmortem, a corrected mistake on
an approver-share precondition, operator-directed activation of the External Send Gate across the
whole procurement stack (14 `ITS_Config` gates + 5 send daemons), and five merged PRs closing real
defects the diagnosis and activation work surfaced along the way.

## Pre-flight findings

- **The dashboard was never actually down.** PID 1381, HTTP 200. The "offline" symptom traced to a
  **~12.7-hour Smartsheet backend outage** (HTTP 500 code 4000 + read timeouts; circuit breaker
  OPEN 729 minutes; 361 short-circuits logged) — every Smartsheet-backed daemon froze for that
  window ("approved sends FROZEN and nothing is being filed"). The outage had self-healed roughly
  5 minutes before the session started looking at it.
- **3 stale open CRITICALs** were cleared, each verified healthy first (not blind-resolved).
  Clearing them released watchdog Check W's incident hold; log rotation then ran, `~/its/logs`
  going 72M → 66M.
- **The outage paged zero times.** All three legs of the CRITICAL triple-fire degraded
  simultaneously: `ITS_Errors` couldn't write (Smartsheet itself was the outage), Resend rejected
  every send with a 403 (unverified sender domain — the account is in test mode, so only the
  account owner's own address is deliverable, and `system.operator_email` is not that address),
  leaving Sentry as the sole surviving leg. This is the **second occurrence** of two design gaps
  already on record from the 2026-07-15 error-flood diagnosis (`ITS_Errors` writes are lost, not
  queued, during a Smartsheet outage; a total Smartsheet outage by itself pages nobody in
  real time). The Resend leg is tracked separately in `docs/tech_debt.md` under the
  `resend_client.DEFAULT_FROM swap` entry, whose severity this session's finding raised from `low`
  to `HIGH` with the 403 payload quoted verbatim in that entry.

## Gate activation — the operator-directed External Send Gate flip

Operator directed flipping the procurement stack live. Per HOUSE_REFLEXES §5 ("read a gate row's
full Description before flipping it"), every gate's `ITS_Config` Description cell was read before
any `update_rows` call, not just its row ID.

**A mistake made and corrected in the same pass:** the first read of VC-10 (approver-shares
verification) reported zero approver shares and looked like a hard blocker. That reading was
**wrong** — re-verification showed all four workspaces were properly shared with 4 real Evergreen
users; VC-10's own check expects 7 names from a manifest, and 3 of those 7 are not shared users at
all (not a gap in the live sharing state). Corrected before acting on the wrong reading, which
would have stalled a legitimate go-live on a false blocker.

With that resolved: **14 gates flipped `true`, 5 send daemons loaded** (procurement PO/RFQ/
subcontract/estimate send + poll lanes). Verified **zero external email transmitted** as a direct
consequence — `dispatched=0` on all five lanes, where `dispatched` increments immediately before
`send_fn` is ever called, so a `0` is proof of no attempted send, not merely no confirmed one.

## Decisions worth recording

1. **PR #12 is a FILTER over `CHECKS`, not a wrapper.** `tests/test_watchdog.py:124` pins `CHECKS`
   by function identity, plus four more tests on membership/index — a wrapper would break all five
   and decouple `CHECKS` ↔ `CHECK_LETTERS` parity. Six checks are actively harmful run hourly: W
   inverts its own growth bound, I could fire ~72 weekly compiles minting unrotatable open
   CRITICALs, D pumps 24 review rows/day, O writes into the very sheet it warns about, L
   creates-and-deletes a real sheet every run, U shrinks a security window from 24h to 1h. Keeping
   W on the daily tier also preserved two cadence-coupled constants for free.
2. **PR #13** — `GH_MAIN_CI_REPO` pointed at a **different, still-active** repo, so the landing gate
   structurally could not fail; a gate that cannot fail manufactures false confidence rather than
   verifying anything. Deliberately did **not** sweep the ~15 other stale references to the old
   repo elsewhere in the tree — two repos are genuinely active now (this production host's
   `evergreen-its` and the dev-side `its`), and a blind sweep risked misdirecting tooling that
   correctly points at one or the other for reasons not yet audited.
3. **PR #14** — rejected synthesizing an xlsx preview from the tool's own parse output. Rendering
   our own extraction as its own confirmation would make a wrong parse self-confirming, defeating
   the ADR-0004 decision 3 fidelity control (no accept without a loaded *source* preview).
4. **PR #16** was found by testing the real artifact (`quote_form.render_quote_form`), not a
   synthetic fixture. The first merged-cell fill implementation was **wrong** — it filled across
   and overwrote the Qty header, producing zero lines — caught by a failing test and corrected to
   fill-down-only.
5. **Declined to point the E6 corpus baseline at this session's own test fixtures.**
   `--write-expectations` against documents this session wrote would qualify the tier against its
   own output — the same false-green shape being fixed in #13. Left open instead (see below).

## Prove-the-control-bites — every new/changed control RED-lit before shipping

- **#12** — defeating the filter (re-adding the wrapper shape) failed 2 tests; stamping the marker
  unconditionally failed 1.
- **#13** — re-injecting the old repo slug produced a FAILED run with an actionable message, not a
  silent pass.
- **#14** — disabling the cell-totals override failed 2 tests.
- **#16** — reverting to substring keyword matching failed 3 tests.

All four reverted back to the shipped state after confirming the RED.

## PRs landed

| PR | Title | Purpose |
|----|-------|---------|
| #11 | `docs(tech-debt): delivery-day cleanup — archive 4, sweep 6 sub-bullets, reconcile the lying index` | Tech-debt hygiene pass |
| #12 | `feat(watchdog): hourly sweep with a daily-only tier — close the outage blind spot` | Adds an hourly + daily-only watchdog tier as a `CHECKS` filter |
| #13 | `fix(watchdog): Check S was watching the WRONG repository — a silent false-green on a landing gate` | Repoints `GH_MAIN_CI_REPO` to the repo this host actually runs |
| #14 | `feat(estimates): deterministic Tier-1 for VENDOR spreadsheets — stop hand-keying Excel quotes` | xlsx estimate-import tier |
| #16 | `fix(estimates): header keyword "um" matched inside "Part NUMber" — column inference mis-assigned` | Substring-match bug in column inference |

Each verified independently against `its-sys-admin/evergreen-its` (this host's origin) via the
canonical four-part check (`docs/operations/pr_merge_discipline.md`): `state=MERGED` · `mergedAt`
non-null · `mergeCommit.oid` present · main-branch CI (`test`/`portal`/`secrets`) on the merge
commit = SUCCESS.

```
#11  MERGED  mergeCommit=4590f691735e61314d9c0c9d5d6a8ce138b69d57  mainCI(test/portal/secrets)=success/success/success
#12  MERGED  mergeCommit=41b9361c4f60839ba4df33683b940efca65f2a45  mainCI(test/portal/secrets)=success/success/success
#13  MERGED  mergeCommit=23ca3d17eca42b390f86574cfe6f4a73c97353aa  mainCI(test/portal/secrets)=success/success/success
#14  MERGED  mergeCommit=1553920dfe12eadd4ad5369f76b9d5e180fe7feb  mainCI(test/portal/secrets)=success/success/success
#16  MERGED  mergeCommit=4c630689d65d01ad957a352b222bd597804fff49  mainCI(test/portal/secrets)=success/success/success
```

**Four-part verify clean on all five PRs.**

## Incidents / process notes

- **The publish daemon stranded work on the live tree TWICE this session** (ran `git checkout`
  mid-cycle; once a commit landed on local `main` before the tree was later reverted). Recovered
  both times with nothing lost — this is the standing `docs/tech_debt.md` M7 finding
  ("Safety Portal M7 — publish daemon runs destructive git on the live `~/its` tree without a lock
  or worktree") biting live, not a new defect. Response was to move all subsequent work to
  per-task worktrees with their own venvs, per `docs/operations/worktree_discipline.md`.
- **Concurrent sessions landed PRs #17–#28 on the same `main` while this work was in flight** —
  `main` moved 4+ times under this session's branches; PR #16 needed two `gh pr update-branch`
  cycles to clear a behind-base state. Per HOUSE_REFLEXES §2 ("a textually-clean auto-merge is not
  semantically proven"), the full suite was re-run on the rebased/merged tree rather than trusting
  a conflict-free rebase — 4,659 outcomes clean on that run.
- **Nearly duplicated PR #28.** Another concurrent session had already fixed the same
  empty-`MULTI_PICKLIST` vendor/subcontractor up-sync bug this session had independently
  root-caused. On discovering the overlap, this session's own fix branch was stopped and the
  stray branch removed rather than racing a duplicate PR; the other session's fix
  (`c2876521512d3dfa91c016e3a40b9674d9e47cf7`, merged 2026-08-10) was reviewed instead — it is the
  better fix (raises on the empty-picklist case rather than papering over it, matching an existing
  convention, and it also caught the equivalent subcontractors-side case this session's draft had
  missed).

## Operational results — three days of live data

Post-activation soak, read from the live watchdog/error surfaces rather than asserted from
session-time observation:

- Watchdog sweep cadence: 24 / 23 / 10 sweeps across the three observed days, with exactly **one**
  daily-tier pass per day (the #12 filter behaving as designed). Watchdog's own write cost is
  ~31 `ITS_Errors` rows/day.
- Two error storms found and stopped during the soak: `archive_enabled` firing with **no
  ITS_Config row at all** (~500 rows/day until seeded) and `po_vendors_empty_projection` (self-
  resolved once vendors were seeded — not a code fix).
- Vendor up-sync recovered cleanly once PR #28 landed: `vendors down=8 up=33 errors=0`.
- **Open CRITICALs: 0** at last check.

## What was NOT touched — left open deliberately

- **Resend 403 (operator declined a fix this session; logged HIGH).** See `docs/tech_debt.md`,
  the `resend_client.DEFAULT_FROM swap` entry, 2026-08-06 update — the CRITICAL email leg is
  confirmed dead in production, blocked on CL-10 (verified sender domain).
- **`ITS_Errors` write durability during a Smartsheet outage** — the 2026-07-15 gap, now with a
  second real occurrence on record; still no queue/retry.
- **M365 secret-expiry detection** — not built this session.
- **`/api/login` rate limiting + bcrypt cost-10** — not addressed this session.
- **`/api/recent` ownership scope** — not addressed this session.
- **The ~15 other stale repo references** found alongside #13's fix — deliberately not swept (see
  Decision 2 above); two active repos exist now and a blind sweep risks misdirection.
- **The E6 corpus baseline** — the parser-eval corpus lives on the dev Mac, not this production
  host (a pre-existing gap, not introduced this session; cf. the 2026-08-07 manifest-import
  activation log's identical host-asymmetry finding). `po_materials.estimate_poll.tier1_enabled`
  (or the equivalent xlsx-tier gate landed in #14) is already live `true` while unqualified against
  that corpus — a code-vs-doctrine-adjacent divergence that predates this session and was not
  resolved by it. Declined to close it with a self-referential baseline (see Decision 5).

## Sequencing context

- What this unblocks: the procurement External Send Gate (PO / RFQ / subcontract / estimate send
  and poll lanes) is now live end-to-end with a verified zero-dispatch activation proof; the
  watchdog's hourly/daily split closes the "an hourly-only sweep can't see a daily-cadence problem,
  and a daily-only sweep can't see an outage that starts and ends inside one day" blind spot named
  in #12's title.
- What was prerequisite: the corrected VC-10 reading (Decision under Gate activation) — acting on
  the first, wrong reading would have stalled the whole activation on a non-existent blocker.
- Follow-ons: the corpus-baseline qualification (operator-run, off this host), the Resend
  domain-verification (CL-10, tracked in tech debt), and a decision on whether/how to sweep the
  remaining stale repo references now that two repos are both genuinely live.

## Cross-references

- `docs/tech_debt.md` — `resend_client.DEFAULT_FROM swap` entry (severity raised to HIGH,
  2026-08-06 update, quotes the live 403 payload this session surfaced) and the 2026-07-15
  error-flood diagnosis entry (both alerting-triple-fire gaps this session's outage re-hit).
- `docs/tech_debt.md` — "[OPEN 2026-06-09] Safety Portal M7 — publish daemon runs destructive git
  on the live `~/its` tree without a lock or worktree" (the standing finding behind this session's
  two live-tree strandings).
- `docs/operations/pr_merge_discipline.md` — the four-part verify applied to all five PRs above.
- `docs/operations/worktree_discipline.md` — the per-task-worktree response to the live-tree
  strandings.
- `docs/HOUSE_REFLEXES.md` §2 (prove-the-control-bites — the inject/confirm-RED/revert pattern on
  all four changed controls; "a textually-clean auto-merge is not semantically proven" — the
  full-suite re-run on the concurrently-moved `main`), §3 (worktree discipline — the publish-daemon
  strandings), §5 ("read a gate row's full Description before flipping it" — applied to all 14
  gates; the corrected VC-10 reading).
- `docs/session_logs/2026-08-07_manifest-import-activation.md` — the same "confirm X exists on the
  target host fails open in practice" host-asymmetry class, recurring here as the corpus-on-the-
  dev-Mac gap.
- PR #28 (`c2876521512d3dfa91c016e3a40b9674d9e47cf7`, merged 2026-08-10) — the vendor/subcontractor
  empty-multi-picklist fix from a concurrent session that superseded this session's own in-flight
  draft of the same fix; not counted among this session's landed PRs.
