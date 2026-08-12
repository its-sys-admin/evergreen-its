// Weekly Production Report — the office screen (0067).
//
// This is where the office supplies the three sections D1 structurally cannot derive (the six
// OSHA case counts, labor-by-company man-hours, pending RFIs/submittals/COs) and makes the two
// judgment calls the machine must not make for them: which days count as INCLEMENT WEATHER (a
// contractual delay claim, never inferred from a conditions string) and which photos represent
// the week to a client.
//
// Everything else on the page is READ-ONLY context, shown because the office is reviewing a
// document, not filling a form in the dark: the week's weather as the field reported it, the
// crews that actually worked, the delivery ledger, the material problems, the schedule state.
//
// TWO RULES THE LAYOUT ENCODES:
//
//  1. **Carried-forward values are marked.** A week with no saved row inherits the most recent
//     earlier one; the banner says so and the affected fields are badged, so "reviewed and
//     unchanged" never masquerades as "nobody looked".
//  2. **The narrative textareas are PRE-FILLED with the assembled seed.** `saved` is row-level:
//     the moment the office saves anything, an untouched narrative field renders BLANK on the
//     report rather than falling back to the derived text. Pre-filling is what makes the derived
//     assembly reach the document — a live mock render produced two empty narrative sections
//     precisely because a hand-seeded row had saved-empty narrative.
//
// ── LAYOUT (2026-08 design pass) ──────────────────────────────────────────────────────────
// This page shipped referencing fifteen `wr__*` class names, NONE of which were ever defined in
// global.css. It rendered as raw unstyled HTML — bare UA widgets, labels running into their
// inputs, no card, no grid — and it was the only page in the portal that skipped PageShell, so
// it had no app header and no sign-out. That is what "extremely rough" meant, and it was a
// missing stylesheet rather than a layout opinion.
//
// The rebuild adopts PageShell and puts the page on three ideas:
//
//   • AUTHORSHIP IS THE DESIGN. Every section is tagged YOURS TO FILL or FROM THE FIELD, and
//     derived sections sit on the receding page ground with no white input wells — so "nothing
//     for you to do here" is legible before a word is read. This page's real difficulty is not
//     length, it is knowing which numbers are yours to own.
//   • A STICKY SECTION RAIL, because eight sections of a five-page document need an index. It
//     is a vertical rail on a laptop and a horizontal chip scroller on a phone.
//   • A STICKY SAVE BAR that states whether anything is unsaved. The save button used to be a
//     lone control below eight sections of scroll, with nothing anywhere saying the form was
//     dirty.
//
// Materials integration (2026-08): the payload already carried nine delivery fields and a
// material-incidents array. The page rendered four delivery fields and no incidents at all, so
// both are widened here — no Worker change, no wire change; the data was already arriving.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  SAFETY_ROWS,
  fetchWeeklyReport,
  saveWeeklyReport,
  weekEndFor,
  weekStartFor,
  type ProductionReportResponse,
  type WeeklyReportLaborRow,
  type WeeklyReportPhoto,
} from "../lib/fieldops_report";
import { PageShell } from "../components/PageShell";

interface Props {
  jobId: string;
  onBack: () => void;
  /** Optional so the page keeps its two-prop test signature; App passes it for the home strip. */
  onHome?: () => void;
}

type Draft = {
  site_location: string;
  ess_management: string;
  mobilization_date: string;
  subcontractors: string;
  prepared_by: string;
  safety: Record<string, { month: string; to_date: string }>;
  inclement: Set<string>;
  weather_days_to_date: string;
  labor: WeeklyReportLaborRow[];
  critical_items: string;
  upcoming_activities: string;
  rfis: string;
  submittals: string;
  ifc_review: string;
  change_orders: string;
  photos: WeeklyReportPhoto[] | null;
};

/** The section index. Order here IS the order on the page and in the rail, and it follows the
 *  printed report's five pages. */
const SECTIONS: { id: string; label: string }[] = [
  { id: "wpr-header", label: "Header" },
  { id: "wpr-safety", label: "Safety" },
  { id: "wpr-weather", label: "Weather" },
  { id: "wpr-labor", label: "Labor" },
  { id: "wpr-progress", label: "Progress" },
  { id: "wpr-photos", label: "Photos" },
  { id: "wpr-materials", label: "Materials" },
  { id: "wpr-pending", label: "Pending" },
];

/** The deterministic seeds the report would use for an UNSAVED week — mirrored here so the
 *  textareas start from the same text the compile would have assembled. Kept deliberately simple
 *  and identical in spirit to `wpr_data._assemble_*`; the Python side remains authoritative for
 *  what actually renders. */
function seedNarrative(d: ProductionReportResponse): { critical: string; upcoming: string } {
  const lines: string[] = [];
  // Behind-schedule tasks lead, worst slip first — the Worker already ordered them. This MUST
  // match wpr_data._assemble_critical_items: the screen's whole job is to show what the report
  // will render, so a divergence here would pre-fill text the compile would never have produced.
  for (const t of d.schedule?.behind ?? []) {
    const head = t.section ? `${t.section}: ${t.name}` : t.name;
    const kind = t.is_contract_milestone ? "Contract milestone behind schedule" : "Behind schedule";
    lines.push(`${kind} — ${head} (due ${t.finish_date}, ${t.percent}% complete)`);
  }
  for (const inc of d.material_incidents) {
    const head = [inc.material, inc.issue].filter(Boolean).join(": ");
    if (head) lines.push(inc.details ? `${head} — ${inc.details}` : head);
  }
  for (const n of d.daily_notes) if (n.comments.trim()) lines.push(n.comments.trim());
  const goals = d.daily_notes.map((n) => n.tomorrows_goals.trim()).filter(Boolean);
  return {
    critical: [...new Set(lines)].slice(0, 12).join("\n"),
    upcoming: goals.length ? goals[goals.length - 1] : "",
  };
}

function toDraft(d: ProductionReportResponse): Draft {
  const o = d.office;
  const seed = seedNarrative(d);
  const safety: Draft["safety"] = {};
  for (const { key } of SAFETY_ROWS) {
    const cur = o.safety[key] ?? { month: 0, to_date: 0 };
    safety[key] = { month: String(cur.month), to_date: String(cur.to_date) };
  }
  return {
    site_location: o.header.site_location || [d.job?.address_city, d.job?.address_state].filter(Boolean).join(", "),
    ess_management: o.header.ess_management,
    mobilization_date: o.header.mobilization_date,
    subcontractors: o.header.subcontractors.join(", "),
    prepared_by: o.header.prepared_by,
    safety,
    inclement: new Set(o.weather.inclement_dates),
    weather_days_to_date: String(o.weather.weather_days_to_date),
    // Seed the labor table from the crews the field reported when the office has no table yet.
    labor: o.labor.rows.length
      ? o.labor.rows
      : d.labor.crews.map((c) => ({
          company: c.company,
          workers: c.workers ? String(c.workers) : "",
          // Blank on purpose: hours cannot be attributed to a company (no employer column).
          man_hours: "",
        })),
    critical_items: o.saved ? o.narrative.critical_items : seed.critical,
    upcoming_activities: o.saved ? o.narrative.upcoming_activities : seed.upcoming,
    rfis: o.pending.rfis,
    submittals: o.pending.submittals,
    ifc_review: o.pending.ifc_review,
    change_orders: o.pending.change_orders,
    photos: o.photos,
  };
}

/** A stable string for the whole draft, so "is anything unsaved" is one comparison. The Set is
 *  sorted first — membership is what matters, insertion order is not. */
function draftKey(d: Draft): string {
  return JSON.stringify({ ...d, inclement: [...d.inclement].sort() });
}

/** Shift a Saturday week-start by whole weeks, staying on Saturday. */
function shiftWeek(weekStart: string, weeks: number): string {
  const d = new Date(`${weekStart}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + weeks * 7);
  return d.toISOString().slice(0, 10);
}

/** Delivery mark → chip copy. The ledger's three kinds read as what happened, not as codes. */
function deliveryKindChip(kind: string): { label: string; className: string } {
  switch (kind) {
    case "delivered":
      return { label: "Delivered", className: "dash-pill dash-pill--ok" };
    case "partial":
      return { label: "Partial", className: "dash-pill dash-pill--warn" };
    case "not_delivered":
      return { label: "Not delivered", className: "dash-pill dash-pill--danger" };
    default:
      return { label: kind || "—", className: "dash-pill" };
  }
}

export function WeeklyReportPage({ jobId, onBack, onHome }: Props) {
  const [weekStart, setWeekStart] = useState(() => weekStartFor(new Date()));
  const [data, setData] = useState<ProductionReportResponse | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [baseline, setBaseline] = useState<string>("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");
  const [active, setActive] = useState<string>(SECTIONS[0].id);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async (ws: string) => {
    setErr(""); setSaved(""); setData(null); setDraft(null);
    try {
      const d = await fetchWeeklyReport(jobId, ws);
      const next = toDraft(d);
      setData(d); setDraft(next); setBaseline(draftKey(next));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not load the weekly report.");
    }
  }, [jobId]);

  useEffect(() => { void load(weekStart); }, [load, weekStart]);

  const dirty = draft !== null && draftKey(draft) !== baseline;

  // Leaving with unsaved edits loses a week of the office's work. The browser's own prompt is
  // the only one that can block a tab close, so it is what this uses.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  // Scroll-spy for the rail. Guarded: jsdom has no IntersectionObserver, and the rail is a
  // convenience — without it the links still work, they just do not light up.
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined" || !bodyRef.current) return;
    const seen = new Map<string, number>();
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) seen.set(e.target.id, e.intersectionRatio);
        let best: string | null = null;
        let bestRatio = 0;
        for (const s of SECTIONS) {
          const r = seen.get(s.id) ?? 0;
          if (r > bestRatio) { bestRatio = r; best = s.id; }
        }
        if (best) setActive(best);
      },
      { rootMargin: "-72px 0px -60% 0px", threshold: [0, 0.25, 0.5, 1] },
    );
    for (const s of SECTIONS) {
      const el = document.getElementById(s.id);
      if (el) obs.observe(el);
    }
    return () => obs.disconnect();
  }, [data]);

  // The photo list the report will use: the office's explicit picks, else the server's spread.
  const selectedIds = useMemo(
    () => new Set((draft?.photos ?? data?.photos.selected ?? []).map((p) => p.pool_id)),
    [draft?.photos, data],
  );

  const backBar = (
    <div className="dash-back-btn">
      <button type="button" className="btn btn--secondary" onClick={onBack}>
        ← Back to job
      </button>
    </div>
  );

  if (err) {
    return (
      <PageShell onHome={onHome} wide>
        {backBar}
        <p className="login__error" role="alert">{err}</p>
        <button type="button" className="btn btn--secondary" onClick={() => void load(weekStart)}>
          Retry
        </button>
      </PageShell>
    );
  }
  if (!data || !draft) {
    return (
      <PageShell onHome={onHome} wide>
        {backBar}
        <div aria-busy="true" aria-label="Loading the weekly report">
          <div className="sched-skel" />
          <div className="sched-skel" />
          <div className="sched-skel" />
        </div>
      </PageShell>
    );
  }

  const carried = data.office.carried_from;
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft({ ...draft, [k]: v });

  const togglePhoto = (p: WeeklyReportPhoto) => {
    const base = draft.photos ?? data.photos.selected;
    const next = selectedIds.has(p.pool_id)
      ? base.filter((x) => x.pool_id !== p.pool_id)
      : [...base, p];
    set("photos", next);
  };

  const save = async () => {
    setBusy(true); setErr(""); setSaved("");
    try {
      await saveWeeklyReport({
        job_id: jobId,
        week_start: weekStart,
        header: {
          site_location: draft.site_location,
          ess_management: draft.ess_management,
          mobilization_date: draft.mobilization_date,
          subcontractors: draft.subcontractors.split(",").map((s) => s.trim()).filter(Boolean),
          prepared_by: draft.prepared_by,
        },
        safety: Object.fromEntries(
          SAFETY_ROWS.map(({ key }) => [key, {
            month: Number(draft.safety[key]?.month || 0),
            to_date: Number(draft.safety[key]?.to_date || 0),
          }]),
        ),
        weather: {
          inclement_dates: [...draft.inclement].sort(),
          weather_days_to_date: Number(draft.weather_days_to_date || 0),
        },
        labor: { rows: draft.labor.filter((r) => r.company.trim()) },
        narrative: {
          critical_items: draft.critical_items,
          upcoming_activities: draft.upcoming_activities,
          // PASS THE STORED VALUE BACK, never a literal []. This screen has no editor for
          // hazard_topics, and it used to hard-code an empty list here — so every save from
          // this page silently wiped whatever was in the field. Nothing on screen said so,
          // and nothing would have: it is not rendered anywhere. A field a page cannot edit
          // is a field that page must not clear.
          hazard_topics: data.office.narrative.hazard_topics ?? [],
        },
        pending: {
          rfis: draft.rfis, submittals: draft.submittals,
          ifc_review: draft.ifc_review, change_orders: draft.change_orders,
        },
        // Omit `photos` entirely while the office has not curated — that preserves auto-select.
        // Sending [] would mean "no photos this week", a different and deliberate state.
        ...(draft.photos === null ? {} : { photos: draft.photos }),
      });
      setSaved("Saved. Tick Compile Now on the job's week sheet to rebuild the PDF.");
      await load(weekStart);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <PageShell onHome={onHome} wide>
      {backBar}

      <p className="page__heading--eyebrow">Weekly production report</p>
      <h1 className="page__heading">{data.job?.project_name ?? jobId}</h1>

      <div className="wpr-week">
        <button
          type="button"
          className="wpr-week__nav"
          aria-label="Previous week"
          onClick={() => setWeekStart((w) => shiftWeek(w, -1))}
        >
          ←
        </button>
        <span className="wpr-week__range">
          {weekStart} → {weekEndFor(weekStart)}
        </span>
        <button
          type="button"
          className="wpr-week__nav"
          aria-label="Next week"
          onClick={() => setWeekStart((w) => shiftWeek(w, 1))}
        >
          →
        </button>
        <label className="wpr-field__label" style={{ marginBottom: 0 }}>
          Week starting (Saturday)
          <input
            type="date"
            className="wpr-week__input"
            value={weekStart}
            onChange={(e) =>
              e.target.value && setWeekStart(weekStartFor(new Date(`${e.target.value}T12:00:00`)))
            }
          />
        </label>
      </div>

      {carried && (
        <p className="wpr-banner" role="status">
          <span>
            These values were carried forward from the week of <strong>{carried}</strong>. Nothing
            has been saved for this week yet — review them and save.
          </span>
        </p>
      )}
      {data.daily_report_count === 0 && (
        <p className="wpr-banner wpr-banner--warn" role="status">
          No daily reports were filed for this week. As it stands the compile will HOLD this week
          rather than send a report — get the dailies filed, or decide with the office.
        </p>
      )}
      {saved && <p className="wpr-banner wpr-banner--ok" role="status">{saved}</p>}

      <div className="wpr">
        <nav className="wpr__rail" aria-label="Report sections">
          {SECTIONS.map((s) => (
            <a
              key={s.id}
              className="wpr__rail-link"
              href={`#${s.id}`}
              aria-current={active === s.id ? "true" : undefined}
            >
              {s.label}
            </a>
          ))}
        </nav>

        <div className="wpr__body" ref={bodyRef}>
          {/* ── page 1: header + safety ─────────────────────────────────────────── */}
          <section className="wpr-sec" id="wpr-header">
            <header className="wpr-sec__head">
              <h2 className="wpr-sec__title">Report header</h2>
              <span className="wpr-sec__tag wpr-sec__tag--yours">Yours to fill</span>
              {carried && <span className="wpr-carried">Carried forward</span>}
            </header>
            <div className="wpr-grid">
              <label className="wpr-field">
                <span className="wpr-field__label">Site location</span>
                <input className="wpr-field__input" value={draft.site_location}
                       onChange={(e) => set("site_location", e.target.value)} />
              </label>
              <label className="wpr-field">
                <span className="wpr-field__label">ESS management</span>
                <input className="wpr-field__input" value={draft.ess_management}
                       onChange={(e) => set("ess_management", e.target.value)} />
              </label>
              <label className="wpr-field">
                <span className="wpr-field__label">Mobilization date</span>
                <input type="date" className="wpr-field__input" value={draft.mobilization_date}
                       onChange={(e) => set("mobilization_date", e.target.value)} />
              </label>
              <label className="wpr-field">
                <span className="wpr-field__label">Prepared by</span>
                <input className="wpr-field__input" value={draft.prepared_by}
                       onChange={(e) => set("prepared_by", e.target.value)} />
              </label>
              <label className="wpr-field wpr-grid__wide">
                <span className="wpr-field__label">Subcontractors (comma separated)</span>
                <input className="wpr-field__input" value={draft.subcontractors}
                       onChange={(e) => set("subcontractors", e.target.value)} />
              </label>
            </div>
          </section>

          <section className="wpr-sec" id="wpr-safety">
            <header className="wpr-sec__head">
              <h2 className="wpr-sec__title">Project safety status</h2>
              <span className="wpr-sec__tag wpr-sec__tag--yours">Yours to fill</span>
              {carried && <span className="wpr-carried">Carried forward</span>}
            </header>
            <p className="wpr-sec__hint">
              Incident reports do not carry an OSHA case classification, so these counts cannot be
              derived — they are the office&apos;s record.
            </p>
            <table className="wpr-table">
              <thead>
                <tr><th>Site safety record</th><th>This month</th><th>Project to date</th></tr>
              </thead>
              <tbody>
                {SAFETY_ROWS.map(({ key, label }) => (
                  <tr key={key}>
                    <td><strong>{label}</strong></td>
                    <td data-cell="This month">
                      <input type="number" min={0} inputMode="numeric" className="wpr-num"
                             aria-label={`${label} this month`}
                             value={draft.safety[key].month}
                             onChange={(e) => set("safety", { ...draft.safety, [key]: { ...draft.safety[key], month: e.target.value } })} />
                    </td>
                    <td data-cell="Project to date">
                      <input type="number" min={0} inputMode="numeric" className="wpr-num"
                             aria-label={`${label} project to date`}
                             value={draft.safety[key].to_date}
                             onChange={(e) => set("safety", { ...draft.safety, [key]: { ...draft.safety[key], to_date: e.target.value } })} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.hazard_form_codes.length > 0 && (
              <p className="wpr-sec__hint">
                Safety-meeting topics are derived from the {data.hazard_form_codes.length} toolbox
                talk(s) / JHA(s) filed this week.
              </p>
            )}
          </section>

          {/* ── page 2: weather + labor ─────────────────────────────────────────── */}
          <section className="wpr-sec" id="wpr-weather">
            <header className="wpr-sec__head">
              <h2 className="wpr-sec__title">Weather</h2>
              <span className="wpr-sec__tag wpr-sec__tag--yours">Yours to mark</span>
            </header>
            <p className="wpr-sec__hint">
              Conditions and temperature come from the daily reports. A <strong>weather day</strong> is
              a delay claim, so it is yours to mark — never inferred from the conditions text.
            </p>
            <table className="wpr-table">
              <thead>
                <tr><th>Date</th><th>Conditions</th><th>Avg temp</th><th>Weather day</th></tr>
              </thead>
              <tbody>
                {data.weather.days.length === 0 && (
                  <tr><td colSpan={4}>No daily reports filed this week.</td></tr>
                )}
                {data.weather.days.map((d) => (
                  <tr key={d.work_date}>
                    <td data-cell="Date"><strong>{d.work_date}</strong></td>
                    <td data-cell="Conditions">{d.conditions || "—"}</td>
                    <td data-cell="Avg temp">{d.avg_temp || "—"}</td>
                    <td>
                      <label className="wpr-daymark">
                        <input type="checkbox" checked={draft.inclement.has(d.work_date)}
                               aria-label={`Weather day on ${d.work_date}`}
                               onChange={() => {
                                 const n = new Set(draft.inclement);
                                 if (n.has(d.work_date)) n.delete(d.work_date);
                                 else n.add(d.work_date);
                                 set("inclement", n);
                               }} />
                        Weather day
                      </label>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <label className="wpr-field" style={{ maxWidth: "22rem", marginTop: "var(--space-4)" }}>
              <span className="wpr-field__label">Total weather days to date</span>
              <input type="number" min={0} inputMode="numeric" className="wpr-num"
                     value={draft.weather_days_to_date}
                     onChange={(e) => set("weather_days_to_date", e.target.value)} />
            </label>
          </section>

          <section className="wpr-sec" id="wpr-labor">
            <header className="wpr-sec__head">
              <h2 className="wpr-sec__title">Construction labor report</h2>
              <span className="wpr-sec__tag wpr-sec__tag--yours">Yours to fill</span>
            </header>
            <p className="wpr-sec__hint">
              Crews are seeded from the daily reports. Man-hours cannot be attributed to a company
              automatically — the roster has no employer field — so enter them here.
              {" "}The job logged <strong>{data.labor.total_hours}</strong> hours in total this week.
            </p>
            <table className="wpr-table">
              <thead>
                <tr><th>Company</th><th># of workers</th><th>Man hours</th><th /></tr>
              </thead>
              <tbody>
                {draft.labor.map((r, i) => (
                  <tr key={i}>
                    <td data-cell="Company">
                      <input className="wpr-field__input" aria-label={`Company ${i + 1}`} value={r.company}
                             onChange={(e) => { const n = [...draft.labor]; n[i] = { ...r, company: e.target.value }; set("labor", n); }} />
                    </td>
                    <td data-cell="Workers">
                      <input className="wpr-num" inputMode="numeric" aria-label={`Workers for row ${i + 1}`} value={r.workers}
                             onChange={(e) => { const n = [...draft.labor]; n[i] = { ...r, workers: e.target.value }; set("labor", n); }} />
                    </td>
                    <td data-cell="Man hours">
                      <input className="wpr-num" inputMode="decimal" aria-label={`Man hours for row ${i + 1}`} value={r.man_hours}
                             onChange={(e) => { const n = [...draft.labor]; n[i] = { ...r, man_hours: e.target.value }; set("labor", n); }} />
                    </td>
                    <td>
                      <button type="button" className="btn btn--danger"
                              aria-label={`Remove labor row ${i + 1}`}
                              onClick={() => set("labor", draft.labor.filter((_, j) => j !== i))}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button type="button" className="btn btn--secondary" style={{ marginTop: "var(--space-3)" }}
                    onClick={() => set("labor", [...draft.labor, { company: "", workers: "", man_hours: "" }])}>
              Add company
            </button>
          </section>

          {/* ── page 3: progress narrative ──────────────────────────────────────── */}
          <section className="wpr-sec" id="wpr-progress">
            <header className="wpr-sec__head">
              <h2 className="wpr-sec__title">Construction progress</h2>
              <span className="wpr-sec__tag wpr-sec__tag--yours">Yours to write</span>
            </header>
            <p className="wpr-sec__hint">
              {data.schedule === null
                ? "No schedule is imported for this job, so the report's percent-complete table will say so. Upload and commit a project schedule to fill it."
                : `Percentages come from the committed project schedule — ${data.schedule.task_count} active task(s)` +
                  (data.schedule.behind.length
                    ? `, ${data.schedule.behind.length} behind schedule as of ${data.schedule.today} (seeded into Critical Items below).`
                    : ", none behind schedule.")}
            </p>

            {/* The percent table the report's page 3 renders, shown here because this screen's
                whole premise is that the office sees what the document will say. Read-only —
                the numbers are the committed schedule's, marked off on the schedule page. */}
            {data.schedule !== null && data.schedule.sections.length > 0 && (
              <table className="wpr-table">
                <thead>
                  <tr>
                    <th>Section</th>
                    <th>Task</th>
                    <th>Complete</th>
                  </tr>
                </thead>
                <tbody>
                  {data.schedule.sections.flatMap((s) =>
                    s.items.map((it, i) => (
                      <tr key={`${s.name}-${it.label}-${i}`}>
                        <td data-cell="Section">{i === 0 ? <strong>{s.name}</strong> : ""}</td>
                        <td data-cell="Task">{it.label}</td>
                        <td data-cell="Complete">{it.percent === null ? "—" : `${it.percent}%`}</td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            )}

            <div className="wpr-grid">
              <label className="wpr-field wpr-grid__wide">
                <span className="wpr-field__label">Critical items / delays</span>
                <textarea className="wpr-field__textarea" rows={6} value={draft.critical_items}
                          onChange={(e) => set("critical_items", e.target.value)} />
              </label>
              <label className="wpr-field wpr-grid__wide">
                <span className="wpr-field__label">Upcoming activities</span>
                <textarea className="wpr-field__textarea" rows={4} value={draft.upcoming_activities}
                          onChange={(e) => set("upcoming_activities", e.target.value)} />
              </label>
            </div>
            {!data.office.saved && (
              <p className="wpr-sec__hint">
                Both boxes are pre-filled from what the crews filed this week. Edit freely — what you
                save is what the client reads.
              </p>
            )}
          </section>

          {/* ── page 4: photos ──────────────────────────────────────────────────── */}
          <section className="wpr-sec" id="wpr-photos">
            <header className="wpr-sec__head">
              <h2 className="wpr-sec__title">Progress photos</h2>
              <span className="wpr-sec__tag wpr-sec__tag--yours">Yours to choose</span>
            </header>
            <p className="wpr-sec__hint">
              {data.photos.auto_selected
                ? `Auto-selected a spread across the week. ${data.photos.available.length} screened photo(s) available.`
                : `Your selection: ${(draft.photos ?? []).length} of ${data.photos.available.length}.`}
              {" "}Only screened photos appear here.
            </p>
            {data.photos.available.length === 0 && <p>No screened photos for this week.</p>}
            <ul className="wpr-photos">
              {data.photos.available.map((p) => (
                <li key={p.pool_id} className={`wpr-photo${selectedIds.has(p.pool_id) ? " is-on" : ""}`}>
                  <label className="wpr-photo__pick">
                    <input type="checkbox" checked={selectedIds.has(p.pool_id)}
                           aria-label={`Include the photo from ${p.work_date}`}
                           onChange={() => togglePhoto(p)} />
                    <span>{p.work_date}</span>
                    <span className="wpr-photo__meta">{selectedIds.has(p.pool_id) ? "In the report" : "Not used"}</span>
                  </label>
                  <input className="wpr-photo__caption"
                         aria-label={`Caption for the photo from ${p.work_date}`}
                         value={
                           (draft.photos ?? data.photos.selected).find((x) => x.pool_id === p.pool_id)?.caption ?? p.caption
                         }
                         placeholder="Caption"
                         onChange={(e) => {
                           const base = draft.photos ?? data.photos.selected;
                           set("photos", base.map((x) => x.pool_id === p.pool_id ? { ...x, caption: e.target.value } : x));
                         }} />
                </li>
              ))}
            </ul>
            {data.photos.available.length > 0 && (
              <button type="button" className="btn btn--secondary" style={{ marginTop: "var(--space-3)" }}
                      onClick={() => set("photos", [])}>
                Use no photos this week
              </button>
            )}
          </section>

          {/* ── page 5: materials + pending ─────────────────────────────────────── */}
          <section className="wpr-sec wpr-sec--derived" id="wpr-materials">
            <header className="wpr-sec__head">
              <h2 className="wpr-sec__title">Material deliveries</h2>
              <span className="wpr-sec__tag wpr-sec__tag--derived">From the field</span>
            </header>
            <p className="wpr-sec__hint">
              Every delivery mark the field recorded against this job&apos;s material lines this
              week. Read-only — the record is the delivery ledger, and it is corrected there.
            </p>
            <table className="wpr-table">
              <thead>
                <tr>
                  <th>Date</th><th>Item</th><th>Part no.</th><th>Qty</th>
                  <th>Vendor</th><th>BOL</th><th>Carrier</th><th>Mark</th>
                </tr>
              </thead>
              <tbody>
                {data.deliveries.length === 0 && (
                  <tr><td colSpan={8}>No deliveries recorded this week.</td></tr>
                )}
                {data.deliveries.map((d, i) => {
                  const chip = deliveryKindChip(d.kind);
                  return (
                    <tr key={i}>
                      <td data-cell="Date"><strong>{d.event_date}</strong></td>
                      <td data-cell="Item">{d.item || d.part_number || "—"}</td>
                      <td data-cell="Part no.">{d.part_number || "—"}</td>
                      <td data-cell="Qty">{[d.qty, d.unit].filter(Boolean).join(" ") || "—"}</td>
                      <td data-cell="Vendor">{d.vendor || "—"}</td>
                      <td data-cell="BOL">{d.bol_number || "—"}</td>
                      <td data-cell="Carrier">{d.carrier || "—"}</td>
                      <td data-cell="Mark"><span className={chip.className}>{chip.label}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {/* The payload has always carried these; the page only ever folded them into the
                narrative seed, so a reader of the screen could not see the week's material
                problems as a list. */}
            <h3 className="wpr-sec__title" style={{ fontSize: "var(--fs-base)", marginTop: "var(--space-6)" }}>
              Material problems this week
            </h3>
            <table className="wpr-table">
              <thead>
                <tr><th>Date</th><th>Material</th><th>Issue</th><th>Detail</th></tr>
              </thead>
              <tbody>
                {data.material_incidents.length === 0 && (
                  <tr><td colSpan={4}>No material problems reported this week.</td></tr>
                )}
                {data.material_incidents.map((m, i) => (
                  <tr key={i}>
                    <td data-cell="Date"><strong>{m.work_date}</strong></td>
                    <td data-cell="Material">{m.material || "—"}</td>
                    <td data-cell="Issue">{m.issue || "—"}</td>
                    <td data-cell="Detail">{m.details || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="wpr-sec__hint" style={{ marginTop: "var(--space-3)" }}>
              These also seed the Critical items box above. Editing that text does not change this
              table — it is what the field filed.
            </p>
          </section>

          <section className="wpr-sec" id="wpr-pending">
            <header className="wpr-sec__head">
              <h2 className="wpr-sec__title">Pending requests</h2>
              <span className="wpr-sec__tag wpr-sec__tag--yours">Yours to fill</span>
              {carried && <span className="wpr-carried">Carried forward</span>}
            </header>
            <p className="wpr-sec__hint">
              RFIs, submittals and change orders are not tracked in the portal, so the report takes
              them from here.
            </p>
            <div className="wpr-grid">
              <label className="wpr-field">
                <span className="wpr-field__label">Pending RFIs</span>
                <input className="wpr-field__input" value={draft.rfis}
                       onChange={(e) => set("rfis", e.target.value)} />
              </label>
              <label className="wpr-field">
                <span className="wpr-field__label">Pending submittals</span>
                <input className="wpr-field__input" value={draft.submittals}
                       onChange={(e) => set("submittals", e.target.value)} />
              </label>
              <label className="wpr-field">
                <span className="wpr-field__label">IFC review</span>
                <input className="wpr-field__input" value={draft.ifc_review}
                       onChange={(e) => set("ifc_review", e.target.value)} />
              </label>
              <label className="wpr-field">
                <span className="wpr-field__label">Pending change orders</span>
                <input className="wpr-field__input" value={draft.change_orders}
                       onChange={(e) => set("change_orders", e.target.value)} />
              </label>
            </div>
          </section>
        </div>
      </div>

      <div className="wpr-save">
        <span className={`wpr-save__state${dirty ? " wpr-save__state--dirty" : ""}`}>
          <span className="wpr-save__dot" aria-hidden="true" />
          {dirty ? "Unsaved changes" : data.office.saved ? "All changes saved" : "Nothing saved for this week yet"}
        </span>
        {data.office.updated_by && (
          <span className="wpr-sec__hint" style={{ margin: 0 }}>
            Last saved by {data.office.updated_by}
          </span>
        )}
        <span className="wpr-save__spacer" />
        <button type="button" className="btn btn--primary" disabled={busy} onClick={() => void save()}>
          {busy ? "Saving…" : "Save weekly report inputs"}
        </button>
      </div>
    </PageShell>
  );
}
