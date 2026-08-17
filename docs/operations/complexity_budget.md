---
type: operations
date: 2026-08-17
status: active
related_prs: []
workstream: null
tags: [complexity, ratchet, preservation, anti-slop, code-review]
---

# Complexity budget — extract at the touch point, never in a sweep

## Purpose

One rule, for one recurring shape:

> **When a function is already above CC 30, the next change that adds a branch to
> it extracts a dispatch table or a per-case handler FIRST, then adds the branch
> to the extracted piece.**

Not a rewrite. Not a refactor sprint. An extraction, at the moment of next
touch, paid for by the feature that would otherwise have made it worse.

### Why this rule and not "keep functions small"

"Keep functions small" is unfalsifiable advice that costs tokens and changes
nothing. This rule has a number, a trigger, and a measurement — and it fires only
on code someone is already editing, which is the only moment the cost is real and
the context is loaded.

The mechanism it targets is specific and measured. *SlopCodeBench*
(arXiv:2603.24755) found structural erosion — the share of total complexity mass
held by high-complexity functions — rises **monotonically in 80% of agent
trajectories**, and named the cause: *"current agents tend to append logic."*
Adding one more `elif` to an existing dispatcher is the cheapest correct move
available on every single occasion. That is exactly why it never stops.

ITS shows the same signature. Of the 19 functions above CC 30 at first
measurement, **17 are poll/dispatch handlers** — `_process_pending_*`,
`_service_one_*`, `_status_pass`. Every one grew a branch at a time, and every
one of those branches was individually the right call.

### Why it does NOT apply retroactively

**Do not refactor the existing roster.** Op Stds §14 preservation-over-refactor
is measurably working: verbosity measured **0.0664** against a human-panel
baseline of 0.11 and an agent-panel baseline of 0.32 — the direct product of §14
plus the "defer abstraction until ≥4 real reuse cases" rule, which together
suppress both copy-paste and speculative abstraction. A sweep through 19 large,
load-bearing daemon handlers would put that at risk to fix a number.

The roster is the baseline. It may only shrink. This rule ratchets on **new**
work.

## Procedure

1. **Before adding a branch, check the function.**

   ```bash
   python scripts/check_code_quality_metrics.py | sed -n '/CC>30 roster/,$p'
   ```

   The roster names every function above the threshold, worst first, with file
   and line. If your target is not on it, this rule does not apply — add your
   branch and move on.

2. **If it is on the roster, extract first, in its own commit.**

   The extraction is mechanical and behaviour-preserving:

   - a `dict` from the discriminator to a handler function, or
   - one function per case, with the original reduced to a lookup.

   Nothing else changes. No renames, no signature changes, no "while I'm here".
   A reviewer must be able to confirm the extraction is a no-op by reading it.

3. **Then add your branch to the extracted piece**, in a second commit.

4. **Re-measure.**

   ```bash
   python scripts/check_code_quality_metrics.py
   ```

   `functions_over_cc30` must not have risen. If the extraction was real, it
   fell.

## Examples

**In scope.** `po_materials/po_poll.py::_process_pending_po` is CC 47. A new PO
status needs handling. Extract the status dispatch to a mapping, then add the new
status as an entry.

**In scope.** `safety_reports/weekly_send.py::send_one_row` is CC 45. A new HELD
condition is required. Extract the hold checks into a sequence of predicates,
then append the new one.

**Out of scope — do not extract.** `field_ops/schedule_parse.py::_classify_tokens`
is CC 45 and you are fixing a typo in one of its messages. No branch added, no
extraction owed.

**Out of scope — do not extract.** A CC 22 function is growing a branch. Under
the threshold; add it. The ceiling that matters here is 30, and the CC>10 mass is
tracked separately by the erosion metric.

**Out of scope — do not extract.** You noticed a CC 38 function while reading
something else. Reading is not touching. Leave it.

## Validation

Two mechanical surfaces, neither of which requires anyone to remember this page:

- `scripts/check_code_quality_metrics.py` reports `functions_over_cc30` and the
  named roster.
- `.quality-ratchet.json` holds the ceiling, and
  `scripts/check_quality_ratchet.py` fails CI when the count rises. Raising the
  ceiling requires a `regression_reason`, a `tech_debt_ref`, and an `expires`
  date — the checker fails on a relaxation missing any of the three, and fails
  again once the date passes.

So the rule does not depend on review catching it. If a PR pushes a twentieth
function over CC 30, CI says so, and the only way past is an expiring, justified,
reviewable entry.

**What this deliberately does not do:** nothing detects that you added a branch
to an *already-listed* function without extracting first — the count stays at 19
and CI stays green. That case is a review question, and the honest position is
that this rule is Tier-2 judgment enforced by a Tier-1 backstop, not a fully
mechanical control. Saying otherwise would be the narrated-not-enforced pattern
(§52) in the document meant to reduce it.

## Owner

Developer-Operator. The threshold and the roster ceiling are ratchet values —
lowering the ceiling is free and needs no ceremony; raising it follows the
relaxation rule in `.quality-ratchet.json`.
