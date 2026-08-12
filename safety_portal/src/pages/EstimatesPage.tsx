import { useCallback, useEffect, useMemo, useState } from "react";
import * as est from "../lib/estimates";
import { fetchVendors, type Vendor } from "../lib/po";
import { fetchJobs, type Job } from "../lib/api";
import { useAuth } from "../lib/auth";
import { EstimateDispositionPage } from "./EstimateDispositionPage";

// Vendor-estimate importer E1/E3 (ADR-0004) — the office upload form + status tracker.
// One page, two faces (the PoBuilderPage tracker/builder shape): the TRACKER (every
// estimate from GET /api/po/estimates with status badges + per-status actions) and the
// DISPOSITION screen (EstimateDispositionPage) for a reviewable row. cap.po.manage gates
// the whole view (router VIEW_CAPS) AND every write affordance; the Worker re-gates every
// call (Invariant 2 — SPA gating is convenience, never the boundary).
//
// FOLDED (2026-07): renders as the "Vendor Estimates" TAB PANEL inside PurchaseOrdersPage
// (the hub owns the PageShell + tab strip). Cross-tab hooks: `reviewRequest` opens the
// disposition screen for an estimate picked over on the Orders tab ("New PO from a vendor
// estimate"), and a successful import hands the minted draft-PO id up via `onImported` so
// the hub flips back to Orders with the draft open for editing.
//
// The upload pools UNTRUSTED bytes send-free in D1; the Mac daemon screens (§34),
// classifies the doc type (invoices/AP reports are REFUSED from the PO path — visible,
// never silent), files the original to Box, and advances the row to needs_review /
// extracted — at which point the disposition screen is the human fidelity control.

const STATUS_PILL: Record<est.EstimateStatus, string> = {
  pending: "dash-pill",
  claimed: "dash-pill dash-pill--warn",
  refused: "dash-pill dash-pill--danger",
  needs_review: "dash-pill dash-pill--warn",
  extracted: "dash-pill dash-pill--warn",
  imported: "dash-pill dash-pill--ok",
  rejected: "dash-pill",
  superseded: "dash-pill",
};

const JOB_NO_RE = /^\d{4}\.\d{3}$/;

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

export function EstimatesPage({
  reviewRequest,
  onImported,
  onOpenPoTab,
}: {
  /** Nonce-keyed one-shot from the hub: open the disposition screen for this estimate. */
  reviewRequest?: { id: number; nonce: number } | null;
  /** A disposition import minted this draft PO — the hub flips to Orders and opens it. */
  onImported: (poId: number) => void;
  /** Jump to the Purchase Orders tab (the imported-row cross-link). */
  onOpenPoTab: () => void;
}) {
  const { user } = useAuth();
  const caps = user?.capabilities ?? [];
  const canManage = caps.includes("cap.po.manage"); // UI affordance only — the Worker re-gates

  const [rows, setRows] = useState<est.EstimateRow[]>([]);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [statusFilter, setStatusFilter] = useState<"all" | est.EstimateStatus>("all");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  /** A reviewable row opened into the disposition screen. */
  const [openId, setOpenId] = useState<number | null>(null);

  // ── Upload form state ──────────────────────────────────────────────────────────────────────────
  const [file, setFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState("");
  const [jobNo, setJobNo] = useState("");
  const [jobName, setJobName] = useState("");
  const [vendorKey, setVendorKey] = useState("");
  const [fileInputKey, setFileInputKey] = useState(0); // remount to clear the picker

  const reload = useCallback(
    (status?: est.EstimateStatus) => {
      setLoading(true);
      est
        .fetchEstimates(status)
        .then(setRows)
        .catch(() => setError("Failed to load estimates."))
        .finally(() => setLoading(false));
    },
    [],
  );

  useEffect(() => {
    reload();
    fetchVendors().then(setVendors).catch(() => setVendors([]));
    fetchJobs().then(setJobs).catch(() => setJobs([]));
  }, [reload]);

  // Cross-tab one-shot: the Orders tab picked a reviewable estimate ("New PO from a vendor
  // estimate") — open its disposition screen here. Nonce-keyed so the same estimate can be
  // re-requested after backing out.
  useEffect(() => {
    if (reviewRequest) {
      setMsg(null);
      setOpenId(reviewRequest.id);
    }
  }, [reviewRequest]);

  const vendorByKey = useMemo(() => new Map(vendors.map((v) => [v.vendor_key, v])), [vendors]);

  function onJobSelect(id: string) {
    setJobId(id);
    const job = jobs.find((j) => j.job_id === id);
    if (!job) return;
    setJobName(job.project_name);
    // Stored Evergreen number (0057) first; name-prefix parse stays the fallback. The regex is
    // anchored at the END OF THE NUMBER (`(?![\d.])`, not `$` — the name continues): the old
    // open-tailed form matched "2026.384.1 Coker Solar" and returned the truncated "2026.384",
    // a wrong-but-plausible number for a DIFFERENT site of the same project. Only job_no is
    // needed here — the site travels with job_id — but the parse must still not lie.
    const m = /^(\d{4}\.\d{3})(?:\.(\d+))?(?![\d.])/.exec((job.project_name ?? "").trim());
    setJobNo(job.job_no || (m ? m[1] : ""));
  }

  async function onUpload() {
    if (busy || !file) return;
    if (!JOB_NO_RE.test(jobNo.trim())) {
      setMsg({ ok: false, text: "Enter the job number as YYYY.NNN before uploading." });
      return;
    }
    const dot = file.name.lastIndexOf(".");
    const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    const mime = est.ESTIMATE_MIME_BY_EXT[ext];
    if (!mime) {
      setMsg({ ok: false, text: `"${file.name}" isn't an allowed type — PDF, JPG, PNG, DOCX, or XLSX.` });
      return;
    }
    if (file.size > est.ESTIMATE_MAX_BYTES) {
      setMsg({ ok: false, text: `"${file.name}" is over the 10 MB limit.` });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const b64 = await fileToB64(file);
      await est.uploadEstimate({
        job_no: jobNo.trim(),
        // 0069 — the page has held this in state since the job dropdown was added and simply
        // dropped it. Without it the disposition screen cannot tell MH405 from OG593 and every
        // estimate-imported PO composes at site 0.
        ...(jobId ? { job_id: jobId } : {}),
        job_name: jobName.trim() || undefined,
        vendor_key: vendorKey || undefined,
        filename: file.name,
        mime,
        data_b64: b64,
      });
      setMsg({ ok: true, text: `"${file.name}" uploaded — it will appear as Needs review once screened.` });
      setFile(null);
      setFileInputKey((k) => k + 1);
      reload(statusFilter === "all" ? undefined : statusFilter);
    } catch (err) {
      const code = err && typeof err === "object" && "code" in err ? (err as { code: string | null }).code : null;
      if (code === "duplicate_estimate") {
        setMsg({ ok: false, text: "This exact file is already in the pipeline (duplicate estimate)." });
      } else {
        setMsg({ ok: false, text: err instanceof Error ? err.message : "Upload failed." });
      }
    }
    setBusy(false);
  }

  // ── Disposition face ───────────────────────────────────────────────────────────────────────────
  if (openId !== null) {
    return (
      // key IS LOAD-BEARING (adversarial review, 2026-07-20): the cross-tab reviewRequest can
      // retarget openId while a disposition is already mounted (panel keep-alive). Without a
      // remount, estimate A's loaded-preview evidence, manual Tier-3 lines, ship-to state, and
      // site phase would carry into estimate B's import — bypassing the ADR-0004 decision-3
      // fidelity gate. The key forces a fresh instance (and a fresh gate) per estimate.
      <EstimateDispositionPage
        key={openId}
        estimateId={openId}
        onClose={(notice, importedPoId) => {
          setOpenId(null);
          reload(statusFilter === "all" ? undefined : statusFilter);
          if (importedPoId !== undefined) {
            // A draft PO was minted — hand off to the hub (flips to Orders, opens the draft).
            // The local banner is skipped: the Orders tab shows its own imported banner.
            onImported(importedPoId);
          } else if (notice) {
            setMsg(notice);
          }
        }}
      />
    );
  }

  // ── Tracker face ───────────────────────────────────────────────────────────────────────────────
  function row(r: est.EstimateRow) {
    const vendor = r.vendor_key ? vendorByKey.get(r.vendor_key) : undefined;
    const reviewable = r.status === "extracted" || r.status === "needs_review";
    return (
      <section key={r.id} className="card">
        <div className="dash-card__head">
          <h3 className="dash-card__title">{r.filename}</h3>
          <span className={STATUS_PILL[r.status] ?? "dash-pill"}>
            {est.ESTIMATE_STATUS_LABEL[r.status] ?? r.status}
          </span>
        </div>
        <div className="dash-card__sub">
          {r.job_no} · {r.job_name || "—"} · {vendor?.vendor_name ?? r.vendor_key ?? "vendor unconfirmed"}
          {r.doc_type ? ` · ${r.doc_type}` : ""}
        </div>
        {r.detail ? <div className="dash-card__row muted">{r.detail}</div> : null}
        {r.po_id !== null ? (
          <div className="dash-card__row">
            <span className="muted">Imported into PO draft #{r.po_id}</span>{" "}
            <button className="btn btn--secondary" onClick={onOpenPoTab}>
              Open Purchase Orders
            </button>
          </div>
        ) : null}
        {canManage && reviewable ? (
          <div className="dash-card__row">
            <button className="btn btn--primary" disabled={busy} onClick={() => setOpenId(r.id)}>
              Review &amp; disposition
            </button>
          </div>
        ) : null}
      </section>
    );
  }

  const STATUSES = Object.keys(est.ESTIMATE_STATUS_LABEL) as est.EstimateStatus[];

  return (
    <>
      <p className="dash__intro">
        Upload vendor quotes and estimates received at the office. Each document is screened and
        classified on the Mac (invoices and AP reports are refused from the PO path), then reviewed
        line-by-line here before anything becomes a draft purchase order.
      </p>

      {msg && <div className={`banner ${msg.ok ? "banner--ok" : "banner--err"}`}>{msg.text}</div>}
      {error && <div className="banner banner--err">{error}</div>}

      {canManage ? (
        <section className="card dash-section" aria-label="Upload an estimate">
          <h3 className="jha__section-title">Upload an estimate</h3>
          <label className="field">
            <span className="field__label">Job</span>
            <select
              className="field__input"
              aria-label="Job"
              value={jobId}
              onChange={(e) => onJobSelect(e.target.value)}
            >
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
              <input
                className="field__input"
                value={jobNo}
                maxLength={8}
                onChange={(e) => setJobNo(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field__label">Vendor (optional — confirmed at review)</span>
              <select
                className="field__input"
                aria-label="Vendor"
                value={vendorKey}
                onChange={(e) => setVendorKey(e.target.value)}
              >
                <option value="">— not sure yet —</option>
                {vendors.map((v) => (
                  <option key={v.vendor_key} value={v.vendor_key}>
                    {v.vendor_name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="field">
            <span className="field__label">Document (PDF, JPG, PNG, DOCX, or XLSX — 10 MB max)</span>
            <input
              key={fileInputKey}
              className="field__input"
              type="file"
              accept={est.ESTIMATE_ACCEPT}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>
          <div className="jha__actions">
            <button
              className="btn btn--primary"
              disabled={busy || !file || !JOB_NO_RE.test(jobNo.trim())}
              onClick={() => void onUpload()}
            >
              {busy ? "Uploading…" : "Upload estimate"}
            </button>
          </div>
        </section>
      ) : null}

      <label className="field">
        <span className="field__label">Status</span>
        <select
          className="field__input"
          aria-label="Status filter"
          value={statusFilter}
          onChange={(e) => {
            const s = e.target.value as "all" | est.EstimateStatus;
            setStatusFilter(s);
            reload(s === "all" ? undefined : s);
          }}
        >
          <option value="all">All</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {est.ESTIMATE_STATUS_LABEL[s]}
            </option>
          ))}
        </select>
      </label>

      {loading && rows.length === 0 ? (
        <p className="muted">Loading…</p>
      ) : rows.length === 0 ? (
        <div className="dash-empty">No estimates yet.</div>
      ) : (
        <div className="dash-grid">{rows.map(row)}</div>
      )}
    </>
  );
}
