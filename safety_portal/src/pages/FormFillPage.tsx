import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, ReactNode, SetStateAction } from "react";
import { AppHeader } from "../components/AppHeader";
import { useAuth } from "../lib/auth";
import * as api from "../lib/api";
import { formCatalog, getDefinition, WORKFLOWS_ORDERED } from "../forms/registry";
import { FormRenderer, initialValues, type FormValues } from "../forms/FormRenderer";
import { useSubmissionId } from "./useSubmissionId";

function todayIso(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * The daily-form fill flow (unified shell, P1). Opened from the home "Submit a form"
 * card as <FormFillPage onBack={…} />. Admins KEEP submit-as here: the "filled out as"
 * account selector renders for admins (gated on role, server re-validates), so an admin
 * can attribute a submission to any enabled account — unchanged by the home unification.
 * (The optional `tabBar` mount is retained for any caller that renders its own nav above
 * the form.)
 */
/** Deep-link prefill (P4 S4 loop-closure): a form_linked/inspection checklist item opens FormFillPage
 * with the instance's job + the item's form + the instance date pre-selected, so filing the linked form
 * auto-checks the item on the next checklist read. All fields optional; each seeds the matching state.
 * S5 auto-rollup adds `values`: an assembled Daily Report draft (FormRenderer FormValues), merged over
 * the form's empty defaults on first render so the manager reviews/edits a pre-populated form. */
export interface FormPrefill {
  jobId?: string;
  parentCode?: string;
  variantCode?: string;
  workDate?: string;
  values?: FormValues;
}

/** R3: the deep-link return target — present ONLY when App captured an originating view in openForm
 * (a checklist deep-link / rollup draft). Drives the Submitted screen's primary "Back to My Tasks"
 * and the pre-submit "← Back …" control; absent for a home-card form fill (default flow unchanged). */
export interface FormReturnTo {
  label: string;
  onReturn: () => void;
}

const DISCARD_PROMPT = "Discard this form? Your entries haven't been submitted.";

export function FormFillPage({
  onBack,
  tabBar,
  prefill,
  returnTo,
  onDirtyChange,
}: {
  onBack?: () => void;
  tabBar?: ReactNode;
  prefill?: FormPrefill;
  returnTo?: FormReturnTo;
  /** R3: reports unsaved-input state up (dirty = any form field touched since load/submit) so App's
   * popstate handler can confirm before discarding on hardware back. */
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { user, logout } = useAuth();
  const isAdmin = user?.role === "admin";
  // Pool eligibility mirrors the Worker's DAILY_PHOTO_ROLES (manager/admin — decision 9,
  // 2026-08-27: submitters keep their 403; the SPA half is the honest placeholder). The
  // Worker is the boundary — this gate is UX, exactly like the Daily tab's canFileDaily.
  const canPool = user?.role === "manager" || user?.role === "admin";
  const me = user?.username ?? "";
  const catalog = useMemo(() => formCatalog(), []);

  const [jobs, setJobs] = useState<api.Job[]>([]);
  const [jobsErr, setJobsErr] = useState<string | null>(null);
  const [jobId, setJobId] = useState(prefill?.jobId ?? "");
  const [parentCode, setParentCode] = useState(prefill?.parentCode ?? "");
  const [variantCode, setVariantCode] = useState(prefill?.variantCode ?? "");
  const [workDate, setWorkDate] = useState(prefill?.workDate ?? todayIso());

  // Admin "filled out as" — the account this submission is attributed to (default =
  // self). Only admins ever see / send this; submitters always submit as themselves.
  // The list of accounts is fetched once when an admin opens the form. The server
  // re-validates the choice (role + target enabled), so this selector is convenience,
  // never the boundary.
  const [accounts, setAccounts] = useState<api.Account[]>([]);
  const [filledOutAs, setFilledOutAs] = useState("");

  const [values, setValues] = useState<FormValues>({});
  const [amendsUuid, setAmendsUuid] = useState<string | null>(null);
  const [prefillable, setPrefillable] = useState<api.RecentSubmission | null>(null);
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [submittedAs, setSubmittedAs] = useState<string | null>(null);
  // Receipt fields captured AT submit success (the submit response carries no
  // timestamp, and the submission id renews on reset — so snapshot both here).
  const [submittedUuid, setSubmittedUuid] = useState<string | null>(null);
  const [submittedAt, setSubmittedAt] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  // R3: any form field touched since load/submit — drives the beforeunload guard + the confirm on
  // the pre-submit return control, and is reported up via onDirtyChange for App's popstate guard.
  const [dirty, setDirty] = useState(false);
  // Stable across retries (lost-ACK idempotency); renewed only on a new submission (reset).
  const { submissionUuid, renew: renewSubmissionId } = useSubmissionId();

  // R3: report dirtiness up (mount reports false, so a fresh form clears App's stale ref).
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // R3: native beforeunload guard while dirty (tab close / reload — popstate is App's half).
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = ""; // legacy engines require a set returnValue to show the prompt
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  // R3: FormRenderer writes go through this wrapper so any field edit marks the form dirty; the
  // programmatic (re)initializations below call setValues directly and stay clean.
  const editValues: Dispatch<SetStateAction<FormValues>> = (v) => {
    setDirty(true);
    setValues(v);
  };

  useEffect(() => {
    api.fetchJobs().then(setJobs).catch((e) => setJobsErr(e instanceof Error ? e.message : "load failed"));
  }, []);

  // Admins only: load the account list once and default the attribution to self.
  // Submitters never call /api/admin/users (it would 403); they always submit as me.
  useEffect(() => {
    if (!isAdmin) return;
    setFilledOutAs(me);
    api.listAccounts().then(setAccounts).catch(() => setAccounts([]));
  }, [isAdmin, me]);

  const parent = catalog.find((p) => p.parent_form_code === parentCode) ?? null;
  const formCode = parent ? (parent.variants.length ? variantCode : (parent.form_code ?? "")) : "";
  const def = formCode ? getDefinition(formCode) : null;
  // R3: the human name of the deep-linked form — drives the heading for a prefilled fill. Dismissed
  // on "Submit another" (reset) so the heading doesn't keep naming the old form over an open picker.
  const [prefillDismissed, setPrefillDismissed] = useState(false);
  const prefillFormName = !prefillDismissed && prefill?.parentCode
    ? catalog.find((p) => p.parent_form_code === prefill.parentCode)?.name ?? null
    : null;

  // S5: an assembled draft to seed ONCE, on the first form definition load (the prefilled form the
  // manager opened into). Cleared after the first apply so switching forms afterward resets cleanly —
  // and the App-level keyed remount guarantees this ref is fresh for each deep-link open.
  const pendingPrefillValues = useRef<FormValues | null>(prefill?.values ?? null);

  // (Re)initialize the fill state whenever the chosen form changes. On the first load, merge any
  // assembled prefill values (S5 rollup draft) over the empty defaults so the form opens pre-populated.
  useEffect(() => {
    const d = formCode ? getDefinition(formCode) : null;
    if (d && pendingPrefillValues.current) {
      setValues({ ...initialValues(d), ...pendingPrefillValues.current });
      pendingPrefillValues.current = null; // one-shot; later form switches reset to empty defaults
    } else {
      setValues(d ? initialValues(d) : {});
    }
    setAmendsUuid(null);
  }, [formCode]);

  // ── Pool-ref hygiene (Photos program, 2026-08-27) ─────────────────────────────
  // Pool references (values.additional_photos) are bound to the (job, work date) they were
  // uploaded under — carried across a scope change they would 4xx at submit
  // (unknown_photo_ref: the claim checks job_id + work_date + uploader). Strip them whenever
  // EITHER changes. The ref tracks the last-seen scope (not a first-run boolean: StrictMode
  // double-invokes mount effects, and a boolean guard would strip a deep-link prefill's
  // seeded refs on the second invoke); the first REAL scope is recorded without stripping.
  // loadAmend changes neither jobId nor workDate, so an amend load never fires this — its
  // refs legitimately belong to the loaded submission's scope.
  const poolScope = useRef<string | null>(null);
  useEffect(() => {
    const scope = `${jobId}\n${workDate}`;
    if (poolScope.current === scope) return; // StrictMode re-invoke / no-op re-run
    const isFirst = poolScope.current === null;
    poolScope.current = scope;
    if (isFirst) return;
    // Programmatic setValues (not editValues): a hygiene strip is not user input — it must
    // not arm the dirty guard on its own.
    setValues((v) => {
      const refs = v.additional_photos;
      if (!Array.isArray(refs) || refs.length === 0) return v;
      const next = { ...v };
      delete next.additional_photos;
      return next;
    });
  }, [jobId, workDate]);

  // Amend prefill: when job + form + work-date are all set, look for a prior submission.
  useEffect(() => {
    setPrefillable(null);
    if (jobId && formCode && workDate) {
      let active = true;
      api.fetchRecent(jobId, formCode, workDate).then((r) => {
        if (active) setPrefillable(r);
      }).catch(() => {});
      return () => {
        active = false;
      };
    }
  }, [jobId, formCode, workDate]);

  function loadAmend() {
    const d = formCode ? getDefinition(formCode) : null;
    if (!prefillable || !d) return;
    setValues({ ...initialValues(d), ...prefillable.values });
    setAmendsUuid(prefillable.submission_uuid);
    setPrefillable(null);
  }

  async function onSubmit() {
    if (!def || !jobId || !workDate) return;
    setBusy(true);
    setError(null);
    // Only an admin attributes to someone else; for a self-submit (or any submitter)
    // we omit submitted_as entirely so the server takes the normal self-submit path.
    const attributeTo = isAdmin && filledOutAs && filledOutAs !== me ? filledOutAs : undefined;
    try {
      await api.submitForm({
        job_id: jobId,
        form_code: def.form_code,
        variant_label: def.variant_label,
        work_date: workDate,
        values,
        submission_uuid: submissionUuid,
        amends_uuid: amendsUuid,
        submitted_as: attributeTo,
      });
      setSubmittedAs(attributeTo ?? null);
      // Snapshot the receipt identity BEFORE reset() renews the submission id. The
      // submit response carries no server timestamp, so capture a client one here.
      setSubmittedUuid(submissionUuid);
      setSubmittedAt(new Date());
      setSubmitted(true);
      setDirty(false); // filed — nothing unsaved to guard
    } catch (e) {
      setError(e instanceof Error ? e.message : "Submission failed.");
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setPrefillDismissed(true);
    setSubmitted(false);
    setSubmittedAs(null);
    setSubmittedUuid(null);
    setSubmittedAt(null);
    setParentCode("");
    setVariantCode("");
    setValues({});
    setAmendsUuid(null);
    setDirty(false);
    renewSubmissionId(); // a fresh id for the NEXT submission (the prior one succeeded)
    // Reset the attribution back to self for the next submission (admins only).
    if (isAdmin) setFilledOutAs(me);
  }

  // R3: pre-submit return to the deep-link origin (confirm first when fields were touched).
  function onReturnGuarded() {
    if (!returnTo) return;
    if (dirty && !window.confirm(DISCARD_PROMPT)) return;
    returnTo.onReturn();
  }

  if (submitted) {
    // Surface the job (not just the date) in the confirmation — a PM filing for
    // several jobs needs to see WHICH one was recorded. jobId/jobs are still in
    // scope (reset() clears the form, not the job), so the lookup resolves; the
    // fallback drops the clause if the job somehow isn't in the loaded list.
    const projectName = jobs.find((j) => j.job_id === jobId)?.project_name;
    return (
      <div className="page">
        <AppHeader />
        {tabBar}
        <main className="page__main">
          <div className="card centered-card">
            <h1 className="page__heading">Submitted ✓</h1>
            <p className="muted">
              Your {def?.form_name} for {projectName ? `${projectName} on ` : ""}
              {workDate} was submitted. The office will confirm it once it’s filed.
            </p>

            {/* Receipt — a record of exactly what was filed. `submittedAs` carries the
                admin "filled out as" attribution (the true actor is still logged
                server-side); both the id and the timestamp were snapshotted at submit
                success, before reset() renews the submission id. */}
            <dl className="receipt">
              <div>
                <dt>Form</dt>
                <dd>{def?.form_name ?? "—"}</dd>
              </div>
              {projectName ? (
                <div>
                  <dt>Job</dt>
                  <dd>{projectName}</dd>
                </div>
              ) : null}
              <div>
                <dt>Work date</dt>
                <dd>{workDate}</dd>
              </div>
              {submittedAt ? (
                <div>
                  <dt>Submitted at</dt>
                  <dd>{submittedAt.toLocaleString()}</dd>
                </div>
              ) : null}
              {submittedAs ? (
                <div>
                  <dt>Submitted as</dt>
                  <dd><strong>{submittedAs}</strong></dd>
                </div>
              ) : null}
              {submittedUuid ? (
                <div>
                  <dt>Submission ID</dt>
                  <dd><code>{submittedUuid}</code></dd>
                </div>
              ) : null}
            </dl>

            {/* Request-driven canonical PDF download (PR-4 Part A): nothing is cached
                until the PM clicks "Make available for download". */}
            {submittedUuid ? <PdfDownload uuid={submittedUuid} /> : null}

            {returnTo ? (
              // R3 — deep-link round trip: one tap back to the originating view. The checklist item
              // auto-closes via the server loop-closure reconcile on that page's next load.
              <>
                <p className="muted">
                  Filing this form checks off your checklist item on the next load.
                </p>
                <div className="jha__actions">
                  <button className="btn btn--primary" onClick={returnTo.onReturn}>{returnTo.label}</button>
                  <button className="btn btn--secondary" onClick={reset}>Submit another</button>
                </div>
              </>
            ) : (
              <div className="jha__actions">
                <button className="btn btn--primary" onClick={reset}>Submit another</button>
                {onBack ? <button className="btn btn--secondary" onClick={onBack}>Home</button> : null}
              </div>
            )}
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="page">
      <AppHeader
        action={<button className="btn btn--ghost" onClick={() => void logout()}>Sign out</button>}
      />
      {tabBar}
      <main className="page__main">
        {returnTo ? (
          // R3 — pre-submit back/cancel returns to the deep-link origin (not Home); confirms first
          // when fields were touched.
          // --secondary, NOT --ghost: this sits on the light page ground (--c-surface), where the
          // header ghost's white text + 60%-white border is invisible (~1.04:1). --secondary is the
          // canonical in-page back control (FieldOpsJobTracker, EquipmentManageView, ...), and it
          // matches the Submitted-screen twin below that already renders green.
          <div className="dash-back-btn">
            <button type="button" className="btn btn--secondary" onClick={onReturnGuarded}>
              ← {returnTo.label}
            </button>
          </div>
        ) : null}
        {/* R3 — a deep-linked fill names the form it opened into instead of the generic heading. */}
        <h1 className="page__heading">{prefillFormName ?? "New safety form"}</h1>

        <section className="card fr__select">
          {prefill?.jobId && jobs.length === 0 && !jobsErr ? (
            // R3 — while jobs load, the deep-linked job renders as read-only text, not a blank select.
            <div className="field">
              <span className="field__label">Job *</span>
              <div className="field__input" aria-label="Job (from your checklist)">{prefill.jobId}</div>
            </div>
          ) : (
            <label className="field">
              <span className="field__label">Job *</span>
              <select className="field__input" value={jobId} onChange={(e) => setJobId(e.target.value)}>
                <option value="">Select a job…</option>
                {jobs.map((j) => <option key={j.job_id} value={j.job_id}>{j.project_name}</option>)}
              </select>
            </label>
          )}
          {jobsErr ? <p className="login__error" role="alert">{jobsErr}</p> : null}

          <label className="field">
            <span className="field__label">Form *</span>
            <select className="field__input" value={parentCode}
              onChange={(e) => { setParentCode(e.target.value); setVariantCode(""); }}>
              <option value="">Select a form…</option>
              {WORKFLOWS_ORDERED.map((w) => {
                // D2 (SOP daily form): parents launched from a dedicated surface (catalog
                // launch:"daily-tab" — the Daily Report lives on My Tasks → Daily report) are
                // HIDDEN from this CREATE picker only. `catalog` itself stays complete, so
                // deep-link prefills and the Form Request / download / history surfaces (which
                // the office still uses to retrieve filed dailies) are untouched.
                const inCat = catalog.filter((p) => p.category === w.id && !p.launch);
                if (inCat.length === 0) return null;
                return (
                  <optgroup key={w.id} label={w.label}>
                    {inCat.map((p) => (
                      <option key={p.parent_form_code} value={p.parent_form_code}>{p.name}</option>
                    ))}
                  </optgroup>
                );
              })}
            </select>
          </label>

          {parent && parent.variants.length ? (
            <label className="field">
              <span className="field__label">Type *</span>
              <select className="field__input" value={variantCode} onChange={(e) => setVariantCode(e.target.value)}>
                <option value="">Select a type…</option>
                {parent.variants.map((v) => <option key={v.form_code} value={v.form_code}>{v.variant_label}</option>)}
              </select>
            </label>
          ) : null}

          <label className="field">
            <span className="field__label">Work date *</span>
            <input className="field__input" type="date" value={workDate} onChange={(e) => setWorkDate(e.target.value)} />
          </label>

          {isAdmin ? (
            // Admin-only "Filled out as": attribute this submission to another account.
            // Default is the admin's own username. Submitters never see this (it isn't
            // rendered), and even if a forged value reached the server it is rejected
            // there (Invariant 2 — the selector is convenience, not the gate).
            <label className="field">
              <span className="field__label">Filled out as</span>
              <select
                className="field__input"
                value={filledOutAs}
                onChange={(e) => setFilledOutAs(e.target.value)}
              >
                <option value={me}>{me} (you)</option>
                {accounts
                  .filter((a) => a.username !== me)
                  .map((a) => (
                    <option key={a.username} value={a.username}>{a.username}</option>
                  ))}
              </select>
            </label>
          ) : null}
        </section>

        {prefillable ? (
          <div className="jha__notice" role="status">
            <strong>A submission already exists</strong> for this job, form, and date.{" "}
            <button className="btn btn--secondary" onClick={loadAmend}>Load & amend it</button>
          </div>
        ) : null}

        {def ? (
          <>
            {amendsUuid ? <p className="jha__notice"><strong>Amending</strong> a previous submission.</p> : null}
            <section className="card">
              {/* onDraftDirty: a signature does not reach setValues until Done, so a user
                  whose FIRST action is signing was invisible to BOTH guards above — no
                  beforeunload on tab close, no popstate confirm on hardware Back — and the
                  form was discarded silently. The first completed stroke arms them. */}
              {/* Generic pool adapter (Photos program, 2026-08-27 — mirrors DailyReportTab):
                  scope = the selected job + work date; supplied only for pool-eligible roles
                  (manager/admin), so submitters see the honest placeholder instead of an
                  uploader whose every request the Worker would 403. amendsUuid threads the
                  load-&-amend target so the filed report's own claimed rows chip "on file". */}
              <FormRenderer def={def} values={values} setValues={editValues}
                additionalPhotos={jobId && workDate && canPool ? { jobId, workDate, amendsUuid } : undefined}
                onDraftDirty={() => setDirty(true)} />
            </section>
            {error ? <p className="login__error" role="alert">{error}</p> : null}
            <div className="jha__actions">
              <button className="btn btn--primary btn--block" onClick={() => void onSubmit()} disabled={busy || !jobId}>
                {busy ? "Submitting…" : amendsUuid ? "Submit amendment" : "Submit"}
              </button>
            </div>
          </>
        ) : (
          <p className="muted">Pick a job and form to begin.</p>
        )}
      </main>
    </div>
  );
}

type PdfPhase = "idle" | "preparing" | "ready" | "error";

/**
 * "Make available for download" → canonical PDF download (PR-4 Part A).
 *
 * Request-driven: a click POSTs requestPdf (flips the server "cache this" flag), then
 * we poll pdfStatus every 5s until the Mac daemon has uploaded every chunk and the
 * cache is `ready`. The poll mirrors components/PublishMonitor.tsx — a recursive
 * setTimeout guarded by an `active` flag with a useRef(timer) and cleanup, so an
 * unmount (e.g. "Submit another") cancels the in-flight poll. The download itself is a
 * same-origin navigation (downloadPdf): the cookie rides automatically and the Worker's
 * Content-Disposition: attachment makes the browser save rather than navigate away.
 */
function PdfDownload({ uuid }: { uuid: string }) {
  const [phase, setPhase] = useState<PdfPhase>("idle");
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Poll only while preparing; stop the moment the cache is ready (or on unmount).
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
        // Transient status error: keep polling. A hard failure surfaces only from the
        // initial requestPdf click (below), never from a single dropped poll.
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

  const onRequest = useCallback(async () => {
    setPhase("preparing");
    try {
      // The flip is idempotent; whether it returns ready:true (already cached) or
      // false (just queued), the poll's first tick fetches status (which also carries
      // expires_at) and settles the UI — so we don't branch on the result here.
      await api.requestPdf(uuid);
    } catch {
      setPhase("error");
    }
  }, [uuid]);

  if (phase === "preparing") {
    return <p className="muted" role="status">Preparing… (usually under 2 min)</p>;
  }

  if (phase === "ready") {
    const until = expiresAt ? new Date(expiresAt * 1000).toLocaleString() : null;
    return (
      <div className="jha__actions">
        <button className="btn btn--primary" onClick={() => api.downloadPdf(uuid)}>
          Download{until ? ` (available until ${until})` : ""}
        </button>
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="jha__actions">
        <p className="login__error" role="alert">Couldn’t prepare the download.</p>
        <button className="btn btn--secondary" onClick={() => void onRequest()}>Try again</button>
      </div>
    );
  }

  return (
    <div className="jha__actions">
      <button className="btn btn--secondary" onClick={() => void onRequest()}>
        Make available for download
      </button>
    </div>
  );
}
