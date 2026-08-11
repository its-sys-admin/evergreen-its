import { useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import * as api from "../lib/api";
import * as jobs from "../lib/fieldops_jobtracker";
import {
  fetchDailyFormStatus,
  fetchDailyRequirements,
  type DailyFormStatus,
  type DailyRequirementItem,
} from "../lib/fieldops_daily_form";
import {
  fetchExpectedMaterials,
  type ExpectedMaterialRow,
} from "../lib/fieldops_expected_materials";
import { formCatalog, getDefinition, resolveFormTarget } from "../forms/registry";
import {
  FormRenderer,
  initialValues,
  seedRequirementResponses,
  type ExpectedMaterialsAdapter,
  type FormLinkAdapter,
  type FormValues,
} from "../forms/FormRenderer";
import { useSubmissionId } from "../pages/useSubmissionId";
import type { FormPrefill } from "../pages/FormFillPage";
import { useAuth } from "../lib/auth";
import {
  ROSTER_LINK_COPY,
  SectionError,
  SectionLoading,
  SectionRefreshWarn,
  errMsg,
  fmtDate,
  pacificToday,
} from "./myTasksShared";

// ─────────────────────────────────────────────────────────────────────────────
// SOP daily form (slice D2) — the Daily tab IS the form now.
//
// Replaces DailyChecklistSection (the R2 checkbox checklist): the daily SOP content lives in the
// daily-report-v2 FORM DEFINITION (guidance + form_link sections, slice D1), rendered INLINE here
// through the SAME machinery the generic fill page uses — FormRenderer + initialValues (the shared
// rendering contract), useSubmissionId (lost-ACK idempotency), api.submitForm (the send-free
// /api/submit path) and api.fetchRecent (the amend-prefill lookup). What is deliberately NOT reused
// is FormFillPage's page shell (job/form pickers, admin submit-as, the Submitted receipt screen):
// the tab has a FIXED envelope (job from the actor's placement, date from the selector below) and
// stays in place after filing, so it carries its own thin state machine instead.
//
// Placement (CS4 #12 waterfall collapse): the actor's job arrives as a PROP — the parent's ONE
// /tasks/mine read now carries viewer_placement (the caller's own placement, resolved server-side),
// so the tab no longer downloads a full Job Tracker list page (the old fetchJobList("active") →
// viewer_current_job stage 1). fetchJobList stays the Job Tracker page's own source. The R1/R2
// empty-state reasons are re-derived: role ∉ {manager, admin} (session) → the not-a-crew-lead
// copy (directive 2026-07-03: a placed ADMIN files the daily report exactly like a placed
// manager; a subcontractor never does — the parent also hides the Daily tab for submitters, so
// this in-component gate is the mounted-panel defense, and the Worker re-gates every daily
// surface on the same role set); parent-reported linked:false (/tasks/mine) → the roster-link
// copy; placed nowhere → the not-placed copy. The parent fetch's loading/error ride down too
// (linked === null = in flight; placementError + onRetryPlacement = the never-silent error +
// Retry surface).
//
// Prefill (best-effort, NEVER a blocker): the job detail (cap.jobtracker.read) seeds
// crew_progress rows from the placed crew, equipment_on_site rows from equipment-on-site, and
// prepared_by from the viewer's roster name. A detail failure → empty tables + a soft warn.
//
// form_link deep-links ride the R3 openForm machinery (App captures the originating view; the
// submitted form returns here), and the live "Filed ✓ <time> by <name>" indicators + the
// "already filed" banner come from GET /api/fieldops/daily-form/status (family-matched
// server-side; display-name-only attribution — the W9 posture).
//
// Never-silent (the R2 bar): distinct loading / error+Retry for placement; soft warns for a failed
// status read or prefill; inline submit errors with the button re-enabled.
// ─────────────────────────────────────────────────────────────────────────────

/** The daily-report PARENT family (catalog.json) — the tab renders its CURRENT version. */
const DAILY_REPORT_PARENT = "daily-report";

/** Draft-write debounce window (ms). One sessionStorage write at most per window, carrying the
 *  LATEST values; a pending write flushes early on unmount or a (job, date) key change. */
const DRAFT_DEBOUNCE_MS = 500;

const NOT_MANAGER_COPY =
  "The daily report is for crew-lead managers who are placed on a job — it doesn't apply to this account.";
const NOT_PLACED_COPY =
  "You're not placed on a job yet — ask the office to place you. Your daily report appears once you're placed.";

/** What the tab reports up after each placement load (drives the parent's auto-tab-switch +
 *  Add-crew placement hint + Log-time deep-link — the same duties the old checklist instance had). */
export interface DailyPlacement {
  job_id: string;
  project_name: string | null;
}

/** Epoch seconds → localized short time (the filed-indicator timestamp). */
function fmtEpochTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function DailyReportTab({
  linked,
  placement: placementProp,
  placementError = null,
  onRetryPlacement,
  onOpenForm,
  onOpenMaterials,
  refreshToken = 0,
  onLoaded,
}: {
  /** /tasks/mine `linked` from the parent (null while that fetch is in flight) — disambiguates
   *  "no placement" into unlinked-account vs not-placed copy without an extra fetch. */
  linked: boolean | null;
  /** (CS4 #12) The viewer's own placement from the parent's /tasks/mine read (viewer_placement) —
   *  null while in flight (linked === null) or when the viewer is unlinked/unplaced. Replaces the
   *  tab's own fetchJobList stage. */
  placement: DailyPlacement | null;
  /** The parent /tasks/mine fetch's error (never-silent: with no placement landed, the tab shows
   *  it with a working Retry instead of a lying empty state). */
  placementError?: string | null;
  /** Retry for the parent fetch (wired to the parent's load()). */
  onRetryPlacement?: () => void;
  /** R3 deep-link opener (App.openForm) — absent renders the form_link buttons disabled. */
  onOpenForm?: (p: FormPrefill) => void;
  /** PR2 — "Materials tracking →" out of the material-receipt region into the per-job Materials
   *  page. Absent (no cap.materials.receive) → the section renders without the button. */
  onOpenMaterials?: (jobId: string) => void;
  /** Bump to refetch the filed status + per-job sections (page Refresh / focus — the parent owns
   *  the trigger; the placement itself refreshes with the parent's own /tasks/mine refetch). */
  refreshToken?: number;
  /** Reports each placement resolution up ({ placement: null } for a non-manager / unplaced actor). */
  onLoaded?: (info: { placement: DailyPlacement | null }) => void;
}) {
  const { user } = useAuth();
  // Directive 2026-07-03: manager OR admin (the placed-admin case) — never submitter.
  const canFileDaily = user?.role === "manager" || user?.role === "admin";

  // ── The form definition: the daily-report parent's CURRENT version (v2 today; robust to a v3). ──
  // Resolved once — the catalog + definitions are build-time bundles.
  const [def] = useState(() => {
    const parent = formCatalog().find((p) => p.parent_form_code === DAILY_REPORT_PARENT);
    return parent?.form_code ? getDefinition(parent.form_code) : null;
  });

  // Project-name enrichment from the job DETAIL read, applied only when the placement prop's
  // project_name is null (the soft-ref edge: current_job names a job the jobs table lost) and
  // only for the job it was fetched for (a stale enrichment must never dress a new placement).
  const [detailName, setDetailName] = useState<{ job: string; name: string } | null>(null);
  const [prefillWarn, setPrefillWarn] = useState<string | null>(null);

  // The RESOLVED placement the rest of the tab consumes: the parent-provided viewer placement,
  // with a detail-read project-name fill for the rare null-name case (see detailName above).
  const placement: DailyPlacement | null = useMemo(() => {
    if (!canFileDaily || !placementProp) return null;
    return {
      job_id: placementProp.job_id,
      project_name:
        placementProp.project_name ??
        (detailName?.job === placementProp.job_id ? detailName.name : null),
    };
  }, [canFileDaily, placementProp, detailName]);

  const [date, setDate] = useState(pacificToday());
  const dateRef = useRef(date);
  dateRef.current = date;
  const [values, setValues] = useState<FormValues>(() => (def ? initialValues(def) : {}));
  const [amendsUuid, setAmendsUuid] = useState<string | null>(null);
  const [prefillable, setPrefillable] = useState<api.RecentSubmission | null>(null);

  const [status, setStatus] = useState<DailyFormStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  // ── Per-job requirements (slice D4) — the admin-authored D1 overlay rendered inside the
  // form's job_requirements section. Per JOB (not per date); the answers live in values under
  // the definition's section key, so the draft/submit machinery carries them for free.
  const [requirements, setRequirements] = useState<DailyRequirementItem[] | null>(null);
  const [reqError, setReqError] = useState<string | null>(null);
  // The section's value key, read from the definition (daily-report-v4: "job_requirements").
  const reqSection = def?.sections.find((s) => s.type === "job_requirements");
  const reqKey = reqSection && "key" in reqSection ? reqSection.key : "job_requirements";

  // ── Expected materials — a COUNT SUMMARY + deep link, per JOB (not per date).
  //
  // The section's third contract in four days, each an operator decision worth recording:
  // v5/v6 filed no values ("don't snapshot mutable state"); v7 (#45) inverted that and
  // filed the day's list as a snapshot; and on 2026-08-11 — the first day real BOMs put
  // hundreds of lines behind this section — the operator cut the inline list entirely:
  // the daily form shows the day's SHAPE (counts) and deep-links the Materials page,
  // which owns every action (the two-tap three-way mark, Report-a-problem, resolve).
  // The rows are still fetched for the counts; NO values are seeded, so a new filing's
  // PDF renders the classic note line (form_pdf's absent-key path) while already-filed
  // v7 snapshots keep rendering as tables.
  const [expectedRows, setExpectedRows] = useState<ExpectedMaterialRow[] | null>(null);
  const [expectedError, setExpectedError] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [justFiled, setJustFiled] = useState(false);
  // Stable across retries (lost-ACK idempotency, same hook as the fill page); renewed on success.
  const { submissionUuid, renew: renewSubmissionId } = useSubmissionId();

  // Ref'd so effects don't re-fire when the parent re-renders with a new inline callback.
  const onLoadedRef = useRef(onLoaded);
  onLoadedRef.current = onLoaded;
  // The last successfully built prefill (crew/equipment/prepared_by) — re-applied on the
  // post-submit reset so "file another" starts from the same seeded tables.
  const lastPrefill = useRef<FormValues>({});
  // Any field touched since load/submit (a ref — read inside the prefill effect without re-firing
  // it): a detail-arriving prefill must never clobber typed work.
  const dirtyRef = useRef(false);

  // Field edits go through this wrapper so the dirty flag tracks real typing only.
  const editValues: Dispatch<SetStateAction<FormValues>> = (v) => {
    dirtyRef.current = true;
    setValues(v);
  };

  // ── Draft persistence (D2 regression BLOCK fix; write path debounced + photo-stripped) ──────────
  // A form_link deep-link navigates away and UNMOUNTS this tab (App swaps its single page node), so
  // pure component state = the manager's whole typed day silently lost on "Create Incident Report".
  // Drafts persist per (job, date) in sessionStorage, restore on mount / date switch (winning over
  // prefill), and clear on successful submit. Storage failures are non-fatal (quota / private
  // mode) — worst case reverts to pre-fix behavior.
  //
  // WRITE PATH (perf, optimization #1): the first cut re-serialized the FULL values — including
  // base64 photo data URLs — on EVERY keystroke: a megabytes-per-keypress JSON.stringify plus a
  // near-guaranteed ~5 MB sessionStorage quota blowout that silently killed the very protection
  // this exists for. Now:
  //   • writes are DEBOUNCED (DRAFT_DEBOUNCE_MS): at most one write per window, always carrying
  //     the LATEST values via pendingDraftRef;
  //   • a pending write FLUSHES synchronously on unmount (the form_link deep-link unmount is
  //     exactly the loss-moment the draft protects) and whenever the (job, date) key changes
  //     mid-window (a date switch must not drop the old date's last keystrokes);
  //   • photo-typed keys are STRIPPED from the persisted draft — photos are re-attachable, typed
  //     text is not. HONEST REGRESSION, on purpose: a draft-restore drops attached-but-unsubmitted
  //     photos (they re-seed to the initialValues [] and must be re-attached). That small visible
  //     loss is the deliberate trade against a quota failure that silently loses the WHOLE day.
  // readDraft / clearDraft / draft-wins-over-prefill semantics are unchanged.
  const readDraft = (job: string, d: string): FormValues | null => {
    try {
      const raw = sessionStorage.getItem(`its-daily-draft:${job}:${d}`);
      return raw ? (JSON.parse(raw) as FormValues) : null;
    } catch {
      return null;
    }
  };
  const clearDraft = (job: string, d: string) => {
    try {
      sessionStorage.removeItem(`its-daily-draft:${job}:${d}`);
    } catch {
      /* non-fatal */
    }
  };
  // The definition's photo header-field keys — stripped from every persisted draft (see above).
  const photoDraftKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const s of def?.sections ?? []) {
      if (s.type === "header") {
        for (const f of s.fields) if (f.input === "photo") keys.add(f.key);
      }
    }
    return keys;
  }, [def]);
  // The one not-yet-written draft (latest values for the (job, date) it names) + its window timer.
  const pendingDraftRef = useRef<{ job: string; date: string; values: FormValues } | null>(null);
  const draftTimerRef = useRef<number | null>(null);
  const writeDraft = (job: string, d: string, vals: FormValues) => {
    try {
      const persistable = Object.fromEntries(
        Object.entries(vals).filter(([k]) => !photoDraftKeys.has(k)),
      );
      sessionStorage.setItem(`its-daily-draft:${job}:${d}`, JSON.stringify(persistable));
    } catch {
      /* non-fatal (quota / private mode) */
    }
  };
  // Write the pending draft NOW (unmount / key-change / window elapsed).
  const flushDraft = () => {
    const p = pendingDraftRef.current;
    pendingDraftRef.current = null;
    if (p) writeDraft(p.job, p.date, p.values);
  };
  useEffect(() => {
    // A pending write whose (job, date) no longer matches must flush under ITS OWN key first —
    // e.g. a date switch inside the debounce window: the old date's typed work stays under the
    // old date's draft (onDateChange relies on this having happened).
    const p = pendingDraftRef.current;
    if (p && (!placement || p.job !== placement.job_id || p.date !== date)) flushDraft();
    if (!placement || !dirtyRef.current) return;
    pendingDraftRef.current = { job: placement.job_id, date, values };
    if (draftTimerRef.current === null) {
      // Bare setTimeout (not window.setTimeout): identical in the browser, and resolves through
      // the patchable global so vi.useFakeTimers() can drive the window in tests.
      draftTimerRef.current = setTimeout(() => {
        draftTimerRef.current = null;
        flushDraft();
      }, DRAFT_DEBOUNCE_MS);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values, placement, date]);
  // Unmount = the App page-node swap (the form_link deep-link) — THE loss-moment this draft
  // exists for. Flush the pending write synchronously; a lost debounce window here would
  // reintroduce the D2 data-loss bug. (Refs + writeDraft's captures are all render-stable, so
  // the first-render closure this []-effect keeps is current forever.)
  useEffect(() => {
    return () => {
      if (draftTimerRef.current !== null) {
        clearTimeout(draftTimerRef.current);
        draftTimerRef.current = null;
      }
      flushDraft();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // R3-F9: mobile Safari tab-kill / cross-page navigation never runs the unmount cleanup above —
  // `pagehide` is the last reliable signal on those paths. flushDraft is naturally
  // duplicate-guarded (it nulls pendingDraftRef before writing), so pagehide firing alongside the
  // debounce timer or the unmount cleanup can never double-write. Same first-render-closure
  // stability argument as the unmount effect (refs + writeDraft captures are render-stable).
  useEffect(() => {
    window.addEventListener("pagehide", flushDraft);
    return () => window.removeEventListener("pagehide", flushDraft);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Placement (the envelope job) — parent-provided (CS4 #12), reported up as it resolves. ──────
  // Mirrors the retired loadPlacement's reporting contract: a daily-ineligible role reports
  // { placement: null } immediately (so the parent's auto-switch settles); a manager/admin
  // reports once the parent fetch LANDS (linked !== null) — resolved placement or null — and
  // again whenever it changes (incl. the detail-read name enrichment). While the parent fetch is
  // in flight or failed with nothing landed, nothing is reported — exactly the old
  // load-in-flight / load-failed silence.
  useEffect(() => {
    if (!canFileDaily) {
      onLoadedRef.current?.({ placement: null });
      return;
    }
    if (placement) {
      onLoadedRef.current?.({ placement });
      return;
    }
    if (linked !== null) onLoadedRef.current?.({ placement: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canFileDaily, linked, placement?.job_id, placement?.project_name]);

  // ── Best-effort prefill from the job detail (crew / equipment / prepared_by). ──────────────────
  const placedJob = placement?.job_id ?? null;
  useEffect(() => {
    if (!placedJob || !def) return;
    let active = true;
    (async () => {
      try {
        const d = await jobs.fetchJobDetail(placedJob);
        if (!active) return;
        setPrefillWarn(null);
        // Fill a project-name miss from the detail header (job-keyed, applied by the resolved-
        // placement memo above; the report-up effect re-fires on the enrichment automatically).
        setDetailName({ job: placedJob, name: d.job.project_name });
        const seeded: FormValues = {};
        if (d.viewer_personnel?.name) seeded.prepared_by = d.viewer_personnel.name;
        if (d.job.crew.length > 0) {
          seeded.crew_progress = d.job.crew.map((m) => ({
            crew_subcontractor: m.trade ? `${m.name} (${m.trade})` : m.name,
            manpower: "",
            todays_progress: "",
          }));
        }
        if (d.job.equipment_on_site.length > 0) {
          seeded.equipment_on_site = d.job.equipment_on_site.map((e) => ({
            equipment_type: e.identifier ? `${e.name} (${e.identifier})` : e.name,
            owner_rental: "",
          }));
        }
        // MERGE into the seed store (D4): the requirements loader contributes its own seed key —
        // neither loader may clobber the other's contribution.
        lastPrefill.current = { ...lastPrefill.current, ...seeded };
        // A persisted draft (typed work from before an unmount) WINS over the prefill; otherwise
        // apply the seed only over a pristine form — never clobber typed work on a refresh. The
        // seed store spreads UNDER the draft so keys the draft predates (e.g. the D4
        // requirements array in a pre-D4 draft) are still seeded — the draft wins where present.
        const draft = readDraft(placedJob, dateRef.current);
        if (draft) {
          dirtyRef.current = true;
          // LIVE photo values ride on top (2026-07-03 field bug): photo keys are STRIPPED from
          // every persisted draft (the quota trade above), so a draft application must never
          // touch them — re-seeding them from initialValues wiped a just-attached photo whenever
          // this effect re-fired (the file picker's own close→focus bumps refreshToken via the
          // parent's onWake). On a fresh mount the live values are still the [] seed, so the
          // documented honest regression (photos don't survive an unmount) is unchanged.
          setValues((cur) => ({
            ...initialValues(def),
            ...lastPrefill.current,
            ...draft,
            ...Object.fromEntries([...photoDraftKeys].filter((k) => k in cur).map((k) => [k, cur[k]])),
          }));
        } else if (!dirtyRef.current) setValues({ ...initialValues(def), ...lastPrefill.current });
      } catch {
        // Best-effort by design (spec): failure = empty tables + a soft warn, never a blocker.
        if (active) {
          setPrefillWarn(
            "Couldn't prefill crew and equipment from the Job Tracker — the tables start blank. Refresh to retry.",
          );
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [placedJob, def, refreshToken]);

  // ── Per-job requirements (D4) — fetch + seed. Never a blocker: a failure soft-warns with a
  // Retry (the R2 never-silent bar) and the form still works (no prop → the section renders
  // nothing). Success seeds values[reqKey] with the self-describing all-empty answers array so a
  // zero-interaction submission still files what it displayed; a draft / amend-load already
  // carrying answers wins (merge-if-absent).
  async function loadRequirements(jobId: string) {
    try {
      const items = await fetchDailyRequirements(jobId);
      setRequirements(items);
      setReqError(null);
      const seed = seedRequirementResponses(items);
      lastPrefill.current = { ...lastPrefill.current, [reqKey]: seed };
      setValues((v) => (v[reqKey] === undefined ? { ...v, [reqKey]: seed } : v));
    } catch (err) {
      setReqError(errMsg(err, "Couldn't load this job's added requirements."));
    }
  }

  useEffect(() => {
    if (!placedJob) return;
    let active = true;
    void (async () => {
      // Wrapped like the status effect so a stale (job-switched) response can't land.
      try {
        const items = await fetchDailyRequirements(placedJob);
        if (!active) return;
        setRequirements(items);
        setReqError(null);
        const seed = seedRequirementResponses(items);
        lastPrefill.current = { ...lastPrefill.current, [reqKey]: seed };
        setValues((v) => (v[reqKey] === undefined ? { ...v, [reqKey]: seed } : v));
      } catch (err) {
        if (active) setReqError(errMsg(err, "Couldn't load this job's added requirements."));
      }
    })();
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placedJob, refreshToken]);

  // ── Expected materials (M2) — fetch. Never a blocker: a failure soft-warns with a Retry (the
  // R2 never-silent bar) and the form still works (no adapter → the section renders nothing,
  // never a lying "no expected materials" empty state).
  async function loadExpectedMaterials(jobId: string) {
    try {
      const d = await fetchExpectedMaterials(jobId);
      setExpectedRows(d.expected_materials);
      setExpectedError(null);
    } catch (err) {
      setExpectedError(errMsg(err, "Couldn't load this job's expected materials."));
    }
  }

  useEffect(() => {
    if (!placedJob) return;
    let active = true;
    void (async () => {
      // Wrapped like the status/requirements effects so a stale (job-switched) response can't land.
      try {
        const d = await fetchExpectedMaterials(placedJob);
        if (!active) return;
        setExpectedRows(d.expected_materials);
        setExpectedError(null);
      } catch (err) {
        if (active) setExpectedError(errMsg(err, "Couldn't load this job's expected materials."));
      }
    })();
    return () => {
      active = false;
    };
  }, [placedJob, refreshToken]);

  // (The v7 snapshot-seeding effect lived here until 2026-08-11 — the deep-link-card
  // decision removed it; see the expected-materials comment block above.)

  // ── Filed status (the form_link indicators + the filed banner) + the amend candidate. ──────────
  async function loadStatus(jobId: string, forDate: string) {
    try {
      const s = await fetchDailyFormStatus(jobId, forDate);
      setStatus(s);
      setStatusError(null);
    } catch (err) {
      // The form stays fillable — the indicators are additive. But never silent (R2 bar).
      setStatusError(errMsg(err, "Couldn't check what's already been filed."));
    }
  }

  useEffect(() => {
    if (!placedJob) return;
    let active = true;
    void (async () => {
      // Wrapped so a stale (job/date-switched) response can't land: loadStatus sets state directly
      // on the manual-retry path, but the effect path re-checks `active` via this shim.
      try {
        const s = await fetchDailyFormStatus(placedJob, date);
        if (!active) return;
        setStatus(s);
        setStatusError(null);
      } catch (err) {
        if (active) setStatusError(errMsg(err, "Couldn't check what's already been filed."));
      }
    })();
    // The amend candidate (the existing fill-page machinery: fetchRecent → "Load & amend it").
    setPrefillable(null);
    if (def) {
      api
        .fetchRecent(placedJob, def.form_code, date)
        .then((r) => {
          if (active) setPrefillable(r);
        })
        .catch(() => {});
    }
    return () => {
      active = false;
    };
  }, [placedJob, date, def, refreshToken]);

  // ── The R2-built empty / loading / error states (mutually exclusive). ───────────────────────────
  if (!canFileDaily) {
    return (
      <section className="card dash-section" aria-label="Daily report status">
        <h3 className="dash-detail__h2">Daily report</h3>
        <div className="dash-unavail">{NOT_MANAGER_COPY}</div>
      </section>
    );
  }
  if (!def) {
    // A packaging regression (the daily-report definition missing from the bundle) must fail LOUD.
    return (
      <section className="card dash-section" aria-label="Daily report status">
        <h3 className="dash-detail__h2">Daily report</h3>
        <div className="banner banner--err" role="alert">
          The Daily Report form definition failed to load — tell the office. This is a portal build
          problem, not something you can fix in the field.
        </div>
      </section>
    );
  }
  // Parent fetch not yet landed (linked === null): distinct error+Retry / loading states (the
  // never-silent bar). Once landed, an unplaced viewer gets the explanatory copy even if a later
  // REFRESH fails — the parent's own refresh-warn covers that path.
  if (!placement && linked === null) {
    if (placementError) {
      return (
        <SectionError
          message={placementError}
          onRetry={() => onRetryPlacement?.()}
          what="loading your daily report"
        />
      );
    }
    return <SectionLoading label="Loading your daily report…" />;
  }
  if (!placement) {
    return (
      <section className="card dash-section" aria-label="Daily report status">
        <h3 className="dash-detail__h2">Daily report</h3>
        <div className="dash-unavail">{linked === false ? ROSTER_LINK_COPY : NOT_PLACED_COPY}</div>
      </section>
    );
  }

  const today = pacificToday();
  const isToday = date === today;
  const filedForDate = status?.daily_filed ?? null;
  const validDate = /^\d{4}-\d{2}-\d{2}$/.test(date);

  function onDateChange(next: string) {
    // max= guards the picker UI; this guards typed input. Empty/future → clamp to today.
    const clamped = /^\d{4}-\d{2}-\d{2}$/.test(next) && next <= pacificToday() ? next : pacificToday();
    setDate(clamped);
    // A date switch starts a NEW submission for that date. Typed work stays under ITS OWN date's
    // draft (already persisted, or flushed by the save effect's key-change check on this render);
    // the new date shows its own draft or the seed.
    const draft = placement && def ? readDraft(placement.job_id, clamped) : null;
    if (draft && def) {
      dirtyRef.current = true;
      setValues({ ...initialValues(def), ...lastPrefill.current, ...draft });
    } else if (def) {
      dirtyRef.current = false;
      setValues({ ...initialValues(def), ...lastPrefill.current });
    }
    setAmendsUuid(null);
    setJustFiled(false);
    setSubmitError(null);
  }

  function loadAmend() {
    if (!prefillable || !def) return;
    dirtyRef.current = true; // amend-loaded values are the manager's work — persist as a draft
    // The seed store spreads UNDER the filed values: keys the filing predates (the D4
    // requirements array) get the current seed; everything the submission carried wins.
    setValues({ ...initialValues(def), ...lastPrefill.current, ...prefillable.values });
    setAmendsUuid(prefillable.submission_uuid);
    dirtyRef.current = true; // loaded content counts as work-in-progress (prefill must not clobber it)
    setPrefillable(null);
  }

  async function onSubmit() {
    if (!def || !placement || !validDate || busy) return;
    setBusy(true);
    setSubmitError(null);
    setJustFiled(false);
    try {
      // The standard send-free submit path — the same api.submitForm the fill page uses. No
      // submitted_as: the Daily tab is the manager's own surface (self-submit only).
      await api.submitForm({
        job_id: placement.job_id,
        form_code: def.form_code,
        variant_label: def.variant_label,
        work_date: date,
        values,
        submission_uuid: submissionUuid,
        amends_uuid: amendsUuid,
      });
      setJustFiled(true);
      dirtyRef.current = false;
      // Discard any pending debounced write BEFORE clearing — a live window timer firing after
      // clearDraft would silently resurrect the just-filed values as a stale draft.
      pendingDraftRef.current = null;
      clearDraft(placement.job_id, date);
      setAmendsUuid(null);
      renewSubmissionId(); // fresh id for the NEXT submission (this one succeeded)
      // Reset to a fresh seeded form for a potential file-another/amend pass.
      setValues({ ...initialValues(def), ...lastPrefill.current });
      // Reflect the filing in the indicators + banner (best-effort; failure soft-warns), and
      // refresh the amend candidate so "Load & amend it" now offers the just-filed submission.
      await loadStatus(placement.job_id, date);
      api
        .fetchRecent(placement.job_id, def.form_code, date)
        .then((r) => setPrefillable(r))
        .catch(() => {});
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : "Submission failed.");
    } finally {
      setBusy(false);
    }
  }

  // The expected_materials section adapter — the deep-link card's counts + link (2026-08-11;
  // every ACTION lives on the Materials page it opens). Supplied only once the read
  // SUCCEEDED: while loading (null) or failed (the soft-warn above carries the Retry) the
  // section renders nothing rather than a lying "no expected materials" empty state.
  const expectedAdapter: ExpectedMaterialsAdapter | undefined =
    expectedRows !== null
      ? {
          rows: expectedRows,
          onOpenMaterials:
            onOpenMaterials && placement ? () => onOpenMaterials(placement.job_id) : undefined,
        }
      : undefined;

  // R3 deep-link adapter for the definition's form_link sections. `open` rides App.openForm (which
  // captures My Tasks as the return target); the indicator label comes from the status read.
  const formLinks: FormLinkAdapter | undefined = onOpenForm
    ? {
        open: (parentFormCode: string) => {
          const target = resolveFormTarget(parentFormCode);
          onOpenForm({
            jobId: placement.job_id,
            parentCode: target.parentCode,
            variantCode: target.variantCode || undefined,
            workDate: date,
          });
        },
        filedLabel: (parentFormCode: string) => {
          const entry = status?.filed[parentFormCode];
          if (!entry) return null;
          return `Filed ✓ ${fmtEpochTime(entry.filed_at)}${entry.filed_by_name ? ` by ${entry.filed_by_name}` : ""}`;
        },
      }
    : undefined;

  const formCard = (
    <>
      {amendsUuid ? (
        <p className="jha__notice">
          <strong>Amending</strong> the submission filed for {fmtDate(date)}.
        </p>
      ) : null}
      <section className="card">
        <FormRenderer
          def={def}
          values={values}
          setValues={editValues}
          formLinks={formLinks}
          requirements={requirements ?? undefined}
          expectedMaterials={expectedAdapter}
          /* DR-photo-pool: the additional_photos mount's SCOPE (job + date + the amend target).
             Everything else — pool uploads, screening-status chips, removal — is self-contained
             in AdditionalPhotosSection; the refs ride `values` through editValues, so the existing
             draft machinery persists them (tiny [{pool_id, caption?}] — never photo bytes).
             amendsUuid lets the amend read resolve the filed report's own claimed rows as
             "Photo on file ✓" instead of "missing" (the Worker verifies it server-side). */
          additionalPhotos={{ jobId: placement.job_id, workDate: date, amendsUuid }}
          /* The daily SOP's chronological day-rail — presentational, Daily tab only
             (the generic fill page renders the same definition without it). */
          dayRail
        />
      </section>
      {submitError ? (
        <p className="login__error" role="alert">
          {submitError}
        </p>
      ) : null}
      <div className="jha__actions">
        <button
          type="button"
          className="btn btn--primary btn--block"
          aria-label="Submit daily report"
          onClick={() => void onSubmit()}
          disabled={busy || !validDate}
        >
          {busy ? "Submitting…" : amendsUuid ? "Submit amendment" : "Submit daily report"}
        </button>
      </div>
    </>
  );

  return (
    <section className="dash-section" aria-label="Daily report">
      <div className="card dash-section">
        <h3 className="dash-detail__h2">
          Daily report
          <span className="dash-card__sub">
            {" "}
            · {placement.project_name ?? placement.job_id} · {placement.job_id}
          </span>
        </h3>
        <label className="field">
          <span className="field__label">Report date</span>
          <input
            className="field__input"
            type="date"
            aria-label="Report date"
            value={date}
            max={today}
            onChange={(e) => onDateChange(e.target.value)}
          />
        </label>

        {statusError && (
          <SectionRefreshWarn
            message={statusError}
            onRetry={() => void loadStatus(placement.job_id, date)}
            what="checking filed forms"
          />
        )}
        {reqError && (
          <SectionRefreshWarn
            message={reqError}
            onRetry={() => void loadRequirements(placement.job_id)}
            what="loading job-specific requirements"
          />
        )}
        {expectedError && (
          <SectionRefreshWarn
            message={expectedError}
            onRetry={() => void loadExpectedMaterials(placement.job_id)}
            what="loading expected materials"
          />
        )}
        {prefillWarn && <div className="dash-unavail" role="status">{prefillWarn}</div>}

        {justFiled && (
          <div className="banner banner--ok" role="status">
            Submitted ✓ — the office will confirm it once it’s filed.
          </div>
        )}
        {filedForDate && (
          <div className="banner banner--ok" role="status" aria-label="Daily report filed">
            Daily report filed ✓ {fmtEpochTime(filedForDate.filed_at)}
            {filedForDate.filed_by_name ? ` by ${filedForDate.filed_by_name}` : ""} for {fmtDate(date)}.
            {prefillable ? (
              <>
                {" "}
                <button type="button" className="btn btn--secondary" onClick={loadAmend}>
                  Load &amp; amend it
                </button>
              </>
            ) : null}
          </div>
        )}
      </div>

      {/* Past dates default to the filed state first: the form collapses behind a disclosure.
          Today's form (and any unfiled past date) renders open. */}
      {!isToday && filedForDate ? (
        <details className="dash-completed">
          <summary className="dash-card__sub" style={{ cursor: "pointer" }}>
            File another or amend for this date
          </summary>
          {formCard}
        </details>
      ) : (
        formCard
      )}
    </section>
  );
}
