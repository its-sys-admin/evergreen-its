---
type: session_log
date: 2026-07-29
status: closed
related_prs: [2, 3, 4, 5, 6, 7]
workstream: null
tags: [session_log, docs, cutover, external_send_gate, heartbeat, house_reflexes, doc_currency]
---

# 2026-07-26 → 2026-07-29 — Documentation consolidation: six themed PRs against a 172-finding audit

Session focus: land the reviewed corrections from a 12-agent documentation audit (6 auditors +
6 adversarial verifiers, run against HEAD `885d4a4`) as a series of themed PRs, re-verifying
every finding against live HEAD before editing. Ran on the production host immediately after
its stand-up, with the 15-daemon fleet live — so all work happened in a `git worktree`, never
in `~/its`.

## Commits landed

Six PRs, all squash-merged through branch protection (`test` + `portal` + `secrets`, strict,
`enforce_admins=true`). Each went `BLOCKED → CLEAN` on real CI — the gate did not exist before
this host's stand-up.

- **PR #2** (`eb6fe11c9`) — `docs(host-migration)`: all **19** findings against
  `docs/operations/host_migration_runbook.md`, 7 dangerous. The only file taken to completion.
- **PR #3** (`143ba258c`) — `docs(heartbeat)`: the external monitor is **Healthchecks.io**, and
  it has **never been armed**. 17 live surfaces.
- **PR #4** (`8c1bc25df`) — `docs(daemon-health)`: stale `ITS_Daemon_Health` sheet id
  `4529351700729732` → `6272022823784324` on 4 live surfaces.
- **PR #5** (`16e500ee8`) — `docs(runbooks)`: send-gate prose rewritten to semantics
  (HOUSE_REFLEXES §5).
- **PR #6** (`6cab535dd`) — `docs(registries)`: `DARK_UNLOADED_LABELS` (2) vs
  `SEND_DISPATCH_LABELS` (5) disambiguated; stale daemon counts.
- **PR #7** (`06e0dac74`) — `docs(invariant-1)`: the send gate covers **any external recipient**,
  not just customer-facing output.

**Zero Python files touched across all six** (`git diff --name-only -- '*.py'` → 0, checked per PR).

## CI runs

Every PR: `test` + `portal` + `secrets` green, double-triggered (push + pull_request) as expected.
`test` ~5–6½ min, `portal` ~5 min, `secrets` ~10 s.

- #2 → 30228751527 · #3 → 30229372424 · #4 → 30229702114
- #5 → 30230071683 · #6 → 30230440881 · #7 → 30230744404

Main-branch CI on each merge commit: SUCCESS (`eb6fe11c9`, `143ba258c`, `8c1bc25df`,
`16e500ee8`, `6cab535dd`, `06e0dac74`).

## Decisions made during session

- **Worktree, not `~/its`.** The fleet was live and `publish_daemon._reset_to_main()` runs
  `git checkout main` whenever HEAD is not on main, every 120 s — it would have reset a feature
  branch out from under the session mid-edit. All six PRs were authored in `~/its-docs`
  (later `~/its-close`). Rejected alternative: docs-only edits on the live tree, which the
  standing rule permits — but *committing* there is what strands the publish daemon, not editing.
- **Resolved the heartbeat cross-repo conflict in favour of code, and did NOT touch blueprint
  doctrine.** The brief required resolving it before renaming anywhere. Evidence: the client
  docstring, the `hc-ping.com` tests, and the seeded row's own Description all say
  Healthchecks.io, and `docs/session_logs/2026-05-28_f16-heartbeat-ping.md` records *why*
  (UptimeRobot's free tier gates heartbeat monitoring behind Pro and restricts commercial use).
  Blueprint doctrine is `version: 9` and lives in another repo; per CLAUDE.md the planning layer
  wins on doctrine, so it was flagged, not edited.
- **Did NOT rename the frozen literal `PLACEHOLDER_uptimerobot_heartbeat_url`.** It must stay
  byte-identical across `seed_its_config.py`, `watchdog.py`, `test_watchdog.py` and
  `test_heartbeat_client_integration.py` — the watchdog compares against it to decide whether the
  beacon is armed. Renaming it is a code change. Documented as deliberate in the
  `integration_reference.md` naming note so a future pass doesn't "fix" it.
- **Left the A3 clone org/repo as a placeholder instead of applying `draft_fix` verbatim.** The
  draft hardcoded `SolutionSmith-debug/its`, which was the *dev box's* remote at audit time; this
  host's remote is `its-sys-admin/evergreen-its`. Hardcoding either would send a future
  provisioner to the wrong repo, so the verified defect (SSH → HTTPS) was applied and
  `git remote -v` on the source host named as the URL of record. **Still needs an operator
  decision.**
- **Dropped "loaded-but-runtime-dark" from the A5 token rows** even though `draft_fix` kept it —
  it asserts a live gate state, the §5 violation PR #5 exists to remove. Declined to reintroduce
  the defect while fixing it.
- **Swept the vendor rename across all 17 live surfaces, wider than the brief's list.** A
  half-renamed vendor is precisely the drift that produced the finding.
- **Recorded "verified correct" results, not just corrections.** Three suspected-stale counts were
  counted from code and found exact; recording them prevents the next pass re-chasing them.

## Findings that turned out to be WRONG (recorded so they are not re-derived)

- **My own Stage-6 "404-kill" hypothesis — withdrawn, and now disproven by runtime.** During the
  stand-up I inferred the ten daemons that stopped at 19:23–19:28Z on 2026-07-24 were 404-killed
  against dead sandbox sheets, because the cluster aligned exactly with the §4.5 data-gap onset.
  The config disproved it (all five "frozen" daemons read `polling_enabled = false`), and **four
  days of healthy uptime on this host confirm it**: those five are loaded, exit 0, and still show
  their Jul-24 markers, because a fully-gated daemon writes no marker by design. Tech-debt §4.4
  should be re-scoped from "frozen, undiagnosed" to "intentionally dark."
- **The brief's `-U` argument-order warning does not reproduce.** Both `-w VALUE -U` and
  `-U … -w VALUE` store a 6-byte value as exactly 6 bytes on macOS 26.5.2 (tested with a
  disposable dummy). Whatever corrupted the Box token twice, argument order was not the
  mechanism — that root cause is still unidentified. The real, confirmed trap is
  `keychain.py::_has_controlling_tty()`: bare `-w` reads `/dev/tty` and ignores piped stdin.
- **`daemon_reference.md`'s "The 14 daemons that construct a `HeartbeatReporter`" is correct.**
  Flagged as suspect in PR #4; exactly 14 non-test modules do. Retracted in PR #6.
- **CLAUDE.md's watchdog inventory is exact** — 21 registered callables, 22 `_check_*` defs
  (`_check_generate_catchup` is the unregistered shared helper), 20 distinct letters A–W,
  Check C = 18 jobs. No change needed.

## Open items handed off

1. **Blueprint doctrine amendment — the one with teeth.**
   `~/its-blueprint/doctrine/vision-and-roadmap.md:16,113,188` states the "UptimeRobot heartbeat
   ping (audit F16) is **live**" — wrong vendor *and* wrong status. **Pre-Cutover Condition 4
   declares itself met partly on that ping being operational**, and it has never fired. The
   equivalent claim in the exec repo (`docs/2026-07-09_aug7_delivery_program.md` row 4) was
   corrected to PARTIAL in PR #3. Doctrine is version-gated and requires explicit approval.
   *Suggested wording:* "the external **Healthchecks.io** heartbeat ping (audit F16) is
   implemented but **not yet armed** — `system.heartbeat_url` holds its seed placeholder, so
   Condition 4 is PARTIAL pending that config change."
2. **Canonical clone URL for a fresh host.** See the A3 decision above.
3. **`its_config_dictionary.md` states "every read is fail-open to this value."** That is **wrong
   for send gates** — `po_send`/`rfq_send`/`subcontract_send` default `False` and fail safe. It is
   a GENERATED file; the fix belongs in `scripts/generate_config_dictionary.py` (code PR).
4. **Check S is a false-green.** `scripts/watchdog.py:1743` hardcodes
   `GH_MAIN_CI_REPO = "SolutionSmith-debug/its"`, so it reports the *other* repo's CI regardless
   of what happens on `evergreen-its`. Branch protection now genuinely gates merges here; Check S
   is not a backstop for it. Needs a code PR.
5. **~41 legitimate doc findings remain unapplied** (see coverage accounting below).

## Coverage accounting — the six themes are done; the 172 findings are not

```
172  findings in the audit
113  live in files these PRs touched  <- NOT all applied: only those matching each PR's theme
 59  in files never opened
```

`host_migration_runbook.md` is the **only** file taken to completion (all 19). Within the other
touched files only the themed subset was applied, so the true applied count is well below 113 —
honestly ~60–70.

Of the 59 untouched: **~13 are out of scope by rule** (4 `verify_cutover.py`, 3
`generate_config_dictionary.py`, 1 each `watchdog.py` / `system_map.py` = code; 4 `docs/reports/`
= historical), **5 are blueprint-repo**, and **~41 are legitimate remaining docs work**. Largest
clusters: `docs/doctrine_manifest.yaml` (4), `docs/runbooks/its_errors_triage.md` (3),
`context-pack/repo-overview.md` (3), `docs/ROADMAP.md` (3), `docs/troubleshooting/tree.yaml` (3).

## What was NOT touched

- **No code.** Zero Python/TS files across all six PRs, verified per PR.
- **No historical records** (rule: never correct one): `docs/session_logs/`, `docs/audits/`,
  `docs/reports/`, `docs/tech_debt_closed.md`, and the ADRs (`0002`/`0003`/`0004`) — "ships dark"
  there describes the decision as made.
- **`docs/HOUSE_REFLEXES.md`** — it quotes "ships dark" / "currently off" *as the rule being
  stated*. Editing it would delete the standard.
- **"ships DARK (fail-closed until `ITS_OPERATOR_PIN` set)"** in README / CLAUDE.md / ROADMAP —
  a structural code property, not a config-cell assertion. Not a §5 violation.
- **No `ITS_Config` value, no daemon load/unload, no tenant-boundary value.**
- **The five send dispatchers stayed unloaded throughout.**

## Verification gates

- **pytest**: 4515 passed, 51 deselected (`-m 'not integration'`) — from the stand-up's three-gate
  run on this host; unchanged, no code touched since.
- **mypy**: `Success: no issues found in 466 source files`.
- **ruff**: `All checks passed!`.
- **main-branch CI on merge commit**: SUCCESS for all six merge commits (listed above).

Per-PR docs gates, all green before push: `check_doctrine_drift --strict` (no blocking M1/M4/M7
drift) · `regen_doc_indexes --check` · `build_docs_pdfs --check` (all 22 current) ·
`lint_doc_conventions` clean for edited files.

## ⚠ Correction to the documented CI traps

The brief warned that editing `docs/enablement/` drifts its recorded sha256. **The pinned set is
much larger:** `docs/enablement/manifest.yaml` pins **22 sources**, including **10 under
`docs/references/` and `docs/troubleshooting/`** — `daemon_reference.md`,
`integration_reference.md`, `escalation_matrix.md`, `system_architecture.md`,
`documentation_index.md`, `its_config_dictionary.md`, `security_trust_model.md`, `glossary.md`,
`data_model_reference.md`, `troubleshooting_guide.md`. Editing any of them without re-recording
turns `test_docs_pdf --check` RED. **8 sha256 values were re-recorded across this series.** There
is no `--record` flag; re-recording is manual (`shasum -a 256` → paste into the manifest).

## Host state at session close (2026-07-29)

Four days of unattended operation since the stand-up:

```
fleet          15 loaded, 0 crashes, send gate INTACT (5 dispatchers unloaded)
dashboard      /healthz HTTP 200
portal_poll    3259 cycles, OK      compile_now_poll  2319 cycles, OK
fieldops_sync  2286 cycles, OK      publish_daemon    1865 cycles, OK
ITS_Errors     37 rows (33 WARN / 4 ERROR) — 0 OPEN CRITICALs
watchdog       first runs on this host: 20 INFO, 1 WARN
```

**Watchdog Check C WARNs "11 of 18 tracked scheduled jobs stale" — all 11 are accounted for,
zero unexplained:** 5 gated OFF (`polling_enabled=false`, write no marker by design), 5 send
dispatchers unloaded by operator decision, and `safety_picklist_audit`.

**`safety_picklist_audit` is a genuine (self-resolving) gap.** It is a Sunday-15:00
`StartCalendarInterval` job, and the fleet was loaded Sunday 2026-07-26 at ~16:50 — *after* its
slot. launchd does not retroactively fire for a window predating the job's load, so it missed
Jul 26 and next runs **2026-08-02**. Check C will keep WARNing on it until then. Expected, not a
fault — recorded so nobody chases it.

## Lessons captured to memory

- `its-ms-client-secret-expiry` (written at the previous session close) — expiry 2028-07-24, no
  detection path, calendar reminder is the only mitigation.
- Proposed: a memory entry for the **enablement sha-pin set being 22 sources, not 12** — it is a
  CI trap that is not derivable from the brief and bit this session immediately.
