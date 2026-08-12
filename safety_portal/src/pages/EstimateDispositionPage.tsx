import { useEffect, useMemo, useState } from "react";
import * as est from "../lib/estimates";
import {
  createDraft,
  deletePoDraft,
  fetchJobShipTo,
  fetchVendors,
  formatCents,
  parseDollarsToCents,
  type DraftBody,
  type DraftLine,
  type Vendor,
} from "../lib/po";
import { fetchRfq, type RfqDetail } from "../lib/rfq";

// Vendor-estimate DISPOSITION screen (ADR-0004 E3) — the human fidelity control and the
// first-class replacement for the estimator's highlight color-coding. Left: the rendered
// source-page previews (never the original untrusted bytes). Right: the ADVISORY
// extraction lines (accept/reject per line) + manual Tier-3 entry rows, vendor + job
// confirmation, and the totals bar.
//
// THE HARD RULE (ADR decision 3 / red-team #2): the automated gates verify internal
// CONSISTENCY, not fidelity to the source — the ONLY fidelity control is the human
// side-by-side. So when the estimate carries extraction lines, "Create draft PO" stays
// DISABLED until at least one preview page has actually LOADED (tracked via img onLoad),
// with an explicit "No preview available — I verified against the original document"
// checkbox as the only override. Manual-entry docs (no extraction) skip the gate but
// still show previews when present.
//
// Every dollar re-enters the trusted path ONLY through the EXISTING createDraft route
// (ADR decision 2) — cents-normalized lines, server-recomputed money, estimate_id as
// store-only provenance. dispose 409 already_disposed on the import path = "someone
// already imported this": the just-created duplicate draft is DISCARDED and we leave.

interface ManualLine {
  description: string;
  qty: string;
  unit: string;
  unitCost: string; // dollars text — parsed to cents at submit (string math, never floats)
}
const EMPTY_MANUAL: ManualLine = { description: "", qty: "", unit: "", unitCost: "" };

const JOB_NO_RE = /^\d{4}\.\d{3}$/;
const STATE_RE = /^[A-Z]{2}$/;
const QTY_RE = /^\d+(\.\d{0,3})?$/;

/** Resolve an accepted extraction line's unit cost in cents, or null when it cannot be
 *  imported without manual re-entry (e.g. a lump-sum line with neither unit cost nor a
 *  qty-1 extended). Honest fallbacks only — never invent a price. */
function lineUnitCostCents(l: est.ExtractionLine): number | null {
  if (l.unit_cost_cents !== null) return l.unit_cost_cents;
  if (l.extended_cents !== null && (l.qty ?? 1) === 1) return l.extended_cents;
  return null;
}

export function EstimateDispositionPage({
  estimateId,
  onClose,
}: {
  estimateId: number;
  /** Leave the screen; an optional notice rides back to the tracker's banner. A successful
   *  import ALSO carries the minted draft-PO id structurally (not just in copy), so the
   *  Purchase-Orders hub can flip to the Orders tab and open the draft for editing. */
  onClose: (notice?: { ok: boolean; text: string }, importedPoId?: number) => void;
}) {
  const [detail, setDetail] = useState<est.EstimateDetail | null>(null);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // ── Preview state (the red-team-2 gate's evidence) ────────────────────────────────────────────
  const [page, setPage] = useState(1);
  const [loadedPages, setLoadedPages] = useState<Set<number>>(new Set());
  const [failedPages, setFailedPages] = useState<Set<number>>(new Set());
  const [noPreviewVerified, setNoPreviewVerified] = useState(false);

  // ── Review state ──────────────────────────────────────────────────────────────────────────────
  const [accepted, setAccepted] = useState<Set<number>>(new Set()); // extraction line ids
  const [manualLines, setManualLines] = useState<ManualLine[]>([{ ...EMPTY_MANUAL }]);
  const [vendorKey, setVendorKey] = useState("");
  const [jobNo, setJobNo] = useState("");
  const [sitePhase, setSitePhase] = useState("0");
  const [shipToState, setShipToState] = useState("");
  const [armedReject, setArmedReject] = useState(false);

  // R4 round-trip context: when the estimate auto-bound to an RFQ (a verified Tier-0 form
  // token round-tripped), fetch that RFQ so we can show the requested-vs-quoted compare.
  const [rfqDetail, setRfqDetail] = useState<RfqDetail | null>(null);

  useEffect(() => {
    est
      .fetchEstimate(estimateId)
      .then((d) => {
        setDetail(d);
        setJobNo(d.estimate.job_no);
        setVendorKey(d.estimate.vendor_key ?? "");
        // 0064/0069 — seed Site/phase from the JOB, not from 0. job_no alone cannot resolve
        // it (2026.384 is both MH405 site 1 and OG593 site 2), which is why the estimate now
        // carries job_id. Without this every estimate-imported PO for a multi-site project
        // composes 2026.384.0.x instead of 2026.384.1.x — a contractual number for a site
        // that does not exist.
        //
        // Best-effort and non-blocking, matching the PO builder: a 404 or a read failure
        // leaves the operator-editable input alone rather than gating the disposition. The
        // ship-to block rides along because the same call already returns it and the screen
        // otherwise makes the reviewer hand-type the state.
        if (d.estimate.job_id) {
          fetchJobShipTo(d.estimate.job_id)
            .then((sh) => {
              setSitePhase(String(sh.site_phase ?? 0));
              if (sh.ship_to_state) setShipToState(sh.ship_to_state.toUpperCase());
            })
            .catch(() => {
              /* convenience only — the Site/phase and state inputs stay operator-editable */
            });
        }
        // Pre-accept every extracted line — the reviewer UNCHECKS what the source
        // contradicts (mirrors the green-by-default read of a clean quote, but every
        // line stays individually confirmable against the preview).
        setAccepted(new Set(d.lines.map((l) => l.id)));
        // If auto-bound to an RFQ, load its detail (best-effort — the banner + panel are
        // context only; a fetch failure never blocks the disposition).
        if (d.estimate.rfq_id !== null) {
          fetchRfq(d.estimate.rfq_id).then(setRfqDetail).catch(() => setRfqDetail(null));
        }
      })
      .catch(() => setError("Failed to load the estimate."));
    fetchVendors().then(setVendors).catch(() => setVendors([]));
  }, [estimateId]);

  const lines = detail?.lines ?? [];
  const hasExtractionLines = lines.length > 0;
  const previewCount = detail?.preview_count ?? 0;

  // THE gate: with extraction lines present, import is blocked until a preview page has
  // actually rendered — or the explicit no-preview acknowledgment when none exists/loads.
  const previewEvidence = loadedPages.size > 0 || noPreviewVerified;
  const gateSatisfied = !hasExtractionLines || previewEvidence;

  // ── Build the draft lines (accepted extraction lines + filled manual rows) ────────────────────
  const acceptedLines = useMemo(() => lines.filter((l) => accepted.has(l.id)), [lines, accepted]);
  const unimportable = useMemo(
    () => acceptedLines.filter((l) => lineUnitCostCents(l) === null),
    [acceptedLines],
  );

  interface BuiltManual { line: DraftLine; cents: number }
  const builtManual: BuiltManual[] | string = useMemo(() => {
    const out: BuiltManual[] = [];
    for (const [i, m] of manualLines.entries()) {
      const blank = !m.description.trim() && !m.qty.trim() && !m.unitCost.trim();
      if (blank) continue;
      if (!m.description.trim()) return `Manual line ${i + 1} needs a description.`;
      if (!QTY_RE.test(m.qty.trim())) return `Manual line ${i + 1} needs a quantity (up to 3 decimals).`;
      const cents = parseDollarsToCents(m.unitCost);
      if (cents === null) return `Manual line ${i + 1} needs a unit cost in dollars.`;
      const qty = parseFloat(m.qty.trim());
      out.push({
        line: {
          description: m.description.trim().slice(0, 512),
          qty,
          unit: m.unit.trim().slice(0, 32) || undefined,
          unit_cost_cents: cents,
        },
        cents: Math.round(qty * cents),
      });
    }
    return out;
  }, [manualLines]);

  const manualProblem = typeof builtManual === "string" ? builtManual : null;
  const manualBuilt = typeof builtManual === "string" ? [] : builtManual;

  const totalCents = useMemo(() => {
    let t = 0;
    for (const l of acceptedLines) {
      const cost = lineUnitCostCents(l);
      if (cost !== null) t += l.extended_cents ?? Math.round((l.qty ?? 1) * cost);
    }
    for (const m of manualBuilt) t += m.cents;
    return t;
  }, [acceptedLines, manualBuilt]);

  const draftLineCount = acceptedLines.length - unimportable.length + manualBuilt.length;
  const formProblem =
    !JOB_NO_RE.test(jobNo.trim())
      ? "Enter the job number as YYYY.NNN."
      : !vendorKey
        ? "Confirm the vendor."
        : !STATE_RE.test(shipToState.trim().toUpperCase())
          ? "Enter the 2-letter ship-to state (drives tax)."
          : unimportable.length > 0
            ? `${unimportable.length} accepted line(s) have no resolvable unit cost — uncheck them and re-enter manually.`
            : manualProblem ??
              (draftLineCount === 0 ? "Accept at least one line or enter one manually." : null);

  const canImport = gateSatisfied && formProblem === null && detail !== null && !busy;

  // ── Actions ───────────────────────────────────────────────────────────────────────────────────
  async function onImport() {
    if (!canImport || detail === null) return;
    setBusy(true);
    setMsg(null);
    const body: DraftBody = {
      vendor_key: vendorKey,
      job_no: jobNo.trim(),
      site_phase: Number.isSafeInteger(parseInt(sitePhase, 10)) ? parseInt(sitePhase, 10) : 0,
      job_name: detail.estimate.job_name ?? undefined,
      ship_to_state: shipToState.trim().toUpperCase(),
      tax_mode: "auto",
      estimate_id: estimateId,
      line_items: [
        ...acceptedLines
          .filter((l) => lineUnitCostCents(l) !== null)
          .map<DraftLine>((l) => ({
            part_number: l.part_number ?? undefined,
            description: l.description,
            qty: l.qty ?? 1,
            unit: l.unit ?? undefined,
            unit_cost_cents: lineUnitCostCents(l),
          })),
        ...manualBuilt.map((m) => m.line),
      ],
    };
    let poId: number;
    try {
      const created = await createDraft(body);
      poId = created.id;
    } catch (err) {
      const code = err && typeof err === "object" && "code" in err ? (err as { code: string | null }).code : null;
      if (code === "estimate_already_imported") {
        setBusy(false);
        onClose({ ok: false, text: "This estimate was already imported into a draft PO." });
        return;
      }
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Draft creation failed." });
      setBusy(false);
      return;
    }
    try {
      const disposed = await est.disposeEstimate(estimateId, {
        action: "imported",
        po_id: poId,
        line_dispositions: lines.map((l) => ({
          line_id: l.id,
          disposition: accepted.has(l.id) ? ("accepted" as const) : ("rejected" as const),
        })),
        // The checkbox state rides to the Worker so the server-side fidelity gate can
        // record which evidence path (rendered preview vs acknowledgment) authorized
        // this import.
        no_preview_verified: noPreviewVerified,
      });
      if (!disposed.ok) {
        // already_disposed — someone imported it in parallel: DISCARD the duplicate
        // draft we just minted (best-effort; a draft is hard-deletable) and leave.
        try {
          await deletePoDraft(poId);
        } catch {
          // The duplicate draft survives as a visible draft in the PO tracker — never silent.
        }
        setBusy(false);
        onClose({ ok: false, text: "This estimate was already imported — the duplicate draft was discarded." });
        return;
      }
      setBusy(false);
      // The hub consumes the structural poId: flips to the Orders tab and opens the draft
      // for editing. The notice is the fallback banner if no hand-off is wired.
      onClose({ ok: true, text: `Imported into draft PO #${poId} — finish it on the Purchase Orders tab.` }, poId);
    } catch (err) {
      setMsg({
        ok: false,
        text:
          (err instanceof Error ? err.message : "Disposition failed.") +
          ` The draft PO #${poId} exists — retry the disposition or discard the draft in Purchase Orders.`,
      });
      setBusy(false);
    }
  }

  async function onReject() {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const disposed = await est.disposeEstimate(estimateId, { action: "rejected" });
      setBusy(false);
      onClose(
        disposed.ok
          ? { ok: true, text: "Estimate rejected." }
          : { ok: false, text: "This estimate was already disposed." },
      );
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Reject failed." });
      setBusy(false);
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────────────────────────
  if (error) {
    return (
      <>
        <div className="banner banner--err">{error}</div>
        <button className="btn btn--secondary" onClick={() => onClose()}>
          ← Back to estimates
        </button>
      </>
    );
  }
  if (detail === null) {
    return <p className="muted">Loading…</p>;
  }

  const e = detail.estimate;
  const reviewable = e.status === "extracted" || e.status === "needs_review";
  const extraction = detail.extraction;

  const previewPane = (
    <section className="card dash-section" aria-label="Source document preview">
      <h3 className="jha__section-title">Source document</h3>
      {previewCount > 0 ? (
        <>
          <div className="dash-row">
            <button
              className="btn btn--secondary"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              ‹ Prev
            </button>
            <span className="dash-pill">
              Page {page} / {previewCount}
            </span>
            <button
              className="btn btn--secondary"
              disabled={page >= previewCount}
              onClick={() => setPage((p) => Math.min(previewCount, p + 1))}
            >
              Next ›
            </button>
          </div>
          <img
            key={page}
            src={est.estimatePreviewUrl(estimateId, page)}
            alt={`Estimate page ${page}`}
            style={{ width: "100%", height: "auto", border: "1px solid #ccc" }}
            onLoad={() => setLoadedPages((s) => new Set(s).add(page))}
            onError={() => setFailedPages((s) => new Set(s).add(page))}
          />
          {failedPages.has(page) ? (
            <p className="banner banner--err">This page preview failed to load.</p>
          ) : null}
        </>
      ) : (
        <p className="muted">No preview was rendered for this document.</p>
      )}
      {hasExtractionLines && loadedPages.size === 0 ? (
        <label className="field">
          <span className="field__label">
            <input
              type="checkbox"
              checked={noPreviewVerified}
              onChange={(ev) => setNoPreviewVerified(ev.target.checked)}
            />{" "}
            No preview available — I verified against the original document
          </span>
        </label>
      ) : null}
    </section>
  );

  return (
    <>
      <div className="dash-row">
        <button className="btn btn--secondary" onClick={() => onClose()}>
          ← Back to estimates
        </button>
        <span className="dash-pill">{est.ESTIMATE_STATUS_LABEL[e.status] ?? e.status}</span>
      </div>
      {/* h3: the hub's "Purchase Orders" h2 is the page heading — sub-faces nest under it. */}
      <h3 className="page__heading">{e.filename}</h3>
      <p className="dash__intro">
        {e.job_no} · {e.job_name || "—"} · uploaded by {e.uploaded_by}
        {e.doc_type ? ` · classified ${e.doc_type}` : ""}
        {extraction ? ` · extraction tier ${extraction.tier}` : " · manual entry (no extraction)"}
      </p>

      {/* ── R4 auto-bind banner + requested-vs-quoted compare (red-team #10: the human still
             CONFIRMS the vendor; the token asserted identity, not truth) ── */}
      {e.rfq_id !== null ? (
        <div className="banner banner--ok" role="status">
          <strong>Auto-bound to RFQ {rfqDetail?.rfq.rfq_number ?? `#${e.rfq_id}`}
            {e.rfq_vendor_key ? ` / ${e.rfq_vendor_key}` : ""}</strong> via the fillable-form
          token — <em>confirm the vendor below</em> before importing. The form token asserts
          the (RFQ, vendor) identity; every quoted price is still untrusted until you verify it
          against the source.
          {rfqDetail ? (
            <details className="rfq-compare">
              <summary>Requested vs. quoted lines</summary>
              <div className="table-scroll">
                <table className="dash-table">
                  <thead>
                    <tr><th>#</th><th>Requested (RFQ)</th><th>Qty</th><th>Quoted (this estimate)</th><th>Unit price</th></tr>
                  </thead>
                  <tbody>
                    {rfqDetail.line_items.map((req, i) => {
                      const quoted = lines[i];
                      return (
                        <tr key={req.position}>
                          <td>{req.position}</td>
                          <td>{req.description}{req.part_number ? ` (${req.part_number})` : ""}</td>
                          <td>{req.qty ?? "—"}{req.unit ? ` ${req.unit}` : ""}</td>
                          <td>{quoted ? quoted.description : "—"}</td>
                          <td>{quoted && quoted.unit_cost_cents !== null ? formatCents(quoted.unit_cost_cents) : "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </details>
          ) : null}
        </div>
      ) : null}

      {msg && <div className={`banner ${msg.ok ? "banner--ok" : "banner--err"}`}>{msg.text}</div>}

      {!reviewable ? (
        <>
          <div className="banner banner--err">
            This estimate is {est.ESTIMATE_STATUS_LABEL[e.status]?.toLowerCase() ?? e.status} and can no
            longer be dispositioned{e.po_id !== null ? ` (draft PO #${e.po_id})` : ""}.
          </div>
          {previewPane}
        </>
      ) : (
        <>
          {previewPane}

          {/* ── Extraction lines (advisory — the human accepts/rejects each) ── */}
          {hasExtractionLines ? (
            <section className="card dash-section" aria-label="Extracted lines">
              <h3 className="jha__section-title">
                Extracted lines <span className="dash-pill dash-pill--warn">advisory — verify against the source</span>
              </h3>
              {extraction && extraction.math_ok !== 1 ? (
                <p className="banner banner--err">
                  The extraction's arithmetic did NOT cross-check — verify every line against the source.
                </p>
              ) : null}
              <div style={{ overflowX: "auto" }}>
                <table className="dash-table">
                  <thead>
                    <tr>
                      <th>Use</th>
                      <th>#</th>
                      <th>Part</th>
                      <th>Description</th>
                      <th>Qty</th>
                      <th>Unit</th>
                      <th>Unit cost</th>
                      <th>Extended</th>
                    </tr>
                  </thead>
                  <tbody>
                    {lines.map((l) => {
                      const cost = lineUnitCostCents(l);
                      return (
                        <tr key={l.id}>
                          <td>
                            <input
                              type="checkbox"
                              aria-label={`Accept line ${l.position}`}
                              checked={accepted.has(l.id)}
                              onChange={(ev) => {
                                setAccepted((s) => {
                                  const next = new Set(s);
                                  if (ev.target.checked) next.add(l.id);
                                  else next.delete(l.id);
                                  return next;
                                });
                              }}
                            />
                          </td>
                          <td>{l.position}</td>
                          <td>{l.part_number ?? ""}</td>
                          <td>
                            {l.section ? <span className="muted">{l.section} · </span> : null}
                            {l.description}
                            {l.line_note ? <span className="muted"> — {l.line_note}</span> : null}
                          </td>
                          <td>{l.qty ?? ""}</td>
                          <td>{l.unit ?? ""}</td>
                          <td>{cost !== null ? formatCents(cost) : "—"}</td>
                          <td>{l.extended_cents !== null ? formatCents(l.extended_cents) : "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {unimportable.length > 0 ? (
                <p className="banner banner--err">
                  {unimportable.length} accepted line(s) carry no resolvable unit cost — uncheck them and
                  re-enter them manually below.
                </p>
              ) : null}
            </section>
          ) : null}

          {/* ── Manual (Tier-3) line entry ── */}
          <section className="card dash-section" aria-label="Manual lines">
            <h3 className="jha__section-title">{hasExtractionLines ? "Additional manual lines" : "Manual line entry"}</h3>
            <div style={{ overflowX: "auto" }}>
              <table className="dash-table">
                <thead>
                  <tr>
                    <th>Description</th>
                    <th>Qty</th>
                    <th>Unit</th>
                    <th>Unit cost ($)</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {manualLines.map((m, i) => {
                    const cell = (
                      field: keyof ManualLine,
                      label: string,
                      width?: number,
                    ) => (
                      <input
                        className="field__input"
                        style={width ? { width } : undefined}
                        aria-label={`Manual line ${i + 1} ${label}`}
                        value={m[field]}
                        onChange={(ev) =>
                          setManualLines((ls) =>
                            ls.map((x, xi) => (xi === i ? { ...x, [field]: ev.target.value } : x)),
                          )
                        }
                      />
                    );
                    return (
                      <tr key={i}>
                        <td>{cell("description", "description")}</td>
                        <td>{cell("qty", "quantity", 80)}</td>
                        <td>{cell("unit", "unit", 70)}</td>
                        <td>{cell("unitCost", "unit cost", 110)}</td>
                        <td>
                          <button
                            type="button"
                            className="btn btn--secondary"
                            aria-label={`Remove manual line ${i + 1}`}
                            disabled={manualLines.length === 1}
                            onClick={() => setManualLines((ls) => ls.filter((_, xi) => xi !== i))}
                          >
                            ✕
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <button
              type="button"
              className="btn btn--secondary"
              onClick={() => setManualLines((ls) => [...ls, { ...EMPTY_MANUAL }])}
            >
              + Add line
            </button>
          </section>

          {/* ── Confirmation + totals + actions ── */}
          <section className="card dash-section" aria-label="Confirm and import">
            <h3 className="jha__section-title">Confirm &amp; import</h3>
            {extraction?.vendor_name ? (
              <p className="muted">
                The document names <strong>{extraction.vendor_name}</strong>
                {extraction.quote_number ? ` (quote ${extraction.quote_number})` : ""} — confirm the
                vendor from the directory below (documents never write the vendor list).
              </p>
            ) : null}
            <div className="jha__grid">
              <label className="field">
                <span className="field__label">Vendor</span>
                <select
                  className="field__input"
                  aria-label="Vendor"
                  value={vendorKey}
                  onChange={(ev) => setVendorKey(ev.target.value)}
                >
                  <option value="">— confirm vendor —</option>
                  {vendors.map((v) => (
                    <option key={v.vendor_key} value={v.vendor_key}>
                      {v.vendor_name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field__label">Job number (YYYY.NNN)</span>
                <input className="field__input" value={jobNo} maxLength={8} onChange={(ev) => setJobNo(ev.target.value)} />
              </label>
              <label className="field">
                <span className="field__label">Site / phase</span>
                <input
                  className="field__input"
                  value={sitePhase}
                  maxLength={4}
                  onChange={(ev) => setSitePhase(ev.target.value)}
                />
              </label>
              <label className="field">
                <span className="field__label">Ship-to state (2 letters — drives tax)</span>
                <input
                  className="field__input"
                  value={shipToState}
                  maxLength={2}
                  onChange={(ev) => setShipToState(ev.target.value.toUpperCase())}
                />
              </label>
            </div>

            <div className="dash-card__row">
              <span className="dash-card__label">Draft subtotal ({draftLineCount} line{draftLineCount === 1 ? "" : "s"})</span>
              <strong>{formatCents(totalCents)}</strong>
              {extraction?.grand_total_cents != null ? (
                <span className="muted"> · document total {formatCents(extraction.grand_total_cents)}</span>
              ) : null}
            </div>

            {formProblem ? <p className="muted">{formProblem}</p> : null}
            {!gateSatisfied ? (
              <p className="banner banner--err">
                Load a source preview page (or check the no-preview acknowledgment) before importing —
                extracted numbers are advisory until verified against the document.
              </p>
            ) : null}

            <div className="jha__actions">
              <button className="btn btn--primary" disabled={!canImport} onClick={() => void onImport()}>
                {busy ? "Working…" : "Create draft PO"}
              </button>
              {armedReject ? (
                <button className="btn btn--retire" disabled={busy} onClick={() => void onReject()}>
                  Confirm reject
                </button>
              ) : (
                <button className="btn btn--retire" disabled={busy} onClick={() => setArmedReject(true)}>
                  Reject estimate
                </button>
              )}
            </div>
          </section>
        </>
      )}
    </>
  );
}
