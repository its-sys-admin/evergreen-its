---
type: session_log
date: 2026-08-20
status: closed
related_prs: [183, 178, 179]
workstream: null
tags: [publish-daemon, config-actuator, actuator-branches, production-host, verify-cutover, resend, adversarial-review, ci-gate]
---

# Session log — 2026-08-20 · Actuator orphan-branch cleanup, a read-only production recon that corrected the send-lane brief, and an adversarial audit of the publish daemon

Three threads, one session. Landed the fix for actuator branches that never get cleaned up on a
failed publish (PR #183). Ran a read-only reconnaissance pass against the production host that
overturned several premises of the operator's own brief — most notably that the send lanes were
never live, when in fact they ran loaded and enabled for ten days with one real external send.
Ran a three-lens adversarial audit of `publish_daemon.py` and found the deploy pipeline's headline
safety claim — a post-deploy health check — does not exist in the code that claims to run it. This
log covers everything **except** PRs #180/#181, the erosion-form publish fix and republish, which
already has its own log: `docs/session_logs/2026-08-19_erosion-form-publish-unblock.md`.

## Commits landed

| PR | SHA | Purpose |
|---|---|---|
| #183 | `f7cc5f2` | `shared/actuator_branches.py` — per-failure + per-cycle orphan-branch cleanup for both `publish_daemon` and `config_actuator`, extracted rather than duplicated to hold the ratchet |

## Arc 1 — PR #183: actuator branches strand on every failure path, silently

A per-request branch is removed only on the success path (`gh pr merge --squash
--delete-branch`), so any actuation that failed at commit, CI, or merge left its branch — and
usually an open PR — on GitHub with nothing to clean it up and nothing to mention it.
`_commit_test_merge` clears a stale branch for the *same* request id, but a re-publish issues a
*new* id, so debris accumulated across retries. Concretely: requests 5 and 6 for
`erosion-inspection-v1` (the 2026-08-19 session) each stranded one branch, and
`config/req-1-po_materials-purchaser` sat for **40 days** carrying no PR at all — the push landed,
`gh pr create` never did.

This was not filed as untidiness. Both stranded publish PRs from requests 5/6 **conflicted with
the definition that actually shipped** in request 7, and the conflict hunk was the form's legal
attestation clause — resolving "theirs" on either stale PR would have silently dropped a legal
certification from a live safety form. Proven empirically: merging PR #178 into real merged main
in a scratch clone produced `CONFLICT (add/add)` on exactly that hunk.

**Fix.** New `shared/actuator_branches.py`; both `publish_daemon.py` and `config_actuator.py` get
thin delegators (the `shared/heartbeat.py` pattern — canonical test-mock seams, not a
reimplementation). Per-failure cleanup runs **after** `_fail` stamps the row and raises the
CRITICAL, so the audit trail survives even if cleanup itself dies. A per-cycle sweep sits beside
the existing `_sweep_stale_rows`, before any new claim. A branch is deleted only when **both**
guards hold: the request is absent from the in-flight set, **and** its tip is older than
`2 * CI_TIMEOUT_S`. Unknown age skips (fail-closed); a failed in-flight read deletes nothing; a
`MERGED` PR is refused loudly rather than assumed to be debris.

### Non-obvious decisions

- **Extraction over duplication, forced by the ratchet, not by taste.** The first attempt
  hand-copied the branch logic into both daemons (~180 lines) and the duplicate-block ratchet
  rejected it outright: verbosity landed at 0.0683 against a 0.067 ceiling, structural erosion at
  0.3906 against 0.389 — both **worse than main's own numbers**. Extracting to
  `shared/actuator_branches.py` returned 0.0661 / 0.3870, **below** main's baseline. The extraction
  also closes a real drift the audit had already flagged: `_check_failure_detail` had diverged
  between the two files — bare job name in `publish_daemon`, an explanatory string in
  `config_actuator` — which is the reason the confusing `"test; portal; secrets"` incident message
  (2026-08-19 log) surfaced where it did.
- **Success record rides WARN, not INFO.** INFO rows are env-gated
  (`ITS_ERROR_LOG_INFO=1`, default off), and the real orphan case that motivated this fix
  (`config/req-1-po_materials-purchaser`) had no PR left to carry an explanatory comment. A
  destructive ref delete with no default-visible trace would recreate exactly the invisibility
  problem the fix exists to close.
- **Registry reconciled in the same PR.** `shared/actuator_branches.py` joins
  `NETWORK_LIB_ALLOWLIST` (it imports `subprocess`), with the allowlist entry noting it is
  narrower than either caller — no `wrangler`, no `npm`, no deploy, and it can only remove a ref
  matching `<prefix>/req-<digits>-`. `tests/test_capability_gating` caught the omission on first
  run; a two-file review would not have walked that surface.

### Verification

51 new tests. All 11 controls proven to bite under injection — each neutralised individually,
confirmed RED, then restored from byte-level `cp` backups, never `git checkout` (per
`never-git-checkout-to-revert-an-injected-violation`).

Four-part verify, measured in the worktree pre-merge and CI-confirmed:

- pytest: 5766 passed / 29 failed (all 29 are this host's conftest live-state guard — failure
  set IDENTICAL to the pre-change baseline; CI itself was green) / coverage 84.95% vs floor 84
- mypy: 0 errors / 512 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS

Four-part PR-landing verify for PR #183 (per `docs/operations/pr_merge_discipline.md`):

```
state=MERGED
mergedAt=2026-08-20T16:02:31Z
mergeCommit.oid=f7cc5f2f010e790f9967b954a1adddc81196e2ef
main-branch CI on merge commit=SUCCESS (run 32389719437)
```

Four-part verify clean.

### Outcome caveat — the sweep is unproven end-to-end

`config/req-1-po_materials-purchaser` — the live orphan the operator deliberately preserved as
the real-world test case — was deleted by a **concurrent session** at 2026-08-19T17:54:25Z
(GitHub `DeleteEvent`, actor `its-sys-admin`, 31 seconds after that session deleted its own
`docs/session-log-2026-08-19` branch while landing PR #182). The sweep in #183 ran correctly at
09:04 on 2026-08-20 and found nothing to clean up, because the one real orphan on record was
already gone. Its 11 controls are proven under synthetic injection; the production path has
**never fired against a real orphan**. Flagged as an open item below, not closed out.

## Arc 2 — production-host reconnaissance (read-only) that corrected the operator brief

A read-only pass against the production host (`itss-macbook-pro`, Tailscale) to answer the
operator's send-lane question. The findings overturned several premises of the brief itself.

**All 22 shipped plists were already loaded, including all five send dispatchers.** VC-02 passes
because `DARK_UNLOADED_LABELS` is deliberately `frozenset()` — the operator activated every send
lane on 2026-08-10. The brief's premise ("halt if any send plist exists unloaded") was stale
before the recon started.

**Send gates all read false; all five modules fail-closed by declared default**
(`DEFAULT_POLLING_ENABLED=False`). `send_poll_core.py:458` short-circuits before lock, heartbeat,
or marker writes, which is why their daemon-health markers show frozen — not because the daemons
are down, but because a gate-false cycle returns before touching any of them.

**The gates were flipped false ~2026-08-17 08:47 PDT** — markers freeze at that timestamp while
`runs=` counters kept climbing after it — meaning the five send lanes were **loaded and enabled
from 2026-08-07 through 2026-08-17**, ten days, not "never live" as the brief assumed. Exactly
**one** real external send occurred in that window: a WSR row, `Sent At` 2026-08-07, `Approved By`
`daniels@evergreenrenewables.com`. Every other row in that window sat `PENDING`. The External Send
Gate held throughout — this had simply never been documented anywhere the operator could see it.

**Timezone: the three calendar jobs are stuck on Eastern reckoning.** The system timezone is
`America/Los_Angeles` (changed 2026-08-06), but the three calendar-triggered plists were installed
2026-07-26 while the host was Eastern and have `runs=3` each — never reloaded since the TZ change
— so launchd still fires them by the Eastern wall clock: 14:02 / 14:34 / 15:00 **Eastern**, not
Pacific. Running `install.sh load` for Phase 1 would silently shift the Friday compile from
18:00Z to 21:00Z; the operator chose **not** to reload rather than accept a surprise 3-hour
schedule shift mid-recon. Those same three plists also carry `RunAtLoad=false`, contradicting a
blanket "every plist has `RunAtLoad=true`" claim in the prior brief.

**`verify_cutover`: 7 passed / 3 failed.**
- VC-03 — three rows still point at the sandbox: `worker_base_url` is still
  `safety.evergreenmirror.com`; `weekly_send`/`progress_send` `polling_enabled` expected `true`,
  read `false`.
- VC-04 — same five send daemons, stale by the same measure.
- VC-10 — 16 missing approver USER shares across 4 workspaces.
- VC-09 (the external dead-man's switch) is genuinely armed — confirmed, not assumed.

**VC-06 passes shape-only and is actively misleading.** Resend returns HTTP 403 — *"You can only
send testing emails to your own email address (seths@evergreenmirror.com)"* — meaning **CRITICAL
alert emails are currently undeliverable**. Three such delivery failures were logged live during
this very recon. `DEFAULT_FROM` is hardcoded to `onboarding@resend.dev` in
`shared/resend_client.py`, with a comment claiming it "accepts any recipient"; the live 403
disproves the comment outright — Op Stds §52, narrated-not-enforced, caught in the wild rather
than in review.

**Dashboard state was correctly left alone.** It was already loaded, PIN already set, an origin
patch already applied. The repo's plist template ships that value blank, so an `install.sh load`
would have **erased** the live origin patch — the correct action was to not touch it.
`tailscale serve` has no serve config on the host, so the dashboard remains localhost-only as
designed.

**`~/its/.venv` on the production host is missing `radon`.** `tests/test_code_quality_metrics.py`
fails to *import*, which aborts pytest **collection** for the whole suite (exit 2, zero tests
run) — plus 3 further failures in `test_quality_ratchet.py` that depend on it. Four visible
failures, one root cause. Deliberately **not** fixed in place: mutating the live daemon venv
violates `docs/operations/worktree_discipline.md`; this is recorded as an open item, not patched
around.

## Arc 3 — adversarial audit of `publish_daemon.py` (all findings still open)

Ran a three-lens adversarial workflow (attacker / auditor / skeptic, each verdict subjected to
refutation) against the publish pipeline surfaced by Arc 1. All three lenses' *initial* verdicts
were refuted and corrected before the findings below were accepted — recorded because the refutation
step is what caught them, not the first pass.

**Headline finding: the post-deploy health check does not exist.** The module docstring for
`publish_daemon.py` promises a "post-deploy health check (GET the live form)". The only post-deploy
action `_deploy_land_health` actually performs is a `get_publish_pending(limit=1)` liveness ping —
it never fetches the published form. `current_form_code` appears exactly **once** in the function
(the signature) and is never read. `tests/test_publish_daemon.py:58` mocks `_deploy_land_health`
wholesale, so no test in the suite can catch the gap between the promised check and the real one.

**`_pending_migrations` is fail-open.** It substring-matches against `wrangler`'s human-formatted
table stdout. A wrapped or truncated migration filename resolves to "none pending," which lets a
deploy proceed **ahead of** unapplied migrations — forensic class #2, reproduced inside the exact
function meant to guard against it.

**No subprocess on the deploy path is timeout-bounded.** A hung `wrangler` invocation blocks the
daemon indefinitely with no escalation.

**`_reset_to_main`'s `git clean -fd safety_portal/forms` only removes untracked files.** A
*modified but tracked* `forms/*.json` survives the reset and rides into the next publish commit
unnoticed.

**The cancellation guard's docstring misstates its own trigger, in both modules.** `ci.yml`'s
concurrency group means a `pull_request` run structurally *cannot* cancel a `push` run on the same
ref — the real 2026-08-19 event was push-vs-push, and the docstring's "routinely" framing is false.
Still **undiagnosed**: two push events landed on the *same* sha, one second apart, though
`_commit_test_merge` issues exactly one push per request. Handed off unresolved.

**Correction to the 2026-08-19 record, surfaced by this audit.** Request 6's own `test` job
genuinely **failed** in both surviving CI runs — the cancellation guard never killed a healthy
publish. PR #180's fix #1 (per-section `min_rows` counting) was the real fix for that incident;
fix #2 (the cancellation guard) only makes the failure *reason* reported truthfully, and has never
actually fired in production. `docs/session_logs/2026-08-19_erosion-form-publish-unblock.md`
should be read with this correction in mind — it is not amended there.

None of these five findings were fixed this session. All remain open.

## Process lessons this session surfaced

- **A CI wait-loop condition of "zero checks with status != COMPLETED" is trivially true for an
  empty rollup.** It reported "CI COMPLETE" during the window between `gh pr update-branch` and CI
  registration. Fix needs a minimum check-count floor, not just a status filter — not yet applied
  anywhere in the codebase, recorded as a finding.
- **A bite-proof harness can itself pass vacuously.** One injection case named a test that had
  since been renamed; a missing pytest selector exits non-zero, which reads exactly like "the
  control bit." The PR #183 harness now pre-checks that each named test is green *before*
  injecting, closing this specific instance — but the class (a harness whose own selectors can go
  stale) is worth watching elsewhere.
- **`block-dangerous-git.sh` fired three times on text that would never execute** — comment prose
  twice, a string literal once. The first instinct was a `_DELETE_FLAG` indirection to dodge the
  grep; that was reverted, because obfuscating production code to satisfy a dev-only guard is the
  wrong trade. The hook stayed as-is; the code was written around it honestly instead.
- **`gh pr merge --squash --delete-branch` can succeed on the merge and fail the delete step
  separately.** It failed with `fatal: 'main' is already used by worktree` on this host; the merge
  itself succeeded, so the branch ref had to be removed after the fact via `gh api -X DELETE`.
- **The live tree does not auto-pull merged code.** `_unstrand_if_needed` skips its pull when
  already on `main`; the actual pull lives inside `_actuate`, gated on there being queued work.
  A post-merge `git pull` in `~/its` between daemon cycles is required for any fix to go live —
  it does not happen automatically just because main advanced.

## Operator actions taken this session

- Closed PR #178 and PR #179 and deleted their branches, after verifying merged HEAD is a strict
  superset of both (zero lines unique to either stranded PR; both lacked the legal attestation
  that shipped in request 7).
- Merged PR #183.

## Open items / next session

- **PR #183's cleanup path is unproven against a real orphan** — see the Arc 1 outcome caveat.
  The next actual actuator failure is the first live test of the fix.
- **Resend delivery is broken for CRITICAL alerts** (VC-06 shape-only pass, live 403). This is a
  silent-alerting failure mode on top of the pre-existing `project_alerting-path-broken` item —
  needs its own fix, not just a VC-06 tightening.
- **Two calendar plists are running on stale Eastern time** post-TZ-change; a deliberate
  `install.sh load` is needed to correct them, understood to shift the Friday compile by 3 hours
  when it happens.
- **VC-03 sandbox rows + VC-04 stale send daemons + VC-10's 16 missing approver shares** are all
  open cutover gaps on the production host, independent of anything landed this session.
- **`radon` missing from the production `.venv`** — blocks the full pytest suite from collecting
  on that host. Needs an operator-run `pip install` on the live venv (out of scope for a worktree
  fix per `worktree_discipline.md`).
- **All five `publish_daemon.py` adversarial-audit findings are open**: the missing post-deploy
  health check, the fail-open migration-name match, the unbounded deploy subprocesses, the
  tracked-file gap in `_reset_to_main`'s clean, and the undiagnosed double-push. None fixed this
  session.

## Cross-references

- `docs/session_logs/2026-08-19_erosion-form-publish-unblock.md` — PRs #180/#181, the erosion-form
  publish fix and republish that this log deliberately does not duplicate.
- `docs/operations/pr_merge_discipline.md` — canonical four-part PR-landing verify.
- `docs/operations/worktree_discipline.md` — why the production `.venv` gap was left unpatched.
- `docs/tech_debt.md` — `project_alerting-path-broken` (Resend delivery), publish-daemon findings
  candidate for a new entry.
- HOUSE_REFLEXES §2 (prove-the-control-bites) and §3 (git/worktree discipline) — both directly
  exercised in Arc 1 and the process-lessons section.
