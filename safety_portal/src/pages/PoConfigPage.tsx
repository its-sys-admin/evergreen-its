import { useState, useEffect, useCallback, useRef } from "react";
import type { FormEvent } from "react";
import * as api from "../lib/po";
import * as sub from "../lib/subcontracts";
import { useAuth } from "../lib/auth";
import { PageShell } from "../components/PageShell";
import { TermsProfilesEditor } from "../components/TermsProfilesEditor";
import { ExhibitTemplatesEditor } from "../components/ExhibitTemplatesEditor";

// PO/SC Configuration (Administration) — the browser EDITOR for the config classes that print on every
// purchase order AND every subcontract. PO: the Purchaser identity (D5), the ship-to-state tax table
// (D8), the delivery-contact suggestion list (Feature C — the builder's <datalist>; free text always
// still accepted), and the terms-library profiles (D6/S3). Subcontracts: the Contractor identity (SC-S2) and the
// subcontract terms-library. It reads current values from the same cap-gated routes the builders use —
// GET /api/po/config + /api/po/terms (cap.po.manage) and GET /api/subcontracts/config + /terms
// (cap.subcontracts.manage) — and edits them through the §50 send-free queue.
//
// THE §50 BOUNDARY (made visible in the UI): these values live in version-controlled config
// (po_materials|subcontracts/config/*.json) and sha256-PINNED terms files. Editing them is a privileged
// code-actuation (Operational Standards §50) with a legal-review gate on terms text. This editor NEVER
// writes those files directly — it POSTs a change to the cloud queue (POST /api/config/requests,
// send-free), the Mac config daemon is the sole actuator that validates → git-commits → auto-deploys.
// So the SPA can only QUEUE; the change goes live (or fails, never silently) through the status monitor
// below. A new terms version ships legal_review: pending and is NOT used until the operator clears it +
// points current_version at it — the editor mints the version; activation is a separate operator step.
//
// The read affordances are visibility for the office; the edit forms are wrapped in {canManage} (PO,
// cap.po.manage) / {canManageSub} (subcontracts, cap.subcontracts.manage). The Worker re-gates every
// write per-workstream (Invariant 2), so the SPA gating is convenience, never the boundary. (Subcontract
// Payment-terms editing is a fast-follow — the actuator needs the payment-cadence day fields the served
// /api/subcontracts/config does not yet expose.)
//
// VISUAL: the same URS-Marine dash look as the Materials Catalog / Vendors admin pages — `.card
// dash-section` blocks, gold-underlined `.jha__section-title` heads, `.dash-chip` chips, the shared
// `.field` edit-form idiom, and the `.form-editor__*` status-monitor stepper reused from PublishMonitor.

const WORKSTREAM = "po_materials";
const SUB_WORKSTREAM = "subcontracts";

/** Basis points → a fixed 2-decimal percent string (900 → "9.00%"). Integer-safe display. */
function bpToPct(bp: number): string {
  return `${(bp / 100).toFixed(2)}%`;
}

/** Basis points → a bare percent number string for the editable rate field (900 → "9.00"). */
function bpToPctInput(bp: number): string {
  return (bp / 100).toFixed(2);
}

function FieldInput({
  label,
  value,
  onChange,
  className,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  className?: string;
  placeholder?: string;
}) {
  return (
    <label className={`field${className ? ` ${className}` : ""}`}>
      <span className="field__label">{label}</span>
      <input
        className="field__input"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function FieldTextarea({
  label,
  value,
  onChange,
  rows = 4,
  maxLength = 8000,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  maxLength?: number;
  placeholder?: string;
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      <textarea
        className="field__textarea"
        aria-label={label}
        value={value}
        rows={rows}
        maxLength={maxLength}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

// Light client-side email shape — mirrors the actuator's _EMAIL_RE (config_apply.py) so the
// operator never queues an avoidably-failing §50 request over a malformed contact email.
const EMAIL_SHAPE_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

// ── Editor form buffers (flat all-strings, per the admin edit-form idiom) ──────────────────────────
type PurchaserForm = { entity: string; address_lines: string; phone: string; to: string; cc: string };
type TaxRow = { state: string; name: string; rate: string }; // rate entered as a PERCENT string
type DeliveryRow = { name: string; phone: string; email: string }; // Feature C — the builder's <datalist> suggestions
// Subcontract Contractor identity (SC-S2). The terms editors own their own buffers in TermsProfilesEditor.
type ContractorForm = { entity: string; address_lines: string; phone: string; signature_entity: string; prime_contractor_default: string };

export function PoConfigPage({ onBack }: { onBack: () => void }) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.po.manage"); // PO edit affordance — the Worker re-gates every write
  const canManageSub = caps.includes("cap.subcontracts.manage"); // subcontract edit affordance

  const [config, setConfig] = useState<api.PoConfig | null>(null);
  const [terms, setTerms] = useState<api.TermsProfile[]>([]);
  const [subConfig, setSubConfig] = useState<sub.SubcontractConfig | null>(null);
  const [subTerms, setSubTerms] = useState<sub.TermsProfile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Write feedback (shared across all editors) + a bump that re-polls the status monitor.
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [refreshSignal, setRefreshSignal] = useState(0);
  const bumpRefresh = () => setRefreshSignal((n) => n + 1);
  // Which config workstream tab is shown — Purchase Order or Subcontract (a switch, not a route).
  const [tab, setTab] = useState<"po" | "sub">("po");

  // Which JSON editor is open + its buffer (the terms editors own their own state in TermsProfilesEditor).
  const [purchaserOpen, setPurchaserOpen] = useState(false);
  const [pf, setPf] = useState<PurchaserForm>({ entity: "", address_lines: "", phone: "", to: "", cc: "" });
  const [taxOpen, setTaxOpen] = useState(false);
  const [taxRows, setTaxRows] = useState<TaxRow[]>([]);
  const [deliveryOpen, setDeliveryOpen] = useState(false);
  const [deliveryRows, setDeliveryRows] = useState<DeliveryRow[]>([]);
  const [contractorOpen, setContractorOpen] = useState(false);
  const [cf, setCf] = useState<ContractorForm>({ entity: "", address_lines: "", phone: "", signature_entity: "", prime_contractor_default: "" });

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfg, tp] = await Promise.all([api.fetchPoConfig(), api.fetchTerms()]);
      setConfig(cfg);
      setTerms(tp);
    } catch {
      setError("Could not load PO configuration. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
    // Subcontract config loads independently — a degraded /subcontracts route (or an operator without
    // cap.subcontracts.manage) must NOT blank the PO view; it just hides the subcontract group.
    void sub.fetchSubcontractConfig().then(setSubConfig).catch(() => setSubConfig(null));
    void sub.fetchTerms().then(setSubTerms).catch(() => setSubTerms([]));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Sorted tax rows — union of the rate table and the state-name map, so a state with a name but
  // no explicit rate (or vice-versa) still shows, never silently dropped.
  const taxStates = config
    ? Array.from(
        new Set([...Object.keys(config.tax.rates_bp), ...Object.keys(config.tax.state_names)]),
      ).sort()
    : [];

  // ── Editor open (seed the buffer from the current value) ─────────────────────────────────────────
  function openPurchaser() {
    if (!config) return;
    const p = config.purchaser;
    setPf({
      entity: p.entity,
      address_lines: p.address_lines.join("\n"),
      phone: p.phone,
      to: p.invoice_routing.to,
      cc: p.invoice_routing.cc.join("\n"),
    });
    setMsg(null);
    setPurchaserOpen(true);
  }

  function openTax() {
    if (!config) return;
    setTaxRows(
      taxStates.map((st) => ({
        state: st,
        name: config.tax.state_names[st] ?? "",
        rate: config.tax.rates_bp[st] == null ? "" : bpToPctInput(config.tax.rates_bp[st]),
      })),
    );
    setMsg(null);
    setTaxOpen(true);
  }

  // ── Delivery-contacts (Feature C) editor open (seed the buffer from the served list) ──────────────
  function openDelivery() {
    if (!config) return;
    setDeliveryRows(
      config.delivery_contacts.map((ct) => ({
        name: ct.name,
        phone: ct.phone ?? "",
        email: ct.email ?? "",
      })),
    );
    setMsg(null);
    setDeliveryOpen(true);
  }

  // ── Contractor identity (SC-S2) editor open (seed the buffer from the current subcontract config) ─
  function openContractor() {
    if (!subConfig) return;
    const c = subConfig.contractor;
    setCf({
      entity: c.entity,
      address_lines: c.address_lines.join("\n"),
      phone: c.phone,
      signature_entity: c.signature_entity,
      prime_contractor_default: c.prime_contractor_default,
    });
    setMsg(null);
    setContractorOpen(true);
  }

  // ── Editor submit (the 5-step envelope: guard → busy → await → reload+bump → catch → finally) ─────
  async function submitPurchaser(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const entity = pf.entity.trim();
    const to = pf.to.trim();
    if (!entity) {
      setMsg({ ok: false, text: "The purchaser entity is required." });
      return;
    }
    if (!to) {
      setMsg({ ok: false, text: "The invoice-routing To address is required." });
      return;
    }
    if (!pf.phone.trim()) {
      // The actuator (_apply_purchaser_edit) rejects an empty phone — guard client-side so the
      // operator never queues an avoidably-failing §50 request.
      setMsg({ ok: false, text: "The purchaser phone is required." });
      return;
    }
    const payload = {
      entity,
      address_lines: pf.address_lines.split("\n").map((s) => s.trim()).filter(Boolean),
      phone: pf.phone.trim(),
      invoice_routing: {
        to,
        cc: pf.cc.split("\n").map((s) => s.trim()).filter(Boolean),
      },
    };
    setBusy(true);
    setMsg(null);
    try {
      await api.submitConfigEdit({ workstream: WORKSTREAM, artifact_key: "purchaser", op: "edit", payload });
      setPurchaserOpen(false);
      setMsg({ ok: true, text: "Queued — the purchaser change will go live after review. Track it below." });
      setRefreshSignal((n) => n + 1);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Submit failed." });
    } finally {
      setBusy(false);
    }
  }

  async function submitTax(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const rates_bp: Record<string, number> = {};
    const state_names: Record<string, string> = {};
    for (const row of taxRows) {
      const st = row.state.trim().toUpperCase();
      if (!/^[A-Z]{2}$/.test(st)) {
        setMsg({ ok: false, text: `Each state must be a 2-letter code (got "${row.state}").` });
        return;
      }
      if (rates_bp[st] !== undefined) {
        setMsg({ ok: false, text: `${st} is listed twice — remove the duplicate.` });
        return;
      }
      const bp = api.pctToBp(row.rate);
      if (bp === null) {
        setMsg({ ok: false, text: `Enter the ${st} rate as a percent 0–100 with at most 2 decimals (e.g. 9.25).` });
        return;
      }
      rates_bp[st] = bp;
      if (row.name.trim()) state_names[st] = row.name.trim();
    }
    if (Object.keys(rates_bp).length === 0) {
      setMsg({ ok: false, text: "Add at least one state rate." });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.submitConfigEdit({
        workstream: WORKSTREAM,
        artifact_key: "tax",
        op: "edit",
        payload: { rates_bp, state_names },
      });
      setTaxOpen(false);
      setMsg({ ok: true, text: "Queued — the tax-table change will go live after review. Track it below." });
      setRefreshSignal((n) => n + 1);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Submit failed." });
    } finally {
      setBusy(false);
    }
  }

  async function submitDelivery(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    // Client-side guards mirror _apply_delivery_contacts_edit (name required, email shape,
    // unique names) so the operator never queues an avoidably-failing §50 request. An EMPTY
    // list is valid — that clears every suggestion.
    const contacts: { name: string; phone: string; email: string }[] = [];
    const seen = new Set<string>();
    for (const row of deliveryRows) {
      const name = row.name.trim();
      const phone = row.phone.trim();
      const email = row.email.trim();
      if (!name) {
        setMsg({ ok: false, text: "Every delivery contact needs a name (remove empty rows)." });
        return;
      }
      if (seen.has(name.toLowerCase())) {
        setMsg({ ok: false, text: `"${name}" is listed twice — contact names must be unique.` });
        return;
      }
      seen.add(name.toLowerCase());
      if (email && !EMAIL_SHAPE_RE.test(email)) {
        setMsg({ ok: false, text: `"${email}" doesn't look like an email address.` });
        return;
      }
      contacts.push({ name, phone, email });
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.submitConfigEdit({
        workstream: WORKSTREAM,
        artifact_key: "delivery_contacts",
        op: "edit",
        payload: { contacts },
      });
      setDeliveryOpen(false);
      setMsg({ ok: true, text: "Queued — the delivery-contact change will go live after review. Track it below." });
      bumpRefresh();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Submit failed." });
    } finally {
      setBusy(false);
    }
  }

  async function submitContractor(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const entity = cf.entity.trim();
    const signature_entity = cf.signature_entity.trim();
    const prime = cf.prime_contractor_default.trim();
    if (!entity) {
      setMsg({ ok: false, text: "The contractor entity is required." });
      return;
    }
    if (!signature_entity) {
      setMsg({ ok: false, text: "The signature entity is required." });
      return;
    }
    if (!prime) {
      setMsg({ ok: false, text: "The default prime contractor is required." });
      return;
    }
    if (!cf.phone.trim()) {
      // _apply_contractor_edit rejects an empty phone — guard client-side (parity with purchaser).
      setMsg({ ok: false, text: "The contractor phone is required." });
      return;
    }
    const address_lines = cf.address_lines.split("\n").map((s) => s.trim()).filter(Boolean);
    if (address_lines.length === 0) {
      setMsg({ ok: false, text: "Enter at least one address line." });
      return;
    }
    const payload = { entity, address_lines, phone: cf.phone.trim(), signature_entity, prime_contractor_default: prime };
    setBusy(true);
    setMsg(null);
    try {
      await api.submitConfigEdit({ workstream: SUB_WORKSTREAM, artifact_key: "contractor", op: "edit", payload });
      setContractorOpen(false);
      setMsg({ ok: true, text: "Queued — the contractor change will go live after review. Track it below." });
      bumpRefresh();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Submit failed." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <PageShell onHome={onBack}>
      <h2 className="page__heading">PO/SC Configuration</h2>
      <p className="muted po-config__intro">
        The identity, tax, and terms values that print on every purchase order and subcontract. An
        admin can edit them here — each change is queued for review and takes effect (or fails, never
        silently) once the operator&rsquo;s config actuator validates and deploys it. Editing terms
        text mints a new version behind a legal-review gate; it is not used until the operator clears it.
      </p>

      {/* Tab bar — cycle between the two config workstreams (a switch, not a route). */}
      <nav className="admin-tabs" role="tablist" aria-label="Config workstream">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "po"}
          className={`admin-tabs__tab${tab === "po" ? " admin-tabs__tab--active" : ""}`}
          onClick={() => setTab("po")}
        >
          Purchase Order
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "sub"}
          className={`admin-tabs__tab${tab === "sub" ? " admin-tabs__tab--active" : ""}`}
          onClick={() => setTab("sub")}
        >
          Subcontract
        </button>
      </nav>

      {msg && <div className={`banner ${msg.ok ? "banner--ok" : "banner--err"}`}>{msg.text}</div>}
      {error && <div className="banner banner--err">{error}</div>}
      {loading && !config && tab === "po" && <div className="centered muted">Loading…</div>}

      {/* ── Purchase Order tab: Purchaser identity + tax table + PO terms library ─── */}
      {tab === "po" && config && (
        <>
          {/* ── Purchaser identity (D5) ─────────────────────────────────────────────── */}
          <section className="card dash-section" aria-label="Purchaser identity">
            <h3 className="jha__section-title">Purchaser</h3>
            <div className="po-config__block">
              <div className="po-config__entity">{config.purchaser.entity}</div>
              {config.purchaser.address_lines.map((line, i) => (
                <div key={i} className="po-config__line muted">
                  {line}
                </div>
              ))}
              {config.purchaser.phone && (
                <div className="po-config__line muted">{config.purchaser.phone}</div>
              )}
            </div>
            <div className="po-config__block">
              <div className="field__label">Invoice routing</div>
              <div className="dash-chips">
                <span className="dash-chip">To: {config.purchaser.invoice_routing.to}</span>
                {config.purchaser.invoice_routing.cc.map((cc) => (
                  <span key={cc} className="dash-chip">
                    CC: {cc}
                  </span>
                ))}
              </div>
            </div>

            {canManage &&
              (purchaserOpen ? (
                <form className="accounts__editor" onSubmit={submitPurchaser}>
                  <FieldInput label="Entity (required)" value={pf.entity} onChange={(v) => setPf({ ...pf, entity: v })} />
                  <FieldTextarea
                    label="Address lines (one per line)"
                    value={pf.address_lines}
                    rows={3}
                    maxLength={1000}
                    onChange={(v) => setPf({ ...pf, address_lines: v })}
                  />
                  <FieldInput label="Phone" value={pf.phone} onChange={(v) => setPf({ ...pf, phone: v })} />
                  <FieldInput label="Invoice routing — To (required)" value={pf.to} onChange={(v) => setPf({ ...pf, to: v })} />
                  <FieldTextarea
                    label="Invoice routing — CC (one email per line)"
                    value={pf.cc}
                    rows={3}
                    maxLength={1000}
                    onChange={(v) => setPf({ ...pf, cc: v })}
                  />
                  <div className="jha__actions">
                    <button className="btn btn--primary" type="submit">
                      {busy ? "Working…" : "Queue change"}
                    </button>
                    <button className="btn btn--secondary" type="button" onClick={() => setPurchaserOpen(false)}>
                      Cancel
                    </button>
                  </div>
                </form>
              ) : (
                <div className="jha__actions">
                  <button className="btn btn--edit" type="button" onClick={openPurchaser}>
                    Edit purchaser
                  </button>
                </div>
              ))}
          </section>

          {/* ── Ship-to-state tax table (D8) ────────────────────────────────────────── */}
          <section className="card dash-section" aria-label="Tax table">
            <h3 className="jha__section-title">
              Sales tax by ship-to state <span className="dash-pill">{taxStates.length}</span>
            </h3>
            {taxStates.length === 0 ? (
              <p className="muted">No tax states configured.</p>
            ) : (
              <div className="dash-grid">
                {taxStates.map((st) => {
                  const bp = config.tax.rates_bp[st];
                  return (
                    <section key={st} className="card">
                      <div className="po-config__tax-rate">{bp == null ? "—" : bpToPct(bp)}</div>
                      <div className="dash-chips">
                        <span className="dash-chip">{st}</span>
                        {config.tax.state_names[st] && (
                          <span className="dash-chip">{config.tax.state_names[st]}</span>
                        )}
                      </div>
                    </section>
                  );
                })}
              </div>
            )}

            {canManage &&
              (taxOpen ? (
                <form className="accounts__editor" onSubmit={submitTax}>
                  <p className="muted">Enter each rate as a percent (e.g. 9.25) — it is stored as integer basis points.</p>
                  {taxRows.map((row, i) => (
                    <div className="po-config__tax-edit-row" key={i}>
                      <FieldInput
                        label="State"
                        className="field--state"
                        value={row.state}
                        placeholder="IL"
                        onChange={(v) => setTaxRows(taxRows.map((r, j) => (j === i ? { ...r, state: v } : r)))}
                      />
                      <FieldInput
                        label="Name"
                        value={row.name}
                        placeholder="Illinois"
                        onChange={(v) => setTaxRows(taxRows.map((r, j) => (j === i ? { ...r, name: v } : r)))}
                      />
                      <FieldInput
                        label="Rate %"
                        className="field--rate"
                        value={row.rate}
                        placeholder="9.25"
                        onChange={(v) => setTaxRows(taxRows.map((r, j) => (j === i ? { ...r, rate: v } : r)))}
                      />
                      <button
                        className="btn btn--retire"
                        type="button"
                        aria-label={`Remove ${row.state || "row"}`}
                        onClick={() => setTaxRows(taxRows.filter((_, j) => j !== i))}
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                  <div className="jha__actions">
                    <button
                      className="btn btn--secondary"
                      type="button"
                      onClick={() => setTaxRows([...taxRows, { state: "", name: "", rate: "" }])}
                    >
                      + Add state
                    </button>
                  </div>
                  <div className="jha__actions">
                    <button className="btn btn--primary" type="submit">
                      {busy ? "Working…" : "Queue change"}
                    </button>
                    <button className="btn btn--secondary" type="button" onClick={() => setTaxOpen(false)}>
                      Cancel
                    </button>
                  </div>
                </form>
              ) : (
                <div className="jha__actions">
                  <button className="btn btn--edit" type="button" onClick={openTax}>
                    Edit tax table
                  </button>
                </div>
              ))}
          </section>

          {/* ── Delivery contacts (Feature C) — the builder's <datalist> suggestion list ── */}
          <section className="card dash-section" aria-label="Delivery contacts">
            <h3 className="jha__section-title">
              Delivery contacts <span className="dash-pill">{config.delivery_contacts.length}</span>
            </h3>
            <p className="muted">
              Suggested in the PO builder&rsquo;s delivery-contact field — picking a name fills its
              phone and email. Free-text contacts are always still accepted on a PO.
            </p>
            {config.delivery_contacts.length === 0 ? (
              <p className="muted">No delivery contacts configured.</p>
            ) : (
              <div className="dash-grid">
                {config.delivery_contacts.map((ct) => (
                  <section key={ct.name} className="card">
                    <div className="po-config__entity">{ct.name}</div>
                    <div className="dash-chips">
                      {ct.phone && <span className="dash-chip">{ct.phone}</span>}
                      {ct.email && <span className="dash-chip">{ct.email}</span>}
                    </div>
                  </section>
                ))}
              </div>
            )}

            {canManage &&
              (deliveryOpen ? (
                <form className="accounts__editor" onSubmit={submitDelivery}>
                  <p className="muted">Name is required; phone and email are optional (an exact name pick fills them in the builder).</p>
                  {deliveryRows.map((row, i) => (
                    <div className="po-config__tax-edit-row" key={i}>
                      <FieldInput
                        label="Name"
                        value={row.name}
                        placeholder="Riley Receiver"
                        onChange={(v) => setDeliveryRows(deliveryRows.map((r, j) => (j === i ? { ...r, name: v } : r)))}
                      />
                      <FieldInput
                        label="Phone"
                        value={row.phone}
                        placeholder="555-0142"
                        onChange={(v) => setDeliveryRows(deliveryRows.map((r, j) => (j === i ? { ...r, phone: v } : r)))}
                      />
                      <FieldInput
                        label="Email"
                        value={row.email}
                        placeholder="riley@site.example"
                        onChange={(v) => setDeliveryRows(deliveryRows.map((r, j) => (j === i ? { ...r, email: v } : r)))}
                      />
                      <button
                        className="btn btn--retire"
                        type="button"
                        aria-label={`Remove ${row.name || "contact"}`}
                        onClick={() => setDeliveryRows(deliveryRows.filter((_, j) => j !== i))}
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                  <div className="jha__actions">
                    <button
                      className="btn btn--secondary"
                      type="button"
                      onClick={() => setDeliveryRows([...deliveryRows, { name: "", phone: "", email: "" }])}
                    >
                      + Add contact
                    </button>
                  </div>
                  <div className="jha__actions">
                    <button className="btn btn--primary" type="submit">
                      {busy ? "Working…" : "Queue change"}
                    </button>
                    <button className="btn btn--secondary" type="button" onClick={() => setDeliveryOpen(false)}>
                      Cancel
                    </button>
                  </div>
                </form>
              ) : (
                <div className="jha__actions">
                  <button className="btn btn--edit" type="button" onClick={openDelivery}>
                    Edit delivery contacts
                  </button>
                </div>
              ))}
          </section>

          {/* ── PO terms-library profiles (D6/S3) — the SHARED editor, parameterized to po_materials ── */}
          <TermsProfilesEditor
            workstream={WORKSTREAM}
            heading="Terms & conditions profiles"
            terms={terms}
            canManage={canManage}
            busy={busy}
            setBusy={setBusy}
            fetchTermsText={api.fetchTermsText}
            fetchTermsVersions={api.fetchTermsVersions}
            submitConfigEdit={api.submitConfigEdit}
            setMsg={setMsg}
            onQueued={bumpRefresh}
          />
        </>
      )}

      {/* ── Subcontract tab: Contractor identity + the subcontract terms library + the per-trade
           Exhibit A Article II templates. Gated on cap.subcontracts.manage (the read routes require it
           too). Payment-terms editing is a fast-follow (CE-7). ── */}
      {tab === "sub" &&
        (canManageSub ? (
          <>
              <section className="card dash-section" aria-label="Contractor identity">
                <h3 className="jha__section-title">Contractor (subcontracts)</h3>
                {subConfig ? (
                  <>
                    <div className="po-config__block">
                      <div className="po-config__entity">{subConfig.contractor.entity}</div>
                      {subConfig.contractor.address_lines.map((line, i) => (
                        <div key={i} className="po-config__line muted">
                          {line}
                        </div>
                      ))}
                      {subConfig.contractor.phone && (
                        <div className="po-config__line muted">{subConfig.contractor.phone}</div>
                      )}
                    </div>
                    <div className="po-config__block">
                      <div className="field__label">Signature &amp; prime</div>
                      <div className="dash-chips">
                        <span className="dash-chip">Signs as: {subConfig.contractor.signature_entity}</span>
                        <span className="dash-chip">Default prime: {subConfig.contractor.prime_contractor_default}</span>
                      </div>
                    </div>
                    {contractorOpen ? (
                      <form className="accounts__editor" onSubmit={submitContractor}>
                        <FieldInput label="Entity (required)" value={cf.entity} onChange={(v) => setCf({ ...cf, entity: v })} />
                        <FieldTextarea
                          label="Address lines (one per line)"
                          value={cf.address_lines}
                          rows={3}
                          maxLength={1000}
                          onChange={(v) => setCf({ ...cf, address_lines: v })}
                        />
                        <FieldInput label="Phone" value={cf.phone} onChange={(v) => setCf({ ...cf, phone: v })} />
                        <FieldInput
                          label="Signature entity (required)"
                          value={cf.signature_entity}
                          onChange={(v) => setCf({ ...cf, signature_entity: v })}
                        />
                        <FieldInput
                          label="Default prime contractor (required)"
                          value={cf.prime_contractor_default}
                          onChange={(v) => setCf({ ...cf, prime_contractor_default: v })}
                        />
                        <div className="jha__actions">
                          <button className="btn btn--primary" type="submit">
                            {busy ? "Working…" : "Queue change"}
                          </button>
                          <button className="btn btn--secondary" type="button" onClick={() => setContractorOpen(false)}>
                            Cancel
                          </button>
                        </div>
                      </form>
                    ) : (
                      <div className="jha__actions">
                        <button className="btn btn--edit" type="button" onClick={openContractor}>
                          Edit contractor
                        </button>
                      </div>
                    )}
                  </>
                ) : (
                  <p className="muted">Loading the subcontract contractor identity…</p>
                )}
              </section>

              <TermsProfilesEditor
                workstream={SUB_WORKSTREAM}
                heading="Subcontract terms profiles"
                terms={subTerms}
                canManage={canManageSub}
                busy={busy}
                setBusy={setBusy}
                fetchTermsText={sub.fetchTermsText}
                fetchTermsVersions={sub.fetchTermsVersions}
                submitConfigEdit={api.submitConfigEdit}
                setMsg={setMsg}
                onQueued={bumpRefresh}
              />

              <ExhibitTemplatesEditor
                canManage={canManageSub}
                busy={busy}
                setBusy={setBusy}
                fetchKeys={sub.fetchExhibitTemplateKeys}
                fetchText={sub.fetchExhibitKeyText}
                fetchVersions={sub.fetchExhibitKeyVersions}
                submitConfigEdit={api.submitConfigEdit}
                setMsg={setMsg}
                onQueued={bumpRefresh}
              />
          </>
        ) : (
          <p className="muted">
            You don&rsquo;t have subcontract configuration access (cap.subcontracts.manage).
          </p>
        ))}

      {/* ── Status monitor (shared — tracks queued changes for either workstream) ─── */}
      {(canManage || canManageSub) && <ConfigStatusMonitor refreshSignal={refreshSignal} />}
    </PageShell>
  );
}

// ── Status monitor (a read-only poll of the §50 config queue — the SPA never advances it) ──────────
// Mirrors PublishMonitor: fast (4s) while anything is in flight, slow (20s) once terminal; re-polls
// immediately on `refreshSignal`. A `failed` row is NEVER silent — it shows the RED stage + the
// server's failure_reason verbatim.

const CONFIG_STEPS = [
  { key: "queued", label: "Queued" },
  { key: "validated", label: "Validated" },
  { key: "tested", label: "Tested" },
  { key: "live", label: "Live" },
  { key: "archived", label: "Archived" },
] as const;

const STATUS_INDEX: Record<api.ConfigStatus, number> = {
  queued: 0,
  validated: 1,
  tested: 2,
  merged: 3, // transient toward live → render the Live step "in progress"
  live: 3,
  archived: 4,
  failed: -1,
};

const CONFIG_TERMINAL = new Set<api.ConfigStatus>(["archived", "failed"]);

const CONFIG_OP_LABEL: Record<api.ConfigOp, string> = {
  edit: "Edit",
  add_version: "Add version",
  set_current: "Make current",
  create_profile: "New profile",
};

function fmtTime(t: number): string {
  // config_requests.created_at/updated_at are unix SECONDS (migration 0045 unixepoch()); Date()
  // expects milliseconds, so ×1000.
  const d = new Date(t * 1000);
  if (Number.isNaN(d.getTime())) return String(t);
  return d.toLocaleString();
}

// Map the recorded failed_stage onto a stepper index so the RED dot lands sensibly.
function stepIndexForFailure(req: api.ConfigRequest): number {
  const stage = (req.failed_stage ?? "").toLowerCase();
  if (stage.includes("archive")) return 4;
  if (stage.includes("live") || stage.includes("merge") || stage.includes("deploy")) return 3;
  if (stage.includes("test")) return 2;
  if (stage.includes("valid")) return 1;
  if (stage.includes("queue")) return 0;
  return 1;
}

export function ConfigStatusMonitor({ refreshSignal }: { refreshSignal?: number }) {
  const [requests, setRequests] = useState<api.ConfigRequest[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const rows = await api.fetchConfigStatus();
      setRequests(rows);
      setErr(null);
      return rows;
    } catch {
      setErr("Could not load the config-change status.");
      return null;
    }
  }, []);

  useEffect(() => {
    let active = true;
    const tick = async () => {
      if (!active) return;
      const rows = await load();
      if (!active) return;
      const inFlight = (rows ?? []).some((r) => !CONFIG_TERMINAL.has(r.status));
      timer.current = setTimeout(() => void tick(), inFlight ? 4000 : 20000);
    };
    void tick();
    return () => {
      active = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load]);

  useEffect(() => {
    if (refreshSignal !== undefined) void load();
  }, [refreshSignal, load]);

  return (
    <section className="card form-editor__monitor" aria-label="Config change status">
      <div className="form-editor__monitor-head">
        <h2 className="page__heading">Config change status</h2>
        <div className="jha__actions" style={{ marginTop: 0 }}>
          <button type="button" className="btn btn--secondary" onClick={() => void load()}>
            Refresh
          </button>
        </div>
      </div>
      {err ? (
        <p className="login__error" role="alert">
          {err}
        </p>
      ) : requests === null ? (
        <p className="muted">Loading…</p>
      ) : requests.length === 0 ? (
        <p className="muted">No config changes yet.</p>
      ) : (
        <ul className="form-editor__monitor-list">
          {requests.map((r) => (
            <ConfigRequestRow key={r.id} req={r} onCleared={() => void load()} />
          ))}
        </ul>
      )}
    </section>
  );
}

// The resting states a request may be cleared (soft-dismissed) from — LOCKSTEP with the Worker's
// CONFIG_CLEARABLE_STATUSES (worker/config.ts). 'live' counts (the deploy succeeded); the in-flight
// states never do (the Worker refuses them 409).
const CONFIG_CLEARABLE = new Set<api.ConfigStatus>(["live", "archived", "failed"]);

function ConfigRequestRow({ req, onCleared }: { req: api.ConfigRequest; onCleared: () => void }) {
  const failed = req.status === "failed";
  const reached = STATUS_INDEX[req.status];
  const clearable = CONFIG_CLEARABLE.has(req.status);
  const [clearing, setClearing] = useState(false);
  const [clearErr, setClearErr] = useState<string | null>(null);

  async function clear() {
    if (clearing) return;
    setClearing(true);
    setClearErr(null);
    try {
      await api.clearConfigRequest(req.id);
      onCleared(); // re-poll: the cleared row drops out of the default monitor view
    } catch (e) {
      setClearErr(e instanceof Error ? e.message : "Could not clear this change.");
      setClearing(false);
    }
  }

  return (
    <li className={`form-editor__req${failed ? " form-editor__req--failed" : ""}`}>
      <div className="form-editor__req-head">
        <span className="form-editor__req-op">{CONFIG_OP_LABEL[req.op] ?? req.op}</span>
        <span className="form-editor__req-target">
          {req.workstream}/{req.artifact_key}
        </span>
        <span className={`form-editor__req-status form-editor__req-status--${req.status}`}>{req.status}</span>
        <span className="form-editor__req-time muted">{fmtTime(req.updated_at)}</span>
        {clearable && (
          <button
            type="button"
            className="btn btn--secondary form-editor__req-clear"
            onClick={() => void clear()}
            disabled={clearing}
            aria-label={`Clear ${CONFIG_OP_LABEL[req.op] ?? req.op} ${req.workstream}/${req.artifact_key}`}
          >
            {clearing ? "Clearing…" : "Clear"}
          </button>
        )}
      </div>
      <ol className="form-editor__stepper" aria-label="Config change progress">
        {CONFIG_STEPS.map((step, i) => {
          let state: "done" | "current" | "todo" | "failed";
          if (failed) {
            const at = stepIndexForFailure(req);
            state = i < at ? "done" : i === at ? "failed" : "todo";
          } else if (i < reached) {
            state = "done";
          } else if (i === reached) {
            state = req.status === "archived" ? "done" : "current";
          } else {
            state = "todo";
          }
          return (
            <li key={step.key} className={`form-editor__step form-editor__step--${state}`}>
              <span className="form-editor__step-dot" aria-hidden="true" />
              <span>{step.label}</span>
            </li>
          );
        })}
      </ol>
      {failed ? (
        <p className="form-editor__req-failure" role="alert">
          Failed{req.failed_stage ? ` at ${req.failed_stage}` : ""}
          {req.failure_reason ? `: ${req.failure_reason}` : "."}
        </p>
      ) : null}
      {clearErr ? (
        <p className="form-editor__req-failure" role="alert">
          {clearErr}
        </p>
      ) : null}
    </li>
  );
}
