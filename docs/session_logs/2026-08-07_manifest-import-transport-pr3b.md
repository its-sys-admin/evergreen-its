---
type: session_log
date: 2026-08-07
status: closed
related_prs: [727, 729, 732, 733, 734]
workstream: field_ops
tags: [session_log, field_ops, portal, materials_tracking, manifest_import, hmac, sandbox, section34, adr, registry_reconciliation]
---

# Session log — 2026-08-07 · Manifest import transport (PR3b): the pool, the daemon, and the paged commit

## Purpose

Execute the PR3b half of the materials-tracking program: everything around the parser #727
landed — upload → §34 screen → daemon → reviewable grid → commit. The committed design record is
[ADR-0005](../adr/0005-materials-manifest-import.md), which carries the six ratified operator
decisions and supersedes the chat-session brief this work started from. The same brief also
scoped PR4 (daily-report snapshot + the §51 receipts mirror); that is **not** started, and its
spec is summarised under "Not done" below so a fresh session needs nothing but this repo.

## The brief's first claim was wrong, and finding that out was the session's first job

The brief listed #727 (the parser) as landed, with "(check `gh pr view 727`)" against its merge
commit. It was **OPEN and BEHIND**. `field_ops/manifest_parse.py` was not on `main` at all, so
PR3b had nothing to build on. Updated the branch, waited out CI, merged, four-part verified.

This is the brief's own warning about itself paying off — and the reason the first action of the
session was `grep`/`gh` rather than `Edit`.

## Shape of the work: four slices, not one PR

The brief framed PR3b as a single PR. Delivered as four, matching how Track 6 shipped:

| slice | PR | merge | contents |
|---|---|---|---|
| foundation | #729 | `c4b5e8b` | `manifest:v1` HMAC + 15 golden vectors; `extract_xlsx_rows` in the sandbox; the two Worker line validators exported |
| the pool | #732 | `f26b3e7` | migration `0060`; `worker/fieldops_manifests.ts`; the `portal_client` lane; purge/prune/errorCopy |
| the daemon | #733 | `9785329` | `field_ops/manifest_poll.py`; ADR-0005; the §43 runbook; 18 registries |
| plan + commit | #734 | `baf0ca8` | `/plan` dry run; the paged, watermarked `/commit` |

One mega-PR would have been a single un-reviewable CI cycle over ~4,500 lines. Each slice is
independently gated and independently revertible.

## Decisions worth recording

### Dedupe is PER-JOB, diverging deliberately from the estimate lane

`po_estimates` carries a **global** partial-unique `sha256` index. `job_manifests` uses
`UNIQUE (job_id, sha256)` instead, because a master BOM legitimately covers sibling jobs —
Bradley 1 and Bradley 2 are separate jobs served by one document — and a global index would let
whichever job imported it first lock the other out with a 409 it could never clear.

That widening is only safe because `manifest:v1` binds `job_id`. Byte-identical manifests now
exist under two jobs *by design*, so the signature is the only thing preventing a cross-job
replay. Both halves are tested, including a dedicated cross-job-signature-refused case.

### The parse runs in the killable child; the sandbox is reused, not cloned

openpyxl and pdfplumber are zip/XML parsers over attacker-influenceable bytes. Adding
`extract_xlsx_rows` to `po_materials/estimate_sandbox` also closes the ADR-0004 decision-5 gap
where the xlsx path was the one hostile parse still running in-process. `field_ops` therefore
depends on `po_materials` for the sandbox and the §34 screener — §14 preservation-over-refactor,
recorded in ADR-0005 decision 8 so a later reader does not "fix" it into a clone.

**Cell types ride as JSON scalars, deliberately.** `json.dumps` cannot encode a `datetime`, so
the obvious implementation is `str()` on every cell — and that is exactly wrong here:
`normalize_cell` renders a float `7006955.0` as `"7006955"` (a part number) but a str
`"7006955.0"` as `"7006955.0"` (a part number matching nothing). Only non-encodable values are
stringified. The naive version passes a round-trip test and still corrupts every numeric part
number in the corpus, so it has its own test.

### Three failure classes kept separate

Conflating any two would be a real defect:

- **Integrity** (bad signature/digest/chunk set) → CRITICAL + security-flagged review row, and
  **no result post at all**: the bytes stay in D1 for forensics. The §34 screen never runs on
  unverified bytes.
- **§34 refusal** → MALICIOUS fires CRITICAL naming the uploading account; SUSPICIOUS is WARN.
- **Unreadable** (a scan, an empty export, no header row) → an **ordinary** review row asking
  the office for a better copy, no security flag. This is the only class whose one-shot flag a
  Tier-2 operator may clear and retry.

Crying "security" on the third would train the operator to ignore the first two.

A fourth thing that is **not** a failure: an unresolved Box root is a config gap. The row is
left claimed and deliberately **unflagged**, so it self-heals when the root is set. Getting that
backwards would wedge a good document forever.

### The importer refuses rather than invents

`readExpectationFields`' `description_required` rule fires for a manifest row carrying only a
part number and a quantity. The tempting fix is to synthesize a description from the part
number; that is invented field data (§4), so the row is refused **with its row index** for the
human to fix. The validate screen maps the document's own description column, so a well-formed
import already carries one.

### The replay guard must run before the status guard

The paged commit advances `committed_through_row` in the **same `db.batch()`** as its inserts,
so a page is atomic and a replay is a no-op. The first implementation checked manifest status
first — which meant a client whose **final** page response was lost in flight would retry, find
the manifest already `committed`, and get a 409, reporting failure for work that had succeeded.
That is precisely the ambiguity a watermark exists to remove. Reordered: a fully-replayed
payload answers `{ok, done, inserted: 0}` whatever the status; genuinely new rows against a
refused/discarded manifest are still refused.

## Controls proven to bite (each injected, confirmed RED, reverted)

| injection | caught by |
|---|---|
| swap `manifest_uuid`/`job_id` in the canonical | 5 of 15 HMAC parity tests, incl. the position-binding one |
| delete the sandbox dispatch branch | the round-trip test, with the message naming the cause |
| change the Worker's pending response key | `test_manifests_pending_wire_key_parity` |
| rename a Worker route path | `test_manifest_internal_paths_match_the_worker_routes` |
| shift one purge `results[]` index | the purge counter assertions (1/4/6/5, all distinct) |
| add `graph_client` to the daemon's imports | `test_capability_gating` |

The sandbox one matters most: `_child_main`'s trailing `else:` is an unguarded fall-through to
the allocation-bomb test fn, so an allowlist entry without its dispatch branch does not error —
it burns 512 MiB and spins until the reap, which reads as a slow parse rather than a bug. A
comment now warns the next editor at the dispatch site.

## Registry reconciliation: 11 planned, 5 more found by the teeth

The daemon slice planned 11 registration surfaces from the subsystem map. CI teeth named **five
more**, each by file and symbol: the dashboard interval-tuning registry (`daemon_ops._DAEMONS`),
the state-write allowlist (the `.watchdog` marker is not a state write), the escalation-helper
declared list in `test_transient_fence`, the VC-01 secret-count pin, and the daemon reference.

The three documented cascading CI gates fired in order and were each satisfied rather than
exempted: error-copy parity (globs `fieldops_*.ts`, so the new Worker file was in scope), the
enablement-doc sha256 (re-recorded **three** times — troubleshooting guide, config dictionary,
daemon reference), and the tree xref, which additionally required re-running **both** generators
because the guide is itself sha-tracked.

## Live validation

The brief's stated acceptance gate passes end-to-end through the new sandboxed path:

```
python -m scripts.eval_manifest_parse --corpus "~/Desktop/evergreen project/manifests"
→ 10/10 documents produced importable rows
```

Deep Lake resolves to exactly **51 data rows + 5 continuations**, independently confirming the
corrected reading (51 parts, 5 extra loads) that #727 wrote into `0059`'s header — and falsifying
the "1,195 continuation rows" figure a design subagent had reported in the prior session.

## Two self-inflicted flakes worth remembering

1. `test_estimates_pending_wire_key_parity` failed in a full-suite run because I reverted
   `shared/portal_client.py` **while pytest was running** — `inspect.getsource` re-read the
   changed file with stale line numbers. Re-ran clean on the settled tree. Do not mutate files a
   running suite introspects.
2. Three `/commit` tests failed on rows leaking between cases: the suite's `beforeEach` did not
   clear `job_expected_materials`. A green suite whose fixtures share state proves less than it
   looks like it does.

And one failure that was **not** a defect: the first `test_manifest_poll.py` used a two-column
fixture grid and every happy-path test failed. The daemon was right — the parser requires three
recognisable header tokens before calling a row a header, precisely so a data row reading
"Description" cannot qualify. The fixture was widened; the control stayed.

## Verification

- pytest: exit 0 (full suite) at each slice
- mypy: no issues in 478 source files
- ruff: clean (`ruff check .`, whole tree)
- worker vitest: 70 files / 1227 tests
- main-branch CI on each merge commit: SUCCESS (#727 `437e8fa`, #729 `c4b5e8b`, #732 `f26b3e7`,
  #733 `9785329`; #734 `baf0ca8` verified after merge)

## Not done

- **`ManifestValidatePage.tsx`** — the three-pane validate screen. The `/plan` and `/commit`
  routes have no UI driving them, so the lane is complete server-side and not yet usable by the
  office. This is the only PR3b piece outstanding.
- **All of PR4** — the daily-report snapshot (`daily-report-v7`), migration `0061`
  (`daily_photo_pool.line_uuid` + `receipt_event_id`), and the §51 `<Job> — Material Receipts`
  mirror. Ratified shape: Option A (the filed daily report carries a snapshot of the day's marks,
  accepting the contract inversion); `daily-report-v7`'s sections array is byte-identical to v6
  with only `form_code`/`version`/`comment` changing; `required-content.json` is NOT touched;
  and the confirmation photo binds via a server-side column, never a submission-ref field.

## Operator steps before any of this runs

1. `npx wrangler d1 migrations apply its-safety-portal-db --remote` — `0060` must land **before**
   the Worker deploys, or every manifest route 500s. `git -C ~/its pull origin main` first.
2. `npx wrangler secret put PORTAL_MANIFEST_API_TOKEN`, and the Keychain twin
   `ITS_PORTAL_MANIFEST_TOKEN`.
3. Flip `field_ops.manifest_poll.polling_enabled` and load the plist — a §44 capability
   activation, so Seth's call. The row ships seeded `false` so this is a visible cell-flip.
