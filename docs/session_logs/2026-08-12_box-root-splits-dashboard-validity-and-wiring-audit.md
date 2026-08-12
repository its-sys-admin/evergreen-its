---
type: session_log
date: 2026-08-12
status: closed
related_prs: [79, 98, 104, 112]
workstream: field_ops
tags: [session_log, po_materials, subcontracts, operator_dashboard, field_ops, box, archive, job_archive, wiring_audit, house_reflexes]
---

# Session log — 2026-08-11 → 2026-08-12 · Box-root splits (PO, subcontracts), dashboard validity wiring, and the all-lanes audit that closed 24 gaps

## Summary

Four PRs, one throughline. The operator surfaced three defects on the live **Test** job — Smartsheet
rows filing without their PDFs attached, and PO/RFQ/subcontract Box folders nested three levels deep
under `<safety root>/<job>/` — and each follow-on ask ("will this persist to the subcontract workflow
too", "wire it into the dashboard", "verify all other workflows are wired properly") widened the fix
from a two-lane patch into a full-repo consistency sweep. **#79** built the recipe: a new
ITS_Config-owned Box root per lane, resolvers rewired to it, every service self-healing its own
attachment, `job_archive` growing a container slot both directions, and a full HOUSE_REFLEXES §1
registry fan-out — validated by a 31-agent adversarial review (21 confirmed findings, all fixed) and
actuated live with the root built and the config row seeded *before* merge, so there was never a
window where the lane pointed at an unresolved root. **#98** applied the identical recipe to
subcontracts (8 containers; no migration — verified zero live subcontract content anywhere, so there
was nothing to relocate) and its own review caught a latent gap #79 had shipped: the review-row Box
attach was fresh-create-only, meaning a crash between row-add and attach left the approver's row
permanently bare. **#104** answered "wire it into the dashboard" with a new read-only Box-roots
validity panel and a five-root system-map brief; its review caught a blocker — the panel's primary
detection path called `get_setting`, which *raises* `SmartsheetNotFoundError` on a missing config row
rather than returning a value, and the original mock had modeled a return contract the client doesn't
actually have, so the panel silently degraded its worst-case detection to an amber "renamed" reading
instead of a red "config row MISSING — filings HOLD." **#112** answered "verify all other workflows are
wired properly" with an 8-lane, 33-agent adversarial audit across every product lane; a session usage
limit killed 19 of the verifiers mid-run, and `resumeFromRunId` replayed the 14 already-cached findings
and re-ran only the dead ones. 24 findings confirmed (1 refuted), all fixed in the same PR — the
create-only review-row attach recurring a third time (PO, RFQ, on top of the subcontract instance #98
had already fixed), an already-filed intake replay that never re-attached its document, a WSR-twin
review row that had been carrying the internal packet instead of the client-facing report, an
`Estimate_Log` row with a filed file and no attachment or link, a silent job-name fallback that would
have grown an archive-invisible Box container, and a `job_archive.md` container table still one row
short of its own stated count. All four PRs are four-part verify clean.

## PRs landed

| PR | What | Merge SHA | Diff | Verify |
|---|---|---|---|---|
| #79 | PO lane gets its own Box root ("ITS Purchase Orders", `408361866550`, key `po_materials.box.portal_root_folder_id`); attach parity; archive → 7 containers | `5ac2cd3` | 50 files, +1138/-335 | four-part clean |
| #98 | Subcontract-lane twin — "ITS Subcontracts" root (`408583610037`, key `subcontracts.box.portal_root_folder_id`); archive → 8 containers; no migration (verified zero live content) | `c5ecc02` | 34 files, +416/-239 | four-part clean |
| #104 | Dashboard Box-roots validity panel (five roots) + system-map five-root brief | `9b2d860` | 11 files, +315/-17 | four-part clean |
| #112 | All-lanes wiring audit — 24 confirmed gaps closed (1 refuted) | `b591b8a` | 20 files, +246/-65 | four-part clean |

`pr-landed-verifier` output, quoted verbatim (all four via `gh`; independently spot-checked for this
log against `gh api repos/.../commits/<sha>/check-runs` — `test`/`portal`/`secrets` all
`completed`/`success` on every merge commit):

```
  #79  5ac2cd3  MERGED 2026-08-11T23:14:03Z  CI SUCCESS
  #98  c5ecc02  MERGED 2026-08-12T17:53:17Z  CI SUCCESS
  #104 9b2d860  MERGED 2026-08-12T19:22:18Z  CI SUCCESS
  #112 b591b8a  MERGED 2026-08-12T22:25:50Z  CI SUCCESS (current origin/main tip)
```

**Note on interleaving.** `main` between #79 and #112 also carries the ADR-0006 schedule/payment lane
(#80, #84, #85, #90–#93) and the Weekly-Production-Report + tech-debt sessions (#94–#97, #99–#103,
#105–#111) — two entirely separate concurrent sessions, each already logged
(`2026-08-12_adr0006-schedule-payment-tracking-full-lane.md`,
`2026-08-12_weekly-production-report-schedule-and-debt.md`). None of those PRs are part of this arc;
they're noted here only because `git log --first-parent` between the four SHAs above shows them
interleaved.

### #79 — the PO lane gets its own Box root

Evidence gathered live on the **Test** job first: 5 `PO_Pending_Review` rows, zero attachments, Box
folders sitting at `<safety root>/Test/Purchase Orders/`. Root cause was one thing wearing three faces
— the PO lane had never had a Box root of its own, so it piggybacked on `safety_reports`'s tree and its
attach path only ever wrote to the review sheet.

- **New root**: "ITS Purchase Orders" (`408361866550`) + ITS_Config key
  `po_materials.box.portal_root_folder_id`, owned by the leaf module `po_naming`. Resolvers rewired in
  `po_poll` / `rfq_poll` / `estimate_poll`.
- **Attach parity**: the PO PDF now attaches on the flat `PO_Log` row AND the per-job mirror row
  (every-service self-heal — the RFQ-ledger posture already in place elsewhere); per-job `RFQs` mirror
  rows now carry the RFQ PDF + the fillable quote form.
- **Bonus fix caught during evidence-gathering**: `manifest_poll` was filing under the raw `job_id`
  (an archive-invisible `JOB-000031` folder sitting beside `Test`) because the Worker's internal pending
  payload never carried the job's display name. Fixed by having the Worker JOIN `jobs.project_name`
  into the payload; the daemon keys the folder off it, falling back to `job_id` only if that's absent.
- **Archive wiring**: `job_archive` grows a third Box slot, `box:purchase_orders` → **7 containers**
  (4 Smartsheet + 3 Box), both directions; SPA `CONTAINER_COUNT` 6→7.
- **Full HOUSE_REFLEXES §1 registry fan-out**: `build_box_roots.py` (4th root), `standup.py` seed,
  `verify_cutover` VC-03 `non_empty`, dashboard config-editor registry, `production_repoint_map.json`
  §D, config-dictionary regen + manifest shas, troubleshooting tree + regenerated guide, six runbooks,
  CLAUDE.md, ROADMAP Track 6 text, `box_client.move_folder`'s §42 docstring.
- **Migrations** (operator-run, plan-by-default): `relocate_po_box_folders.py` — three passes, the
  first a single atomic Box **move+rename** (`<safety>/<job>/Purchase Orders` becomes `<PO root>/<job>`
  directly — no create-new-folder-and-copy step; a name collision under the PO root REFUSES that job
  rather than merging), the second repairs the id-named-folder bug's children into the project-name
  folder and leaves the emptied id-named folder for manual trash (`box_client` is deliberately
  MOVE-ONLY — no delete primitive exists to automate that last step), the third relocates the archive
  side. `backfill_po_row_attachments.py` attaches already-filed PDFs/forms on pre-change rows.
- **Review**: a 31-agent adversarial workflow (`ops-stds-enforcer` + `portal-worker-security-reviewer`
  + correctness + fan-out lenses) found 21 confirmed findings, **all fixed pre-merge** — including the
  migration's unfenced pass-2 and a smoke test that was still reading the old safety-root config key.
- **Live actuation**: root built and the ITS_Config row seeded *before* merge (zero-downtime ordering —
  no window where the lane resolves an empty/missing root), Worker deployed, both migrations applied,
  everything re-verified against the live tenant after.

### #98 — the subcontract lane gets the identical treatment

Operator's follow-on: "will this persist to the subcontract workflow" — yes, by design, and #98 is that
answer applied end-to-end.

- **New root**: "ITS Subcontracts" (`408583610037`) + key `subcontracts.box.portal_root_folder_id`,
  owned by `subcontract_naming`. `subcontract_poll` files the Subcontract `.docx` + Exhibit A `.docx` +
  Annex C `.xlsx` + the send ZIP directly under `<SC root>/<job>/` — the old
  `<safety root>/<job>/Subcontracts/` nesting is gone.
- **Attach parity**: the three package files inline-attach on the flat `Subcontract_Log` row AND the
  per-job mirror row. The **review-row attach also becomes every-service** — it had been
  fresh-creation-only, meaning a crash between row-add and attach left the approver's surface
  permanently bare. This is the finding that turned out to recur again in #112.
- **Archive wiring**: `job_archive` grows `box:subcontracts` → **8 containers** (4 Smartsheet + 4 Box),
  both directions; every count surface updated.
- **No migration scripts — deliberate.** Verified live: zero `Subcontract_Log` rows, zero per-job
  folders, zero subcontract Box content live or archived. The split lands before any content exists, so
  there is nothing to relocate or backfill (don't-harden-dormant, HOUSE_REFLEXES §6).
- **Review**: a 17-agent adversarial pass (refutation-verified) found 11 confirmed findings (7
  distinct), all fixed — the review-row attach window above, plus the `production_repoint_map.json`
  §D / CL-17 cutover worksheet rows being three behind the map (po_materials + subcontracts + archive
  all missing) — backfilled in the same PR.

### #104 — dashboard wiring: validity, not just presence

Operator's follow-on: "validity and wire into the dashboard."

- **New read-only "Box roots" panel** — for each of the five top-level roots (Safety, Progress,
  Purchase Orders, Subcontracts portal roots + the Track 6 archive root), reads the ITS_Config row,
  live-resolves the folder id via the new `box_client.get_folder_name` read, and compares the live name
  to the canonical one. Red = row missing (raises) / blank / dead id — filings hold; amber = renamed or
  Box unreachable; green = all five canonical. VC-03 only proves the rows are non-empty; this panel is
  the only surface proving each id actually *resolves*.
- **`/system` five-root brief** — the `box` store node's blurb + operator brief describe the five-root
  topology and point at the panel as the validity surface.
- **`shared/box_client.get_folder_name(folder_id)`** — pure read, retry-decorated, the folder twin of
  `get_file_metadata`.
- **Review — a BLOCKER caught**: `get_setting` RAISES `SmartsheetNotFoundError` for a missing row; the
  panel's blanket `except` was rendering its own primary detection class — the missing-row case — as
  amber "config unreadable" instead of red "config row MISSING." The original test had mocked a return
  contract the real client doesn't have (mock-models-the-wrong-seam, HOUSE_REFLEXES §2). Fixed to catch
  the raise explicitly; the test now models the real contract.
- **Token economics, also caught in review**: the dashboard is the system's first long-lived Box-client
  holder. Holding the singleton open across sweeps would mint `box_refresh_token_consumed_retry` WARN
  noise every time a daemon rotates the Keychain token underneath it. Fixed with a sweep-end
  `_reset_client()` plus a 1-hour TTL — at most one clean exchange per hour with a tab left open.
- **Review**: a compact 13-agent adversarial workflow, both findings above fixed in-branch.

### #112 — the all-lanes wiring audit

Operator's follow-on: "verify all other workflows are wired properly." An 8-lane adversarial audit
workflow, 33 agents — one auditor per lane, every finding refutation-verified — swept every workstream
against the post-split Box topology, the attach-parity standard #98 established, the archive model, and
the registries. A session usage limit killed 19 of the verifier agents mid-run; `resumeFromRunId`
replayed the 14 already-cached findings and re-ran only the dead ones rather than re-running the whole
audit from zero. **24 findings confirmed (1 refuted), all fixed in this one PR:**

- **Attach self-heal parity made uniform across every lane** (generalizing the #98 posture):
  - `po_poll` and `rfq_poll` review-row attaches were fresh-create-only — the same class #98 had
    already fixed for subcontracts, independently present in two more lanes. Now every-service in both.
  - `intake`'s already-filed replay never re-attached; it now re-downloads the filed original and
    self-heals the Submission row, wholly fenced so a Box hiccup can't fail the replay.
  - The **WPR review row was carrying the wrong document** — the internal FieldRecords packet, while
    the Compiled PDF cell and the send both pointed at the client-facing WSR. The client report now
    attaches too; safety was unaffected (no provider bound there).
  - **`Estimate_Log` had no attachment and no link** — a bare Box file id sitting in a TEXT_NUMBER
    cell, on the sheet whose stated purpose is "the ledger the office reads without portal access." The
    filed original now attaches, content-typed, every service.
- **Archive integrity**:
  - `rfq_poll`'s silent `job_name or job_no` fallback would quietly grow an archive-invisible container
    (the PO lane already fails loud on the identical input) — now WARNs (`rfq_job_name_fallback`),
    naming the consequence rather than silently degrading.
  - `fieldops_sync`'s startup sweep now resolves `job_archive`'s five Box-root keys — a contract
    `job_archive.py` had stated ("lands when fieldops_sync gains the archive pass") but never actually
    received.
- **Enumeration truth** (the greppable-datum reflex, HOUSE_REFLEXES §1):
  - `job_archive.md`'s container table gained its missing 8th row and the "Eight, not eleven" paragraph
    — the two blocks #98 missed inside the very file it had edited.
  - The ADR-0006 Schedules ridership (and progress-category per-submission PDFs) is now named in every
    safety-root enumeration: `job_archive.py`, `box_client.move_folder` (+ its don't-add-slots list),
    CLAUDE.md rows, ROADMAP, the `project_closure` retention table, the dashboard box-node brief.
  - CLAUDE.md's "`schedule` is null until ADR-0006 lands" claim retired — the lane landed same-week
    (the ADR-0006 session, logged separately).
  - `estimate_log.py`'s docstring no longer claims a dispose-status sync pass that has zero writers; the
    real gap is now `docs/tech_debt.md`'s `[OPEN 2026-08-12, low]` entry (see Open items below).

## Decisions

1. **Seed the root and the config row BEFORE merging the code that resolves it (#79, #98).**
   Alternative considered: build the root as part of the migration, after merge. Rejected — that leaves
   a window where the lane has already been deployed to resolve a Box root that doesn't exist yet.
   Building-then-seeding-then-merging means there is never a live moment where `po_poll`/`rfq_poll`/
   `subcontract_poll` resolve an unset config key.

2. **The relocation migration is a single atomic Box move+rename, not create-new-and-copy (#79).** The
   existing `<safety root>/<job>/Purchase Orders` folder object becomes the per-job folder at the new
   root directly — Box's `move_folder` can move and rename in one PUT (per `shared/box_client.py`'s
   documented contract). A name collision under the PO root REFUSES that job rather than merging,
   because neither system has a merge primitive.

3. **Skip migration scripts for the subcontract split (#98).** Alternative considered: write the
   symmetric relocation script anyway, for consistency with #79. Rejected on don't-harden-dormant
   (HOUSE_REFLEXES §6) — verified live that zero `Subcontract_Log` rows, zero per-job folders, and zero
   Box content exist for the lane, live or archived. There is nothing to relocate; a migration script
   with no target to act on is dead weight that would itself need maintaining.

4. **WARN, not refuse, on `rfq_poll`'s job-name fallback (#112).** The PO lane refuses loudly on the
   identical missing-job-name input; RFQ's fallback silently used `job_no` instead, which would grow an
   archive-invisible container with no operator signal. Alternative considered: match PO's hard refusal
   exactly. Rejected — RFQ filing is not gated on the job name the way PO's archive-slot resolution is,
   and a hard refuse there would brick RFQ filing on an input the lane can otherwise process correctly.
   WARN (`rfq_job_name_fallback`, naming the consequence) satisfies "never silent" without bricking a
   working lane over a cosmetic-but-consequential gap.

5. **The review-row create-only attach was one latent class, fixed lane-by-lane rather than swept once
   (#98 → #112).** #98 found and fixed it for subcontracts as a local finding; #112's cross-lane audit
   found the same pattern independently present in PO and RFQ. This is the HOUSE_REFLEXES §1 datum with
   N implementations, in the wild: nothing forced a search across every review-row attach path when the
   first instance was found, so it took a dedicated 8-lane audit to close the other two. Recorded here
   as the shape of the miss, not just the fix.

6. **The dashboard panel's blocker was a mock that modeled a seam the client doesn't have (#104).** The
   fix is not "add a try/except" in isolation — it's that the original test asserted behavior against a
   return-value contract `get_setting` never offers (it raises on missing, never returns a sentinel).
   Per HOUSE_REFLEXES §2, this is exactly the mocks-pass-but-live-API-rejects class; the fix pins the
   real raise contract, not just the panel's handling of it.

7. **Sweep-end Box-client reset + 1h TTL for the dashboard, rather than per-request client construction
   (#104).** The dashboard is the first long-lived holder of the Box singleton. Constructing fresh per
   request would avoid the token-noise problem entirely but reconstructs on every panel render;
   resetting once per sweep plus a TTL caps the exchange rate to at most once per hour with a tab open,
   while still surfacing a real credential problem within one sweep.

## Open items / next session

- **JOB-000031 Box husk** — the id-named folder `manifest_poll`'s bug created is now empty (pass 2 of
  `relocate_po_box_folders.py` moved its children into the project-name folder) but the empty husk
  itself remains, because `box_client` is deliberately MOVE-ONLY with no delete primitive. Needs a
  manual Box-UI trash by the operator. (`scripts/migrations/README.md`, PO Box-root split family.)
- **Estimate dispose→ledger status sync is an OPEN tech_debt entry**
  (`docs/tech_debt.md`, `[OPEN 2026-08-12, low]`, "Estimate dispositions never mirror back to
  Estimate_Log"). The SPA dispose flow flips only the D1 `po_estimates` row; nothing stamps the outcome
  onto the `Estimate_Log` row, so the office-facing ledger reads `extracted`/`needs_review` forever even
  after a quote becomes a PO draft. D1 stays authoritative — nothing is lost — but the sheet gives a
  permanently wrong answer to "which quotes are still open." Fix shape recorded in the entry: a
  forward-only status-sync pass in `estimate_poll` (the `rfq_poll` pass-② pattern), gated on the
  existing lane gate.
- **Blueprint mission files are now stale, flagged not fixed** — planning-layer edit, not this repo's to
  make: `~/its-blueprint/workstreams/purchase-orders/mission.md:157` ("`po_poll` reuses the shared Box
  mirror-tree root owned by `safety_reports`") is superseded by #79; `subcontracts/mission.md:51`
  ("Box: three uploads (§45 find-or-create ROOT→job→"Subcontracts")") is superseded by #98 — the
  subcontract lane no longer nests under the safety root at all. Both need a planning-layer pass to
  reconcile against the as-built five-root topology.

## Cross-references

- `docs/tech_debt.md` — `[OPEN 2026-08-12, low]` estimate-dispose-to-ledger entry; the manifest
  line-provenance cluster carved out the same triage session.
- `scripts/migrations/README.md` — "PO Box-root split (2026-08-11 family)" section documents both
  migration scripts and the husk-trash caveat.
- `docs/HOUSE_REFLEXES.md` §1 (registry fan-out; N-implementations-enumerate-all-first), §2
  (prove-the-control-bites / adversarial review as definition-of-done on a trust-boundary surface), §6
  (don't-harden-dormant).
- `docs/operations/pr_merge_discipline.md` — the four-part landing verify this log's quotes follow.
- Predecessor logs this arc extends: `docs/session_logs/2026-08-10_archive-button-diagnosis-and-live-drill.md`
  and `docs/session_logs/2026-08-11_archive-followups-deploy-and-tech-debt-trim.md` (Track 6 job-archive
  lineage — the container-count model #79/#98 grow).
- Concurrent same-week sessions, not part of this arc but interleaved on `main`:
  `docs/session_logs/2026-08-12_adr0006-schedule-payment-tracking-full-lane.md`,
  `docs/session_logs/2026-08-12_weekly-production-report-schedule-and-debt.md`.
- `~/its-blueprint/workstreams/purchase-orders/mission.md:157`,
  `~/its-blueprint/workstreams/subcontracts/mission.md:51` — flagged stale above, planning-layer fix.

## Verification (final state, PR #112)

- pytest: 0 failures (excl. the known `test_publish_daemon` host-guard reds, green in CI)
- mypy: 0 errors / 502 source files
- ruff: clean
- Worker vitest 1506 passed · SPA vitest 905 passed (as of #98; unchanged surface at #104/#112 beyond
  the new panel/audit-fix test additions)
- main-branch CI on all four merge commits (`5ac2cd3`, `c5ecc02`, `9b2d860`, `b591b8a`): SUCCESS
