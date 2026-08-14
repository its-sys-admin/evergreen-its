import { useEffect, useState } from "react";

// Shared scroll-spy for the two-column section-rail shell (extracted 2026-08-13 from the
// byte-similar copies in FieldOpsJobTracker + WeeklyReportPage; observer options preserved
// VERBATIM — this is a behavior-frozen extraction, not a redesign).
//
// Guarded: jsdom has no IntersectionObserver, and the rail is a convenience — without it the
// links still work, they just do not light up.

/** Track which of `ids` owns the viewport. Returns the best-ratio section id, or null before
 *  the first intersection (callers fall back to their first section for the initial highlight).
 *  `enabled` gates observation entirely (the tracker observes only on its detail view);
 *  `observeKey` forces re-observation when the DOM under the same ids remounts (the tracker
 *  re-observes per selected job — the sections remount on every reload). */
export function useScrollSpy(
  ids: string[],
  opts: { enabled?: boolean; observeKey?: unknown } = {},
): string | null {
  const { enabled = true, observeKey } = opts;
  const [active, setActive] = useState<string | null>(null);
  // The join is the effect key: a NEW ARRAY with the same ids must not re-observe, while a
  // changed filtered set (e.g. a capability section appearing) must.
  const idsKey = ids.join("|");
  useEffect(() => {
    if (!enabled) return;
    if (typeof IntersectionObserver === "undefined") return;
    const list = idsKey === "" ? [] : idsKey.split("|");
    const seen = new Map<string, number>();
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) seen.set(e.target.id, e.intersectionRatio);
        let best: string | null = null;
        let bestRatio = 0;
        for (const id of list) {
          const r = seen.get(id) ?? 0;
          if (r > bestRatio) { bestRatio = r; best = id; }
        }
        if (best) setActive(best);
      },
      { rootMargin: "-72px 0px -60% 0px", threshold: [0, 0.25, 0.5, 1] },
    );
    for (const id of list) {
      const el = document.getElementById(id);
      if (el) obs.observe(el);
    }
    return () => obs.disconnect();
  }, [enabled, idsKey, observeKey]);
  return active;
}
