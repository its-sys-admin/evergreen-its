import { useState, useEffect } from "react";
import * as api from "../lib/fieldops_personnel";
import { PageShell } from "../components/PageShell";

// Format helpers: epoch SECONDS (as stored in D1) → ×1000 for JS Date. Kept local (like the
// list page) — the two-line helpers don't warrant a shared module the way the equipment
// status-pill mapping did.
function fmtDateTime(epochSeconds: number | null): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}
function fmtHours(hours: number | null): string {
  if (hours == null || isNaN(hours)) return "—";
  return hours.toFixed(2);
}

/**
 * Personnel DETAIL — one crew member's header (trade / account / standing placement) + their full
 * time-entry history, cursor-paginated (DESC; prepend the older page). A read-only sibling view of
 * the roster, mirroring the URS-Marine PersonnelDetail refinement and the portal's own
 * EquipmentDetailView shape (fetch-by-id in an effect, its own loading/error states). Roster CRUD
 * (create / edit / link / assign / retire) lives on the roster page, not here — this view has no
 * write path and no cap gate.
 */
export function PersonnelDetailView({
  id,
  onBack,
  onHome,
}: {
  id: number;
  onBack: () => void; // back to the personnel roster
  onHome: () => void; // portal home (PageShell nav)
}) {
  const [person, setPerson] = useState<api.PersonnelDetail | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    api
      .fetchPersonnelDetail(id, undefined)
      .then((res) => {
        if (!live) return;
        setPerson(res.personnel);
        setCursor(res.next_cursor);
      })
      .catch(() => live && setError("Failed to load personnel details."))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [id]);

  async function loadMoreEntries() {
    if (!person || !cursor) return;
    try {
      const res = await api.fetchPersonnelDetail(person.id, cursor);
      // DESC order — the next page is OLDER, so prepend it (matches the prior in-page behaviour).
      setPerson((prev) =>
        prev ? { ...prev, time_entries: [...res.personnel.time_entries, ...prev.time_entries] } : res.personnel,
      );
      setCursor(res.next_cursor);
    } catch {
      setError("Failed to load more entries.");
    }
  }

  const back = (
    <div className="dash-back-btn">
      <button onClick={onBack} className="btn--secondary">
        ← Back to personnel
      </button>
    </div>
  );

  if (loading && !person) {
    return (
      <PageShell onHome={onHome}>
        {back}
        <div className="muted">Loading personnel…</div>
      </PageShell>
    );
  }
  if (!person) {
    return (
      <PageShell onHome={onHome}>
        {back}
        <div className="banner banner--err">{error ?? "Personnel not found."}</div>
      </PageShell>
    );
  }

  return (
    <PageShell onHome={onHome}>
      {back}

      <div className="dash-detail__head">
        <h2 className="page__heading">{person.name}</h2>
        {person.current_job && (
          <span className="dash-pill dash-pill--ok">Placed on {person.current_job_name ?? person.current_job}</span>
        )}
      </div>
      <div className="dash-chips">
        <span className="dash-chip">{person.trade || "No trade"}</span>
        <span className="dash-chip">{person.username ? `@${person.username}` : "No account linked"}</span>
      </div>
      {error && <div className="banner banner--err">{error}</div>}

      <section className="card dash-section">
        <h3 className="dash-detail__h2">Time history</h3>
        {person.time_entries.length === 0 ? (
          <div className="dash-unavail">No time logged.</div>
        ) : (
          <>
            <table className="dash-table">
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Work started</th>
                  <th>Work ended</th>
                  <th>Hours</th>
                </tr>
              </thead>
              <tbody>
                {person.time_entries.map((e) => (
                  <tr key={e.uuid} className="dash-row">
                    <td>{e.project_name ?? e.job_id}</td>
                    <td>{fmtDateTime(e.work_started_at)}</td>
                    <td>{fmtDateTime(e.work_ended_at)}</td>
                    <td>{fmtHours(e.hours)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {cursor && (
              <div className="dash-row dash-load-more">
                <button onClick={loadMoreEntries} className="btn--secondary">
                  Load more
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </PageShell>
  );
}
