---
type: reference
date: 2026-07-14
status: active
workstream: null
tags: [documentation-corpus, tier-1]
---

# ITS Escalation Matrix

## Purpose

<!-- src: CLAUDE.md:141-179 (## Maintenance & successor-operator model) | verified 2026-07-14 -->
This is the decision reference for **who resolves an ITS fault, and when to stop and escalate.**
ITS is built to keep running after its original developer departs, so every failure has a defined
owner: a self-healing daemon, a trained operator running Claude Code, or the developer of record.
This doc paraphrases the operator-facing maintenance model from the project's canonical execution
conventions (`CLAUDE.md`, "Maintenance & successor-operator model") and cites the governing doctrine
by section number. When you are staring at a red alert, a stuck row, or a daemon that will not run,
start here.

<!-- src: CLAUDE.md:141,168 · docs/doctrine_manifest.yaml:48,89,110 | verified 2026-07-14 -->
The model is **training-bounded co-resolution**, governed by Operational Standards **v21 §§43–44**
and Foundation Mission **v11** (the Handover Plan is at **v10**). "Training-bounded" is load-bearing:
the boundary between who-resolves-solo and who-escalates is held by the operator's judgment and this
matrix, **not** by a software lock. There is no structural "non-developer-safe enforcement layer" —
none is built and none is required. That makes reading this table correctly a real safety
responsibility, not a formality.

## The Golden Rule

> ## WHEN UNSURE, ESCALATE.

<!-- src: CLAUDE.md:163-166 (both-rule; novel OR high-class → Tier 3) | verified 2026-07-14 -->
Every rule below reduces to this. A fault qualifies for solo resolution **only** when it is *both*
documented *and* low-capability-class. **Anything novel, OR anything touching a high-capability
class, escalates** — no exceptions, no judgment calls in the grey zone. Because the boundary is
enforced by training rather than by code, the cost of guessing wrong is not caught by a machine.
When two readings are plausible, the safe reading is always the one that escalates.

## Background — the three-tier model

<!-- src: CLAUDE.md:143-157 (three tiers) | verified 2026-07-14 -->
ITS recovery is layered. Most faults never reach a human at all; the ones that do are triaged by
capability class, not by severity. The three tiers are:

| Tier | Owner | What happens | Human action |
|------|-------|--------------|--------------|
| **Tier 1 — self-heal** | The daemons + watchdog | Interval daemons recover on their own via launchd re-invocation; watchdog **Check C** catches a stale daemon; the external **Healthchecks.io** ping is the intended dead-man's switch for total-host death — **not armed yet**, so total-host death is currently silent | **None** (until the beacon is armed — see below) |
| **Tier 2 — Claude-assisted repair** | The **Successor-Operator** | A trained operator runs Claude Code, follows the matching `§43` runbook, and performs a **low-capability-class** repair | Operator acts, with Claude driving from the runbook |
| **Tier 3 — escalate** | The **Developer-Operator (Seth)** | The fault is novel or high-class; it is handed up to the developer of record | Escalate + co-resolve |

<!-- src: CLAUDE.md:146-151 (Tier 1 self-heal mechanics) | verified 2026-07-14 -->
**Tier 1 (self-heal)** is the default and covers the vast majority of transient faults. Interval
daemons are one-shot-per-`StartInterval` under launchd, so a crashed cycle is simply re-invoked on
the next interval. Watchdog **Check C** enforces a marker-file staleness floor across all tracked
jobs (`TRACKED_JOBS`), and the external **Healthchecks.io** heartbeat is the intended dead-man's
switch for whole-host death (the watchdog cannot alert about its own death). **That beacon is not
armed** — `scripts/watchdog.py` skips the ping while `system.heartbeat_url` holds its seed
placeholder, so today a total-host death produces no external alert at all. Arming it is a config
change (VC-09 requires an https URL). No human acts at Tier 1.

<!-- src: CLAUDE.md:152-155 (Tier 2 Successor-Operator scope) | verified 2026-07-14 -->
**Tier 2 (Claude-assisted repair)** is owned by the **Successor-Operator** — a *trained* operator
who runs Claude Code and reads Smartsheet rows plus alert emails, but who is **not** a developer.
The Successor-Operator writes no code, does no §§37–41 developer operations, and touches no
secrets or the macOS Keychain. Their toolkit is the documented, low-capability-class repair: re-run
a daemon, toggle a documented `ITS_Config` value, re-send an approval, re-seed a row, or clear a
stuck lock.

<!-- src: CLAUDE.md:156-157 (Tier 3 Developer-Operator) | verified 2026-07-14 -->
**Tier 3 (escalate)** hands the fault to the **Developer-Operator (Seth)** — a reachable escalation
asset, deliberately *not* the day-to-day operator. Tier 3 is where every novel or high-class fault
lands.

## The two named roles

<!-- src: CLAUDE.md:159-161 (two named roles) | verified 2026-07-14 -->
Every unqualified use of "operator" in ITS docs resolves to exactly one of two roles. Knowing which
one you are determines which faults are yours to resolve.

| Role | Who | Capability | Owns |
|------|-----|------------|------|
| **Developer-Operator (Seth)** | The developer of record | git / Claude Code / shell / worktree-fluent; all §§37–41 operations; Keychain access; code changes | Tier 3, and every high-capability-class fault |
| **Successor-Operator** | A trained non-developer operator | Runs Claude Code; reads Smartsheet + alert email; **no** code, **no** secrets, **no** Keychain | Tier 2 — documented, low-class repairs only |

## Behavior — the both-rule

<!-- src: CLAUDE.md:163-166 (both-rule + FIXED high-class categories) | verified 2026-07-14 -->
The Tier-2 / Tier-3 boundary is a single logical test called **the both-rule**. A fault is
Tier-2-eligible — i.e. the Successor-Operator may resolve it solo — **only if BOTH conditions hold**:

1. It is **documented** — there is a matching `§43` runbook entry describing the repair.
2. It is **low-capability-class** — it does not touch any of the four fixed high-capability classes.

If either condition fails — **novel OR high-class** — the fault escalates to the Developer-Operator.
"Novel" and "high-class" are independent trapdoors: a documented fault that turns out to touch a
high class still escalates; an otherwise-low-class symptom that isn't in any runbook still escalates.

```text
                      ┌─────────────────────────────┐
                      │  A fault surfaces (alert,    │
                      │  stuck row, dead daemon)     │
                      └──────────────┬──────────────┘
                                     │
                     ┌───────────────▼────────────────┐
                     │ Is it DOCUMENTED?               │
                     │ (a §43 runbook entry exists)    │
                     └───────┬──────────────────┬──────┘
                          NO │                  │ YES
                             │                  │
                             │       ┌──────────▼───────────────────┐
                             │       │ Is it LOW-capability-class?   │
                             │       │ (none of the 4 fixed classes) │
                             │       └──────┬─────────────────┬──────┘
                             │           NO │                 │ YES
                             ▼              ▼                 ▼
                   ┌───────────────────────────┐   ┌───────────────────────┐
                   │   NOVEL or HIGH-CLASS      │   │  DOCUMENTED and LOW.   │
                   │   → TIER 3                 │   │  → TIER 2              │
                   │   Escalate + co-resolve    │   │  Successor-Operator    │
                   │   with the                 │   │  resolves solo, Claude │
                   │   Developer-Operator (Seth)│   │  driving from the §43  │
                   └───────────────────────────┘   │  runbook               │
                                                    └───────────────────────┘
```

### The four FIXED high-capability classes (always escalate)

<!-- src: CLAUDE.md:164-166 (four FIXED high-class categories) | verified 2026-07-14 -->
Four capability classes are **fixed** and **always escalate to the Developer-Operator**, regardless
of whether a runbook documents the symptom. A runbook entry can *describe* a high-class fault — the
best §43 entries do, so the Successor-Operator can *recognize, confirm, and escalate* — but it can
never make one solo-resolvable. These are the non-negotiable trapdoors:

| # | Fixed high-capability class | Why it always escalates |
|---|-----------------------------|-------------------------|
| 1 | **External Send Gate** | Any change to the human-approval-before-send boundary (FM Invariant 1). The gate is the real security boundary; touching it is developer-only. |
| 2 | **Secrets / auth** | Keychain secrets, OAuth tokens, API keys, portal/admin tokens. Rotation is a Developer-Operator ceremony, never a Tier-2 move. |
| 3 | **Doctrine** | Changing a canonical rule, or flipping a gate whose activation would contradict documented doctrine. A doctrine-divergent action is a doctrine action. |
| 4 | **Code changes** | Editing any Python/TypeScript source, running a migration, or a `wrangler`/Cloudflare deploy. Writing code is definitionally out of Tier-2 scope. |

<!-- src: docs/runbooks/box_token_freshness.md:28-29 · docs/runbooks/token_write_capability.md:26-28 | verified 2026-07-14 -->
For the secrets/auth class especially, the Successor-Operator's entire job on such a fault is to
**recognize it, confirm the evidence, and escalate** — never to touch the token itself. The Box-token
and Smartsheet-token runbooks state this explicitly: the operator surfaces the symptom to the
Developer-Operator, who then rotates or re-scopes the credential.

### What "documented" means

<!-- src: docs/runbooks/README.md:1-16 (§43 runbook set; four-part shape) | verified 2026-07-14 -->
"Documented" has a precise meaning: there is a **Successor-Remediation runbook entry** (Op Stds §43)
for the capability, living in `docs/runbooks/`. Each entry is written for the Successor-Operator — a
trained operator who reads rows and alert emails but not code — and follows the §43 four-part shape:

1. **Symptom** — the observable signal (an alert, a stuck row, a "held" status).
2. **What the Successor-Operator checks** — the evidence to confirm.
3. **The Claude prompt or UI action** — the low-class repair to run.
4. **The escalate-to-Seth condition** — the explicit boundary, stated in observable terms.

<!-- src: docs/runbooks/README.md:18-57 (AUTO-INDEX of live §43 entries) · CLAUDE.md:175-179 (§43 document-as-you-build DoD) | verified 2026-07-14 -->
The runbook set is a live, growing index (see `docs/runbooks/README.md` for the current roster —
roughly three dozen entries at this writing, spanning safety, portal, progress, PO, subcontracts,
field-ops, and infrastructure). Shipping a §43 entry is **definition-of-done** for any capability
with a Tier-2-reachable failure mode: symptom, low-class repair steps, and the explicit
escalate-to-Seth boundary. Where a code-level `§42` docstring records *why the code is the way it
is* (developer audience), a `§43` runbook records *what the Successor-Operator does when it
misbehaves* (trained-operator audience).

### Training-enforced, not structurally enforced

<!-- src: CLAUDE.md:168-173 (training-enforced reframe) | verified 2026-07-14 -->
The both-rule is upheld by **training and judgment, not by a software lock.** This is deliberate and
canonical (Op Stds v21 §44 / FM v11 reframe). The verified-in-code capability gating (Invariant 1,
`tests/test_capability_gating.py`) and the `.claude/hooks` guards exist to protect *developer and
subagent* sessions — and they **fail open** for the operator's own session, so they do *not* confine
a Tier-2 repair. Nothing in the running system will physically stop a Successor-Operator from
overreaching. The boundary holds only by the operator's judgment, the both-rule, and co-resolution
with the Developer-Operator on the four high-class categories until per-category clearance is
granted. This is exactly why the Golden Rule matters.

## What the Successor-Operator resolves solo

<!-- src: CLAUDE.md:152-155 (low-class Tier-2 repair examples) | verified 2026-07-14 -->
When — and only when — a fault is **both documented and low-capability-class**, the
Successor-Operator resolves it solo, with Claude driving from the matching runbook. The canonical
low-class repair kit is small and bounded:

| Solo repair | What it is | Class |
|-------------|------------|-------|
| **Re-run a daemon** | Kick a stalled interval daemon (unload/reload, or re-invoke a cycle) per its runbook | Low |
| **Toggle a documented `ITS_Config` value** | Flip a gate/interval whose runbook authorizes the change — after reading the row's full Description first | Low |
| **Re-send an approval** | Re-drive a human-approved row that failed to send transiently | Low |
| **Re-seed a row** | Recreate a missing tracking/config row per the runbook | Low |
| **Clear a stuck lock** | Release a stale state-file lock so a daemon can proceed | Low |

<!-- src: docs/runbooks/config_actuator.md:72,84,89 (documented low-class gate flip is conditional) | verified 2026-07-14 -->
Even a solo repair can carry a precondition. A documented `ITS_Config` gate flip is Tier-2 **only if
the runbook's stated preconditions are already met and activation was already Developer-Operator-
approved**. If activation *itself* is the open question — for example, whether a Worker has been
deployed or a secret provisioned — that is the fixed high-class "code changes / secrets" category,
and it escalates. Always read a gate row's **full Description** before flipping it: the Description
can carry an explicit doctrine precondition, and honoring the *decision* does not release the
*documented precondition*.

## Symptom → class quick table

<!-- src: docs/runbooks/README.md + individual runbooks (per-row src below) | verified 2026-07-14 -->
The following maps common symptoms to their resolution class. The two class labels are **role-based**:
*Successor-Operator — solo* (documented AND low-class) and *Escalate → co-resolve with the
Developer-Operator* (novel OR high-class). The troubleshooting tree's class badges link back to these
same two classes.

| Symptom | Class | Resolution | Documented at |
|---------|-------|------------|---------------|
| A daemon is stale / not running (transient) | **Successor-Operator — solo** | Re-run per the daemon's runbook; confirm Check C recovers it | (per-daemon §43 runbook) |
| A stuck state-file lock blocks a daemon | **Successor-Operator — solo** | Clear the stale lock per the runbook | (per-daemon §43 runbook) |
| A missing tracking/config row | **Successor-Operator — solo** | Re-seed the row per the runbook | (per-workstream §43 runbook) |
| A documented `ITS_Config` gate needs flipping, preconditions met | **Successor-Operator — solo** | Toggle after reading the row's full Description | <!-- src: docs/runbooks/config_actuator.md:72 --> config_actuator.md |
| A human-approved send failed transiently | **Successor-Operator — solo** | Re-send the approved row per the runbook | (per-send §43 runbook) |
| Circuit breaker open — transient storm, documented cause | **Successor-Operator — solo** | Follow the breaker-clear runbook step (delete the stale local breaker state file — an explicitly low-capability-class action) | <!-- src: docs/runbooks/circuit_breaker.md:78-81 (low-class clear step) --> circuit_breaker.md |
| Circuit breaker open — root cause is high-class | **Escalate → co-resolve** | Recognize, confirm, escalate | <!-- src: docs/runbooks/circuit_breaker.md:85-94 (breaker-open Escalate-to-Seth condition) --> circuit_breaker.md |
| Box OAuth token stale / near expiry | **Escalate → co-resolve** | Recognize + confirm + escalate; developer rotates the token | <!-- src: docs/runbooks/box_token_freshness.md:28-29,77 --> box_token_freshness.md |
| Smartsheet token cannot write | **Escalate → co-resolve** | Confirm the symptom, then escalate; developer re-scopes | <!-- src: docs/runbooks/token_write_capability.md:26-28,68 --> token_write_capability.md |
| A send row stuck HELD / "contamination" guard fired | **Escalate → co-resolve** | External Send Gate territory — escalate regardless of docs | <!-- src: docs/runbooks/safety_weekly_send.md:25-27 --> safety_weekly_send.md |
| A live D1 migration or `wrangler` deploy is needed | **Escalate → co-resolve** | Deploy/secrets surface — never a Tier-2 move | <!-- src: docs/runbooks/config_actuator.md:84 --> config_actuator.md |
| A gate flip would contradict documented doctrine | **Escalate → co-resolve** | Doctrine is a fixed high class — escalate | <!-- src: CLAUDE.md:165-166 --> CLAUDE.md (both-rule) |
| Any secret / Keychain / auth-token change | **Escalate → co-resolve** | Fixed high class (secrets/auth) — escalate | <!-- src: CLAUDE.md:165 --> CLAUDE.md (four fixed classes) |
| Any source-code edit | **Escalate → co-resolve** | Fixed high class (code changes) — escalate | <!-- src: CLAUDE.md:166 --> CLAUDE.md (four fixed classes) |
| **A symptom not in any runbook (novel)** | **Escalate → co-resolve** | Novel always escalates, even if it looks low-class | <!-- src: CLAUDE.md:164 --> CLAUDE.md (both-rule) |

<!-- src: docs/runbooks/circuit_breaker.md:85,127 · box_token_freshness.md:69-77 | verified 2026-07-14 -->
Note the two circuit-breaker rows and the "recognize + confirm + escalate" phrasing on the
credential rows: the same symptom can resolve to different classes depending on **root cause and
capability class**, which is precisely why the both-rule tests the class, not the severity. A
runbook that documents a high-class fault does so to help the Successor-Operator *confirm and
escalate faster*, never to authorize a solo fix.

## Edge cases & limitations

<!-- src: CLAUDE.md:168-173 (no structural enforcement) | verified 2026-07-14 -->
- **No machine will stop an overreach.** The both-rule is training-enforced. The in-code capability
  gating and hooks fail open for the operator's own session by design, so a wrong reading of this
  matrix is not caught by software. This is the single most important limitation to internalize.
- **"Documented" is necessary but not sufficient.** A `§43` entry existing does not make a fault
  solo-resolvable — it must *also* be low-class. High-class faults are documented specifically so
  they can be recognized and escalated, not resolved.
<!-- src: docs/runbooks/config_actuator.md:72,84 (conditional gate flip) | verified 2026-07-14 -->
- **A low-class repair can hide a high-class precondition.** A gate flip that looks like a simple
  `ITS_Config` toggle can require a prior deploy or secret (high class). Read the row's full
  Description; when activation itself is the question, escalate.
<!-- src: CLAUDE.md:164 (novel escalates) | verified 2026-07-14 -->
- **Grey-zone reading resolves to escalation.** When two class readings are both plausible, the
  correct move is the Golden Rule: escalate. Per-category clearance from the Developer-Operator can
  later widen what is solo-resolvable, but until then the conservative reading wins.
<!-- src: docs/doctrine_manifest.yaml:48,54,89,110 (doctrine versions are the source of truth) | verified 2026-07-14 -->
- **Doctrine versions drift; verify them.** This matrix paraphrases Op Stds v21 §§43–44 and FM v11
  (Handover v10) as recorded in `docs/doctrine_manifest.yaml`. If a future doctrine bump renumbers
  or reframes the tiers, the manifest is the source of truth — re-verify against it, not against a
  cached memory of this page.

## Related docs

- [`system_architecture.md`](system_architecture.md) — the layered architecture the tiers recover.
- [`daemon_reference.md`](daemon_reference.md) — the daemons + watchdog checks that make Tier 1 self-heal.
- [`data_model_reference.md`](data_model_reference.md) — `ITS_Config`, `ITS_Errors`, and the tracking sheets a Tier-2 repair reads and re-seeds.
- [`integration_reference.md`](integration_reference.md) — the Smartsheet / Box / Graph / Cloudflare boundaries behind the secrets/auth class.
- [`security_trust_model.md`](security_trust_model.md) — the External Send Gate and Invariant-2 defenses that anchor two of the four fixed high classes.
- [`glossary.md`](glossary.md) — definitions for Successor-Operator, Developer-Operator, capability class, and the both-rule.
- [`documentation_index.md`](documentation_index.md) — the master index of this Tier-1 reference set.
- `docs/runbooks/README.md` (execution repo) — the live §43 Successor-Remediation runbook roster that defines "documented."
