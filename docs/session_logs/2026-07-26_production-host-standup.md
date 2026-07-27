---
type: session_log
date: 2026-07-26
status: closed
related_prs: []
workstream: infrastructure
tags: [session_log, infrastructure, cutover, launchd, keychain, box_oauth, branch_protection, send_gate]
---

# 2026-07-25 → 2026-07-26 — ITS production host stand-up (Florida MacBook), fleet live at 15 daemons

Session focus: provision the permanent production host from a clean macOS install, migrate
secrets and state off the old Mac, prove the old host is out of the business, and bring up the
15-daemon fleet with the five external-send dispatchers deliberately dark — then produce
remotely-readable evidence.

**No code commits landed.** This session changed no `.py`/`.ts` source. Its output is host
state, plus this log and the untracked evidence file `~/its_standup_evidence.md`. It is logged
anyway because the session produced a large number of non-obvious decisions and three
corrections to briefed fact — exactly the class of context `docs/session_logs/README.md` exists
to preserve.

## Commits landed

None. The only repository-side change is this log.

## CI runs

- `its-sys-admin/evergreen-its` run **30219443153** (`ci.yml`, `workflow_dispatch` on `main`) —
  **success**, `test` + `portal` + `secrets` all green, `test` job 6m43s. This was the
  **first-ever** Actions run on the mirror repo; branch protection was applied only after it
  proved green.

## Decisions made during session

- **Cloned `~/its` and `~/its-blueprint` rather than stopping.** The brief asserted the repo was
  already cloned; both were absent. Cloning is a reversible read-only fetch of public repos and
  every later stage depends on it. Rejected alternative: halting for operator confirmation —
  would have blocked all verification work for no safety gain.
- **Pinned the venv interpreter explicitly** (`/opt/homebrew/bin/python3.13 -m venv`) instead of
  `python3 -m venv`. System `python3` is 3.9.6 here and `pyproject` floors at `>=3.12`, so the
  briefed command would have built a venv that could not run the code.
- **Installed `gh` via Homebrew despite `gh` already being present.** See defect below — the
  existing copy was at `~/.local/bin`, invisible to launchd. Version was already correct; only
  the *location* was wrong.
- **Host-specific git identity** (`its-sys-admin <its@evergreenrenewables.com>`) over matching
  prior commit history (`SolutionSmith-debug <seth@…>`). Operator choice. Rationale: git history
  is the one surface where ITS *can* record which host performed an action, and the
  "nothing records which host wrote this" gap made the Stage 6 evidence awkward. Identity was
  UNSET before this; `publish-daemon` commits would have failed under launchd with no TTY.
- **Verified secrets by sha256 FINGERPRINT, not length.** Nine of the twenty are exactly 64
  chars, so a length check cannot detect a transposition — and the failure modes are asymmetric:
  a swapped bearer fails closed on one lane, but a swapped `ITS_PORTAL_HMAC_SECRET` makes every
  portal submission verify `False`, is one-shot-flagged, never filed, and looks like a portal
  fault rather than a secret fault. Result: 20/20 exact, 0 mismatches.
- **Withheld the `ITS_OPERATOR_PIN` digest from all output.** A 6-digit PIN has only 10^6
  candidates, so an 8-hex sha256 prefix is brute-forceable in seconds — publishing the
  fingerprint would publish the PIN. Compared internally; only a DIFFERS/MATCHES-OLD verdict was
  emitted. The old PIN was the literal `123456` (`fp 8d969eef`), so MATCHES-OLD is the error
  condition, not DIFFERS.
- **Applied branch protection only AFTER CI proved green.** Rejected alternative: applying it
  first. `ci.yml` requires zero repository secrets (verified: 0 configured, none referenced), so
  a deadlock was unlikely — but a red first run behind three required checks would have blocked
  every `publish-daemon` merge with no operator present to diagnose it.
- **Left `required_pull_request_reviews` NULL.** `publish-daemon` squash-merges unattended;
  requiring human review would deadlock it exactly as absent checks did. `strict=true` is safe
  because `publish_daemon._wait_for_ci` already handles `mergeStateStatus == "BEHIND"` via
  `gh pr update-branch` — a state that only occurs when strict is on, so the daemon was written
  expecting this setting.
- **Ran Stage 8 (state copy) BEFORE Stage 7 (Box OAuth)**, inverting the brief. Cleaner: it
  removes any chance of the rsync re-adding `box_oauth_last_refresh.json` after the OAuth run,
  which is the very thing the brief's Stage 8 has to `rm` defensively.
- **Refused to load any send dispatcher** despite a later blanket "full permissions, stepping
  away" grant. Loading one is a FIXED high-capability-class External-Send-Gate action (Op Stds
  §44) reserved to Seth personally; a blanket grant is not a deliberate decision to begin
  transmitting customer email. Pre-flight dry-run asserted the exclusion logic (exactly 5
  skipped, exactly 15 to load, each dispatcher individually confirmed absent) *before* anything
  was loaded.
- **Declined to run a recommended `gh auth logout`** — twice. Two inbound reports claimed the gh
  token was invalid and branch protection absent. Both were false against live state
  (`gh api user` → `its-sys-admin`, protection object returned in full). The proposed remediation
  would have destroyed a working credential requiring an interactive browser to restore, while
  the operator was travelling — manufacturing the outage it claimed to fix.

## Defects found and fixed

- **`gh` was invisible to launchd.** Every plist bakes
  `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`; `gh` was at
  `~/.local/bin/gh`, not on it, and `/opt/homebrew` did not yet exist. Effects:
  `safety_reports/publish_daemon.py:368` and `po_materials/config_actuator.py:291` invoke bare
  `["gh", ...]` with `check=True` → `FileNotFoundError` under launchd;
  `scripts/watchdog.py:1797` `shutil.which("gh")` → `None` → Check S silently INFO-skips. Both
  daemons are in the load-15. Fixed by installing gh via Homebrew into `/opt/homebrew/bin`;
  verified by resolving `gh` under the exact plist PATH string with `env -i`. The brief
  anticipated a PATH problem only on Intel hosts; this bit on arm64 for a different reason.
- **Stage 12's documented procedure does not work.** The brief says apply
  `ITS_DASH_ALLOWED_ORIGINS` to the installed plist then `launchctl kickstart -k`, explicitly
  "not load". `kickstart` restarts the PROCESS but launchd retains the job definition cached at
  bootstrap, so the edited plist is ignored. Verified directly: after `kickstart`,
  `launchctl print` still showed `ITS_DASH_ALLOWED_ORIGINS => ` (empty) while the plist on disk
  held the correct value. Following the brief exactly leaves the allowlist EMPTY — producing
  precisely the silent failure it warns about (read panels render; every PIN-gated ACT POST is
  refused cross-origin). **Correct procedure:** `launchctl bootout` immediately followed by
  `launchctl bootstrap` of the EDITED plist. This is a reload, not the teardown that
  `install.sh unload` exists for; and NOT `install.sh load`, which re-renders from template and
  wipes the origin again.

## Corrections to briefed fact (verified against live HEAD)

- **"Every plist carries `RunAtLoad=true`" is false.** Four are `false` — `picklist-audit`,
  `progress-generate`, `watchdog`, `weekly-generate` (calendar-driven; `true` would misfire a
  Friday compile on a Tuesday boot). The safety rule still holds where it matters: **all five
  send dispatchers are `RunAtLoad=true`.**
- **Stage 10's `grep -n "__"` recipe yields 3 false positives** — it matches
  `__POLL_INTERVAL_SECONDS__` inside XML *comments*. A comment-aware check plus `plutil -lint`
  gives 20/20 clean.
- **The `-U` argument-order warning does not reproduce.** The brief states `-U` must precede
  `-w VALUE` "or it is swallowed as part of the password"; `scripts/setup_box_oauth.py`'s own
  docstring (lines 23–24) uses the opposite order. Both forms were tested empirically on macOS
  26.5.2 with a disposable dummy: each stores a 6-byte value as exactly 6 bytes. Neither
  corrupts. **Whatever corrupted the Box token twice, argument order was not the mechanism — that
  root cause remains unidentified.** The trap that IS real, and is confirmed in
  `shared/keychain.py::_has_controlling_tty()`: bare `-w` reads `/dev/tty` and IGNORES piped
  stdin, so never pipe into it.
- **`autorestart` is unsupported on this hardware** — absent from `pmset -g cap`; Apple Silicon
  laptops do not expose it, and `sudo pmset -a … autorestart 1` is silently ignored. Power-cut
  resilience here comes from battery + auto-login + `disablesleep`.
- **Tech-debt item §4.4 ("five daemons frozen since 2026-07-24 ~15:2x, undiagnosed") is
  mis-scoped.** All five read `polling_enabled = false` in ITS_Config. Per `TRACKED_JOBS`' own
  comment, a loaded daemon whose gates are all false is "a pre-lock no-op and writes NO marker —
  an intentional dark state, not a fault." They are switched off, not broken; they are loaded and
  healthy on this host (exit 0, live PIDs) and correctly write nothing. Recommend re-scoping the
  entry from "frozen, undiagnosed" to "intentionally dark."
  - *A mid-session hypothesis that these were 404-killed by dead sandbox sheets — inferred from
    the marker cluster aligning with the §4.5 gap onset at 19:28Z — was **withdrawn** once the
    config was read. Recorded here because the timestamp correlation is genuinely suggestive and
    a future session may re-derive it; the config disproves it.*

## Open items handed off

1. **`ITS_MS_CLIENT_SECRET` expires 2028-07-24** (minted 2026-07-24, 2-year validity; supplied by
   operator). There is a repair path but **no detection path** — nothing checks this date and the
   watchdog cannot surface it. On expiry every Graph send lane fails at once, silently, while
   `system.heartbeat_url` remains a placeholder. Mitigation is calendar-only: reminder ~2028-06-24.
2. **`progress_reports.progress_send.polling_enabled = 'true'`** with a production
   `from_mailbox` (`its@evergreenrenewables.com`). `progress_send_poll.py:76` sets
   `DEFAULT_POLLING_ENABLED = True`, so the brief's concern was a *missing* row failing open —
   the reality is worse: the row exists and is **explicitly armed**. `weekly-send` has two
   independent protections (plist unloaded AND row `false`); `progress-send` has exactly ONE.
   Any `install.sh load` sweep that omits the exclusion list transmits customer email within
   seconds. **Suggested tech-debt wording:** *"progress_send is armed (`polling_enabled=true`)
   while its plist is dark — single-layer send protection; consider setting the row `false` so
   the gate is defence-in-depth like weekly_send."*
3. **Tailscale Serve is not enabled on the tailnet** — `tailscale serve --bg 8484` returns
   "Serve is not enabled on your tailnet." An admin-console action. Until then the operator
   dashboard is reachable only at `127.0.0.1:8484`; the origin allowlist is already staged and
   verified live in the process environment, so it should work the moment Serve is on.
   **Dashboard remote acceptance (`/healthz` + panels + one PIN-gated ACT POST from a phone on
   cellular) therefore remains UNRUN.** Deferred by the operator as not needed this week.
4. **Check S remains a false-green** — `scripts/watchdog.py:1743` hardcodes
   `GH_MAIN_CI_REPO = "SolutionSmith-debug/its"`, so it reports the OTHER repo's CI regardless of
   what happens on `evergreen-its`. Branch protection now genuinely gates merges here; Check S is
   not a backstop for it. Still needs a PR.
5. **`JOB-000030 "Production test"` is test residue** on the production tenant — present in
   `ITS_Active_Jobs` and therefore in the portal's D1 job dropdown.
6. **Cloudflare production infrastructure is owned by a personal Google account**
   (`sethsmithusmc@gmail.com`, account `a1d033090d474174c43fd3d0e6f7a0ab`) — the
   `its-safety-portal` Worker and `its-safety-portal-db` D1, which every workstream's submissions
   pass through. `publish-daemon` runs `wrangler deploy` against it unattended each cycle.
   Explicitly confirmed by the operator as intended; recorded as an accepted risk, not an
   oversight.
7. **`evergreen-its` is a PUBLIC repo**, while `shared/sheet_ids.py`'s own docstring states the
   practice is "each customer gets a **private** repo forked from the blueprint." Flagged for
   awareness; not changed (operator decision is migrate as-is).

## What was NOT touched

- **No tenant-boundary value was changed.** The split is inherited deliberately and was recorded
  read-only: `sheet_ids` → production; all three `safety_reports.portal.worker_base_url` rows →
  `https://safety.evergreenmirror.com` (the exact 3 rows the `phase1-hybrid` profile exempts);
  `intake.mailbox` and `intake.allowed_senders` → mirror; both send `from_mailbox` rows →
  production. Net effect worth knowing: a send lane would transmit FROM a production mailbox
  about data pulled from the MIRROR portal.
- **No send dispatcher was loaded.** `po-send`, `progress-send`, `rfq-send`, `subcontract-send`,
  `weekly-send` remain unloaded and were re-verified absent after every load step.
- **No `polling_enabled` row was flipped**, including the armed `progress_send` row — that is a
  §44 send-gate decision.
- **No Python source was edited.** Per the standing rule, the live `~/its` tree is executed from
  disk every ~60s and reset to `main` by `publish-daemon`; only this docs file was added.
- **`~/.local/bin/gh`** (38 MB stale duplicate) was left in place. Harmless today — Homebrew's
  copy wins in both the interactive and launchd PATH — but a version-drift hazard worth deleting.
- **`heartbeat_row_ids.json` was deliberately NOT copied**, so this host builds its own row cache
  and `Total Cycles Today` resets low and climbs. That is the only positive evidence the new host
  owns the `ITS_Daemon_Health` rows; VC-04 goes green whether takeover happened, never happened,
  or both hosts are writing.

## Acceptance summary

15 daemons loaded, all exit 0, send gate intact. `verify_cutover --profile phase1-hybrid`:
**6 passed / 4 failed**, every failure correct-by-decision — VC-02 (fully-dark, 3 dispatchers
"missing"), **VC-03** (`weekly_send.polling_enabled=false` where the gate wants `true` — the same
fully-dark decision, and NOT listed as an expected failure in the brief), VC-09 (heartbeat
placeholder), VC-10 (production approver shares unseeded; 7 approvers × 4 workspaces).

> **VC-08 caveat.** It fails with `FileNotFoundError: 'npx'` when `verify_cutover` is invoked from
> a shell lacking `/opt/homebrew/bin`. That is an artifact of the invoking environment, not a host
> defect — `npx` resolves correctly under the real launchd PATH. Re-run with brew on PATH: PASS.

Live round trip (Evidence G) **PASSED**: `JOB-000030` / `jha-v3` submitted 2026-07-27 00:24:31Z,
filed 00:26:12Z (~101s), `box_verified=1`, `box_file_id=2370152970615`, week folder created.
Operator then deleted the artifacts as cleanup, so that Box file now 404s — expected, not a defect.

> **Verification lesson.** An empty `pending` queue and an empty Box folder BOTH looked like
> failure and were actually success. `GET /api/internal/pending` returns only `box_verified=0`
> rows, so a drained queue is indistinguishable from one that never received anything, and
> `portal_poll.out.log` stays 0 bytes because it logs nothing on empty cycles. To verify a round
> trip, query D1 directly:
> `wrangler d1 execute its-safety-portal-db --remote --json --command "SELECT submission_uuid, job_id, form_code, box_verified, filed_at, box_file_id FROM submissions ORDER BY created_at DESC LIMIT 10;"`

**Old-host takeover boundary: 2026-07-25 ~15:52Z** — the last `safety_portal_poll` marker on the
old Mac — *not* the 2026-07-26T19:46:50Z teardown timestamp. `install.sh unload` printed
`not loaded:` for all 20 labels, which per `cmd_unload` means the plists were already absent and
`bootout` found no such label: the teardown command CONFIRMED the state rather than creating it.

## Verification gates

Run on the new production host against `885d4a4`, in the freshly built `.venv` (Python 3.13.14):

- **pytest**: 4515 passed, 51 deselected (the `-m 'not integration'` default — those need live
  Smartsheet credentials and are excluded in CI too). Exit 0.
- **mypy**: `Success: no issues found in 466 source files`. Exit 0.
- **ruff**: `All checks passed!`. Exit 0.
- **main-branch CI**: run
  [30219443153](https://github.com/its-sys-admin/evergreen-its/actions/runs/30219443153) —
  **SUCCESS** (`test` + `portal` + `secrets`), `workflow_dispatch` on `main`. Note this is a
  dispatch run, not a merge commit: it was triggered deliberately to prove CI green on the
  mirror repo *before* branch protection was applied. **The merge-commit CI for this log's own
  PR is the first run to exercise the new protection end-to-end and is pending at time of
  writing.**

## Lessons captured to memory

- `its-ms-client-secret-expiry` (new) — the 2028-07-24 expiry, why no detection path exists, and
  that a calendar reminder is the only real mitigation. Indexed in `MEMORY.md`.

Full machine-level evidence, including the fingerprint table, the tenant-boundary table and every
correction above, is at `~/its_standup_evidence.md` — deliberately outside `~/its` so
`publish-daemon`'s reset cycle cannot destroy it.
