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
// crews that actually worked, the delivery ledger, the schedule state.
//
// TWO RULES THE LAYOUT ENCODES:
//
//  1. **Carried-forward values are marked.** A week with no saved row inherits the most recent
//     earlier one; the banner says so and every carried field is badged, so "reviewed and
//     unchanged" never masquerades as "nobody looked".
//  2. **The narrative textareas are PRE-FILLED with the assembled seed.** `saved` is row-level:
//     the moment the office saves anything, an untouched narrative field renders BLANK on the
//     report rather than falling back to the derived text. Pre-filling is what makes the derived
//     assembly reach the document — a live mock render produced two empty narrative sections
//     precisely because a hand-seeded row had saved-empty narrative.

import { useCallback, useEffect, useMemo, useState } from "react";
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

interface Props {
  jobId: string;
  onBack: () => void;
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

/** The deterministic seeds the report would use for an UNSAVED week — mirrored here so the
 *  textareas start from the same text the compile would have assembled. Kept deliberately simple
 *  and identical in spirit to `wpr_data._assemble_*`; the Python side remains authoritative for
 *  what actually renders. */
function seedNarrative(d: ProductionReportResponse): { critical: string; upcoming: string } {
  const lines: string[] = [];
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

export function WeeklyReportPage({ jobId, onBack }: Props) {
  const [weekStart, setWeekStart] = useState(() => weekStartFor(new Date()));
  const [data, setData] = useState<ProductionReportResponse | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");

  const load = useCallback(async (ws: string) => {
    setErr(""); setSaved(""); setData(null); setDraft(null);
    try {
      const d = await fetchWeeklyReport(jobId, ws);
      setData(d); setDraft(toDraft(d));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not load the weekly report.");
    }
  }, [jobId]);

  useEffect(() => { void load(weekStart); }, [load, weekStart]);

  // The photo list the report will use: the office's explicit picks, else the server's spread.
  const selectedIds = useMemo(
    () => new Set((draft?.photos ?? data?.photos.selected ?? []).map((p) => p.pool_id)),
    [draft?.photos, data],
  );

  if (err) {
    return (
      <main className="page">
        <button className="btn" onClick={onBack}>← Back</button>
        <p className="error">{err}</p>
      </main>
    );
  }
  if (!data || !draft) return <main className="page"><p>Loading the weekly report…</p></main>;

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
          hazard_topics: [],
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
    <main className="page wr">
      <header className="wr__head">
        <button className="btn" onClick={onBack}>← Back</button>
        <h1>Weekly Production Report</h1>
        <p className="wr__sub">
          {data.job?.project_name ?? jobId} · week of {weekStart} → {weekEndFor(weekStart)}
        </p>
        <label className="wr__week">
          Week starting (Saturday)
          <input type="date" value={weekStart}
                 onChange={(e) => e.target.value && setWeekStart(weekStartFor(new Date(`${e.target.value}T12:00:00`)))} />
        </label>
      </header>

      {carried && (
        <p className="wr__banner" role="status">
          These values were carried forward from the week of <strong>{carried}</strong>. Nothing has
          been saved for this week yet — review them and save.
        </p>
      )}
      {data.daily_report_count === 0 && (
        <p className="wr__banner wr__banner--warn" role="status">
          No daily reports were filed for this week. As it stands the compile will HOLD this week
          rather than send a report — get the dailies filed, or decide with the office.
        </p>
      )}
      {saved && <p className="wr__banner wr__banner--ok" role="status">{saved}</p>}

      {/* ── page 1: header + safety ─────────────────────────────────────────── */}
      <section className="wr__sec">
        <h2>Report header</h2>
        <div className="wr__grid2">
          <label>Site location<input value={draft.site_location} onChange={(e) => set("site_location", e.target.value)} /></label>
          <label>ESS management<input value={draft.ess_management} onChange={(e) => set("ess_management", e.target.value)} /></label>
          <label>Mobilization date<input type="date" value={draft.mobilization_date} onChange={(e) => set("mobilization_date", e.target.value)} /></label>
          <label>Prepared by<input value={draft.prepared_by} onChange={(e) => set("prepared_by", e.target.value)} /></label>
          <label className="wr__wide">Subcontractors (comma separated)
            <input value={draft.subcontractors} onChange={(e) => set("subcontractors", e.target.value)} /></label>
        </div>
      </section>

      <section className="wr__sec">
        <h2>Project safety status</h2>
        <p className="wr__hint">
          Incident reports do not carry an OSHA case classification, so these counts cannot be
          derived — they are the office's record.
        </p>
        <table className="wr__table">
          <thead><tr><th>Site safety record</th><th>This month</th><th>Project to date</th></tr></thead>
          <tbody>
            {SAFETY_ROWS.map(({ key, label }) => (
              <tr key={key}>
                <td>{label}</td>
                <td><input type="number" min={0} value={draft.safety[key].month}
                     onChange={(e) => set("safety", { ...draft.safety, [key]: { ...draft.safety[key], month: e.target.value } })} /></td>
                <td><input type="number" min={0} value={draft.safety[key].to_date}
                     onChange={(e) => set("safety", { ...draft.safety, [key]: { ...draft.safety[key], to_date: e.target.value } })} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.hazard_form_codes.length > 0 && (
          <p className="wr__hint">
            Safety-meeting topics are derived from the {data.hazard_form_codes.length} toolbox
            talk(s) / JHA(s) filed this week.
          </p>
        )}
      </section>

      {/* ── page 2: weather + labor ─────────────────────────────────────────── */}
      <section className="wr__sec">
        <h2>Weather</h2>
        <p className="wr__hint">
          Conditions and temperature come from the daily reports. A <strong>weather day</strong> is
          a delay claim, so it is yours to mark — never inferred from the conditions text.
        </p>
        <table className="wr__table">
          <thead><tr><th>Date</th><th>Conditions</th><th>Avg temp</th><th>Weather day</th></tr></thead>
          <tbody>
            {data.weather.days.length === 0 && <tr><td colSpan={4}>No daily reports filed this week.</td></tr>}
            {data.weather.days.map((d) => (
              <tr key={d.work_date}>
                <td>{d.work_date}</td><td>{d.conditions || "—"}</td><td>{d.avg_temp || "—"}</td>
                <td><input type="checkbox" checked={draft.inclement.has(d.work_date)}
                     onChange={() => { const n = new Set(draft.inclement);
                       n.has(d.work_date) ? n.delete(d.work_date) : n.add(d.work_date); set("inclement", n); }} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <label>Total weather days to date
          <input type="number" min={0} value={draft.weather_days_to_date}
                 onChange={(e) => set("weather_days_to_date", e.target.value)} /></label>
      </section>

      <section className="wr__sec">
        <h2>Construction labor report</h2>
        <p className="wr__hint">
          Crews are seeded from the daily reports. Man-hours cannot be attributed to a company
          automatically — the roster has no employer field — so enter them here.
          {" "}The job logged <strong>{data.labor.total_hours}</strong> hours in total this week.
        </p>
        <table className="wr__table">
          <thead><tr><th>Company</th><th># of workers</th><th>Man hours</th><th /></tr></thead>
          <tbody>
            {draft.labor.map((r, i) => (
              <tr key={i}>
                <td><input value={r.company} onChange={(e) => { const n = [...draft.labor]; n[i] = { ...r, company: e.target.value }; set("labor", n); }} /></td>
                <td><input value={r.workers} onChange={(e) => { const n = [...draft.labor]; n[i] = { ...r, workers: e.target.value }; set("labor", n); }} /></td>
                <td><input value={r.man_hours} onChange={(e) => { const n = [...draft.labor]; n[i] = { ...r, man_hours: e.target.value }; set("labor", n); }} /></td>
                <td><button className="btn btn--sm" onClick={() => set("labor", draft.labor.filter((_, j) => j !== i))}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="btn btn--sm" onClick={() => set("labor", [...draft.labor, { company: "", workers: "", man_hours: "" }])}>
          Add company
        </button>
      </section>

      {/* ── page 3: progress narrative ──────────────────────────────────────── */}
      <section className="wr__sec">
        <h2>Construction progress</h2>
        <p className="wr__hint">
          {data.schedule === null
            ? "No schedule is imported for this job, so the report's percent-complete table will say so. Upload and commit a project schedule to fill it."
            : "Percentages come from the committed project schedule."}
        </p>
        <label className="wr__wide">Critical items / delays
          <textarea rows={5} value={draft.critical_items} onChange={(e) => set("critical_items", e.target.value)} /></label>
        <label className="wr__wide">Upcoming activities
          <textarea rows={4} value={draft.upcoming_activities} onChange={(e) => set("upcoming_activities", e.target.value)} /></label>
        {!data.office.saved && (
          <p className="wr__hint">
            Both boxes are pre-filled from what the crews filed this week. Edit freely — what you
            save is what the client reads.
          </p>
        )}
      </section>

      {/* ── page 4: photos ──────────────────────────────────────────────────── */}
      <section className="wr__sec">
        <h2>Progress photos</h2>
        <p className="wr__hint">
          {data.photos.auto_selected
            ? `Auto-selected a spread across the week. ${data.photos.available.length} screened photo(s) available.`
            : `Your selection: ${(draft.photos ?? []).length} of ${data.photos.available.length}.`}
          {" "}Only screened photos appear here.
        </p>
        {data.photos.available.length === 0 && <p>No screened photos for this week.</p>}
        <ul className="wr__photos">
          {data.photos.available.map((p) => (
            <li key={p.pool_id} className={selectedIds.has(p.pool_id) ? "is-on" : ""}>
              <label>
                <input type="checkbox" checked={selectedIds.has(p.pool_id)} onChange={() => togglePhoto(p)} />
                <span>{p.work_date}</span>
                <input className="wr__cap" value={
                  (draft.photos ?? data.photos.selected).find((x) => x.pool_id === p.pool_id)?.caption ?? p.caption
                } placeholder="caption"
                  onChange={(e) => {
                    const base = draft.photos ?? data.photos.selected;
                    set("photos", base.map((x) => x.pool_id === p.pool_id ? { ...x, caption: e.target.value } : x));
                  }} />
              </label>
            </li>
          ))}
        </ul>
        {data.photos.available.length > 0 && (
          <button className="btn btn--sm" onClick={() => set("photos", [])}>
            Use no photos this week
          </button>
        )}
      </section>

      {/* ── page 5: materials + pending ─────────────────────────────────────── */}
      <section className="wr__sec">
        <h2>Material deliveries</h2>
        <p className="wr__hint">Derived from the delivery ledger — nothing to enter.</p>
        <table className="wr__table">
          <thead><tr><th>Item</th><th>Vendor</th><th>Qty</th><th>Delivered</th></tr></thead>
          <tbody>
            {data.deliveries.length === 0 && <tr><td colSpan={4}>No deliveries recorded this week.</td></tr>}
            {data.deliveries.map((d, i) => (
              <tr key={i}><td>{d.item || d.part_number}</td><td>{d.vendor || "—"}</td><td>{d.qty}</td><td>{d.event_date}</td></tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="wr__sec">
        <h2>Pending requests</h2>
        <div className="wr__grid2">
          <label className="wr__wide">Pending RFIs<input value={draft.rfis} onChange={(e) => set("rfis", e.target.value)} /></label>
          <label className="wr__wide">Pending submittals<input value={draft.submittals} onChange={(e) => set("submittals", e.target.value)} /></label>
          <label className="wr__wide">IFC review<input value={draft.ifc_review} onChange={(e) => set("ifc_review", e.target.value)} /></label>
          <label className="wr__wide">Pending change orders<input value={draft.change_orders} onChange={(e) => set("change_orders", e.target.value)} /></label>
        </div>
      </section>

      <footer className="wr__foot">
        <button className="btn btn--primary" disabled={busy} onClick={() => void save()}>
          {busy ? "Saving…" : "Save weekly report inputs"}
        </button>
        {data.office.updated_by && (
          <span className="wr__hint">Last saved by {data.office.updated_by}</span>
        )}
      </footer>
    </main>
  );
}
