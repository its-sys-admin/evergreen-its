---
type: reference
date: 2026-07-18
status: active
related_prs: []
workstream: po_materials
tags: [adr, purchase-orders, rfq, vendor-estimate, extraction, local-inference, no-cloud-ai, disposition, external-send-gate, adversarial-input, section-34, section-51, ships-dark, post-delivery]
---

# ADR-0004 — RFQ generator + vendor-estimate importer (po_materials sub-lane)

**Status:** Accepted / build deferred post-delivery. This ADR records the design decided from a full
survey of the Evergreen vendor-quote corpus (`~/Desktop/Evergreen project/Z. Quotes 1/` — Platt/Nassau
distributor quotes, contractor estimates/proposals, a scanned AP report, revision chains, hand-marked-up
sheets) + a component reuse-map of the live Purchase-Order workstream, then adversarially red-teamed
(findings 1–7 folded in as required changes). It **revives the RFQ scope that
[purchase-orders mission v5](../../../its-blueprint/workstreams/purchase-orders/mission.md) deferred**,
but with a decisive shape change: the extraction ladder is **local-only — no cloud AI anywhere in the
lane** — so the "sole live Anthropic consumer is `intake.py`" invariant is preserved. It is built as a
sub-lane of `po_materials` (not a new workstream), ships **dark**, and its build is slotted **after the
Aug-7 Evergreen delivery** (slot-into-roadmap). **This ADR is the committed design record**; the
slice-by-slice build order is in §Slices below and mirrored in
[purchase-orders mission v6 §10](../../../its-blueprint/workstreams/purchase-orders/mission.md).

## Context

Evergreen's procurement thread today stops at the PO composer. Vendor quotes arrive as ad-hoc PDFs; an
estimator dispositions line items by highlight color (green = buy, red = excluded, yellow = pending —
the "Sheb Mark-Up" sheets), then re-types the accepted lines into the PO builder. This ADR adds the
**front half of the procurement lifecycle**: (a) an outbound **RFQ generator** with an attached fillable
quote form, and (b) a **vendor-estimate importer** — office-upload → §34 screen → tiered extraction →
line-item **disposition** → pre-filled draft PO in the existing composer.

### What the corpus showed
- **The corpus is doc-type-mixed.** Quotes, estimates, proposals, **invoices**, and a scanned QuickBooks
  **AP report** all live together. Invoices/AP reports must be **classified and refused from the PO path**,
  never parsed as new line items. Doc-type classification is therefore stage 1.
- **~20% are scanned images** (Nassau invoice, the Apricus AP ledger) → an OCR tier is required, not
  optional.
- **Revision chains are real and filenames lie.** Same quote number appears across `(2)/(3)/(4)` and
  "Mark-Up" siblings with **drifting content**; one file (`Colfax_ (Sheb Mark-Up).pdf`) actually contains
  a *different vendor's* proposal. Identity must be **body-derived** (`vendor` + `quote_number`), never
  taken from the filename.
- **Layout is heterogeneous.** Platt is section-grouped with per-line stock notes; OnPoint intermixes
  SOV/SOC **section-header rows that carry no qty/price** (must not become $0 lines); Terratech is a clean
  table; Nelson/Brimfield are lump-sum / narrative "not-to-exceed." A single union schema covers ≥90% and
  the lump-sum docs degrade to one line + a `not_to_exceed_cap`.
- **The highlight markup encodes buyer-side decisions.** Plain text extraction drops it → the **disposition
  screen is the first-class replacement** for the manual color-coding step.

### Reuse map (the PO workstream is the parent)
- **REUSE-AS-IS:** `po_attach_screen.screen_attachment` (a pure `(filename, mime, bytes) → verdict`
  function, fully decoupled from the PO pool); the D1 chunk-pool wire shape (`po_attachments` + chunks);
  the `weekly_send` engine (already parameterized — po_send/subcontract_send bind it); `form_pdf._p`/`_esc`
  (the escaping render path); integer-cents math + `_js_round`; the existing **`POST /api/po/drafts`**
  session route + `parseDraftBody`/`parseLines`/`computeTotals` validators.
- **PARAMETERIZE:** the `weekly_send` attachment resolver (single → sequence, for RFQ PDF + xlsx form);
  `vendors.get_vendor_by_key` recipient lookup (read-only).
- **FORK-NEW:** the D1 tables (`po_estimates`/`estimate_extractions`/`rfqs`/…), `worker/po_estimates.ts` +
  `worker/rfq.ts`, the `estimate_*`/`rfq_*` Python modules, `shared/ollama_client.py` +
  `shared/schema_loader.py`, `schemas/vendor_estimate_extraction.json`, three Smartsheet builders.

## Decisions

1. **Local-only extraction ladder — NO cloud AI in this lane.** Tier 0: our fillable `.xlsx` quote form
   round-trip (deterministic openpyxl parse). Tier 1: deterministic native-PDF parse (pdfplumber) +
   **data-driven per-vendor templates** (YAML matchers/column-maps/section-band rules — *data, not code*;
   adding a vendor = adding a YAML file). Tier 2: a **local Ollama model** on the production MacBook
   (~7–9B Q4, `keep_alive=0` load-on-demand — fits the 18 GB host with headroom), **schema-constrained
   decoding** + macOS Vision OCR for scans, gated by deterministic math cross-checks. Tier 3: Review Queue
   → manual entry in the disposition screen. Vendor pricing **never leaves the machine**; the "sole live
   Anthropic consumer is `intake.py`" invariant holds. Model is ITS_Config-pinned; swapping it re-runs the
   corpus eval to re-qualify.

2. **Extraction is ADVISORY; every dollar re-enters the trusted path only through the human-reviewed
   session route.** The Mac daemon posts extraction results to D1 as advisory data (over an authenticated
   bearer, no HMAC needed on results); the SPA disposition screen renders them; on accept the SPA calls the
   **existing `POST /api/po/drafts`** (`requireSession` + `cap.po.manage`) with cents-normalized lines, and
   the PO is signed (`po:v1`) only at generate. **No new draft-create route, no importer bypass** of the
   existing validators.

3. **The automated gates verify internal CONSISTENCY, not FIDELITY to the source (red-team #2).** A
   self-consistent wrong price (`qty×unit==extended`, `Σ==total`, schema-valid) passes the math gate,
   constrained decoding, `jsonschema.validate`, AND the server-side recompute — because every layer checks
   arithmetic, not agreement with the document. The **single fidelity control is the human side-by-side
   accept.** Therefore it is hardened: **accept of a Tier-1/2 line is blocked unless the source preview for
   its page is loaded** (preview render failed → forced Tier-3 manual or explicit "no-preview verified"
   acknowledgment); Tier-2 (LLM-sourced) lines get distinct visual treatment + per-line confirmation. This
   is named as the boundary, not hidden behind gate rhetoric.

4. **Two internal bearer tokens, not one (red-team #1, overrides the first architect pass).**
   `ITS_PORTAL_ESTIMATE_TOKEN` scopes only `/api/po/estimates/internal/*` and is held by the
   highest-exposure process (`estimate_poll`, which decodes hostile PDF/xlsx bytes through pdfplumber /
   openpyxl / Pillow / Quartz / Vision / a local LLM). `ITS_PORTAL_RFQ_TOKEN` scopes only
   `/api/po/rfqs/internal/*` and is held by `rfq_poll`/`rfq_send_poll`. A compromised extraction daemon
   must **not** reach the RFQ send-lane control surface. This restores the repo's per-tier token doctrine
   (`po`/`config`/`sub`/`fieldops` each mint their own bearer). Both register in `DARK_BEARER_SECRETS`.

5. **§34 screening precedes every parse; untrusted-parse stages are isolated (red-team #5, #12).**
   `po_attach_screen.screen_attachment` is reused verbatim (magic → structural PDF/OpenXML/image →
   config-gated ClamAV). Each hostile-input stage (pdfplumber, openpyxl, Quartz render, Vision OCR) runs in
   a **killable subprocess with an `RLIMIT_AS` memory cap + wall-clock timeout**, so a wedged/OOM parse is
   reaped, the document routes to Tier-3, and the daemon survives.

6. **Doc-type gate first (red-team framing).** Deterministic keyword/layout classifier (Tier-2 `doc_type`
   as fallback confirmation). `invoice`/`ap_report` → estimate status `refused` + an `Estimate_Log` row +
   a Review-Queue row (`POLICY_EDGE`, WARN) — **visible, never silently dropped, never into the PO path.**

7. **Identity is body-derived; dedupe is index-enforced (red-team #9).**
   `family_key = normalize(vendor)|quote_number` (fallback `sha256` for numberless Brimfield-class docs).
   Exact-byte replay → HTTP 409 `duplicate_estimate`, enforced by a **partial-unique index**
   (`idx_po_estimates_sha_live`, live rows only) with the constraint violation mapped to 409 (not a 500);
   concurrent-double-upload is RED-tested. A newer revision in the same family supersedes; markup siblings
   are kept as distinct flagged rows.

8. **Realize the `schemas/` convention (first real occupant).** `schemas/vendor_estimate_extraction.json`
   v1.0.0 (the corpus union schema) + a new `shared/schema_loader.py` that enforces the documented
   version-field / reject-on-mismatch contract. The **same** schema drives Ollama `format=` constrained
   decoding AND post-hoc `jsonschema.validate`. The schema carries **explicit numeric maxima** (`qty`,
   `*_cents`, `confidence` 0–1) so validation is a real value gate. `anomaly_logger.check` runs on **string
   fields only** (mirroring intake's `collect_anomalies` — cents integers would trip its `>1000` numeric
   sentinel and burn the tripwire) and is **not** credited as a price-manipulation control (red-team #6).

9. **The importer/disposition lane is READ-ONLY against the vendor SoR (red-team #7).** No route ever
   creates or updates `po_vendors`/`ITS_Vendors` from extracted (attacker-controlled) content — vendor
   identity and `Contact Email` are picked/confirmed by the human from the existing vendor list. This
   closes recipient-poisoning: RFQ/PO send resolves `TO` live from `ITS_Vendors` by Vendor Key, so an
   untrusted document must never be able to write that field.

10. **Fillable-form identity + xlsx round-trip hardening (red-team #3, #10).** The Tier-0 `.xlsx` form
    carries hidden `_ITS_META` defined-names (`ITS_RFQ_NUMBER`, `ITS_VENDOR_KEY`, `ITS_FORM_TOKEN` =
    `rfq-form:v1` HMAC). A verified token auto-binds the upload to `(rfq_id, vendor_key)`; absent/tampered →
    ordinary ladder upload. The disposition UI **shows** that an auto-bind happened ("confirm vendor").
    `parse_quote_form` reads `data_only=True`; any numeric input cell whose raw value begins with
    `= + - @ \t \r` is rejected → ladder fall-through; carried-forward text is formula-lead-neutralized
    before entering `payload_json`; `po_attach_screen._scan_openxml` is extended to flag
    `externalLink`/`externalReference` relationships.

11. **Draft-import is idempotent (red-team #4).** DraftBody carries `estimate_id`; the draft route refuses
    if a non-canceled draft already references it; `dispose` is in-WHERE status-guarded (second call → 409
    `already_disposed`); the SPA treats a dispose-409 as "already imported → discard the just-created
    duplicate." RED test: draft-succeeds → dispose-fails → retry ⇒ exactly one draft.

12. **RFQ send is one row per `(rfq, vendor)` with a hard-populated Workstream tag (red-team #8).**
    `rfq_poll` writes one `RFQ_Pending_Review` row per vendor (Vendor Key on the schema-twin "Job ID"
    slot). Because the sheet is brand-new, the `weekly_send` **fail-open-on-absent-Workstream** path has no
    pre-backfill excuse: the `Workstream` cell is hard-populated at row creation and registered in
    `picklist_validation.REGISTRY`; the twin-shape test asserts it is non-empty. Send scripts forbid
    `anthropic` **and `ollama_client`** (send scripts are local-AI-free too).

## Invariants preserved
- **External Send Gate (Invariant 1):** two-process. `rfq_generate`/`rfq_poll`/`estimate_*` have zero send
  capability; `rfq_send`/`rfq_send_poll` have zero AI (cloud **or** local). Ships dark; RFQ send go-live is
  a FIXED high-class operator flip (§44), never in a PR. Recipients resolve at send time from `ITS_Vendors`
  via F22.
- **Adversarial Input (Invariant 2):** HMAC domains `est:v1` (upload pool), `rfq:v1` (RFQ canonical JSON),
  `rfq-form:v1` (form identity) — all domain-separated. Every Worker body is shape-guarded + `?`-bound;
  every mutation is atomic with its audit row (`db.batch`, the W4 class). §34 screening + subprocess
  isolation is the untrusted-byte defense; the extraction OUTPUT is advisory and re-verified by a human
  before any dollar enters the trusted path.

## Slices (each a dark-shipped, independently-mergeable, live-smokeable PR)
- **Lane 1 — importer (build first):** **E1** upload pool (D1 `0054` + `worker/po_estimates.ts` + SPA +
  `requireEstimateToken` + `est:v1`) · **E2** `estimate_poll` daemon (screen → classify → refuse invoices →
  Box → `Estimate_Log`, all → `needs_review`) · **E3** disposition screen + Quartz previews + manual Tier-3
  → draft PO (**first end-to-end import**) · **E4** Tier-1 deterministic parse + vendor templates + math
  gate + dedupe · **E5** Tier-2 local Ollama + OCR (`shared/ollama_client.py`, `schema_loader`, schema
  v1.0.0) · **E6** Tier-0 quote-form parse + `scripts/eval_estimate_ladder.py` (offline corpus-replay
  acceptance gate).
- **Lane 2 — RFQ generator + send:** **R1** RFQ composer (D1 `0056` + `worker/rfq.ts` + SPA) · **R2**
  `rfq_generate` + `rfq_poll` (render RFQ PDF + per-vendor xlsx form → Box → one `RFQ_Pending_Review` row
  per vendor) · **R3** send lane (`rfq_send`/`rfq_send_poll`, F22, the `weekly_send` sequence-attachment
  seam) · **R4** round-trip close (form token auto-bind → `responded`, requested-vs-quoted compare,
  cutover-checklist + enablement entries, registry-sweep audit).

## Consequences
The sub-lane ships dark; the operator applies migrations `0054`–`0056`, runs the three Smartsheet builders,
`ollama pull`s the pinned model on the production host, seeds the config gates `false`, refreshes the live
venv for the new deps (`pdfplumber`, `ocrmac`, `pyobjc-Quartz/Vision`, `jsonschema`), and flips the gates
only after the offline corpus eval qualifies extraction quality. Because it mirrors PO (pool, daemon,
send, Box, review-twin), the operator's PO mental model transfers directly. The genuinely-new surfaces —
a local inference process, untrusted-document parsing, and the disposition UI — carry their own RED tests
and adversarial review as definition-of-done. Parked-not-hidden: vendor-direct upload (office-upload only),
email intake (Email Triage's future scope), and cloud-AI escalation (a dark config tier only if local
quality disappoints) are first-class future options, not silent gaps.

---

## Amendment 2026-08-07 — Tier 1 gains a deterministic VENDOR-SPREADSHEET path

**Status:** accepted, additive. Nothing in the original decisions is reversed.

**What was wrong.** The ladder as built could not extract a vendor `.xlsx` at all.
`estimate_poll._attempt_extraction_ladder` bailed with `if declared_mime != MIME_PDF: return None`
immediately below the Tier-0 branch, so a spreadsheet was screened, filed and then hand-keyed by
the office — despite already being a typed numeric grid, i.e. the *easiest* thing in the lane to
parse deterministically. Tier 0 could not help: it is bound to ITS's own artifact by an
`_ITS_META` sheet probe plus the `rfq-form:v1` HMAC, exactly as decision 8 intends.

**Decision.** Add **Tier 1 (xlsx)**: `estimate_parse.parse_xlsx_estimate`, gated by
`po_materials.estimate_extract.tier1_xlsx_enabled` (seeded `false`). It is an **adapter, not a
second parser** — a worksheet already IS the `list[list[list[list[str]]]]` shape
`parse_generic_table` consumes, so column inference, section bands, `check_math` and
`to_worker_payload` are reused verbatim. **No AI**, consistent with decision 1: Tier 2 remains the
lane's only inference tier and is untouched (present, dark).

**Three consequences worth recording, because each was a real decision:**

1. **Doc-level totals are read from the CELL GRID, never regexed from flattened text.**
   `_GENERIC_TOTALS` requires exactly two decimals; openpyxl returns the *stored* value, and a
   round money amount stringifies with one (`str(4000.00) == "4000.0"`). Regexing therefore drops
   round totals — subtotals especially — and `check_math` *skips* comparisons with absent
   operands, so a book with a **wrong** subtotal posts as `extracted` with `math_ok=True` and the
   cross-check never performed. Measured on lines summing to 4000.00 against a claimed 3999.00:
   text-regex `math_ok=True, flags=[]`; cell-grid `math_ok=False` with both the subtotal and the
   grand-total flags. This is a correctness control, not an optimization.
2. **Merged cells fill DOWN only, never ACROSS.** Filling across copies one column's value into a
   sibling that has none. Measured: merging the "Description" and "Qty" headers and filling across
   overwrote the Qty header, `_infer_columns` claimed nothing, and the sheet yielded zero lines.
   Filling down is semantically sound (a category cell merged over its member rows applies to each).
3. **`.xlsx` gets no rendered preview, and that is left alone.** Quartz cannot open a workbook, so
   an accepting import rides the `no_preview_verified` acknowledgment through the Worker fidelity
   gate — precisely the path a verified Tier-0 form already takes. Synthesizing a preview *from our
   own parse* was considered and **rejected**: it would render our extraction rather than the
   vendor's document, making a wrong parse self-confirming and defeating decision 3's single
   fidelity control. A real xlsx→PDF→PNG conversion (LibreOffice headless) is a new heavyweight
   native toolchain on the highest-exposure hostile-byte path and stays out of scope.

**Sandboxing.** The parse runs in the killable child (`estimate_sandbox.parse_xlsx_grid`), per
decision 5 — openpyxl is a zip+XML parser and is no safer than pdfplumber. Tier 0's in-process
openpyxl call remains defensible only because that file is ITS's own fixed-geometry, HMAC-verified
form; a vendor workbook earns no such assumption.

**E6 corpus gate still applies.** Per the original E6, a tier gate flips `true` only after
`scripts/eval_estimate_ladder.py` qualifies it against
`tests/fixtures/estimate_corpus_expectations.json`. The eval's xlsx branch now falls through from
Tier 0 to this tier so it can score vendor books at all. **The fixture is still empty** (only its
`_README`), so no baseline has ever been snapshotted — meaning this gate and the pre-existing
`tier1_enabled` gate are BOTH unqualified until the operator runs `--write-expectations` against
the local corpus. That run needs the corpus on the production host and is therefore an operator
step, not a CI one.
