import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import {
  owedLabel,
  rollupMaterials,
  lineOwed,
  needsSettledConfirm,
  pacificDateOf,
  pacificTimeOf,
  settledConfirmText,
} from "../lib/materials_view";

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

/** A CORRECTION in flight (0080, decision D3): a mark that replaces a voided one. It carries its
 *  own kind because a correction is often a different kind from the mark it replaces — a
 *  "Delivered 40" that was really "Partially delivered 30". */
type CorrDraft = MarkDraft & { kind: api.MaterialReceiptKind };

/** The voided-entry reason cap. The Worker refuses longer (400 invalid_reason); the box is capped
 *  to match so the refusal can only come from a stale tab or a direct call. */
const VOID_REASON_MAX = 500;

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

/** "2026-10-02 · Delivered · 40" — what a mark SAID: its date, kind and quantity. The pills that
 *  link a correction to the entry it replaced, and the correction form's own copy, name the other
 *  entry with this. */
function eventSummary(line: api.ExpectedMaterialRow, ev: api.MaterialReceiptEventRow): string {
  return `${ev.event_date ?? "—"} · ${rollupPill({ ...line, receipt_status: ev.kind }).label}` +
    (ev.qty != null ? ` · ${ev.qty}` : "");
}

/** "2026-10-02 · Delivered · 40 · recorded 09:47" — the part of a history row that IDENTIFIES the
 *  mark. A double-mark is exactly two rows whose date, kind, quantity and note all match, so
 *  without the moment each was RECORDED (created_at, Pacific) the two rows — and the two Void
 *  buttons a screen reader announces — would be word-for-word the same, and the person voiding
 *  could not say which one they meant. The Void button and the panels name the entry with this. */
function eventRowSummary(line: api.ExpectedMaterialRow, ev: api.MaterialReceiptEventRow): string {
  return `${eventSummary(line, ev)} · recorded ${pacificTimeOf(ev.created_at)}`;
}

/** Worker refusals that are about the void REASON itself. Any other refusal (already voided, the
 *  job closed) leaves the reason box valid: aria-invalid draws the red border, so it may only
 *  point at the box when the box is what is wrong. */
const REASON_CODES = new Set(["reason_required", "invalid_reason"]);
/** …and the ones about a correction's QUANTITY, for the same reason. */
const QTY_CODES = new Set(["qty_required", "invalid_qty"]);

/** An inline error, and whether it is about the input it sits under (→ aria-invalid). */
type FieldError = { text: string; invalid: boolean };

/** D9 — the inline "this line is already in full" question, asked before a delivered/partial mark
 *  on a settled line (lib/materials_view.needsSettledConfirm decides WHEN). Inline, never
 *  window.confirm: a browser lets its user suppress a page's native dialogs, after which confirm()
 *  returns false without asking — and the mark would become silently impossible to record.
 *  `action` names what is being confirmed, in the question, the button and Cancel alike: the
 *  correction form asks about "the correction", not "another delivery". */
function SettledAsk({
  line,
  busy,
  onRecord,
  onCancel,
  action = "Record another delivery",
}: {
  line: api.ExpectedMaterialRow;
  busy: boolean;
  onRecord: () => void;
  onCancel: () => void;
  action?: string;
}) {
  return (
    <div className="mat-settled-ask" role="group" aria-label={`Already received — ${rowTitle(line)}`}>
      <p className="mat-settled-ask__text">{settledConfirmText(line, action)}</p>
      <button type="button" className="btn btn--primary btn--sm" disabled={busy} onClick={onRecord}>
        {action}
      </button>{" "}
      <button
        type="button"
        className="btn btn--secondary btn--sm"
        aria-label={`Cancel — do not ${action.toLowerCase()} for ${rowTitle(line)}`}
        onClick={onCancel}
      >
        Cancel
      </button>
    </div>
  );
}

export function JobMaterialsPage({
  jobId,
  onHome,
  onOpenJob,
  onOpenSchedule,
}: {
  jobId: string;
  onHome: () => void;
  onOpenJob: (jobId: string) => void;
  /** A5: the reverse of the schedule page's Materials drawer — deliveries context lives there. */
  onOpenSchedule?: (jobId: string) => void;
}) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.materials.manage");
  // Mirrors the Worker's requireDailyReportRole — marking is a manager/admin action.
  const canMark = caps.includes("cap.materials.receive") && (user?.role === "manager" || user?.role === "admin");

  const [data, setData] = useState<{
    lines: api.ExpectedMaterialRow[];
    /** D11: lines taken off the list that still carry receipt events. Their deliveries count
     *  until voided, so they stay reachable here — void-only, nothing new recorded against them. */
    removed: api.ExpectedMaterialRow[];
    shipments: api.MaterialShipmentRow[];
    events: api.MaterialReceiptEventRow[];
    project_name: string | null;
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
  // Resolve-problem draft (B7, 2026-08-11): which line's resolve panel is open + its
  // REQUIRED note. Clearing a flag is an explicit human act with a written reason — a
  // delivery mark never does it.
  const [resolveFor, setResolveFor] = useState<number | null>(null);
  const [resolveNote, setResolveNote] = useState("");
  const [resolveError, setResolveError] = useState<string | null>(null);

  // ── Void a delivery mark (0080; operator decisions D1-D3) ──────────────────────────────────
  // A mis-recorded mark used to be permanent: the ledger is append-only and had no delete path,
  // so a double-tapped "Delivered" read as twice the material on this page, the §51 Receipts
  // mirror and the client's weekly report alike. A void KEEPS the row (struck through, with who,
  // when and why) and takes it out of every rollup. Who may void is exactly who may mark (D1) —
  // `canMark` below — and the Worker re-gates role, job scope and an active job.
  //
  // The reason is REQUIRED and is kept with the row: a struck-through delivery with no "why" is
  // a question every later reader has to chase. `voidFor` is the EVENT id whose panel is open.
  const [voidFor, setVoidFor] = useState<number | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voidError, setVoidError] = useState<FieldError | null>(null);
  const voidReasonInput = useRef<HTMLInputElement | null>(null);
  // Voided rows are HIDDEN by default, per line: the history is read to answer "what came", and
  // a struck-through row mostly answers a different question. The toggle says how many it hides,
  // so nothing vanishes silently.
  const [showVoided, setShowVoided] = useState<Record<number, boolean>>({});

  // ── Record the correct entry (D3, D4) ──────────────────────────────────────────────────────
  // Step two, optional: a voided mark that was WRONG (not merely a duplicate) needs the right one
  // recorded, linked back to it (supersedes_id) so the history reads "this replaced that" and the
  // Worker files it in the original's position. Offered straight after a void (the second answer
  // to the `afterVoid` question below) AND for as long as the voided row stays uncorrected — D4
  // has no time window, so coming back next week must work. `corrFor` is the VOIDED event id
  // being corrected.
  const [corrFor, setCorrFor] = useState<number | null>(null);
  const [corrDraft, setCorrDraft] = useState<CorrDraft>({ ...emptyMark(), kind: "delivered" });
  const [corrError, setCorrError] = useState<FieldError | null>(null);
  const [corrAsk, setCorrAsk] = useState(false);
  // Straight after a void the page ASKS rather than opening the correction form: most voids are
  // duplicates, which need nothing more, and a form that opens on its own makes that common case
  // look unfinished — and invites a second mark nobody needed. "Done — it was a duplicate" comes
  // first for that reason. `afterVoid` names the line and the event just voided.
  const [afterVoid, setAfterVoid] = useState<{ lineId: number; eventId: number } | null>(null);

  // Remove-line refusals (D11: 409 line_has_deliveries) are shown AT the line, like the other
  // per-line errors on this page — the banner is off-screen for most of a long BOM.
  const [removeError, setRemoveError] = useState<{ lineId: number; text: string } | null>(null);

  // D9: which (line, kind) is showing the "already received in full — record another?" question.
  // On a settled line this question REPLACES the two-step arm rather than stacking a third tap on
  // it: it is a stronger confirmation than "Confirm delivered?", and it sits below the row, so a
  // double-tap on the button cannot land on its "Record another delivery".
  const [settledAsk, setSettledAsk] = useState<{ lineId: number; kind: api.MaterialReceiptKind } | null>(null);

  // ── Two-step confirm on every delivery mark (operator request, 2026-08-10) ────────────────
  // A mark is an APPEND-ONLY ledger event with no delete path: since 0080 a mis-tap can be VOIDED
  // (kept, struck through, taken out of every total), but only once someone notices — and until
  // then the running total and the §51 Material Receipts mirror both read wrong. These three buttons also sit right next to
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

  // ── Quantity is REQUIRED on Delivered / Partially delivered (2026-08-24) ─────────────────────
  // A mark that does not say HOW MUCH arrived cannot move the outstanding balance: `lineOwed`
  // reads `qty_received_total ?? 0`, so a qty-less delivery leaves the line owing its full amount
  // while the status pill turns green — and the §51 Material List then reads "received" where this
  // page reads "outstanding". The Worker refuses it (`qty_required`); this is the usability half,
  // and it is checked BEFORE the two-step arm so the first tap surfaces the prompt rather than
  // arming a button whose second tap could only produce a 400.
  //
  // The message is per LINE, not the page banner: this page renders a 50-line BOM flat, so the
  // banner at the top is thousands of pixels from the row being marked. `aria-describedby` ties it
  // to the input, and focusing the input scrolls it into view.
  //
  // 'Not delivered' is exempt by design — nothing arrived, so there is no quantity to state.
  const [qtyError, setQtyError] = useState<Record<number, string>>({});
  const qtyInputs = useRef<Record<number, HTMLInputElement | null>>({});

  function clearQtyError(lineId: number) {
    setQtyError((m) => {
      if (m[lineId] === undefined) return m;
      const next = { ...m };
      delete next[lineId];
      return next;
    });
  }

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
        setData({
          lines: d.expected_materials, removed: d.removed_lines, shipments: d.shipments,
          events: d.receipt_events, project_name: d.project_name,
        }),
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

  /**
   * One mutation, single-flight. Resolves TRUE only when the request succeeded — callers holding
   * typed input (a void reason, a resolve note, a correction) clear it ONLY then. It used to
   * resolve the same either way, and the resolve panel's `.then` wiped the note and closed the
   * panel on a refusal too: the person lost what they wrote AND the place the error belonged.
   *
   * `onFail` routes the error copy to an INLINE slot beside the input that caused it instead of
   * the page banner. This page renders a whole BOM flat, so the banner is often thousands of
   * pixels above the row being worked on (the qty-required precedent below); and showing both
   * would announce the same refusal twice to a screen reader. It also gets the Worker's machine
   * code, so the caller can tell a refusal OF the typed input from a refusal of the action.
   */
  async function run(
    id: number,
    fn: () => Promise<unknown>,
    okText: string,
    onFail?: (text: string, code: string) => void,
  ): Promise<boolean> {
    if (busyId !== null) return false;
    setBusyId(id);
    setMsg(null);
    try {
      await fn();
      load();
      setMsg({ ok: true, text: okText });
      return true;
    } catch (e) {
      const text = errText(e, "That didn't work — try again.");
      if (onFail) onFail(text, e instanceof Error ? e.message : "");
      else setMsg({ ok: false, text });
      return false;
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

  /** The quantity a mark of this kind carries, or the message to show beside the box. */
  function readMarkQty(
    kind: api.MaterialReceiptKind,
    raw: string,
  ): { qty: number | null } | string {
    // not_delivered carries no quantity — the Worker refuses one rather than dropping it silently,
    // so don't send one from here either.
    if (kind === "not_delivered") return { qty: null };
    const t = raw.trim();
    if (t === "") return "Enter the quantity received before recording this delivery.";
    const n = Number(t);
    if (!Number.isFinite(n) || n <= 0) return "Quantity must be a number greater than 0.";
    return { qty: n };
  }

  function onMark(line: api.ExpectedMaterialRow, kind: api.MaterialReceiptKind) {
    const d = marks[line.id] ?? emptyMark();
    // Validated BEFORE the arm: an armed button says "Confirm delivered?", which promises the mark
    // is ready to record. Arming an unrecordable mark would make that promise falsely and spend the
    // user's second tap on a 400.
    const parsed = readMarkQty(kind, d.qty);
    if (typeof parsed === "string") {
      setArmed(null);
      setQtyError((m) => ({ ...m, [line.id]: parsed }));
      qtyInputs.current[line.id]?.focus();
      return;
    }
    clearQtyError(line.id);
    // D9: the line already has everything it was owed. Ask in words — with the numbers — instead
    // of arming; the answer to this question IS the second step (see `settledAsk`).
    if (needsSettledConfirm(line, kind)) {
      setArmed(null);
      setSettledAsk({ lineId: line.id, kind });
      return;
    }
    setSettledAsk(null);
    // First click on this (line, kind) only ARMS it — see the `armed` state above for why a
    // delivery mark is not a one-click action.
    if (!(armed && armed.lineId === line.id && armed.kind === kind)) {
      setArmed({ lineId: line.id, kind });
      return;
    }
    setArmed(null);
    commitMark(line, kind, d, parsed.qty);
  }

  /** "Record another delivery" on the D9 question. Re-reads and RE-VALIDATES the draft: the
   *  quantity box stays editable while the question is up, so what was valid when it was asked
   *  is not necessarily what would be posted now. */
  function onSettledRecord(line: api.ExpectedMaterialRow) {
    if (!settledAsk || settledAsk.lineId !== line.id) return;
    const kind = settledAsk.kind;
    setSettledAsk(null);
    const d = marks[line.id] ?? emptyMark();
    const parsed = readMarkQty(kind, d.qty);
    if (typeof parsed === "string") {
      setQtyError((m) => ({ ...m, [line.id]: parsed }));
      qtyInputs.current[line.id]?.focus();
      return;
    }
    commitMark(line, kind, d, parsed.qty);
  }

  function commitMark(
    line: api.ExpectedMaterialRow,
    kind: api.MaterialReceiptKind,
    d: MarkDraft,
    qty: number | null,
  ) {
    const body: Parameters<typeof api.markReceipt>[1] = { kind, event_date: d.date || undefined };
    if (d.note.trim()) body.note = d.note.trim();
    if (d.shipmentId) body.shipment_id = Number(d.shipmentId);
    if (qty !== null) body.qty = qty;
    void run(line.id, async () => {
      await api.markReceipt(line.id, body);
      setMarks((m) => ({ ...m, [line.id]: emptyMark() }));
      clearQtyError(line.id);
    }, "Delivery recorded.");
  }

  function openVoid(ev: api.MaterialReceiptEventRow) {
    setVoidFor(ev.id);
    setVoidReason("");
    setVoidError(null);
    setAfterVoid(null);
  }

  /** `removed` — the line is off the list (D11): the void goes through, but nothing is offered
   *  after it, because a correction is a new mark and the Worker records none on a removed line. */
  function onConfirmVoid(line: api.ExpectedMaterialRow, ev: api.MaterialReceiptEventRow, removed: boolean) {
    const reason = voidReason.trim();
    // Checked here, not by disabling the button: a disabled "Confirm void" says nothing about
    // WHY it will not go, and a silent no-op is how a person comes to believe a void happened.
    if (!reason) {
      setVoidError({ text: "Say why this entry is being voided — the reason is kept with it.", invalid: true });
      voidReasonInput.current?.focus();
      return;
    }
    setVoidError(null);
    void run(
      line.id,
      () => api.voidReceiptEvent(ev.id, reason),
      "Entry voided — it stays in the history, struck through, and no longer counts toward the received total.",
      (text, code) => setVoidError({ text, invalid: REASON_CODES.has(code) }),
    ).then((ok) => {
      // On a refusal NOTHING is cleared: the panel, the typed reason and the inline error stay.
      if (!ok) return;
      setVoidFor(null);
      setVoidReason("");
      // D3: ask whether a correction is needed (see `afterVoid`) — never on a removed line.
      if (!removed) setAfterVoid({ lineId: line.id, eventId: ev.id });
    });
  }

  /** Open the correction form for a VOIDED mark, prefilled from it — the usual fix is one field
   *  (the quantity, or the kind), so retyping the date, note and load would be busywork and a
   *  fresh chance to get them wrong. The line's voided rows are shown while it is open: they are
   *  hidden by default, and the entry being replaced is the one row this form is about. */
  function openCorrection(line: api.ExpectedMaterialRow, ev: api.MaterialReceiptEventRow) {
    const loads = shipmentsByLine.get(line.id) ?? [];
    setAfterVoid(null);
    setShowVoided((m) => ({ ...m, [line.id]: true }));
    setCorrFor(ev.id);
    setCorrError(null);
    setCorrAsk(false);
    setCorrDraft({
      kind: ev.kind,
      qty: ev.qty != null ? String(ev.qty) : "",
      note: ev.note ?? "",
      date: ev.event_date ?? pacificToday(),
      // Only a load still on the line: a removed load's id would be refused (invalid_shipment_id),
      // and a <select> whose value matches no option would display as blank while posting it.
      shipmentId:
        ev.shipment_id != null && loads.some((s) => s.id === ev.shipment_id) ? String(ev.shipment_id) : "",
    });
  }

  function onRecordCorrection(line: api.ExpectedMaterialRow, confirmed: boolean) {
    if (corrFor === null) return;
    const supersedes = corrFor;
    // Held to the same quantity rule as any mark — a correction that does not say how much
    // arrived could not fix the received total it exists to fix.
    const parsed = readMarkQty(corrDraft.kind, corrDraft.qty);
    if (typeof parsed === "string") {
      setCorrError({ text: parsed, invalid: true });
      return;
    }
    // D9 applies here too: the void has already taken the wrong mark out of the total, so a line
    // that is STILL settled after it would be over-counted by this one.
    if (!confirmed && needsSettledConfirm(line, corrDraft.kind)) {
      setCorrAsk(true);
      return;
    }
    setCorrAsk(false);
    setCorrError(null);
    const body: Parameters<typeof api.markReceipt>[1] = {
      kind: corrDraft.kind,
      supersedes_id: supersedes,
    };
    if (corrDraft.date) body.event_date = corrDraft.date;
    if (corrDraft.note.trim()) body.note = corrDraft.note.trim();
    if (corrDraft.shipmentId) body.shipment_id = Number(corrDraft.shipmentId);
    if (parsed.qty !== null) body.qty = parsed.qty;
    void run(
      line.id,
      () => api.markReceipt(line.id, body),
      "Correct entry recorded — it takes the voided entry's place in the history.",
      (text, code) => setCorrError({ text, invalid: QTY_CODES.has(code) }),
    ).then((ok) => {
      if (ok) setCorrFor(null);
    });
  }

  function onRemoveLine(line: api.ExpectedMaterialRow) {
    setRemoveError(null);
    void run(
      line.id,
      () => api.deactivateExpectedMaterial(line.id),
      "Removed.",
      (text) => setRemoveError({ lineId: line.id, text }),
    );
  }

  /**
   * One line's delivery history: the marks in the Worker's order (newest first by the ledger's
   * order key, so a correction sits where the mark it replaced did), each with its Void action
   * and — once voided — who, when and why; then the Show/Hide voided toggle. Shared by the list's own lines
   * and the "Removed from the list" group (D11), so the void rules have ONE rendering.
   *
   * `removed` makes it void-only: a removed line's marks still count until voided, so Void stays,
   * but "Record the correct entry" does not — a correction is a new mark, and the Worker records
   * none on a removed line (W2).
   */
  function historyBlock(line: api.ExpectedMaterialRow, removed: boolean) {
    const history = eventsByLine.get(line.id) ?? [];
    const byId = new Map(history.map((e) => [e.id, e] as const));
    // Voided marks stay in the ledger and on the wire; the page hides them unless asked.
    const voidedCount = history.filter((ev) => ev.voided_at !== null).length;
    const shownHistory = showVoided[line.id] ? history : history.filter((ev) => ev.voided_at === null);
    return (
      <>
        {shownHistory.length > 0 && (
          <ul className="dash-tasklist" aria-label={`Delivery history for ${rowTitle(line)}`}>
            {shownHistory.map((ev) => {
              const summary = eventRowSummary(line, ev);
              const isVoided = ev.voided_at !== null;
              // The pills NAME the linked entry. "Corrects an earlier entry" left the reader to
              // hunt for which one — on a line with several voided marks, a guess.
              const corrects = ev.supersedes_id !== null ? byId.get(ev.supersedes_id) : undefined;
              const correctedBy = ev.superseded_by_id !== null ? byId.get(ev.superseded_by_id) : undefined;
              return (
                <li key={ev.id}>
                  {/* Struck through, NOT removed: a voided mark is still part of the record
                      of what was claimed at the gate — it just no longer counts. */}
                  <span className={isVoided ? "mat-event--voided" : undefined}>
                    {summary}
                    {ev.actor_name ? ` · ${ev.actor_name}` : ""}
                    {ev.note ? ` — ${ev.note}` : ""}
                  </span>
                  {ev.supersedes_id !== null && (
                    <span className="dash-pill mat-event__link-pill">
                      {corrects ? `Corrects ${eventSummary(line, corrects)}` : "Corrects an earlier entry"}
                    </span>
                  )}
                  {isVoided && <span className="dash-pill dash-pill--danger">Voided</span>}
                  {isVoided && ev.superseded_by_id !== null && (
                    <span className="dash-pill mat-event__link-pill">
                      {correctedBy ? `Corrected by ${eventSummary(line, correctedBy)}` : "Corrected"}
                    </span>
                  )}
                  {canMark && !isVoided && voidFor !== ev.id && (
                    <button
                      type="button"
                      className="btn btn--danger btn--sm"
                      disabled={busy}
                      aria-label={`Void entry ${summary} for ${rowTitle(line)}`}
                      onClick={() => openVoid(ev)}
                    >
                      Void
                    </button>
                  )}
                  {/* Offered for as long as the voided row is uncorrected — not only in the
                      moment after the void (decision D4 has no time window). Quieter than the
                      post-void question's button: this row is history, and its action should
                      not shout over the marks that still count. */}
                  {canMark && !removed && isVoided && ev.superseded_by_id === null && corrFor !== ev.id && (
                    <button
                      type="button"
                      className="btn btn--secondary btn--sm mat-quiet-btn"
                      disabled={busy}
                      aria-label={`Record the correct entry in place of ${summary} for ${rowTitle(line)}`}
                      onClick={() => openCorrection(line, ev)}
                    >
                      Record the correct entry
                    </button>
                  )}
                  {isVoided && (
                    // Who, when and WHY — the reason is the point of requiring one. The name is a
                    // roster display name or, for an unlinked account, its role label (decision
                    // D6); never a username. A subcontractor account gets neither name nor reason
                    // from the Worker (W7) and sees only that the mark was voided, and when. The
                    // date is Pacific YYYY-MM-DD, the same form as the event date it sits under.
                    <span className="dash-card__sub mat-event__void-note">
                      {[
                        "Voided",
                        ev.voided_by_name ? `by ${ev.voided_by_name}` : null,
                        ev.voided_at ? `on ${pacificDateOf(ev.voided_at)}` : null,
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      {ev.void_reason ? ` — ${ev.void_reason}` : ""}
                    </span>
                  )}
                  {voidFor === ev.id && (
                    // The ConfirmDelete shape (danger action → inline group with the
                    // consequence in words + a danger Confirm + Cancel), with the required
                    // reason inside it. Inline, never window.confirm.
                    <div
                      className="dash-row mat-void"
                      role="group"
                      aria-label={`Void entry ${summary} for ${rowTitle(line)} confirmation`}
                    >
                      <p className="dash-card__sub mat-void__copy">
                        {removed
                          ? "Voiding keeps this entry in the history, struck through, and takes it " +
                            "out of every total that still counts it."
                          : "Voiding keeps this entry in the history, struck through, and takes it " +
                            "out of the received total. If the mark was wrong rather than a " +
                            "duplicate, you can record the correct entry next."}
                      </p>
                      <input
                        ref={voidReasonInput}
                        aria-label="Reason for voiding this entry"
                        placeholder="Why is it being voided? (required)"
                        maxLength={VOID_REASON_MAX}
                        value={voidReason}
                        aria-invalid={voidError?.invalid ? true : undefined}
                        aria-describedby={voidError ? `void-error-${ev.id}` : undefined}
                        onChange={(e) => {
                          setVoidReason(e.target.value);
                          setVoidError(null);
                        }}
                      />{" "}
                      {/* Retire, not the small utility-delete: a void changes the received total,
                          the §51 mirror and the client's weekly report — this screen's
                          destructive lifecycle action (global.css design language). */}
                      <button
                        type="button"
                        className="btn btn--retire btn--sm"
                        disabled={busy}
                        onClick={() => onConfirmVoid(line, ev, removed)}
                      >
                        Confirm void
                      </button>{" "}
                      <button
                        type="button"
                        className="btn btn--secondary btn--sm"
                        aria-label="Cancel void"
                        onClick={() => {
                          setVoidFor(null);
                          setVoidReason("");
                          setVoidError(null);
                        }}
                      >
                        Cancel
                      </button>
                      {voidError && (
                        <p className="mat-mark-error" id={`void-error-${ev.id}`} role="alert">
                          {voidError.text}
                        </p>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {voidedCount > 0 && (
          <div className="dash-row">
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              aria-expanded={!!showVoided[line.id]}
              onClick={() => setShowVoided((m) => ({ ...m, [line.id]: !m[line.id] }))}
            >
              {showVoided[line.id] ? `Hide voided (${voidedCount})` : `Show voided (${voidedCount})`}
            </button>
          </div>
        )}
      </>
    );
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
    <PageShell onHome={onHome} wide>
      <div className="dash-back-btn">
        <button type="button" className="btn btn--secondary" onClick={() => onOpenJob(jobId)}>
          ← Back to job
        </button>
      {onOpenSchedule && (
        <button type="button" className="btn btn--secondary" onClick={() => onOpenSchedule(jobId)}>
          Schedule deliveries →
        </button>
      )}
      </div>
      {/* The job NAME, not the JOB-###### key (operator request 2026-08-11) — the key is a
          system identifier the field never speaks; the id falls back only while loading. */}
      <h1 className="page__heading">Materials — {data?.project_name ?? jobId}</h1>

      {/* WHAT IS STILL OWED. The lane recorded expected and received and never subtracted
          them, so this — the question the page exists for — was unanswerable. Derived in
          lib/materials_view.ts from fields already on the wire; no Worker change. */}
      {data !== null && data.lines.length > 0 ? (() => {
        const roll = rollupMaterials(data.lines);
        return (
          <div className="mat-rollup" aria-label="Materials summary">
            <span className="mat-rollup__stat">
              <span className={`mat-rollup__figure${roll.short > 0 ? " mat-rollup__figure--short" : ""}`}>
                {roll.short}
              </span>
              <span className="mat-rollup__label">Lines still owed</span>
            </span>
            <span className="mat-rollup__stat">
              <span className="mat-rollup__figure">{roll.settled}</span>
              <span className="mat-rollup__label">Settled</span>
            </span>
            <span className="mat-rollup__stat">
              <span className="mat-rollup__figure">{roll.lines}</span>
              <span className="mat-rollup__label">Lines total</span>
            </span>
            {roll.incidents > 0 ? (
              <span className="mat-rollup__stat">
                <span className="mat-rollup__figure mat-rollup__figure--short">{roll.incidents}</span>
                <span className="mat-rollup__label">Problems</span>
              </span>
            ) : null}
            {roll.over > 0 ? (
              <span className="mat-rollup__stat">
                <span className="mat-rollup__figure">{roll.over}</span>
                <span className="mat-rollup__label">Over-delivered</span>
              </span>
            ) : null}
            {roll.unquantified ? (
              <span className="mat-rollup__stat">
                <span className="mat-rollup__label" style={{ textTransform: "none", letterSpacing: 0 }}>
                  Some lines carry no expected quantity, so the owed count is not the whole picture.
                </span>
              </span>
            ) : null}
          </div>
        );
      })() : null}
      <p className="dash__intro">
        What this job is expecting, when each part is due to ship and arrive, and what has actually
        turned up.{" "}
        {canMark
          ? "Mark each line as loads arrive — a line can be marked more than once. A wrong mark can be " +
            "voided from its line's delivery history."
          : null}
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
                            : m.status === "committed"
                              ? "Imported"
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
                        {/* Remove (operator request 2026-08-11): a refused or stale upload
                            used to sit in this list forever with no way off it. Discards
                            the manifest — same action as the validate screen's button; the
                            row is retained in D1 for audit but stops rendering. An
                            IMPORTED manifest keeps its row (it is the provenance of lines
                            now on the list; the Worker refuses its discard anyway). */}
                        {m.status !== "committed" ? (
                          <>
                            {" "}
                            <ConfirmDelete
                              actionLabel="Remove"
                              ariaLabel={`Remove ${m.filename}`}
                              copy={
                                m.status === "committing"
                                  ? "Stop this import and remove it? Lines already imported stay on the list."
                                  : "Remove this upload from the list?"
                              }
                              busy={manifestBusy}
                              onConfirm={() =>
                                void (async () => {
                                  try {
                                    await manifests.discardManifest(m.id);
                                  } catch {
                                    /* surfaced by the list not changing; refresh below */
                                  }
                                  loadManifests();
                                })()
                              }
                            />
                          </>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>
      ) : null}

      {groups.map((group) => {
        // A NAMED type group collapses; the unnamed single group is the whole page and has
        // nothing to collapse to. Open by default HERE — this is the page you came to in
        // order to read them — where the job page's copy of this list ships closed.
        // aria-label stays the BARE category name: getByLabelText("HARDWARE") is pinned.
        const owed = group.lines.filter((l) => {
          const o = lineOwed(l);
          return o.outstanding !== null && !o.settled;
        }).length;
        const inner = (
          <>
          {group.lines.map((line) => {
            const pill = rollupPill(line);
            const loads = shipmentsByLine.get(line.id) ?? [];
            const history = eventsByLine.get(line.id) ?? [];
            const corrEvent = corrFor !== null ? history.find((ev) => ev.id === corrFor) ?? null : null;
            const justVoided =
              afterVoid?.lineId === line.id ? history.find((ev) => ev.id === afterVoid.eventId) ?? null : null;
            const draft = marks[line.id] ?? emptyMark();
            const setDraft = (patch: Partial<MarkDraft>) =>
              setMarks((m) => ({ ...m, [line.id]: { ...draft, ...patch } }));
            return (
              <div key={line.id} className="dash-card">
                <div className="dash-card__head">
                  <span className={pill.className}>{pill.label}</span>{" "}
                  <strong>{rowTitle(line)}</strong>
                  {(() => {
                    // Deliberately NOT a .dash-pill: the first .dash-pill--ok / --danger on
                    // a line is pinned by test to be the status pill's own copy.
                    const label = owedLabel(line);
                    if (label === null) return null;
                    return (
                      <>
                        {" "}
                        <span className={`mat-owed${lineOwed(line).settled ? " mat-owed--settled" : ""}`}>
                          {label}
                        </span>
                      </>
                    );
                  })()}
                  {line.status === "incident" && (
                    <>
                      {" "}
                      <span className="dash-pill dash-pill--danger">Problem reported</span>
                      {canMark && (
                        <>
                          {" "}
                          <button
                            type="button"
                            className="btn btn--secondary btn--sm"
                            disabled={busy}
                            onClick={() => {
                              setResolveFor((cur) => (cur === line.id ? null : line.id));
                              setResolveNote("");
                              setResolveError(null);
                            }}
                          >
                            {resolveFor === line.id ? "Cancel" : "Resolve problem"}
                          </button>
                        </>
                      )}
                    </>
                  )}
                </div>
                {resolveFor === line.id && line.status === "incident" && (
                  <div className="dash-row" aria-label={`Resolve problem for ${rowTitle(line)}`}>
                    <input
                      type="text"
                      aria-label="How was it resolved"
                      placeholder="How was it resolved? (required)"
                      value={resolveNote}
                      aria-describedby={resolveError ? `resolve-error-${line.id}` : undefined}
                      onChange={(e) => setResolveNote(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn btn--primary btn--sm"
                      disabled={busy || resolveNote.trim().length === 0}
                      onClick={() =>
                        void run(
                          line.id,
                          () => api.resolveExpectedMaterialIncident(line.id, resolveNote.trim()),
                          "Problem resolved — the line now shows what the delivery ledger says.",
                          setResolveError,
                        ).then((ok) => {
                          // Only on success. This used to clear the note and close the panel on a
                          // refusal too, so the explanation the person had written vanished along
                          // with the place the error message belonged.
                          if (!ok) return;
                          setResolveFor(null);
                          setResolveNote("");
                          setResolveError(null);
                        })
                      }
                    >
                      Mark resolved
                    </button>
                    {resolveError && (
                      <p className="mat-mark-error" id={`resolve-error-${line.id}`} role="alert">
                        {resolveError}
                      </p>
                    )}
                  </div>
                )}
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
                    {/* Quantity is required for Delivered / Partially delivered — see `onMark`.
                        The asterisk marks it required at a glance; the red prompt below the row
                        says so in words the moment someone tries to record without it, and
                        aria-invalid draws the red border (schedule-report.css keys it on the
                        attribute, so the border and what a screen reader hears cannot differ). */}
                    <input
                      ref={(el) => {
                        qtyInputs.current[line.id] = el;
                      }}
                      aria-label={`Quantity received for ${rowTitle(line)}`}
                      aria-invalid={qtyError[line.id] ? true : undefined}
                      aria-describedby={qtyError[line.id] ? `mark-qty-error-${line.id}` : undefined}
                      value={draft.qty}
                      onChange={(e) => {
                        setDraft({ qty: e.target.value });
                        clearQtyError(line.id);
                      }}
                      placeholder="Qty *"
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
                    {/* Report a problem MOVED HERE from the daily form (2026-08-11, with the
                        deep-link-card decision — the daily form keeps no per-line actions).
                        Single-tap on purpose, unlike the marks: the flag is corrected by the
                        explicit Resolve action, not append-only, and the note prompt is
                        already its own deliberate step. Only a still-expected line offers
                        it — a received line's problem is a resolve-then-reflag flow. */}
                    {line.status === "expected" ? (
                      <>
                        {" "}
                        <button
                          type="button"
                          className="btn btn--secondary"
                          disabled={busy}
                          aria-label={`Report a problem with ${rowTitle(line)}`}
                          onClick={() => {
                            const note = window.prompt(
                              `Report a problem with "${rowTitle(line)}" — what's wrong? (required)`,
                            );
                            if (note === null) return;
                            if (!note.trim()) {
                              setMsg({ ok: false, text: "A short note describing the problem is required." });
                              return;
                            }
                            void run(
                              line.id,
                              () => api.flagExpectedMaterialIncident(line.id, note.trim()),
                              "Problem reported — the line shows the flag until someone resolves it.",
                            );
                          }}
                        >
                          Report a problem
                        </button>
                      </>
                    ) : null}
                    {settledAsk?.lineId === line.id && (
                      <SettledAsk
                        line={line}
                        busy={busy}
                        onRecord={() => onSettledRecord(line)}
                        onCancel={() => setSettledAsk(null)}
                      />
                    )}
                    {/* role="alert" so a screen reader announces it the moment the tap is refused;
                        the input's aria-describedby points here regardless of DOM order. */}
                    {qtyError[line.id] && (
                      <p className="mat-mark-error" id={`mark-qty-error-${line.id}`} role="alert">
                        {qtyError[line.id]}
                      </p>
                    )}
                  </div>
                )}

                {historyBlock(line, false)}

                {canMark && justVoided !== null && corrFor === null && (
                  <div
                    className="dash-row mat-void-done"
                    role="group"
                    aria-label={`After voiding ${eventRowSummary(line, justVoided)} for ${rowTitle(line)}`}
                  >
                    <p className="dash-card__sub mat-void-done__copy">
                      Voided {eventRowSummary(line, justVoided)}. If it was a duplicate, that is all.
                      If the mark itself was wrong, record the correct entry — now, or later from the
                      voided row.
                    </p>
                    <button
                      type="button"
                      className="btn btn--primary btn--sm"
                      onClick={() => setAfterVoid(null)}
                    >
                      Done — it was a duplicate
                    </button>{" "}
                    <button
                      type="button"
                      className="btn btn--secondary btn--sm"
                      disabled={busy}
                      onClick={() => openCorrection(line, justVoided)}
                    >
                      Record the correct entry
                    </button>
                  </div>
                )}

                {canMark && corrEvent !== null && (
                  <div
                    className="dash-row mat-correct"
                    role="group"
                    aria-label={`Record the correct entry for ${rowTitle(line)}`}
                  >
                    <p className="dash-card__sub mat-correct__copy">
                      Replacing the voided entry {eventSummary(line, corrEvent)}. Only needed if that
                      mark was wrong — a duplicate needs no correction.
                    </p>
                    {/* VISIBLE labels, each wrapping its control: this form opens PREFILLED, so a
                        placeholder would never be seen, and a row of bare values ("Delivered",
                        "40", a date) does not say which box is which. The aria-labels stay the
                        accessible names and each contains its visible word (label-in-name). */}
                    <label className="mat-correct__field">
                      <span className="mat-correct__label">Kind</span>
                      <select
                        aria-label="Correct entry kind"
                        value={corrDraft.kind}
                        onChange={(e) => {
                          setCorrDraft({ ...corrDraft, kind: e.target.value as api.MaterialReceiptKind });
                          setCorrAsk(false);
                        }}
                      >
                        <option value="delivered">Delivered</option>
                        <option value="partial">Partially delivered</option>
                        <option value="not_delivered">Not delivered</option>
                      </select>
                    </label>{" "}
                    <label className="mat-correct__field">
                      <span className="mat-correct__label">Quantity</span>
                      <input
                        aria-label="Correct entry quantity"
                        aria-invalid={corrError?.invalid ? true : undefined}
                        aria-describedby={corrError ? `correct-error-${line.id}` : undefined}
                        value={corrDraft.qty}
                        onChange={(e) => {
                          setCorrDraft({ ...corrDraft, qty: e.target.value });
                          setCorrError(null);
                        }}
                        placeholder="Qty *"
                        inputMode="decimal"
                        size={6}
                      />
                    </label>{" "}
                    <label className="mat-correct__field">
                      <span className="mat-correct__label">Note</span>
                      <input
                        aria-label="Correct entry note"
                        value={corrDraft.note}
                        onChange={(e) => setCorrDraft({ ...corrDraft, note: e.target.value })}
                        placeholder="Note"
                        maxLength={500}
                      />
                    </label>{" "}
                    <label className="mat-correct__field">
                      <span className="mat-correct__label">Date</span>
                      <input
                        aria-label="Correct entry date"
                        type="date"
                        value={corrDraft.date}
                        onChange={(e) => setCorrDraft({ ...corrDraft, date: e.target.value })}
                      />
                    </label>{" "}
                    {loads.length > 0 && (
                      <label className="mat-correct__field">
                        <span className="mat-correct__label">Load</span>
                        <select
                          aria-label="Correct entry load"
                          value={corrDraft.shipmentId}
                          onChange={(e) => setCorrDraft({ ...corrDraft, shipmentId: e.target.value })}
                        >
                          <option value="">— against which load? —</option>
                          {loads.map((s) => (
                            <option key={s.id} value={s.id}>
                              {s.bol_number ?? `load ${s.id}`}
                            </option>
                          ))}
                        </select>
                      </label>
                    )}{" "}
                    <button
                      type="button"
                      className="btn btn--primary btn--sm"
                      disabled={busy}
                      onClick={() => onRecordCorrection(line, false)}
                    >
                      Record correct entry
                    </button>{" "}
                    <button
                      type="button"
                      className="btn btn--secondary btn--sm"
                      onClick={() => {
                        setCorrFor(null);
                        setCorrAsk(false);
                        setCorrError(null);
                      }}
                    >
                      Not now
                    </button>
                    {corrAsk && (
                      <SettledAsk
                        line={line}
                        busy={busy}
                        action="Record the correction"
                        onRecord={() => onRecordCorrection(line, true)}
                        onCancel={() => setCorrAsk(false)}
                      />
                    )}
                    {corrError && (
                      <p className="mat-mark-error" id={`correct-error-${line.id}`} role="alert">
                        {corrError.text}
                      </p>
                    )}
                  </div>
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
                          onConfirm={() => onRemoveLine(line)}
                        />
                      </>
                    )}
                    {/* D11: a line whose deliveries still count is refused (409
                        line_has_deliveries) — said here, at the line, in words that name the way
                        out (void them first). */}
                    {removeError?.lineId === line.id && (
                      <p className="mat-mark-error" role="alert">
                        {removeError.text}
                      </p>
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
          </>
        );
        if (group.name === null) {
          return (
            <section key="__all" className="card dash-section" aria-label="Materials">
              {inner}
            </section>
          );
        }
        return (
          <details key={group.name} className="sched-drawer" aria-label={group.name} open>
            <summary>
              {group.name}
              <span className="sched-drawer__note">
                {group.lines.length} {group.lines.length === 1 ? "line" : "lines"}
                {owed > 0 ? ` · ${owed} still owed` : ""}
              </span>
            </summary>
            <div className="sched-drawer__body">{inner}</div>
          </details>
        );
      })}

      {/* ── Removed from the list (D11, operator decision 2026-10-21) ──────────────────────────
          A line can no longer be removed while its deliveries count — but lines removed BEFORE
          that rule still carry marks, and those marks still count toward the Receipts mirror and
          the client's weekly report. Without this group they could be neither seen nor voided
          from anywhere. VOID-ONLY by design: no mark buttons, no correction, no editing — the
          Worker records nothing new on a removed line, and the affordances say so by absence.
          Not in the summary above: that counts what this job is still expecting. */}
      {data !== null && data.removed.length > 0 && (
        <section className="card dash-section" aria-label="Removed from the list">
          <h3 className="dash-detail__h2">Removed from the list</h3>
          {data.removed.map((line) => {
            const pill = rollupPill(line);
            return (
              <div key={line.id} className="dash-card">
                {/* Title first: the head is a space-between row, and with no owed chip on the
                    right a leading pill would push the title to the far edge of the card. */}
                <div className="dash-card__head">
                  <strong>{rowTitle(line)}</strong>{" "}
                  <span className={pill.className}>{pill.label}</span>
                </div>
                <p className="dash-card__sub">
                  Removed from this job&apos;s list. Deliveries recorded against it still count until voided.
                </p>
                <div className="dash-card__sub">
                  {line.part_number ? `Part ${line.part_number} · ` : ""}
                  Expected {fmtQty(line.qty)} {line.unit ?? ""}
                  {line.qty_received_total != null ? ` · received ${fmtQty(line.qty_received_total)}` : ""}
                </div>
                {historyBlock(line, true)}
              </div>
            );
          })}
        </section>
      )}

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
