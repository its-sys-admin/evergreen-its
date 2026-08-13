// JSON nesting-depth bound for client-supplied payloads (Invariant 2).
//
// WHY: SQLite's JSON1 parser has an internal recursion limit. A `payload_json` document with
// sufficiently deep nesting ANYWHERE in it (empirically between ~500 and ~900 levels — a ~4KB
// body suffices) makes `json_type(payload_json, '$.path')` itself raise `malformed JSON` at
// DOCUMENT parse time, before any per-element guard can run. Once such a row is STORED, every
// json_extract-bearing query over that job's submissions (dailySql / crewSql / hazardSql /
// jhaSql in worker/fieldops_report.ts) 500s persistently until a human finds and deletes the
// row — a poisoning, not a transient. No read-side WHERE clause can close this, so the bound
// lives at the WRITE boundary: reject the payload before it is ever stored.
// (portal-worker-security-reviewer finding (b), 2026-08-13, live-reproduced.)
//
// The one untrusted producer is /api/submit (client-shaped `values`). The checklist-completion
// emit (fieldops_checklist.ts) synthesizes its values from validated scalars, so its depth is
// structurally bounded; buildSubmissionInsert deliberately stays guard-free (see its header).
//
// MAX_VALUES_DEPTH = 24: real form payloads nest 3-4 levels (section → repeating-table rows →
// cell objects; photo values are 2). 24 is far above any legitimate shape and far below where
// SQLite's parser degrades.

export const MAX_VALUES_DEPTH = 24;

/** True when `root` nests deeper than `maxDepth` levels. The root counts as level 1; a CONTAINER
 *  (object/array) sitting at `maxDepth` is already "too deep" — its children would exceed the
 *  bound — while a scalar leaf at `maxDepth` is fine. Iterative (explicit stack), so a hostile
 *  ten-thousand-level input cannot blow the JS call stack in the checker itself. */
export function jsonDepthExceeds(root: unknown, maxDepth: number): boolean {
  const stack: { v: unknown; d: number }[] = [{ v: root, d: 1 }];
  while (stack.length > 0) {
    const { v, d } = stack.pop()!;
    if (v === null || typeof v !== "object") continue;
    if (d >= maxDepth) return true;
    if (Array.isArray(v)) {
      for (const x of v) stack.push({ v: x, d: d + 1 });
    } else {
      for (const k of Object.keys(v)) stack.push({ v: (v as Record<string, unknown>)[k], d: d + 1 });
    }
  }
  return false;
}
