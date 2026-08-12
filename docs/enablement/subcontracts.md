---
type: operations
date: 2026-07-13
status: active
related_prs: []
workstream: subcontracts
tags: [enablement, a8, subcontracts, docx, exhibit-a, sov, legal-gate, review, ships-dark]
---

# Enablement — Subcontracts: generating a subcontract package · Op Stds §6/A8

**Audience:** the Evergreen office **admin** who drafts subcontracts and the **operator** who reviews
them. No code knowledge assumed. This is the plain-language companion to the successor-operator runbook
[`../runbooks/subcontract_generation_path.md`](../runbooks/subcontract_generation_path.md) (the fix-it
side). The subcontracts workstream is the deterministic, wet-signature cousin of Purchase Orders — if you
know the PO flow, this will feel familiar.

## What it produces

From a subcontract you draft, ITS builds a **subcontract package** as **editable Office files** (not flat
PDFs, on purpose — so you can adjust specific clauses or values in Word/Excel before signature):

- **`Subcontract.docx`** — the 27-article Subcontract Agreement body.
- **`Exhibit A.docx`** — the Scope of Work.
- **`Annex C - Schedule of Values.xlsx`** — the price breakdown.

The fixed annex kit (the static annexes) is copied in verbatim — only the two documents above are
authored. The whole build is **deterministic and uses NO AI**: the same inputs always produce the same
files, and ITS never invents contract language.

## The flow, end to end

1. **Draft** — an admin builds the subcontract in the portal (job → subcontractor → trade → contract
   price → Schedule of Values → governing law). The portal computes the money and signs the draft.
2. **Generate & file** — the Mac-side worker (`subcontract-poll`) picks up the queued draft, re-checks
   everything (below), renders the three files, and files them to **Box** (the job's folder under
   the `ITS Subcontracts` root), the **`Subcontract_Log`** ledger, and a
   **`Subcontract_Pending_Review`** row for approval.
3. **Review** — an authorized reviewer opens the `Subcontract_Pending_Review` row, opens the attached
   `.docx`/`.xlsx`, and edits as needed. This is the same WSR-style review surface used for safety and PO.
4. **Sign & execute** — because this is a wet-signature document, the executed contract is handled
   outside ITS; when both parties have signed, the operator marks the ledger row **`executed`**.

> **Sending ships dark.** ITS **generates, files, and stages** subcontract packages for review, and the
> automated send half (SC-S4) is now built — it emails the package to the subcontractor after a human
> approves the review row (send approval is by workspace membership, §46). The send lane **ships dark**:
> it does nothing until the operator flips its `subcontracts.subcontract_send.polling_enabled` gate. Until
> that gate is on, delivering the package remains a manual step.

## What ITS adds over the manual process

Every generated subcontract is checked before it can be filed — a package that fails any check is
**fenced to the Review Queue, never filed as a wrong contract**:

- **Schedule-of-Values-sums-to-price.** The SOV lines must add up to the §2.1 Contract Price. The money
  is re-computed and re-checked (in exact cents, no rounding drift) rather than taken on faith.
- **Words match figures.** The §2.1 price *in words* is derived from the same cents figure as the number,
  so "words" and "$…" can never disagree (the paper corpus had a real mismatch this prevents).
- **Canonical entity names.** Consistent, correct legal entity strings — no "…, LLC, LLC" drift.
- **Governing law from the job site.** The governing-law clause is **derived from the job-site state** —
  e.g. "the Commonwealth of Virginia" or "the State of Oregon" — with venue in that state's courts (a
  Virginia job keeps Evergreen's home **Fairfax County** venue, per the corpus). A missing or unrecognized
  state **fails safely** rather than guessing.

## The trade + Exhibit-A template model

The variable part of Exhibit A is **Article II — "The Work"** (the trade-specific scope). It works like
this:

- Each subcontractor carries a **trade**. The trade maps to a standard **Article II template**.
- The operator gets that trade's standard Article II as an **editable starting point**, then adjusts it —
  ITS does the fixed ~95% of the package; you author the scope specifics.
- Related trades share a template where the corpus doesn't distinguish them (the electrical trades share
  one electrical scope); a specialty trade with no standard scope starts from a placeholder.

Both the contract body and the per-trade Exhibit A scopes are **versioned**: a wording change is always a
**new version**, never an edit to an existing one, so past subcontracts render identically forever.

## The Layer-A legal gate (§50)

A subcontract body renders on a live subcontract **only once its terms version has been legally cleared**.
A newly-created (or newly-edited) version starts as **pending** — and a pending version **will not render**;
it fences the subcontract until someone clears it. Clearing a version is the **"make this version current"**
action on the **PO/SC Configuration** page (the §50 config editor) — an operator/Seth step that marks the
version legally reviewed and active. The same gate applies to a new per-trade Exhibit A scope version.

This is a belt-and-suspenders legal guardrail *on top of* the send gate: even a correctly-drafted
subcontract won't render from un-reviewed language.

## Two kinds of terms profile

- **Library profile** — the standard 27-article body ITS fills in. Versioned and behind the Layer-A legal
  gate above.
- **Attach profile (negotiated MSA)** — for a subcontractor already on a negotiated Master Subcontract
  Agreement. Instead of the full body, `Subcontract.docx` is a **one-page reference** (preamble + Contract
  Price + a fixed reference line + signature); the binding terms live in the external MSA. Exhibit A and
  Annex C still render, so the package stays three files.

## What's not built yet

- **Short form, insurance/COI compliance gate, AI-assisted scope drafting, and e-signature** are
  designed-for but out of the current build (first-class future slices, not silent gaps).

## It ships dark

The whole subcontracts pipeline **ships off** — every pass of the `subcontract-poll` worker is a
configuration gate seeded to **false**, so nothing runs until the operator turns it on at go-live.
Go-live is a checklist (in the runbook): deploy the Worker, seed the credentials and the subcontractor
registry, set the Box filing root, confirm the config, then flip the gates **after reading each gate's
description first**. Turning generation on enables **filing only** — the subcontractor **send** lane
has its own separate gates and approval flow, and nothing dispatches without a human-approved
review row (the External Send Gate).

## If something looks stuck

| What you see | What it means / what to do |
|---|---|
| A subcontract **never filed**, and a Review-Queue item appeared | It was fenced by a check. If the reason is an **operator-fixable input** (unknown subcontractor, unknown/invalid terms profile, a numbering clash with a hand-issued contract), fix the underlying data (or re-draft), then clear its one-shot flag so the next cycle re-files it. The runbook has the exact steps. |
| The Review-Queue item is about **HMAC / render math / SOV mismatch / a rejected write** | These are trust, code, or schema issues — **escalate to Seth**, don't retry. |
| **"Box portal root unresolved"** | The Box filing root isn't configured — set the documented `ITS_Config` root value (a Tier-2 step); the queued subcontract then files automatically. |
| A subcontract needs its **terms version cleared** (pending) | Someone must "make current" that version on the PO/SC Configuration page (the Layer-A legal gate) before it will render. |
| The worker seems to **do nothing** and the watchdog flags it stale | Expected before go-live — the pipeline ships dark (all gates off). Not a fault. |
| A **per-job tracking sheet** is missing one row | The per-job mirror is best-effort; a miss is permanent and repaired by hand (copy the row from `Subcontract_Log`). The flat ledger and the Box files remain the source of record. |
| Anything you're unsure about | Goes to Seth — sending, secrets, legal-review clearance, numbering, and code are always his. |

## Owner

`@solutionsmith`. Part of the §6 / A8 documentation program. This in-repo version is the source of truth
for its content; the polished distributable PDF is rendered from it.
