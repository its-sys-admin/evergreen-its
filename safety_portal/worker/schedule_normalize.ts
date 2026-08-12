// ─────────────────────────────────────────────────────────────────────────────
// schedule_normalize.ts — the ONE match_key implementation (ADR-0006 decision 8).
//
// `job_schedule_tasks.match_key` exists so a schedule REVISION's rows can be matched
// back onto the living task list. Every writer of the column MUST compute it through
// this function — the PR-4 degenerate commit and the manual add/edit routes
// (worker/fieldops_schedules.ts + worker/fieldops_schedule_tasks.ts) both import it,
// and PR-6's reconcile diff engine imports THIS same function to derive the incoming
// side of its three-way diff. Two implementations would let a task written by one
// route silently never match a row keyed by the other — the exact drift class the
// shared readExpectationFields extraction killed in the manifest lane.
//
// The key is deliberately NOT unique in the schema: duplicate (section, name) pairs are
// real in the corpus, and at reconcile they become BLOCKING-ambiguous outcomes the
// human resolves — never a silent first-match (decision 9).
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Normalize one component (section or name) for matching:
 *   • NFKC — fold compatibility forms (full-width digits, ligatures) so an OCR
 *     variant of the same glyph can't split a match;
 *   • toLowerCase — the JS stand-in for casefold (schedule text is ASCII-ish;
 *     the locale-sensitive Turkish-I class doesn't appear in this vocabulary);
 *   • collapse internal whitespace runs to one space;
 *   • strip LEADING/TRAILING punctuation — OCR bar-edge/bullet debris ('|', '·',
 *     '—') and expander glyphs ride the ends of real names;
 *   • PRESERVE trailing numerals — "Fencing 2" and "Fencing 3" are distinct tasks,
 *     so the strip is punctuation-classes only, never [^a-z0-9]-greedy.
 */
function normalizeComponent(text: string): string {
  const collapsed = text.normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();
  // Strip anything that is neither a letter nor a number from each END only —
  // interior punctuation ("pre-drill", "phase 1/2") is identity and stays.
  return collapsed.replace(/^[^\p{L}\p{N}]+/u, "").replace(/[^\p{L}\p{N}]+$/u, "");
}

/**
 * The canonical match key: normalize(section) + '\n' + normalize(name). The '\n'
 * separator cannot appear in either component (whitespace is collapsed to single
 * spaces above), so the pair round-trips unambiguously. An absent section
 * normalizes to '' — a top-level task keys as '\n<name>'.
 *
 * PR-6's diff engine imports THIS — do not fork it.
 */
export function scheduleMatchKey(section: string, name: string): string {
  return `${normalizeComponent(section)}\n${normalizeComponent(name)}`;
}
