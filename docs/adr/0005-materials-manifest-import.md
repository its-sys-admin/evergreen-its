---
type: reference
date: 2026-08-07
status: active
related_prs: []
workstream: field_ops
tags: [adr, materials, manifest, import, bom, shipping-log, adversarial-input, section-34, section-51, sandbox, ships-dark]
---

# ADR-0005 — Materials-manifest import (field-ops materials sub-lane)

**Status:** Accepted, building. This ADR records the design decided from a survey of the ten real
Evergreen manifest documents at `~/Desktop/evergreen project/manifests/` (four Customer BOMs, two
DELTA BOMs, one master BOM, two shipping logs, one xlsx Customer BOM), the six operator decisions
ratified 2026-08-06/07, and a component reuse-map of the live vendor-estimate importer (ADR-0004).
It extends the materials-tracking work that landed in #724/#725/#727 rather than opening a new
workstream. The lane **ships dark** and its go-live is a visible `ITS_Config` cell-flip.

## Context

PR2 (#725) gave materials tracking a home: a per-job page, an append-only receipt ledger, a
shipment level under each line, and three-way delivery marking. It did not give the office a way to
**get the list in**. Today a project manager retypes a customer BOM — hundreds of rows — into the
portal by hand, and a shipping log's per-load dates never arrive at all.

The documents already exist. They arrive by email as PDFs and spreadsheets, and they are the
authoritative statement of what a job expects. The gap is transport, not information.

### What the corpus showed

Ten real documents, four distinct shapes, each with a trap (every one now has a test in
`tests/test_manifest_parse.py`):

- **Customer BOM (PDF).** The header appears on **page 1 only**; pages 2–3 are headerless data
  continuations. A per-table column re-inference silently drops ~60% of the document.
- **Customer BOM (XLSX).** Title rows, then a wholly-empty row, then the header on **row 4** — a
  fixed offset is wrong and the header must be scanned for. Part numbers arrive as ints, so
  normalization must not render `7006955.0`.
- **DELTA BOM (PDF).** A metadata block in its own table carries the `PRODUCT CODE` row that
  *labels* seven otherwise-indistinguishable `QUANTITY` columns, alongside `OVERAGE`/`REV 1`/`REV 2`
  /`DELTA`. Duplicate header names mean the column map must be **positional**, never name-keyed.
- **Shipping log (XLSX).** One row per truckload: when a part ships in several loads, rows 2..n
  blank their identity columns and are continuations of the row above. Both logs also over-declare
  wildly — the Deep Lake sheet reports 92 columns × 1,247 rows and holds 12 × 57 (51 parts + 5
  continuations + a header).

Two further facts shaped the whole design. **Duplicate part numbers are universal** — 7000153
appears twice in three of the four sample BOMs, under different groupings — so nothing may collapse
by part number without a human deciding. And **filenames lie**, so document identity is derived from
header shape, never from the name.

## Decisions

1. **The parser PROPOSES; the human DISPOSES.** The vendor-estimate lane can collapse a document
   straight to line items because its output schema is fixed. Here the *shape* is the judgement: a
   DELTA BOM carries seven quantity-ish columns and which one is "the expected quantity" is a domain
   call. So `field_ops/manifest_parse.py` emits a normalized **grid** plus a *proposed* column map
   and a document profile, and the validate screen decides. A parser that picked for you would leave
   the human able only to rubber-stamp.

2. **The proposal is evidence-backed, not a guess.** On a revision BOM the default is the highest
   `REV n` column, and `delta_arithmetic_check` verifies the document's own internal identity
   (`Σ(bare QUANTITY) + OVERAGE == REV n`) so the screen can *show its working* ("agrees on 47/47
   rows"). It is a consistency signal, never a gate — a document that fails it still imports, with
   the disagreement surfaced.

3. **REV 2 is authoritative on DELTA BOMs, with per-upload override** (operator decision). The
   parser defaults to it and proves it; office/admin review can change it per upload.

4. **Two-level model** (operator decision): BOM rows become expected-material **lines**;
   shipping-log rows become **shipments** (loads) attached to a line by part number. Keeping loads
   as shipments rather than as extra lines is also what keeps the §51 Material List mirror small —
   the mirror re-projects every active LINE each cycle.

5. **Re-upload is a tracked batch** (operator decision), with merge-by-part-number vs add-as-new
   chosen at validation. `job_manifests.mode` + `merge_options_json` record the choice.

6. **No merging by part number anywhere below the human.** Duplicates are universal and legitimate;
   collapsing them would silently pick a winner. The validate screen surfaces **ambiguous** matches
   for a per-row decision — this is not optional polish, it is the difference between an import you
   can trust and one that quietly loses a line.

7. **The Worker never parses.** It bounds-gates (size / filename / declared-MIME allowlist / magic
   sniff), signs `manifest:v1`, and pools the bytes SEND-FREE in D1. Every parse of untrusted bytes
   happens on the Mac.

8. **Every hostile-input parse runs in the killable child** (`po_materials/estimate_sandbox.py`).
   PDFs reuse the existing `parse_native`; xlsx gains `extract_xlsx_rows`, which also closes the
   ADR-0004 decision-5 gap where openpyxl was the one hostile parse still running in-process.
   Reusing the estimate lane's sandbox rather than cloning one is §14 preservation-over-refactor —
   the cross-package import is deliberate.

9. **Its own bearer tier** (`PORTAL_MANIFEST_API_TOKEN`), for the ADR-0004 decision-4 reason: this
   daemon decodes hostile PDF/xlsx bytes, so it is a highest-exposure process and its token scopes
   only the manifest pool.

10. **Dedupe is PER-JOB, not global** — the one deliberate divergence from `po_estimates`. A master
    BOM legitimately covers sibling jobs (Bradley 1 / Bradley 2), and a global sha256 index would
    let whichever job imported it first lock the other out with a 409 it could never clear. The
    widening is safe only because `manifest:v1` binds `job_id`: byte-identical manifests now exist
    under two jobs by design, and the signature is the only thing preventing a cross-job replay.

11. **The MIME allowlist is narrower than the estimate lane** — PDF + XLSX only. Every allowed type
    must be one the parser can actually read; accepting a `.docx` would queue a document that can
    only ever end in `refused`, after it had been screened and stored.

12. **An imported line is INDISTINGUISHABLE from a hand-authored one.** The commit route imports
    `readExpectationFields` / `catalogIdValid` from `worker/fieldops_expected_materials.ts` rather
    than re-deriving the bounds — re-deriving is how two paths drift apart. `status` stays
    `'expected'` and `unplanned` stays `0`: an import is on-manifest by definition, and the delivery
    system of record remains `material_receipt_events`, so a shipping log's delivery_date column
    must never be written straight to `status='received'`.

13. **The commit is PAGED with a watermark.** A 900-row master BOM cannot commit inside one Worker
    request, so `/commit` lands ~100 source rows per call and advances `committed_through_row` **in
    the same `db.batch()` as the inserts**. A page therefore either fully lands or fully rolls back,
    and a replayed page is a no-op because the next call resumes strictly above the watermark.

14. **The ledger mirrors to Smartsheet** (operator decision) — a per-job `<Job> — Material Receipts`
    sheet, cloned from the `material_incidents` append-only posture. Lands with PR4.

## Invariants preserved

- **Invariant 1 (External Send Gate).** Nothing in this lane transmits externally. The daemon is
  AI-free and send-free and enrolls in `GATED_SCRIPTS`; the Worker is send-free by construction.
- **Invariant 2 (Adversarial Input).** A `/pending` row is untrusted until its `manifest:v1` HMAC
  verifies **and** the reassembled bytes match the signed length and sha256 — two separate checks,
  because the HMAC covers only the *claims* about the bytes. §34 screening
  (`po_attach_screen.screen_attachment`, reused verbatim) precedes every parse, and every parse runs
  in the sandbox child.
- **§51 (ITS-owned structured SoR).** The receipts mirror is one-way ITS→Smartsheet, append-only.

## Slices

| # | PR | Contents |
|---|---|---|
| 3a | #727 | `manifest_parse.py` — the parser + 20 tests + the corpus eval script |
| 3b-1 | #729 | `manifest:v1` HMAC + parity suite; `extract_xlsx_rows` in the sandbox; the two Worker line validators exported |
| 3b-2 | #731 | migration `0060`; `worker/fieldops_manifests.ts`; `portal_client` lane + cross-runtime pins; purge/prune/errorCopy reconciliation |
| 3b-3 | — | `field_ops/manifest_poll.py` + registration seams + this ADR + the §43 runbook |
| 3b-4 | — | `ManifestValidatePage.tsx` + `/plan` + the paged `/commit` |
| 4 | — | daily-report snapshot (`daily-report-v7`) + the §51 receipts mirror |

## Consequences

- `field_ops` gains a dependency on `po_materials.estimate_sandbox` and
  `po_materials.po_attach_screen`. That is the intended reuse (decision 8) and is recorded here so a
  future reader does not "fix" it into a clone.
- The acceptance gate before enabling import is a clean run of
  `python -m scripts.eval_manifest_parse --corpus "~/Desktop/evergreen project/manifests"` over the
  real documents — 10/10 currently produce importable rows.
- Go-live is `field_ops.manifest_poll.polling_enabled` → true plus loading the plist. The row ships
  seeded `false` so activation is a visible cell-flip rather than a phantom switch.
