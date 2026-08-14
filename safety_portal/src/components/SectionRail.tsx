// Shared section rail for the two-column shell (extracted 2026-08-13 from the near-identical
// copies in FieldOpsJobTracker + WeeklyReportPage; markup and classes preserved verbatim per
// page via `classPrefix` — CSS untouched, this is a behavior-frozen extraction).
//
// THE ONE RULE THIS COMPONENT EXISTS TO KEEP: rail chips scroll DIRECTLY and never touch
// history. A bare fragment navigation fires popstate, and App's popstate handler bumps popEpoch
// and REMOUNTS the routed page — on the job detail that loses scroll + state, on the weekly
// report it DISCARDS an unsaved draft with no confirm (the beforeunload guard only covers real
// unloads). Live-verified on both pages before the fix; see
// docs/session_logs/2026-08-12_job-detail-design-pass-part-three.md. Every new two-column
// surface should consume THIS component rather than re-learning that defect.

export interface RailSection {
  id: string;
  label: string;
}

export function SectionRail(props: {
  sections: RailSection[];
  /** The section to highlight; pass your scroll-spy value (with any first-section fallback
   *  your page wants — the tracker falls back, the report starts on its first section). */
  activeId: string | null;
  ariaLabel: string;
  /** Per-page class family — "job-rail" (job detail) or "wpr__rail" (weekly report). */
  classPrefix: "job-rail" | "wpr__rail";
}) {
  const { sections, activeId, ariaLabel, classPrefix } = props;
  const linkClass = classPrefix === "job-rail" ? "job-rail__link" : "wpr__rail-link";
  return (
    <nav className={classPrefix} aria-label={ariaLabel}>
      {sections.map((s) => (
        <a
          key={s.id}
          className={linkClass}
          href={`#${s.id}`}
          aria-current={activeId === s.id ? "true" : undefined}
          onClick={(e) => {
            e.preventDefault();
            document.getElementById(s.id)?.scrollIntoView();
          }}
        >
          {s.label}
        </a>
      ))}
    </nav>
  );
}
