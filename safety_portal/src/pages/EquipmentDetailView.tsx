import { useState, useEffect } from "react";
import type { FormEvent } from "react";
import * as api from "../lib/fieldops_equipment";
import { useAuth } from "../lib/auth";
import { PageShell } from "../components/PageShell";
import { fmtDateTime, fmtNumber, equipStatusLabel, equipStatusPillClass } from "./equipmentView";

/**
 * Equipment DETAIL — one unit's readiness header + the OPERATIONAL field actions (set readiness
 * status, log a machine event, move to a job site) and its full history (location reads,
 * inspections, machine logs), each history leg paginating independently. Mirrors the URS-Marine
 * EquipmentDetail refinement but keeps the portal's cursor-paginated data layer + its cap gate:
 * every write is gated on cap.equipment.field (a CONVENIENCE gate — the Worker re-gates server-side,
 * Invariant 2). Roster CRUD (add/edit/retire) lives on the Manage screen, not here.
 */
export function EquipmentDetailView({
  id,
  onBack,
  onHome,
}: {
  id: number;
  onBack: () => void; // back to the equipment dashboard
  onHome: () => void; // portal home (PageShell nav)
}) {
  const { user } = useAuth();
  const canField = (user?.capabilities ?? []).includes("cap.equipment.field");

  const [unit, setUnit] = useState<api.EquipmentDetail | null>(null);
  const [locCursor, setLocCursor] = useState<string | null>(null);
  const [inspCursor, setInspCursor] = useState<string | null>(null);
  const [logCursor, setLogCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Field-action form state (cap.equipment.field)
  const [statusVal, setStatusVal] = useState<api.EquipStatus>("fmc");
  const [statusNote, setStatusNote] = useState("");
  const [maintType, setMaintType] = useState<api.LogType>("maintenance");
  const [maintValue, setMaintValue] = useState("");
  const [maintDetail, setMaintDetail] = useState("");
  const [moveJob, setMoveJob] = useState("");
  const [moveLabel, setMoveLabel] = useState("");
  const [jobOptions, setJobOptions] = useState<api.JobOption[]>([]);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionMsg, setActionMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    api
      .fetchEquipmentDetail(id, undefined)
      .then((res) => {
        if (!live) return;
        setUnit(res.equipment);
        setStatusVal(res.equipment.header.status);
        setStatusNote(res.equipment.header.status_note ?? "");
        setLocCursor(res.cursors.loc);
        setInspCursor(res.cursors.insp);
        setLogCursor(res.cursors.log);
      })
      .catch(() => live && setError("Failed to load equipment details."))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [id]);

  // Active-job options for the "move to job" picker (only when the actor can act; falls back to []).
  useEffect(() => {
    if (canField) api.fetchActiveJobOptions().then(setJobOptions).catch(() => {});
  }, [canField]);

  async function reloadDetail() {
    const res = await api.fetchEquipmentDetail(id, undefined);
    setUnit(res.equipment);
    setLocCursor(res.cursors.loc);
    setInspCursor(res.cursors.insp);
    setLogCursor(res.cursors.log);
  }

  async function submitStatus(e: FormEvent) {
    e.preventDefault();
    if (!unit || actionBusy) return;
    setActionBusy(true);
    setActionMsg(null);
    try {
      await api.setEquipmentStatus(unit.header.id, {
        uuid: crypto.randomUUID(),
        status: statusVal,
        status_note: statusNote.trim() || undefined,
      });
      setStatusNote("");
      await reloadDetail();
      setActionMsg({ ok: true, text: "Readiness updated." });
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Update failed." });
    } finally {
      setActionBusy(false);
    }
  }

  async function submitMaint(e: FormEvent) {
    e.preventDefault();
    if (!unit || actionBusy) return;
    const value = maintValue.trim() === "" ? undefined : Number(maintValue);
    if (value !== undefined && !Number.isFinite(value)) {
      setActionMsg({ ok: false, text: "Value must be a number." });
      return;
    }
    setActionBusy(true);
    setActionMsg(null);
    try {
      await api.logEquipmentMaintenance(unit.header.id, {
        uuid: crypto.randomUUID(),
        log_type: maintType,
        value_num: value,
        detail: maintDetail.trim() || undefined,
      });
      setMaintValue("");
      setMaintDetail("");
      await reloadDetail();
      setActionMsg({ ok: true, text: "Log entry recorded." });
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Log failed." });
    } finally {
      setActionBusy(false);
    }
  }

  async function submitMove(e: FormEvent) {
    e.preventDefault();
    if (!unit || actionBusy || !moveJob) return;
    setActionBusy(true);
    setActionMsg(null);
    try {
      await api.moveEquipment(unit.header.id, { job_id: moveJob, label: moveLabel.trim() || undefined });
      setMoveLabel("");
      await reloadDetail();
      setActionMsg({ ok: true, text: "Location recorded." });
    } catch (err) {
      setActionMsg({ ok: false, text: err instanceof Error ? err.message : "Move failed." });
    } finally {
      setActionBusy(false);
    }
  }

  // Each history leg paginates INDEPENDENTLY (DESC order; prepend the older page).
  async function loadMoreLocations() {
    if (!unit || !locCursor) return;
    try {
      const res = await api.fetchEquipmentDetail(unit.header.id, { loc: locCursor });
      setUnit((prev) => (prev ? { ...prev, locations: [...res.equipment.locations, ...prev.locations] } : res.equipment));
      setLocCursor(res.cursors.loc);
    } catch {
      setError("Failed to load more locations.");
    }
  }
  async function loadMoreInspections() {
    if (!unit || !inspCursor) return;
    try {
      const res = await api.fetchEquipmentDetail(unit.header.id, { insp: inspCursor });
      setUnit((prev) => (prev ? { ...prev, inspections: [...res.equipment.inspections, ...prev.inspections] } : res.equipment));
      setInspCursor(res.cursors.insp);
    } catch {
      setError("Failed to load more inspections.");
    }
  }
  async function loadMoreLogs() {
    if (!unit || !logCursor) return;
    try {
      const res = await api.fetchEquipmentDetail(unit.header.id, { log: logCursor });
      setUnit((prev) => (prev ? { ...prev, logs: [...res.equipment.logs, ...prev.logs] } : res.equipment));
      setLogCursor(res.cursors.log);
    } catch {
      setError("Failed to load more logs.");
    }
  }

  const back = (
    <div className="dash-back-btn">
      <button onClick={onBack} className="btn--secondary">
        ← Back to equipment
      </button>
    </div>
  );

  if (loading && !unit) {
    return (
      <PageShell onHome={onHome}>
        {back}
        <div className="muted">Loading equipment…</div>
      </PageShell>
    );
  }
  if (!unit) {
    return (
      <PageShell onHome={onHome}>
        {back}
        <div className="banner banner--err">{error ?? "Equipment not found."}</div>
      </PageShell>
    );
  }

  const eq = unit.header;
  return (
    <PageShell onHome={onHome}>
      {back}

      <div className="dash-detail__head">
        <h2 className="page__heading">{eq.name}</h2>
        <span className={equipStatusPillClass(eq.status)}>{equipStatusLabel(eq.status)}</span>
      </div>
      <p className="dash-card__sub muted">
        {eq.kind ?? "Equipment"} · {eq.identifier ?? `#${eq.id}`}
      </p>
      {eq.status_note && <p className="muted">{eq.status_note}</p>}
      {eq.status_actor && (
        <p className="dash-card__sub">
          Set by {eq.status_actor}
          {eq.status_changed_at ? ` · ${fmtDateTime(eq.status_changed_at)}` : ""}
        </p>
      )}
      {error && <div className="banner banner--err">{error}</div>}

      {/* ── Field actions (cap.equipment.field) ── */}
      {canField && (
        <section className="card dash-section equip-detail__actions">
          <h3 className="dash-detail__h2">Field actions</h3>
          {actionMsg && (
            <div className={`banner ${actionMsg.ok ? "banner--ok" : "banner--err"}`}>{actionMsg.text}</div>
          )}
          <form onSubmit={submitStatus} className="dash-row" aria-label="Update readiness status">
            <label className="dash-card__label">
              Readiness:{" "}
              <select value={statusVal} onChange={(e) => setStatusVal(e.target.value as api.EquipStatus)}>
                <option value="fmc">Full Mission Capable</option>
                <option value="degraded">Degraded</option>
                <option value="down">Down</option>
              </select>
            </label>{" "}
            <input
              value={statusNote}
              onChange={(e) => setStatusNote(e.target.value)}
              placeholder="Status note (optional)"
              maxLength={512}
            />{" "}
            <button type="submit" disabled={actionBusy} className="btn--primary">
              Update status
            </button>
          </form>
          <form onSubmit={submitMaint} className="dash-row" aria-label="Add machine log">
            <label className="dash-card__label">
              Log:{" "}
              <select value={maintType} onChange={(e) => setMaintType(e.target.value as api.LogType)}>
                <option value="maintenance">Maintenance</option>
                <option value="fuel">Fuel</option>
                <option value="hours">Hours</option>
              </select>
            </label>{" "}
            <input
              value={maintValue}
              onChange={(e) => setMaintValue(e.target.value)}
              placeholder="Value (optional)"
              inputMode="decimal"
            />{" "}
            <input
              value={maintDetail}
              onChange={(e) => setMaintDetail(e.target.value)}
              placeholder="Detail (optional)"
              maxLength={2000}
            />{" "}
            <button type="submit" disabled={actionBusy} className="btn--secondary">
              Add log
            </button>
          </form>
          <form onSubmit={submitMove} className="dash-row" aria-label="Move equipment to a job">
            <label className="dash-card__label">
              Move to job:{" "}
              <select value={moveJob} onChange={(e) => setMoveJob(e.target.value)}>
                <option value="">Select a job…</option>
                {jobOptions.map((j) => (
                  <option key={j.job_id} value={j.job_id}>
                    {j.project_name} ({j.job_id})
                  </option>
                ))}
              </select>
            </label>{" "}
            <input
              value={moveLabel}
              onChange={(e) => setMoveLabel(e.target.value)}
              placeholder="Site label (optional)"
              maxLength={256}
            />{" "}
            <button type="submit" disabled={actionBusy || !moveJob} className="btn--secondary">
              Record location
            </button>
          </form>
        </section>
      )}

      {/* ── Location history ── */}
      <section className="card dash-section">
        <h3 className="dash-detail__h2">Location history</h3>
        {unit.locations.length === 0 ? (
          <div className="dash-empty">No location records for this unit.</div>
        ) : (
          <>
            <table className="dash-table">
              <thead>
                <tr>
                  <th>Recorded</th>
                  <th>Label / Job</th>
                  <th>Lat</th>
                  <th>Lon</th>
                </tr>
              </thead>
              <tbody>
                {unit.locations.map((loc) => (
                  <tr key={loc.recorded_at + ":" + loc.id} className="dash-row">
                    <td>{fmtDateTime(loc.recorded_at)}</td>
                    <td>{loc.label ?? (loc.job_id ? `Job ${loc.job_id}` : "—")}</td>
                    <td>{loc.lat != null ? fmtNumber(loc.lat, 4) : "—"}</td>
                    <td>{loc.lon != null ? fmtNumber(loc.lon, 4) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {locCursor && (
              <div className="dash-row dash-load-more">
                <button onClick={loadMoreLocations} className="btn--secondary">
                  Load more
                </button>
              </div>
            )}
          </>
        )}
      </section>

      {/* ── Inspection history ── */}
      <section className="card dash-section">
        <h3 className="dash-detail__h2">Inspections</h3>
        {unit.inspections.length === 0 ? (
          <div className="dash-empty">No inspections recorded.</div>
        ) : (
          <>
            <table className="dash-table">
              <thead>
                <tr>
                  <th>Form</th>
                  <th>Version</th>
                  <th>Performed at</th>
                </tr>
              </thead>
              <tbody>
                {unit.inspections.map((insp) => (
                  <tr key={insp.uuid} className="dash-row">
                    <td>{insp.form_code}</td>
                    <td>v{insp.version}</td>
                    <td>{fmtDateTime(insp.performed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {inspCursor && (
              <div className="dash-row dash-load-more">
                <button onClick={loadMoreInspections} className="btn--secondary">
                  Load more
                </button>
              </div>
            )}
          </>
        )}
      </section>

      {/* ── Machine logs & status changes ── */}
      <section className="card dash-section">
        <h3 className="dash-detail__h2">Recent logs</h3>
        {unit.logs.length === 0 ? (
          <div className="dash-empty">No recent maintenance or status logs.</div>
        ) : (
          <>
            <ul className="dash-loglist">
              {unit.logs.map((log) => (
                <li key={log.uuid}>
                  <strong>{log.log_type}</strong>:{" "}
                  {log.detail ?? (log.value_num != null ? `${fmtNumber(log.value_num)} ${log.status_value ?? ""}` : "—")}
                  {log.performed_at && <span className="muted"> • {fmtDateTime(log.performed_at)}</span>}
                </li>
              ))}
            </ul>
            {logCursor && (
              <div className="dash-row dash-load-more">
                <button onClick={loadMoreLogs} className="btn--secondary">
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
