import { useState, useEffect, useCallback, useMemo } from "react";
import * as api from "../lib/subcontracts";
import {
  stateName,
  formatCents,
  parseDollarsToCents,
  sovExtendedCents,
  computeSubtotal,
} from "../lib/subcontracts";
import { fetchJobs, type Job } from "../lib/api";
import { errorText } from "../lib/errorCopy";
import { useAuth } from "../lib/auth";
import { PageShell } from "../components/PageShell";

// Subcontracts workstream SC-S5 — the builder + status tracker (a faithful mirror of PoBuilderPage).
// One page, two faces: the TRACKER (every subcontract from GET /api/subcontracts/subs with per-status
// actions) and the BUILDER (a single-page wizard: job → subcontractor → schedule of values → contract
// price → Exhibit A/scope → terms → supersedes → review → Generate). cap.subcontracts.manage gates the
// whole view (router VIEW_CAPS) AND every write affordance in-page; the Worker re-gates every call
// (Invariant 2 — SPA gating is convenience, never the boundary).
//
// MONEY (the biggest structural delta from PO): a subcontract is a LUMP-SUM contract price. There is
// NO tax, NO shipping, NO per-watt line. subtotal_cents = Σ round(qty × unit_price_cents) and MUST
// equal contract_price_cents — the "SOV-sums-to-price gate", enforced at BOTH draft (parseDraftBody)
// and generate (worker/subcontract.ts). The page mirrors the Worker's integer math for the LIVE
// display (sovExtendedCents/computeSubtotal — the same rounding the HMAC signs), but the Worker is
// authoritative: the save response's `subtotal_cents` confirms the draft, generate sends exactly the
// DISPLAYED contract price, and a `sov_mismatch` 409 re-renders the gate from the server's
// `recomputed` numbers — the client never argues with the Worker about money.
//
// SUPERSESSION: a SENT or EXECUTED subcontract ('executed' — the wet-signature countersign terminal —
// is NEW vs PO's 7 statuses) is superseded through POST /api/subcontracts/:id/supersede, which CLONES
// it into a fresh draft (supersede_seq+1); the builder then opens that clone. The old subcontract
// stays in force until the successor is actually sent. A `supersede_in_progress` 409 opens the
// existing successor instead of minting a sibling.
//
// JOB PICK: the dropdown rides the shared, capability-free GET /api/jobs (id + project_name), exactly
// as PoBuilderPage does. There is NO subcontract ship-to/site auto-fill route (the PO one is
// cap.po.manage-gated), so site_name/site_address/owner_entity/prime_contractor are MANUAL in v1 —
// project_name/job_name/site_name derive from the dropdown row's project_name and prime_contractor
// seeds from config; the rest stay operator-typed.

const JOB_NO_RE = /^\d{4}\.\d{3}$/;
const QTY_RE = /^\d+(\.\d{0,3})?$/; // the Worker normalizes qty to ≤3dp
const INT_RE = /^\d+$/;
const STATE_RE = /^[A-Z]{2}$/;

const STATUS_LABEL: Record<api.SubcontractStatus, string> = {
  draft: "Draft",
  queued: "Queued for render",
  pending_review: "Pending review",
  approved: "Approved",
  sent: "Sent",
  executed: "Executed",
  superseded: "Superseded",
  canceled: "Canceled",
};
const STATUS_PILL: Record<api.SubcontractStatus, string> = {
  draft: "dash-pill",
  queued: "dash-pill dash-pill--warn",
  pending_review: "dash-pill dash-pill--warn",
  approved: "dash-pill dash-pill--ok",
  sent: "dash-pill dash-pill--ok",
  executed: "dash-pill dash-pill--ok",
  superseded: "dash-pill",
  canceled: "dash-pill dash-pill--danger",
};

const PRICE_BASES: [api.PriceBasis, string][] = [
  ["fixed", "Fixed price"],
  ["not_to_exceed", "Not-to-exceed"],
];
const TEMPLATE_FAMILIES: [api.TemplateFamily, string][] = [
  ["long_form", "Long form"],
  ["short_form", "Short form"],
];

/** One editable SOV row — everything a string (controlled inputs); parsed per keystroke for the live
 *  extended/subtotal mirror. */
interface SovLineForm {
  item_number: string;
  description: string;
  qty: string;
  unit: string;
  unit_price: string; // dollars
}
const EMPTY_LINE: SovLineForm = { item_number: "", description: "", qty: "", unit: "", unit_price: "" };

/** Parse one SOV row. Returns the wire line, or null while the row is incomplete/invalid (the grid
 *  shows "—" for its extended and Generate blocks). unit_price_cents is REQUIRED on every line. */
function parseLine(l: SovLineForm): api.SovDraftLine | null {
  const description = l.description.trim();
  if (!description) return null;
  if (!QTY_RE.test(l.qty.trim())) return null;
  const cents = parseDollarsToCents(l.unit_price);
  if (cents === null || l.unit_price.trim() === "") return null;
  return {
    item_number: l.item_number.trim(),
    description,
    qty: Number(l.qty),
    unit: l.unit.trim(),
    unit_price_cents: cents,
  };
}

/** Integer cents → a plain editable input value ("1234.56"). */
function centsToInput(c: number | null): string {
  if (c === null || c === undefined) return "";
  return `${Math.floor(c / 100)}.${String(c % 100).padStart(2, "0")}`;
}
/** Basis points → percent input ("10.00"). Same digit relationship as cents→dollars. */
const bpToInput = centsToInput;
/** Percent input → basis points ("10.00" → 1000). Same ×100 digit shift as dollars→cents. */
const pctToBp = parseDollarsToCents;

function fmtDate(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}

function FieldInput({
  label,
  value,
  onChange,
  maxLength,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  maxLength?: number;
  /** Native input type — "date" gives a calendar picker whose value is ISO YYYY-MM-DD. */
  type?: string;
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      <input
        className="field__input"
        type={type}
        value={value}
        maxLength={maxLength}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

export function SubcontractBuilderPage({
  onBack,
  openDraftRequest,
}: {
  onBack: () => void;
  /** Nonce-keyed one-shot from App (Track D2 — the job screen's just-created change-order
   *  draft): open this draft in the builder. The PoBuilderPage openDraftRequest twin. */
  openDraftRequest?: { id: number; nonce: number } | null;
}) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.subcontracts.manage"); // UI affordance only — the Worker re-gates

  // ── Shared data ────────────────────────────────────────────────────────────────────────────────
  const [subcontracts, setSubcontracts] = useState<api.SubcontractListRow[]>([]);
  const [subcontractors, setSubcontractors] = useState<api.Subcontractor[]>([]);
  const [terms, setTerms] = useState<api.TermsProfile[]>([]);
  const [config, setConfig] = useState<api.SubcontractConfig | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  // Live trade vocabulary (manifest-derived); static api.TRADES is the degraded-fetch fallback.
  const [trades, setTrades] = useState<string[]>(api.TRADES);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const reloadSubs = useCallback((status?: api.SubcontractStatus) => {
    setLoading(true);
    api
      .fetchSubDrafts(status)
      .then(setSubcontracts)
      .catch(() => setError("Failed to load subcontracts."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reloadSubs();
    api.fetchSubcontractors().then(setSubcontractors).catch(() => setError("Failed to load the subcontractor list."));
    api.fetchSubTerms().then(setTerms).catch(() => setTerms([]));
    api.fetchSubConfig().then(setConfig).catch(() => setConfig(null));
    fetchJobs().then(setJobs).catch(() => setJobs([]));
    // Live trades feed the Trade dropdown; a miss degrades to the static api.TRADES (never empty).
    api.fetchTrades().then((t) => setTrades(t.length ? t : api.TRADES)).catch(() => setTrades(api.TRADES));
  }, [reloadSubs]);

  const subByKey = useMemo(() => new Map(subcontractors.map((s) => [s.sub_key, s])), [subcontractors]);

  // ── Tracker state ──────────────────────────────────────────────────────────────────────────────
  const [view, setView] = useState<"tracker" | "builder">("tracker");
  const [statusFilter, setStatusFilter] = useState<"all" | api.SubcontractStatus>("all");
  const [armedCancelId, setArmedCancelId] = useState<number | null>(null);
  const [armedDeleteId, setArmedDeleteId] = useState<number | null>(null);

  // ── Builder form state ─────────────────────────────────────────────────────────────────────────
  const [draftId, setDraftId] = useState<number | null>(null);
  const [supersedesScId, setSupersedesScId] = useState<number | null>(null);
  // Track D2: set when the open draft IS a change order — parent id + this CO's sequence.
  const [changeOrderOf, setChangeOrderOf] = useState<{ id: number; seq: number | null } | null>(null);
  const [jobId, setJobId] = useState("");
  const [jobName, setJobName] = useState("");
  const [jobNo, setJobNo] = useState("");
  const [sitePhase, setSitePhase] = useState("0");
  const [projectName, setProjectName] = useState("");
  const [ownerEntity, setOwnerEntity] = useState("");
  const [primeContractor, setPrimeContractor] = useState("");
  const [siteName, setSiteName] = useState("");
  const [siteAddress, setSiteAddress] = useState("");
  const [governingLawState, setGoverningLawState] = useState("");
  const [subKey, setSubKey] = useState("");
  const [stateFilter, setStateFilter] = useState<string | null>(null);
  const [trade, setTrade] = useState("");
  const [scopeSummary, setScopeSummary] = useState("");
  const [exhibitAWorkText, setExhibitAWorkText] = useState("");
  /** The trade whose Exhibit A template last OVERWROTE the Work text (drives the "set from X" hint);
   *  null when the Work text was authored/opened manually or the last trade fetch failed, so the hint
   *  never goes stale. */
  const [articleIiPrefillTrade, setArticleIiPrefillTrade] = useState<string | null>(null);
  const [startDate, setStartDate] = useState("");
  const [completionDate, setCompletionDate] = useState("");
  const [lines, setLines] = useState<SovLineForm[]>([{ ...EMPTY_LINE }]);
  const [contractPrice, setContractPrice] = useState(""); // dollars
  const [priceBasis, setPriceBasis] = useState<api.PriceBasis>("fixed");
  const [retainageBp, setRetainageBp] = useState(""); // percent string
  const [templateFamily, setTemplateFamily] = useState<api.TemplateFamily>("long_form");
  const [termsProfileId, setTermsProfileId] = useState("");
  const [approverName, setApproverName] = useState("");
  const [approverTitle, setApproverTitle] = useState("");
  /** The Worker's confirmed SOV gate for the current saved/recomputed snapshot (save response's
   *  subtotal_cents, or a generate 409's `recomputed`). Displayed over the live mirror when present;
   *  any money-affecting edit clears it back to the live mirror. */
  const [serverGate, setServerGate] = useState<{ subtotal_cents: number; contract_price_cents: number } | null>(null);

  function resetForm() {
    setDraftId(null);
    setSupersedesScId(null);
    setChangeOrderOf(null);
    setJobId("");
    setJobName("");
    setJobNo("");
    setSitePhase("0");
    setProjectName("");
    setOwnerEntity("");
    setPrimeContractor(config?.contractor.prime_contractor_default ?? "");
    setSiteName("");
    setSiteAddress("");
    setGoverningLawState("");
    setSubKey("");
    setStateFilter(null);
    setTrade("");
    setScopeSummary("");
    setExhibitAWorkText("");
    setArticleIiPrefillTrade(null);
    setStartDate("");
    setCompletionDate("");
    setLines([{ ...EMPTY_LINE }]);
    setContractPrice("");
    setPriceBasis("fixed");
    // Prefill from config; fall back to 10% (1000 bp — the standard §2.5 retention) if the config
    // fetch failed, so a degraded /config never leaves retainage empty and blocks generate.
    setRetainageBp(bpToInput(config?.payment_terms.retainage_bp ?? 1000));
    setTemplateFamily("long_form");
    setTermsProfileId("");
    setApproverName("");
    setApproverTitle("");
    setServerGate(null);
  }

  // ── Live money mirror (display only — the Worker recomputes and is authoritative) ────────────────
  const parsedLines = useMemo(() => lines.map((l) => parseLine(l)), [lines]);
  const validLines = useMemo(() => parsedLines.filter((l): l is api.SovDraftLine => l !== null), [parsedLines]);
  const liveSubtotal = useMemo(() => computeSubtotal(validLines), [validLines]);
  const contractPriceCents = parseDollarsToCents(contractPrice);

  /** Any money-affecting edit (SOV line or contract price) invalidates the saved server gate. */
  const touchMoney = () => setServerGate(null);

  const displaySubtotal = serverGate ? serverGate.subtotal_cents : liveSubtotal;
  const displayContract = serverGate ? serverGate.contract_price_cents : contractPriceCents ?? 0;
  const gateReady = serverGate !== null || contractPriceCents !== null;
  const balances = displaySubtotal === displayContract;

  // ── Job select ───────────────────────────────────────────────────────────────────────────────────
  function onJobSelect(id: string) {
    setJobId(id);
    const job = jobs.find((j) => j.job_id === id);
    if (!job) return;
    // Immediate fills from the /api/jobs dropdown row (always present). project/job/site name all
    // derive from project_name; owner_entity stays manual.
    setProjectName(job.project_name);
    setJobName(job.project_name);
    setSiteName(job.project_name);
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
    // Site address auto-fills from the Smartsheet ITS_Active_Jobs "Address" SoR (C1) when available —
    // a CONVENIENCE only: a blank / 404 / degraded fetch leaves the operator-editable Site-address
    // field alone (never clobbers it with an empty on a job whose SoR address is unset).
    void api
      .fetchJobSiteAddress(id)
      .then(({ site_address }) => {
        if (site_address.trim()) setSiteAddress(site_address);
      })
      .catch(() => {
        /* no site-address SoR row / degraded route — leave the Site-address field manual */
      });
  }

  // ── Subcontractor picker (grouped by STATE) ────────────────────────────────────────────────────────
  const activeSubs = useMemo(() => subcontractors.filter((s) => s.active === 1), [subcontractors]);
  const statesPresent = useMemo(() => {
    const set = new Set<string>();
    for (const s of activeSubs) set.add((s.state || "").toUpperCase());
    return [...set].sort((a, b) => a.localeCompare(b));
  }, [activeSubs]);
  const groupedSubs = useMemo(() => {
    const byState = new Map<string, api.Subcontractor[]>();
    for (const s of activeSubs) {
      const key = (s.state || "").toUpperCase();
      if (stateFilter !== null && key !== stateFilter) continue;
      const bucket = byState.get(key);
      if (bucket) bucket.push(s);
      else byState.set(key, [s]);
    }
    // Blank-state ("Unassigned") collates LAST — consistent with SubcontractorsPage's directory grouping.
    return [...byState.entries()].sort((a, b) =>
      a[0] === "" ? 1 : b[0] === "" ? -1 : a[0].localeCompare(b[0]),
    );
  }, [activeSubs, stateFilter]);
  const selectedSub = subByKey.get(subKey) ?? null;

  function onSubSelect(s: api.Subcontractor) {
    setSubKey(s.sub_key);
    // The subcontractor's default terms profile is PRESELECTED; the picker below can override.
    if (s.default_terms_profile) setTermsProfileId(s.default_terms_profile);
  }

  // ── Trade select → Exhibit A Article II (OVERWRITE) ──────────────────────────────────────────────
  /** On a trade pick, OVERWRITE Exhibit A ("the Work") with that trade's standard Article II template
   *  (operator directive 2026-07-12: changing the trade REPLACES whatever is in the Work box, so the
   *  boilerplate always tracks the selected trade — the old "only when empty" guard is gone). Selecting
   *  the blank option clears the source hint but leaves the text. An unknown trade / degraded fetch does
   *  NOT clobber existing text — there is no template to write, so we leave the box and clear the hint
   *  (never destroy the operator's work with an empty overwrite). Re-picking the SAME trade fires no
   *  onChange, so a heavily-edited box is only replaced on a deliberate switch to a different trade. */
  async function onTradeSelect(t: string) {
    setTrade(t);
    if (!t) {
      setArticleIiPrefillTrade(null);
      return;
    }
    try {
      const tpl = await api.fetchExhibitTemplate(t);
      setExhibitAWorkText(tpl.article_ii);
      setArticleIiPrefillTrade(t);
    } catch {
      // Unknown trade / degraded /exhibit-templates — no template to write; leave the box untouched.
      setArticleIiPrefillTrade(null);
    }
  }

  const selectedTerms = terms.find((t) => t.id === termsProfileId) ?? null;

  // ── SOV line grid ────────────────────────────────────────────────────────────────────────────────
  const setLine = (i: number, patch: Partial<SovLineForm>) => {
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

  // ── Draft save / generate / open ─────────────────────────────────────────────────────────────────
  function validate(): string | null {
    if (!subKey) return "Pick a subcontractor.";
    if (!JOB_NO_RE.test(jobNo.trim())) return "The job number must look like 2023.126 (year.number).";
    if (!INT_RE.test(sitePhase.trim()) || parseInt(sitePhase, 10) > 9999) {
      return "The site/phase must be a whole number from 0 to 9999.";
    }
    if (parsedLines.some((l) => l === null)) return "Every schedule-of-values line needs a description, quantity, and unit price.";
    if (validLines.length === 0) return "Add at least one schedule-of-values line.";
    if (contractPriceCents === null) return "Enter a valid contract price.";
    const bp = pctToBp(retainageBp);
    if (bp === null || bp < 0 || bp > 10_000) return "The retainage must be a percentage from 0 to 100.";
    if (liveSubtotal !== contractPriceCents) {
      return "The schedule of values must add up to the contract price — adjust a line or the price.";
    }
    const st = governingLawState.trim().toUpperCase();
    if (!STATE_RE.test(st) || !(config?.governing_law_states ?? []).includes(st)) {
      return "Pick a governing-law state — a subcontract can't be generated without one.";
    }
    // Render-required party/scope fields (subcontract_generate _REQUIRED_FIELDS + the strict Exhibit
    // tokens). A blank one used to pass the whole way through and fence PERMANENTLY at render — flag it here.
    if (!trade) return "Pick a trade — the Exhibit A scope can't render without one.";
    if (!projectName.trim()) return "Enter the project name.";
    if (!ownerEntity.trim()) return "Enter the Owner entity (the Evergreen contracting SPV).";
    return null;
  }

  function buildBody(): api.DraftBody {
    return {
      sub_key: subKey,
      job_no: jobNo.trim(),
      site_phase: parseInt(sitePhase, 10),
      job_id: jobId,
      job_name: jobName.trim(),
      project_name: projectName.trim(),
      owner_entity: ownerEntity.trim(),
      prime_contractor: primeContractor.trim(),
      site_name: siteName.trim(),
      site_address: siteAddress.trim(),
      governing_law_state: governingLawState.trim().toUpperCase(),
      trade: trade.trim(),
      exhibit_a_work_text: exhibitAWorkText,
      scope_summary: scopeSummary.trim(),
      price_basis: priceBasis,
      contract_price_cents: contractPriceCents ?? 0,
      retainage_bp: pctToBp(retainageBp) ?? 0,
      start_date: startDate.trim(),
      completion_date: completionDate.trim(),
      terms_profile_id: termsProfileId,
      terms_version: selectedTerms?.current_version ?? "",
      template_family: templateFamily,
      approver_name: approverName.trim(),
      approver_title: approverTitle.trim(),
      sov_lines: validLines,
    };
  }

  /** Persist the draft (create or full-replace update). Returns the saved id + the WORKER's confirmed
   *  subtotal — the number the review gate then displays and generate asserts against. */
  async function saveDraft(): Promise<{ id: number; subtotal_cents: number } | null> {
    const problem = validate();
    if (problem) {
      setMsg({ ok: false, text: problem });
      return null;
    }
    const body = buildBody();
    try {
      const res = draftId === null ? await api.createSubDraft(body) : await api.updateSubDraft(draftId, body);
      setDraftId(res.id);
      // Save succeeded → the Worker's sums-to-price gate passed, so subtotal == contract price.
      setServerGate({ subtotal_cents: res.subtotal_cents, contract_price_cents: contractPriceCents ?? 0 });
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
      setMsg({ ok: true, text: `Draft saved (#${saved.id}) — the schedule of values balances to the contract price.` });
      reloadSubs(statusFilter === "all" ? undefined : statusFilter);
    }
    setBusy(false);
  }

  async function onGenerate() {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    // Save first so the stored draft is exactly what's on screen, then generate against the DISPLAYED
    // contract price — the anti-skew assert sees the displayed cents.
    const saved = await saveDraft();
    if (!saved) {
      setBusy(false);
      return;
    }
    try {
      const r = await api.generateSubcontract(saved.id, { contract_price_cents: contractPriceCents ?? 0 });
      if (r.ok) {
        setMsg({ ok: true, text: `Subcontract ${r.sc_number} generated — queued for rendering and review.` });
        resetForm();
        setView("tracker");
        reloadSubs(statusFilter === "all" ? undefined : statusFilter);
      } else if (r.error === "sov_mismatch") {
        // The Worker's recomputed money is authoritative — re-render the gate from it.
        setServerGate(r.recomputed);
        setMsg({ ok: false, text: errorText("sov_mismatch") });
      } else {
        setMsg({ ok: false, text: errorText(r.error) });
      }
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Generate failed." });
    }
    setBusy(false);
  }

  /** Open an existing draft in the builder (tracker "Open", supersede clones). */
  async function openDraft(id: number) {
    setBusy(true);
    setMsg(null);
    try {
      const { subcontract: sc, sov_lines } = await api.fetchSubDraft(id);
      setDraftId(sc.id);
      setSupersedesScId(sc.supersedes_sc_id);
      setChangeOrderOf(sc.change_order_of !== null ? { id: sc.change_order_of, seq: sc.co_seq } : null);
      setJobId(sc.job_id);
      setJobName(sc.job_name);
      setJobNo(sc.job_no);
      setSitePhase(String(sc.site_phase));
      setProjectName(sc.project_name);
      setOwnerEntity(sc.owner_entity);
      setPrimeContractor(sc.prime_contractor);
      setSiteName(sc.site_name);
      setSiteAddress(sc.site_address);
      setGoverningLawState(sc.governing_law_state);
      setSubKey(sc.sub_key);
      setTrade(sc.trade);
      setScopeSummary(sc.scope_summary);
      setExhibitAWorkText(sc.exhibit_a_work_text);
      setArticleIiPrefillTrade(null);
      setStartDate(sc.start_date);
      setCompletionDate(sc.completion_date);
      setContractPrice(centsToInput(sc.contract_price_cents));
      setPriceBasis((sc.price_basis as api.PriceBasis) || "fixed");
      setRetainageBp(bpToInput(sc.retainage_bp));
      setTemplateFamily((sc.template_family as api.TemplateFamily) || "long_form");
      setTermsProfileId(sc.terms_profile_id);
      setApproverName(sc.approver_name);
      setApproverTitle(sc.approver_title);
      setLines(
        sov_lines.length === 0
          ? [{ ...EMPTY_LINE }]
          : sov_lines.map((l) => ({
              item_number: l.item_number,
              description: l.description,
              qty: String(l.qty),
              unit: l.unit,
              unit_price: centsToInput(l.unit_price_cents),
            })),
      );
      setServerGate({ subtotal_cents: sc.subtotal_cents, contract_price_cents: sc.contract_price_cents });
      setView("builder");
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Could not open the draft." });
    }
    setBusy(false);
  }

  /** Supersede a SENT or EXECUTED subcontract: the Worker clones it into a new draft, which opens in
   *  the builder. A successor already in flight (409) opens instead — never a sibling. */
  async function onSupersede(id: number) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.supersedeSubcontract(id);
      if (r.ok) {
        setMsg({ ok: true, text: "A replacement draft was created from the subcontract — review and generate it." });
        await openDraft(r.id);
      } else {
        setMsg({ ok: false, text: errorText("supersede_in_progress") });
        await openDraft(r.existing_id);
      }
      reloadSubs(statusFilter === "all" ? undefined : statusFilter);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Supersede failed." });
    }
    setBusy(false);
  }

  /** Create a CHANGE-ORDER draft from a sent/executed subcontract (Track D2): the Worker
   *  clones the parent's full configuration + SOV; the office edits what changed and
   *  generates — the parent stays in force, and the CO's number ({parent}-CO{n}) is minted
   *  at generate. */
  async function onChangeOrder(id: number) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.createSubChangeOrder(id);
      setMsg({
        ok: true,
        text: `Change order CO${r.co_seq ?? "?"} drafted from the subcontract — edit what changed, then Generate.`,
      });
      await openDraft(r.id);
      reloadSubs(statusFilter === "all" ? undefined : statusFilter);
    } catch (err) {
      // ApiError.message is already human copy (errorText in its constructor).
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Change order failed." });
    }
    setBusy(false);
  }

  // The App-level one-shot (Track D2): the job Procurement screen created a change-order
  // draft and navigated here — open it. Discard-guard: if the builder is mid-edit on a
  // DIFFERENT draft, confirm before replacing (the PoBuilderPage openDraftRequest twin).
  useEffect(() => {
    if (!openDraftRequest) return;
    const { id } = openDraftRequest;
    void (async () => {
      if (
        view === "builder" &&
        draftId !== id &&
        !window.confirm(
          `Open change-order draft #${id} now? The subcontract you were editing will be replaced (unsaved changes are lost).`,
        )
      ) {
        setMsg({ ok: true, text: `Change-order draft #${id} is in the list — open it when you're ready.` });
        reloadSubs(statusFilter === "all" ? undefined : statusFilter);
        return;
      }
      await openDraft(id);
      reloadSubs(statusFilter === "all" ? undefined : statusFilter);
    })();
    // Deliberately keyed on the request nonce alone (the PoBuilderPage precedent): openDraft/
    // reloadSubs are stable-enough in-component closures; re-running on their identity would
    // replay the open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openDraftRequest]);

  async function onCancel(id: number) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    setArmedCancelId(null);
    try {
      await api.cancelSubcontract(id);
      if (draftId === id) resetForm();
      setMsg({ ok: true, text: "Subcontract canceled." });
      reloadSubs(statusFilter === "all" ? undefined : statusFilter);
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
      await api.deleteSubDraft(id);
      if (draftId === id) resetForm();
      setMsg({ ok: true, text: "Draft deleted." });
      reloadSubs(statusFilter === "all" ? undefined : statusFilter);
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Delete failed." });
    }
    setBusy(false);
  }

  // ── Render: SOV line grid ──────────────────────────────────────────────────────────────────────────
  function lineRow(l: SovLineForm, i: number) {
    const parsed = parsedLines[i];
    const extended = parsed !== null ? formatCents(sovExtendedCents(parsed)) : "—";
    const cell = (field: keyof SovLineForm, label: string, width?: number) => (
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
        <td>{cell("item_number", "item number", 110)}</td>
        <td>{cell("description", "description")}</td>
        <td>{cell("qty", "quantity", 80)}</td>
        <td>{cell("unit", "unit", 70)}</td>
        <td>{cell("unit_price", "unit price", 110)}</td>
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

  // ── Render: the tracker ────────────────────────────────────────────────────────────────────────────
  const supersedableSubs = useMemo(
    () => subcontracts.filter((s) => s.status === "sent" || s.status === "executed"),
    [subcontracts],
  );

  function trackerRow(p: api.SubcontractListRow) {
    const sub = subByKey.get(p.sub_key);
    return (
      <section key={p.id} className="card">
        <div className="dash-card__head">
          <h3 className="dash-card__title">{p.sc_number ?? `Draft #${p.id}`}</h3>
          <span className={STATUS_PILL[p.status] ?? "dash-pill"}>{STATUS_LABEL[p.status] ?? p.status}</span>
        </div>
        <div className="dash-card__sub">
          {p.job_no} · {p.job_name || p.project_name || p.job_id || "—"} · {sub?.sub_name ?? p.sub_key}
        </div>
        <div className="dash-card__row">
          <div className="dash-chips">
            <span className="dash-chip">{formatCents(p.contract_price_cents)}</span>
            <span className="dash-chip">Updated {fmtDate(p.updated_at)}</span>
            {p.supersedes_sc_id !== null ? <span className="dash-chip">Supersedes #{p.supersedes_sc_id}</span> : null}
          </div>
        </div>
        {canManage ? (
          <div className="dash-row">
            {p.status === "draft" ? (
              <button className="btn btn--edit" disabled={busy} onClick={() => void openDraft(p.id)}>
                Open
              </button>
            ) : null}
            {/* A DRAFT is un-generated (no SC number / audit weight) → a clear hard delete (row + SOV lines).
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
                  Cancel SC
                </button>
              )
            ) : null}
            {(p.status === "sent" || p.status === "executed") && p.change_order_of === null ? (
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

  const tracker = (
    <>
      {canManage ? (
        <section className="card dash-section">
          <button
            className="btn btn--primary"
            onClick={() => {
              resetForm();
              setView("builder");
            }}
          >
            + New subcontract
          </button>
        </section>
      ) : null}

      <label className="field">
        <span className="field__label">Status</span>
        <select
          className="field__input"
          aria-label="Status filter"
          value={statusFilter}
          onChange={(e) => {
            const s = e.target.value as "all" | api.SubcontractStatus;
            setStatusFilter(s);
            reloadSubs(s === "all" ? undefined : s);
          }}
        >
          <option value="all">All</option>
          {(Object.keys(STATUS_LABEL) as api.SubcontractStatus[]).map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
            </option>
          ))}
        </select>
      </label>

      {loading && subcontracts.length === 0 ? (
        <p className="muted">Loading…</p>
      ) : subcontracts.length === 0 ? (
        <div className="dash-empty">No subcontracts yet.</div>
      ) : (
        <div className="dash-grid">{subcontracts.map(trackerRow)}</div>
      )}
    </>
  );

  // ── Render: the builder wizard ─────────────────────────────────────────────────────────────────────
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
        {supersedesScId !== null ? <span className="dash-pill dash-pill--warn">Supersedes SC #{supersedesScId}</span> : null}
        {changeOrderOf !== null ? <span className="dash-pill dash-pill--warn">Change order CO{changeOrderOf.seq ?? "?"} of SC #{changeOrderOf.id}</span> : null}
      </div>

      {/* 1 — Job & project */}
      <section className="card dash-section" aria-label="Step 1 — Job and project">
        <h3 className="jha__section-title">1 · Job &amp; project</h3>
        <label className="field">
          <span className="field__label">Job</span>
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
          <FieldInput label="Job number (YYYY.NNN)" value={jobNo} onChange={(v) => setJobNo(v)} maxLength={8} />
          <FieldInput label="Site / phase" value={sitePhase} onChange={(v) => setSitePhase(v)} maxLength={4} />
        </div>
        {jobNo && !JOB_NO_RE.test(jobNo.trim()) ? (
          <p className="muted">The job number must look like 2023.126 (year.number).</p>
        ) : null}
        <div className="jha__grid">
          <FieldInput label="Project name" value={projectName} onChange={setProjectName} maxLength={256} />
          <FieldInput label="Owner entity" value={ownerEntity} onChange={setOwnerEntity} maxLength={256} />
        </div>
        <FieldInput label="Prime contractor" value={primeContractor} onChange={setPrimeContractor} maxLength={256} />
        <h4 className="dash-card__label">Project site</h4>
        <FieldInput label="Site name" value={siteName} onChange={setSiteName} maxLength={256} />
        <FieldInput label="Site address" value={siteAddress} onChange={setSiteAddress} maxLength={512} />
        <label className="field">
          <span className="field__label">Governing-law state</span>
          <select
            className="field__input"
            aria-label="Governing-law state"
            value={governingLawState}
            onChange={(e) => setGoverningLawState(e.target.value)}
          >
            <option value="">— governing-law state —</option>
            {(config?.governing_law_states ?? []).map((code) => (
              <option key={code} value={code}>
                {stateName(code)}
              </option>
            ))}
          </select>
        </label>
      </section>

      {/* 2 — Subcontractor (grouped by state) */}
      <section className="card dash-section" aria-label="Step 2 — Subcontractor">
        <h3 className="jha__section-title">2 · Subcontractor</h3>
        {statesPresent.length > 1 ? (
          <div className="mats-cat-filter" role="group" aria-label="Filter subcontractors by state">
            {statesPresent.map((st) => (
              <button
                key={st || "unassigned"}
                type="button"
                className={`mats-cat-filter__chip${stateFilter === st ? " mats-cat-filter__chip--active" : ""}`}
                aria-pressed={stateFilter === st}
                onClick={() => setStateFilter(stateFilter === st ? null : st)}
              >
                {st ? stateName(st) : "Unassigned"}
              </button>
            ))}
          </div>
        ) : null}
        {groupedSubs.length === 0 ? (
          <div className="dash-empty">No active subcontractors match the filters.</div>
        ) : (
          groupedSubs.map(([st, group]) => (
            <div key={st || "unassigned"}>
              <h4 className="dash-card__label">{st ? stateName(st) : "Unassigned"}</h4>
              <div className="po-vendor-pick">
                {group.map((s) => (
                  <div
                    key={s.sub_key}
                    className={`dash-row dash-row--click${s.sub_key === subKey ? " po-vendor-pick__row--selected" : ""}`}
                  >
                    <button
                      type="button"
                      className="po-vendor-pick__btn"
                      aria-pressed={s.sub_key === subKey}
                      onClick={() => onSubSelect(s)}
                    >
                      <span className="dash-table__name">{s.sub_name}</span>{" "}
                      {s.state ? <span className="dash-chip">{s.state.toUpperCase()}</span> : null}
                      {s.trades.slice(0, 4).map((t) => (
                        <span key={t} className="dash-chip">
                          {t}
                        </span>
                      ))}
                      {s.sub_key === subKey ? <span className="dash-pill dash-pill--ok">Selected</span> : null}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))
        )}
      </section>

      {/* 3 — Schedule of values */}
      <section className="card dash-section" aria-label="Step 3 — Schedule of values">
        <h3 className="jha__section-title">3 · Schedule of values</h3>
        <table className="dash-table po-lines">
          <thead>
            <tr>
              <th>#</th>
              <th>Item #</th>
              <th>Description</th>
              <th>Qty</th>
              <th>Unit</th>
              <th>Unit price</th>
              <th>Extended</th>
              <th />
            </tr>
          </thead>
          <tbody>{lines.map(lineRow)}</tbody>
        </table>
        <div className="jha__actions">
          <button type="button" className="btn btn--secondary" onClick={addLine}>
            + Add a line
          </button>
        </div>
      </section>

      {/* 4 — Contract price */}
      <section className="card dash-section" aria-label="Step 4 — Contract price">
        <h3 className="jha__section-title">4 · Contract price</h3>
        <div className="jha__grid">
          <label className="field">
            <span className="field__label">Contract price ($)</span>
            <input
              className="field__input"
              aria-label="Contract price dollars"
              value={contractPrice}
              maxLength={14}
              onChange={(e) => {
                touchMoney();
                setContractPrice(e.target.value);
              }}
            />
          </label>
          <label className="field">
            <span className="field__label">Retainage (%)</span>
            <input
              className="field__input"
              aria-label="Retainage percent"
              value={retainageBp}
              maxLength={6}
              onChange={(e) => setRetainageBp(e.target.value)}
            />
          </label>
        </div>
        <div className="mats-cat-filter" role="group" aria-label="Price basis">
          {PRICE_BASES.map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={`mats-cat-filter__chip${priceBasis === key ? " mats-cat-filter__chip--active" : ""}`}
              aria-pressed={priceBasis === key}
              onClick={() => setPriceBasis(key)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="po-totals" aria-label="Contract price panel">
          <div className="dash-card__row">
            <span className="dash-card__label">SOV subtotal</span>
            <span className="dash-table__name">{formatCents(displaySubtotal)}</span>
          </div>
          <div className="dash-card__row">
            <span className="dash-card__label">Contract price</span>
            <span className="dash-table__name">{formatCents(displayContract)}</span>
          </div>
          {gateReady ? (
            balances ? (
              <span className="dash-pill dash-pill--ok">SOV balances to the contract price</span>
            ) : (
              <span className="dash-pill dash-pill--warn">
                SOV subtotal {formatCents(displaySubtotal)} ≠ contract price {formatCents(displayContract)} — adjust a line
                or the price
              </span>
            )
          ) : (
            <p className="muted">Enter the contract price and schedule of values to check the balance.</p>
          )}
          {serverGate ? <p className="muted">Confirmed by the server for the saved draft.</p> : null}
        </div>
      </section>

      {/* 5 — Exhibit A & scope */}
      <section className="card dash-section" aria-label="Step 5 — Exhibit A and scope">
        <h3 className="jha__section-title">5 · Exhibit A &amp; scope</h3>
        <label className="field">
          <span className="field__label">Trade</span>
          <select
            className="field__input"
            aria-label="Trade"
            value={trade}
            onChange={(e) => void onTradeSelect(e.target.value)}
          >
            <option value="">— trade —</option>
            {trades.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field__label">Scope summary</span>
          <textarea
            className="field__textarea"
            aria-label="Scope summary"
            value={scopeSummary}
            maxLength={512}
            rows={2}
            onChange={(e) => setScopeSummary(e.target.value)}
          />
        </label>
        <label className="field">
          <span className="field__label">Exhibit A — the Work</span>
          <textarea
            className="field__textarea"
            aria-label="Exhibit A work text"
            value={exhibitAWorkText}
            maxLength={100000}
            rows={5}
            onChange={(e) => setExhibitAWorkText(e.target.value)}
          />
        </label>
        {articleIiPrefillTrade ? (
          <p className="muted">
            Exhibit A set from the {articleIiPrefillTrade} template — edit as needed, or pick another
            trade to replace it.
          </p>
        ) : null}
        <div className="jha__grid">
          <FieldInput label="Start date" value={startDate} onChange={setStartDate} type="date" />
          <FieldInput label="Completion date" value={completionDate} onChange={setCompletionDate} type="date" />
        </div>
      </section>

      {/* 6 — Terms & conditions */}
      <section className="card dash-section" aria-label="Step 6 — Terms">
        <h3 className="jha__section-title">6 · Terms &amp; conditions</h3>
        <label className="field">
          <span className="field__label">Terms profile{selectedSub?.default_terms_profile ? " (subcontractor default preselected)" : ""}</span>
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
          </>
        ) : null}
      </section>

      {/* 7 — Supersedes (optional) */}
      <section className="card dash-section" aria-label="Step 7 — Supersedes">
        <h3 className="jha__section-title">7 · Supersedes (optional)</h3>
        {changeOrderOf !== null ? (
          <p className="muted">
            This draft is change order CO{changeOrderOf.seq ?? "?"} of SC #{changeOrderOf.id} — the
            original subcontract stays in force; this document carries the change. Its number is
            minted at generate as the original&apos;s number + -CO{changeOrderOf.seq ?? "n"}.
          </p>
        ) : supersedesScId !== null ? (
          <p className="muted">
            This draft supersedes SC #{supersedesScId} — the old subcontract stays in force until this one is sent.
          </p>
        ) : supersedableSubs.length === 0 ? (
          <p className="muted">No sent or executed subcontracts to supersede.</p>
        ) : (
          <label className="field">
            <span className="field__label">Replace a sent/executed subcontract (clones it into a fresh draft)</span>
            <select
              className="field__input"
              aria-label="Supersede a subcontract"
              value=""
              onChange={(e) => {
                const id = parseInt(e.target.value, 10);
                if (Number.isSafeInteger(id)) void onSupersede(id);
              }}
            >
              <option value="">— sent / executed subcontract —</option>
              {supersedableSubs.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.sc_number ?? `#${p.id}`} · {p.job_name || p.job_no} · {formatCents(p.contract_price_cents)}
                </option>
              ))}
            </select>
          </label>
        )}
      </section>

      {/* 8 — Review & generate */}
      <section className="card dash-section" aria-label="Step 8 — Review and generate">
        <h3 className="jha__section-title">8 · Review &amp; generate</h3>
        <div className="dash-card__row">
          <span className="dash-card__label">Job</span>
          <span>
            {jobName || "—"} · {jobNo || "—"} · phase {sitePhase || "—"}
          </span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Project site</span>
          <span>{[siteName, siteAddress, stateName(governingLawState.trim().toUpperCase())].filter(Boolean).join(", ") || "—"}</span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Subcontractor</span>
          <span>{selectedSub ? `${selectedSub.sub_name} (${selectedSub.sub_key})` : "—"}</span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Schedule of values</span>
          <span>
            {validLines.length} of {lines.length} valid · {PRICE_BASES.find(([k]) => k === priceBasis)?.[1]}
          </span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Contract price</span>
          <span className="dash-table__name">
            {formatCents(displayContract)} (SOV {formatCents(displaySubtotal)}
            {balances ? " — balances" : " — does not balance"})
          </span>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Template</span>
          <label className="field">
            <span className="field__label" style={{ position: "absolute", left: -9999 }}>
              Subcontract template
            </span>
            <select
              className="field__input"
              aria-label="Subcontract template"
              value={templateFamily}
              onChange={(e) => setTemplateFamily(e.target.value as api.TemplateFamily)}
            >
              {TEMPLATE_FAMILIES.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="dash-card__row">
          <span className="dash-card__label">Terms</span>
          <span>{selectedTerms ? `${selectedTerms.label}${selectedTerms.current_version ? ` v${selectedTerms.current_version}` : ""}` : "—"}</span>
        </div>
        {scopeSummary ? (
          <div className="dash-card__row">
            <span className="dash-card__label">Scope summary</span>
            <span>{scopeSummary}</span>
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
              {busy ? "Working…" : "Generate subcontract"}
            </button>
          </div>
        ) : null}
      </section>
    </>
  );

  return (
    <PageShell onHome={onBack}>
      <h2 className="page__heading">Subcontracts</h2>
      <p className="dash__intro">
        Build a subcontract — schedule of values, contract price, and terms — then track it from draft
        through render, review, and send. Generated subcontracts queue for the Mac-side renderer;
        sending always goes through human approval.
      </p>

      {msg && <div className={`banner ${msg.ok ? "banner--ok" : "banner--err"}`}>{msg.text}</div>}
      {error && <div className="banner banner--err">{error}</div>}

      {view === "tracker" ? tracker : builder}
    </PageShell>
  );
}
