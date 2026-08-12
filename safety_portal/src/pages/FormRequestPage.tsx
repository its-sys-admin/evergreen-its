import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../lib/api";
import { AppHeader } from "../components/AppHeader";
import { BackHomeNav } from "../components/BackHomeNav";

/** Render a "YYYY-MM" work-month as a human "Month YYYY" label (e.g. "June 2026"). */
function monthLabel(ym: string): string {
  const [y, m] = ym.split("-").map(Number);
  if (!y || !m) return ym;
  return new Date(y, m - 1, 1).toLocaleString(undefined, { month: "long", year: "numeric" });
}

/**
 * PR-5/PR-6 Form Request — browse an ACTIVE job's filed safety forms and download them on the
 * spot (random-inspection use case). Any authenticated account may browse + request; a
 * requested download is bound to THIS account for 24h (the Worker re-gates: a different
 * account gets 404). PR-6 cascade: pick a job → pick a Month-Year → (optionally) a form type →
 * multi-select forms → "Request selected" → each row turns into the PR-4 "Preparing… →
 * Download" poll. The month step keeps a year-long job's hundreds of filed forms from dumping
 * in one flat table. No browser render: the downloaded PDF is the Box-filed copy, byte-identical.
 */
export function FormRequestPage({ onBack }: { onBack: () => void }) {
  const [jobs, setJobs] = useState<api.Job[]>([]);
  const [jobId, setJobId] = useState("");
  const [months, setMonths] = useState<api.MonthBucket[]>([]);
  const [formCodes, setFormCodes] = useState<string[]>([]);
  const [selectedMonth, setSelectedMonth] = useState("");
  const [selectedFormCode, setSelectedFormCode] = useState("");
  const [filed, setFiled] = useState<api.FiledForm[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [requested, setRequested] = useState<Set<string>>(new Set());

  useEffect(() => {
    api.fetchJobs().then(setJobs).catch(() => setJobs([]));
  }, []);

  // Fetch the filtered filed-forms table. Only fetches once a month is chosen (the cascade
  // requires a month before any documents are shown). form_code "" means "All forms".
  const loadFiled = useCallback(async (id: string, month: string, formCode: string) => {
    setChecked(new Set());
    setRequested(new Set());
    setFiled(null);
    if (!id || !month) return;
    setLoading(true);
    try {
      const rows = await api.fetchFiled(id, { month, form_code: formCode || undefined });
      setFiled(rows);
      // Rows this account already requested (server-side, still live) start in the download flow.
      setRequested(new Set(rows.filter((r) => r.requested).map((r) => r.submission_uuid)));
    } finally {
      setLoading(false);
    }
  }, []);

  // Job select → load the month + form dropdowns; reset month/form/table.
  const onJobChange = useCallback(async (id: string) => {
    setJobId(id);
    setSelectedMonth("");
    setSelectedFormCode("");
    setMonths([]);
    setFormCodes([]);
    setFiled(null);
    setChecked(new Set());
    setRequested(new Set());
    if (!id) return;
    try {
      const { months: m, form_codes } = await api.fetchFiledMonths(id);
      setMonths(m);
      setFormCodes(form_codes);
    } catch {
      setMonths([]);
      setFormCodes([]);
    }
  }, []);

  const onMonthChange = useCallback(
    (month: string) => {
      setSelectedMonth(month);
      void loadFiled(jobId, month, selectedFormCode);
    },
    [jobId, selectedFormCode, loadFiled],
  );

  const onFormChange = useCallback(
    (formCode: string) => {
      setSelectedFormCode(formCode);
      if (selectedMonth) void loadFiled(jobId, selectedMonth, formCode);
    },
    [jobId, selectedMonth, loadFiled],
  );

  const toggle = useCallback((uuid: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(uuid)) next.delete(uuid);
      else next.add(uuid);
      return next;
    });
  }, []);

  const onRequestSelected = useCallback(async () => {
    const uuids = [...checked];
    if (uuids.length === 0) return;
    try {
      await api.requestPdfs(uuids);
      setRequested((prev) => new Set([...prev, ...uuids]));
      setChecked(new Set());
    } catch {
      // A hard failure surfaces per row; keep the selection so the user can retry.
    }
  }, [checked]);

  return (
    <div className="page">
      <AppHeader />
      <BackHomeNav onHome={onBack} />
      <main className="page__main">
        <h1 className="page__heading">Form Request</h1>
        <p className="muted">
          Browse a job's filed safety forms and download them on the spot. A download stays
          available to you for 24 hours.
        </p>

        <label className="field">
          <span className="field__label">Job</span>
          <select
            className="field__input"
            aria-label="Job"
            value={jobId}
            onChange={(e) => void onJobChange(e.target.value)}
          >
            <option value="">Select a job…</option>
            {jobs.map((j) => (
              <option key={j.job_id} value={j.job_id}>
                {j.project_name}
              </option>
            ))}
          </select>
        </label>

        {jobId && months.length === 0 ? (
          <p className="muted">No filed forms for this job yet.</p>
        ) : null}

        {jobId && months.length > 0 ? (
          <>
            <label className="field">
              <span className="field__label">Month</span>
              <select
                className="field__input"
                aria-label="Month"
                value={selectedMonth}
                onChange={(e) => onMonthChange(e.target.value)}
              >
                <option value="">Select a month…</option>
                {months.map((mb) => (
                  <option key={mb.month} value={mb.month}>
                    {monthLabel(mb.month)} ({mb.count})
                  </option>
                ))}
              </select>
            </label>

            <label className="field">
              <span className="field__label">Form type</span>
              <select
                className="field__input"
                aria-label="Form type"
                value={selectedFormCode}
                onChange={(e) => onFormChange(e.target.value)}
              >
                <option value="">All forms</option>
                {formCodes.map((fc) => (
                  <option key={fc} value={fc}>
                    {fc}
                  </option>
                ))}
              </select>
            </label>
          </>
        ) : null}

        {loading ? <p className="muted">Loading…</p> : null}
        {selectedMonth && filed && filed.length === 0 ? (
          <p className="muted">No filed forms for this month.</p>
        ) : null}

        {filed && filed.length > 0 ? (
          <>
            <table>
              <thead>
                <tr>
                  <th aria-label="select" />
                  <th>Form</th>
                  <th>Work date</th>
                  <th>Download</th>
                </tr>
              </thead>
              <tbody>
                {filed.map((f) => {
                  const isReq = requested.has(f.submission_uuid);
                  return (
                    <tr key={f.submission_uuid}>
                      <td>
                        {isReq ? null : (
                          <input
                            type="checkbox"
                            aria-label={`select ${f.form_code} ${f.work_date}`}
                            checked={checked.has(f.submission_uuid)}
                            onChange={() => toggle(f.submission_uuid)}
                          />
                        )}
                      </td>
                      <td>{f.form_code}</td>
                      <td>{f.work_date}</td>
                      <td>
                        <RowDownload uuid={f.submission_uuid} requested={isReq} initiallyReady={f.ready} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="jha__actions">
              <button
                className="btn btn--primary"
                disabled={checked.size === 0}
                onClick={() => void onRequestSelected()}
              >
                Request selected{checked.size ? ` (${checked.size})` : ""}
              </button>
            </div>
          </>
        ) : null}
      </main>
    </div>
  );
}

type RowPhase = "idle" | "preparing" | "ready";

/**
 * One filed-form row's download cell: idle → (parent requests) → "Preparing…" 5s poll →
 * "Download". Mirrors FormFillPage's PdfDownload poll (recursive setTimeout + active flag +
 * useRef timer + cleanup). Driven by the `requested` prop the parent flips on batch-request.
 */
function RowDownload({
  uuid,
  requested,
  initiallyReady,
}: {
  uuid: string;
  requested: boolean;
  initiallyReady: boolean;
}) {
  const [phase, setPhase] = useState<RowPhase>(initiallyReady ? "ready" : requested ? "preparing" : "idle");
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // When the parent batch-requests this row, move it into the preparing/poll state.
  useEffect(() => {
    if (requested) setPhase((p) => (p === "idle" ? "preparing" : p));
  }, [requested]);

  useEffect(() => {
    if (phase !== "preparing") return;
    let active = true;
    const tick = async () => {
      if (!active) return;
      try {
        const s = await api.pdfStatus(uuid);
        if (!active) return;
        if (s.ready) {
          setExpiresAt(s.expires_at);
          setPhase("ready");
          return; // ready — stop polling
        }
      } catch {
        // Transient status error: keep polling.
      }
      if (!active) return;
      timer.current = setTimeout(() => void tick(), 5000);
    };
    void tick();
    return () => {
      active = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [phase, uuid]);

  if (phase === "ready") {
    const until = expiresAt ? new Date(expiresAt * 1000).toLocaleString() : null;
    return (
      <button className="btn btn--secondary" onClick={() => api.downloadPdf(uuid)}>
        Download{until ? ` (until ${until})` : ""}
      </button>
    );
  }
  if (phase === "preparing") {
    return (
      <span className="muted" role="status">
        Preparing… (usually under 2 min)
      </span>
    );
  }
  return <span className="muted">—</span>;
}
