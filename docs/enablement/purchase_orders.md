---
type: operations
date: 2026-07-09
status: active
related_prs: []
workstream: null
tags: [enablement, a8, purchase_orders, po, vendors, terms, external-send-gate, office-admin]
---

<!-- TODO(operator): register this doc in the §6a enablement-doc manifest once it exists. No
concrete §6a manifest / capability-registry file exists in this exec repo yet — the WS3 docs
program (Aug-7 delivery, slice D2-1) is scheduled to create `docs/enablement/manifest.yaml` as
THE §6a manifest. Add the `purchase_orders` capability entry there once that artifact lands.
Do not fabricate a registration in the meantime. Same interim posture as
`portal_job_creation.md`. -->

# Enablement — Purchase Orders in the ITS Portal · Op Stds §6/A8

**Audience:** the Evergreen office **admin** who drafts purchase orders, and the **operator**
who approves and monitors them. No code or Smartsheet-formula knowledge required. This is the
plain-language companion to the successor-operator runbooks
[`../runbooks/po_poll.md`](../runbooks/po_poll.md) (generation) and
[`../runbooks/po_send.md`](../runbooks/po_send.md) (send).

## Purpose — what the PO flow does

ITS turns a purchase order you draft in the portal into a **branded PDF**, files it to Box
and the `PO_Log` ledger, puts it in front of an approver for **human sign-off**, and — only
after approval — **emails it to the vendor** from `procurement@`. The purchaser identity, the
tax rules, the terms language, and the PO number are all handled for you. Nothing goes to a
vendor without a person approving it (the permanent **External Send Gate**).

## The flow, end to end

1. **Draft** — an admin builds the PO in the portal's **PO Builder** (job → vendor → line
   items → totals → terms → preview). Saving creates a draft; you can edit it until you
   generate.
2. **Generate** — click **Generate**. The Worker re-checks the math, allocates the PO number,
   and queues the PO. Within a minute or two the Mac-side generator (`po_poll`) renders the
   PDF, files it to **Box** (the job's `Purchase Orders` folder), appends the **`PO_Log`**
   ledger, and creates a **`PO_Pending_Review`** row for approval.
3. **Review & approve** — an authorised approver opens the `PO_Pending_Review` row, reads the
   attached PDF, edits the **Email Body** if desired, and checks **Send Now** (send this
   cycle) or **Approve for Scheduled Send** (Monday 07:00 Pacific batch). ITS verifies the
   approver is authorised (the F22 gate) before anything sends.
4. **Send** — the send daemon (`po_send`) emails the PO PDF to the **vendor's contact email**
   (read live from the vendor record) from `procurement@`, CC'ing the internal invoice-routing
   list. The row stamps `SENT`.

## Who can do what (permissions & approval authority)

- **Drafting a PO** requires the **`cap.po.manage`** capability — granted to the portal
  **admin** role. Any admin can draft and generate POs.
- **Approving a send** is separate and stricter: approval authority = **membership of the
  `ITS — Purchase Orders` Smartsheet workspace** (Op Stds §46, decision D11). Only people
  **shared into that workspace** can approve a PO for sending. ITS verifies the actual person
  who checked the approval box against that share list (the **F22** gate) — an unauthorised
  or unshared approver is refused, and if **no one** is shared in, **every** PO send is
  blocked (fail-closed). Sharing the approvers into the workspace is a go-live step.

## The PO Builder form

The builder walks top to bottom:

| Step | What you do | Notes |
|---|---|---|
| **Job** | Pick the Evergreen job. | The job supplies the PO number's job segment and the ship-to state (which drives the tax badge). |
| **Vendor** | Pick the vendor. | Region + Supply-Category chips help you find the right one. The vendor's default terms profile pre-selects the terms. |
| **Line items** | Add rows: description, quantity, unit cost. | ITS computes each line's extended amount, the subtotal, tax, shipping, and total. Money is exact-to-the-cent (no rounding drift). |
| **Tax** | Usually automatic. | Resolved by **ship-to state** — Illinois **9%**, Oregon **0%**. You can also mark a PO **Tax Exempt**, **Sales Tax included**, or set an **override** rate for a state whose rate differs. |
| **SOW / delivery / payment** | Fill the scope, delivery, and payment fields. | Printed on the PO. |
| **Terms** | Pick the terms profile (see below). | Defaults from the vendor's profile; you can change it per PO. |
| **Supersede** (optional) | Point this PO at the one it replaces. | ITS chains the numbers and prints the supersession clause. |
| **Preview → Generate** | Review the on-screen preview, then Generate. | Generate is the point of no return for editing — after it, changes are a **supersede** or **cancel + re-draft**. |
| **Status tracker** | Watch the PO move draft → pending_review → approved → sent. | Mirrors the ledger state. |

## The PO number

ITS generates the number — you never type it. The scheme (decision D7) is five dot-separated
segments:

```
{job_no}.{site_phase}.{supersede_seq}.{revision}
```

where `job_no` is the Evergreen `YYYY.NNN` project number and `site_phase` is the **site**
segment of the job's Evergreen identifier — so a job numbered `2026.384.1` produces PO numbers
`2026.384.1.<supersede>.<revision>`, e.g. **`2025.364.1.2`**. You never re-type the site: picking
the job in the builder fills both the job number and the Site/phase from the job record. When a
PO supersedes an earlier one, the numbers **chain** (a revised PO carries a higher revision;
the predecessor is marked **Superseded** in `PO_Log`). The number comes from the **job
record**, never from a folder name, and ITS refuses to file a **duplicate** number — if a
hand-issued PO already used a number, the draft is fenced for you to reconcile (see the
`po_poll` runbook).

## Vendor management (`ITS_Vendors`)

Vendors live in the **`ITS_Vendors`** Smartsheet (the vendor **source of record**, decision
D4). Each vendor has:

- **Vendor Name** + an immutable **Vendor Key** (`VEN-######`) — the key never changes and is
  how the portal, the PO, and the send all recognise the same vendor.
- **Contact Email** — **the address the PO is emailed to** (resolved live at send time; keep
  it current).
- Address, Contact Name/Phone, **Region**, **Supply Categories**, **Default Terms Profile**,
  **GTC Reference**, **Active** (Active / Inactive / Archived), Notes.

**Edit vendors in two places, kept in sync automatically (§51 bidirectional sync):**

- The **portal** (the PO Vendors page) — for the admin's day-to-day edits.
- The **`ITS_Vendors` sheet** — the operator's source of record.

A change in either side flows to the other within a sync cycle: the sheet is the master
(full-replace down into the portal cache), and portal edits mirror **up** into the sheet by
Vendor Key. In-flight portal edits are protected (never clobbered mid-edit). **Vendors are
never deleted** — retire one by setting it **Inactive** (or **Archived**); it drops off the
picker but the record and its PO history stay.

## Terms library — how to pick terms

Terms are **git-versioned, immutable** documents (decision D6). Each PO **pins** a terms
profile + version at generation, so it renders the same language forever. Available profiles:

| Profile | What it is | When to use |
|---|---|---|
| **Standard 17-clause** (`standard_17`) | Evergreen's standard domestic equipment/services terms (the 4-item additional-instructions list + the 17-clause block, from the 2019 PO). | **Default** for most vendors. |
| **Chint/CPS vendor terms** (`chint_vendor`) | The short vendor-specific inline regime used on Chint Power Systems POs. | Chint / CPS purchases. |
| **Negotiated GTC** (`negotiated_gtc`) | A reference line only — the full negotiated General Terms & Conditions live in a separate document (the vendor's GTC Reference). | Vendors (e.g. VSUN) with a negotiated multi-page GTC already held by both parties. |

A vendor carries a **Default Terms Profile**, which pre-selects on the PO; you can change it
per PO. Changing the *wording* of a profile is never an edit — it is a **new version**, so
past POs are unaffected.

> **Legal review pending:** the `standard_17` and `chint_vendor` term texts are transcribed
> from the corpus and are marked **legal-review pending** — Evergreen's legal review of the
> terms language is a pre-first-live-send checklist item.

## Purchaser identity & tax (versioned config)

- **Purchaser** (decision D5): the PO header, invoice-routing line, and signature block print
  **Evergreen Renewables LLC**, the Irvine STE 570 address, and the phone — from versioned
  config, never hard-coded. Every PO CC's the internal **invoice-routing** distribution so
  procurement / PM / permitting see every outbound PO.
  > The purchaser entity + address are **pending Evergreen's written confirmation** (a Day-1
  > external request and a pre-cutover checklist line).
- **Tax** (decision D8): a ship-to-state table (Illinois 9%, Oregon 0%) in exact integer
  math. `exempt` / `included` / `override` are per-PO toggles for the cases the table doesn't
  cover.
- **Delivery contacts** — a configurable suggestion list for the builder's delivery-contact
  field: pick a saved name and its phone + email fill in automatically, or just type a
  free-text contact as before (the list never blocks anything).
- **Seeing and editing the current config** — the **PO/SC Configuration** page (Administration,
  cap.po.manage) shows the live purchaser identity, the full tax table (rates as %), the
  delivery-contact list, and the terms profiles that print on every PO. An admin can queue an
  edit from the same page; each change goes through a review-and-deploy pipeline (Op Stds §50;
  ADR-0002) and takes effect — or fails visibly in the page's status monitor — never silently.
  A new terms version additionally sits behind a legal-review gate until the operator makes it
  current.
  > **§43 (Successor-Operator):** if the PO Configuration page won't load, it's a display-only
  > failure — reload / re-check network + session; there is no daemon to restart, config to toggle,
  > or lock to clear. If it persists after a reload, escalate to Seth. (Nothing on this page can
  > change a PO or a config value — it only reads.)

## "Held" states — what they mean and who fixes them

If a PO can't be sent cleanly, the send daemon marks the `PO_Pending_Review` row **HELD**
rather than send something wrong. Nothing was emailed. The fixes below are detailed in
[`../runbooks/po_send.md`](../runbooks/po_send.md):

| HELD reason | What it means | Who fixes it |
|---|---|---|
| `held_no_recipient` | The vendor's Contact Email is blank or the Vendor Key is unknown. | **Operator** — fill the vendor's Contact Email in `ITS_Vendors`, then re-approve. (Unknown vendor → Seth.) |
| `held_missing_envelope` | The PO row lost its PO-number tag, so the email can't name the PO. | **Operator** — restore the number tag (from `PO_Log`) or cancel + re-draft, then re-approve. |
| `held_missing_pdf` | The row has no attached PDF. | **Operator** — confirm the PO filed to Box; restore the link, or check the `po_poll` runbook if it never filed. |
| `held_workstream_mismatch` | A non-PO row landed on the PO review sheet (contamination guard). | **Seth** — this touches the Send Gate; the operator captures the evidence and escalates. |
| `held_oversized_packet` | A PO PDF over the email ceiling (should be impossible for a PO). | **Seth** — anomalous; escalate. |
| `EMPTY_ALLOWLIST` (all sends blocked) | No approvers are shared into the `ITS — Purchase Orders` workspace. | **Operator** — share the PO approvers into the workspace (§46). |

The operator **never** forces a send, marks a row SENT, or edits the approval columns — held
means "fix the underlying data and re-approve through the normal flow."

## Go-live activation sequence

Purchase Orders **ship dark** — the generation and send gates all seed to `false`, so nothing
runs until the operator turns it on. The go-live sequence (full detail in the
[`po_poll`](../runbooks/po_poll.md) runbook's deploy-activation checklist):

1. Deploy the Worker with the PO routes + its bearer token; seed the Keychain tokens (Seth).
2. Seed `ITS_Vendors` and confirm the purchaser/tax/terms config; set the Box mirror-tree
   root.
3. Load the two daemons (`po-poll`, `po-send`); run the live smokes on the mirror —
   `smoke_test_po_send.py` today, plus `smoke_test_po_generate.py` once the S8 slice adds it
   (until then, verify generation with one `po_poll.poll_once()` cycle — see the `po_poll`
   runbook).
4. Provision the `procurement@` mailbox and its Application Access Policy scope; **share the
   PO approvers** into the `ITS — Purchase Orders` workspace.
5. Flip the gates (reading each row's Description first): generation first (filing only), then
   the **send** gate last — after the two fail-closed smokes pass.

## What's NOT in the first release (fast-follows)

Designed into the data model, built after the Aug-7 delivery: **RFQ** (multi-supplier
quoting), the **subcontractor contract** workstream, **GTC attach-at-send**, **drawn
signatures** on POs, and **material-catalog line-item picking**. Today a PO is drafted with
free-form line items, sent for the vendor to countersign, and the negotiated GTC is referenced
(not auto-attached).

## Owner

`@solutionsmith`. This guide is part of the §6/A8 enablement-doc program (operator/admin-facing
manuals). The polished distributable PDF version renders via the WS3 docs pipeline before
cutover; this in-repo version is the source of truth for its content.
