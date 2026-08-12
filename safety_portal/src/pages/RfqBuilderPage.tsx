import { useCallback, useEffect, useMemo, useState } from "react";
import * as rfq from "../lib/rfq";
import { createVendor, fetchVendors, fetchJobShipTo, fetchPoMaterials, catalogLineFields, type Vendor, type CatalogMaterial } from "../lib/po";
import { fetchJobs, type Job } from "../lib/api";
import { useAuth } from "../lib/auth";

// RFQ composer R1 (ADR-0004) — the office multi-vendor Request-for-Quote builder + tracker.
// FOLDED (2026-07): renders as the "RFQs" TAB PANEL inside PurchaseOrdersPage (the hub owns
// the PageShell + tab strip beside the Orders / Vendor Estimates panels); the panel stays
// mounted across tab flips, so a half-composed RFQ survives a glance at the other tabs.
// One page, two faces (the EstimatesPage/PoBuilderPage shape): the TRACKER (every RFQ from
// GET /api/po/rfqs with per-vendor status badges) and the BUILDER (job pick with the PO
// ship-to autofill, scope, due date, a PRICE-FREE line grid, and the multi-vendor chip
// picker). cap.po.manage gates the view (router VIEW_CAPS) AND every write affordance; the
// Worker re-gates every call (Invariant 2 — SPA gating is convenience, never the boundary).
//
// PRICE-FREE: no money field renders anywhere here — the RFQ asks vendors for prices; the
// answers come back through the Vendor Estimates importer. Generate signs + queues the RFQ
// (SEND-FREE); the Mac daemon renders/files per vendor, and sending happens only after
// F22-verified human approval Mac-side (Invariant 1).

const STATUS_PILL: Record<rfq.RfqStatus, string> = {
  draft: "dash-pill",
  queued: "dash-pill dash-pill--warn",
  generated: "dash-pill dash-pill--warn",
  partially_sent: "dash-pill dash-pill--warn",
  sent: "dash-pill dash-pill--ok",
  closed: "dash-pill",
  canceled: "dash-pill",
};

const VENDOR_PILL: Record<rfq.RfqVendorStatus, string> = {
  pending: "dash-pill",
  filed: "dash-pill dash-pill--warn",
  sent: "dash-pill dash-pill--ok",
  responded: "dash-pill dash-pill--ok",
  canceled: "dash-pill",
};

const JOB_NO_RE = /^\d{4}\.\d{3}$/;
// 0070 — Site/phase is a plain non-negative integer, same shape the PO builder uses.
const INT_RE = /^\d+$/;
// UI hint only — worker/po.ts parseVendorFields re-validates with the same shape (Invariant 2).
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/** One editable line-grid row (qty stays a string while typing; parsed at save). */
interface LineDraft {
  part_number: string;
  description: string;
  qty: string;
  unit: string;
  line_note: string;
}
const emptyLine = (): LineDraft => ({ part_number: "", description: "", qty: "", unit: "", line_note: "" });

export function RfqBuilderPage() {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.po.manage"); // UI affordance only — the Worker re-gates

  const [rows, setRows] = useState<rfq.RfqListRow[]>([]);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [statusFilter, setStatusFilter] = useState<"all" | rfq.RfqStatus>("all");
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // ── Builder state (null editingId = tracker face; 0 = new draft; >0 = editing) ────────────────
  const [editingId, setEditingId] = useState<number | null>(null);
  const [jobId, setJobId] = useState("");
  const [jobNo, setJobNo] = useState("");
  // 0070 — the Evergreen identifier's site segment. Auto-filled from the job on select; stays
  // operator-editable, matching the PO builder. 0 = no site, and the number then omits it.
  const [sitePhase, setSitePhase] = useState("0");
  const [jobName, setJobName] = useState("");
  const [shipTo, setShipTo] = useState({
    ship_to_name: "", ship_to_address: "", ship_to_city: "", ship_to_state: "", ship_to_zip: "",
    delivery_contact_name: "", delivery_contact_phone: "", delivery_contact_email: "",
  });
  const [scopeText, setScopeText] = useState("");
  const [dueDate, setDueDate] = useState("");
  const [lines, setLines] = useState<LineDraft[]>([emptyLine()]);
  const [vendorKeys, setVendorKeys] = useState<string[]>([]);
  const [vendorPick, setVendorPick] = useState("");
  // ── Quick-add vendor (operator ask 2026-07-20: "free text, not just a pick list") ──────────────
  // Free-TEXT ENTRY that lands as a real DIRECTORY row: the RFQ send lane resolves the
  // recipient live from ITS_Vendors by Vendor Key (ADR-0004 decision 9 — the recipient-
  // poisoning closure), so a keyless free-text vendor could never be sent to. The quick-add
  // calls the EXISTING POST /api/po/vendors (PoVendorsPage's create route: atomic VEN-######
  // allocation, origin='portal', immediately dirty for the §51 up-sync) and joins the new key
  // to this RFQ on success. Email is REQUIRED here (the route allows blank, but a blank
  // Contact Email makes the send HOLD) — required now beats held later.
  const [nvOpen, setNvOpen] = useState(false);
  const [nvName, setNvName] = useState("");
  const [nvEmail, setNvEmail] = useState("");
  const [nvContact, setNvContact] = useState("");
  const [nvBusy, setNvBusy] = useState(false);
  // Material-catalog TYPE vocabulary (the SAME price-free picker the PO builder uses —
  // GET /api/po/materials, cap.po.manage; identity only, no price). Selecting a type fills a
  // line's Part # + Description; free text over those fields stays the fallback for non-catalog items.
  const [catalog, setCatalog] = useState<CatalogMaterial[]>([]);

  const reload = useCallback((status?: rfq.RfqStatus) => {
    setLoading(true);
    rfq
      .fetchRfqs(status)
      .then(setRows)
      .catch(() => setMsg({ ok: false, text: "Failed to load RFQs." }))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reload();
    fetchVendors().then(setVendors).catch(() => setVendors([]));
    fetchJobs().then(setJobs).catch(() => setJobs([]));
    fetchPoMaterials().then(setCatalog).catch(() => setCatalog([]));
  }, [reload]);

  const vendorByKey = useMemo(() => new Map(vendors.map((v) => [v.vendor_key, v])), [vendors]);
  const activeVendors = useMemo(() => vendors.filter((v) => v.active === 1), [vendors]);

  function resetBuilder() {
    setJobId("");
    setJobNo("");
    setSitePhase("0");
    setJobName("");
    setShipTo({
      ship_to_name: "", ship_to_address: "", ship_to_city: "", ship_to_state: "", ship_to_zip: "",
      delivery_contact_name: "", delivery_contact_phone: "", delivery_contact_email: "",
    });
    setScopeText("");
    setDueDate("");
    setLines([emptyLine()]);
    setVendorKeys([]);
    setVendorPick("");
    setNvOpen(false);
    setNvName("");
    setNvEmail("");
    setNvContact("");
  }

  /** Create the typed vendor through the existing directory route, then join it to the RFQ.
   *  Optimistically appended to the local vendor list (full shape, sync_state pending) so the
   *  chip shows the NAME immediately; a quiet refetch then converges on the server row. */
  async function onQuickAddVendor() {
    const vendor_name = nvName.trim();
    const contact_email = nvEmail.trim();
    if (nvBusy || !vendor_name || !EMAIL_RE.test(contact_email)) return;
    // Cap check BEFORE the create (adversarial review 2026-07-20): the select can join the
    // 12th vendor while this form sits open, and creating-then-silently-not-joining with a
    // success banner would be an affirmative false claim — refuse visibly instead.
    if (vendorKeys.length >= rfq.MAX_RFQ_VENDORS) {
      setMsg({
        ok: false,
        text: `This RFQ already has the maximum ${rfq.MAX_RFQ_VENDORS} vendors — remove one before adding another.`,
      });
      return;
    }
    setNvBusy(true);
    setMsg(null);
    try {
      const { vendor_key } = await createVendor({
        vendor_name,
        contact_email,
        contact_name: nvContact.trim() || undefined,
      });
      setVendors((vs) => [
        ...vs,
        {
          vendor_key,
          vendor_name,
          address: "",
          contact_name: nvContact.trim(),
          contact_email,
          contact_phone: "",
          region: "",
          supply_categories: [],
          default_terms_profile: "",
          gtc_reference: "",
          active: 1,
          notes: "",
          origin: "portal",
          sync_state: "pending",
          mirror_version: 1,
        },
      ]);
      // Dedupe-only join: the cap was refused BEFORE the create, and the "Add a vendor"
      // select is disabled while nvBusy, so vendorKeys cannot reach the cap during the
      // in-flight create — the join (and the success banner's "and this RFQ") always holds.
      setVendorKeys((ks) => (ks.includes(vendor_key) ? ks : [...ks, vendor_key]));
      setNvOpen(false);
      setNvName("");
      setNvEmail("");
      setNvContact("");
      setMsg({ ok: true, text: `Vendor added to the directory and this RFQ (${vendor_key}).` });
      // Convergence read, MERGE-safe: never let a response missing the just-created row
      // (a stale read) clobber the optimistic entry — locally-known keys are kept.
      fetchVendors()
        .then((server) =>
          setVendors((vs) => {
            const known = new Set(server.map((v) => v.vendor_key));
            return [...server, ...vs.filter((v) => !known.has(v.vendor_key))];
          }),
        )
        .catch(() => {});
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Vendor create failed." });
    } finally {
      setNvBusy(false);
    }
  }

  // Job pick → ship-to/delivery autofill (the PO builder's convenience feed; a 404 or
  // absent field silently leaves the block blank — every field stays editable).
  function onJobSelect(id: string) {
    setJobId(id);
    const job = jobs.find((j) => j.job_id === id);
    if (job) {
      setJobName(job.project_name);
      // Stored Evergreen number (0057) + site (0064) first; the name-prefix parse stays the
      // fallback. Anchored at the END OF THE NUMBER (`(?![\d.])`, not `$` — the name continues):
      // the old open-tailed form matched "2026.384.1 Coker" and returned the truncated
      // "2026.384", i.e. a DIFFERENT site of the same project. Both parts come from the SAME
      // source — stored or parsed, never one of each — or the RFQ numbers a site nobody chose.
      const m = /^(\d{4}\.\d{3})(?:\.(\d+))?(?![\d.])/.exec((job.project_name ?? "").trim());
      const storedJobNo = job.job_no || "";
      setJobNo(storedJobNo || (m ? m[1] : ""));
      setSitePhase(String(storedJobNo ? (job.site_phase ?? 0) : m && m[2] !== undefined ? parseInt(m[2], 10) : 0));
    }
    if (!id) return;
    fetchJobShipTo(id)
      .then((s) => {
        // The ship-to read is the authoritative echo of the job record; take job_no and the
        // site together so they can never be mixed from different sources.
        if (s.job_no) {
          setJobNo(s.job_no);
          setSitePhase(String(s.site_phase ?? 0));
        }
        setShipTo({
          ship_to_name: s.ship_to_name, ship_to_address: s.ship_to_address,
          ship_to_city: s.ship_to_city, ship_to_state: s.ship_to_state, ship_to_zip: s.ship_to_zip,
          delivery_contact_name: s.delivery_contact_name,
          delivery_contact_phone: s.delivery_contact_phone,
          delivery_contact_email: s.delivery_contact_email,
        });
      })
      .catch(() => {}); // autofill is a convenience, never a gate
  }

  function openDraft(id: number) {
    setBusy(true);
    rfq
      .fetchRfq(id)
      .then((d) => {
        setEditingId(id);
        setJobId("");
        setJobNo(d.rfq.job_no);
        // Restore the site too — without this, re-saving an existing draft silently resets it
        // to 0 and the RFQ generates against the wrong site of the project.
        setSitePhase(String(d.rfq.site_phase ?? 0));
        setJobName(d.rfq.job_name);
        setShipTo({
          ship_to_name: d.rfq.ship_to_name, ship_to_address: d.rfq.ship_to_address,
          ship_to_city: d.rfq.ship_to_city, ship_to_state: d.rfq.ship_to_state,
          ship_to_zip: d.rfq.ship_to_zip, delivery_contact_name: d.rfq.delivery_contact_name,
          delivery_contact_phone: d.rfq.delivery_contact_phone,
          delivery_contact_email: d.rfq.delivery_contact_email,
        });
        setScopeText(d.rfq.scope_text);
        setDueDate(d.rfq.due_date ?? "");
        setLines(
          d.line_items.length > 0
            ? d.line_items.map((l) => ({
                part_number: l.part_number, description: l.description,
                qty: l.qty === null ? "" : String(l.qty), unit: l.unit, line_note: l.line_note,
              }))
            : [emptyLine()],
        );
        setVendorKeys(d.vendors.map((v) => v.vendor_key));
        setMsg(null);
      })
      .catch(() => setMsg({ ok: false, text: "Failed to load that RFQ." }))
      .finally(() => setBusy(false));
  }

  /** Build the draft body, or return a human problem string. */
  function buildBody(): rfq.RfqDraftBody | string {
    if (!JOB_NO_RE.test(jobNo.trim())) return "Enter the job number as YYYY.NNN.";
    if (!INT_RE.test(sitePhase.trim()) || parseInt(sitePhase, 10) > 9999) {
      return "Site / phase must be a whole number from 0 to 9999.";
    }
    const items: rfq.RfqDraftBody["line_items"] = [];
    for (const l of lines) {
      const description = l.description.trim();
      if (!description) {
        if (!l.part_number.trim() && !l.qty.trim() && !l.unit.trim() && !l.line_note.trim()) continue; // wholly blank row
        return "Every line needs a description.";
      }
      let qty: number | null = null;
      if (l.qty.trim()) {
        const n = Number(l.qty);
        if (!Number.isFinite(n) || n < 0) return `"${l.qty}" isn't a valid quantity.`;
        qty = n;
      }
      items.push({
        part_number: l.part_number.trim() || undefined,
        description,
        qty,
        unit: l.unit.trim() || undefined,
        line_note: l.line_note.trim() || undefined,
      });
    }
    if (items.length === 0) return "Add at least one line item.";
    if (vendorKeys.length === 0) return "Pick at least one vendor.";
    return {
      job_no: jobNo.trim(),
      site_phase: parseInt(sitePhase, 10),
      job_name: jobName.trim() || undefined,
      ...shipTo,
      scope_text: scopeText.trim() || undefined,
      due_date: dueDate.trim() || null,
      line_items: items,
      vendor_keys: vendorKeys,
    };
  }

  async function onSave(): Promise<number | null> {
    const body = buildBody();
    if (typeof body === "string") {
      setMsg({ ok: false, text: body });
      return null;
    }
    setBusy(true);
    setMsg(null);
    try {
      if (editingId !== null && editingId > 0) {
        await rfq.updateRfqDraft(editingId, body);
        setMsg({ ok: true, text: "Draft saved." });
        return editingId;
      }
      const { id } = await rfq.createRfqDraft(body);
      setEditingId(id);
      setMsg({ ok: true, text: "Draft created." });
      return id;
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Save failed." });
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function onGenerate() {
    const id = await onSave(); // save-then-generate: the signed snapshot is what's on screen
    if (id === null) return;
    setBusy(true);
    try {
      const res = await rfq.generateRfq(id);
      if (res.ok) {
        setMsg({ ok: true, text: `${res.rfq_number} generated — queued for per-vendor rendering.` });
        setEditingId(null);
        resetBuilder();
        reload(statusFilter === "all" ? undefined : statusFilter);
      } else if (res.error === "rfq_number_conflict" || res.error === "draft_changed") {
        setMsg({ ok: false, text: "The draft changed underneath — please generate again." });
      } else {
        setMsg({ ok: false, text: "This RFQ is no longer a draft — refresh the list." });
      }
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Generate failed." });
    } finally {
      setBusy(false);
    }
  }

  async function onCancelRfq(id: number) {
    setBusy(true);
    try {
      await rfq.cancelRfq(id);
      setMsg({ ok: true, text: "RFQ canceled." });
      reload(statusFilter === "all" ? undefined : statusFilter);
    } catch {
      setMsg({ ok: false, text: "Cancel failed — the RFQ may have advanced past cancelable." });
    } finally {
      setBusy(false);
    }
  }

  function setLine(i: number, patch: Partial<LineDraft>) {
    setLines((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  }

  /** Pick from the material_catalog TYPE vocabulary: populate line `i`'s Part # + Description
   *  from the chosen type (catalogLineFields — model_id + manufacturer/key_specs, sliced to the
   *  64/512 caps). Qty/Unit/Note are left untouched, and typing over Part #/Description stays the
   *  free-text fallback. Mirrors PoBuilderPage.applyCatalog (price-free — the route carries no cost). */
  const applyCatalog = (i: number, id: number) => {
    const m = catalog.find((x) => x.id === id);
    if (m) setLine(i, catalogLineFields(m));
  };

  // ── Builder face ───────────────────────────────────────────────────────────────────────────────
  if (editingId !== null) {
    return (
      <>
        {/* h3: the hub's "Purchase Orders" h2 is the page heading — sub-faces nest under it. */}
        <h3 className="page__heading">{editingId > 0 ? "Edit RFQ draft" : "New RFQ"}</h3>
        {msg && <div className={`banner ${msg.ok ? "banner--ok" : "banner--err"}`}>{msg.text}</div>}

        <section className="card dash-section" aria-label="Job">
          <h3 className="jha__section-title">Job &amp; delivery</h3>
          <label className="field">
            <span className="field__label">Job (autofills ship-to)</span>
            <select className="field__input" aria-label="Job" value={jobId} onChange={(e) => onJobSelect(e.target.value)}>
              <option value="">— job —</option>
              {jobs.map((j) => (
                <option key={j.job_id} value={j.job_id}>
                  {j.project_name}
                </option>
              ))}
            </select>
          </label>
          <div className="jha__grid">
            <label className="field">
              <span className="field__label">Job number (YYYY.NNN)</span>
              <input className="field__input" value={jobNo} maxLength={8} onChange={(e) => setJobNo(e.target.value)} />
            </label>
            <label className="field">
              <span className="field__label">Site / phase</span>
              <input
                className="field__input"
                aria-label="Site / phase"
                value={sitePhase}
                maxLength={4}
                onChange={(e) => setSitePhase(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field__label">Job name</span>
              <input className="field__input" value={jobName} maxLength={256} onChange={(e) => setJobName(e.target.value)} />
            </label>
            <label className="field">
              <span className="field__label">Ship-to name</span>
              <input
                className="field__input"
                value={shipTo.ship_to_name}
                maxLength={256}
                onChange={(e) => setShipTo({ ...shipTo, ship_to_name: e.target.value })}
              />
            </label>
            <label className="field">
              <span className="field__label">Ship-to address</span>
              <input
                className="field__input"
                value={shipTo.ship_to_address}
                maxLength={512}
                onChange={(e) => setShipTo({ ...shipTo, ship_to_address: e.target.value })}
              />
            </label>
            <label className="field">
              <span className="field__label">Delivery contact</span>
              <input
                className="field__input"
                value={shipTo.delivery_contact_name}
                maxLength={256}
                onChange={(e) => setShipTo({ ...shipTo, delivery_contact_name: e.target.value })}
              />
            </label>
            <label className="field">
              <span className="field__label">Quote due date</span>
              <input className="field__input" type="date" value={dueDate} onChange={(e) => setDueDate(e.target.value)} />
            </label>
          </div>
          <label className="field">
            <span className="field__label">Scope of supply</span>
            <textarea
              className="field__input"
              rows={4}
              maxLength={8000}
              value={scopeText}
              onChange={(e) => setScopeText(e.target.value)}
            />
          </label>
        </section>

        <section className="card dash-section" aria-label="Line items">
          <h3 className="jha__section-title">Line items (no prices — the vendor quotes them)</h3>
          {lines.map((l, i) => (
            <div key={i} className="jha__grid">
              {catalog.length > 0 && (
                <label className="field">
                  <span className="field__label">Catalog</span>
                  <select
                    className="field__input"
                    aria-label={`Line ${i + 1} pick from catalog`}
                    value=""
                    onChange={(e) => {
                      const id = parseInt(e.target.value, 10);
                      if (Number.isSafeInteger(id)) applyCatalog(i, id);
                    }}
                  >
                    <option value="">— pick from catalog —</option>
                    {catalog.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.manufacturer ? `${m.manufacturer} · ` : ""}
                        {m.model_id}
                        {m.category ? ` (${m.category})` : ""}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="field">
                <span className="field__label">Part #</span>
                <input className="field__input" value={l.part_number} maxLength={64} onChange={(e) => setLine(i, { part_number: e.target.value })} />
              </label>
              <label className="field">
                <span className="field__label">Description</span>
                <input className="field__input" value={l.description} maxLength={512} onChange={(e) => setLine(i, { description: e.target.value })} />
              </label>
              <label className="field">
                <span className="field__label">Qty</span>
                <input className="field__input" inputMode="decimal" value={l.qty} onChange={(e) => setLine(i, { qty: e.target.value })} />
              </label>
              <label className="field">
                <span className="field__label">Unit</span>
                <input className="field__input" value={l.unit} maxLength={32} onChange={(e) => setLine(i, { unit: e.target.value })} />
              </label>
              <label className="field">
                <span className="field__label">Note</span>
                <input className="field__input" value={l.line_note} maxLength={256} onChange={(e) => setLine(i, { line_note: e.target.value })} />
              </label>
              <div className="jha__actions">
                <button
                  type="button"
                  className="btn btn--secondary"
                  disabled={lines.length === 1}
                  onClick={() => setLines((ls) => ls.filter((_, j) => j !== i))}
                >
                  Remove
                </button>
              </div>
            </div>
          ))}
          <div className="jha__actions">
            <button
              type="button"
              className="btn btn--secondary"
              disabled={lines.length >= rfq.MAX_RFQ_LINES}
              onClick={() => setLines((ls) => [...ls, emptyLine()])}
            >
              Add line
            </button>
          </div>
        </section>

        <section className="card dash-section" aria-label="Vendors">
          <h3 className="jha__section-title">Vendors (1–{rfq.MAX_RFQ_VENDORS})</h3>
          <div className="dash-card__row">
            {vendorKeys.map((k) => (
              <span key={k} className="dash-pill">
                {vendorByKey.get(k)?.vendor_name ?? k}{" "}
                <button
                  type="button"
                  // --danger, NOT --ghost: the chip sits on the light page, where the header
                  // ghost's white glyph is invisible. --danger is the design language's
                  // "small white/red utility-delete style kept for inline icon/remove controls".
                  className="btn btn--danger"
                  aria-label={`Remove ${vendorByKey.get(k)?.vendor_name ?? k}`}
                  onClick={() => setVendorKeys((ks) => ks.filter((x) => x !== k))}
                >
                  ×
                </button>
              </span>
            ))}
            {vendorKeys.length === 0 && <span className="muted">No vendors picked yet.</span>}
          </div>
          <label className="field">
            <span className="field__label">Add a vendor</span>
            <select
              className="field__input"
              aria-label="Add a vendor"
              disabled={nvBusy}
              value={vendorPick}
              onChange={(e) => {
                const k = e.target.value;
                setVendorPick("");
                if (k && vendorKeys.length < rfq.MAX_RFQ_VENDORS && !vendorKeys.includes(k)) {
                  setVendorKeys((ks) => [...ks, k]);
                }
              }}
            >
              <option value="">— vendor —</option>
              {activeVendors
                .filter((v) => !vendorKeys.includes(v.vendor_key))
                .map((v) => (
                  <option key={v.vendor_key} value={v.vendor_key}>
                    {v.vendor_name}
                  </option>
                ))}
            </select>
          </label>
          {/* Quick-add: type a vendor that isn't in the directory yet. It becomes a REAL
              directory row (the send lane emails the directory's Contact Email — never a
              free-floating name), then joins this RFQ immediately. */}
          {!nvOpen ? (
            <button
              type="button"
              className="btn btn--secondary"
              disabled={nvBusy || vendorKeys.length >= rfq.MAX_RFQ_VENDORS}
              onClick={() => setNvOpen(true)}
            >
              + New vendor (not in the list)
            </button>
          ) : (
            <>
              <div className="jha__grid">
                <label className="field">
                  <span className="field__label">Vendor name</span>
                  <input
                    className="field__input"
                    aria-label="New vendor name"
                    value={nvName}
                    maxLength={256}
                    onChange={(e) => setNvName(e.target.value)}
                  />
                </label>
                <label className="field">
                  <span className="field__label">Quote contact email</span>
                  <input
                    className="field__input"
                    aria-label="New vendor quote contact email"
                    type="email"
                    value={nvEmail}
                    maxLength={320}
                    onChange={(e) => setNvEmail(e.target.value)}
                  />
                </label>
                <label className="field">
                  <span className="field__label">Contact name (optional)</span>
                  <input
                    className="field__input"
                    aria-label="New vendor contact name"
                    value={nvContact}
                    maxLength={256}
                    onChange={(e) => setNvContact(e.target.value)}
                  />
                </label>
              </div>
              <p className="muted">
                Joins the vendor directory (and syncs to the vendor sheet) plus this RFQ — the
                quote request is emailed to this contact after operator approval.
              </p>
              <div className="jha__actions">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={nvBusy || !nvName.trim() || !EMAIL_RE.test(nvEmail.trim())}
                  onClick={() => void onQuickAddVendor()}
                >
                  {nvBusy ? "Adding…" : "Add vendor"}
                </button>
                <button
                  type="button"
                  className="btn btn--secondary"
                  disabled={nvBusy}
                  onClick={() => {
                    setNvOpen(false);
                    setNvName("");
                    setNvEmail("");
                    setNvContact("");
                  }}
                >
                  Cancel
                </button>
              </div>
            </>
          )}
        </section>

        <div className="jha__actions">
          <button
            className="btn btn--secondary"
            disabled={busy}
            onClick={() => {
              setEditingId(null);
              resetBuilder();
              setMsg(null);
              reload(statusFilter === "all" ? undefined : statusFilter);
            }}
          >
            Back to list
          </button>
          <button className="btn btn--secondary" disabled={busy} onClick={() => void onSave()}>
            {busy ? "Working…" : "Save draft"}
          </button>
          <button className="btn btn--primary" disabled={busy} onClick={() => void onGenerate()}>
            {busy ? "Working…" : "Generate RFQ"}
          </button>
        </div>
      </>
    );
  }

  // ── Tracker face ───────────────────────────────────────────────────────────────────────────────
  const STATUSES = Object.keys(rfq.RFQ_STATUS_LABEL) as rfq.RfqStatus[];
  return (
    <>
      <p className="dash__intro">
        Compose a price-free Request for Quote for up to {rfq.MAX_RFQ_VENDORS} vendors at once. Generate
        queues it for per-vendor rendering (an RFQ PDF plus a fillable quote form each); sending happens
        only after the operator approves each vendor's package, and vendor replies come back through
        Vendor Estimates.
      </p>

      {msg && <div className={`banner ${msg.ok ? "banner--ok" : "banner--err"}`}>{msg.text}</div>}

      {canManage && (
        <div className="jha__actions">
          <button
            className="btn btn--primary"
            disabled={busy}
            onClick={() => {
              resetBuilder();
              setEditingId(0);
              setMsg(null);
            }}
          >
            New RFQ
          </button>
        </div>
      )}

      <label className="field">
        <span className="field__label">Status</span>
        <select
          className="field__input"
          aria-label="Status filter"
          value={statusFilter}
          onChange={(e) => {
            const s = e.target.value as "all" | rfq.RfqStatus;
            setStatusFilter(s);
            reload(s === "all" ? undefined : s);
          }}
        >
          <option value="all">All</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {rfq.RFQ_STATUS_LABEL[s]}
            </option>
          ))}
        </select>
      </label>

      {loading && rows.length === 0 ? (
        <p className="muted">Loading…</p>
      ) : rows.length === 0 ? (
        <div className="dash-empty">No RFQs yet.</div>
      ) : (
        <div className="dash-grid">
          {rows.map((r) => (
            <section key={r.id} className="card">
              <div className="dash-card__head">
                <h3 className="dash-card__title">{r.rfq_number ?? `Draft (RFQ #${r.id})`}</h3>
                <span className={STATUS_PILL[r.status] ?? "dash-pill"}>
                  {rfq.RFQ_STATUS_LABEL[r.status] ?? r.status}
                </span>
              </div>
              <div className="dash-card__sub">
                {r.job_no} · {r.job_name || "—"}
                {r.due_date ? ` · quotes due ${r.due_date}` : ""}
              </div>
              {/* Per-vendor status badges — the fan-out truth the tracker exists to show. */}
              <div className="dash-card__row">
                {r.vendors.map((v) => (
                  <span key={v.vendor_key} className={VENDOR_PILL[v.status] ?? "dash-pill"}>
                    {vendorByKey.get(v.vendor_key)?.vendor_name ?? v.vendor_key}: {rfq.RFQ_VENDOR_STATUS_LABEL[v.status] ?? v.status}
                  </span>
                ))}
              </div>
              {canManage && (
                <div className="dash-card__row">
                  {r.status === "draft" && (
                    <button className="btn btn--primary" disabled={busy} onClick={() => openDraft(r.id)}>
                      Edit draft
                    </button>
                  )}
                  {(r.status === "draft" || r.status === "queued") && (
                    <button className="btn btn--secondary" disabled={busy} onClick={() => void onCancelRfq(r.id)}>
                      Cancel
                    </button>
                  )}
                </div>
              )}
            </section>
          ))}
        </div>
      )}
    </>
  );
}
