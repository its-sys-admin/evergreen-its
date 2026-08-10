import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import * as api from "../lib/fieldops_expected_materials";
import * as manifests from "../lib/fieldops_manifests";
import { ManifestValidatePage } from "./ManifestValidatePage";
import { fetchMaterials, type CatalogRow } from "../lib/fieldops_materials";
import { useAuth } from "../lib/auth";
import { errorText } from "../lib/errorCopy";
import { PageShell } from "../components/PageShell";
import { ConfirmDelete, planRenumber, nextSeq } from "../components/ChecklistItemForm";
import {
  EMPTY_FORM,
  ExpectationForm,
  formFromRow,
  rollupPill,
  rowTitle,
  toFields,
  type FormState,
} from "../components/ExpectedMaterialsSection";
import { pacificToday } from "../components/myTasksShared";

// Per-job MATERIALS TRACKING page (PR2) — the deep-link target from the Job Tracker's
// "Materials tracking →" and from the daily field report's material-receipt region.
//
// It is the home for what the job is expecting and what has actually turned up:
//   • cap.materials.manage (admin/office) — authors the expected list (part number, category,
//     qty/unit, expected SHIP and DELIVERY dates), and the scheduled LOADS under each line.
//   • cap.materials.receive + a manager/admin role — marks each line Delivered / Partially
//     delivered / Not delivered, each with a note, optionally against a specific load.
//   • Everyone with cap.materials.receive reads it.
// The Worker re-gates every call; the capability checks here drive affordances only
// (Invariant 2 — SPA gating is convenience, never the boundary).
//
// A line's delivery state is a ROLLUP over the append-only receipt ledger, not a flag: a part
// number that arrives across five trucks gets five events, and the line shows the running total.
// The editor is IMPORTED from ExpectedMaterialsSection rather than re-implemented — /update
// full-replaces the content fields, so a second form that forgot one would null it on every edit.

/** One delivery mark in flight, per line. */
type MarkDraft = { qty: string; note: string; date: string; shipmentId: string };
const emptyMark = (): MarkDraft => ({ qty: "", note: "", date: pacificToday(), shipmentId: "" });

/** How long a delivery-mark button stays ARMED before reverting to its normal label. Long enough
 *  to read the confirm and tap it deliberately, short enough that a forgotten armed button is not
 *  still live when the phone comes back out of a pocket. */
const MARK_ARM_TIMEOUT_MS = 6000;

/** Shipment editor state (strings while typing; parsed at submit). */
type ShipDraft = {
  partNumber: string;
  bol: string;
  carrier: string;
  qty: string;
  unit: string;
  shipDate: string;
  deliveryDate: string;
};
const emptyShip = (): ShipDraft => ({
  partNumber: "", bol: "", carrier: "", qty: "", unit: "", shipDate: "", deliveryDate: "",
});

function shipToFields(d: ShipDraft): api.MaterialShipmentFields | string {
  const out: api.MaterialShipmentFields = {};
  if (d.partNumber.trim()) out.part_number = d.partNumber.trim();
  if (d.bol.trim()) out.bol_number = d.bol.trim();
  if (d.carrier.trim()) out.carrier = d.carrier.trim();
  if (d.unit.trim()) out.unit = d.unit.trim();
  if (d.shipDate.trim()) out.ship_date = d.shipDate.trim();
  if (d.deliveryDate.trim()) out.delivery_date = d.deliveryDate.trim();
  if (d.qty.trim() !== "") {
    const n = Number(d.qty);
    if (!Number.isFinite(n) || n <= 0) return "Load quantity must be a number greater than 0.";
    out.qty = n;
  }
  return out;
}

function fmtQty(n: number | null): string {
  return n == null ? "—" : String(n);
}

/** Wire code → plain language via the CANONICAL registry (src/lib/errorCopy.ts), not a local map:
 *  every field-ops Worker error code is required to have an entry there
 *  (tests/test_error_copy_parity.py gates it), and a second map here would drift from it. */
function errText(e: unknown, fallback: string): string {
  const code = e instanceof Error ? e.message : "";
  return code ? errorText(code) : fallback;
}

export function JobMaterialsPage({
  jobId,
  onHome,
  onOpenJob,
}: {
  jobId: string;
  onHome: () => void;
  onOpenJob: (jobId: string) => void;
}) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.materials.manage");
  // Mirrors the Worker's requireDailyReportRole — marking is a manager/admin action.
  const canMark = caps.includes("cap.materials.receive") && (user?.role === "manager" || user?.role === "admin");

  const [data, setData] = useState<{
    lines: api.ExpectedMaterialRow[];
    shipments: api.MaterialShipmentRow[];
    events: api.MaterialReceiptEventRow[];
  } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const [catalog, setCatalog] = useState<CatalogRow[]>([]);
  const [catalogError, setCatalogError] = useState(false);

  const [addForm, setAddForm] = useState<FormState>({ ...EMPTY_FORM });
  const [editId, setEditId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<FormState>({ ...EMPTY_FORM });
  const [marks, setMarks] = useState<Record<number, MarkDraft>>({});
  const [shipFor, setShipFor] = useState<number | null>(null);
  const [shipDraft, setShipDraft] = useState<ShipDraft>(emptyShip());

  // ── Two-step confirm on every delivery mark (operator request, 2026-08-10) ────────────────
  // A mark is an APPEND-ONLY ledger event with no delete path: a mis-tap cannot be undone, only
  // corrected by recording a compensating event, and until someone notices, the running total and
  // the §51 Material Receipts mirror both read wrong. These three buttons also sit right next to
  // each other on a phone held on a job site. So the first click ARMS the button and the second
  // commits.
  //
  // Arming EXPIRES. A button left armed — the manager got distracted, pocketed the phone — must
  // not still be one stray tap from filing a delivery minutes later; after the timeout it reverts
  // to its normal label and the next click merely re-arms.
  //
  // Arming is keyed by (line, kind), so clicking a DIFFERENT button re-arms that one instead of
  // committing: changing your mind from "Delivered" to "Not delivered" can never commit the first.
  const [armed, setArmed] = useState<{ lineId: number; kind: api.MaterialReceiptKind } | null>(null);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(null), MARK_ARM_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [armed]);

  // Manifest import (PR3b). Deliberately NOT routed through `busyId` — that sentinel space is
  // shared with the add-line form (busyId = -1), so a manifest op reusing it would grey out an
  // unrelated form. Its own state, its own single-flight.
  const [manifestList, setManifestList] = useState<manifests.ManifestListRow[] | null>(null);
  const [openManifest, setOpenManifest] = useState<number | null>(null);
  const [manifestBusy, setManifestBusy] = useState(false);

  const load = useCallback(() => {
    setLoadError(null);
    api
      .fetchExpectedMaterials(jobId)
      .then((d) =>
        setData({ lines: d.expected_materials, shipments: d.shipments, events: d.receipt_events }),
      )
      .catch(() => setLoadError("Could not load this job's materials."));
  }, [jobId]);

  // The manifest list is a SEPARATE read: it must survive a materials-load failure (a manifest
  // still needs discarding even if the line list is down), and it refreshes on its own after an
  // upload or a commit.
  const loadManifests = useCallback(() => {
    if (!canManage) return;
    manifests
      .fetchManifests(jobId)
      .then((d) => setManifestList(d.manifests))
      .catch(() => setManifestList([]));
  }, [jobId, canManage]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadManifests();
  }, [loadManifests]);

  useEffect(() => {
    if (!canManage) return;
    fetchMaterials()
      .then((d) => setCatalog(d.materials))
      .catch(() => setCatalogError(true));
  }, [canManage]);

  const shipmentsByLine = useMemo(() => {
    const m = new Map<number, api.MaterialShipmentRow[]>();
    for (const s of data?.shipments ?? []) m.set(s.line_id, [...(m.get(s.line_id) ?? []), s]);
    return m;
  }, [data]);

  const eventsByLine = useMemo(() => {
    const m = new Map<number, api.MaterialReceiptEventRow[]>();
    for (const e of data?.events ?? []) m.set(e.line_id, [...(m.get(e.line_id) ?? []), e]);
    return m;
  }, [data]);

  // Group by BOM category when any line carries one — a 90-line BOM is unreadable flat.
  const groups = useMemo(() => {
    const lines = data?.lines ?? [];
    if (!lines.some((l) => l.category)) return [{ name: null as string | null, lines }];
    const byCat = new Map<string, api.ExpectedMaterialRow[]>();
    for (const l of lines) {
      const key = l.category ?? "Uncategorised";
      byCat.set(key, [...(byCat.get(key) ?? []), l]);
    }
    return [...byCat.entries()].map(([name, ls]) => ({ name, lines: ls }));
  }, [data]);

  async function run(id: number, fn: () => Promise<unknown>, okText: string) {
    if (busyId !== null) return;
    setBusyId(id);
    setMsg(null);
    try {
      await fn();
      load();
      setMsg({ ok: true, text: okText });
    } catch (e) {
      setMsg({ ok: false, text: errText(e, "That didn't work — try again.") });
    } finally {
      setBusyId(null);
    }
  }

  function onAdd(e: FormEvent) {
    e.preventDefault();
    const fields = toFields(addForm);
    if (typeof fields === "string") {
      setMsg({ ok: false, text: fields });
      return;
    }
    void run(-1, async () => {
      await api.createExpectedMaterial(jobId, { ...fields, seq: nextSeq(data?.lines ?? []) });
      setAddForm({ ...EMPTY_FORM });
    }, "Added.");
  }

  function onEdit(e: FormEvent) {
    e.preventDefault();
    if (editId === null) return;
    const fields = toFields(editForm);
    if (typeof fields === "string") {
      setMsg({ ok: false, text: fields });
      return;
    }
    void run(editId, async () => {
      await api.updateExpectedMaterial(editId, fields);
      setEditId(null);
    }, "Saved.");
  }

  function onMark(line: api.ExpectedMaterialRow, kind: api.MaterialReceiptKind) {
    // First click on this (line, kind) only ARMS it — see the `armed` state above for why a
    // delivery mark is not a one-click action.
    if (!(armed && armed.lineId === line.id && armed.kind === kind)) {
      setArmed({ lineId: line.id, kind });
      return;
    }
    setArmed(null);
    const d = marks[line.id] ?? emptyMark();
    const body: Parameters<typeof api.markReceipt>[1] = { kind, event_date: d.date || undefined };
    if (d.note.trim()) body.note = d.note.trim();
    if (d.shipmentId) body.shipment_id = Number(d.shipmentId);
    // not_delivered carries no quantity — the Worker refuses one rather than dropping it silently,
    // so don't send one from here either.
    if (kind !== "not_delivered" && d.qty.trim() !== "") {
      const n = Number(d.qty);
      if (!Number.isFinite(n) || n <= 0) {
        setMsg({ ok: false, text: "Quantity must be a number greater than 0." });
        return;
      }
      body.qty = n;
    }
    void run(line.id, async () => {
      await api.markReceipt(line.id, body);
      setMarks((m) => ({ ...m, [line.id]: emptyMark() }));
    }, "Delivery recorded.");
  }

  function onAddShipment(e: FormEvent) {
    e.preventDefault();
    if (shipFor === null) return;
    const fields = shipToFields(shipDraft);
    if (typeof fields === "string") {
      setMsg({ ok: false, text: fields });
      return;
    }
    void run(shipFor, async () => {
      await api.createMaterialShipment(shipFor, fields);
      setShipDraft(emptyShip());
      setShipFor(null);
    }, "Load added.");
  }

  // planRenumber takes the row's INDEX in the ordered list (not its id) and returns
  // {row, seq} pairs — the checklist 10/20/30 renumber convention, one call per changed row.
  function onMove(line: api.ExpectedMaterialRow, dir: -1 | 1) {
    const lines = data?.lines ?? [];
    const idx = lines.findIndex((l) => l.id === line.id);
    const plan = planRenumber(lines, idx, dir);
    if (!plan.length) return;
    void run(line.id, async () => {
      for (const p of plan) await api.setExpectedMaterialSeq(p.row.id, p.seq);
    }, "Reordered.");
  }

  const busy = busyId !== null;

  async function uploadManifestFile(file: File) {
    if (manifestBusy) return;
    if (file.size > manifests.MANIFEST_MAX_BYTES) {
      setMsg({ ok: false, text: "That file is too large to import — split it or export a smaller range." });
      return;
    }
    setManifestBusy(true);
    setMsg(null);
    try {
      await manifests.uploadManifest(jobId, file);
      loadManifests();
      setMsg({
        ok: true,
        text: "Uploaded. It will be read in a minute or two — refresh to see it ready to check.",
      });
    } catch (e) {
      const code = e && typeof e === "object" && "code" in e ? (e as { code: string | null }).code : null;
      setMsg({ ok: false, text: code ? errorText(code) : "Could not upload that file." });
    } finally {
      setManifestBusy(false);
    }
  }

  return (
    <PageShell onHome={onHome}>
      <div className="dash-back-btn">
        <button type="button" className="btn btn--secondary" onClick={() => onOpenJob(jobId)}>
          ← Back to job
        </button>
      </div>
      <h1 className="page__heading">Materials — {jobId}</h1>
      <p className="dash__intro">
        What this job is expecting, when each part is due to ship and arrive, and what has actually
        turned up. {canMark ? "Mark each line as loads arrive — a line can be marked more than once." : null}
      </p>

      {msg && (
        <p className={msg.ok ? "dash__msg" : "login__error"} role={msg.ok ? "status" : "alert"}>
          {msg.text}
        </p>
      )}

      {openManifest !== null ? (
        <ManifestValidatePage
          key={openManifest}
          manifestId={openManifest}
          onClose={(notice, committed) => {
            setOpenManifest(null);
            if (notice) setMsg(notice);
            loadManifests();
            // A commit inserts expected-material lines, so the list behind this screen is stale
            // the moment we finish.
            if (committed) load();
          }}
        />
      ) : null}
      {catalogError && canManage && (
        <p className="login__error" role="alert">
          The material catalog didn&apos;t load — you can still add lines as free text.
        </p>
      )}

      {loadError && (
        <p className="login__error" role="alert">
          {loadError}{" "}
          <button type="button" className="btn btn--secondary" onClick={load}>
            Retry
          </button>
        </p>
      )}

      {data === null && !loadError && <p className="dash__intro">Loading…</p>}

      {data !== null && data.lines.length === 0 && (
        <p className="dash__intro">
          Nothing is on this job&apos;s materials list yet.
          {canManage ? " Add the first line below." : ""}
        </p>
      )}

      {canManage && openManifest === null ? (
        <section className="card dash-section" aria-label="Import a manifest">
          <h3 className="dash-detail__h2">Import from a document</h3>
          <p className="dash__intro">
            Upload a bill of materials or a shipping log (PDF or Excel). It is read on the office
            Mac and comes back as a grid you check before anything is added — nothing reaches this
            list until you import it.
          </p>
          <div className="dash-row">
            <input
              type="file"
              accept={manifests.MANIFEST_ACCEPT}
              aria-label="Manifest file"
              disabled={manifestBusy}
              onChange={(e) => {
                const f = e.target.files?.[0];
                // Clear the picker so re-choosing the SAME file after a failure still fires.
                e.target.value = "";
                if (f) void uploadManifestFile(f);
              }}
            />
            {manifestBusy ? <span>Uploading…</span> : null}
          </div>

          {manifestList && manifestList.length > 0 ? (
            <div style={{ overflowX: "auto" }}>
              <table className="dash-table">
                <thead>
                  <tr>
                    <th scope="col">File</th>
                    <th scope="col">Status</th>
                    <th scope="col">Rows</th>
                    <th scope="col" />
                  </tr>
                </thead>
                <tbody>
                  {manifestList.map((m) => (
                    <tr key={m.id}>
                      <td>{m.filename}</td>
                      <td>
                        {m.status === "parsed"
                          ? "Ready to check"
                          : m.status === "pending" || m.status === "claimed"
                            ? "Being read…"
                            : m.status === "refused"
                              ? `Refused${m.detail ? ` — ${m.detail}` : ""}`
                              : m.status}
                      </td>
                      <td>{m.row_count ?? "—"}</td>
                      <td>
                        <button
                          type="button"
                          className="btn btn--secondary"
                          aria-label={`Open ${m.filename}`}
                          disabled={m.status === "pending" || m.status === "claimed"}
                          onClick={() => setOpenManifest(m.id)}
                        >
                          {m.status === "parsed" ? "Check & import" : "Open"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>
      ) : null}

      {groups.map((group) => (
        <section key={group.name ?? "__all"} className="card dash-section" aria-label={group.name ?? "Materials"}>
          {group.name && <h3 className="dash-detail__h2">{group.name}</h3>}
          {group.lines.map((line) => {
            const pill = rollupPill(line);
            const loads = shipmentsByLine.get(line.id) ?? [];
            const history = eventsByLine.get(line.id) ?? [];
            const draft = marks[line.id] ?? emptyMark();
            const setDraft = (patch: Partial<MarkDraft>) =>
              setMarks((m) => ({ ...m, [line.id]: { ...draft, ...patch } }));
            return (
              <div key={line.id} className="dash-card">
                <div className="dash-card__head">
                  <span className={pill.className}>{pill.label}</span>{" "}
                  <strong>{rowTitle(line)}</strong>
                  {line.status === "incident" && (
                    <>
                      {" "}
                      <span className="dash-pill dash-pill--danger">Problem reported</span>
                    </>
                  )}
                </div>
                <div className="dash-card__sub">
                  {line.part_number ? `Part ${line.part_number} · ` : ""}
                  Expected {fmtQty(line.qty)} {line.unit ?? ""}
                  {line.qty_received_total != null
                    ? ` · received ${fmtQty(line.qty_received_total)}`
                    : ""}
                  {line.expected_ship_date ? ` · ships ${line.expected_ship_date}` : ""}
                  {line.expected_date ? ` · due ${line.expected_date}` : ""}
                </div>

                {loads.length > 0 && (
                  <ul className="dash-tasklist" aria-label={`Loads for ${rowTitle(line)}`}>
                    {loads.map((s) => (
                      <li key={s.id}>
                        {s.bol_number ?? "(no BOL)"} · {fmtQty(s.qty)} {s.unit ?? ""}
                        {s.ship_date ? ` · ships ${s.ship_date}` : ""}
                        {s.delivery_date ? ` → arrives ${s.delivery_date}` : ""}
                        {s.carrier ? ` · ${s.carrier}` : ""}
                        {canManage && (
                          <>
                            {" "}
                            <ConfirmDelete
                              actionLabel="Remove"
                              ariaLabel={`Remove load ${s.bol_number ?? s.id}`}
                              copy="Remove this load?"
                              busy={busy}
                              onConfirm={() =>
                                void run(line.id, () => api.deactivateMaterialShipment(s.id), "Load removed.")
                              }
                            />
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                )}

                {canMark && (
                  <div className="dash-row" aria-label={`Mark delivery for ${rowTitle(line)}`}>
                    <input
                      aria-label={`Quantity received for ${rowTitle(line)}`}
                      value={draft.qty}
                      onChange={(e) => setDraft({ qty: e.target.value })}
                      placeholder="Qty"
                      inputMode="decimal"
                      size={6}
                    />{" "}
                    <input
                      aria-label={`Note for ${rowTitle(line)}`}
                      value={draft.note}
                      onChange={(e) => setDraft({ note: e.target.value })}
                      placeholder="Note"
                      maxLength={500}
                    />{" "}
                    <input
                      aria-label={`Delivery date for ${rowTitle(line)}`}
                      type="date"
                      value={draft.date}
                      onChange={(e) => setDraft({ date: e.target.value })}
                    />{" "}
                    {loads.length > 0 && (
                      <select
                        aria-label={`Load for ${rowTitle(line)}`}
                        value={draft.shipmentId}
                        onChange={(e) => setDraft({ shipmentId: e.target.value })}
                      >
                        <option value="">— against which load? —</option>
                        {loads.map((s) => (
                          <option key={s.id} value={s.id}>
                            {s.bol_number ?? `load ${s.id}`}
                          </option>
                        ))}
                      </select>
                    )}{" "}
                    {/* Two-step: the first click arms, the second commits. The armed button says
                        so in its LABEL, not only its styling — a colour change alone is not a
                        confirmation prompt, and the aria-label carries the same words so a screen
                        reader hears the state change too. */}
                    {(
                      [
                        { kind: "delivered", label: "Delivered", cls: "btn--primary" },
                        { kind: "partial", label: "Partially delivered", cls: "btn--secondary" },
                        { kind: "not_delivered", label: "Not delivered", cls: "btn--retire" },
                      ] as const
                    ).map(({ kind, label, cls }, i) => {
                      const isArmed = armed?.lineId === line.id && armed.kind === kind;
                      return (
                        <span key={kind}>
                          {i > 0 ? " " : null}
                          <button
                            type="button"
                            className={`btn ${isArmed ? "btn--primary is-armed" : cls}`}
                            disabled={busy}
                            aria-label={
                              isArmed
                                ? `Confirm ${label.toLowerCase()} for ${rowTitle(line)} — tap again to record`
                                : `Mark ${rowTitle(line)} ${label.toLowerCase()}`
                            }
                            onClick={() => onMark(line, kind)}
                          >
                            {isArmed ? `Confirm ${label.toLowerCase()}?` : label}
                          </button>
                        </span>
                      );
                    })}
                  </div>
                )}

                {history.length > 0 && (
                  <ul className="dash-tasklist" aria-label={`Delivery history for ${rowTitle(line)}`}>
                    {history.map((ev) => (
                      <li key={ev.id}>
                        {ev.event_date ?? "—"} · {rollupPill({ ...line, receipt_status: ev.kind }).label}
                        {ev.qty != null ? ` · ${ev.qty}` : ""}
                        {ev.actor_name ? ` · ${ev.actor_name}` : ""}
                        {ev.note ? ` — ${ev.note}` : ""}
                      </li>
                    ))}
                  </ul>
                )}

                {canManage && (
                  <div className="dash-row">
                    {editId === line.id ? (
                      <ExpectationForm
                        label={`Edit ${rowTitle(line)}`}
                        draft={editForm}
                        onChange={setEditForm}
                        onSubmit={onEdit}
                        busy={busy}
                        submitLabel="Save"
                        onCancel={() => setEditId(null)}
                        catalog={catalog}
                      />
                    ) : (
                      <>
                        <button
                          type="button"
                          className="btn btn--edit"
                          disabled={busy}
                          aria-label={`Edit ${rowTitle(line)}`}
                          onClick={() => {
                            setEditId(line.id);
                            setEditForm(formFromRow(line));
                          }}
                        >
                          Edit
                        </button>{" "}
                        <button
                          type="button"
                          className="btn btn--secondary"
                          disabled={busy}
                          aria-label={`Add a load to ${rowTitle(line)}`}
                          onClick={() => {
                            setShipFor(line.id);
                            setShipDraft({ ...emptyShip(), partNumber: line.part_number ?? "" });
                          }}
                        >
                          + Load
                        </button>{" "}
                        <button
                          type="button"
                          className="btn btn--secondary"
                          disabled={busy}
                          aria-label={`Move ${rowTitle(line)} up`}
                          onClick={() => onMove(line, -1)}
                        >
                          ▲
                        </button>{" "}
                        <button
                          type="button"
                          className="btn btn--secondary"
                          disabled={busy}
                          aria-label={`Move ${rowTitle(line)} down`}
                          onClick={() => onMove(line, 1)}
                        >
                          ▼
                        </button>{" "}
                        <ConfirmDelete
                          actionLabel="Remove"
                          ariaLabel={`Remove ${rowTitle(line)}`}
                          copy="Remove this line from the list? Its delivery history is kept."
                          busy={busy}
                          onConfirm={() =>
                            void run(line.id, () => api.deactivateExpectedMaterial(line.id), "Removed.")
                          }
                        />
                      </>
                    )}
                  </div>
                )}

                {canManage && shipFor === line.id && (
                  <form onSubmit={onAddShipment} className="dash-row" aria-label={`Add a load to ${rowTitle(line)}`}>
                    <input
                      aria-label="Load BOL number"
                      value={shipDraft.bol}
                      onChange={(e) => setShipDraft({ ...shipDraft, bol: e.target.value })}
                      placeholder="BOL / load #"
                      maxLength={64}
                    />{" "}
                    <input
                      aria-label="Load carrier"
                      value={shipDraft.carrier}
                      onChange={(e) => setShipDraft({ ...shipDraft, carrier: e.target.value })}
                      placeholder="Carrier"
                      maxLength={64}
                    />{" "}
                    <input
                      aria-label="Load quantity"
                      value={shipDraft.qty}
                      onChange={(e) => setShipDraft({ ...shipDraft, qty: e.target.value })}
                      placeholder="Qty"
                      inputMode="decimal"
                      size={6}
                    />{" "}
                    <input
                      aria-label="Load ship date"
                      type="date"
                      value={shipDraft.shipDate}
                      onChange={(e) => setShipDraft({ ...shipDraft, shipDate: e.target.value })}
                    />{" "}
                    <input
                      aria-label="Load delivery date"
                      type="date"
                      value={shipDraft.deliveryDate}
                      onChange={(e) => setShipDraft({ ...shipDraft, deliveryDate: e.target.value })}
                    />{" "}
                    <button type="submit" className="btn btn--primary" disabled={busy}>
                      Add load
                    </button>{" "}
                    <button
                      type="button"
                      className="btn btn--secondary"
                      onClick={() => setShipFor(null)}
                    >
                      Cancel
                    </button>
                  </form>
                )}
              </div>
            );
          })}
        </section>
      ))}

      {canManage && (
        <section className="card dash-section" aria-label="Add an expected material">
          <h3 className="dash-detail__h2">Add a line</h3>
          <ExpectationForm
            label="Add expected material"
            draft={addForm}
            onChange={setAddForm}
            onSubmit={onAdd}
            busy={busy}
            submitLabel="Add"
            catalog={catalog}
          />
        </section>
      )}
    </PageShell>
  );
}
