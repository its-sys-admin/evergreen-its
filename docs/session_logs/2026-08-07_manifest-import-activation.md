---
type: session_log
date: 2026-08-07
status: closed
related_prs: []
workstream: field_ops
tags: [session_log, field_ops, manifest_import, activation, go_live, secrets_rotation, waived_precondition, tier_2]
---

# Session log — 2026-08-07 · Manifest import activation on the production Mac (and the precondition that was waived)

## Purpose

Discharge the three operator steps left open by
[the PR3b transport session](2026-08-07_manifest-import-transport-pr3b.md) ("Operator steps before
any of this runs") on the **production Mac**, and bring the materials-manifest import lane live.

This session landed **no commits and no code changes**. It is an operations/activation record —
the same shape as [`2026-07-26_production-host-migration-phase1.md`](2026-07-26_production-host-migration-phase1.md)
and [`2026-07-23_tenant-wipe-standup-rehearsal.md`](2026-07-23_tenant-wipe-standup-rehearsal.md).
It exists because the lane went live with **one of four documented go-live preconditions
deliberately waived**, and that fact must not live only in a chat transcript.

## Pre-flight findings

Three of these contradicted the brief the session started from. Recording them because each is an
instance of a class the house reflexes already name.

1. **The Keychain twin was MISSING on the production Mac.** The brief's step 7 read "Confirm the
   Keychain twin exists on that Mac (it's present here; that proves nothing about there)" — the
   parenthetical was right and the optimism was wrong. `security find-generic-password -s
   ITS_PORTAL_MANIFEST_TOKEN` returned MISSING. The **Worker** side was fine
   (`wrangler secret list` showed `PORTAL_MANIFEST_API_TOKEN` present), so only the Mac half of the
   pair was absent. Classic host-asymmetry: the dev Mac's state proved nothing about the daemon host.

2. **The parser eval corpus does not exist on the production Mac.** The brief's step 9 pointed at
   `~/Desktop/evergreen project/manifests`. That path is absent; this Mac's Desktop holds a single
   screenshot. A depth-5 search of `~` plus iCloud Drive found no `evergreen`/`manifest` directory
   anywhere. The corpus lives on the dev Mac. This is what forced the decision recorded below.

3. **`IndentationError` on the verify snippet** — the operator's paste carried the brief's two-space
   markdown indent into the `python -c "..."` string, so Python saw an indented first line. Not a
   code fault. Noted only because it cost time and will recur for any multi-line `-c` snippet copied
   out of an indented doc block. Prefer a heredoc or a `scripts/` one-shot for anything multi-line.

### One thing checked BEFORE acting, which changed the plan's safety

The daemon is documented as **fail-CLOSED with a CRITICAL** on a missing bearer
(`docs/references/daemon_reference.md`). With the token absent, loading the plist could plausibly
have fired a CRITICAL every 120s. Before installing, the ordering was read directly:
`field_ops/manifest_poll.py:457` runs `_polling_enabled()` and returns **before**
`_poll_inside_lock()` → `_resolve_credentials()`. So a dark load is a pure no-op regardless of
credential state. Confirmed empirically on the first fire — empty `out.log`/`err.log`, no
`.watchdog/manifest_poll.last_run` marker, `LastExitStatus = 0`.

The comment at `manifest_poll.py:458` says this out loud ("Deliberately silent: no heartbeat, no
marker, no log line") — which is why watchdog **Check C WARNs on `manifest_poll` until the operator
BOTH loads the plist AND flips the gate**. That WARN is the designed signal for a half-activated
lane, not a fault.

## Code changes

**None.** No file was edited, no commit was made, no PR was opened. Every change was to live state:
two `ITS_Config` cells, one Cloudflare Worker secret, one macOS Keychain entry, one launchd job.

## What was actually done

| Step | Action | Result |
|------|--------|--------|
| — | Verify checkout | `main` @ `2acda74`, clean tree, remote → `its-sys-admin/evergreen-its` |
| 6 | `scripts/migrations/seed_manifest_config.py` | rows `7518688683884420` (gate) + `1172819474775940` (interval=120) created |
| 7 | **Token rotated on BOTH sides** | see below |
| 8 | `install.sh load org.solutionsmith.its.manifest-poll` | loaded, `LastExitStatus = 0`, dark |
| 9 | Gate flip `false` → `true` | `field_ops.manifest_poll.polling_enabled = true` |

Migration `0060` was verified already applied (`wrangler d1 migrations list --remote` → "No
migrations to apply", from an up-to-date checkout per forensic class #2), and the Worker had
deployed at 19:19Z, after the 19:17Z secret change.

## Decisions worth recording

### 1. Rotate the token rather than recover it — Seth's call

Cloudflare cannot read a secret back, so the existing `PORTAL_MANIFEST_API_TOKEN` value was
unrecoverable from the production Mac. Three options were put to the operator: rotate both sides,
paste the value across from the dev Mac, or have the operator do it unassisted. **Rotate** was
chosen.

Rotation was safe specifically because this bearer is **privilege-separated** — it scopes only
`/api/fieldops/manifests/internal/*` (`worker/index.ts:293`, `requireManifestToken`), so no other
lane could be affected, and the manifest lane itself was still dark at the time.

`shared.keychain.set_secret` was used rather than the raw `security` CLI. Two reasons: it selects
the write form by TTY presence (the documented `/dev/tty` trap that corrupted the Box refresh token
twice), and it defaults `account` to `getpass.getuser()`, which is what `get_secret` looks up —
using `security -a itsmacbook` by hand would have worked here only by coincidence of the username
matching.

**Consequence, deliberately accepted:** the dev Mac's Keychain now holds a superseded value. See
"Operator-side actions remaining".

### 2. The parser eval was WAIVED, not passed — the load-bearing entry in this log

Go-live precondition 3 of 4 in [`docs/runbooks/material_manifest_import.md`](../runbooks/material_manifest_import.md)
(§ Go-live) reads: *"Confirm a clean run of the parser eval over the sample corpus."* The expected
result was 10/10.

**It was not run.** The corpus is not on this host (pre-flight finding 2). The operator was offered
three paths — copy the corpus over, run the eval on the dev Mac at the same commit, or skip and flip
anyway — and chose **skip and flip**, with the concern already stated.

Why this matters enough to write down, rather than being a formality:

- `tests/test_manifest_parse.py` pins the parser against **grids transcribed** from real documents,
  which is the correct CI boundary (the source files are customer data and must not enter the repo).
  `scripts/eval_manifest_parse.py`'s own docstring names the gap it closes: *"a transcription is a
  model of a document, not the document."*
- So a green CI **does not** substitute for the eval. Nothing currently in the repo exercises
  pdfplumber/openpyxl against a real manifest on this host.
- The eval is pure and credential-free (*"Reads only. Writes nothing, uploads nothing, and needs no
  credentials... Nothing here is on the daemon path."*), so running it on the dev Mac at the same
  commit would have been equally valid. That option was declined; the outstanding work is therefore
  a **run**, not a corpus migration.

The lane is live and has been processing since. It has **not** been compensated by real-world
traffic — see Live validation: every cycle in three days is all-zero, so the parser has still never
seen a real document on this host. **This is tracked in `docs/tech_debt.md`.**

### 3. A first-probe 401 was diagnosed, not assumed away

Immediately after rotation the authenticated probe returned `401`. The tempting read was
"propagation lag, retry later." Instead `requireManifestToken` (`worker/index.ts:293`) and the route
registrations (`worker/fieldops_manifests.ts:302`ff) were read and confirmed correct first; only
then was it re-probed, and it returned `200`. It was propagation. The diagnosis cost one file read
and removed the possibility of shipping a real auth defect under a "probably just lag" assumption.

## Live smoke — the control proven to bite in both directions

Against `https://safety.evergreenmirror.com/api/fieldops/manifests/internal/pending`:

| Request | Result |
|---|---|
| No bearer | `401` |
| Wrong bearer (`deadbeef`) | `401` |
| Rotated bearer | **`200` `{"manifests":[]}`** |

Keychain↔generated parity was asserted by exact string comparison in-process (the value itself and
its digest are deliberately not recorded here).

## Live validation — first cycle, then a three-day soak

First live cycle (2026-08-07 22:12Z, forced with `launchctl kickstart` rather than waiting out the
interval):

```
manifest cycle: scanned=0 filed=0 refused=0 integrity=0 rows=0 previews=0 skipped_flagged=0 errors=0
```

- `ITS_Daemon_Health` row **self-provisioned**, `Last Cycle Status = OK`.
- Observable config resolution logged all 4 keys with `(ITS_Config)` as the source — no silent
  defaults (forensic class #7).

**Soak re-verification on 2026-08-10** (the activation claims above were re-checked against live
state three days on, rather than restated from session-time observation):

| Date (local) | Live cycles |
|---|---|
| 2026-08-07 | 238 |
| 2026-08-08 | 668 |
| 2026-08-09 | 641 |
| 2026-08-10 (to 10:02) | 244 |

- **1791 total**, which matches `ITS_Daemon_Health.Total Cycles Today = 1791` exactly. Two
  independent sources agreeing validates both. (Per ARCH-3 that column is lifetime-monotonic despite
  its name — the equality is the proof, not a coincidence.)
- Marker fresh (~100s old at check time); `LastExitStatus = 0`; gate still `true`.
- **Every single cycle is all-zero.** Nothing has been uploaded, so zero manifests have been
  processed. The waived eval has *not* been retired by real-world evidence.
- `ITS_Errors`: **5 rows, all `WARN`, all `smartsheet_retry_recovered`.** Zero ERROR, zero CRITICAL.

## A correction worth preserving: the 503 was misattributed mid-session

During the session the ~26s live-cycle duration was attributed to Smartsheet 503 retries. **That was
wrong**, and the correction is more useful than the original claim:

- Only **one** 503 was ever logged to `manifest_poll.err.log`, yet *every* live cycle took ~22–26s.
- The real contrast is dark-vs-live on the same daemon: dark cycles ran **~4.9s** (`started` →
  `config resolved` → `completed`, short-circuiting at the gate); live cycles run **~22–26s**. The
  cost is the live path's ordinary Smartsheet round-trips, not transient retries.
- Peer daemons at idle, for calibration: `rfq_poll` ~26s, `estimate_poll` ~31s,
  `subcontract_poll` ~52s, `po_poll` ~75s, `portal_poll` ~108s. **`manifest_poll` is the fastest of
  its peer group.**
- Fleet-wide `smartsheet_retry_recovered` totals confirm the same at three-day scale:
  `fieldops_sync` 281, `portal_poll` 202, `publish_daemon` 79 … **`manifest_poll` 5** — second-lowest
  of the polling daemons, 5 of 719.

Effective cadence is therefore ~148s, not the nominal 120s: launchd's `StartInterval` re-arms from
process **exit**, so real cadence is 120s + runtime. That is a fleet-wide property. Against Check C's
10-minute marker window for `manifest_poll` it leaves ~4× headroom, so no watchdog tuning is needed —
but if cycle duration ever grows materially, that headroom is what shrinks.

## Verification

The canonical four-part PR-landing verify (`docs/operations/pr_merge_discipline.md`) **does not apply
to this session** — no commit, no PR, no merge SHA, therefore no main-branch CI to assert. Stating
this explicitly rather than omitting the section, so its absence is not later read as an unrecorded
failure.

The four canonical lines, filled in honestly rather than omitted or fabricated:

- pytest: **not run** — no code changed, so there is nothing to regress
- mypy: **not run** — no code changed
- ruff: **not run** — no code changed
- main-branch CI on merge commit: **N/A** — no commit, therefore no merge SHA

The verification actually performed was the live-smoke + soak evidence above:

- bearer gate: `401` / `401` / `200` (negative *and* positive — the control proven to bite in both
  directions, not merely to permit)
- config read-back: gate `true`, interval `120`, both resolved from `ITS_Config`
- 1791 clean cycles across three days, cross-validated against two independent counters
- `ITS_Errors`: zero ERROR, zero CRITICAL for this script; 5 WARN, all `smartsheet_retry_recovered`

Note that none of this is evidence about the **parser**, which is the subject of the waived
precondition — see tech debt.

## What was NOT touched

Deliberate omissions, so they read as choices rather than oversights:

- **No code, no commit, no PR.** Live state only.
- **`CLAUDE.md` "What's stubbed vs. real"** — checked and correctly left alone. The `field_ops/` row
  already says *"Gate `field_ops.manifest_poll.polling_enabled` (seeded row; read ITS_Config for live
  state)"*, which states the semantics and points at the source of truth. Editing it to say "now
  live" would have **introduced** the static-text-asserts-live-gate-state violation the house
  reflexes were written against (the 2026-07-21 fleet-wide instance).
- **The runbook's Go-live section** — left as the procedure it is. The waiver is a dated historical
  fact and belongs in this log and in tech debt, not baked into a reusable checklist.
- **Watchdog Check C window** — unchanged; ~4× headroom at the observed cadence.
- **The dev Mac** — not touched at all. Its stale token is handed off below.
- **The other five procurement/field-ops gates** — not inspected or altered.

## Sequencing context

This session discharges steps 1–3 of the PR3b log's "Operator steps before any of this runs". With
the lane live, the next thing that exercises it is a real office upload through the SPA — which is
also what will finally put the parser in front of a real document on this host.

## Operator-side actions remaining

1. **Run the parser eval — the waived precondition.** Either copy the corpus to the production Mac,
   or run it on the dev Mac:
   ```
   cd ~/its && .venv/bin/python -m scripts.eval_manifest_parse \
     --corpus "$HOME/Desktop/evergreen project/manifests"
   ```
   Expect 10/10. The lane is live now, so this is validating something already in service.

   **What "commit parity" means here — check the parser, not the SHA.** `main` has already moved
   past the activation commit (`2acda74` → `d3f5669` as of 2026-08-10), so pinning that SHA would be
   stale advice. What matters is that `field_ops/manifest_parse.py`, `field_ops/manifest_poll.py`,
   and `scripts/eval_manifest_parse.py` match what production runs — verified **byte-identical**
   across that range, and `manifest_parse.py` imports only stdlib (`re`, `dataclasses`), so it
   shares no code with the lanes that did change. Confirm with:
   ```
   git diff --stat <prod-sha>..HEAD -- field_ops/manifest_parse.py \
     field_ops/manifest_poll.py scripts/eval_manifest_parse.py   # empty == parity
   ```

2. **Refresh the dev Mac's Keychain twin.** It holds the superseded token and will `401` if
   `manifest_poll` is ever run from there. Only this lane is affected (privilege-separated bearer).

3. **Consider whether the corpus should have a durable home.** It is customer data and correctly
   not in the repo, but a go-live precondition that can only be satisfied on one specific laptop is
   fragile — this session is the proof.

4. **Land this log via a PR.** Branch protection requires it (docs included); nothing here is
   committed.

## Lessons captured

Candidates for `docs/HOUSE_REFLEXES.md` — not added there in this session, since that file is
execution-standards doctrine and the operator should confirm the wording:

- **"Confirm X exists on the target host" is a pre-flight step that fails open in practice.** Both
  host-specific artifacts this activation needed (a Keychain secret, a data corpus) were present on
  the dev Mac and absent on the production Mac. Extends forensic class #3 from *stale claims about
  code* to *stale claims about host state*.
- **A waived precondition must leave a written trace.** A skipped gate and a passed gate are
  indistinguishable three days later unless one of them is recorded. This log is that trace.
- **Diagnose the first failing probe before blaming propagation.** Reading the auth middleware cost
  one file read; assuming lag would have masked a real defect had one existed.
