---
type: session_log
status: active
workstream: null
tags: [tech_debt, triage, ci, testing, docs]
---

# 2026-08-12 — Tech-debt triage against live HEAD, then building the actionable half

Six PRs (#105 #106 #107 #108 #110 #111), all four-part verified. 15 entries resolved;
`docs/tech_debt.md` stood at **130 open** when the last of them landed. (Exact start/end counts
drift with concurrent sessions — three other PR arcs landed the same day.)

## What the session was

A full triage of every entry in `docs/tech_debt.md` against live HEAD — classify each as
already-fixed / dormant / actionable-now / needs-Seth — followed by building the actionable subset.
Triage ran as a 27-agent fan-out: 15 agents over contiguous slices of the file, then an independent
refute-first verifier on every "already fixed" candidate.

## The three findings that mattered

### 1. The backlog was three times the size anyone thought

The brief said ~46 open entries. The real number was **138 open of 146 sections**. The count came
from `grep -c '^## \[OPEN'`, which only matches entries carrying `[OPEN …]` at the **start** of the
header — but most entries carry it at the **end**. Severity split was 4 high / 13 medium / 34 low
and **91 with no severity tag at all** (the brief estimated 12).

**Any future count of this file must match both header forms.** A brief, a script, or an agent that
matches only the prefix form will silently under-report by ~3×.

### 2. Refute-first killed 10 of 12 "already fixed" claims

Twelve entries looked stale to the triage pass. An independent verifier, instructed to *refute* each
and to default to "refuted" under uncertainty, killed ten. Only two were genuinely stale.

That ratio is the argument for the pattern. The failure mode it caught repeatedly: an entry bundles
3–9 sub-items, one is fixed, and a reader generalises. The nine-defect materials cluster was the
sharpest case — 8 of 9 genuinely closed, but **A4's second clause survives verbatim**, and closing
the parent would have dropped it. It is now its own entry.

### 3. One stale-branch docs PR produced two false-open entries

This is the root cause of the "several entries are stale" intuition, and it is a session-close
process hazard:

- PR #66 (`add93dd`) closed the nine-defect materials cluster and wrote a `[RESOLVED]` annotation.
- **46 minutes later PR #70 (`65278fd`) — a docs session-close PR built on a stale branch — replaced
  that header with `[OPEN 2026-08-10, high]`.** Its body never mentions the entry. Verified by diff
  direction: one deletion, zero additions.
- The *same* PR **added** an entry describing a `regen_doc_indexes` bug that PR #67 (`201f281`) had
  fixed 46 minutes earlier. `git merge-base --is-ancestor 201f281 65278fd` confirms the ordering.

A high-severity entry read OPEN for a day purely because bookkeeping was reverted. **A session-close
docs PR must be rebased immediately before it is written, not merely before it is merged** — the
window between drafting and merging is exactly where the clobber happens.

## The two waived go-live evals — both runnable here, so both were run

Two `high` entries share a shape: a capability went live with its go-live precondition unmet, each
recording that its corpus "lives only on the dev Mac." **This host IS that dev Mac** — zero
`org.solutionsmith.its` launchd jobs, and local HEAD at exact `origin/main` parity, which is the
manifest entry's own blessed path. Both corpora are present at `~/Desktop/evergreen project/`.

Both evals are read-only and credential-free, so running them is evidence-gathering, not a gate
action:

- **Manifest parser eval — `10/10 documents produced importable rows`, exit 0.** Exactly the bar
  `material_manifest_import.md` names. One flag: the Steger master BOM reports `qty_unparseable: 2`.
- **Estimate ladder eval — 82 files, ZERO Tier-1 extractions.** 75 bottomed out at `needs_review`
  with `line_count: 0`; 2 refused as invoices; 5 refused by §34 screening.

The second result reframes its entry. Read honestly: the run was `tier2=off ocr=off`, and much of
that corpus is scanned Apricus/Platt PDFs that deterministic text extraction cannot read *by
design* — so this is **not** evidence the parser is broken. It is evidence that `tier1_enabled` is
**live and inert**: everything falls through to the manual Tier-3 floor. Safe failure mode, no
delivered capability, and still no snapshotted baseline (`estimate_corpus_expectations.json` holds
only its `_README`).

**Still Seth's decision.** The eval closes the evidence gap; the risk acceptance is unchanged.

## Decisions taken during execution

**The rfq fix was NOT applied as the entry specified.** The entry proposed a format regex
`^RFQ-\d{4}\.\d{3}\.\d+-\d{3}$`. That would have been wrong twice: `worker/rfq.ts:713` emits the
site segment **only when `sitePhase > 0`** and falls back to `RFQ-{job_no}-{NNN}` otherwise — a
shape that is permanent on pre-0070 drafts — so the regex would red-light on legitimate data. And
`rfq_poll` never composes this number, so validating its *format* asserts a contract the Python side
does not own. Replaced with exact pass-through, parametrized over both legitimate shapes.

**picklist-sync (DASH-11) took the entry's second option — document, don't enroll.** Adding it is
not a one-line allowlist change: it has no `*.poll_interval_seconds` row at all (hourly by plist
literal) and is correspondingly absent from `install.sh`'s table, which is *why* the existing parity
test passes. The registries agree; this is a coverage boundary, not drift. Documented in place and
pinned both halves, so a future config-driven plist red-lights instead of the note going stale.

**`test_publish_daemon` closed on the observable, not a root-cause narrative.** The entry's "29
local failures" does not reproduce at HEAD: 53/53 pass in the live `~/its` checkout on the host it
names, and 53/53 from a worktree. The DIAGNOSED note's prescribed fix is already present as the
`fence_state` fixture. Closed with that evidence; if it recurs, reopen with the failing output.

**`wrangler` local-D1 refiled, not closed.** It names no action anyone can take, which is
`tech_debt.md`'s own preamble test for belonging in `docs/references/platform_constraints.md`.

## Two recorded claims corrected

- **The BOM "zero overlap" is wrong.** The committed tooling reports **5 of 218** as `in_catalogue`
  — and the *original* scratchpad script reported the same 5, so the reproduction is faithful, not a
  regression. All five are token false positives (`SERRATED FLANGE HEX NUT 300 SERIES SS` matched
  `Series 7 TR1` on `SERIES`). "Zero overlap" was a human verdict on top of five spurious hits. The
  docstring now says so and flags `in_catalogue` as a candidate, not a verdict. Auto-memory
  `project_bom-catalogue-reconciliation-2026-08-11.md` still carries the wrong phrasing.
- **The VC-01 entry's own count was stale.** It claimed the real figure was 21; measured at HEAD it
  is **22**. That is the entry's own argument — a hand-counted total in prose cannot hold — so the
  fix is a test that parses the number out of the docstring, not a third hand-count.

## Controls proven, not merely green

Every control was injected against before it was trusted, then reverted:

| Injection | Result |
|---|---|
| Drop the migration-0070 site segment in the mirror hand-off | RED — `'RFQ-2026.001-007' == 'RFQ-2026.001.1-007'` |
| …same injection, with the **OLD** prefix assertion restored | **2 passed** — provably blind to it |
| Make `state_io.with_path_lock` a no-op | RED — `lock allowed 2 concurrent writers` |
| Neuter the BOM repo-write guard / partial-seed guard | RED — partial-seed emitted **1 row instead of 28** |
| Revert VC-01 docstring to 18 | RED — new docstring pin |
| Delete `subcontract_send.from_mailbox`; enroll picklist-sync | RED — both, plus 2 pre-existing tests |
| Revert `doctrine_manifest` count to 6 | RED — new `count == len(slugs)` parity test |

Flake check on the rewritten lock test: 30 unloaded runs + 12 under 8 concurrent CPU spinners, 0
failures.

## What was deliberately left

**31 of the original 43 actionable-now items remain unbuilt.** Roughly 13 small, ~16 medium, one
full re-audit. Several mediums are trust-boundary work where adversarial review is
definition-of-done and should get a dedicated pass rather than a session tail: `GET /api/recent`
ownership scoping, `fieldops_sync` mirror resilience, the manifest commit replay finalize-gap.

The 49 needs-Seth and 52 dormant entries were not touched by design.

## Process note for the next session

**Main took ~15 commits from a parallel session during this one**, and every merge re-`behind`s each
open PR. One update-branch loop re-triggered CI faster than it could finish — the busy-main race
`reference_github-api-flaky-merge-mechanics` warns about. Batching the remaining work into two or
three larger PRs will land materially faster than one PR per cluster.

Also seen: the `secrets` CI job infra-failed twice on `curl: (22) … 503` fetching the pinned
gitleaks binary. Not a finding — re-run cleared it. Worth knowing before anyone chases a red
`secrets` job as a leak.

## Verification

- pytest: 5447 passed / 4 skipped / 58 deselected
- mypy: 0 errors / 502 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS (all six — `cc0d301`, `a981081`, `78dcc47`, `8ca3b65`,
  `1f783be`, `b7d0dfc`)
