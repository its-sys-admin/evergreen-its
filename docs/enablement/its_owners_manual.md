---
type: operations
date: 2026-07-10
status: active
related_prs: []
workstream: null
tags: [enablement, a8, owners-manual, overview, daemons, lifecycle, systems-of-record]
---

# Enablement — The ITS Owner's Manual · Op Stds §6/A8

**Audience:** the Evergreen owner and the person operating ITS day-to-day. No code knowledge
assumed. This is the one-page mental model of the whole system — *what ITS is, what runs and
when, how a report gets from the field to a customer, where every record lives, who does what,
and what to do when something looks wrong.* Every other enablement guide is a close-up of one
part of this picture; this is the picture.

## What ITS is

ITS — the **Integrated Technical System** — is a Claude-powered assistant that runs on a Mac in
the office. It takes the busywork out of the weekly reporting cycle: it collects safety and
progress reports from the field, compiles them into clean weekly packets, and — **only after a
person approves each one** — emails them to the customer. It reads and writes your existing
systems (Smartsheet, Box, Outlook); it does **not** replace them.

Two rules are built into ITS at the deepest level and never bend:

> **Rule 1 — nothing goes out the door without a human "send."** ITS can *prepare* a customer
> email, but it physically cannot *send* one on its own. Every external message waits for a
> person to approve it. This is permanent, by design.
>
> **Rule 2 — anything from outside is treated as untrusted.** Emails, uploads, and form
> submissions from outside the company are handled as data to be screened, never as instructions
> ITS should obey. Attachments and photos are scanned before ITS ever files them.

Everything below is how those two rules play out in practice.

## The daily rhythm — what runs, and when

ITS is a set of small, independent background workers (**daemons**). Each wakes on its own
schedule, does one job, writes down what it did, and goes back to sleep. If one stops, the others
keep running. You do not start or stop these by hand in normal operation — they run themselves.

| Worker | Roughly how often | What it does |
|---|---|---|
| **Portal intake** (`portal-poll`) | every ~1 min | Pulls new safety-report submissions from the field portal, screens their photos, and files each as a PDF in Box. |
| **Safety weekly compile** (`weekly-generate`) | Fridays, early afternoon | Gathers the week's safety reports for each active job into one packet and stages it for your review. |
| **Safety send** (`weekly-send`) | every ~15 min | Emails the safety packets **you have approved** to the customer. |
| **"Compile now"** (`compile-now-poll`) | every few min | Lets you force a safety recompile on demand instead of waiting for Friday. |
| **Progress weekly compile** (`progress-generate`) | weekly | Same idea as the safety compile, for progress reports. |
| **Progress send** (`progress-send`) | every ~15 min | Emails the **approved** progress reports. |
| **Purchase-order pull / send** (`po-poll`, `po-send`) | every few–15 min | Pulls submitted purchase orders from the portal and emails the **approved** ones. |
| **Job mirror** (`fieldops-sync`) | every few min | Copies portal jobs, hours, equipment, materials, and incidents up into Smartsheet (ships **off** until cutover). |
| **Config actuator** (`config-actuator`) | every ~2 min | Applies configuration changes **you have approved** (e.g. purchaser/tax/terms). |
| **Picklist sync / audit** (`picklist-sync`, `picklist-audit`) | hourly / daily | Keeps dropdown lists across sheets in step with their master lists. |
| **Watchdog** (`watchdog`) | daily | The system's own health check — see "When something looks wrong" below. |

The exact wake-up intervals are settings (see the **ITS_Config Data Dictionary**), so they can be
tuned without touching code.

## The review → approve → send lifecycle

This is the heart of ITS and the same for every kind of outbound message (safety, progress,
purchase orders). It always has the same three beats:

1. **ITS prepares** — a compile worker builds the packet (or draft) and writes a **review row**
   into a Smartsheet "Pending Review" sheet, with the draft email body already filled in and the
   status set to **PENDING**. No email exists yet.
2. **A person approves** — you open the review row, read the draft, edit the email body if you
   like, then check **Send Now** (or **Approve for Scheduled Send** for a Monday-morning window).
   Your name and the time are stamped as the approver.
3. **ITS sends** — a *separate* send worker notices the approved row, addresses the email to the
   right people (looked up fresh at send time), attaches the compiled PDF, sends it, and marks the
   row **SENT**.

The two halves are deliberately different programs: the half that **thinks** (uses AI, builds the
draft) has no ability to send email at all, and the half that **sends** has no AI in it. That
separation is Rule 1 made concrete — even in the worst case, the thinking half cannot put a
message on the wire.

## Where everything lives

ITS keeps different kinds of information in the system best suited to it. Knowing which is which
tells you where to look for any given fact.

| You're looking for… | It lives in… |
|---|---|
| **Structured facts** — jobs, contacts, review rows, approvals, settings | **Smartsheet** (the system of record for data) |
| **Documents** — the rendered report PDFs and weekly packets | **Box** (the system of record for files) |
| **Customer email** — what actually got sent | **Outlook / Microsoft 365** (the send path) |
| **Live field data** — portal accounts, submissions in flight | the **field portal** (a Cloudflare web app; its database is a fast cache, the real record is the Smartsheet + Box copy) |
| **Every setting ITS reads** | the **ITS_Config** sheet — see the **ITS_Config Data Dictionary** |
| **Every error ITS hit** | the **ITS_Errors** sheet (one row per occurrence) |
| **Anything ITS wasn't sure about** | the **ITS_Review_Queue** sheet (low-confidence or flagged items land here instead of being guessed) |
| **Whether the workers are alive** | the **ITS_Daemon_Health** sheet (one row per worker, updated each cycle) |

## Who does what

| Role | Who | What they do |
|---|---|---|
| **Developer-Operator** | Seth | Builds and changes ITS, holds the passwords/keys, does anything touching code, secrets, or the send gate. The escalation point of last resort. |
| **Successor-Operator** | the trained office operator | Day-to-day operation: approves sends, flips a documented setting, re-runs a stuck worker, clears a stuck item. Follows the runbooks; writes no code and touches no secrets. |
| **Office PM / admin** | office | Creates and maintains jobs, manages portal accounts, reviews and approves the weekly reports. |
| **Manager** (crew lead) | field | Runs crews, assigns people and equipment to jobs, manages tasks. (See the *Manager role* guide.) |
| **Field PM / subcontractor** | field | Submits the daily/weekly safety and progress reports from the portal. |
| **Stakeholder / customer** | external | Receives the approved weekly reports. Never has access to ITS itself. |

## When something looks wrong

ITS is built to keep running with as little intervention as possible. Problems resolve in three
tiers, and knowing which tier you're in tells you what to do:

1. **It fixes itself.** A worker that crashes is simply re-run on its next scheduled wake-up. The
   **watchdog** notices a worker that has gone quiet, a review item that's been sitting too long,
   or an unresolved critical error, and raises a flag. An external service (Healthchecks.io) is
   set up to watch the whole Mac, so even "the office computer died" gets noticed — **this last
   piece is not switched on yet**, so right now a total computer failure would not raise an
   outside alarm. *You do nothing.*
2. **The operator fixes it.** For a **known, low-risk** problem — re-run a worker, flip a
   documented setting, re-send an approval, clear a stuck lock — the trained operator follows the
   matching runbook (`docs/runbooks/`) and resolves it. *No code, no passwords.*
3. **Escalate to Seth.** Anything **new**, or anything touching **the send gate, passwords/keys,
   policy, or code**, always goes to Seth. These four are never handled by improvising — they are
   the fixed "call the developer" categories.

**Alerts reach you two ways:** a critical problem emails the operator (and, if email itself is
down, a second independent channel fires). Every problem is also written to the **ITS_Errors**
sheet, so the morning review is "scan ITS_Errors and ITS_Review_Queue."

## The kill switch

There is one master pause: the **`system.state`** setting in ITS_Config. Set it to **PAUSED** or
**MAINTENANCE** and every worker exits cleanly at its next wake-up without doing anything; set it
back to **ACTIVE** to resume. It is a convenience pause for maintenance windows — *not* a security
control (Rule 1, the send gate, is the real safety boundary, and it is always on regardless).

## The rest of the shelf

This manual is the overview. For the close-ups:

- **Safety Reports** — the submit → review → weekly-packet flow, end to end.
- **The Portal Admin Dashboard** — creating and managing portal accounts.
- **Creating jobs in the ITS Portal** — the office-PM job form.
- **Purchase Orders in the ITS Portal** — the PO flow.
- **The Manager role / The Subcontractor tier** — who can do what in the field.
- **The Weekly Progress Rollup Numbers** — how the progress figures are computed.
- **The ITS_Config Data Dictionary** — every setting ITS reads, what it does, and its default.

## Owner

`@solutionsmith`. Part of the §6 / A8 documentation program. This in-repo version is the source of
truth for its content; the polished distributable PDF is rendered from it.
