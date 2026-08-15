import { useState, useEffect, useCallback, useMemo } from "react";
import * as api from "../lib/po";
import {
  SUPPLY_CATEGORIES,
  REGIONS,
  categoryLabel,
  formatCents,
  parseDollarsToCents,
  parseDollarsToMicrocents,
  computeDisplayTotals,
} from "../lib/po";
import { fetchJobs, type Job } from "../lib/api";
import * as est from "../lib/estimates";
import { errorText } from "../lib/errorCopy";
import { useAuth } from "../lib/auth";

// PO workstream S6 — the builder + status tracker (Aug-7 delivery program WS1). One page, two
// faces: the TRACKER (every PO from GET /api/po/pos with per-status actions) and the BUILDER
// (a single-page wizard: job → vendor → line items → totals → scope/delivery/payment → terms →
// review → Generate). cap.po.manage gates the whole view (router VIEW_CAPS) AND every write
// affordance in-page; the Worker re-gates every call (Invariant 2 — SPA gating is convenience,
// never the boundary).
//
// FOLDED (2026-07): this renders as the "Purchase Orders" TAB PANEL inside PurchaseOrdersPage
// (which owns the PageShell + tab strip beside the RFQs / Vendor Estimates panels). The panel
// stays mounted across tab flips (hub `hidden` pattern), so wizard state survives a mid-build
// glance at the other tabs. Two cross-tab hooks: `onReviewEstimate` sends a picked reviewable
// estimate to the DISPOSITION screen (the ADR-0004 §3 fidelity gate — the ONLY estimate→PO
// path, deliberately not shortcut here), and `openDraftRequest` receives the freshly-imported
// draft back so it opens in the builder still fully editable (lines, attachments, terms).
//
// MONEY (D8): all values are integer cents. The page mirrors the Worker's integer math for the
// LIVE display (src/lib/po.ts computeDisplayTotals — the same rounding worker/po.ts signs), but
// the Worker is authoritative: the save response's `totals` replace the client mirror, generate
// sends exactly the DISPLAYED cents, and a `totals_mismatch` 409 re-renders the panel from the
// server's `recomputed` numbers — the client never argues with the Worker about money.
//
// SUPERSESSION (D7): a sent PO is superseded through POST /api/po/:id/supersede, which CLONES
// it into a fresh draft (supersede_seq+1) — the builder then opens that clone. The old PO stays
// in force until the successor is actually sent (the Worker flips it at status-sync). A
// `supersede_in_progress` 409 opens the existing successor instead of minting a sibling.
//
// AUTO-FILL SCOPE (S6 deviation now RESOLVED): job select fills the whole ship-to step from the
// routing SoR. /api/jobs (id + name) drives the dropdown; GET /api/po/jobs/:job_id/ship-to
// (worker/po.ts, session + cap.po.manage — the cap the whole page already holds) returns the
// same routing row the internal-token tier serves (jobs.address + stakeholder_*), so the builder
// auto-fills job name/number, the ship-to name + address line, and the delivery contact WITHOUT
// the internal token. The routing SoR carries a single free-text `address` line (no structured
// city/state/zip — those live only on purchase_orders), so those three ride back empty and stay
// manual. Auto-fill is a CONVENIENCE: every field is editable, and a 404 / read error silently
// leaves the field blank (never blocks the wizard).
//
// MATERIAL CATALOG PICK (line items): each line row carries a "pick from catalog" <select> fed
// by GET /api/po/materials (cap.po.manage — a thin read of the SAME material_catalog TYPE table
// the field-ops Materials Catalog admin manages, migration 0019). material_catalog is a TYPE
// vocabulary (manufacturer / model / specs) with NO price, so a pick populates only the line's
// IDENTITY — part_number ← model_id, description ← manufacturer + model_id + key_specs
// (api.catalogLineFields) — while qty/unit/unit_cost stay operator-entered per PO (prices drift,
// quantities vary). Free-form typing over every field remains the fallback (many lines — steel,
// crane, shipping — aren't equipment types).

const JOB_NO_RE = /^\d{4}\.\d{3}$/;
const QTY_RE = /^\d+(\.\d{0,3})?$/; // the Worker normalizes qty to ≤3dp
const INT_RE = /^\d+$/;
const STATE_RE = /^[A-Z]{2}$/;

const STATUS_LABEL: Record<api.PoStatus, string> = {
  draft: "Draft",
  queued: "Queued for render",
  pending_review: "Pending review",
  approved: "Approved",
  sent: "Sent",
  superseded: "Superseded",
  canceled: "Canceled",
};
const STATUS_PILL: Record<api.PoStatus, string> = {
  draft: "dash-pill",
  queued: "dash-pill dash-pill--warn",
  pending_review: "dash-pill dash-pill--warn",
  approved: "dash-pill dash-pill--ok",
  sent: "dash-pill dash-pill--ok",
  superseded: "dash-pill",
  canceled: "dash-pill dash-pill--danger",
};

const VARIANTS: [api.LineColumnVariant, string][] = [
  ["default", "Standard"],
  ["lump_sum", "Lump sum"],
  ["per_watt", "Per watt"],
];

const TAX_MODES: [api.TaxMode, string][] = [
  ["auto", "Automatic (by ship-to state)"],
  ["exempt", "Exempt"],
  ["included", "Included in line prices"],
  ["override", "Override rate"],
];

/** One editable line row — everything a string (controlled inputs); parsed per keystroke for
 *  the live extended/subtotal mirror. */
interface LineForm {
  part_number: string;
  description: string;
  qty: string;
  unit: string;
  unit_cost: string; // dollars
  watts: string;
  panels: string;
  pallets: string;
  ppw: string; // dollars per watt
}
const EMPTY_LINE: LineForm = {
  part_number: "",
  description: "",
  qty: "",
  unit: "",
  unit_cost: "",
  watts: "",
  panels: "",
  pallets: "",
  ppw: "",
};

/** Parse one row per the active variant. Returns the wire line, or null while the row is
 *  incomplete/invalid (the grid shows "—" for its extended and Generate blocks). */
function parseLine(l: LineForm, variant: api.LineColumnVariant): api.DraftLine | null {
  const description = l.description.trim();
  if (!description) return null;
  if (variant === "per_watt") {
    if (!INT_RE.test(l.watts.trim())) return null;
    const ppw = parseDollarsToMicrocents(l.ppw);
    if (ppw === null || l.ppw.trim() === "") return null;
    const panels = l.panels.trim() === "" ? null : INT_RE.test(l.panels.trim()) ? parseInt(l.panels, 10) : undefined;
    const pallets = l.pallets.trim() === "" ? null : INT_RE.test(l.pallets.trim()) ? parseInt(l.pallets, 10) : undefined;
    if (panels === undefined || pallets === undefined) return null;
    return {
      part_number: l.part_number.trim(),
      description,
      qty: 1,
      unit: l.unit.trim(),
      watts: parseInt(l.watts, 10),
      panels,
      pallets,
      price_per_watt_microcents: ppw,
    };
  }
  const cost = parseDollarsToCents(l.unit_cost);
  if (cost === null || l.unit_cost.trim() === "") return null;
  if (variant === "lump_sum") {
    return { description, qty: 1, unit: l.unit.trim(), unit_cost_cents: cost };
  }
  if (!QTY_RE.test(l.qty.trim())) return null;
  return {
    part_number: l.part_number.trim(),
    description,
    qty: Number(l.qty),
    unit: l.unit.trim(),
    unit_cost_cents: cost,
  };
}

/** Integer cents → a plain editable input value ("1234.56"). */
function centsToInput(c: number | null): string {
  if (c === null || c === undefined) return "";
  return `${Math.floor(c / 100)}.${String(c % 100).padStart(2, "0")}`;
}
/** Microcents/W → dollars-per-watt input ("0.35"), trailing zeros trimmed. */
function microToInput(m: number | null): string {
  if (m === null || m === undefined) return "";
  const whole = Math.floor(m / 100_000_000);
  const frac = String(m % 100_000_000).padStart(8, "0").replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : String(whole);
}
/** Basis points → percent input ("9.00"). Same digits relationship as cents→dollars. */
const bpToInput = centsToInput;

function fmtDate(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}

function FieldInput({
  label,
  value,
  onChange,
  maxLength,
  listId,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  maxLength?: number;
  /** Optional <datalist> id — turns the input into a suggest-or-free-text combobox. */
  listId?: string;
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      <input
        className="field__input"
        value={value}
        maxLength={maxLength}
        list={listId}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

export function PoBuilderPage({
  onReviewEstimate,
  onOpenEstimatesTab,
  openDraftRequest,
}: {
  /** Open the disposition screen for a reviewable estimate (hub flips to the Estimates tab). */
  onReviewEstimate: (estimateId: number) => void;
  /** Jump to the Vendor Estimates tab (the "none ready — upload one" affordance). */
  onOpenEstimatesTab: () => void;
  /** Nonce-keyed one-shot from the hub: open this draft in the builder. `origin` picks the
   *  copy — the channel serves BOTH estimate imports and job-screen change-order handoffs. */
  openDraftRequest?: { id: number; nonce: number; origin?: "estimate" | "change_order" } | null;
}) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.po.manage"); // UI affordance only — the Worker re-gates

  // ── Shared data ────────────────────────────────────────────────────────────────────────────────
  const [pos, setPos] = useState<api.PoListRow[]>([]);
  const [vendors, setVendors] = useState<api.Vendor[]>([]);
  const [terms, setTerms] = useState<api.TermsProfile[]>([]);
  const [config, setConfig] = useState<api.PoConfig | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  // The material_catalog TYPE vocabulary (GET /api/po/materials) — the per-line "pick from
  // catalog" feed. A pick populates a line's part_number + description; free-form entry stays
  // the fallback (many PO lines — steel, crane, shipping — aren't equipment types).
  const [catalog, setCatalog] = useState<api.CatalogMaterial[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const reloadPos = useCallback((status?: api.PoStatus) => {
    setLoading(true);
    api
      .fetchPos(status)
      .then(setPos)
      .catch(() => setError("Failed to load purchase orders."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reloadPos();
    api.fetchVendors().then(setVendors).catch(() => setError("Failed to load the vendor list."));
    api.fetchTerms().then(setTerms).catch(() => setTerms([]));
    api.fetchPoConfig().then(setConfig).catch(() => setConfig(null));
    fetchJobs().then(setJobs).catch(() => setJobs([]));
    api.fetchPoMaterials().then(setCatalog).catch(() => setCatalog([]));
  }, [reloadPos]);

  const vendorByKey = useMemo(() => new Map(vendors.map((v) => [v.vendor_key, v])), [vendors]);

  // ── Tracker state ──────────────────────────────────────────────────────────────────────────────
  const [view, setView] = useState<"tracker" | "builder">("tracker");
  const [statusFilter, setStatusFilter] = useState<"all" | api.PoStatus>("all");
  const [armedCancelId, setArmedCancelId] = useState<number | null>(null);
  const [armedDeleteId, setArmedDeleteId] = useState<number | null>(null);

  // ── "New PO from a vendor estimate" picker ─────────────────────────────────────────────────────
  // On-demand list of reviewable estimates (extracted / needs_review). Picking one routes to the
  // DISPOSITION screen (onReviewEstimate) — the human side-by-side accept is the only fidelity
  // control (ADR-0004 decision 3), so this picker never imports lines itself. Refetched on every
  // open (an import over on the Estimates tab goes stale otherwise); null = not loaded yet.
  const [fromEstOpen, setFromEstOpen] = useState(false);
  const [fromEstRows, setFromEstRows] = useState<est.EstimateRow[] | null>(null);

  async function onOpenFromEstimate() {
    setFromEstOpen(true);
    setFromEstRows(null);
    try {
      const all = await est.fetchEstimates();
      setFromEstRows(all.filter((r) => r.status === "extracted" || r.status === "needs_review"));
    } catch {
      setFromEstOpen(false);
      setMsg({ ok: false, text: "Could not load the vendor-estimates list." });
    }
  }

  // ── Builder form state ─────────────────────────────────────────────────────────────────────────
  const [draftId, setDraftId] = useState<number | null>(null);
  const [supersedesPoId, setSupersedesPoId] = useState<number | null>(null);
  // Track D2: set when the open draft IS a change order — parent id + this CO's sequence.
  const [changeOrderOf, setChangeOrderOf] = useState<{ id: number; seq: number | null } | null>(null);
  const [jobId, setJobId] = useState("");
  const [jobName, setJobName] = useState("");
  const [jobNo, setJobNo] = useState("");
  const [sitePhase, setSitePhase] = useState("0");
  const [shipToName, setShipToName] = useState("");
  const [shipToAddress, setShipToAddress] = useState("");
  const [shipToCity, setShipToCity] = useState("");
  const [shipToState, setShipToState] = useState("");
  const [shipToZip, setShipToZip] = useState("");
  const [deliveryName, setDeliveryName] = useState("");
  const [deliveryPhone, setDeliveryPhone] = useState("");
  const [deliveryEmail, setDeliveryEmail] = useState("");
  const [vendorKey, setVendorKey] = useState("");
  const [regionFilter, setRegionFilter] = useState<string | null>(null);
  const [categoryFilter, setCategoryFilter] = useState<string | null>(null);
  const [variant, setVariant] = useState<api.LineColumnVariant>("default");
  const [lines, setLines] = useState<LineForm[]>([{ ...EMPTY_LINE }]);
  const [taxMode, setTaxMode] = useState<api.TaxMode>("auto");
  const [taxOverridePct, setTaxOverridePct] = useState("");
  const [shipping, setShipping] = useState("");
  const [sow, setSow] = useState("");
  const [deliveryInstructions, setDeliveryInstructions] = useState("");
  const [paymentTerms, setPaymentTerms] = useState("");
  const [termsProfileId, setTermsProfileId] = useState("");
  const [approverName, setApproverName] = useState("");
  const [approverTitle, setApproverTitle] = useState("");
  /** The Worker's totals for the CURRENT saved snapshot (save response / generate 409
   *  `recomputed`). Displayed over the client mirror when present; any money-affecting edit
   *  clears it back to the live mirror. */
  const [serverTotals, setServerTotals] = useState<api.PoTotals | null>(null);
  // ── Attachments (Feature B): specs/drawings pooled on the DRAFT, §34-screened on the
  //    Mac before Box — the list here is metadata only (bytes never come back to the SPA).
  const [attachments, setAttachments] = useState<api.PoAttachment[]>([]);
  const [attBusy, setAttBusy] = useState(false);

  function resetForm() {
    setDraftId(null);
    setSupersedesPoId(null);
    setChangeOrderOf(null);
    setJobId("");
    setJobName("");
    setJobNo("");
    setSitePhase("0");
    setShipToName("");
    setShipToAddress("");
    setShipToCity("");
    setShipToState("");
    setShipToZip("");
    setDeliveryName("");
    setDeliveryPhone("");
    setDeliveryEmail("");
    setVendorKey("");
    setRegionFilter(null);
    setCategoryFilter(null);
    setVariant("default");
    setLines([{ ...EMPTY_LINE }]);
    setTaxMode("auto");
    setTaxOverridePct("");
    setShipping("");
    setSow("");
    setDeliveryInstructions("");
    setPaymentTerms(seedPaymentTerms(config));
    setTermsProfileId("");
    setApproverName("");
    setApproverTitle("");
    setServerTotals(null);
    setAttachments([]);
  }

  /** Seed the payment-terms textarea with the invoice-routing line from the versioned
   *  purchaser config (D5) — the admin adds the commercial terms wording around it. */
  function seedPaymentTerms(cfg: api.PoConfig | null): string {
    if (!cfg) return "";
    const { to, cc } = cfg.purchaser.invoice_routing;
    return `Send invoices to ${to}${cc.length ? ` (cc: ${cc.join(", ")})` : ""}.`;
  }

  // ── Live money mirror (display only — the Worker recomputes and is authoritative) ────────────
  const parsedLines = useMemo(() => lines.map((l) => parseLine(l, variant)), [lines, variant]);
  const validLines = useMemo(() => parsedLines.filter((l): l is api.DraftLine => l !== null), [parsedLines]);
  const taxOverrideBp = taxMode === "override" ? parseDollarsToCents(taxOverridePct) : 0; // %→bp shares the ×100 digit shift
  const shippingCents = shipping.trim() === "" ? 0 : parseDollarsToCents(shipping);
  const ratesBp = config?.tax.rates_bp ?? {};
  const stateNames = config?.tax.state_names ?? {};
  const stateUpper = shipToState.trim().toUpperCase();

  const clientTotals = useMemo(() => {
    if (shippingCents === null || (taxMode === "override" && (taxOverrideBp === null || taxOverrideBp > 10_000))) return null;
    return computeDisplayTotals(
      validLines.map((l) => ({
        qty: l.qty,
        unit_cost_cents: l.unit_cost_cents ?? null,
        watts: l.watts ?? null,
        price_per_watt_microcents: l.price_per_watt_microcents ?? null,
      })),
      taxMode,
      taxOverrideBp ?? 0,
      shippingCents,
      stateUpper,
      ratesBp,
    );
  }, [validLines, taxMode, taxOverrideBp, shippingCents, stateUpper, ratesBp]);

  const displayTotals = serverTotals ?? clientTotals;

  /** Any money-affecting edit invalidates the saved server totals — back to the live mirror. */
  const touchMoney = () => setServerTotals(null);

  // ── Delivery-contact suggestions (Feature C) ───────────────────────────────────────────────────
  // The configured list from the served PO config (GET /api/po/config → delivery_contacts, the
  // §50-edited po_materials/config/delivery_contacts.json bundle) feeds a <datalist> on the
  // delivery-contact NAME input. SUGGESTIONS ONLY: free text is always accepted, the field stays
  // optional, and the job-stakeholder auto-fill below is untouched (this fill only fires on an
  // exact typed/picked name match — additive, never a replacement).
  const deliveryContacts = config?.delivery_contacts ?? [];

  /** Name edits flow through here: on a case-INSENSITIVE match with a configured contact, fill its
   *  phone + email (non-empty values only — an empty configured field never wipes an operator-entered
   *  or stakeholder-auto-filled value). Free text simply sets the name. The match is case-insensitive
   *  to align with the config editor's server-side dedupe (config_apply._apply_delivery_contacts_edit
   *  lowercases for its uniqueness check), so a saved "Pat Yardman" still autofills on a typed
   *  "pat yardman" — the list can't hold two entries that differ only by case. */
  function onDeliveryNameChange(v: string) {
    setDeliveryName(v);
    const key = v.trim().toLowerCase();
    const match = deliveryContacts.find((ct) => ct.name.trim().toLowerCase() === key);
    if (!match) return;
    if (match.phone) setDeliveryPhone(match.phone);
    if (match.email) setDeliveryEmail(match.email);
  }

  // ── Job select + auto-fill ─────────────────────────────────────────────────────────────────────
  async function onJobSelect(id: string) {
    setJobId(id);
    const job = jobs.find((j) => j.job_id === id);
    if (!job) return;
    // Immediate fills from the /api/jobs dropdown row (always present).
    setJobName(job.project_name);
    // The STORED Evergreen number (0057) first; the name-prefix parse stays the fallback for
    // jobs that predate the structured field. Editable either way.
    //
    // 0064 — the identifier's THIRD segment is the Site/phase, so it auto-fills alongside the
    // number rather than being re-derived by hand: picking MH405 (2026.384.1) fills job_no
    // 2026.384 + Site/phase 1, and the document number composes as 2026.384.1.<supersede>.<rev>.
    // The fallback regex captures that segment too and is anchored at the END OF THE NUMBER
    // (`(?![\d.])`, not `$` — the name continues after it). Previously it was open at the tail,
    // so "2026.384.1 Coker Solar" silently matched and filled the TRUNCATED "2026.384" — a
    // wrong-but-plausible number belonging to a different site of the same project.
    const m = /^(\d{4}\.\d{3})(?:\.(\d+))?(?![\d.])/.exec(job.project_name.trim());
    const storedJobNo = job.job_no || "";
    // Both parts from the SAME source — never a stored number paired with a name-parsed site.
    setJobNo(storedJobNo || (m ? m[1] : ""));
    const sitePhaseFill = storedJobNo ? job.site_phase : m && m[2] !== undefined ? parseInt(m[2], 10) : 0;
    setSitePhase(String(sitePhaseFill ?? 0));
    setShipToName(job.project_name);
    // Full ship-to + delivery auto-fill from the routing SoR (session + cap.po.manage — see the
    // AUTO-FILL SCOPE note atop this file). Convenience only: every field stays editable, and a
    // 404 / read error silently leaves the fields blank — never blocks the wizard.
    try {
      const s = await api.fetchJobShipTo(id);
      if (s.ship_to_name) setShipToName(s.ship_to_name);
      if (s.ship_to_address) setShipToAddress(s.ship_to_address);
      if (s.ship_to_city) setShipToCity(s.ship_to_city);
      if (s.ship_to_state) {
        setShipToState(s.ship_to_state.toUpperCase());
        touchMoney(); // the ship-to state is the 'auto' tax basis — invalidate saved totals
      }
      if (s.ship_to_zip) setShipToZip(s.ship_to_zip);
      if (s.delivery_contact_name) setDeliveryName(s.delivery_contact_name);
      if (s.delivery_contact_phone) setDeliveryPhone(s.delivery_contact_phone);
      if (s.delivery_contact_email) setDeliveryEmail(s.delivery_contact_email);
    } catch {
      /* auto-fill only — never blocks the wizard */
    }
  }

  // ── Vendor picker ──────────────────────────────────────────────────────────────────────────────
  const activeVendors = useMemo(() => vendors.filter((v) => v.active === 1), [vendors]);
  const filteredVendors = useMemo(
    () =>
      activeVendors.filter(
        (v) =>
          (regionFilter === null || v.region === regionFilter) &&
          (categoryFilter === null || v.supply_categories.includes(categoryFilter)),
      ),
    [activeVendors, regionFilter, categoryFilter],
  );
  const selectedVendor = vendorByKey.get(vendorKey) ?? null;

  function onVendorSelect(v: api.Vendor) {
    setVendorKey(v.vendor_key);
    // The vendor's default terms profile is PRESELECTED (D6); the picker below can override.
    if (v.default_terms_profile) setTermsProfileId(v.default_terms_profile);
  }

  const selectedTerms = terms.find((t) => t.id === termsProfileId) ?? null;

  // ── Line grid ──────────────────────────────────────────────────────────────────────────────────
  const setLine = (i: number, patch: Partial<LineForm>) => {
    touchMoney();
    setLines((prev) => prev.map((l, idx) => (idx === i ? { ...l, ...patch } : l)));
  };
  const addLine = () => {
    touchMoney();
    setLines((prev) => [...prev, { ...EMPTY_LINE }]);
  };
  const removeLine = (i: number) => {
    touchMoney();
    setLines((prev) => (prev.length > 1 ? prev.filter((_, idx) => idx !== i) : prev));
  };
  /** Pick from the material_catalog TYPE vocabulary: populate line `i`'s part_number +
   *  description from the chosen type (api.catalogLineFields). qty/unit/unit_cost are left
   *  untouched — the catalog carries no price, and free-form typing over these fields stays the
   *  fallback. */
  const applyCatalog = (i: number, id: number) => {
    const m = catalog.find((x) => x.id === id);
    if (m) setLine(i, api.catalogLineFields(m));
  };

  // ── Tax badge ──────────────────────────────────────────────────────────────────────────────────
  let taxBadge: { cls: string; text: string } | null = null;
  if (taxMode === "auto") {
    if (!stateUpper) {
      taxBadge = { cls: "dash-pill", text: "Enter the ship-to state for automatic tax" };
    } else if (ratesBp[stateUpper] !== undefined) {
      const bp = ratesBp[stateUpper];
      taxBadge = {
        cls: "dash-pill dash-pill--ok",
        text: `${stateNames[stateUpper] ?? stateUpper} — ${bpToInput(bp)}% sales tax`,
      };
    } else {
      taxBadge = {
        cls: "dash-pill dash-pill--warn",
        text: `No tax-table entry for ${stateUpper} — automatic tax will be refused (use exempt or override)`,
      };
    }
  }

  // ── Draft save / generate / open ───────────────────────────────────────────────────────────────
  function validate(): string | null {
    if (!vendorKey) return "Pick a vendor.";
    if (!JOB_NO_RE.test(jobNo.trim())) return "The job number must look like 2023.126 (year.number).";
    if (!INT_RE.test(sitePhase.trim()) || parseInt(sitePhase, 10) > 9999) {
      return "The site/phase must be a whole number from 0 to 9999.";
    }
    if (parsedLines.some((l) => l === null)) return "Every line needs a description and valid amounts.";
    if (validLines.length === 0) return "Add at least one line item.";
    if (taxMode === "auto") {
      if (!STATE_RE.test(stateUpper)) return "Enter the 2-letter ship-to state (required for automatic tax).";
      if (ratesBp[stateUpper] === undefined) {
        return `There's no tax-table entry for ${stateUpper} — use the exempt or override tax mode.`;
      }
    }
    if (taxMode === "override" && (taxOverrideBp === null || taxOverrideBp > 10_000)) {
      return "The tax override must be a percentage from 0 to 100.";
    }
    if (shippingCents === null) return "The shipping amount isn't valid.";
    // Render-required: po_generate resolve_terms(get_profile) fences PERMANENTLY on a blank terms profile
    // (reachable whenever the vendor has no default terms profile). Flag it here, not silently at render.
    if (!termsProfileId) return "Pick a terms profile.";
    return null;
  }

  function buildBody(): api.DraftBody {
    const body: api.DraftBody = {
      vendor_key: vendorKey,
      job_no: jobNo.trim(),
      site_phase: parseInt(sitePhase, 10),
      job_id: jobId,
      job_name: jobName.trim(),
      ship_to_name: shipToName.trim(),
      ship_to_address: shipToAddress.trim(),
      ship_to_city: shipToCity.trim(),
      ship_to_state: stateUpper,
      ship_to_zip: shipToZip.trim(),
      delivery_contact_name: deliveryName.trim(),
      delivery_contact_phone: deliveryPhone.trim(),
      delivery_contact_email: deliveryEmail.trim(),
      sow_text: sow,
      delivery_instructions: deliveryInstructions,
      payment_terms_text: paymentTerms,
      terms_profile_id: termsProfileId,
      terms_version: selectedTerms?.current_version ?? "",
      tax_mode: taxMode,
      shipping_cents: shippingCents ?? 0,
      line_column_variant: variant,
      approver_name: approverName.trim(),
      approver_title: approverTitle.trim(),
      line_items: validLines,
    };
    if (taxMode === "override") body.tax_rate_bp = taxOverrideBp ?? 0;
    return body;
  }

  /** Persist the draft (create or full-replace update). Returns the saved id + the WORKER's
   *  totals — the numbers the review panel then displays and generate asserts against. */
  async function saveDraft(): Promise<{ id: number; totals: api.PoTotals } | null> {
    const problem = validate();
    if (problem) {
      setMsg({ ok: false, text: problem });
      return null;
    }
    const body = buildBody();
    try {
      const res = draftId === null ? await api.createDraft(body) : await api.updateDraft(draftId, body);
      setDraftId(res.id);
      setServerTotals(res.totals);
      return res;
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Save failed." });
      return null;
    }
  }

  async function onSaveDraft() {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    const saved = await saveDraft();
    if (saved) {
      setMsg({ ok: true, text: `Draft saved (#${saved.id}) — totals confirmed by the server.` });
      reloadPos(statusFilter === "all" ? undefined : statusFilter);
    }
    setBusy(false);
  }

  async function onGenerate() {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    // Save first so the stored draft is exactly what's on screen, then generate against the
    // Worker's own totals for that snapshot — the anti-skew assert (D8) sees the displayed cents.
    const saved = await saveDraft();
    if (!saved) {
      setBusy(false);
      return;
    }
    try {
      const r = await api.generateDraft(saved.id, saved.totals);
      if (r.ok) {
        setMsg({ ok: true, text: `PO ${r.po_number} generated — queued for rendering and review.` });
        resetForm();
        setView("tracker");
        reloadPos(statusFilter === "all" ? undefined : statusFilter);
      } else if (r.error === "totals_mismatch") {
        // The Worker's recomputed money is authoritative — re-render the panel from it.
        setServerTotals(r.recomputed);
        setMsg({ ok: false, text: errorText("totals_mismatch") });
      } else {
        setMsg({ ok: false, text: errorText(r.error) });
      }
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Generate failed." });
    }
    setBusy(false);
  }

  /** Open an existing draft in the builder (tracker "Open", supersede clones, estimate
   *  imports). Returns whether the open succeeded (callers layering a success banner). */
  async function openDraft(id: number): Promise<boolean> {
    setBusy(true);
    setMsg(null);
    try {
      const { po, line_items } = await api.fetchPo(id);
      setDraftId(po.id);
      setSupersedesPoId(po.supersedes_po_id);
      setChangeOrderOf(po.change_order_of !== null ? { id: po.change_order_of, seq: po.co_seq } : null);
      setJobId(po.job_id);
      setJobName(po.job_name);
      setJobNo(po.job_no);
      setSitePhase(String(po.site_phase));
      setShipToName(po.ship_to_name);
      setShipToAddress(po.ship_to_address);
      setShipToCity(po.ship_to_city);
      setShipToState(po.ship_to_state);
      setShipToZip(po.ship_to_zip);
      setDeliveryName(po.delivery_contact_name);
      setDeliveryPhone(po.delivery_contact_phone);
      setDeliveryEmail(po.delivery_contact_email);
      setVendorKey(po.vendor_key);
      setVariant((po.line_column_variant as api.LineColumnVariant) || "default");
      setTaxMode((po.tax_mode as api.TaxMode) || "auto");
      setTaxOverridePct(po.tax_mode === "override" ? bpToInput(po.tax_rate_bp) : "");
      setShipping(po.shipping_cents ? centsToInput(po.shipping_cents) : "");
      setSow(po.sow_text);
      setDeliveryInstructions(po.delivery_instructions);
      setPaymentTerms(po.payment_terms_text);
      setTermsProfileId(po.terms_profile_id);
      setApproverName(po.approver_name);
      setApproverTitle(po.approver_title);
      setLines(
        line_items.length === 0
          ? [{ ...EMPTY_LINE }]
          : line_items.map((l) => ({
              part_number: l.part_number,
              description: l.description,
              qty: String(l.qty),
              unit: l.unit,
              unit_cost: centsToInput(l.unit_cost_cents),
              watts: l.watts === null ? "" : String(l.watts),
              panels: l.panels === null ? "" : String(l.panels),
              pallets: l.pallets === null ? "" : String(l.pallets),
              ppw: microToInput(l.price_per_watt_microcents),
            })),
      );
      setServerTotals({
        subtotal_cents: po.subtotal_cents,
        tax_rate_bp: po.tax_rate_bp,
        tax_cents: po.tax_cents,
        total_cents: po.total_cents,
      });
      // Attachment list is a best-effort convenience read — never blocks the open.
      api.fetchPoAttachments(po.id).then(setAttachments).catch(() => setAttachments([]));
      setView("builder");
      setBusy(false);
      return true;
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Could not open the draft." });
      setBusy(false);
      return false;
    }
  }

  // Cross-tab one-shot: a disposition import over on the Estimates tab minted a draft PO and
  // the hub handed it back here — open it in the builder, still fully editable, and refresh
  // the tracker so the new draft row is visible when the user backs out. Two guards from the
  // 2026-07-20 adversarial review: (a) the picker that started the round-trip closes (its
  // list is stale the moment the import lands); (b) if the builder face is mid-edit on
  // ANOTHER PO, opening the import would silently wipe that unsaved work — confirm first
  // (the R3 discard-guard precedent), and on decline leave the import findable in the
  // tracker, never silently dropped.
  useEffect(() => {
    if (!openDraftRequest) return;
    const { id } = openDraftRequest;
    // The channel serves two producers with different copy: an estimate disposition minted
    // this draft, or the job Procurement screen created a change order from a sent PO.
    const co = openDraftRequest.origin === "change_order";
    const noun = co ? "change-order draft" : "imported draft";
    setFromEstOpen(false);
    setFromEstRows(null);
    void (async () => {
      if (
        view === "builder" &&
        draftId !== id && // re-opening the SAME draft loses nothing — don't threaten it
        !window.confirm(
          `Open ${noun} #${id} now? The purchase order you were editing will be replaced (unsaved changes are lost).`,
        )
      ) {
        setMsg({
          ok: true,
          text: co
            ? `Change-order draft #${id} is in the list — open it when you're ready.`
            : `Estimate imported into draft #${id} — open it from the list when you're ready.`,
        });
        reloadPos(statusFilter === "all" ? undefined : statusFilter);
        return;
      }
      const ok = await openDraft(id);
      if (ok) {
        setMsg({
          ok: true,
          text: co
            ? `Change-order draft #${id} opened — edit what changed, then Generate.`
            : `Estimate imported into draft #${id} — adjust lines, attachments, and terms below, then Generate.`,
        });
      }
      reloadPos(statusFilter === "all" ? undefined : statusFilter);
    })();
    // Deliberately keyed on the request nonce alone: openDraft/reloadPos are stable-enough
    // in-component closures, and re-running on their identity would replay the open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openDraftRequest]);

  /** Supersede a SENT PO: the Worker clones it into a new draft, which opens in the builder.
   *  A successor already in flight (409) opens instead — never a sibling. */
  async function onSupersede(id: number) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.supersedePo(id);
      if (r.ok) {
        setMsg({ ok: true, text: "A replacement draft was created from the sent PO — review and generate it." });
        await openDraft(r.id);
      } else {
        setMsg({ ok: false, text: errorText("supersede_in_progress") });
        await openDraft(r.existing_id);
      }
      reloadPos(statusFilter === "all" ? undefined : statusFilter);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Supersede failed." });
    }
    setBusy(false);
  }

  /** Create a CHANGE-ORDER draft from a sent PO (Track D2): the Worker clones the parent's
   *  full configuration + lines; the office edits what changed and generates — the parent
   *  stays in force, and the CO's number ({parent}-CO{n}) is minted at generate. */
  async function onChangeOrder(id: number) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.createPoChangeOrder(id);
      setMsg({
        ok: true,
        text: `Change order CO${r.co_seq ?? "?"} drafted from the sent PO — edit what changed, then Generate.`,
      });
      await openDraft(r.id);
      reloadPos(statusFilter === "all" ? undefined : statusFilter);
    } catch (err) {
      // ApiError.message is already human copy (errorText in its constructor).
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Change order failed." });
    }
    setBusy(false);
  }

  async function onCancel(id: number) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    setArmedCancelId(null);
    try {
      await api.cancelPo(id);
      if (draftId === id) resetForm();
      setMsg({ ok: true, text: "Purchase order canceled." });
      reloadPos(statusFilter === "all" ? undefined : statusFilter);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Cancel failed." });
    }
    setBusy(false);
  }

  async function onDelete(id: number) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    setArmedDeleteId(null);
    try {
      await api.deletePoDraft(id);
      if (draftId === id) resetForm();
      setMsg({ ok: true, text: "Draft deleted." });
      reloadPos(statusFilter === "all" ? undefined : statusFilter);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Delete failed." });
    }
    setBusy(false);
  }

  // ── Attachments (Feature B) ────────────────────────────────────────────────────────────────────
  /** File → base64 (no data: prefix — the wire contract the Worker validates). */
  function fileToB64(f: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const s = String(reader.result ?? "");
        const comma = s.indexOf(",");
        resolve(comma >= 0 ? s.slice(comma + 1) : s);
      };
      reader.onerror = () => reject(new Error("Could not read the file."));
      reader.readAsDataURL(f);
    });
  }

  /** Upload the picked files onto the SAVED draft. Client-side size/type checks are
   *  HINTS only — the Worker re-gates every upload (size, count, filename, MIME
   *  allowlist + magic sniff), and the real §34 screen runs on the Mac before Box. */
  async function onAttachFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0 || draftId === null || attBusy) return;
    // Snapshot the File references SYNCHRONOUSLY — the change handler resets the
    // input's value right after this call, which empties the live FileList.
    const files = Array.from(fileList);
    setAttBusy(true);
    setMsg(null);
    let count = attachments.length;
    for (const f of files) {
      const dot = f.name.lastIndexOf(".");
      const ext = dot >= 0 ? f.name.slice(dot).toLowerCase() : "";
      const mime = api.ATTACHMENT_MIME_BY_EXT[ext];
      if (!mime) {
        setMsg({ ok: false, text: `"${f.name}" isn't an allowed type — PDF, JPG, PNG, DOCX, or XLSX.` });
        continue;
      }
      if (f.size > api.ATTACHMENT_MAX_BYTES) {
        setMsg({ ok: false, text: `"${f.name}" is over the 10 MB per-file limit.` });
        continue;
      }
      if (count >= api.MAX_ATTACHMENTS_PER_PO) {
        setMsg({ ok: false, text: `A purchase order can carry at most ${api.MAX_ATTACHMENTS_PER_PO} attachments.` });
        break;
      }
      try {
        const b64 = await fileToB64(f);
        await api.uploadPoAttachment(draftId, f.name, mime, b64);
        count += 1;
      } catch (err) {
        setMsg({ ok: false, text: err instanceof Error ? err.message : `Upload of "${f.name}" failed.` });
      }
    }
    try {
      setAttachments(await api.fetchPoAttachments(draftId));
    } catch {
      /* list refresh is best-effort */
    }
    setAttBusy(false);
  }

  async function onRemoveAttachment(attachmentId: number) {
    if (draftId === null || attBusy) return;
    setAttBusy(true);
    setMsg(null);
    try {
      await api.deletePoAttachment(draftId, attachmentId);
      setAttachments(await api.fetchPoAttachments(draftId));
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Could not remove the attachment." });
    }
    setAttBusy(false);
  }

  function formatBytes(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} MB`;
    if (n >= 1_000) return `${Math.round(n / 1_000)} KB`;
    return `${n} B`;
  }

  const ATTACHMENT_STATUS_LABEL: Record<api.PoAttachment["status"], string> = {
    pending: "Awaiting screening",
    claimed: "Screening…",
    filed: "Filed ✓",
    refused: "Refused — see review",
  };

  // ── Render: line grid columns per variant ──────────────────────────────────────────────────────
  function lineHeaders() {
    if (variant === "per_watt") {
      return (
        <tr>
          <th>#</th>
          <th>Part #</th>
          <th>Description</th>
          <th>Watts</th>
          <th>Panels</th>
          <th>Pallets</th>
          <th>$/W</th>
          <th>Extended</th>
          <th />
        </tr>
      );
    }
    if (variant === "lump_sum") {
      return (
        <tr>
          <th>#</th>
          <th>Description</th>
          <th>Amount</th>
          <th>Extended</th>
          <th />
        </tr>
      );
    }
    return (
      <tr>
        <th>#</th>
        <th>Part #</th>
        <th>Description</th>
        <th>Qty</th>
        <th>Unit</th>
        <th>Unit cost</th>
        <th>Extended</th>
        <th />
      </tr>
    );
  }

  function lineRow(l: LineForm, i: number) {
    const parsed = parsedLines[i];
    const extended =
      parsed !== null
        ? formatCents(
            api.lineExtendedCents({
              qty: parsed.qty,
              unit_cost_cents: parsed.unit_cost_cents ?? null,
              watts: parsed.watts ?? null,
              price_per_watt_microcents: parsed.price_per_watt_microcents ?? null,
            }),
          )
        : "—";
    const cell = (field: keyof LineForm, label: string, width?: number) => (
      <input
        className="field__input"
        aria-label={`Line ${i + 1} ${label}`}
        value={l[field]}
        style={width ? { width } : undefined}
        onChange={(e) => setLine(i, { [field]: e.target.value })}
      />
    );
    return (
      <tr key={i}>
        <td>{i + 1}</td>
        {variant !== "lump_sum" ? <td>{cell("part_number", "part number", 110)}</td> : null}
        <td>
          {catalog.length > 0 ? (
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
          ) : null}
          {cell("description", "description")}
        </td>
        {variant === "per_watt" ? (
          <>
            <td>{cell("watts", "watts", 90)}</td>
            <td>{cell("panels", "panels", 80)}</td>
            <td>{cell("pallets", "pallets", 80)}</td>
            <td>{cell("ppw", "price per watt", 90)}</td>
          </>
        ) : variant === "lump_sum" ? (
          <td>{cell("unit_cost", "amount", 110)}</td>
        ) : (
          <>
            <td>{cell("qty", "quantity", 80)}</td>
            <td>{cell("unit", "unit", 70)}</td>
            <td>{cell("unit_cost", "unit cost", 110)}</td>
          </>
        )}
        <td className="dash-table__name">{extended}</td>
        <td>
          <button
            type="button"
            className="btn btn--secondary"
            aria-label={`Remove line ${i + 1}`}
            disabled={lines.length === 1}
            onClick={() => removeLine(i)}
          >
            ✕
          </button>
        </td>
      </tr>
    );
  }

  // ── Render: the tracker ────────────────────────────────────────────────────────────────────────
  // Supersede sources: in-force BASE documents only — a change order categorically can't be
  // superseded (the worker refuses; offering one here was a doomed pick).
  const sentPos = useMemo(() => pos.filter((p) => p.status === "sent" && p.change_order_of === null), [pos]);

  function trackerRow(p: api.PoListRow) {
    const vendor = vendorByKey.get(p.vendor_key);
    return (
      <section key={p.id} className="card">
        <div className="dash-card__head">
          <h3 className="dash-card__title">{p.po_number ?? `Draft #${p.id}`}</h3>
          <span className={STATUS_PILL[p.status] ?? "dash-pill"}>{STATUS_LABEL[p.status] ?? p.status}</span>
        </div>
        <div className="dash-card__sub">
          {p.job_no} · {p.job_name || p.job_id || "—"} · {vendor?.vendor_name ?? p.vendor_key}
        </div>
        <div className="dash-card__row">
          <div className="dash-chips">
            <span className="dash-chip">{formatCents(p.total_cents)}</span>
            <span className="dash-chip">Updated {fmtDate(p.updated_at)}</span>
            {p.supersedes_po_id !== null ? <span className="dash-chip">Supersedes #{p.supersedes_po_id}</span> : null}
            {p.change_order_of !== null ? <span className="dash-chip">Change order of #{p.change_order_of}</span> : null}
          </div>
        </div>
        {canManage ? (
          <div className="dash-row">
            {p.status === "draft" ? (
              <button className="btn btn--edit" disabled={busy} onClick={() => void openDraft(p.id)}>
                Open
              </button>
            ) : null}
            {/* A DRAFT is un-generated (no PO number / audit weight) → a clear hard delete (row + line items).
                A generated queued/pending_review record exits via the soft Cancel below, never a hard delete. */}
            {p.status === "draft" ? (
              armedDeleteId === p.id ? (
                <button className="btn btn--retire" disabled={busy} onClick={() => void onDelete(p.id)}>
                  Confirm delete
                </button>
              ) : (
                <button className="btn btn--retire" disabled={busy} onClick={() => setArmedDeleteId(p.id)}>
                  Delete
                </button>
              )
            ) : null}
            {p.status === "queued" || p.status === "pending_review" ? (
              armedCancelId === p.id ? (
                <button className="btn btn--retire" disabled={busy} onClick={() => void onCancel(p.id)}>
                  Confirm cancel
                </button>
              ) : (
                <button className="btn btn--retire" disabled={busy} onClick={() => setArmedCancelId(p.id)}>
                  Cancel PO
                </button>
              )
            ) : null}
            {p.status === "sent" && p.change_order_of === null ? (
              <>
                <button className="btn btn--primary" disabled={busy} onClick={() => void onSupersede(p.id)}>
                  Supersede
                </button>{" "}
                <button className="btn btn--secondary" disabled={busy} onClick={() => void onChangeOrder(p.id)}>
                  Change order
                </button>
              </>
            ) : null}
          </div>
        ) : null}
      </section>
    );
  }

  /** One reviewable estimate in the from-estimate picker. */
  function fromEstimateRow(r: est.EstimateRow) {
    const vendor = r.vendor_key ? vendorByKey.get(r.vendor_key) : undefined;
    return (
      <div key={r.id} className="dash-row">
        <span className="dash-table__name">{r.filename}</span>
        <span className="dash-chip">{r.job_no}</span>
        {r.job_name ? <span className="dash-chip">{r.job_name}</span> : null}
        <span className="dash-chip">{vendor?.vendor_name ?? r.vendor_key ?? "vendor unconfirmed"}</span>
        <span className="dash-pill dash-pill--warn">{est.ESTIMATE_STATUS_LABEL[r.status] ?? r.status}</span>
        <button className="btn btn--primary" disabled={busy} onClick={() => onReviewEstimate(r.id)}>
          Review &amp; import
        </button>
      </div>
    );
  }

  const tracker = (
    <>
      {canManage ? (
        <section className="card dash-section" aria-label="Start a purchase order">
          <div className="jha__actions">
            <button
              className="btn btn--primary"
              onClick={() => {
                resetForm();
                setView("builder");
              }}
            >
              + New purchase order
            </button>
            {fromEstOpen ? (
              <button className="btn btn--secondary" onClick={() => setFromEstOpen(false)}>
                Hide vendor estimates
              </button>
            ) : (
              <button className="btn btn--secondary" onClick={() => void onOpenFromEstimate()}>
                New PO from a vendor estimate
              </button>
            )}
          </div>
          {fromEstOpen ? (
            fromEstRows === null ? (
              <p className="muted">Loading estimates…</p>
            ) : fromEstRows.length === 0 ? (
              <>
                <p className="muted">
                  No estimates are ready to import. Upload a vendor quote on the Vendor Estimates
                  tab — once screened, it appears here for review.
                </p>
                <button className="btn btn--secondary" onClick={onOpenEstimatesTab}>
                  Open Vendor Estimates
                </button>
              </>
            ) : (
              <>
                <p className="muted">
                  Pick an estimate to review side-by-side against the source document — accepted
                  lines become an editable draft PO that opens right back here.
                </p>
                {fromEstRows.map(fromEstimateRow)}
              </>
            )
          ) : null}
        </section>
      ) : null}

      <label className="field">
        <span className="field__label">Status</span>
        <select
          className="field__input"
          aria-label="Status filter"
          value={statusFilter}
          onChange={(e) => {
            const s = e.target.value as "all" | api.PoStatus;
            setStatusFilter(s);
            reloadPos(s === "all" ? undefined : s);
          }}
        >
          <option value="all">All</option>
          {(Object.keys(STATUS_LABEL) as api.PoStatus[]).map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
            </option>
          ))}
        </select>
      </label>

      {loading && pos.length === 0 ? (
        <p className="muted">Loading…</p>
      ) : pos.length === 0 ? (
        <div className="dash-empty">No purchase orders yet.</div>
      ) : (
        <div className="dash-grid">{pos.map(trackerRow)}</div>
      )}
    </>
  );

  // ── Render: the builder wizard ─────────────────────────────────────────────────────────────────
  const builder = (
    <>
      <div className="dash-row">
        <button
          className="btn btn--secondary"
          onClick={() => {
            setView("tracker");
            setMsg(null);
          }}
        >
          ← Back to the list
        </button>
        {draftId !== null ? <span className="dash-pill">Editing draft #{draftId}</span> : null}
        {supersedesPoId !== null ? <span className="dash-pill dash-pill--warn">Supersedes PO #{supersedesPoId}</span> : null}
        {changeOrderOf !== null ? <span className="dash-pill dash-pill--warn">Change order CO{changeOrderOf.seq ?? "?"} of PO #{changeOrderOf.id}</span> : null}
      </div>

      {/* 1 — Job */}
      <section className="card dash-section" aria-label="Step 1 — Job">
        <h3 className="jha__section-title">1 · Job</h3>
        <label className="field">
          <span className="field__label">Job</span>
          <select className="field__input" aria-label="Job" value={jobId} onChange={(e) => void onJobSelect(e.target.value)}>
            <option value="">— job —</option>
            {jobs.map((j) => (
              <option key={j.job_id} value={j.job_id}>
                {j.project_name}
              </option>
            ))}
          </select>
        </label>
        <div className="jha__grid">
          <FieldInput label="Job number (YYYY.NNN)" value={jobNo} onChange={(v) => setJobNo(v)} maxLength={8} />
          <FieldInput label="Site / phase" value={sitePhase} onChange={(v) => setSitePhase(v)} maxLength={4} />
        </div>
        {jobNo && !JOB_NO_RE.test(jobNo.trim()) ? (
          <p className="muted">The job number must look like 2023.126 (year.number).</p>
        ) : null}
        <h4 className="dash-card__label">Ship to</h4>
        <FieldInput label="Site / receiving name" value={shipToName} onChange={setShipToName} maxLength={256} />
        <FieldInput label="Address" value={shipToAddress} onChange={setShipToAddress} maxLength={512} />
        <div className="jha__grid">
          <FieldInput label="City" value={shipToCity} onChange={setShipToCity} maxLength={256} />
          <label className="field">
            <span className="field__label">State (2-letter)</span>
            <input
              className="field__input"
              aria-label="Ship-to state"
              value={shipToState}
              maxLength={2}
              onChange={(e) => {
                touchMoney();
                setShipToState(e.target.value.toUpperCase());
              }}
            />
          </label>
          <FieldInput label="ZIP" value={shipToZip} onChange={setShipToZip} maxLength={16} />
        </div>
        {taxBadge ? <span className={taxBadge.cls}>{taxBadge.text}</span> : null}
        <h4 className="dash-card__label">Delivery contact</h4>
        <div className="jha__grid">
          <FieldInput
            label="Name"
            value={deliveryName}
            onChange={onDeliveryNameChange}
            maxLength={256}
            listId={deliveryContacts.length > 0 ? "po-delivery-contact-options" : undefined}
          />
          <FieldInput label="Phone" value={deliveryPhone} onChange={setDeliveryPhone} maxLength={40} />
          <FieldInput label="Email" value={deliveryEmail} onChange={setDeliveryEmail} maxLength={320} />
        </div>
        {/* The configured-contact suggestions (Feature C). A pick fills phone/email via
            onDeliveryNameChange's exact-match; free text stays fully accepted. */}
        {deliveryContacts.length > 0 && (
          <datalist id="po-delivery-contact-options">
            {deliveryContacts.map((ct) => (
              <option key={ct.name} value={ct.name} />
            ))}
          </datalist>
        )}
      </section>

      {/* 2 — Vendor */}
      <section className="card dash-section" aria-label="Step 2 — Vendor">
        <h3 className="jha__section-title">2 · Vendor</h3>
        <div className="mats-cat-filter" role="group" aria-label="Filter vendors by region">
          {REGIONS.map((r) => (
            <button
              key={r}
              type="button"
              className={`mats-cat-filter__chip${regionFilter === r ? " mats-cat-filter__chip--active" : ""}`}
              aria-pressed={regionFilter === r}
              onClick={() => setRegionFilter(regionFilter === r ? null : r)}
            >
              {r}
            </button>
          ))}
        </div>
        <div className="mats-cat-filter" role="group" aria-label="Filter vendors by supply category">
          {SUPPLY_CATEGORIES.map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={`mats-cat-filter__chip${categoryFilter === key ? " mats-cat-filter__chip--active" : ""}`}
              aria-pressed={categoryFilter === key}
              onClick={() => setCategoryFilter(categoryFilter === key ? null : key)}
            >
              {label}
            </button>
          ))}
        </div>
        {filteredVendors.length === 0 ? (
          <div className="dash-empty">No active vendors match the filters.</div>
        ) : (
          <div className="po-vendor-pick">
            {filteredVendors.map((v) => (
              <div
                key={v.vendor_key}
                className={`dash-row dash-row--click${v.vendor_key === vendorKey ? " po-vendor-pick__row--selected" : ""}`}
              >
                <button
                  type="button"
                  className="po-vendor-pick__btn"
                  aria-pressed={v.vendor_key === vendorKey}
                  onClick={() => onVendorSelect(v)}
                >
                  <span className="dash-table__name">{v.vendor_name}</span>{" "}
                  {v.region ? <span className="dash-chip">{v.region}</span> : null}
                  {v.supply_categories.slice(0, 4).map((c) => (
                    <span key={c} className="dash-chip">
                      {categoryLabel(c)}
                    </span>
                  ))}
                  {v.vendor_key === vendorKey ? <span className="dash-pill dash-pill--ok">Selected</span> : null}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 3 — Line items */}
      <section className="card dash-section" aria-label="Step 3 — Line items">
        <h3 className="jha__section-title">3 · Line items</h3>
        <div className="mats-cat-filter" role="group" aria-label="Line-item layout">
          {VARIANTS.map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={`mats-cat-filter__chip${variant === key ? " mats-cat-filter__chip--active" : ""}`}
              aria-pressed={variant === key}
              onClick={() => {
                touchMoney();
                setVariant(key);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <table className="dash-table po-lines">
          <thead>{lineHeaders()}</thead>
          <tbody>{lines.map(lineRow)}</tbody>
        </table>
        <div className="jha__actions">
          <button type="button" className="btn btn--secondary" onClick={addLine}>
            + Add a line
          </button>
        </div>
      </section>

      {/* 4 — Totals */}
      <section className="card dash-section" aria-label="Step 4 — Totals">
        <h3 className="jha__section-title">4 · Totals</h3>
        <div className="jha__grid">
          <label className="field">
            <span className="field__label">Tax</span>
            <select
              className="field__input"
              aria-label="Tax mode"
              value={taxMode}
              onChange={(e) => {
                touchMoney();
                setTaxMode(e.target.value as api.TaxMode);
              }}
            >
              {TAX_MODES.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {taxMode === "override" ? (
            <label className="field">
              <span className="field__label">Override rate (%)</span>
              <input
                className="field__input"
                aria-label="Tax override percent"
                value={taxOverridePct}
                maxLength={6}
                onChange={(e) => {
                  touchMoney();
                  setTaxOverridePct(e.target.value);
                }}
              />
            </label>
          ) : null}
          <label className="field">
            <span className="field__label">Shipping ($)</span>
            <input
              className="field__input"
              aria-label="Shipping dollars"
              value={shipping}
              maxLength={14}
              onChange={(e) => {
                touchMoney();
                setShipping(e.target.value);
              }}
            />
          </label>
        </div>
        <div className="po-totals" aria-label="Totals panel">
          {displayTotals ? (
            <>
              <div className="dash-card__row">
                <span className="dash-card__label">Subtotal</span>
                <span className="dash-table__name">{formatCents(displayTotals.subtotal_cents)}</span>
              </div>
              <div className="dash-card__row">
                <span className="dash-card__label">Tax ({bpToInput(displayTotals.tax_rate_bp)}%)</span>
                <span className="dash-table__name">{formatCents(displayTotals.tax_cents)}</span>
              </div>
              <div className="dash-card__row">
                <span className="dash-card__label">Shipping</span>
                <span className="dash-table__name">{formatCents(shippingCents ?? 0)}</span>
              </div>
              <div className="dash-card__row">
                <span className="dash-card__label">Total</span>
                <span className="dash-table__name">{formatCents(displayTotals.total_cents)}</span>
              </div>
              {serverTotals ? <p className="muted">Totals confirmed by the server for the saved draft.</p> : null}
            </>
          ) : (
            <p className="muted">Totals appear once every line and the tax basis are valid.</p>
          )}
        </div>
      </section>

      {/* 5 — Scope, delivery, payment */}
      <section className="card dash-section" aria-label="Step 5 — Scope and delivery">
        <h3 className="jha__section-title">5 · Scope of work, delivery, payment</h3>
        <label className="field">
          <span className="field__label">Scope of work</span>
          <textarea className="field__textarea" aria-label="Scope of work" value={sow} maxLength={8000} rows={5} onChange={(e) => setSow(e.target.value)} />
        </label>
        <label className="field">
          <span className="field__label">Delivery instructions</span>
          <textarea
            className="field__textarea"
            aria-label="Delivery instructions"
            value={deliveryInstructions}
            maxLength={4000}
            rows={3}
            onChange={(e) => setDeliveryInstructions(e.target.value)}
          />
        </label>
        <label className="field">
          <span className="field__label">Payment terms (invoice routing pre-filled)</span>
          <textarea
            className="field__textarea"
            aria-label="Payment terms"
            value={paymentTerms}
            maxLength={2000}
            rows={3}
            onChange={(e) => setPaymentTerms(e.target.value)}
          />
        </label>
      </section>

      {/* 6 — Terms */}
      <section className="card dash-section" aria-label="Step 6 — Terms">
        <h3 className="jha__section-title">6 · Terms &amp; conditions</h3>
        <label className="field">
          <span className="field__label">Terms profile{selectedVendor?.default_terms_profile ? " (vendor default preselected)" : ""}</span>
          <select
            className="field__input"
            aria-label="Terms profile"
            value={termsProfileId}
            onChange={(e) => setTermsProfileId(e.target.value)}
          >
            <option value="">— terms profile —</option>
            {terms.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
                {t.current_version ? ` (v${t.current_version})` : ""}
              </option>
            ))}
          </select>
        </label>
        {selectedTerms ? (
          <>
            <p className="muted">{selectedTerms.description}</p>
            {selectedTerms.kind === "attach" && selectedTerms.render_line ? (
              <div className="dash-card__row">
                <span className="dash-card__label">Rendered reference line</span>
                <span>{selectedTerms.render_line}</span>
              </div>
            ) : null}
            {selectedTerms.kind === "attach" && selectedVendor?.gtc_reference ? (
              <div className="dash-card__row">
                <span className="dash-card__label">Vendor GTC</span>
                <span>{selectedVendor.gtc_reference}</span>
              </div>
            ) : null}
          </>
        ) : null}
      </section>

      {/* 7 — Supersedes (optional) */}
      <section className="card dash-section" aria-label="Step 7 — Supersedes">
        <h3 className="jha__section-title">7 · Supersedes (optional)</h3>
        {changeOrderOf !== null ? (
          <p className="muted">
            This draft is change order CO{changeOrderOf.seq ?? "?"} of PO #{changeOrderOf.id} — the
            original PO stays in force; this document carries the change. Its number is minted at
            generate as the original&apos;s number + -CO{changeOrderOf.seq ?? "n"}.
          </p>
        ) : supersedesPoId !== null ? (
          <p className="muted">This draft supersedes PO #{supersedesPoId} — the old PO stays in force until this one is sent.</p>
        ) : sentPos.length === 0 ? (
          <p className="muted">No sent POs to supersede.</p>
        ) : (
          <label className="field">
            <span className="field__label">Replace a sent PO (clones it into a fresh draft)</span>
            <select
              className="field__input"
              aria-label="Supersede a sent PO"
              value=""
              onChange={(e) => {
                const id = parseInt(e.target.value, 10);
                if (Number.isSafeInteger(id)) void onSupersede(id);
              }}
            >
              <option value="">— sent PO —</option>
              {sentPos.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.po_number ?? `#${p.id}`} · {p.job_name || p.job_no} · {formatCents(p.total_cents)}
                </option>
              ))}
            </select>
          </label>
        )}
      </section>

      {/* 8 — Attachments (Feature B — the "end of the workflow" attachment field). Files ride
          the SAVED draft in the send-free D1 pool and are §34-screened on the Mac after the PO
          files; only CLEAN files reach Box + the PO ledger row. The Worker is the real gate —
          the checks here are hints. */}
      <section className="card dash-section" aria-label="Step 8 — Attachments">
        <h3 className="jha__section-title">8 · Attachments (specs, drawings, docs)</h3>
        {draftId === null ? (
          <p className="muted">Save the draft first — attachments ride the saved draft and file with the PO.</p>
        ) : (
          <>
            {attachments.length > 0 ? (
              <table className="dash-table">
                <thead>
                  <tr>
                    <th>File</th>
                    <th>Size</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {attachments.map((a) => (
                    <tr key={a.id}>
                      <td className="dash-table__name">{a.filename}</td>
                      <td>{formatBytes(a.size_bytes)}</td>
                      <td>
                        <span className="dash-pill">{ATTACHMENT_STATUS_LABEL[a.status] ?? a.status}</span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn btn--retire"
                          aria-label={`Remove attachment ${a.filename}`}
                          disabled={attBusy || busy}
                          onClick={() => void onRemoveAttachment(a.id)}
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted">No attachments yet.</p>
            )}
            {canManage ? (
              <label className="field">
                <span className="field__label">
                  Add files (PDF, JPG, PNG, DOCX, XLSX — up to 10 MB each, max {api.MAX_ATTACHMENTS_PER_PO})
                </span>
                <input
                  className="field__input"
                  type="file"
                  aria-label="Attach files"
                  accept={api.ATTACHMENT_ACCEPT}
                  multiple
                  disabled={attBusy || busy || attachments.length >= api.MAX_ATTACHMENTS_PER_PO}
                  onChange={(e) => {
                    void onAttachFiles(e.target.files);
                    e.target.value = ""; // allow re-picking the same file after a refusal
                  }}
                />
              </label>
            ) : null}
            <p className="muted">
              Attachments are security-screened before filing; clean files land in the job's Box
              folder beside the PO and on the PO ledger row.
            </p>
          </>
        )}
      </section>

      {/* 9 — Review & generate */}
      <section className="card dash-section" aria-label="Step 9 — Review and generate">
        <h3 className="jha__section-title">9 · Review &amp; generate</h3>
        <div className="dash-card__row">
          <span className="dash-card__label">Job</span>
          <span>
            {jobName || "—"} · {jobNo || "—"} · phase {sitePhase || "—"}
          </span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Ship to</span>
          <span>
            {[shipToName, shipToAddress, shipToCity, stateUpper, shipToZip].filter(Boolean).join(", ") || "—"}
          </span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Delivery contact</span>
          <span>{[deliveryName, deliveryPhone, deliveryEmail].filter(Boolean).join(" · ") || "—"}</span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Vendor</span>
          <span>{selectedVendor ? `${selectedVendor.vendor_name} (${selectedVendor.vendor_key})` : "—"}</span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Line items</span>
          <span>
            {validLines.length} of {lines.length} valid · {VARIANTS.find(([k]) => k === variant)?.[1]}
          </span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Tax</span>
          <span>
            {TAX_MODES.find(([k]) => k === taxMode)?.[1]}
            {displayTotals ? ` — ${bpToInput(displayTotals.tax_rate_bp)}%` : ""}
            {taxMode === "auto" && stateNames[stateUpper] ? ` (${stateNames[stateUpper]})` : ""}
          </span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Totals</span>
          <span className="dash-table__name">
            {displayTotals
              ? `${formatCents(displayTotals.subtotal_cents)} + tax ${formatCents(displayTotals.tax_cents)} + shipping ${formatCents(shippingCents ?? 0)} = ${formatCents(displayTotals.total_cents)}`
              : "—"}
          </span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Terms</span>
          <span>{selectedTerms ? `${selectedTerms.label}${selectedTerms.current_version ? ` v${selectedTerms.current_version}` : ""}` : "—"}</span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Attachments</span>
          <span>{attachments.length === 0 ? "None" : `${attachments.length} file${attachments.length === 1 ? "" : "s"} (screened before filing)`}</span>
        </div>
        {sow ? (
          <div className="dash-card__row">
            <span className="dash-card__label">Scope of work</span>
            <span>{sow}</span>
          </div>
        ) : null}
        {deliveryInstructions ? (
          <div className="dash-card__row">
            <span className="dash-card__label">Delivery instructions</span>
            <span>{deliveryInstructions}</span>
          </div>
        ) : null}
        {paymentTerms ? (
          <div className="dash-card__row">
            <span className="dash-card__label">Payment terms</span>
            <span>{paymentTerms}</span>
          </div>
        ) : null}
        <div className="jha__grid">
          <FieldInput label="Approver name" value={approverName} onChange={setApproverName} maxLength={256} />
          <FieldInput label="Approver title" value={approverTitle} onChange={setApproverTitle} maxLength={256} />
        </div>
        {canManage ? (
          <div className="jha__actions">
            <button className="btn btn--secondary" disabled={busy} onClick={() => void onSaveDraft()}>
              {busy ? "Working…" : "Save draft"}
            </button>
            <button className="btn btn--primary" disabled={busy} onClick={() => void onGenerate()}>
              {busy ? "Working…" : "Generate PO"}
            </button>
          </div>
        ) : null}
      </section>
    </>
  );

  return (
    <>
      <p className="dash__intro">
        Build a vendor purchase order — line items, tax, and terms — then track it from draft through
        render, review, and send. Generated POs queue for the Mac-side renderer; sending always goes
        through human approval.
      </p>

      {msg && <div className={`banner ${msg.ok ? "banner--ok" : "banner--err"}`}>{msg.text}</div>}
      {error && <div className="banner banner--err">{error}</div>}

      {view === "tracker" ? tracker : builder}
    </>
  );
}
