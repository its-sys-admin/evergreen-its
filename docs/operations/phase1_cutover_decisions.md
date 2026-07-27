---
type: operations
date: 2026-07-23
status: active
related_prs: []
workstream: null
tags: [cutover, phase_1, identity, f22, security, cloudflare, mailbox]
---

# Phase-1 Cutover Decisions (identity / mailbox / Cloudflare)

Decision record for five operator decisions ratified 2026-07-23 (Seth, chat
session) governing the Phase-1 production cutover. This doc exists so a future
session can reconstruct the *why* — especially the deliberately-not-shipped
F22 self-exclusion filter — without the originating chat. Companions:
`docs/operations/cutover_checklist.md` (the mechanical gate, CL-40–CL-42 added
by the same PR as this doc) and `docs/tech_debt.md` (the deferred
self-exclusion design, entry "F22 token-identity self-exclusion filter").

## Purpose

Turn five chat-ratified decisions into a durable, file-referenced record:
the Smartsheet token identity for Phase 1, the Cloudflare account posture,
the single-production-mailbox model, the sandbox disposition, and the staged
`system.operator_email` repoint.

## The five decisions

### D1 — Smartsheet identity: an operator-designated personal PAT (for now)

**Decision (§44 high-class, operator-ratified; changeable later).** ITS runs
Phase 1 on an operator-designated personal Smartsheet PAT. That account runs
the production stand-up and OWNS all ITS workspaces. (The specific identity is
held in the operator's own records / the planning layer, not restated here.)

**The F22 self-exclusion filter is DELIBERATELY NOT shipped now.** The F22
authorized-approver set is a workspace's individual USER-share list, and — as
Op Stds §46 already records (the "owner-inclusion open question") — a
workspace owner's account is inherently within that set. Because Phase 1's
token identity is also a workspace owner (and an intended approver), a
self-exclusion filter that subtracts the token identity would also remove that
account's legitimate approval authority. So the filter waits for a dedicated,
approval-free token identity (see migration trigger below).

Mechanism references (reconstructable from code, at the §46 level): the
approver set is `shared/smartsheet_client.py::list_workspace_share_emails`;
approval identity is matched email-only
(`shared/approval_verification.py:35-38`); the consuming seam is
`safety_reports/send_poll_core.py::_load_authorized_approvers` (:191–:220),
fail-closed.

**Accepted residual.** The Op Stds §46 owner-inclusion residual applies for
Phase 1: the token identity sits within the approver set. This is the SAME
posture the mirror validation period ran under — accepted, not new. Closure =
the deferred self-exclusion filter, gated on a dedicated token identity.

**Migration path / trigger.** A dedicated `its@` Smartsheet seat + the
self-exclusion filter (subtract the token identity's own email from the
approver set). Design fully scoped in `docs/tech_debt.md` → "F22
token-identity self-exclusion filter — DEFERRED until the dedicated its@
Smartsheet token".

### D2 — Cloudflare: transfer the existing account, migrate nothing

The existing dedicated Cloudflare account transfers to
`its@evergreenrenewables.com` ownership/billing (Super-Admin invite + payment
method + Workers Paid). **No Worker / D1 / zone migration.** The portal keeps
`safety.evergreenmirror.com` through Phase 1 (the `phase1-hybrid`
`verify_cutover` profile covers the deliberately-mirror rows). Checklist:
CL-41.

### D3 — Single production mailbox: its@evergreenrenewables.com

ALL send lanes (safety, progress, procurement/PO, RFQ, subcontracts) send from
the single production mailbox `its@evergreenrenewables.com`; the Exchange
Application Access Policy is scoped to it. Later identity splits use **SHARED
mailboxes** (free, no license) — **NEVER a personal mailbox**: the Application
Access Policy grants the app full mail read/write over the entire mailbox it
covers, which is unacceptable blast radius on a human's mailbox. Checklist:
CL-07 (reworded), CL-40.

#### D3 amendment (2026-07-24) — safety@ alias ships in Phase 1

The weekly safety-report lane (`safety_reports.weekly_send.from_mailbox`)
repoints to `safety@evergreenrenewables.com` instead of `its@`. This does NOT
overturn D3 — there is still exactly one production mailbox:

- **`safety@` is an SMTP alias (proxy address) on the `its@` mailbox**, added
  2026-07-24 (`Set-Mailbox -EmailAddresses @{add="smtp:safety@…"}` — additive,
  lowercase `smtp:`, primary untouched). Not a second mailbox; a standalone
  `safety@` mailbox remains the deferred shared-mailbox step.
- **Graph authorization is unaffected.** The RBAC management scope
  `ITS-its-mailbox-only` filters on
  `PrimarySmtpAddress -eq 'its@evergreenrenewables.com'`; an alias does not
  change the primary, so the resolved mailbox object still matches.
- **Reply routing survives regardless of the From header.** Confirmed
  empirically 2026-07-24 — `Get-TransportRule ITS-safety-client-replies-route`
  reports `SentTo : {its@evergreenrenewables.com, its@evergreenrenewables.com}`
  (Exchange resolved the alias to the mailbox's primary at rule-creation time).
- **`SendFromAliasEnabled` is OFF** and is a SEPARATE, still-open cosmetic
  decision: with it off, a successful alias send still stamps the From header
  with the primary (`its@`), so the recipient sees `its@`, not `safety@`.
  Turning it on is what makes the client actually see `safety@`.
- **Known operator-side follow-up (not a code change):** the
  `ITS-safety-client-replies-route` transport rule currently fires on *all*
  external mail to the `its@` mailbox, so once the repoint sweep runs, replies
  to PO/RFQ/subcontract/progress mail (all sent from `its@`) will also BCC the
  four safety approvers. Seth is tightening the rule in PowerShell.
- **Open resolution risk (see the changeset / `graph_client` note):** ITS puts
  the from-mailbox in the Graph URL path (`/users/{from_mailbox}/sendMail`);
  proxy-alias resolution on `/users/{id}` is undocumented. The smoke test
  (`scripts/smoke_test_graph.py --mailbox safety@evergreenrenewables.com`)
  settles it empirically before go-live; fallbacks recorded there.

### D4 — Sandbox left EMPTY after the dev wipe

The `evergreenmirror.com` sandbox tenants stay empty after the development
wipe; rebuild on demand via the rehearsal-proven stand-up tooling
(`scripts/migrations/standup.py`, `docs/runbooks/tenant_standup.md`).

### D5 — system.operator_email → its@; escalation stays Seth through Day-7

`system.operator_email` repoints to `its@evergreenrenewables.com` as a staged
repoint value (`production_repoint.py` sweep). Escalation ROUTING (Resend /
Sentry / Healthchecks.io destinations) stays Seth through the Day-7 gate — CL-32
is unchanged by this decision.

## Execution plan pointer (parallel tracks A–F)

The decisions above execute via the parallel-track Phase-1 plan (chat-ratified
alongside them):

- **Track A** — Seth provisioning (M365 `its@` mailbox + AAP, Cloudflare
  Super-Admin invite/billing/Workers Paid, Smartsheet/Box identities).
- **Track B** — CC PRs B1–B6 (this doc lands as PR-B5).
- **Track C** — dump + wipe of the sandbox tenants (dump-before-delete;
  sandbox then stays empty per D4).
- **Track D** — production stand-up: `standup.py` with `--skip-shares`
  (CL-11 shares are Seth-attended via `seed_production_shares.py`, not
  stand-up-automated) and `--skip-restore-sheet ITS_Active_Jobs` ×2 (both
  Active-Jobs sheets start empty in production — no mirror-job restore).
- **Track E** — D1 cleanup (production hygiene per CL-19).
- **Track F** — verify + go-live (`python -m scripts.verify_cutover`
  `--profile phase1-hybrid`, then the CL-30 gate).

## Validation

- The checklist rows carrying these decisions are CL-07 (reworded), CL-40,
  CL-41, CL-42 in `docs/operations/cutover_checklist.md`.
- The F22 mechanism summary above is reconstructable from code alone:
  `shared/smartsheet_client.py::list_workspace_share_emails`,
  `shared/approval_verification.py` (email-only identity note),
  `safety_reports/send_poll_core.py::_load_authorized_approvers`.
- The deferred self-exclusion design lives in `docs/tech_debt.md`; its
  trigger is the `its@` Smartsheet identity migration (D1).

## Owner

`@solutionsmith` (Seth — all five are §44 operator decisions; D1's token
posture and any future gate flips remain FIXED high-class actions).
